import json
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from access.models import ApiKey, Application, access_for

User = get_user_model()

SECRET = 'test-shared-secret'


@mock.patch.dict(os.environ, {'FLEET_MCP_SHARED_SECRET': SECRET})
class ResolveApiKeyTests(TestCase):
    def setUp(self):
        self.estate = Application.objects.create(name='Estate', hostname='estate.example.com')
        self.thread = Application.objects.create(name='Thread', hostname='thread.example.com')
        self.ops = Application.objects.create(name='Ops', hostname='ops.example.com', admin_only=True)
        self.user = User.objects.create_user('u', email='U@Example.com')
        access_for(self.user).apps.add(self.estate)
        self.key, self.raw = ApiKey.issue(self.user, 'laptop')

    def resolve(self, raw, secret=SECRET):
        return self.client.post(
            '/api/keys/resolve', json.dumps({'key': raw}), content_type='application/json',
            headers={'X-Fleet-MCP-Secret': secret},
        )

    def test_only_the_hash_is_stored(self):
        self.assertTrue(self.raw.startswith('afk_'))
        self.assertNotEqual(self.key.key_hash, self.raw)
        self.assertEqual(self.key.hint, self.raw[:12])

    def test_key_resolves_to_owner_and_their_apps(self):
        resp = self.resolve(self.raw)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['email'], 'u@example.com')
        self.assertFalse(body['is_admin'])
        self.assertEqual([a['hostname'] for a in body['apps']], ['estate.example.com'])
        self.key.refresh_from_db()
        self.assertIsNotNone(self.key.last_used_at)

    def test_admin_gets_every_app(self):
        admin = User.objects.create_user('a', email='a@example.com', is_staff=True)
        _, raw = ApiKey.issue(admin, 'ci')
        hosts = {a['hostname'] for a in self.resolve(raw).json()['apps']}
        self.assertEqual(hosts, {'estate.example.com', 'thread.example.com', 'ops.example.com'})

    def test_wrong_secret_is_refused(self):
        self.assertEqual(self.resolve(self.raw, secret='nope').status_code, 403)

    def test_unknown_revoked_or_inactive_is_refused(self):
        self.assertEqual(self.resolve('afk_not-a-real-key').status_code, 401)
        self.assertEqual(self.resolve('not even the prefix').status_code, 401)

        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.resolve(self.raw).status_code, 401)

        self.user.is_active = True
        self.user.save()
        self.key.revoked_at = timezone.now()
        self.key.save()
        self.assertEqual(self.resolve(self.raw).status_code, 401)

    def test_malformed_body_is_rejected(self):
        resp = self.client.post('/api/keys/resolve', 'nope', content_type='application/json',
                                headers={'X-Fleet-MCP-Secret': SECRET})
        self.assertEqual(resp.status_code, 400)

    def test_unconfigured_secret_fails_closed(self):
        with mock.patch.dict(os.environ, {'FLEET_MCP_SHARED_SECRET': ''}):
            self.assertEqual(self.resolve(self.raw, secret='').status_code, 503)


class ApiKeyPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('u', email='u@example.com')
        self.other = User.objects.create_user('o', email='o@example.com')
        self.client.force_login(self.user)

    def test_create_shows_the_key_once(self):
        resp = self.client.post('/keys/', {'action': 'create', 'name': 'laptop'})
        self.assertEqual(resp.status_code, 200)
        raw = resp.context['new_key']
        self.assertContains(resp, raw)
        self.assertEqual(ApiKey.resolve(raw).user, self.user)
        self.assertNotContains(self.client.get('/keys/'), raw)

    def test_name_is_required(self):
        self.client.post('/keys/', {'action': 'create', 'name': '  '})
        self.assertFalse(ApiKey.objects.exists())

    def test_revoke_own_key(self):
        key, raw = ApiKey.issue(self.user, 'laptop')
        self.client.post('/keys/', {'action': 'revoke', 'key': key.pk})
        self.assertIsNone(ApiKey.resolve(raw))

    def test_users_cannot_see_or_revoke_others_keys(self):
        key, raw = ApiKey.issue(self.other, 'theirs')
        self.assertNotContains(self.client.get('/keys/'), 'theirs')
        self.assertEqual(self.client.post('/keys/', {'action': 'revoke', 'key': key.pk}).status_code, 404)
        self.assertIsNotNone(ApiKey.resolve(raw))

    def test_admins_can_revoke_any_key(self):
        admin = User.objects.create_user('a', email='a@example.com', is_staff=True)
        key, raw = ApiKey.issue(self.other, 'theirs')
        self.client.force_login(admin)
        self.assertContains(self.client.get('/keys/'), 'theirs')
        self.client.post('/keys/', {'action': 'revoke', 'key': key.pk})
        self.assertIsNone(ApiKey.resolve(raw))
