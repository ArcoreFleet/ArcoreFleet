import base64
from datetime import timedelta
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from jose import jwt

from website import github, snapshot
from website.models import WebsiteSync
from website.sync import SyncBusy, sync

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
PUBLIC_PEM = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
SHA = 'a' * 40

POST = """---
title: demo-tool: catch problems early
date: 2026-11-06
track: release
number: 7
summary: A summary.
draft: true
---

**Release #7 in our open-source series.** TODO(sentence) and TODO(more).
"""


def response(status, body):
    return mock.Mock(status_code=status, json=mock.Mock(return_value=body))


class FakeRepository:
    """Stands in for github.Repository with canned API data."""
    full_name = 'owner/site'

    def __init__(self, deployments=None, statuses=None, runs=None):
        self._deployments = deployments if deployments is not None else [{'id': 5, 'environment': 'Preview'}]
        self._statuses = statuses if statuses is not None else [
            {'state': 'success', 'environment_url': 'https://site-git-blog-owner.vercel.app'}]
        self._runs = runs if runs is not None else [
            {'name': 'quality', 'status': 'completed', 'conclusion': 'success'},
            {'name': 'snapshot', 'status': 'completed', 'conclusion': 'skipped'}]

    def open_pulls(self):
        return [{'number': 31}], False

    def pull(self, number):
        return {'number': number, 'title': '<b>Draft release #7</b>', 'user': {'login': 'jare'}, 'draft': True,
                'head': {'sha': SHA, 'ref': 'blog/demo-tool'}, 'mergeable_state': 'clean', 'updated_at': '2026-11-04T10:00:00Z'}

    def pull_files(self, number):
        return [{'filename': 'content/blog/demo-tool.md', 'status': 'added'},
                {'filename': 'lib/data/services.json', 'status': 'modified'},
                {'filename': 'content/blog/gone.md', 'status': 'removed'}], False

    def check_runs(self, sha):
        return self._runs, False

    def deployments(self, sha):
        return self._deployments

    def latest_deployment_status(self, deployment_id):
        return self._statuses[0] if self._statuses else None

    def list_dir(self, path, ref):
        return [{'type': 'file', 'name': 'old.md', 'path': 'content/blog/old.md'},
                {'type': 'dir', 'name': 'img', 'path': 'content/blog/img'}]

    def read_file(self, path, ref):
        if path.endswith('old.md'):
            return '---\ntitle: old: post\ndate: 2026-10-22\ntrack: release\nnumber: 6\ndraft: false\n---\nBody.\n'
        return POST


class GitHubAppTests(TestCase):
    def test_app_jwt_is_short_lived_and_signed_by_the_app_key(self):
        token = github.app_jwt('123', PRIVATE_PEM, now=1_800_000_000)
        claims = jwt.decode(token, PUBLIC_PEM, algorithms=['RS256'], options={'verify_exp': False})
        self.assertEqual(claims['iss'], '123')
        self.assertEqual(claims['iat'], 1_800_000_000 - 60)
        self.assertEqual(claims['exp'] - claims['iat'], 540)

    def test_installation_token_asks_for_the_given_permissions_and_is_cached(self):
        tokens = github.InstallationTokens('123', '456', PRIVATE_PEM)
        expires = (timezone.now() + timedelta(hours=1)).isoformat().replace('+00:00', 'Z')
        with mock.patch('website.github.requests.post', return_value=response(201, {'token': 'ghs_x', 'expires_at': expires})) as post:
            self.assertEqual(tokens.token(github.READ_PERMISSIONS), 'ghs_x')
            self.assertEqual(tokens.token(github.READ_PERMISSIONS), 'ghs_x')
        post.assert_called_once()
        self.assertIn('/app/installations/456/access_tokens', post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs['json'], {'permissions': github.READ_PERMISSIONS})
        self.assertEqual(set(github.READ_PERMISSIONS.values()), {'read'})

    def test_failures_never_include_response_bodies(self):
        tokens = github.InstallationTokens('123', '456', PRIVATE_PEM)
        with mock.patch('website.github.requests.post', return_value=response(401, {'message': 'secret detail'})):
            with self.assertRaisesRegex(github.GitHubError, 'HTTP 401') as caught:
                tokens.token(github.READ_PERMISSIONS)
        self.assertNotIn('secret detail', str(caught.exception))

    def test_file_paths_are_percent_encoded_except_separators(self):
        tokens = mock.Mock(token=mock.Mock(return_value='ghs_x'))
        body = {'encoding': 'base64', 'content': base64.b64encode(b'x').decode()}
        with mock.patch('website.github.requests.get', return_value=response(200, body)) as get:
            github.Repository('owner/site', tokens).read_file('content/blog/c#-release?v=1%.md', 'main')
        self.assertEqual(get.call_args.args[0],
                         'https://api.github.com/repos/owner/site/contents/content/blog/c%23-release%3Fv%3D1%25.md')

    def test_open_pulls_follows_pagination_and_reports_a_cap(self):
        repo = github.Repository('owner/site', mock.Mock(token=mock.Mock(return_value='ghs_x')))
        pages = [response(200, [{'number': n} for n in range(100)]), response(200, [{'number': 100}])]
        with mock.patch('website.github.requests.get', side_effect=pages) as get:
            pulls, truncated = repo.open_pulls()
        self.assertEqual((len(pulls), truncated), (101, False))
        self.assertEqual([c.kwargs['params']['page'] for c in get.call_args_list], [1, 2])
        full = [response(200, [{'number': n} for n in range(100)]) for _ in range(2)]
        with mock.patch('website.github.requests.get', side_effect=full):
            self.assertEqual(repo.open_pulls(max_pages=2)[1], True)

    def test_missing_credentials_are_reported_as_not_configured(self):
        with self.assertRaises(github.NotConfigured):
            github.InstallationTokens('', '456', PRIVATE_PEM)
        with override_settings(WEBSITE_GITHUB_PRIVATE_KEY_FILE='/nonexistent/key.pem'), self.assertRaises(github.NotConfigured):
            github.InstallationTokens.from_settings()

    def test_repository_reads_files_with_the_read_token(self):
        tokens = mock.Mock(token=mock.Mock(return_value='ghs_x'))
        repo = github.Repository('owner/site', tokens)
        body = {'encoding': 'base64', 'content': base64.b64encode(b'hello').decode()}
        with mock.patch('website.github.requests.get', return_value=response(200, body)) as get:
            self.assertEqual(repo.read_file('content/blog/a.md', 'main'), 'hello')
        tokens.token.assert_called_with(github.READ_PERMISSIONS)
        self.assertEqual(get.call_args.args[0], 'https://api.github.com/repos/owner/site/contents/content/blog/a.md')
        self.assertFalse(get.call_args.kwargs['allow_redirects'])


class SnapshotTests(TestCase):
    def test_build_summarizes_pulls_posts_checks_and_previews(self):
        data = snapshot.build(FakeRepository())
        pull = data['pulls'][0]
        self.assertEqual(pull['number'], 31)
        self.assertEqual(pull['kinds'], ['blog', 'catalog'])
        self.assertEqual(pull['files'], 3)
        self.assertEqual(pull['checks']['state'], 'passing')
        self.assertEqual(pull['checks']['quality'], 'success')
        self.assertEqual(pull['preview_url'], 'https://site-git-blog-owner.vercel.app')
        self.assertEqual(pull['posts'], [{'slug': 'demo-tool', 'title': 'demo-tool: catch problems early', 'date': '2026-11-06',
                                          'track': 'release', 'number': 7, 'draft': True, 'todos': 2}])
        self.assertEqual([p['slug'] for p in data['posts']], ['old'])
        self.assertEqual(data['next_release_number'], 8)

    def test_any_incomplete_inventory_suggests_no_release_number(self):
        self.assertEqual(snapshot.build(FakeRepository())['incomplete'], [])
        pulls = FakeRepository()
        pulls.open_pulls = lambda: ([{'number': 31}], True)
        files = FakeRepository()
        files.pull_files = lambda number: (FakeRepository.pull_files(files, number)[0], True)
        posts = FakeRepository()
        posts.list_dir = lambda path, ref: [{'type': 'file', 'name': f'p{n}.md', 'path': f'content/blog/p{n}.md'}
                                            for n in range(snapshot.MAX_POSTS + 1)]
        listing = FakeRepository()
        listing.list_dir = lambda path, ref: [{'type': 'dir', 'name': f'd{n}', 'path': f'd{n}'}
                                              for n in range(snapshot.DIR_LISTING_LIMIT)]
        for repo, reason in ((pulls, 'most recently updated open pull requests'),
                             (files, 'first changed files of pull request #31'),
                             (posts, 'Not every post on main'), (listing, 'Not every post on main')):
            data = snapshot.build(repo)
            self.assertIsNone(data['next_release_number'], reason)
            self.assertIn(reason, ' '.join(data['incomplete']))
        self.assertEqual(len(snapshot.build(posts)['posts']), snapshot.MAX_POSTS)
        self.assertTrue(snapshot.build(files)['pulls'][0]['files_truncated'])

    def test_pull_files_follow_pagination(self):
        repo = github.Repository('owner/site', mock.Mock(token=mock.Mock(return_value='ghs_x')))
        pages = [response(200, [{'filename': f'f{n}'} for n in range(100)]), response(200, [{'filename': 'last'}])]
        with mock.patch('website.github.requests.get', side_effect=pages) as get:
            files, truncated = repo.pull_files(7)
        self.assertEqual((len(files), truncated), (101, False))
        self.assertTrue(get.call_args.args[0].endswith('/pulls/7/files'))

    def test_build_renews_between_github_calls(self):
        renew = mock.Mock()
        snapshot.build(FakeRepository(), renew)
        # Once per pull request, per changed post in it, and per post on main.
        self.assertEqual(renew.call_count, 3)

    def test_check_runs_follow_pagination_to_total_count(self):
        repo = github.Repository('owner/site', mock.Mock(token=mock.Mock(return_value='ghs_x')))
        run = {'name': 'x', 'status': 'completed', 'conclusion': 'success'}
        pages = [response(200, {'total_count': 101, 'check_runs': [run] * 100}),
                 response(200, {'total_count': 101, 'check_runs': [run]})]
        with mock.patch('website.github.requests.get', side_effect=pages):
            runs, truncated = repo.check_runs(SHA)
        self.assertEqual((len(runs), truncated), (101, False))
        capped = [response(200, {'total_count': 500, 'check_runs': [run] * 100}) for _ in range(2)]
        with mock.patch('website.github.requests.get', side_effect=capped):
            self.assertTrue(repo.check_runs(SHA, max_pages=2)[1])

    def test_unread_check_runs_never_look_passing(self):
        ok = [{'name': 'lint', 'status': 'completed', 'conclusion': 'success'}]
        summary = snapshot.checks_summary(ok, truncated=True)
        self.assertEqual(summary['state'], 'incomplete')
        self.assertEqual(summary['quality'], 'not among the runs read')
        failed = ok + [{'name': 'quality', 'status': 'completed', 'conclusion': 'failure'}]
        self.assertEqual(snapshot.checks_summary(failed, truncated=True)['state'], 'failing')
        self.assertEqual(snapshot.checks_summary([], truncated=True)['state'], 'incomplete')

    def test_check_states(self):
        self.assertEqual(snapshot.checks_summary([])['state'], 'none')
        running = [{'name': 'quality', 'status': 'in_progress', 'conclusion': None}]
        self.assertEqual(snapshot.checks_summary(running)['state'], 'pending')
        self.assertEqual(snapshot.checks_summary(running)['quality'], 'in_progress')
        failed = running + [{'name': 'lint', 'status': 'completed', 'conclusion': 'failure'}]
        self.assertEqual(snapshot.checks_summary(failed)['state'], 'failing')
        self.assertEqual(snapshot.checks_summary([{'name': 'other', 'status': 'completed', 'conclusion': 'success'}])['quality'], 'missing')

    def test_only_successful_https_vercel_previews_are_linked(self):
        for statuses in (
            [{'state': 'success', 'environment_url': 'http://site.vercel.app'}],
            [{'state': 'success', 'environment_url': 'https://evil.example.com/site.vercel.app'}],
            [{'state': 'failure', 'environment_url': 'https://site.vercel.app'}],
            [{'state': 'success', 'environment_url': 'https://user:pw@site.vercel.app'}],
        ):
            self.assertEqual(snapshot.preview_url(FakeRepository(statuses=statuses), SHA), '', statuses)
        # Only a deployment's newest status counts: an older success after a failed redeploy does not.
        redeployed = FakeRepository(statuses=[{'state': 'failure', 'environment_url': 'https://site.vercel.app'},
                                              {'state': 'success', 'environment_url': 'https://site.vercel.app'}])
        self.assertEqual(snapshot.preview_url(redeployed, SHA), '')
        production = FakeRepository(deployments=[{'id': 1, 'environment': 'Production'}])
        self.assertEqual(snapshot.preview_url(production, SHA), '')

    def test_unexpected_head_commit_is_rejected(self):
        repo = FakeRepository()
        repo.pull = lambda number: {'head': {'sha': '../../x', 'ref': 'x'}}
        with self.assertRaises(github.GitHubError):
            snapshot.build(repo)

    def test_classify(self):
        self.assertEqual(snapshot.classify('content/blog/a.md'), 'blog')
        self.assertEqual(snapshot.classify('lib/data/ArcoreServiceCatalogMaster.md'), 'catalog')
        self.assertEqual(snapshot.classify('docs/project-metadata/repository-evidence.json'), 'catalog')
        self.assertEqual(snapshot.classify('content/guides/x.md'), 'content')
        self.assertEqual(snapshot.classify('app/page.tsx'), 'code')


class SyncTests(TestCase):
    def test_success_stores_snapshot_and_clears_the_lease(self):
        with mock.patch('website.sync.github.Repository', return_value=FakeRepository()):
            self.assertTrue(sync(force=True, tokens=mock.Mock()))
        control = WebsiteSync.load()
        self.assertEqual(control.snapshot['pulls'][0]['number'], 31)
        self.assertIsNotNone(control.checked_at)
        self.assertEqual((control.error, control.lease_token, control.lease_until), ('', '', None))

    def test_failure_keeps_the_last_good_snapshot(self):
        WebsiteSync.objects.update_or_create(pk=1, defaults={'snapshot': {'pulls': [{'number': 1}]}, 'checked_at': timezone.now()})
        broken = FakeRepository()
        broken.open_pulls = mock.Mock(side_effect=github.GitHubError('HTTP 500'))
        with mock.patch('website.sync.github.Repository', return_value=broken):
            self.assertFalse(sync(force=True, tokens=mock.Mock()))
        control = WebsiteSync.load()
        self.assertEqual(control.snapshot, {'pulls': [{'number': 1}]})
        self.assertIn('Refresh failed', control.error)

    @override_settings(WEBSITE_GITHUB_PRIVATE_KEY_FILE='/nonexistent/key.pem')
    def test_not_configured_is_recorded(self):
        self.assertFalse(sync(force=True))
        self.assertEqual(WebsiteSync.load().error, 'GitHub App private key not mounted.')
        with self.assertRaisesRegex(CommandError, 'not mounted'):
            call_command('sync_website')

    def test_nothing_runs_unless_due_requested_or_forced(self):
        WebsiteSync.objects.update_or_create(pk=1, defaults={'started_at': timezone.now()})
        with mock.patch('website.sync.snapshot.build') as build:
            self.assertIsNone(sync(tokens=mock.Mock()))
        build.assert_not_called()

    def test_long_refresh_keeps_heartbeat_and_lease_current(self):
        seen = []

        def build(repo, renew):
            before = WebsiteSync.load()
            renew()
            seen.append((before.heartbeat_at, WebsiteSync.load().heartbeat_at, WebsiteSync.load().lease_until))
            return {'pulls': []}

        with mock.patch('website.sync.snapshot.build', side_effect=build), \
                mock.patch('website.sync.github.Repository'):
            self.assertTrue(sync(force=True, tokens=mock.Mock()))
        before, after, lease = seen[0]
        self.assertGreaterEqual(after, before)
        self.assertGreater(lease, timezone.now())

    def test_a_refresh_that_lost_its_lease_stops_without_writing(self):
        WebsiteSync.objects.update_or_create(pk=1, defaults={'snapshot': {'pulls': [{'number': 1}]}})

        def build(repo, renew):
            WebsiteSync.objects.filter(pk=1).update(lease_token='someone-else')
            renew()

        with mock.patch('website.sync.snapshot.build', side_effect=build), \
                mock.patch('website.sync.github.Repository'), self.assertRaises(SyncBusy):
            sync(force=True, tokens=mock.Mock())
        self.assertEqual(WebsiteSync.load().snapshot, {'pulls': [{'number': 1}]})

    def test_a_held_lease_blocks_a_second_worker(self):
        WebsiteSync.objects.update_or_create(pk=1, defaults={'lease_until': timezone.now() + timedelta(minutes=1)})
        with self.assertRaises(SyncBusy):
            sync(force=True, tokens=mock.Mock())


class ViewTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user('admin', is_staff=True)
        self.user = get_user_model().objects.create_user('viewer')

    def test_staff_only(self):
        self.assertEqual(self.client.get('/website/').status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/website/').status_code, 302)
        self.assertEqual(self.client.post('/website/refresh/').status_code, 302)
        self.assertIsNone(WebsiteSync.load().requested_at)

    def test_page_reads_the_snapshot_only_and_escapes_it(self):
        WebsiteSync.objects.update_or_create(pk=1, defaults={'snapshot': snapshot.build(FakeRepository()), 'checked_at': timezone.now()})
        self.client.force_login(self.admin)
        with mock.patch('website.github.requests.get') as get, mock.patch('website.github.requests.post') as post:
            page = self.client.get('/website/')
        get.assert_not_called()
        post.assert_not_called()
        self.assertContains(page, '&lt;b&gt;Draft release #7&lt;/b&gt;')
        self.assertNotContains(page, '<b>Draft release #7</b>')
        self.assertContains(page, 'https://github.com/owner/site/pull/31')
        self.assertContains(page, 'Open Vercel preview')
        self.assertContains(page, '2 TODO( left')
        self.assertContains(page, 'Next free release number: #8')
        self.assertEqual(self.client.post('/website/').status_code, 405)

    def test_post_links_encode_url_delimiters_in_filenames(self):
        post = {'slug': 'c#-release?v=1%', 'title': 'C', 'date': '2026-11-01', 'track': 'release', 'number': 1, 'todos': 0}
        WebsiteSync.objects.update_or_create(pk=1, defaults={'snapshot': {
            'pulls': [], 'posts': [{**post, 'draft': True}, {**post, 'slug': 'c#-other', 'draft': False}]}})
        self.client.force_login(self.admin)
        page = self.client.get('/website/')
        self.assertContains(page, 'blob/main/content/blog/c%23-release%3Fv%3D1%25.md')
        self.assertContains(page, 'blob/main/content/blog/c%23-other.md')
        self.assertNotContains(page, 'content/blog/c#')

    def test_incomplete_snapshot_notice(self):
        WebsiteSync.objects.update_or_create(pk=1, defaults={'snapshot': {
            'pulls': [], 'incomplete': ['Not every post on main was read.'], 'next_release_number': None}})
        self.client.force_login(self.admin)
        page = self.client.get('/website/')
        self.assertContains(page, 'This snapshot is incomplete')
        self.assertContains(page, 'Not every post on main was read.')
        self.assertNotContains(page, 'Next free release number')

    def test_refresh_is_queued_once_and_rate_limited(self):
        self.client.force_login(self.admin)
        self.assertRedirects(self.client.post('/website/refresh/'), '/website/')
        first = WebsiteSync.load().requested_at
        self.assertIsNotNone(first)
        self.client.post('/website/refresh/')
        self.assertEqual(WebsiteSync.load().requested_at, first)
        self.assertEqual(self.client.get('/website/refresh/').status_code, 405)

    def test_navigation_and_home_tile_are_staff_only(self):
        self.client.force_login(self.admin)
        home = self.client.get('/')
        self.assertContains(home, 'href="/website/"', count=2)
        self.assertContains(home, 'fleet-sidebar')
        self.assertContains(home, 'access/fleet.css')
        queue = self.client.get('/website/')
        self.assertContains(queue, 'fleet-sidebar')
        self.assertContains(queue, 'href="/website/" class="active"')
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get('/'), 'href="/website/"')
