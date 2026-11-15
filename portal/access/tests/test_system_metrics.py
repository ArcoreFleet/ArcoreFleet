from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.utils import timezone
from prometheus_client.parser import text_string_to_metric_families

from access import collector, system_metrics
from access.models import MonitoredServer, OperationsPoll


def sample(user=100, idle=100, guest=50, steal=0, iowait=10):
    text = f'''
node_cpu_seconds_total{{cpu="0",mode="user"}} {user}
node_cpu_seconds_total{{cpu="0",mode="idle"}} {idle}
node_cpu_seconds_total{{cpu="0",mode="guest"}} {guest}
node_cpu_seconds_total{{cpu="0",mode="steal"}} {steal}
node_cpu_seconds_total{{cpu="0",mode="iowait"}} {iowait}
node_memory_MemTotal_bytes 1000
node_memory_MemAvailable_bytes 250
node_filesystem_size_bytes{{mountpoint="/",device="/dev/vda1"}} 1000
node_filesystem_free_bytes{{mountpoint="/",device="/dev/vda1"}} 400
node_filesystem_avail_bytes{{mountpoint="/",device="/dev/vda1"}} 300
node_uname_info{{nodename="oracle-host"}} 1
'''
    return [s for family in text_string_to_metric_families(text) for s in family.samples]


class MetricsTests(TestCase):
    @mock.patch('access.system_metrics.time.sleep')
    @mock.patch('access.system_metrics.fetch_samples')
    def test_host_usage_excludes_guest_double_count_and_reports_steal_and_available_disk(self, fetch, sleep):
        fetch.side_effect = [sample(), sample(user=120, idle=170, guest=60, steal=5, iowait=15)]
        server = MonitoredServer(name='Oracle', metrics_url='http://100.64.0.1:9100/metrics')
        result = system_metrics.snapshot(server)
        self.assertEqual(result['cpu_percent'], 20.0)
        self.assertEqual(result['steal_percent'], 5.0)
        self.assertEqual(result['cpu_count'], 1)
        self.assertEqual(result['memory_percent'], 75.0)
        self.assertEqual(result['disks'][0]['available'], 300)
        self.assertEqual(result['disks'][0]['percent'], 66.7)
        self.assertEqual(result['hostname'], 'oracle-host')
        sleep.assert_called_once_with(1)

    @mock.patch('access.system_metrics.time.sleep')
    @mock.patch('access.system_metrics.fetch_samples')
    def test_counter_reset_is_not_reported_as_current_usage(self, fetch, sleep):
        fetch.side_effect = [sample(), sample(user=90)]
        with self.assertRaises(ValueError):
            system_metrics.snapshot(MonitoredServer(name='Host', metrics_url='http://host:9100/metrics'))

    @mock.patch('access.system_metrics.time.sleep')
    @mock.patch('access.system_metrics.fetch_samples')
    def test_missing_configured_disk_is_not_silently_omitted(self, fetch, sleep):
        fetch.side_effect = [sample(), sample(user=101)]
        with self.assertRaises(ValueError):
            system_metrics.snapshot(MonitoredServer(name='Host', metrics_url='http://host:9100/metrics', mountpoints='/data'))

    def test_credentials_and_non_absolute_mounts_rejected(self):
        with self.assertRaises(ValidationError):
            MonitoredServer(name='Host', metrics_url='http://user:secret@host:9100/metrics').full_clean()
        with self.assertRaises(ValidationError):
            MonitoredServer(name='Host', metrics_url='http://host:9100/metrics', mountpoints='relative').full_clean()

    @mock.patch('access.system_metrics.requests.get')
    def test_response_size_and_redirects_are_bounded(self, get):
        response = get.return_value.__enter__.return_value
        response.status_code = 200
        response.iter_content.return_value = [b'x' * (2 * 1024 * 1024 + 1)]
        with self.assertRaises(ValueError):
            system_metrics.fetch_samples('http://100.64.0.1:9100/metrics')
        self.assertFalse(get.call_args.kwargs['allow_redirects'])


class PollTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user('operator', is_staff=True)
        self.user = get_user_model().objects.create_user('user')
        self.oracle = MonitoredServer.objects.create(name='Oracle', metrics_url='http://100.64.0.1:9100/metrics')
        self.home = MonitoredServer.objects.create(name='Homelab', metrics_url='http://100.64.0.2:9100/metrics')

    def test_only_admin_post_with_csrf_can_request_a_poll(self):
        self.assertEqual(self.client.post('/operations/poll/').status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post('/operations/poll/').status_code, 302)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/operations/poll/').status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        self.assertEqual(csrf_client.post('/operations/poll/').status_code, 403)

    @mock.patch('access.system_metrics.fetch_samples')
    def test_manual_request_is_queued_coalesced_and_does_not_collect_in_web_worker(self, fetch):
        self.client.force_login(self.admin)
        self.client.post('/operations/poll/', {'server': self.home.pk})
        first = OperationsPoll.load()
        self.client.post('/operations/poll/', {'server': self.oracle.pk})
        second = OperationsPoll.load()
        self.assertEqual(second.requested_server, self.home)
        self.assertEqual(first.requested_at, second.requested_at)
        fetch.assert_not_called()

    def test_only_five_or_ten_minute_automatic_interval_is_allowed(self):
        self.client.force_login(self.admin)
        self.assertEqual(OperationsPoll.load().interval_minutes, 10)
        self.assertEqual(self.client.post('/operations/interval/', {'interval': 1}).status_code, 400)
        self.assertEqual(self.client.post('/operations/interval/', {'interval': 5}).status_code, 302)
        self.assertEqual(OperationsPoll.load().interval_minutes, 5)

    @mock.patch('access.system_metrics.snapshot', return_value={'cpu_percent': 10})
    def test_manual_selection_refreshes_one_host_without_postponing_full_poll(self, snapshot):
        control = OperationsPoll.load()
        previous_full = timezone.now() - timedelta(minutes=2)
        control.full_started_at = previous_full
        control.requested_at = timezone.now()
        control.requested_server = self.home
        control.save()
        self.assertEqual(collector.collect(), 0)
        snapshot.assert_called_once_with(self.home)
        self.oracle.refresh_from_db()
        self.assertIsNone(self.oracle.checked_at)
        self.assertEqual(OperationsPoll.load().full_started_at, previous_full)

    @mock.patch('access.system_metrics.snapshot', return_value={'cpu_percent': 10})
    def test_automatic_collection_obeys_interval_and_skips_disabled_hosts(self, snapshot):
        self.home.enabled = False
        self.home.save()
        control = OperationsPoll.load()
        control.full_started_at = timezone.now()
        control.save()
        self.assertIsNone(collector.collect())
        snapshot.assert_not_called()
        control.full_started_at = timezone.now() - timedelta(minutes=11)
        control.save()
        self.assertEqual(collector.collect(), 0)
        snapshot.assert_called_once_with(self.oracle)

    @mock.patch('access.system_metrics.snapshot')
    def test_failed_server_keeps_dated_data_and_other_servers_still_refresh(self, snapshot):
        self.home.snapshot = {'data': {'cpu_percent': 5}, 'last_success_at': 'yesterday'}
        self.home.save()
        snapshot.side_effect = [ValueError('private endpoint'), {'cpu_percent': 20}]
        self.assertEqual(collector.collect(force=True), 1)  # Alphabetical order: Homelab, Oracle.
        self.home.refresh_from_db()
        self.oracle.refresh_from_db()
        self.assertEqual(self.home.snapshot['last_success_at'], 'yesterday')
        self.assertNotIn('private endpoint', str(self.home.snapshot))
        self.assertEqual(self.oracle.snapshot['data']['cpu_percent'], 20)

    @mock.patch('access.system_metrics.snapshot')
    def test_active_lease_prevents_overlapping_collectors(self, snapshot):
        control = OperationsPoll.load()
        control.lease_until = timezone.now() + timedelta(minutes=1)
        control.lease_token = 'other-worker'
        control.save()
        with self.assertRaises(collector.CollectorBusy):
            collector.collect(force=True)
        snapshot.assert_not_called()

    def test_server_selection_and_configuration_permissions(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/operations/servers/new/').status_code, 302)
        self.client.force_login(self.admin)
        response = self.client.get('/operations/', {'server': self.home.pk})
        self.assertContains(response, 'Homelab')
        self.assertEqual(len(response.context['servers']), 1)
        response = self.client.post(f'/operations/servers/{self.home.pk}/', {
            'name': 'Home server', 'metrics_url': 'http://100.64.0.2:9100/metrics',
            'mountpoints': '/,/srv/data', 'enabled': 'on',
        })
        self.assertEqual(response.status_code, 302)
        self.home.refresh_from_db()
        self.assertEqual(self.home.mountpoint_list, ['/', '/srv/data'])
