import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from provision import Cloudflare, Kube, ProvisionError, apply, reconcile_oidc, registration, validate
from portal_adapter import configure_portainer, oauth_settings

CONFIG = json.loads((Path(__file__).resolve().parents[1] / 'apps.json').read_text())
WILDCARD = {'allowed_idps': ['google', 'microsoft'], 'policies': [
    {'id': 'policy', 'name': 'Authorized users', 'decision': 'allow', 'precedence': 1,
     'include': [{'email_domain': {'domain': 'example.com'}}], 'require': []},
]}
APP = CONFIG['apps'][1]
STATE = {'app_id': 'app-1', 'client_id': 'client-1', 'client_secret': 'sensitive-client',
         'account_id': 'a' * 32, 'hostname': APP['hostname']}


class ValidationTests(unittest.TestCase):
    def test_catalog_valid_and_read_only_plan_needs_no_credentials(self):
        self.assertEqual(validate(copy.deepcopy(CONFIG)), CONFIG)

    def test_rejects_duplicate_ids_and_hosts(self):
        for field in ('id', 'hostname'):
            config = copy.deepcopy(CONFIG)
            config['apps'][1][field] = config['apps'][0][field]
            with self.assertRaises(ProvisionError):
                validate(config)

    def test_rejects_callbacks_outside_exact_https_origin(self):
        for uri in ('http://portainer.example.com/', 'https://evil.example/',
                    'https://portainer.example.com@evil.example/', 'https://portainer.example.com/#fragment'):
            config = copy.deepcopy(CONFIG)
            config['apps'][1]['redirect_uris'] = [uri]
            with self.assertRaises(ProvisionError):
                validate(config)

    def test_portainer_cannot_be_made_non_admin(self):
        config = copy.deepcopy(CONFIG)
        config['apps'][1]['admin_only'] = False
        with self.assertRaises(ProvisionError):
            validate(config)

    def test_only_allow_deny_policies_and_explicit_idps(self):
        for key, value in [('allowed_idps', []), ('policies', []),
                           ('policies', [{'decision': 'bypass', 'include': [{'everyone': {}}]}])]:
            wildcard = copy.deepcopy(WILDCARD)
            wildcard[key] = value
            with self.assertRaises(ProvisionError):
                registration(APP, wildcard)

    def test_policy_requirements_preserved_and_unknown_fields_rejected(self):
        wildcard = copy.deepcopy(WILDCARD)
        wildcard['policies'][0]['require'] = [{'email': {'email': 'admin@example.com'}}]
        body = registration(APP, wildcard)
        self.assertEqual(body['policies'][0]['require'], wildcard['policies'][0]['require'])
        self.assertNotIn('id', body['policies'][0])
        self.assertEqual(body['saas_app']['grant_types'], ['authorization_code'])
        wildcard['policies'][0]['unrecognized_security_condition'] = True
        with self.assertRaises(ProvisionError):
            registration(APP, wildcard)


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.cf = Mock()
        self.cf.account = 'a' * 32
        self.cf.base = 'https://api.cloudflare.com/client/v4/accounts/' + 'a' * 32 + '/access/apps'
        self.kube = Mock()

    def test_first_creation_persists_secret_before_configuration(self):
        self.kube.secret.return_value = None
        self.cf.call.return_value = {'result': {'id': 'app-1', 'saas_app': {
            'client_id': 'client-1', 'client_secret': 'sensitive-client'}}}
        state = reconcile_oidc(self.cf, self.kube, APP, WILDCARD, CONFIG['team_domain'], [])
        self.assertEqual(self.cf.call.call_args.args, ('POST',))
        self.assertEqual(self.kube.save.call_count, 2)
        self.assertEqual(state['client_secret'], 'sensitive-client')
        self.assertTrue(state['userinfo_url'].endswith('/client-1/userinfo'))

    def test_repeat_uses_existing_client_and_preserves_secret(self):
        self.kube.secret.return_value = copy.deepcopy(STATE)
        self.cf.call.return_value = {'result': {'name': 'Fleet SSO: portainer', 'type': 'saas',
                                               'saas_app': {'client_id': 'client-1'}}}
        state = reconcile_oidc(self.cf, self.kube, APP, WILDCARD, CONFIG['team_domain'], [])
        self.assertEqual([call.args[0] for call in self.cf.call.call_args_list], ['GET', 'PUT'])
        self.assertEqual(state['client_secret'], STATE['client_secret'])
        self.assertNotIn('client_secret', self.cf.call.call_args.args[2]['saas_app'])

    def test_missing_state_never_rotates_or_creates_duplicate(self):
        self.kube.secret.return_value = None
        with self.assertRaises(ProvisionError):
            reconcile_oidc(self.cf, self.kube, APP, WILDCARD, CONFIG['team_domain'], [{'name': 'Fleet SSO: portainer'}])
        self.cf.call.assert_not_called()

    def test_mismatched_account_or_remote_client_refused(self):
        self.kube.secret.return_value = {**STATE, 'account_id': 'b' * 32}
        with self.assertRaises(ProvisionError):
            reconcile_oidc(self.cf, self.kube, APP, WILDCARD, CONFIG['team_domain'], [])
        self.cf.call.assert_not_called()

    def test_preflight_missing_bootstrap_does_not_mutate(self):
        self.cf.applications.return_value = [{'id': 'wildcard', 'domain': CONFIG['wildcard_domain'], 'type': 'self_hosted'}]
        self.cf.call.side_effect = [{'result': WILDCARD}, {'result': WILDCARD['policies']}]
        self.kube.secret.return_value = None
        with self.assertRaises(ProvisionError):
            apply(CONFIG, self.cf, self.kube)
        self.kube.portal.assert_not_called()
        self.kube.save.assert_not_called()
        self.assertTrue(all(call.args[0] == 'GET' for call in self.cf.call.call_args_list))

    def test_cloudflare_pagination_and_no_redirects(self):
        cf = Cloudflare('a' * 32, 'private-token')
        responses = [Mock(status_code=200), Mock(status_code=200)]
        responses[0].json.return_value = {'success': True, 'result': [{'id': 1}], 'result_info': {'total_pages': 2}}
        responses[1].json.return_value = {'success': True, 'result': [{'id': 2}], 'result_info': {'total_pages': 2}}
        cf.session = Mock()
        cf.session.request.side_effect = responses
        self.assertEqual(list(cf.applications()), [{'id': 1}, {'id': 2}])
        self.assertFalse(cf.session.request.call_args.kwargs['allow_redirects'])

    def test_sensitive_api_and_subprocess_errors_are_suppressed(self):
        cf = Cloudflare('a' * 32, 'private-token')
        cf.session = Mock()
        cf.session.request.return_value.status_code = 403
        cf.session.request.return_value.text = 'secret-value'
        with self.assertRaises(ProvisionError) as error:
            cf.call('POST')
        self.assertNotIn('secret-value', str(error.exception))
        with patch('provision.subprocess.run', return_value=Mock(returncode=1, stderr='secret-value')):
            with self.assertRaises(ProvisionError) as error:
                Kube().run(['apply'], 'secret-value')
        self.assertNotIn('secret-value', str(error.exception))

    def test_secret_is_sent_on_stdin_without_plaintext_annotation(self):
        kube = Kube()
        kube.run = Mock()
        kube.save('fleet-sso-portainer', {'client_secret': 'sensitive'})
        args, payload = kube.run.call_args.args
        self.assertIn('--server-side', args)
        self.assertNotIn('sensitive', str(args))
        self.assertEqual(json.loads(payload)['stringData']['client_secret'], 'sensitive')


class PortainerTests(unittest.TestCase):
    def test_oauth_retains_unrelated_settings_and_disables_auto_creation(self):
        oidc = {key: key for key in ['client_id', 'client_secret', 'authorization_url', 'token_url',
                                    'userinfo_url', 'redirect_uri', 'logout_url']}
        current = {'AuthenticationMethod': 1, 'Unrelated': 'keep', 'OAuthSettings': {'DefaultTeamID': 7}}
        actual = oauth_settings(current, oidc)
        self.assertEqual(current['AuthenticationMethod'], 1)
        self.assertEqual(actual['Unrelated'], 'keep')
        self.assertTrue(actual['OAuthSettings']['SSO'])
        self.assertFalse(actual['OAuthSettings']['OAuthAutoCreateUsers'])
        self.assertEqual(actual['OAuthSettings']['UserIdentifier'], 'email')
        # Match Portainer 2.21's public URL generation, which does not split commas.
        from urllib.parse import parse_qs, urlsplit
        login_uri = 'https://provider.example/authorization?scope=' + actual['OAuthSettings']['Scopes']
        scopes = parse_qs(urlsplit(login_uri).query)['scope'][0].split()
        self.assertEqual(scopes, ['openid', 'email', 'profile'])

    def test_portainer_without_admin_emails_does_not_contact_api(self):
        with patch('portal_adapter.requests.Session') as session:
            with self.assertRaises(ValueError):
                configure_portainer({}, {}, [])
        session.assert_not_called()

    def test_portainer_only_provisions_named_admins_and_keeps_internal_recovery(self):
        oidc = {key: key for key in ['client_id', 'client_secret', 'authorization_url', 'token_url',
                                    'userinfo_url', 'redirect_uri', 'logout_url']}
        calls = []
        def request(method, url, **kwargs):
            calls.append((method, url, kwargs.get('json')))
            data = {}
            if url.endswith('/auth'):
                data = {'jwt': 'private-session'}
            elif url.endswith('/users') and method == 'GET':
                data = [{'Id': 1, 'Username': 'admin', 'Role': 1}]
            elif url.endswith('/settings') and method == 'GET':
                data = ({'AuthenticationMethod': 3, 'OAuthSettings': {'ClientID': 'client_id'}}
                        if any(c[0] == 'PUT' for c in calls) else {'AuthenticationMethod': 1})
            elif url.endswith('/settings/public'):
                from urllib.parse import urlencode
                data = {'OAuthLoginURI': 'https://provider.example/authorization?' + urlencode({
                    'scope': 'openid email profile', 'client_id': oidc['client_id'],
                    'redirect_uri': oidc['redirect_uri']})}
            response = Mock(status_code=200, content=b'json')
            response.json.return_value = data
            return response
        with patch('portal_adapter.requests.Session') as session:
            session.return_value.request.side_effect = request
            configure_portainer(oidc, {'username': 'admin', 'password': 'private-password'}, ['boss@example.com'])
            session.return_value.cookies.clear.assert_called_once_with()
        users = [c[2] for c in calls if c[0] == 'POST' and c[1].endswith('/users')]
        self.assertEqual([u['Username'] for u in users], ['boss@example.com'])
        self.assertEqual(users[0]['Role'], 1)
        self.assertFalse(any(c[0] == 'DELETE' for c in calls))
        settings = next(c[2] for c in calls if c[0] == 'PUT')
        self.assertFalse(settings['OAuthSettings']['OAuthAutoCreateUsers'])


if __name__ == '__main__':
    unittest.main()
