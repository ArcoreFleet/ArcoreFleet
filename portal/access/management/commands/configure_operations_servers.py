import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.db import transaction

from access.models import MonitoredServer


class Command(BaseCommand):
    help = 'Register server endpoints from a reviewed JSON file; never remove unspecified servers.'

    def add_arguments(self, parser):
        parser.add_argument('config', type=Path)

    def handle(self, *args, **options):
        try:
            data = json.loads(options['config'].read_text(encoding='utf-8'))
            if not isinstance(data, list):
                raise ValueError('Expected a list of servers.')
            with transaction.atomic():
                for entry in data:
                    if not isinstance(entry, dict) or set(entry) != {'name', 'metrics_url', 'mountpoints', 'enabled', 'database_metrics'}:
                        raise ValueError('Unexpected or missing server fields.')
                    if type(entry['enabled']) is not bool or type(entry['database_metrics']) is not bool:
                        raise ValueError('Server flags must be booleans.')
                    server = MonitoredServer.objects.filter(name=entry['name']).first() or MonitoredServer()
                    changed = any(getattr(server, field) != value for field, value in entry.items())
                    for field, value in entry.items():
                        setattr(server, field, value)
                    server.full_clean(exclude=['snapshot', 'checked_at'])
                    if changed:
                        server.snapshot, server.checked_at = {}, None
                    server.save()
        except (OSError, ValueError, TypeError, ValidationError) as exc:
            raise CommandError('Invalid server configuration file.') from exc
        self.stdout.write(f'Registered {len(data)} server endpoint(s).')
