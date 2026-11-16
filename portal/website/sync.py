"""One website worker at a time, with a database lease and coalesced refresh requests
(the same pattern as access/collector.py)."""
from datetime import timedelta
from uuid import uuid4

import requests
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from . import github, snapshot
from .models import WebsiteSync


class SyncBusy(Exception):
    pass


def due(control: WebsiteSync, now) -> bool:
    return control.pending or not control.started_at or now >= control.started_at + timedelta(
        minutes=settings.WEBSITE_SYNC_MINUTES)


def sync(force: bool = False, tokens: github.InstallationTokens | None = None) -> bool | None:
    """Refresh the snapshot if requested, due or forced. Returns True when it ran and
    succeeded, False when it ran and failed (the last good snapshot is kept), None when
    nothing was due."""
    control = WebsiteSync.load()
    now = timezone.now()
    if not force and not due(control, now):
        return None
    token = uuid4().hex
    available = WebsiteSync.objects.filter(pk=1).filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
    if not available.update(
        lease_token=token,
        lease_until=now + timedelta(seconds=300), started_at=now, heartbeat_at=now,
    ):
        raise SyncBusy('Website sync already in progress.')

    def renew():
        stamp = timezone.now()
        if not WebsiteSync.objects.filter(pk=1, lease_token=token).update(
                lease_until=stamp + timedelta(seconds=300), heartbeat_at=stamp):
            raise SyncBusy('Website sync lease lost.')

    try:
        repo = github.Repository(settings.WEBSITE_REPOSITORY, tokens or github.InstallationTokens.from_settings())
        data = snapshot.build(repo, renew)
    except github.NotConfigured as exc:
        WebsiteSync.objects.filter(pk=1, lease_token=token).update(error=str(exc))
        return False
    except (github.GitHubError, requests.RequestException, ValueError, KeyError, TypeError, UnicodeDecodeError):
        # Keep the last good snapshot; the page shows its age and this message.
        WebsiteSync.objects.filter(pk=1, lease_token=token).update(
            error='Refresh failed; check the GitHub App installation, its permissions and connectivity.')
        return False
    else:
        WebsiteSync.objects.filter(pk=1, lease_token=token).update(snapshot=data, checked_at=timezone.now(), error='')
        return True
    finally:
        WebsiteSync.objects.filter(pk=1, lease_token=token).update(
            lease_until=None, lease_token='', completed_at=timezone.now(), heartbeat_at=timezone.now())
