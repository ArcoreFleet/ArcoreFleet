from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import WebsiteSync

staff_required = user_passes_test(lambda u: u.is_active and u.is_staff)


@login_required
@staff_required
@require_GET
def queue(request):
    """The review queue. Reads the stored snapshot only; never calls GitHub."""
    control = WebsiteSync.load()
    data = control.snapshot or {}
    return render(request, 'website/queue.html', {
        'sync': control,
        'repository': data.get('repository') or settings.WEBSITE_REPOSITORY,
        'pulls': data.get('pulls', []),
        'posts': data.get('posts', []),
        'drafts': [p for p in data.get('posts', []) if p.get('draft')],
        'next_release_number': data.get('next_release_number'),
        'incomplete': data.get('incomplete', []),
    })


@login_required
@staff_required
@require_POST
def refresh(request):
    WebsiteSync.load()
    with transaction.atomic():
        control = WebsiteSync.objects.select_for_update().get(pk=1)
        now = timezone.now()
        if control.running or control.pending:
            messages.info(request, 'A refresh is already running or queued.')
        elif control.requested_at and (now - control.requested_at).total_seconds() < 60:
            messages.info(request, 'Please wait one minute between refreshes.')
        else:
            WebsiteSync.objects.filter(pk=1).update(requested_at=now)
            messages.success(request, 'Refresh queued. The website worker checks for requests every five seconds; reload this page after it finishes.')
    return redirect('website_queue')
