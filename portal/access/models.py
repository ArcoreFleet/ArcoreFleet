import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.core.validators import RegexValidator
from django.core.exceptions import ValidationError
from django.utils import timezone
from urllib.parse import urlsplit


class Application(models.Model):
    """An app on the fleet, identified by the hostname Traefik routes to it."""

    name = models.CharField(max_length=100)
    hostname = models.CharField(
        max_length=253, unique=True, help_text='e.g. arcoreestate.example.com - must match the Ingress host'
    )
    description = models.CharField(max_length=300, blank=True)
    icon = models.CharField(max_length=8, default='▦', help_text='An emoji or 1-2 letters shown on the tile')
    color = models.CharField(max_length=7, default='#2563eb', help_text='Tile accent color, #rrggbb')
    is_active = models.BooleanField(default=True, help_text='Inactive apps are hidden and refuse everyone but admins')
    admin_only = models.BooleanField(default=False, help_text='Requires an active user with Admin checked; app grants and All apps do not override this.')
    sort_order = models.PositiveIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    repository = models.CharField(
        max_length=201, blank=True,
        validators=[RegexValidator(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$', 'Use owner/repository.')],
        help_text='GitHub owner/repository, for example ArcoreFleet/ArcoreFleet',
    )

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self) -> str:
        return self.name

    @property
    def url(self) -> str:
        return f'https://{self.hostname}/'


class DeploymentTarget(models.Model):
    """An observed environment; credentials and execution stay outside the portal."""

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name='targets')
    name = models.CharField(max_length=100)
    public_url = models.URLField(blank=True, help_text='BrowserForge baseUrl; defaults to the app URL.')
    branch = models.CharField(max_length=255, default='main')
    workflow = models.CharField(
        max_length=100, blank=True,
        validators=[RegexValidator(r'^[A-Za-z0-9_.-]+$', 'Use a workflow filename or numeric ID.')],
        help_text='Workflow filename/ID; blank shows the latest workflow on the branch.',
    )
    namespace = models.CharField(
        max_length=63, default='apps',
        validators=[RegexValidator(r'^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$', 'Use a Kubernetes namespace.')],
    )
    deployment = models.CharField(
        max_length=253, blank=True,
        validators=[RegexValidator(r'^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$', 'Use a Deployment name.')],
    )
    container = models.CharField(max_length=63, blank=True, help_text='Required for multi-container Deployments.')
    snapshot = models.JSONField(default=dict, editable=False)
    checked_at = models.DateTimeField(null=True, editable=False)

    class Meta:
        ordering = ['application__sort_order', 'application__name', 'name']
        constraints = [models.UniqueConstraint(fields=['application', 'name'], name='unique_app_target')]

    def __str__(self):
        return f'{self.application.name} / {self.name}'

    @property
    def stale(self):
        return not self.checked_at or (timezone.now() - self.checked_at).total_seconds() > 900


class MonitoredServer(models.Model):
    name = models.CharField(max_length=100, unique=True)
    metrics_url = models.URLField(help_text='Internal or Tailscale node_exporter /metrics URL; never a public endpoint.')
    mountpoints = models.CharField(max_length=1000, default='/', help_text='Comma-separated host mountpoints, for example /,/srv/data.')
    enabled = models.BooleanField(default=True)
    database_metrics = models.BooleanField(default=False, help_text='Expect the existing Oracle report textfile metrics on this host.')
    snapshot = models.JSONField(default=dict, editable=False)
    checked_at = models.DateTimeField(null=True, editable=False)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def clean(self):
        url = urlsplit(self.metrics_url)
        if url.scheme not in ('http', 'https') or url.username or url.password or url.query or url.fragment:
            raise ValidationError({'metrics_url': 'Use an HTTP(S) metrics URL without credentials, query or fragment.'})
        if not self.mountpoint_list or any(not p.startswith('/') for p in self.mountpoint_list):
            raise ValidationError({'mountpoints': 'Provide absolute Linux mountpoints.'})

    @property
    def mountpoint_list(self):
        return list(dict.fromkeys(p.strip() for p in self.mountpoints.split(',') if p.strip()))

    @property
    def stale(self):
        return not self.checked_at or (timezone.now() - self.checked_at).total_seconds() > 900


class OperationsPoll(models.Model):
    """One shared request and lease across web workers and the collector."""
    interval_minutes = models.PositiveSmallIntegerField(default=10, choices=[(5, '5 minutes'), (10, '10 minutes')])
    requested_at = models.DateTimeField(null=True)
    requested_server = models.ForeignKey(MonitoredServer, null=True, on_delete=models.SET_NULL)
    started_at = models.DateTimeField(null=True)
    full_started_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    heartbeat_at = models.DateTimeField(null=True)
    lease_until = models.DateTimeField(null=True)
    lease_token = models.CharField(max_length=32, blank=True)
    failures = models.PositiveIntegerField(default=0)

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def running(self):
        return self.lease_until is not None and self.lease_until > timezone.now()

    @property
    def worker_online(self):
        return self.heartbeat_at is not None and (timezone.now() - self.heartbeat_at).total_seconds() < 45

    @property
    def pending(self):
        return self.requested_at and (not self.started_at or self.requested_at > self.started_at)


class Access(models.Model):
    """Which apps a user may open. Admins (is_staff) may open everything."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='access')
    all_apps = models.BooleanField(default=False, help_text='Every app, including ones added later')
    apps = models.ManyToManyField(Application, blank=True, related_name='users')

    def __str__(self) -> str:
        return f'access for {self.user}'


class ApiKey(models.Model):
    """A personal key for the Fleet MCP gateway (fleet-mcp/). It acts as its
    owner, within the owner's app access; only a SHA-256 hash is stored."""

    PREFIX = 'afk_'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='api_keys')
    name = models.CharField(max_length=100, help_text='What uses this key, e.g. "Claude Code on fleet-worker"')
    hint = models.CharField(max_length=16, help_text='The first characters, to recognize the key')
    key_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self) -> str:
        return f'{self.name} ({self.hint}…)'

    @staticmethod
    def hash(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def issue(cls, user, name: str) -> tuple['ApiKey', str]:
        """A new key and its raw value, which is shown once and never stored."""
        raw = cls.PREFIX + secrets.token_urlsafe(32)
        return cls.objects.create(user=user, name=name, hint=raw[:12], key_hash=cls.hash(raw)), raw

    @classmethod
    def resolve(cls, raw: str) -> 'ApiKey | None':
        """The live key for this raw value, if it is unrevoked and its owner is active."""
        if not raw.startswith(cls.PREFIX):
            return None
        key = cls.objects.select_related('user').filter(key_hash=cls.hash(raw), revoked_at__isnull=True).first()
        if key is None or not key.user.is_active:
            return None
        return key

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class PortalSettings(models.Model):
    """Single-row settings (pk=1)."""

    new_users_all_apps = models.BooleanField(
        default=False, help_text='Users created on first sign-in start with access to all apps'
    )

    @classmethod
    def load(cls) -> 'PortalSettings':
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


def access_for(user) -> Access:
    access, _ = Access.objects.get_or_create(
        user=user, defaults={'all_apps': PortalSettings.load().new_users_all_apps}
    )
    return access


def can_access(user, app: Application) -> bool:
    if not user.is_active:
        return False
    if user.is_staff:
        return True
    if not app.is_active or app.admin_only:
        return False
    access = access_for(user)
    return access.all_apps or access.apps.filter(pk=app.pk).exists()


def visible_apps(user):
    apps = Application.objects.all()
    if not user.is_active:
        return apps.none()
    if user.is_staff:
        return apps
    apps = apps.filter(is_active=True, admin_only=False)
    access = access_for(user)
    return apps if access.all_apps else apps.filter(pk__in=access.apps.values('pk'))
