import json

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.core.validators import RegexValidator

from . import signing


class HubApplication(models.Model):
    """An app administrators may assign to Hub customers, separate from web tiles."""
    app_id = models.CharField(max_length=150, unique=True, validators=[
        RegexValidator(r'^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)+$', 'Use a stable app ID such as com.arcore.notes.')])
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=300, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name', 'app_id']

    def __str__(self):
        return f'{self.name} ({self.app_id})'


class HubProvision(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='hub_provisions')
    application = models.ForeignKey(HubApplication, on_delete=models.PROTECT, related_name='provisions')
    is_active = models.BooleanField(default=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'application'], name='unique_hub_user_app')]
        ordering = ['application__name', 'user__username']


class HubProvisionEvent(models.Model):
    """Append-only administrative history; customer pages never expose other users."""
    provision = models.ForeignKey(HubProvision, on_delete=models.PROTECT, related_name='events')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='+')
    action = models.CharField(max_length=16, choices=[('grant', 'Grant'), ('revoke', 'Revoke')])
    created_at = models.DateTimeField(auto_now_add=True)


class AuthorityConfigVersion(models.Model):
    """One published, signed ArcoreHub authority configuration.

    Rows are never edited: publishing creates the next version, and Hubs refuse a
    version lower than one they already trust. The signed delegation is stored with
    each version, so the control plane serves complete profiles without holding the
    configuration key.
    """

    version = models.PositiveBigIntegerField(unique=True)
    authority_id = models.CharField(max_length=64)
    environment = models.CharField(max_length=8)
    tenant = models.CharField(max_length=64)
    endpoints = models.JSONField()
    tls_pins = models.JSONField()
    allow_private_endpoints = models.BooleanField(default=False)
    download_locations = models.JSONField(default=list)
    issued_at = models.BigIntegerField()
    expires = models.BigIntegerField()
    signed_config = models.TextField()
    signed_delegation = models.TextField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-version']

    def __str__(self):
        return f'{self.authority_id} v{self.version}'

    @classmethod
    def latest(cls):
        return cls.objects.order_by('-version').first()

    def profile(self) -> dict:
        return signing.profile(json.loads(self.signed_delegation), json.loads(self.signed_config))

    @classmethod
    def publish(cls, signer: signing.Signer, user, **fields) -> 'AuthorityConfigVersion':
        """Sign and store the next version. Retries if two admins publish at once."""
        for _ in range(3):
            try:
                with transaction.atomic():
                    last = cls.latest()
                    version = (last.version if last else 0) + 1
                    config = signing.build_config(signer, version=version, **fields)
                    return cls.objects.create(
                        version=version,
                        authority_id=config['authority_id'],
                        environment=config['environment'],
                        tenant=config['tenant'],
                        endpoints=config['endpoints'],
                        tls_pins=config['tls_pins'],
                        allow_private_endpoints=config['allow_private_endpoints'],
                        download_locations=config['download_locations'],
                        issued_at=config['issued_at'],
                        expires=config['expires'],
                        signed_config=json.dumps(signing.sign(config, signer.key)),
                        signed_delegation=json.dumps(signer.delegation_signed),
                        created_by=user,
                    )
            except IntegrityError:
                continue
        raise IntegrityError('could not allocate a configuration version')
