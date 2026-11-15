import time
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.utils import timezone

from access.collector import CollectorBusy, collect
from access.models import OperationsPoll


class Command(BaseCommand):
    help = 'Collect operations snapshots once, or run the scheduled/requested collector.'

    def add_arguments(self, parser):
        parser.add_argument('--loop', action='store_true', help='Honor 5/10-minute interval and Poll now requests.')
        parser.add_argument('--server-config', type=Path, help='Register reviewed endpoint JSON once at startup.')

    def handle(self, *args, **options):
        if options['server_config']:
            call_command('configure_operations_servers', options['server_config'], stdout=self.stdout)
        if not options['loop']:
            try:
                failures = collect(force=True)
            except CollectorBusy as exc:
                raise CommandError(str(exc)) from exc
            if failures:
                raise CommandError(f'{failures} source observation(s) failed; last-known results are marked on the page.')
            return
        OperationsPoll.load()
        next_heartbeat = 0
        try:
            while True:
                close_old_connections()
                if time.monotonic() >= next_heartbeat:
                    OperationsPoll.objects.filter(pk=1).update(heartbeat_at=timezone.now())
                    next_heartbeat = time.monotonic() + 30
                try:
                    failures = collect()
                    if failures:
                        self.stderr.write(f'{failures} source observation(s) failed.')
                except CollectorBusy:
                    pass
                time.sleep(5)  # Database request check only; no upstream polling while idle.
        except KeyboardInterrupt:
            return
