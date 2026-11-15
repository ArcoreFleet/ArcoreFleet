"""Builds the review-queue snapshot of the website repository.

The page renders only what is stored here. Values are reduced to plain strings and
numbers, links are rebuilt from the configured repository and numeric IDs (never
copied from upstream HTML URLs), and no response bodies, tokens or file contents
are kept beyond the few post fields the queue shows.
"""
import re
from urllib.parse import urlsplit

from django.utils import timezone

from .github import GitHubError, Repository

SHA = re.compile(r'^[0-9a-f]{40}$')
BLOG_DIR = 'content/blog'
MAX_POSTS = 500  # posts read from main per refresh
DIR_LISTING_LIMIT = 1000  # GitHub's Contents API lists at most this many entries
PASSING = {'success', 'skipped', 'neutral'}
FAILING = {'failure', 'timed_out', 'cancelled', 'action_required', 'startup_failure', 'stale'}


def text(value, limit: int = 200) -> str:
    return str(value if value is not None else '')[:limit]


def classify(path: str) -> str:
    """What a changed file is, for the queue's labels."""
    if path.startswith(f'{BLOG_DIR}/'):
        return 'blog'
    if path.startswith('lib/data/') or path == 'docs/project-metadata/repository-evidence.json':
        return 'catalog'
    if path.startswith('content/'):
        return 'content'
    return 'code'


def frontmatter(source: str) -> dict[str, str]:
    """The blog's simple `key: value` frontmatter (lib/blog.ts reads it the same way)."""
    match = re.match(r'^---\r?\n([\s\S]*?)\r?\n---', source)
    data = {}
    for line in (match.group(1).splitlines() if match else []):
        if ':' in line and not line.lstrip().startswith('#'):
            key, value = line.split(':', 1)
            data[key.strip()] = value.strip().strip('"\'')
    return data


def post_summary(path: str, source: str) -> dict:
    meta = frontmatter(source)
    number = meta.get('number', '')
    return {
        'slug': text(path.rsplit('/', 1)[-1].removesuffix('.md'), 120),
        'title': text(meta.get('title'), 200),
        'date': text(meta.get('date'), 10),
        'track': text(meta.get('track'), 40),
        'number': int(number) if number.isdigit() else None,
        'draft': meta.get('draft') == 'true',
        'todos': source.count('TODO('),
    }


def checks_summary(runs: list[dict], truncated: bool = False) -> dict:
    """Overall state of a commit's check runs: failing, pending, passing, none, or
    incomplete when not every run was read and none of those read failed."""
    items = [{'name': text(r.get('name'), 100), 'status': text(r.get('status'), 20),
              'conclusion': text(r.get('conclusion'), 20)} for r in runs]
    if any(i['conclusion'] in FAILING for i in items):
        state = 'failing'
    elif truncated:
        state = 'incomplete'  # an unread run could be failing or still running
    elif not items:
        state = 'none'
    elif any(i['status'] != 'completed' for i in items):
        state = 'pending'
    elif all(i['conclusion'] in PASSING for i in items):
        state = 'passing'
    else:
        state = 'pending'
    quality = next((i for i in items if i['name'] == 'quality'), None)
    missing = 'not among the runs read' if truncated else 'missing'
    return {'state': state, 'quality': quality['conclusion'] or quality['status'] if quality else missing,
            'runs': sorted(items, key=lambda i: i['name'])}


def preview_url(repo: Repository, sha: str) -> str:
    """The Vercel preview for a commit, from the GitHub deployment Vercel reports.
    Only an https *.vercel.app address is accepted."""
    for deployment in repo.deployments(sha):
        if not text(deployment.get('environment'), 100).lower().startswith('preview'):
            continue
        status = repo.latest_deployment_status(deployment['id']) or {}
        url = text(status.get('environment_url'), 300)
        parts = urlsplit(url)
        if status.get('state') == 'success' and parts.scheme == 'https' and parts.hostname \
                and parts.hostname.endswith('.vercel.app') and not parts.username:
            return url
    return ''


def pull_entry(repo: Repository, summary: dict, renew=lambda: None) -> dict:
    number = int(summary['number'])
    detail = repo.pull(number)
    sha = text(detail['head']['sha'], 40)
    if not SHA.match(sha):
        raise GitHubError(f'Unexpected head commit for PR {number}.')
    files, files_truncated = repo.pull_files(number)
    kinds = sorted({classify(text(f.get('filename'), 300)) for f in files})
    posts = []
    for changed in files:
        name = text(changed.get('filename'), 300)
        if classify(name) == 'blog' and name.endswith('.md') and changed.get('status') != 'removed':
            renew()
            posts.append(post_summary(name, repo.read_file(name, sha)))
    return {
        'number': number,
        'title': text(detail.get('title')),
        'author': text((detail.get('user') or {}).get('login'), 60),
        'draft': bool(detail.get('draft')),
        'branch': text(detail['head'].get('ref'), 120),
        'sha': sha,
        'mergeable_state': text(detail.get('mergeable_state'), 20),
        'updated_at': text(detail.get('updated_at'), 25),
        'kinds': kinds,
        'files': len(files),
        'files_truncated': files_truncated,
        'posts': posts,
        'checks': checks_summary(*repo.check_runs(sha)),
        'preview_url': preview_url(repo, sha),
    }


def blog_posts(repo: Repository, ref: str = 'main', renew=lambda: None) -> tuple[list[dict], bool]:
    """Posts on ref, newest first, and whether the list is incomplete."""
    listing = repo.list_dir(BLOG_DIR, ref)
    entries = [e for e in listing if e.get('type') == 'file' and text(e.get('name')).endswith('.md')]
    posts = []
    for entry in entries[:MAX_POSTS]:
        renew()
        path = text(entry['path'], 300)
        posts.append(post_summary(path, repo.read_file(path, ref)))
    truncated = len(entries) > MAX_POSTS or len(listing) >= DIR_LISTING_LIMIT
    return sorted(posts, key=lambda p: (p['date'], p['slug']), reverse=True), truncated


def build(repo: Repository, renew=lambda: None) -> dict:
    """renew() is called between GitHub calls so the worker can keep its heartbeat and
    lease current during a long refresh; it raises to abandon a refresh it no longer owns."""
    summaries, pulls_truncated = repo.open_pulls()
    pulls = []
    for summary in summaries:
        renew()
        pulls.append(pull_entry(repo, summary, renew))
    posts, posts_truncated = blog_posts(repo, renew=renew)
    # Anything the refresh could not read in full. A release number reserved by a
    # post that was not read could otherwise look free, so none is suggested then.
    incomplete = (['Only the most recently updated open pull requests were read.'] if pulls_truncated else []) + [
        f'Only the first changed files of pull request #{pull["number"]} were read.'
        for pull in pulls if pull['files_truncated']] + (
        ['Not every post on main was read.'] if posts_truncated else [])
    numbers = [p['number'] for p in posts if p['number']] + [
        post['number'] for pull in pulls for post in pull['posts'] if post['number']]
    return {
        'repository': repo.full_name,
        'pulls': pulls,
        'posts': posts,
        'incomplete': incomplete,
        'next_release_number': None if incomplete else max(numbers, default=0) + 1,
        'collected_at': timezone.now().isoformat(),
    }
