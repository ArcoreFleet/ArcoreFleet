from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from hubauthority.models import HubApplication, HubProvision, HubProvisionEvent

User = get_user_model()

class ProvisioningTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('admin', is_staff=True)
        self.alice = User.objects.create_user('alice')
        self.bob = User.objects.create_user('bob')
        self.app = HubApplication.objects.create(app_id='com.arcore.estate', name='ArcoreEstate')

    def grant(self):
        return self.client.post('/hub/provisioning/', {'action': 'grant', 'customer': self.alice.pk, 'application': self.app.pk})

    def test_only_admins_can_register_grant_or_revoke(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.grant().status_code, 302)
        row = HubProvision.objects.get()
        self.client.force_login(self.alice)
        for path, data in [('/hub/provisioning/', {'action': 'register', 'app_id': 'com.arcore.bad', 'name': 'Bad'}),
                           ('/hub/provisioning/', {'action': 'grant', 'customer': self.bob.pk, 'application': self.app.pk}),
                           (f'/hub/provisioning/{row.pk}/revoke/', {})]:
            self.assertEqual(self.client.post(path, data).status_code, 403)
        self.assertEqual(self.client.get('/hub/provisioning/').status_code, 403)
        self.assertEqual(HubApplication.objects.count(), 1)
        self.assertEqual(HubProvision.objects.count(), 1)
        row.refresh_from_db()
        self.assertTrue(row.is_active)

    def test_customer_isolation_and_query_parameter_cannot_select_another_account(self):
        self.client.force_login(self.admin)
        self.grant()
        self.client.force_login(self.alice)
        self.assertContains(self.client.get('/hub/'), 'ArcoreEstate')
        self.assertNotContains(self.client.get('/hub/'), 'Manage app provisioning')
        self.client.force_login(self.bob)
        self.assertNotContains(self.client.get(f'/hub/?customer={self.alice.pk}'), 'ArcoreEstate')
        self.assertContains(self.client.get('/'), 'Your desktop apps')

    def test_revoke_regrant_and_duplicate_grant_are_audited_without_duplicates(self):
        self.client.force_login(self.admin)
        self.grant(); self.grant()
        row = HubProvision.objects.get()
        self.assertEqual(HubProvisionEvent.objects.count(), 1)
        self.assertEqual(self.client.get(f'/hub/provisioning/{row.pk}/revoke/').status_code, 405)
        self.client.post(f'/hub/provisioning/{row.pk}/revoke/')
        self.client.force_login(self.alice)
        self.assertNotContains(self.client.get('/hub/'), 'ArcoreEstate')
        self.client.force_login(self.admin)
        self.grant()
        self.assertEqual(list(HubProvisionEvent.objects.order_by('pk').values_list('action', flat=True)), ['grant', 'revoke', 'grant'])
        self.assertEqual(HubProvision.objects.count(), 1)

    def test_disabled_app_and_customer_cannot_receive_assignments(self):
        self.client.force_login(self.admin)
        self.app.is_active = False
        self.app.save()
        self.grant()
        self.assertFalse(HubProvision.objects.exists())
        self.app.is_active = True
        self.app.save()
        self.alice.is_active = False
        self.alice.save()
        self.grant()
        self.assertFalse(HubProvision.objects.exists())

    def test_csrf_required_for_admin_changes(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post('/hub/provisioning/', {'action':'grant', 'customer':self.alice.pk, 'application':self.app.pk}).status_code,403)

    def test_anonymous_visitors_cannot_read_assignments(self):
        self.assertEqual(self.client.get('/hub/').status_code, 302)
        self.assertEqual(self.client.get('/hub/provisioning/').status_code, 302)
