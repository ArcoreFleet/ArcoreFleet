import base64
import json
import os
import time
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from hubauthority import signing
from hubauthority.models import AuthorityConfigVersion

User = get_user_model()

# Same fixed test keys as ArcoreHub's crates/trust tests.
ROOT = Ed25519PrivateKey.from_private_bytes(bytes([1] * 32))
CONFIG = Ed25519PrivateKey.from_private_bytes(bytes([2] * 32))
PIN = 'sha256/' + base64.b64encode(bytes([7] * 32)).decode()


def delegation(expires_in=90 * 86400, key=CONFIG, environment='dev', authority='arcore-dev'):
    now = int(time.time())
    pub = signing.public_bytes(key.public_key())
    d = {
        'schema': 1, 'kind': 'authority-config-delegation', 'environment': environment,
        'authority_id': authority, 'key_id': signing.key_id(pub), 'public_key': signing.b64(pub),
        'not_before': now - 60, 'expires': now + expires_in,
    }
    return json.dumps(signing.sign(d, ROOT))


def env(**overrides):
    values = {
        'HUB_CONFIG_KEY': base64.b64encode(bytes([2] * 32)).decode(),
        'HUB_CONFIG_DELEGATION': delegation(),
    }
    values.update(overrides)
    return mock.patch.dict(os.environ, values)


def publish_data(**overrides):
    data = {
        'tenant': 'arecorellc',
        'endpoints': 'fleet-control.example-tailnet.ts.net:30443',
        'tls_pins': PIN,
        'validity_days': '30',
        'download_locations': '',
    }
    data.update(overrides)
    return data


class SigningTests(TestCase):
    def test_key_ids_match_the_hub(self):
        # The Hub derives key ids as the first 8 bytes of SHA-256, in hex.
        pub = signing.public_bytes(CONFIG.public_key())
        self.assertEqual(len(signing.key_id(pub)), 16)
        self.assertEqual(signing.key_id(pub), signing.key_id(pub))

    def test_sign_and_verify_round_trip(self):
        signed = signing.sign({'a': 1}, CONFIG)
        self.assertEqual(signing.verify(signed, signing.public_bytes(CONFIG.public_key())), {'a': 1})
        signed['payload'] = signing.b64(b'{"a":2}')
        with self.assertRaises(Exception):
            signing.verify(signed, signing.public_bytes(CONFIG.public_key()))

    def test_signer_requires_matching_unexpired_delegation(self):
        key = base64.b64encode(bytes([2] * 32)).decode()
        self.assertEqual(signing.load_signer(key, delegation()).authority_id, 'arcore-dev')
        other = Ed25519PrivateKey.from_private_bytes(bytes([3] * 32))
        with self.assertRaisesMessage(signing.NotConfigured, 'different key'):
            signing.load_signer(key, delegation(key=other))
        with self.assertRaisesMessage(signing.NotConfigured, 'expired'):
            signing.load_signer(key, delegation(expires_in=-10))
        with self.assertRaises(signing.NotConfigured):
            signing.load_signer('', '')
        with self.assertRaises(signing.NotConfigured):
            signing.load_signer('not base64!', delegation())

    def test_endpoint_and_pin_validation_mirrors_the_hub(self):
        self.assertEqual(signing.parse_endpoint('authority.example.com:8443', False), {'host': 'authority.example.com', 'port': 8443})
        self.assertEqual(signing.parse_endpoint('[2001:db8::1]:443', False), {'host': '2001:db8::1', 'port': 443})
        for bad in ['https://x.com:443', 'x.com', 'UPPER.com:443', 'x.com:0', 'user@x.com:443', 'single:443', '1.2.3:443']:
            with self.assertRaises(ValueError, msg=bad):
                signing.parse_endpoint(bad, True)
        for private in ['10.0.0.5:443', '100.101.102.103:443', '127.0.0.1:443', '[fd00::1]:443']:
            with self.assertRaises(ValueError, msg=private):
                signing.parse_endpoint(private, False)
            signing.parse_endpoint(private, True)
        self.assertEqual(signing.parse_pin(PIN), PIN)
        for bad in ['sha1/abc', 'sha256/notbase64!!', 'sha256/' + base64.b64encode(b'short').decode()]:
            with self.assertRaises(ValueError, msg=bad):
                signing.parse_pin(bad)


class HubPageTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('admin', email='admin@example.com', is_staff=True)
        self.user = User.objects.create_user('user', email='user@example.com')

    def test_admin_publishes_monotonic_signed_versions(self):
        self.client.force_login(self.admin)
        with env():
            resp = self.client.post('/hub/', publish_data())
            self.assertRedirects(resp, '/hub/')
            self.client.post('/hub/', publish_data(endpoints='fleet-control.example-tailnet.ts.net:30444'))
        versions = list(AuthorityConfigVersion.objects.values_list('version', flat=True))
        self.assertEqual(versions, [2, 1])
        latest = AuthorityConfigVersion.latest()
        self.assertEqual(latest.endpoints, [{'host': 'fleet-control.example-tailnet.ts.net', 'port': 30444}])

        # The profile chains root -> delegation -> configuration.
        profile = latest.profile()
        self.assertEqual(profile['format'], 'arcoreprofile')
        d = signing.verify(profile['delegation'], signing.public_bytes(ROOT.public_key()))
        config = signing.verify(profile['config'], base64.b64decode(d['public_key']))
        self.assertEqual(config['version'], 2)
        self.assertEqual(config['authority_id'], 'arcore-dev')
        self.assertEqual(config['environment'], 'dev')
        self.assertLessEqual(config['expires'], d['expires'])
        self.assertEqual(config['tls_pins'], [PIN])

    def test_configuration_never_outlives_its_delegation(self):
        self.client.force_login(self.admin)
        with env(HUB_CONFIG_DELEGATION=delegation(expires_in=3 * 86400)):
            self.client.post('/hub/', publish_data(validity_days='300'))
        config = json.loads(base64.b64decode(json.loads(AuthorityConfigVersion.latest().signed_config)['payload']))
        delegation_expires = json.loads(base64.b64decode(json.loads(AuthorityConfigVersion.latest().signed_delegation)['payload']))['expires']
        self.assertEqual(config['expires'], delegation_expires)

    def test_invalid_input_is_refused_before_signing(self):
        self.client.force_login(self.admin)
        with env():
            resp = self.client.post('/hub/', publish_data(endpoints='100.101.102.103:30443'))
            self.assertContains(resp, 'private address')
            resp = self.client.post('/hub/', publish_data(tls_pins='sha1/abc'))
            self.assertContains(resp, 'pins look like')
            self.client.post('/hub/', publish_data(endpoints='100.101.102.103:30443', allow_private_endpoints='on'))
        self.assertEqual(AuthorityConfigVersion.objects.count(), 1)

    def test_missing_signing_key_blocks_publishing(self):
        self.client.force_login(self.admin)
        with mock.patch.dict(os.environ, {'HUB_CONFIG_KEY': '', 'HUB_CONFIG_DELEGATION': ''}):
            resp = self.client.post('/hub/', publish_data(), follow=True)
        self.assertContains(resp, 'Cannot sign')
        self.assertEqual(AuthorityConfigVersion.objects.count(), 0)

    def test_users_download_but_cannot_publish(self):
        self.client.force_login(self.admin)
        with env():
            self.client.post('/hub/', publish_data())
        self.client.force_login(self.user)
        with env():
            self.assertEqual(self.client.post('/hub/', publish_data()).status_code, 403)
            page = self.client.get('/hub/')
        self.assertContains(page, 'Download ArcoreHub profile')
        self.assertNotContains(page, 'Publish configuration')
        resp = self.client.get('/hub/profile')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/vnd.arcore.profile+json')
        self.assertIn('arcorehub-arcore-dev.arcoreprofile', resp['Content-Disposition'])
        body = json.loads(resp.content)
        self.assertEqual(set(body), {'format', 'version', 'delegation', 'config'})
        self.assertNotIn('HUB_CONFIG_KEY', resp.content.decode())

    def test_download_requires_sign_in(self):
        resp = self.client.get('/hub/profile')
        self.assertEqual(resp.status_code, 302)


@override_settings(ROOT_URLCONF='hubauthority.control_urls')
class ControlPlaneTests(TestCase):
    def test_serves_only_health_and_latest_config(self):
        self.assertEqual(self.client.get('/healthz').content, b'ok')
        self.assertEqual(self.client.get('/v1/config').status_code, 503)
        admin = User.objects.create_user('admin', is_staff=True)
        with env():
            signer = signing.load_signer(os.environ['HUB_CONFIG_KEY'], os.environ['HUB_CONFIG_DELEGATION'])
            for port in (30443, 30444):
                AuthorityConfigVersion.publish(
                    signer, admin, tenant='arecorellc', endpoints=[{'host': 'authority.example.com', 'port': port}],
                    tls_pins=[PIN], allow_private_endpoints=False, validity_days=30, download_locations=[],
                )
        body = self.client.get('/v1/config').json()
        config = json.loads(base64.b64decode(body['config']['payload']))
        self.assertEqual(config['version'], 2)
        # Portal pages are not part of the control plane.
        self.assertEqual(self.client.get('/hub/').status_code, 404)
        self.assertEqual(self.client.get('/admin/').status_code, 404)
