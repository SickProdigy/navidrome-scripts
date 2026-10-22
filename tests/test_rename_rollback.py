import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from navidrome_scripts import rename_rollback as rollback, renames
from navidrome_scripts.subsonic import ApiError
import test_renames as forward_tests


class RollbackLidarr(forward_tests.RenameLidarr):
    def __init__(self, root):
        super().__init__(root)
        self.rescan = None
        self.rescan_running = False
        self.rescan_unknown = False
        self.bad_assignment = False

    def get(self, endpoint, **params):
        if endpoint == 'command':
            rows = [dict(id=9, status='started' if self.queued else 'completed', body=dict(name='RenameFiles'))]
            if self.rescan:
                rows.append(dict(id=10, status='started' if self.rescan_running else 'completed', body=self.rescan))
            return copy.deepcopy(rows)
        if endpoint == 'command/10':
            return dict(id=10, status='started' if self.rescan_running else 'completed')
        return super().get(endpoint, **params)

    def finish_rescan(self):
        self.rescan_running = False
        self.mapped = {16: dict(id=16, path=self.rename['existingPath'], albumId=2)}
        self.track['trackFileId'] = 999 if self.bad_assignment else 16

    def post(self, endpoint, body):
        if body.get('name') != 'RescanFolders':
            return super().post(endpoint, body)
        assert body['addNewArtists'] is False
        assert body['filter'] == 'known'
        assert body['artistIds'] == [1]
        assert body['folders'] == ['/music/Artist/Album', '/music/Artist/Album (2003)']
        self.rescan = copy.deepcopy(body)
        self.posts.append((endpoint, copy.deepcopy(body)))
        if not self.rescan_running:
            self.finish_rescan()
        if self.rescan_unknown:
            raise ApiError('rescan outcome unknown')
        return dict(id=10)


class RollbackTests(unittest.TestCase):
    def setUp(self):
        forward_tests.RenameTests.setUp(self)
        self.client = RollbackLidarr(self.root)
        self.forward = renames.plan(self.client, self.nav, self.root, 2, prefix='Music-Library')
        renames.apply(self.client, self.nav, self.root, self.forward, self.journal)
        self.nav.last_scan = '2026-01-01T00:00:00+00:00'
        self.nav.rows[0]['path'] = 'Music-Library/Artist/Album (2003)/renamed.flac'
        self.reverse = rollback.plan(self.client, self.nav, self.root, self.forward, self.journal)
        self.reverse_journal = Path(self.temp.name) / 'rollback.json'

    def apply(self):
        return rollback.apply(self.client, self.nav, self.root, self.reverse, self.reverse_journal)

    def scanned(self):
        self.nav.rows[0]['path'] = 'Music-Library/Artist/Album/song.flac'
        self.nav.last_scan = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()

    def test_preview_and_complete_restore_preserve_files_and_track_bindings(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(len(self.client.posts), 1)
        before = self.reverse['entries'][0]
        self.assertEqual(self.apply(), 0)
        self.assertEqual(renames.file_state(self.path), rollback.expected(before))
        self.assertTrue(self.art.is_file())
        self.assertTrue(self.lyrics.is_file())
        self.assertEqual(self.client.track['trackFileId'], 16)
        with self.assertRaisesRegex(ValueError, 'new full'):
            rollback.verify(self.client, self.nav, self.root, self.reverse, self.reverse_journal)
        self.scanned()
        self.assertEqual(rollback.verify(self.client, self.nav, self.root, self.reverse, self.reverse_journal), 0)
        self.assertEqual(self.apply(), 0)
        self.assertEqual(len(self.client.posts), 2)
        with self.assertRaisesRegex(ValueError, 'rollback has started'):
            renames.apply(self.client, self.nav, self.root, self.forward, self.journal)

    def test_changed_original_or_renamed_copy_is_never_overwritten(self):
        for changed in (self.path, self.root/'Artist/Album (2003)/renamed.flac'):
            with self.subTest(path=changed):
                changed.parent.mkdir(parents=True, exist_ok=True)
                original = changed.read_bytes() if changed.exists() else None
                original_stat = changed.stat() if changed.exists() else None
                changed.write_bytes(b'new unrelated content')
                with self.assertRaisesRegex(ValueError, 'changed rollback path'):
                    self.apply()
                self.assertEqual(changed.read_bytes(), b'new unrelated content')
                if original is None:
                    changed.unlink()
                else:
                    import os
                    changed.write_bytes(original)
                    os.utime(changed, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        self.assertEqual(len(self.client.posts), 1)

    def test_corrupt_backup_blocks_all_changes(self):
        Path(self.reverse['entries'][0]['backup']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'backup missing or changed'):
            self.apply()
        self.assertFalse(self.path.exists())
        self.assertEqual(len(self.client.posts), 1)

    def test_extra_files_require_review_and_are_retained(self):
        extra = self.root/'Artist/Album (2003)/new-download.flac'
        extra.write_bytes(b'new download')
        with self.assertRaisesRegex(ValueError, 'extra file'):
            self.apply()
        self.assertEqual(extra.read_bytes(), b'new download')
        self.assertFalse(self.path.exists())
        self.assertEqual(len(self.client.posts), 1)

    def test_missing_sidecar_can_be_restored_from_verified_backup(self):
        (self.root/'Artist/Album (2003)/cover.jpg').unlink()
        self.assertEqual(self.apply(), 0)
        self.assertEqual(self.art.read_bytes(), b'original artwork')
        self.assertFalse((self.root/'Artist/Album (2003)').exists())

    def test_interrupted_copy_resumes_without_resubmitting_rescan(self):
        real = rollback.copy_exclusive
        def interrupted(*args):
            real(*args)
            raise OSError('interrupted after exact copy')
        with patch.object(rollback, 'copy_exclusive', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(self.apply(), 0)
        self.assertEqual(len(self.client.posts), 2)

    def test_queue_and_lost_response_reconcile_one_original_command(self):
        self.client.rescan_running = True
        self.client.rescan_unknown = True
        with self.assertRaises(ApiError):
            self.apply()
        self.assertEqual(self.apply(), 2)
        self.client.finish_rescan()
        self.assertEqual(self.apply(), 0)
        self.assertEqual(len(self.client.posts), 2)

    def test_unknown_without_matching_command_never_retries(self):
        self.client.rescan_unknown = True
        with self.assertRaises(ApiError):
            self.apply()
        self.client.rescan = None
        with self.assertRaisesRegex(ValueError, 'outcome unknown'):
            self.apply()
        self.assertEqual(len(self.client.posts), 2)

    def test_assignment_and_personal_data_regressions_are_not_success(self):
        self.client.bad_assignment = True
        with self.assertRaisesRegex(ValueError, 'track binding'):
            self.apply()
        self.client.bad_assignment = False
        self.client.finish_rescan()
        self.assertEqual(self.apply(), 0)
        self.scanned(); self.nav.rows[0]['userRating'] = 0
        self.assertEqual(rollback.verify(self.client, self.nav, self.root, self.reverse, self.reverse_journal), 1)
        self.assertEqual(json.loads(self.reverse_journal.read_text())['status'], 'preservation_failed')

    def test_inflight_commands_and_changed_review_block_restoration(self):
        self.client.queued = True
        with self.assertRaisesRegex(ValueError, 'queued/running'):
            self.apply()
        self.client.queued = False
        self.nav.last_scan = 'different-scan'
        with self.assertRaisesRegex(ValueError, 'changed since rollback review'):
            self.apply()
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
