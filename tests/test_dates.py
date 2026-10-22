import json
from pathlib import Path
import struct
import tempfile
import unittest

from mutagen.flac import FLAC
from navidrome_scripts import dates


def fixture(path):
    # Minimal STREAMINFO metadata for a lossless tag round-trip; not a decoder fixture.
    data = bytearray(34)
    data[0:4] = struct.pack('>HH', 4096, 4096)
    data[10:18] = ((44100 << 44) | (1 << 41) | (15 << 36) | 44100).to_bytes(8, 'big')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'fLaC\x80\x00\x00\x22' + data)
    audio = FLAC(path)
    audio['date'] = '2003'
    audio['originaldate'] = '0001-01-01'
    audio['originalyear'] = '1'
    audio['replaygain_track_gain'] = '-7.0 dB'
    audio['lyrics'] = 'keep these lyrics'
    audio.save()


class DateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'music'
        self.path = self.root / 'Artist/Album (1)/song.flac'
        fixture(self.path)
        self.audit = Path(self.temp.name) / 'audit.jsonl'
        self.plan = Path(self.temp.name) / 'plan.json'
        self.journal = Path(self.temp.name) / 'journal.jsonl'

    def plan_repair(self):
        self.assertEqual(dates.audit(self.root, self.audit), 0)
        dates.make_plan(self.root, self.audit, self.plan)

    def test_date_precision_and_invalid_dates(self):
        for value in ('2003', '2003-02', '2004-02-29', '2024-01-01T00:00:00Z', ''):
            self.assertEqual(dates.date_problem(value), '')
        for value in ('1', '0001-01-01', '0', '0000'):
            self.assertEqual(dates.date_problem(value), 'invalid placeholder year')
        self.assertEqual(dates.date_problem('2003-02-29'), 'invalid calendar date')
        self.assertIn('manually', dates.date_problem('999'))

    def test_audit_is_read_only_and_records_timing(self):
        before = dates.digest(self.path)
        mtime = self.path.stat().st_mtime_ns
        self.plan_repair()
        self.assertEqual(dates.digest(self.path), before)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)
        rows = [json.loads(x) for x in self.audit.read_text().splitlines()]
        self.assertTrue(rows[1]['stale_folder'])
        self.assertEqual(rows[-1]['flagged'], 1)
        self.assertIn('elapsed_seconds', rows[-1])

    def test_apply_preserves_valid_dates_metadata_mtime_and_rollback_is_exact(self):
        before = self.path.read_bytes()
        mtime = self.path.stat().st_mtime_ns
        self.plan_repair()
        dates.apply_plan(self.root, self.plan, self.journal)
        audio = FLAC(self.path)
        self.assertNotIn('originaldate', audio)
        self.assertNotIn('originalyear', audio)
        self.assertEqual(audio['date'], ['2003'])
        self.assertEqual(audio['lyrics'], ['keep these lyrics'])
        self.assertEqual(audio['replaygain_track_gain'], ['-7.0 dB'])
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)
        dates.rollback(self.root, self.journal)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)
        dates.rollback(self.root, self.journal)

    def test_changed_file_blocks_apply_before_any_write(self):
        self.plan_repair()
        audio = FLAC(self.path); audio['title'] = 'new title'; audio.save()
        with self.assertRaisesRegex(ValueError, 'changed since plan'):
            dates.apply_plan(self.root, self.plan, self.journal)
        self.assertFalse(self.journal.exists())

    def test_plan_cannot_delete_valid_dates(self):
        self.plan_repair()
        payload = json.loads(self.plan.read_text()); payload['entries'][0]['remove_keys'].append('date')
        self.plan.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, 'valid or unverified'):
            dates.apply_plan(self.root, self.plan, self.journal)

    def test_path_escape_and_incomplete_audit_rejected(self):
        with self.assertRaises(ValueError):
            dates.contained(self.root, '../other.flac')
        self.audit.write_text('{}\n')
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            dates.make_plan(self.root, self.audit, self.plan)

    def test_rollback_refuses_later_user_changes(self):
        self.plan_repair(); dates.apply_plan(self.root, self.plan, self.journal)
        audio = FLAC(self.path); audio['title'] = 'later change'; audio.save()
        with self.assertRaisesRegex(ValueError, 'changed after repair'):
            dates.rollback(self.root, self.journal)

    def test_resume_skips_already_applied_files(self):
        self.plan_repair()
        dates.apply_plan(self.root, self.plan, self.journal)
        before = self.path.read_bytes()
        dates.apply_plan(self.root, self.plan, self.journal)
        self.assertEqual(before, self.path.read_bytes())

    def test_hardlink_refused_before_writes(self):
        import os
        link = self.root / 'other.flac'
        try:
            os.link(self.path, link)
        except OSError:
            self.skipTest('filesystem cannot create hardlinks')
        self.plan_repair()
        with self.assertRaisesRegex(ValueError, 'hardlinked'):
            dates.apply_plan(self.root, self.plan, self.journal)

    def test_library_lock_excludes_overlapping_repairs(self):
        with dates.library_lock(self.root):
            with self.assertRaises(OSError):
                with dates.library_lock(self.root):
                    self.fail('overlapping lock acquired')

    def test_mixed_valid_and_invalid_values_require_review(self):
        audio = FLAC(self.path); audio['originaldate'] = ['0001-01-01', '2003']; audio.save()
        row = dates.inspect(self.path, self.root)
        self.assertIn('originaldate', row['problems'])
        self.assertNotIn('originaldate', row['remove_keys'])
