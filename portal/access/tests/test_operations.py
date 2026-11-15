import io
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from access import operations
from access.models import Application, DeploymentTarget


class OperationsTests(TestCase):
    def setUp(self):
        self.app = Application.objects.create(name='Fleet', hostname='fleet.example.com', repository='owner/repo')
        self.target = DeploymentTarget.objects.create(application=self.app, name='Production', deployment='fleet')
        self.admin = get_user_model().objects.create_user('admin', is_staff=True)
        self.user = get_user_model().objects.create_user('user')

    def test_staff_only_and_read_only(self):
        self.assertEqual(self.client.get('/operations/').status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/operations/').status_code, 302)
        self.client.force_login(self.admin)
        with mock.patch('access.operations.requests.get') as get:
            response = self.client.get('/operations/')
        self.assertContains(response, 'Fleet / Production')
        self.assertContains(response, 'Never checked')
        get.assert_not_called()
        self.assertEqual(self.client.post('/operations/').status_code, 405)

    def test_old_snapshot_marked_stale_and_escaped(self):
        self.target.checked_at = timezone.now() - timedelta(minutes=16)
        self.target.snapshot = {'workflow': {'error': '<script>bad()</script>'}}
        self.target.save()
        self.client.force_login(self.admin)
        response = self.client.get('/operations/')
        self.assertContains(response, 'stale')
        self.assertContains(response, '&lt;script&gt;')
        self.assertNotContains(response, '<script>bad()')

    def test_environment_configuration_and_repository_changes_reset_observations(self):
        self.target.snapshot = {'workflow': {'data': {'status': 'success'}}}
        self.target.checked_at = timezone.now()
        self.target.save()
        self.client.force_login(self.admin)
        response = self.client.post(f'/manage/apps/{self.app.pk}/', {
            'name': 'Fleet', 'hostname': 'fleet.example.com', 'icon': 'F', 'color': '#123456',
            'sort_order': 1, 'is_active': 'on', 'repository': 'owner/new-repo',
            'targets-TOTAL_FORMS': 1, 'targets-INITIAL_FORMS': 1,
            'targets-0-id': self.target.pk, 'targets-0-name': 'Production',
            'targets-0-branch': 'main', 'targets-0-namespace': 'apps', 'targets-0-deployment': 'fleet',
        })
        self.assertEqual(response.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(self.target.snapshot, {})
        self.assertIsNone(self.target.checked_at)

    @override_settings(FLEET_GITHUB_TOKEN='private-token')
    @mock.patch('access.operations.requests.get')
    def test_workflow_uses_read_only_scoped_endpoint_and_selects_safe_fields(self, get):
        self.target.workflow = 'deploy.yml'
        get.return_value.status_code = 200
        get.return_value.json.return_value = {'workflow_runs': [{
            'id': 42, 'name': 'Deploy', 'status': 'completed', 'conclusion': 'success',
            'head_sha': 'abcdef123', 'updated_at': '2026-10-20', 'html_url': 'javascript:bad()',
        }]}
        result = operations.workflow(self.target)
        self.assertEqual(result['url'], 'https://github.com/owner/repo/actions/runs/42')
        self.assertEqual(result['status'], 'success')
        self.assertNotIn('private-token', str(result))
        self.assertEqual(get.call_args.args[0], 'https://api.github.com/repos/owner/repo/actions/workflows/deploy.yml/runs')
        self.assertEqual(get.call_args.kwargs['params']['branch'], 'main')
        self.assertFalse(get.call_args.kwargs['allow_redirects'])

    @override_settings(FLEET_KUBERNETES_ENABLED=True)
    @mock.patch('access.operations.get_json')
    def test_rollout_requires_observed_generation_and_available_replicas(self, get):
        deployment = {
            'metadata': {'generation': 4},
            'spec': {'replicas': 2, 'template': {'spec': {'containers': [{'name': 'fleet', 'image': 'registry/fleet:abcdef123'}]}}},
            'status': {'observedGeneration': 3, 'replicas': 2, 'readyReplicas': 2, 'updatedReplicas': 2, 'availableReplicas': 2},
        }
        get.return_value = deployment
        with TemporaryDirectory() as directory:
            token = Path(directory) / 'token'
            token.write_text('token')
            with override_settings(FLEET_K8S_TOKEN_FILE=str(token)):
                self.assertEqual(operations.rollout(self.target)['status'], 'Progressing')
                deployment['status']['observedGeneration'] = 4
                ready = operations.rollout(self.target)
                self.assertEqual(ready['status'], 'Ready')
                self.assertEqual(ready['sha'], 'abcdef123')
                deployment['spec']['template']['spec']['containers'][0]['image'] = 'registry/fleet:latest'
                self.assertEqual(operations.rollout(self.target)['sha'], '')
                deployment['status']['availableReplicas'] = 1
                self.assertEqual(operations.rollout(self.target)['status'], 'Progressing')
                deployment['status']['conditions'] = [{'type': 'Progressing', 'status': 'False'}]
                self.assertEqual(operations.rollout(self.target)['status'], 'Failed')

    def test_test_results_match_repository_and_environment_without_storing_env(self):
        runs = [
            {'id': 'other', 'repoUrl': 'https://github.com/owner/other', 'baseUrl': self.app.url},
            {'id': 'staging', 'repoUrl': 'https://github.com/owner/repo', 'baseUrl': 'https://staging.example.com'},
            {'id': 'ours', 'repoUrl': 'https://github.com/owner/repo.git', 'baseUrl': self.app.url,
             'createdAt': '2026-10-20', 'status': 'passed', 'ref': 'abcdef123', 'env': {'SECRET': 'never-store'}},
        ]
        result = operations.tests(self.target, runs)
        self.assertEqual(result['id'], 'ours')
        self.assertNotIn('never-store', str(result))
        self.assertIn('latest 100', operations.tests(self.target, [])['status'])

    def test_observation_failure_preserves_dated_data_without_exposing_exception(self):
        old = {'data': {'status': 'success'}, 'last_success_at': 'yesterday'}
        def fail():
            raise requests.ConnectionError('https://secret-token@example.com')
        result, failed = operations.observe(old, fail)
        self.assertTrue(failed)
        self.assertEqual(result['last_success_at'], 'yesterday')
        self.assertNotIn('secret-token', str(result))

    @mock.patch('access.operations.workflow', side_effect=ValueError('bad'))
    @mock.patch('access.operations.rollout', return_value={'status': 'Ready'})
    @mock.patch('access.operations.test_runs', side_effect=operations.NotConfigured('Not configured'))
    def test_sync_persists_other_sources_and_exits_nonzero_on_failure(self, runs, rollout, workflow):
        with self.assertRaises(CommandError):
            call_command('sync_operations', stdout=io.StringIO())
        self.target.refresh_from_db()
        self.assertEqual(self.target.snapshot['rollout']['data']['status'], 'Ready')
        self.assertIn('error', self.target.snapshot['workflow'])
        self.assertIsNotNone(self.target.checked_at)

    @override_settings(FLEET_BROWSERFORGE_URL='http://browserforge:8434')
    @mock.patch('access.operations.get_json', return_value={'runs': []})
    def test_browserforge_request_is_bounded(self, get):
        self.assertEqual(operations.test_runs(), [])
        get.assert_called_once_with('http://browserforge:8434/runs', params={'limit': 100})
