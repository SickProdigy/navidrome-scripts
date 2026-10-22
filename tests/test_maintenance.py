import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

from mutagen.flac import FLAC

from navidrome_scripts import maintenance as m
from navidrome_scripts import unmapped as u
from navidrome_scripts.subsonic import ApiError
import test_unmapped as fixtures
RELEASE = fixtures.RELEASE


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        fixtures.UnmappedTests.setUp(self)
        payload = u.plan(self.client, self.root, '/music/Artist/Album')
        u.apply(self.client, self.root, payload, self.journal)
        self.handoff = self.journal.with_suffix('.metadata-queue.json')
        self.state = Path(self.temp.name) / 'maintenance'
        self.hooks = Path(self.temp.name) / 'hooks'
        self.calls = []
        self.fail_gain = False

    def approve(self, steps=m.STEPS, profile='profile'):
        return m.approve(self.client, self.root, self.handoff, self.state, steps, profile)

    def runner(self, command, log, env, **kwargs):
        self.calls.append(command)
        if command[0] == 'beet':
            if 'list' in command:
                return RELEASE + '\n'
            if 'export' in command:
                Path(command[command.index('-o')+1]).write_text(json.dumps([dict(path=str(self.path),mb_albumid=RELEASE)]))
        elif command[0] == 'bash':
            before = self.path.stat()
            audio = FLAC(self.path)
            for tag, value in {'replaygain_track_gain': '-5 dB', 'replaygain_album_gain': '-5 dB',
                               'replaygain_track_peak': '0.8', 'replaygain_album_peak': '0.8'}.items():
                audio[tag] = value
            audio.save(); os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
            if self.fail_gain:
                self.fail_gain = False
                raise ValueError('interrupted after writing tags')
        return ''

    def work(self, **kwargs):
        return m.work(self.client, self.root, self.state, self.hooks, Path(self.temp.name), profile='profile', run=self.runner, **kwargs)

    def job(self):
        return json.loads(next((self.state / 'jobs').glob('*.json')).read_text())

    def test_approve_is_read_only_and_repeat_does_not_reset_completed_work(self):
        before = self.path.read_bytes()
        self.assertEqual(self.approve(), 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.approve(), 0)
        self.work()
        self.assertEqual(self.approve(), 0)
        calls = len(self.calls)
        self.work()
        self.assertEqual(len(self.calls), calls)

    def test_order_strict_gain_verification_and_queued_lyrics_status(self):
        self.approve()
        mtime = self.path.stat().st_mtime_ns
        self.assertEqual(self.work(), 0)
        job = self.job()
        self.assertEqual([c[0] for c in self.calls], ['beet', 'beet', 'beet', 'bash', sys.executable])
        self.assertIn('--force', self.calls[3])
        self.assertIn('--strict-enqueue', self.calls[-1])
        self.assertEqual(job['status'], 'complete_with_review')
        self.assertEqual(job['results']['beets-preview']['status'], 'review_ready')
        self.assertEqual(job['results']['lyrics']['status'], 'enqueued')
        self.assertTrue(m.gain_complete(self.path))
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)
        self.assertEqual(FLAC(self.path)['lyrics'], ['keep these lyrics'])

    def test_gain_interruption_restarts_whole_album_even_if_all_tags_exist(self):
        self.approve(); self.fail_gain = True
        self.assertEqual(self.work(now=lambda: 10000), 1)
        self.assertEqual(self.job()['status'], 'retry')
        self.assertTrue(m.gain_complete(self.path))
        count = len(self.calls)
        self.assertEqual(self.work(now=lambda: 10001), 0)
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.work(now=lambda: 14000), 0)
        self.assertEqual(sum(c[0] == 'bash' for c in self.calls), 2)

    def test_missing_gain_after_zero_exit_is_not_success(self):
        self.approve(['replaygain'])
        result = m.work(self.client, self.root, self.state, self.hooks, self.root, profile='profile', run=lambda *a, **k: '')
        self.assertEqual(result, 1)
        self.assertNotIn('replaygain', self.job()['results'])

    def test_user_file_change_and_new_extra_audio_block_processing(self):
        self.approve()
        (self.path.parent / 'unexpected.mp3').write_bytes(b'other recording')
        self.assertEqual(self.work(), 1)
        self.assertEqual(self.job()['status'], 'needs_review')
        self.assertEqual(self.calls, [])

    def test_incompatible_profile_pauses_without_discarding_job(self):
        self.approve(profile='old-profile')
        self.assertEqual(self.work(), 0)
        self.assertEqual(self.job()['status'], 'pending')
        self.assertEqual(self.calls, [])

    def test_unrelated_tag_changes_from_hook_require_review(self):
        self.approve(['replaygain'])
        def bad(command, *args, **kwargs):
            self.runner(command, *args, **kwargs)
            stamp = self.path.stat()
            audio = FLAC(self.path); audio['artist'] = 'Wrong artist'; audio.save()
            os.utime(self.path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertEqual(m.work(self.client, self.root, self.state, self.hooks, self.root, profile='profile', run=bad), 1)
        self.assertEqual(self.job()['status'], 'needs_review')

    def test_preview_never_enables_copy_move_or_tag_writes(self):
        self.approve(['beets-preview']); self.work()
        command = self.calls[0]
        for flag in ('-C', '-M', '-W', '--search-id', '--flat'):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index('--search-id') + 1], RELEASE)
        config = json.loads(Path(command[command.index('-c') + 1]).read_text())
        self.assertEqual(config['plugins'], ['musicbrainz','export'])
        self.assertFalse(config['import']['write'])

    def test_changed_handoff_file_cannot_be_approved(self):
        audio = FLAC(self.path); audio['title'] = 'New title'; audio.save()
        with self.assertRaisesRegex(ValueError, 'changed before'):
            self.approve()

    def test_audit_schedule_records_timing_and_skips_until_due(self):
        self.assertEqual(m.audit(self.client, self.root, self.state, clock=lambda: 100000), 0)
        status = json.loads((self.state / 'audit-status.json').read_text())
        self.assertIn('started', status); self.assertIn('ended', status); self.assertIn('elapsed_seconds', status)
        self.client.get = Mock(side_effect=AssertionError('should not fetch again'))
        self.assertEqual(m.audit(self.client, self.root, self.state, clock=lambda: 100100), 0)

    def test_failed_or_interrupted_audit_retries_after_one_hour(self):
        for status in ('failed', 'running'):
            with self.subTest(status=status):
                marker = self.state / 'audit-status.json'
                self.state.mkdir(parents=True, exist_ok=True)
                marker.write_text(json.dumps(dict(server=self.client.url, root=str(self.root.resolve()),
                    remote_root='/music', attempt=100000, status=status)))
                original = self.client.get
                self.client.get = Mock(side_effect=ApiError('still offline'))
                self.assertEqual(m.audit(self.client, self.root, self.state, clock=lambda: 103599), 0)
                self.client.get.assert_not_called()
                self.client.get = original
                self.assertEqual(m.audit(self.client, self.root, self.state, clock=lambda: 103600), 0)
                self.assertIn(json.loads(marker.read_text())['status'], ('complete', 'complete_with_errors'))

    def test_failed_audit_retains_failure_and_does_not_repeat_every_minute(self):
        self.client.get = Mock(side_effect=ApiError('offline'))
        with self.assertRaises(ApiError):
            m.audit(self.client, self.root, self.state, clock=lambda: 100000)
        self.assertEqual(json.loads((self.state / 'audit-status.json').read_text())['status'], 'failed')
        self.assertEqual(m.audit(self.client, self.root, self.state, clock=lambda: 100100), 0)
