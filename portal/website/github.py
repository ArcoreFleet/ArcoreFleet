"""GitHub access for the website worker, as a GitHub App installed on the website
repository only. Runs in the website worker, never in the web pods: the app's
private key is mounted only into that Deployment (k3s/website-worker.yaml).

Installation tokens are requested with an explicit permission list, so a step that
only reads asks for read permissions even when the app itself may write.
"""
import base64
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests
from django.conf import settings
from jose import jwt

API = 'https://api.github.com'
HEADERS = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
READ_PERMISSIONS = {
    'contents': 'read', 'pull_requests': 'read', 'checks': 'read', 'deployments': 'read', 'metadata': 'read',
}


class NotConfigured(Exception):
    pass


class GitHubError(Exception):
    """An upstream failure. Messages never include response bodies, which can hold
    tokens or internal details."""


def app_jwt(app_id: str, private_key: str, now: float | None = None) -> str:
    """The short-lived JWT a GitHub App signs to ask for an installation token."""
    issued = int(now if now is not None else time.time()) - 60  # GitHub allows for clock drift this way
    return jwt.encode({'iat': issued, 'exp': issued + 540, 'iss': str(app_id)}, private_key, algorithm='RS256')


class InstallationTokens:
    """Caches one installation token per permission set until shortly before it expires."""

    def __init__(self, app_id: str, installation_id: str, private_key: str):
        if not (app_id and installation_id and private_key):
            raise NotConfigured('GitHub App not configured.')
        self.app_id, self.installation_id, self.private_key = app_id, installation_id, private_key
        self._cache: dict[tuple, tuple[str, float]] = {}

    @classmethod
    def from_settings(cls) -> 'InstallationTokens':
        key_file = settings.WEBSITE_GITHUB_PRIVATE_KEY_FILE
        if not key_file or not Path(key_file).is_file():
            raise NotConfigured('GitHub App private key not mounted.')
        return cls(settings.WEBSITE_GITHUB_APP_ID, settings.WEBSITE_GITHUB_INSTALLATION_ID,
                   Path(key_file).read_text())

    def token(self, permissions: dict[str, str]) -> str:
        key = tuple(sorted(permissions.items()))
        cached = self._cache.get(key)
        if cached and cached[1] - time.time() > 300:
            return cached[0]
        response = requests.post(
            f'{API}/app/installations/{self.installation_id}/access_tokens',
            headers={**HEADERS, 'Authorization': f'Bearer {app_jwt(self.app_id, self.private_key)}'},
            json={'permissions': permissions}, timeout=(3, 10), allow_redirects=False,
        )
        if response.status_code != 201:
            raise GitHubError(f'Installation token request returned HTTP {response.status_code}.')
        body = response.json()
        expires = datetime.fromisoformat(body['expires_at'].replace('Z', '+00:00')).timestamp()
        self._cache[key] = (body['token'], expires)
        return body['token']


class Repository:
    """Read calls against one repository with an installation token."""

    def __init__(self, full_name: str, tokens: InstallationTokens, permissions: dict[str, str] = READ_PERMISSIONS):
        self.full_name, self.tokens, self.permissions = full_name, tokens, permissions

    def get(self, path: str, params: dict | None = None):
        response = requests.get(
            f'{API}/repos/{self.full_name}{path}', params=params, timeout=(3, 15), allow_redirects=False,
            headers={**HEADERS, 'Authorization': f'Bearer {self.tokens.token(self.permissions)}'},
        )
        if response.status_code != 200:
            raise GitHubError(f'GitHub returned HTTP {response.status_code} for {path.split("?")[0]}.')
        return response.json()

    def pages(self, path: str, params: dict, max_pages: int) -> tuple[list[dict], bool]:
        """A paginated list, 100 per page, up to max_pages. The flag is True when that
        cap cut the list short, so callers never mistake a partial list for a whole one."""
        items = []
        for page in range(1, max_pages + 1):
            batch = self.get(path, {**params, 'per_page': 100, 'page': page})
            items.extend(batch)
            if len(batch) < 100:
                return items, False
        return items, True

    def open_pulls(self, max_pages: int = 5) -> tuple[list[dict], bool]:
        """Open pull requests, most recently updated first."""
        return self.pages('/pulls', {'state': 'open', 'sort': 'updated', 'direction': 'desc'}, max_pages)

    def pull(self, number: int) -> dict:
        return self.get(f'/pulls/{int(number)}')

    def pull_files(self, number: int, max_pages: int = 30) -> tuple[list[dict], bool]:
        """A pull request's changed files. GitHub lists at most 3,000 (30 pages)."""
        return self.pages(f'/pulls/{int(number)}/files', {}, max_pages)

    def check_runs(self, sha: str, max_pages: int = 10) -> tuple[list[dict], bool]:
        """A commit's check runs, following pagination by total_count. The flag is True
        when max_pages cut the list short."""
        runs, page = [], 0
        while page < max_pages:
            page += 1
            body = self.get(f'/commits/{sha}/check-runs', {'per_page': 100, 'page': page})
            runs.extend(body['check_runs'])
            if len(runs) >= int(body['total_count']) or not body['check_runs']:
                return runs, False
        return runs, True

    def deployments(self, sha: str) -> list[dict]:
        """The commit's 10 newest deployments. Only used to find a preview link, so a
        shorter list can hide a link but never show a wrong one."""
        return self.get('/deployments', {'sha': sha, 'per_page': 10})

    def latest_deployment_status(self, deployment_id: int) -> dict | None:
        """A deployment's current status: GitHub lists statuses newest first."""
        statuses = self.get(f'/deployments/{int(deployment_id)}/statuses', {'per_page': 1})
        return statuses[0] if statuses else None

    def list_dir(self, path: str, ref: str) -> list[dict]:
        """A directory listing. GitHub returns at most 1,000 entries per directory."""
        return self.get(f'/contents/{quote(path, safe="/")}', {'ref': ref})

    def read_file(self, path: str, ref: str) -> str:
        # Git paths may contain '#', '?' or '%'; encode everything but the separators.
        body = self.get(f'/contents/{quote(path, safe="/")}', {'ref': ref})
        if body.get('encoding') != 'base64':
            raise GitHubError(f'Unexpected encoding for {path}.')
        return base64.b64decode(body['content']).decode('utf-8')
