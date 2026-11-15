import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.utils import timezone

from website import github
from website.models import WebsiteSync
from website.sync import SyncBusy, sync


class Command(BaseCommand):
    help = 'Refresh the website review queue once, or run the website worker loop.'

    def add_arguments(self, parser):
        parser.add_argument('--loop', action='store_true', help='Refresh on schedule and on Refresh requests.')

    def handle(self, *args, **options):
        if not options['loop']:
            try:
                ok = sync(force=True)
            except SyncBusy as exc:
                raise CommandError(str(exc)) from exc
            if not ok:
                raise CommandError(WebsiteSync.load().error or 'Website sync failed.')
            return
        try:
            tokens = github.InstallationTokens.from_settings()
        except github.NotConfigured as exc:
            raise CommandError(str(exc)) from exc
        WebsiteSync.load()
        next_heartbeat = 0
        try:
            while True:
                close_old_connections()
                if time.monotonic() >= next_heartbeat:
                    WebsiteSync.objects.filter(pk=1).update(heartbeat_at=timezone.now())
                    next_heartbeat = time.monotonic() + 30
                try:
                    if sync(tokens=tokens) is False:
                        self.stderr.write(WebsiteSync.load().error)
                except SyncBusy:
                    pass
                time.sleep(5)  # Database request check only; GitHub is called only when due or requested.
        except KeyboardInterrupt:
            return
