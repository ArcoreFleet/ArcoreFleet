import importlib.util
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from prometheus_client.parser import text_string_to_metric_families

from access.models import MonitoredServer
from access.system_metrics import database_snapshot


spec = importlib.util.spec_from_file_location('report_export', Path(settings.BASE_DIR) / 'host-metrics/export_oracle_report.py')
report_export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report_export)


def parsed(text):
    return [sample for family in text_string_to_metric_families(text) for sample in family.samples]


class DatabaseReportTests(TestCase):
    def test_cached_report_export_excludes_schema_inventory_and_preserves_source_time(self):
        report = '''# Oracle DB Summary
_Generated: 2020-01-01 10:00:00 UTC | Oracle 23.0 | Host: fleet-control_
## Storage
**Total allocated: 2 GB**
| Tablespace | Used % | Used GB | Total GB | Status |
|---|---|---|---|---|
| DATA | 85.0% | 1 | 2 | warn |
## Schemas
PRIVATE_BUSINESS_SCHEMA secret
'''
        text = report_export.convert(report)
        self.assertNotIn('PRIVATE_BUSINESS_SCHEMA', text)
        self.assertNotIn('secret', text)
        result = database_snapshot(parsed(text))
        self.assertEqual(result['allocated'], 2 * 1024 ** 3)
        self.assertTrue(result['stale'])
        self.assertEqual(result['collected_at'], '2020-01-01T10:00:00+00:00')
        self.assertEqual(result['tablespaces'][0]['pressure'], 'warning')

    def test_missing_or_unavailable_database_report_has_explicit_error(self):
        self.assertIn('error', database_snapshot([]))
        self.assertIn('error', database_snapshot(parsed('fleet_oracle_db_report_available 0\n')))
        with self.assertRaises(ValueError):
            report_export.convert('Report is incomplete')

    def test_endpoint_config_is_idempotent_and_bad_entries_roll_back(self):
        config = [{'name': 'Oracle', 'metrics_url': 'http://192.0.2.10:9100/metrics',
                   'mountpoints': '/', 'enabled': True, 'database_metrics': True}]
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'servers.json'
            path.write_text(json.dumps(config))
            call_command('configure_operations_servers', path, stdout=io.StringIO())
            server = MonitoredServer.objects.get(name='Oracle')
            server.snapshot = {'data': {'cpu_percent': 10}}
            server.save()
            call_command('configure_operations_servers', path, stdout=io.StringIO())
            server.refresh_from_db()
            self.assertEqual(server.snapshot['data']['cpu_percent'], 10)
            self.assertEqual(MonitoredServer.objects.count(), 1)
            config.append({'name': 'Broken'})
            path.write_text(json.dumps(config))
            with self.assertRaises(CommandError):
                call_command('configure_operations_servers', path, stdout=io.StringIO())
            self.assertEqual(MonitoredServer.objects.count(), 1)
