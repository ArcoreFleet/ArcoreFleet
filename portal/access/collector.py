"""One collector, with a database lease and coalesced admin refresh requests."""
from datetime import timedelta
from uuid import uuid4

from django.db.models import Q
from django.utils import timezone

from . import operations, system_metrics
from .models import DeploymentTarget, MonitoredServer, OperationsPoll


class CollectorBusy(Exception):
    pass


def collect(force=False):
    control = OperationsPoll.load()
    now = timezone.now()
    due = not control.full_started_at or now >= control.full_started_at + timedelta(minutes=control.interval_minutes)
    if not force and not control.pending and not due:
        return None
    token = uuid4().hex
    available = OperationsPoll.objects.filter(pk=1).filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
    if not available.update(
        lease_token=token,
        lease_until=now + timedelta(seconds=120), started_at=now, heartbeat_at=now,
    ):
        raise CollectorBusy('Collection already in progress.')
    selected = control.requested_server_id if control.pending and not force else None
    if not selected:
        OperationsPoll.objects.filter(pk=1, lease_token=token).update(full_started_at=now)
    failures = 0

    def renew():
        stamp = timezone.now()
        if not OperationsPoll.objects.filter(pk=1, lease_token=token).update(
            lease_until=stamp + timedelta(seconds=120), heartbeat_at=stamp,
        ):
            raise CollectorBusy('Collector lease lost.')

    try:
        servers = MonitoredServer.objects.filter(enabled=True)
        if selected:
            servers = servers.filter(pk=selected)
        for server in servers:
            renew()
            server.full_clean(exclude=['snapshot', 'checked_at'])
            result, failed = operations.observe(server.snapshot, lambda: system_metrics.snapshot(server))
            renew()
            server.snapshot, server.checked_at = result, timezone.now()
            server.save(update_fields=['snapshot', 'checked_at'])
            database_failed = bool(result.get('data', {}).get('database', {}).get('error'))
            failures += int(failed or database_failed)

        if not selected:
            targets = DeploymentTarget.objects.select_related('application').all()
            runs, runs_error = None, None
            if targets:
                try:
                    runs = operations.test_runs()
                except (operations.NotConfigured, ValueError, KeyError, TypeError, OSError, operations.requests.RequestException) as exc:
                    runs_error = exc

            def browserforge(target):
                if runs_error is not None:
                    raise runs_error
                return operations.tests(target, runs)

            for target in targets:
                renew()
                target.full_clean(exclude=['snapshot', 'checked_at'])
                snapshot = {}
                for source, callback in (
                    ('workflow', lambda: operations.workflow(target)),
                    ('rollout', lambda: operations.rollout(target)),
                    ('tests', lambda: browserforge(target)),
                ):
                    renew()
                    snapshot[source], failed = operations.observe(target.snapshot.get(source, {}), callback)
                    failures += int(failed)
                renew()
                target.snapshot, target.checked_at = snapshot, timezone.now()
                target.save(update_fields=['snapshot', 'checked_at'])
    except Exception:
        failures += 1
        raise
    finally:
        OperationsPoll.objects.filter(pk=1, lease_token=token).update(
            lease_until=None, lease_token='', completed_at=timezone.now(), heartbeat_at=timezone.now(), failures=failures,
        )
    return failures
