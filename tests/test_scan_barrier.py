from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import unittest

from navidrome_scripts.locking import library_lock,tag_state,begin_tag_write,finish_tag_write,require_tag_scan


class ScanBarrierTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.journal=self.root/'repair.jsonl'

    def test_existing_full_scan_before_tags_is_not_a_valid_handoff(self):
        old=datetime.now(timezone.utc)-timedelta(seconds=10)
        with library_lock(self.root):
            begin_tag_write(self.root,self.journal,'metadata');finish_tag_write(self.root,self.journal)
        with self.assertRaisesRegex(ValueError,'new full'):
            require_tag_scan(self.root,dict(scanType='full',lastScan=old.isoformat()))
        new=datetime.now(timezone.utc)+timedelta(seconds=1)
        self.assertEqual(require_tag_scan(self.root,dict(scanType='full',lastScan=new.isoformat()))['status'],'complete')
        with self.assertRaisesRegex(ValueError,'new full'):
            require_tag_scan(self.root,dict(scanType='quick',lastScan=new.isoformat()))

    def test_interrupted_write_requires_its_own_resume_or_rollback(self):
        with library_lock(self.root):
            begin_tag_write(self.root,self.journal,'metadata')
        with self.assertRaisesRegex(ValueError,'interrupted'):
            require_tag_scan(self.root,dict(scanType='full',lastScan=datetime.now(timezone.utc).isoformat()))
        with library_lock(self.root):
            with self.assertRaisesRegex(ValueError,'unfinished tag journal'):
                begin_tag_write(self.root,self.root/'another.jsonl','other metadata')
            finish_tag_write(self.root,self.root/'another.jsonl')
            self.assertEqual(tag_state(self.root)['status'],'writing')
            finish_tag_write(self.root,self.journal)
        self.assertEqual(tag_state(self.root)['status'],'complete')

    def test_receipt_preserves_the_lock_inode_and_uses_no_extra_state_files(self):
        with library_lock(self.root):
            inode=(self.root/'.navidrome-scripts-repair.lock').stat().st_ino
            begin_tag_write(self.root,self.journal,'metadata');finish_tag_write(self.root,self.journal)
        self.assertEqual((self.root/'.navidrome-scripts-repair.lock').stat().st_ino,inode)
        self.assertEqual(len(list(self.root.iterdir())),1)
        with library_lock(self.root):
            self.assertEqual(tag_state(self.root)['status'],'complete')

    def test_partial_state_or_missing_timezone_does_not_silently_allow_renaming(self):
        lock=self.root/'.navidrome-scripts-repair.lock';lock.write_bytes(b'0{')
        with self.assertRaisesRegex(ValueError,'incomplete'):
            tag_state(self.root)
        lock.write_bytes(b'0'+json.dumps(dict(schema=1,status='complete',finished='2026-10-21T00:00:00')).encode())
        with self.assertRaisesRegex(ValueError,'cannot verify'):
            require_tag_scan(self.root,dict(scanType='full',lastScan='2026-10-22T00:00:00Z'))


if __name__=='__main__':
    unittest.main()
