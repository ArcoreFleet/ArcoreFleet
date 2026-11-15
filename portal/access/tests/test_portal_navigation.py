from django.contrib.auth import get_user_model
from django.test import TestCase

from access.models import MonitoredServer, OperationsPoll


class PortalNavigationTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user('operator', is_staff=True)
        self.user = get_user_model().objects.create_user('viewer')

    def test_home_shortcuts_only_visible_to_staff(self):
        self.client.force_login(self.admin)
        response = self.client.get('/')
        self.assertContains(response, 'Fleet dashboard')
        self.assertContains(response, 'Fleet settings')
        self.assertContains(response, 'href="/settings/"')
        self.client.force_login(self.user)
        response = self.client.get('/')
        self.assertNotContains(response, 'href="/settings/"')
        self.assertNotContains(response, 'href="/operations/"')

    def test_settings_access_and_server_configuration(self):
        self.assertEqual(self.client.get('/settings/').status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/settings/').status_code, 302)
        self.client.force_login(self.admin)
        server = MonitoredServer.objects.create(name='Oracle', metrics_url='http://192.0.2.10:9100/metrics')
        response = self.client.get('/settings/')
        self.assertContains(response, server.metrics_url)
        self.assertContains(response, f'/operations/servers/{server.pk}/')
        self.assertContains(response, '10 minutes')
        self.assertEqual(self.client.post('/settings/').status_code, 405)

    def test_interval_returns_to_settings(self):
        self.client.force_login(self.admin)
        response = self.client.post('/operations/interval/', {'interval': '5'})
        self.assertRedirects(response, '/settings/')
        self.assertEqual(OperationsPoll.load().interval_minutes, 5)
