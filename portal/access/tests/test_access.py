import os
import time
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from jose import jwk, jwt

from access import cf_access
from access.models import Access, Application, PortalSettings, access_for, can_access, visible_apps

User = get_user_model()

TEAM = 'https://example.cloudflareaccess.com'
AUD = 'test-aud'
ENV = {'CF_ACCESS_TEAM_DOMAIN': TEAM, 'CF_ACCESS_AUD': AUD, 'CF_ACCESS_ADMIN_EMAILS': 'boss@example.com'}

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
PUBLIC_JWK = {
    **jwk.construct(
        _key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo),
        'RS256',
    ).to_dict(),
    'kid': 'k1',
}


def token(email, aud=AUD):
    now = int(time.time())
    return jwt.encode(
        {'email': email, 'aud': [aud], 'iss': TEAM, 'iat': now, 'exp': now + 600},
        PRIVATE_PEM, algorithm='RS256', headers={'kid': 'k1'},
    )


def fake_certs(url, timeout):
    resp = mock.Mock()
    resp.json.return_value = {'keys': [PUBLIC_JWK]}
    return resp


class AccessRuleTests(TestCase):
    def setUp(self):
        self.estate = Application.objects.create(name='Estate', hostname='estate.example.com')
        self.other = Application.objects.create(name='Other', hostname='other.example.com')
        self.hidden = Application.objects.create(name='Hidden', hostname='hidden.example.com', is_active=False)
        self.user = User.objects.create_user('u', email='u@example.com')

    def test_granted_app_only(self):
        access_for(self.user).apps.add(self.estate)
        self.assertTrue(can_access(self.user, self.estate))
        self.assertFalse(can_access(self.user, self.other))
        self.assertEqual(list(visible_apps(self.user)), [self.estate])

    def test_all_apps_covers_active_apps_including_new_ones(self):
        a = access_for(self.user)
        a.all_apps = True
        a.save()
        later = Application.objects.create(name='Later', hostname='later.example.com')
        self.assertTrue(can_access(self.user, later))
        self.assertFalse(can_access(self.user, self.hidden))
        self.assertNotIn(self.hidden, visible_apps(self.user))

    def test_admin_sees_everything(self):
        self.user.is_staff = True
        self.assertTrue(can_access(self.user, self.hidden))
        self.assertEqual(visible_apps(self.user).count(), 3)

    def test_admin_only_ignores_explicit_and_all_app_grants(self):
        self.estate.admin_only = True
        self.estate.save()
        access = access_for(self.user)
        access.apps.add(self.estate)
        for all_apps in (False, True):
            access.all_apps = all_apps
            access.save()
            self.assertFalse(can_access(self.user, self.estate))
            self.assertNotIn(self.estate, visible_apps(self.user))
        self.user.is_staff = True
        self.assertTrue(can_access(self.user, self.estate))
        self.assertIn(self.estate, visible_apps(self.user))
        self.user.is_active = False
        self.assertFalse(can_access(self.user, self.estate))
        self.assertFalse(visible_apps(self.user).exists())

    def test_inactive_user_refused(self):
        a = access_for(self.user)
        a.all_apps = True
        a.save()
        self.user.is_active = False
        self.assertFalse(can_access(self.user, self.estate))

    def test_new_users_default_from_settings(self):
        s = PortalSettings.load()
        s.new_users_all_apps = True
        s.save()
        fresh = User.objects.create_user('fresh', email='fresh@example.com')
        self.assertTrue(access_for(fresh).all_apps)


@mock.patch('access.cf_access.requests.get', side_effect=fake_certs)
@mock.patch.dict(os.environ, ENV)
class AuthCheckTests(TestCase):
    def setUp(self):
        cf_access.JWKS_CACHE.update(keys=None, expires_at=0.0)
        cache.clear()
        self.estate = Application.objects.create(name='Estate', hostname='estate.example.com')

    def check(self, email, host='estate.example.com', **kw):
        headers = {'HTTP_X_FORWARDED_HOST': host}
        if email:
            headers['HTTP_CF_ACCESS_JWT_ASSERTION'] = token(email, **kw)
        return self.client.get('/auth/check', **headers)

    def test_unknown_user_is_provisioned_and_refused(self, _get):
        resp = self.check('new@example.com')
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "access to Estate", status_code=403)
        self.assertTrue(User.objects.filter(email='new@example.com').exists())

    def test_granted_user_allowed(self, _get):
        u = cf_access.ensure_user('ok@example.com')
        u.access.apps.add(self.estate)
        self.assertEqual(self.check('ok@example.com').status_code, 200)

    def test_admin_only_direct_requests_require_active_admin(self, _get):
        self.estate.admin_only = True
        self.estate.save()
        u = cf_access.ensure_user('ok@example.com')
        u.access.apps.add(self.estate)
        u.access.all_apps = True
        u.access.save()
        self.assertEqual(self.check(u.email).status_code, 403)
        u.is_staff = True
        u.save()
        cache.clear()
        self.assertEqual(self.check(u.email).status_code, 200)
        u.is_staff = False
        u.save()
        cache.clear()
        self.assertEqual(self.check(u.email).status_code, 403)
        u.is_staff = True
        u.is_active = False
        u.save()
        cache.clear()
        self.assertEqual(self.check(u.email).status_code, 403)

    def test_host_port_and_case_ignored(self, _get):
        u = cf_access.ensure_user('ok@example.com')
        u.access.apps.add(self.estate)
        self.assertEqual(self.check('ok@example.com', host='Estate.Example.com:443').status_code, 200)

    def test_admin_email_allowed_everywhere_even_unregistered(self, _get):
        self.assertEqual(self.check('boss@example.com').status_code, 200)
        self.assertEqual(self.check('boss@example.com', host='unknown.example.com').status_code, 200)

    def test_unregistered_host_refused_for_users(self, _get):
        resp = self.check('someone@example.com', host='unknown.example.com')
        self.assertContains(resp, 'not registered', status_code=403)

    def test_missing_or_forged_token(self, _get):
        self.assertEqual(self.check(None).status_code, 401)
        self.assertEqual(self.check('x@example.com', aud='other-app').status_code, 401)

    def test_check_does_not_create_sessions(self, _get):
        from django.contrib.sessions.models import Session
        self.check('boss@example.com')
        self.assertEqual(Session.objects.count(), 0)

    def test_decision_cached_then_cleared_by_manage_save(self, _get):
        u = cf_access.ensure_user('ok@example.com')
        self.assertEqual(self.check('ok@example.com').status_code, 403)
        u.access.apps.add(self.estate)
        self.assertEqual(self.check('ok@example.com').status_code, 403)  # still cached
        cache.clear()
        self.assertEqual(self.check('ok@example.com').status_code, 200)


@mock.patch('access.cf_access.requests.get', side_effect=fake_certs)
@mock.patch.dict(os.environ, ENV)
class BrowserSsoTests(TestCase):
    def setUp(self):
        cf_access.JWKS_CACHE.update(keys=None, expires_at=0.0)
        self.admin = User.objects.create_user('sessionadmin', email='sessionadmin@example.com', is_staff=True)
        self.client.force_login(self.admin)

    def test_existing_session_cannot_replace_missing_assertion(self, _get):
        self.assertEqual(self.client.get('/settings/').status_code, 403)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_existing_session_cannot_replace_invalid_assertion(self, _get):
        response = self.client.get('/settings/', HTTP_CF_ACCESS_JWT_ASSERTION=token(self.admin.email, aud='wrong'))
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_valid_assertion_keeps_admin_access_and_secure_cookie(self, _get):
        self.client.logout()
        response = self.client.get('/settings/', HTTP_CF_ACCESS_JWT_ASSERTION=token(self.admin.email))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.cookies['sessionid']['secure'])
        self.assertTrue(response.cookies['csrftoken']['secure'])

    def test_different_identity_does_not_inherit_admin_session(self, _get):
        response = self.client.get('/settings/', HTTP_CF_ACCESS_JWT_ASSERTION=token('ordinary@example.com'))
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(self.client.session['_auth_user_id'], str(self.admin.pk))

    def test_inactive_identity_cannot_keep_previous_admin_session(self, _get):
        User.objects.create_user('disabled', email='disabled@example.com', is_active=False)
        response = self.client.get('/settings/', HTTP_CF_ACCESS_JWT_ASSERTION=token('disabled@example.com'))
        self.assertEqual(response.status_code, 403)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_partial_sso_configuration_fails_closed(self, _get):
        with mock.patch.dict(os.environ, {'CF_ACCESS_AUD': ''}):
            self.assertEqual(self.client.get('/settings/').status_code, 403)

    def test_only_exact_machine_endpoints_are_exempt(self, _get):
        self.assertEqual(self.client.get('/healthz').status_code, 200)
        self.assertEqual(self.client.get('/auth/check').status_code, 401)
        self.assertEqual(self.client.post('/api/keys/resolve').status_code, 503)
        for path in ('/healthz/admin', '/auth/check-extra', '/api/keys/resolve-extra'):
            self.assertEqual(self.client.get(path).status_code, 403)


class ManageTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('admin', email='admin@example.com', password='pw', is_staff=True)
        self.user = User.objects.create_user('user', email='user@example.com', password='pw')
        self.a1 = Application.objects.create(name='One', hostname='one.example.com')
        self.a2 = Application.objects.create(name='Two', hostname='two.example.com')

    def test_non_admin_cannot_manage(self):
        self.client.login(username='user', password='pw')
        self.assertEqual(self.client.get('/manage/users/').status_code, 302)
        self.assertEqual(self.client.get('/manage/apps/').status_code, 302)

    def test_save_access_matrix(self):
        self.client.login(username='admin', password='pw')
        resp = self.client.post('/manage/users/', {
            f'present_{self.admin.pk}': '1', f'present_{self.user.pk}': '1',
            f'active_{self.user.pk}': 'on', f'app_{self.user.pk}_{self.a2.pk}': 'on',
            'new_users_all_apps': 'on',
        })
        self.assertEqual(resp.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(list(self.user.access.apps.all()), [self.a2])
        self.assertFalse(self.user.access.all_apps)
        self.assertTrue(PortalSettings.load().new_users_all_apps)

    def test_cannot_demote_or_deactivate_self(self):
        self.client.login(username='admin', password='pw')
        self.client.post('/manage/users/', {f'present_{self.admin.pk}': '1'})
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_staff and self.admin.is_active)

    def test_add_user_by_email(self):
        self.client.login(username='admin', password='pw')
        self.client.post('/manage/users/', {'action': 'add_user', 'email': 'Pre@Example.com'})
        self.assertTrue(User.objects.filter(email='pre@example.com').exists())
        self.assertTrue(Access.objects.filter(user__email='pre@example.com').exists())

    def test_register_validates_hostname(self):
        self.client.login(username='admin', password='pw')
        resp = self.client.post('/manage/apps/', {
            'name': 'Bad', 'hostname': 'https://bad.example.com/', 'icon': 'B', 'color': '#123456', 'sort_order': 1,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Application.objects.filter(name='Bad').exists())
        self.client.post('/manage/apps/', {
            'name': 'Good', 'hostname': 'good.example.com', 'icon': 'G', 'color': '#123456', 'sort_order': 1,
            'is_active': 'on',
        })
        self.assertTrue(Application.objects.filter(hostname='good.example.com').exists())

    def test_delete_needs_confirmation(self):
        self.client.login(username='admin', password='pw')
        self.client.post(f'/manage/apps/{self.a1.pk}/delete/')
        self.assertTrue(Application.objects.filter(pk=self.a1.pk).exists())
        self.client.post(f'/manage/apps/{self.a1.pk}/delete/', {'confirm': 'yes'})
        self.assertFalse(Application.objects.filter(pk=self.a1.pk).exists())

    def test_home_tiles(self):
        access_for(self.user).apps.add(self.a1)
        self.client.login(username='user', password='pw')
        resp = self.client.get('/')
        self.assertContains(resp, 'one.example.com')
        self.assertNotContains(resp, 'two.example.com')
