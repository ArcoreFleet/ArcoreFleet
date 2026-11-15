import json
import os
from datetime import datetime, timezone as dt_timezone

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from . import signing
from .models import AuthorityConfigVersion, HubApplication, HubProvision, HubProvisionEvent

PROFILE_TYPE = 'application/vnd.arcore.profile+json'


def current_signer() -> signing.Signer:
    return signing.load_signer(os.getenv('HUB_CONFIG_KEY', ''), os.getenv('HUB_CONFIG_DELEGATION', ''))


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


class PublishForm(forms.Form):
    tenant = forms.CharField(max_length=64, initial='arecorellc')
    endpoints = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 3}),
        help_text='One per line: host:port or [ipv6]:port. Hubs try them in order.',
    )
    tls_pins = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 3}),
        help_text='One per line: sha256/<base64> of the control plane\'s public key. Include a backup pin before rotating keys.',
    )
    allow_private_endpoints = forms.BooleanField(
        required=False, help_text='Needed for tailnet (100.64.0.0/10) or LAN IP addresses. DNS names do not need it.',
    )
    download_locations = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 2}), required=False, help_text='Optional https:// URLs, one per line.',
    )
    validity_days = forms.IntegerField(
        min_value=1, max_value=365, initial=30,
        help_text='Hubs must refresh within this time. Never later than the delegation expiry.',
    )

    def clean(self):
        data = super().clean()
        allow = data.get('allow_private_endpoints', False)
        try:
            data['endpoint_list'] = [signing.parse_endpoint(e, allow) for e in _lines(data.get('endpoints', ''))]
        except ValueError as exc:
            self.add_error('endpoints', str(exc))
        try:
            data['pin_list'] = list(dict.fromkeys(signing.parse_pin(p) for p in _lines(data.get('tls_pins', ''))))
        except ValueError as exc:
            self.add_error('tls_pins', str(exc))
        downloads = _lines(data.get('download_locations', ''))
        if any(not d.startswith('https://') for d in downloads):
            self.add_error('download_locations', 'Use https:// URLs.')
        data['download_list'] = downloads
        return data


def _when(ts: int | None) -> str:
    return datetime.fromtimestamp(ts, dt_timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if ts else ''


@login_required
def hub(request):
    """Everyone: download the ArcoreHub profile. Admins: publish configuration."""
    latest = AuthorityConfigVersion.latest()
    if not request.user.is_active:
        return HttpResponseForbidden('An active account is required.')
    signer, signer_error = None, None
    try:
        signer = current_signer()
    except signing.NotConfigured as exc:
        signer_error = str(exc)

    form = None
    if request.user.is_staff:
        if request.method == 'POST':
            form = PublishForm(request.POST)
            if signer is None:
                messages.error(request, f'Cannot sign: {signer_error}')
            elif form.is_valid():
                d = form.cleaned_data
                try:
                    row = AuthorityConfigVersion.publish(
                        signer, request.user, tenant=d['tenant'], endpoints=d['endpoint_list'],
                        tls_pins=d['pin_list'], allow_private_endpoints=d['allow_private_endpoints'],
                        validity_days=d['validity_days'], download_locations=d['download_list'],
                    )
                except ValueError as exc:
                    messages.error(request, f'Not published: {exc}')
                else:
                    messages.success(request, f'Published configuration version {row.version}. Hubs pick it up on their next refresh.')
                    return redirect('hub')
        else:
            initial = {}
            if latest:
                initial = {
                    'tenant': latest.tenant,
                    'endpoints': '\n'.join(f'[{e["host"]}]:{e["port"]}' if ':' in e['host'] else f'{e["host"]}:{e["port"]}' for e in latest.endpoints),
                    'tls_pins': '\n'.join(latest.tls_pins),
                    'allow_private_endpoints': latest.allow_private_endpoints,
                    'download_locations': '\n'.join(latest.download_locations),
                }
            form = PublishForm(initial=initial)
    elif request.method == 'POST':
        return HttpResponseForbidden('Only admins publish ArcoreHub configuration.')

    return render(request, 'hubauthority/hub.html', {
        'latest': latest,
        'latest_expires': _when(latest.expires) if latest else '',
        'versions': AuthorityConfigVersion.objects.all()[:10] if request.user.is_staff else [],
        'signer': signer,
        'signer_error': signer_error,
        'delegation_expires': _when(signer.expires) if signer else '',
        'form': form,
        'provisions': HubProvision.objects.filter(user=request.user, is_active=True, application__is_active=True).select_related('application'),
    })


@login_required
@require_GET
def hub_profile(request):
    """The signed .arcoreprofile for this authority. Public data only: it grants
    nothing until the user signs in from ArcoreHub."""
    latest = AuthorityConfigVersion.latest()
    if not request.user.is_active:
        return HttpResponseForbidden('An active account is required.')
    if latest is None:
        return HttpResponse('No ArcoreHub configuration has been published yet.', status=404, content_type='text/plain')
    resp = HttpResponse(json.dumps(latest.profile(), indent=2), content_type=PROFILE_TYPE)
    resp['Content-Disposition'] = f'attachment; filename="arcorehub-{latest.authority_id}.arcoreprofile"'
    resp['Cache-Control'] = 'no-store'
    return resp


class HubApplicationForm(forms.ModelForm):
    class Meta:
        model = HubApplication
        fields = ['app_id', 'name', 'description', 'is_active']


class ProvisionForm(forms.Form):
    customer = forms.ModelChoiceField(queryset=get_user_model().objects.filter(is_active=True).order_by('username'))
    application = forms.ModelChoiceField(queryset=HubApplication.objects.filter(is_active=True))


@login_required
def hub_provisioning(request):
    if not (request.user.is_active and request.user.is_staff):
        return HttpResponseForbidden('Only admins provision Hub apps.')
    app_form, provision_form = HubApplicationForm(), ProvisionForm()
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'register':
            app_form = HubApplicationForm(request.POST)
            if app_form.is_valid():
                app_form.save()
                messages.success(request, 'Hub app registered. Assign it to a customer below.')
                return redirect('hub_provisioning')
        elif action == 'grant':
            provision_form = ProvisionForm(request.POST)
            if provision_form.is_valid():
                with transaction.atomic():
                    row, created = HubProvision.objects.get_or_create(
                        user=provision_form.cleaned_data['customer'], application=provision_form.cleaned_data['application'],
                        defaults={'updated_by': request.user})
                    row = HubProvision.objects.select_for_update().get(pk=row.pk)
                    if created or not row.is_active:
                        row.is_active, row.updated_by = True, request.user
                        row.save()
                        HubProvisionEvent.objects.create(provision=row, actor=request.user, action='grant')
                messages.success(request, 'App provisioned. The customer can see it in their Hub tile.')
                return redirect('hub_provisioning')
        else:
            return HttpResponse('Unknown provisioning action.', status=400)
    return render(request, 'hubauthority/provisioning.html', {
        'app_form': app_form, 'provision_form': provision_form,
        'assignments': HubProvision.objects.select_related('user', 'application', 'updated_by'),
        'events': HubProvisionEvent.objects.select_related('actor', 'provision__user', 'provision__application').order_by('-created_at')[:30],
    })


@login_required
@require_POST
def hub_revoke(request, pk):
    if not (request.user.is_active and request.user.is_staff):
        return HttpResponseForbidden('Only admins provision Hub apps.')
    with transaction.atomic():
        row = get_object_or_404(HubProvision.objects.select_for_update(), pk=pk)
        if row.is_active:
            row.is_active, row.updated_by = False, request.user
            row.save()
            HubProvisionEvent.objects.create(provision=row, actor=request.user, action='revoke')
    messages.success(request, 'App assignment revoked.')
    return redirect('hub_provisioning')


# ---- control plane (served by the hub-authority deployment over pinned TLS) -------

@require_GET
def control_healthz(request):
    return HttpResponse('ok', content_type='text/plain')


@require_GET
def control_config(request):
    """The latest signed configuration bundle, which Hubs fetch to refresh."""
    latest = AuthorityConfigVersion.latest()
    if latest is None:
        return JsonResponse({'error': 'no configuration published'}, status=503)
    resp = JsonResponse(latest.profile())
    resp['Cache-Control'] = 'no-store'
    return resp
