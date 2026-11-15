from django.db import models
from django.utils import timezone


class WebsiteSync(models.Model):
    """Single row (pk=1): the latest review-queue snapshot of the website repository,
    plus the request and lease the website worker uses. Web requests only read the
    snapshot or record a refresh request; only the worker talks to GitHub."""

    snapshot = models.JSONField(default=dict, editable=False)
    checked_at = models.DateTimeField(null=True, editable=False)
    error = models.CharField(max_length=300, blank=True, editable=False)
    requested_at = models.DateTimeField(null=True)
    started_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    heartbeat_at = models.DateTimeField(null=True)
    lease_until = models.DateTimeField(null=True)
    lease_token = models.CharField(max_length=32, blank=True)

    @classmethod
    def load(cls) -> 'WebsiteSync':
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def running(self) -> bool:
        return self.lease_until is not None and self.lease_until > timezone.now()

    @property
    def pending(self) -> bool:
        return bool(self.requested_at and (not self.started_at or self.requested_at > self.started_at))

    @property
    def worker_online(self) -> bool:
        return self.heartbeat_at is not None and (timezone.now() - self.heartbeat_at).total_seconds() < 45

    @property
    def stale(self) -> bool:
        return not self.checked_at or (timezone.now() - self.checked_at).total_seconds() > 1800
