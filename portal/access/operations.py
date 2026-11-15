"""Read-only observations. Never execute pipelines or store upstream credentials/payloads."""
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from django.conf import settings
from django.utils import timezone


class NotConfigured(Exception):
    pass


def get_json(url, **kwargs):
    response = requests.get(url, timeout=(3, 10), allow_redirects=False, **kwargs)
    if response.status_code != 200:
        # Bodies and exception strings can contain credentials or internal details.
        raise ValueError(f'Upstream returned HTTP {response.status_code}.')
    return response.json()


def workflow(target):
    repo = target.application.repository
    if not repo:
        raise NotConfigured('Repository not configured.')
    endpoint = f'https://api.github.com/repos/{repo}/actions'
    if target.workflow:
        endpoint += f'/workflows/{quote(target.workflow, safe="")}'
    headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
    if settings.FLEET_GITHUB_TOKEN:
        headers['Authorization'] = f'Bearer {settings.FLEET_GITHUB_TOKEN}'
    runs = get_json(endpoint + '/runs', headers=headers, params={
        'branch': target.branch, 'per_page': 1,
    })['workflow_runs']
    if not runs:
        return {'status': 'No workflow runs on this branch'}
    run = runs[0]
    # Construct links from configured repository + numeric ID, never upstream HTML URLs.
    return {
        'name': str(run.get('name') or 'Workflow'),
        'status': str(run.get('conclusion') or run['status']),
        'sha': str(run['head_sha']),
        'url': f'https://github.com/{repo}/actions/runs/{int(run["id"])}',
        'updated_at': str(run['updated_at']),
    }


def rollout(target):
    if not target.deployment or not settings.FLEET_KUBERNETES_ENABLED:
        raise NotConfigured('Kubernetes observation not configured.')
    token = Path(settings.FLEET_K8S_TOKEN_FILE).read_text().strip()
    deployment = get_json(
        f'https://kubernetes.default.svc/apis/apps/v1/namespaces/{target.namespace}/deployments/{target.deployment}',
        headers={'Authorization': f'Bearer {token}'}, verify=settings.FLEET_K8S_CA_FILE,
    )
    containers = deployment['spec']['template']['spec']['containers']
    if target.container:
        containers = [c for c in containers if c['name'] == target.container]
    if len(containers) != 1:
        raise ValueError('Configure one matching container for this Deployment.')
    image = containers[0]['image']
    # A tag is reported as a commit only when it is a Git SHA, not latest/version/digest.
    tag = image.rsplit('/', 1)[-1].rsplit(':', 1)[-1] if '@' not in image else ''
    sha = tag if re.fullmatch(r'[a-fA-F0-9]{7,40}', tag) else ''
    spec, state = deployment['spec'], deployment.get('status', {})
    desired = spec.get('replicas', 1)
    ready = state.get('readyReplicas', 0)
    observed = state.get('observedGeneration', 0) >= deployment['metadata']['generation']
    failed = any(c['type'] == 'Progressing' and c['status'] == 'False' for c in state.get('conditions', []))
    complete = (
        observed and state.get('updatedReplicas', 0) == desired
        and state.get('availableReplicas', 0) == desired
        and state.get('replicas', 0) == desired and ready == desired
    )
    status = ('Scaled to zero' if desired == 0 else 'Ready') if complete else ('Failed' if failed and observed else 'Progressing')
    return {'status': status, 'image': image, 'sha': sha, 'ready': ready, 'desired': desired}


def test_runs():
    base = settings.FLEET_BROWSERFORGE_URL
    if not base:
        raise NotConfigured('BrowserForge observation not configured.')
    if urlsplit(base).scheme not in ('http', 'https') or not urlsplit(base).netloc:
        raise ValueError('BrowserForge URL must be an HTTP(S) service URL.')
    # BrowserForge currently returns at most 100 runs. Never persist env, logs or artifacts.
    return get_json(base + '/runs', params={'limit': 100})['runs']


def tests(target, runs):
    repo = target.application.repository
    if not repo:
        raise NotConfigured('Repository not configured.')
    repo_url = f'https://github.com/{repo}'.lower()
    base_url = (target.public_url or target.application.url).rstrip('/')
    matches = [r for r in runs if
               str(r.get('repoUrl', '')).rstrip('/').removesuffix('.git').lower() == repo_url
               and str(r.get('baseUrl', '')).rstrip('/') == base_url]
    if not matches:
        return {'status': 'No matching test in the latest 100 runs'}
    run = max(matches, key=lambda r: r['createdAt'])
    return {key: str(value) for key, value in {
        'status': run['status'], 'id': run['id'], 'ref': run['ref'], 'created_at': run['createdAt'],
    }.items()}


def observe(previous, callback):
    """Retain a dated last-known result on failure; never show it as a new success."""
    try:
        data = callback()
    except NotConfigured as exc:
        return {'error': str(exc)}, False
    except (requests.RequestException, ValueError, KeyError, TypeError, OSError):
        return {**previous, 'error': 'Observation failed; check connectivity, configuration and read permissions.'}, True
    return {'data': data, 'last_success_at': timezone.now().isoformat()}, False
