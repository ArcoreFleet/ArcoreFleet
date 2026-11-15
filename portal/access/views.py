import hmac
import json
import os

from django import forms
from django.db import transaction
from django.forms import inlineformset_factory
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.cache import cache
from django.core.validators import RegexValidator
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django.utils import timezone

from . import cf_access
from .models import Access, ApiKey, Application, DeploymentTarget, MonitoredServer, OperationsPoll, PortalSettings, access_for, can_access, visible_apps

User = get_user_model()
staff_required = user_passes_test(lambda u: u.is_active and u.is_staff)

# Decisions are cached briefly per gunicorn worker: /auth/check runs on every
# request to every gated app. Access changes take effect within this window.
DECISION_TTL = 15


@login_required
def home(request):
    return render(request, 'access/home.html', {'apps': visible_apps(request.user)})


@login_required
@staff_required
@require_GET
def operations(request):
    server_form = ServerSelectionForm(request.GET or None)
    if request.GET and not server_form.is_valid():
        return HttpResponse('Invalid server selection.', status=400)
    selected = server_form.cleaned_data.get('server') if server_form.is_valid() else None
    return render(request, 'access/operations.html', {
        'targets': DeploymentTarget.objects.select_related('application'),
        'servers': MonitoredServer.objects.filter(pk=selected.pk) if selected else MonitoredServer.objects.all(),
        'server_form': server_form, 'selected_server': selected,
        'poll': OperationsPoll.load(),
    })


@login_required
@staff_required
@require_GET
def fleet_settings(request):
    return render(request, 'access/settings.html', {
        'poll': OperationsPoll.load(),
        'servers': MonitoredServer.objects.all(),
    })


class ServerSelectionForm(forms.Form):
    server = forms.ModelChoiceField(queryset=MonitoredServer.objects.all(), required=False, empty_label='All servers')


@login_required
@staff_required
@require_POST
def poll_operations(request):
    form = ServerSelectionForm(request.POST)
    if not form.is_valid():
        return HttpResponse('Invalid server selection.', status=400)
    OperationsPoll.load()
    with transaction.atomic():
        control = OperationsPoll.objects.select_for_update().get(pk=1)
        now = timezone.now()
        if control.running or control.pending:
            messages.info(request, 'A poll is already running or queued.')
        elif control.requested_at and (now - control.requested_at).total_seconds() < 60:
            messages.info(request, 'Please wait one minute between manual polls.')
        else:
            server = form.cleaned_data['server']
            if server and not server.enabled:
                return HttpResponse('This server is disabled.', status=400)
            OperationsPoll.objects.filter(pk=1).update(requested_at=now, requested_server=server)
            messages.success(request, 'Poll queued. The collector checks for requests every five seconds; reload this page after it finishes.')
    return redirect('operations')


@login_required
@staff_required
@require_POST
def poll_interval(request):
    form = forms.TypedChoiceField(choices=[(5, '5 minutes'), (10, '10 minutes')], coerce=int)
    try:
        interval = form.clean(request.POST.get('interval'))
    except forms.ValidationError:
        return HttpResponse('Choose a 5- or 10-minute interval.', status=400)
    OperationsPoll.load()
    OperationsPoll.objects.filter(pk=1).update(interval_minutes=interval)
    messages.success(request, f'Automatic collection interval set to {interval} minutes.')
    return redirect('fleet_settings')


class ServerForm(forms.ModelForm):
    class Meta:
        model = MonitoredServer
        fields = ['name', 'metrics_url', 'mountpoints', 'enabled', 'database_metrics']


@login_required
@staff_required
def manage_server(request, pk=None):
    server = get_object_or_404(MonitoredServer, pk=pk) if pk else None
    form = ServerForm(request.POST or None, instance=server)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        if form.has_changed():
            obj.snapshot, obj.checked_at = {}, None
        obj.save()
        messages.success(request, f'Saved server {obj.name}.')
        return redirect('operations')
    return render(request, 'access/server_form.html', {'form': form, 'server': server})


@require_GET
def healthz(request):
    return HttpResponse('ok')


@require_GET
def auth_check(request):
    """Traefik forwardAuth endpoint: 200 lets the request through to the app,
    anything else is returned to the browser instead of the app."""
    email = cf_access.email_from_request(request)
    if not email:
        return _denied(request, 'No verified sign-in came with this request.', status=401)

    host = request.headers.get('X-Forwarded-Host', '').split(':')[0].lower()
    key = f'decision:{email.lower()}:{host}'
    decision = cache.get(key)
    if decision is None:
        decision = _decide(email, host)
        cache.set(key, decision, DECISION_TTL)

    allowed, app_name = decision
    if allowed:
        return HttpResponse('ok')
    if app_name is None:
        return _denied(request, f'{host or "This site"} is not registered in the portal.')
    return _denied(request, f"You don't have access to {app_name}.")


def _decide(email: str, host: str) -> tuple[bool, str | None]:
    user = cf_access.ensure_user(email)
    app = Application.objects.filter(hostname__iexact=host).first()
    if app is None:
        return (user.is_active and user.is_staff, None)
    return (can_access(user, app), app.name)


# last_used_at is a hint for spotting stale keys, not an audit log (fleet-mcp
# logs every call); writing it at most this often keeps resolves cheap.
KEY_TOUCH_SECONDS = 300


@login_required
def api_keys(request):
    """Personal keys for the Fleet MCP gateway. Admins also see everyone's keys, to revoke them."""
    new_key = None
    if request.method == 'POST' and request.POST.get('action') == 'create':
        name = request.POST.get('name', '').strip()[:100]
        if not name:
            messages.error(request, 'Give the key a name, so you can recognize it later.')
            return redirect('api_keys')
        key, new_key = ApiKey.issue(request.user, name)
        messages.success(request, f'Created {key.name}. Copy the key now - it is not shown again.')
    elif request.method == 'POST' and request.POST.get('action') == 'revoke':
        keys = ApiKey.objects.all() if request.user.is_staff else request.user.api_keys.all()
        key = get_object_or_404(keys, pk=request.POST.get('key'), revoked_at__isnull=True)
        key.revoked_at = timezone.now()
        key.save(update_fields=['revoked_at'])
        messages.success(request, f'Revoked {key.name}. The gateway refuses it within 15 seconds.')
        return redirect('api_keys')

    keys = ApiKey.objects.select_related('user') if request.user.is_staff else request.user.api_keys.all()
    return render(request, 'access/api_keys.html', {
        'keys': keys, 'new_key': new_key, 'mcp_url': settings.FLEET_MCP_URL,
    })


@csrf_exempt
@require_POST
def resolve_api_key(request):
    """For fleet-mcp only (in-cluster, with its shared secret): who owns this
    key, and which apps may they use. Same access rules as /auth/check."""
    secret = os.getenv('FLEET_MCP_SHARED_SECRET', '')
    if not secret:
        return JsonResponse({'error': 'fleet-mcp is not configured'}, status=503)
    if not hmac.compare_digest(request.headers.get('X-Fleet-MCP-Secret', ''), secret):
        return JsonResponse({'error': 'forbidden'}, status=403)
    try:
        raw = json.loads(request.body).get('key', '')
    except (ValueError, AttributeError):
        return JsonResponse({'error': 'expected {"key": "..."}'}, status=400)

    key = ApiKey.resolve(raw) if isinstance(raw, str) else None
    if key is None:
        return JsonResponse({'error': 'unknown or revoked key'}, status=401)
    now = timezone.now()
    if key.last_used_at is None or (now - key.last_used_at).total_seconds() > KEY_TOUCH_SECONDS:
        ApiKey.objects.filter(pk=key.pk).update(last_used_at=now)

    user = key.user
    apps = [a for a in Application.objects.all() if can_access(user, a)]
    return JsonResponse({
        'email': user.email.lower(),
        'is_admin': user.is_staff,
        'key': key.name,
        'apps': [{'name': a.name, 'hostname': a.hostname, 'description': a.description} for a in apps],
    })


def _denied(request, reason: str, status: int = 403):
    return render(request, 'access/denied.html', {'reason': reason, 'portal_url': settings.PORTAL_URL}, status=status)


def logout_view(request):
    logout(request)
    # Ending the Access session too - otherwise the next request signs straight back in.
    return redirect(cf_access.LOGOUT_URL if cf_access.is_enabled() else '/')


@login_required
@staff_required
def manage_users(request):
    apps = list(Application.objects.all())
    users = list(User.objects.order_by('-is_staff', 'email').select_related('access'))
    for u in users:
        access_for(u)
    settings_obj = PortalSettings.load()

    if request.method == 'POST' and request.POST.get('action') == 'add_user':
        email = request.POST.get('email', '').strip().lower()
        try:
            forms.EmailField().clean(email)
        except forms.ValidationError:
            messages.error(request, f'"{email}" is not a valid email address.')
        else:
            existed = User.objects.filter(email__iexact=email).exists()
            cf_access.ensure_user(email)
            messages.info(request, f'{email} already exists.' if existed else f'Added {email}.')
        return redirect('manage_users')

    if request.method == 'POST':
        settings_obj.new_users_all_apps = 'new_users_all_apps' in request.POST
        settings_obj.save()
        for u in users:
            if f'present_{u.pk}' not in request.POST:
                continue  # user added after this page was loaded
            is_self = u.pk == request.user.pk
            # You can't remove your own admin rights or deactivate yourself (no lockouts).
            u.is_staff = True if is_self else f'admin_{u.pk}' in request.POST
            u.is_active = True if is_self else f'active_{u.pk}' in request.POST
            u.save(update_fields=['is_staff', 'is_active'])
            access = u.access
            access.all_apps = f'all_{u.pk}' in request.POST
            access.save(update_fields=['all_apps'])
            access.apps.set([a for a in apps if f'app_{u.pk}_{a.pk}' in request.POST])
        cache.clear()
        messages.success(request, 'Access saved. Changes apply to open sessions within 15 seconds.')
        return redirect('manage_users')

    rows = [
        {
            'user': u,
            'access': u.access,
            'app_ids': set(u.access.apps.values_list('pk', flat=True)),
        }
        for u in users
    ]
    return render(request, 'access/manage_users.html', {'apps': apps, 'rows': rows, 'settings': settings_obj})


class ApplicationForm(forms.ModelForm):
    hostname = forms.CharField(
        max_length=253,
        validators=[RegexValidator(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$',
                                   'A hostname like myapp.example.com (lowercase, no https://).')],
    )
    color = forms.CharField(widget=forms.TextInput(attrs={'type': 'color'}))

    class Meta:
        model = Application
        fields = ['name', 'hostname', 'description', 'icon', 'color', 'sort_order', 'is_active', 'admin_only', 'repository']


TargetFormSet = inlineformset_factory(
    Application, DeploymentTarget,
    fields=['name', 'public_url', 'branch', 'workflow', 'namespace', 'deployment', 'container'],
    extra=1, can_delete=True,
)


@login_required
@staff_required
def manage_apps(request):
    form = ApplicationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        app = form.save()
        cache.clear()
        messages.success(request, f'Registered {app.name}.')
        return redirect('manage_apps')
    return render(request, 'access/manage_apps.html', {'apps': Application.objects.all(), 'form': form})


@login_required
@staff_required
def edit_app(request, pk):
    app = get_object_or_404(Application, pk=pk)
    form = ApplicationForm(request.POST or None, instance=app)
    targets = TargetFormSet(request.POST or None, instance=app, prefix='targets')
    if request.method == 'POST' and form.is_valid() and targets.is_valid():
        with transaction.atomic():
            form.save()
            if 'repository' in form.changed_data:
                app.targets.update(snapshot={}, checked_at=None)
            for target in targets.save(commit=False):
                # An observation of a previous configuration must not appear current.
                target.snapshot = {}
                target.checked_at = None
                target.save()
            for target in targets.deleted_objects:
                target.delete()
        cache.clear()
        messages.success(request, f'Saved {app.name}.')
        return redirect('manage_apps')
    return render(request, 'access/edit_app.html', {'app': app, 'form': form, 'targets': targets})


@login_required
@staff_required
@require_POST
def delete_app(request, pk):
    app = get_object_or_404(Application, pk=pk)
    if request.POST.get('confirm') != 'yes':
        messages.error(request, 'Tick the confirmation box to delete.')
        return redirect('edit_app', pk=pk)
    app.delete()
    cache.clear()
    messages.success(request, f'Deleted {app.name}. Its hostname now refuses everyone but admins.')
    return redirect('manage_apps')
