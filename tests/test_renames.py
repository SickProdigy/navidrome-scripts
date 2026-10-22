import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from navidrome_scripts import renames as r
from navidrome_scripts import dates
from navidrome_scripts.subsonic import ApiError
import test_unmapped as fixtures
from test_duplicates import FakeNavidrome


class RenameLidarr(fixtures.FakeLidarr):
    def __init__(self, root):
        super().__init__()
        self.root=root
        self.album['releaseDate']='2003-06-01T00:00:00Z'
        self.mapped[6]=dict(id=6,path='/music/Artist/Album/song.flac',albumId=2)
        self.track.update(hasFile=True,trackFileId=6)
        self.rename=dict(artistId=1,albumId=2,trackFileId=6,existingPath=self.mapped[6]['path'],newPath='/music/Artist/Album (2003)/renamed.flac')
        self.naming=dict(standardTrackFormat='{Album Title} ({Release Year})/{Track Title}')
        self.queued=False
        self.rename_done=False
        self.lost_response=False

    def get(self,endpoint,**params):
        if endpoint=='config/naming':
            return copy.deepcopy(self.naming)
        if endpoint=='rename':
            assert params==dict(artistId=1,albumId=2)
            return [] if self.rename_done else [copy.deepcopy(self.rename)]
        if endpoint=='command/9':
            return dict(id=9,status='started' if self.queued else 'completed')
        return super().get(endpoint,**params)

    def finish(self):
        old=self.root/'Artist/Album/song.flac'
        new=self.root/'Artist/Album (2003)/renamed.flac'
        new.parent.mkdir(parents=True,exist_ok=True)
        old.rename(new)
        self.mapped[6]['path']=self.rename['newPath']
        self.rename_done=True
        self.queued=False

    def post(self,endpoint,body):
        assert endpoint=='command'
        assert body==dict(name='RenameFiles',artistId=1,files=[6])
        self.posts.append((endpoint,copy.deepcopy(body)))
        if not self.queued:
            self.finish()
        if self.lost_response:
            raise ApiError('unknown write outcome')
        return dict(id=9)


class RenameTests(unittest.TestCase):
    def setUp(self):
        fixtures.UnmappedTests.setUp(self)
        self.client=RenameLidarr(self.root)
        self.nav=FakeNavidrome(self.path)
        self.nav.scan_type='full'
        self.art=self.path.parent/'cover.jpg'
        self.art.write_bytes(b'original artwork')
        self.lyrics=self.path.with_suffix('.lrc')
        self.lyrics.write_text('[00:00]original lyrics')

    def plan(self):
        return r.plan(self.client,self.nav,self.root,2,prefix='Music-Library')

    def record(self):
        return json.loads(self.journal.read_text())

    def scanned(self):
        self.nav.last_scan='new-full-scan'
        self.nav.rows[0]['path']='Music-Library/Artist/Album (2003)/renamed.flac'

    def test_preview_read_only_and_apply_scoped_with_exact_backups(self):
        before=self.path.read_bytes();mtime=self.path.stat().st_mtime_ns
        payload=self.plan()
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(self.client.posts,[])
        self.assertEqual(len(payload['sidecars']),2)
        output=Path(self.temp.name)/'review.html'
        r.write_html(payload,output)
        self.assertIn('Album (2003)',output.read_text())
        self.assertEqual(r.apply(self.client,self.nav,self.root,payload,self.journal),0)
        target=self.root/'Artist/Album (2003)/renamed.flac'
        self.assertEqual(target.read_bytes(),before)
        self.assertEqual(target.stat().st_mtime_ns,mtime)
        record=self.record()
        self.assertEqual(record['status'],'awaiting_full_scan')
        self.assertEqual((Path(record['backups'])/'0').read_bytes(),before)
        self.assertFalse(self.art.exists())
        self.assertEqual((target.parent/'cover.jpg').read_bytes(),b'original artwork')
        self.assertEqual(target.with_suffix('.lrc').read_text(),'[00:00]original lyrics')
        r.apply(self.client,self.nav,self.root,payload,self.journal)
        self.assertEqual(len(self.client.posts),1)
        self.scanned()
        self.assertEqual(r.verify(self.client,self.nav,self.root,payload,self.journal),0)
        self.assertEqual(self.record()['status'],'verified')
        r.apply(self.client,self.nav,self.root,payload,self.journal)
        self.assertEqual(self.record()['status'],'verified')

    def test_queue_reconciles_original_command_without_resubmitting(self):
        payload=self.plan();self.client.queued=True
        r.apply(self.client,self.nav,self.root,payload,self.journal)
        r.apply(self.client,self.nav,self.root,payload,self.journal)
        self.assertTrue(self.path.exists())
        self.assertEqual(len(self.client.posts),1)
        self.client.finish()
        r.apply(self.client,self.nav,self.root,payload,self.journal)
        self.assertEqual(len(self.client.posts),1)
        self.assertEqual(self.record()['status'],'awaiting_full_scan')

    def test_lost_post_response_reconciles_finished_paths_without_retry(self):
        payload=self.plan();self.client.lost_response=True
        with self.assertRaises(ApiError):
            r.apply(self.client,self.nav,self.root,payload,self.journal)
        self.assertEqual(self.record()['status'],'outcome_unknown')
        r.apply(self.client,self.nav,self.root,payload,self.journal)
        self.assertEqual(len(self.client.posts),1)

    def test_unknown_incomplete_result_stops_instead_of_reissuing(self):
        payload=self.plan();self.client.lost_response=True;self.client.queued=True
        with self.assertRaises(ApiError):
            r.apply(self.client,self.nav,self.root,payload,self.journal)
        with self.assertRaisesRegex(ValueError,'incomplete'):
            r.apply(self.client,self.nav,self.root,payload,self.journal)
        self.assertEqual(len(self.client.posts),1)
        self.assertTrue(self.path.exists())

    def test_invalid_date_active_scan_and_quick_scan_require_review(self):
        self.client.album['releaseDate']='0001-01-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError,'date'):
            self.plan()
        self.client.album['releaseDate']='2003'
        self.nav.scanning=True
        with self.assertRaisesRegex(ValueError,'scan'):
            self.plan()
        self.nav.scanning=False;self.nav.scan_type='quick'
        with self.assertRaisesRegex(ValueError,'full'):
            self.plan()

    def test_changed_preview_settings_or_files_block_before_post(self):
        for change in ('preview','settings','file'):
            with self.subTest(change=change):
                payload=self.plan()
                if change=='preview':
                    self.client.rename['newPath']='/music/Artist/Album (2003)/other.flac'
                elif change=='settings':
                    self.client.naming['standardTrackFormat']='changed'
                else:
                    self.art.write_bytes(b'changed artwork')
                with self.assertRaisesRegex(ValueError,'stale'):
                    r.apply(self.client,self.nav,self.root,payload,self.journal)
                self.assertEqual(self.client.posts,[])

    def test_destination_merge_duplicate_audio_and_hardlinks_are_blocked(self):
        target=self.root/'Artist/Album (2003)';target.mkdir()
        with self.assertRaisesRegex(ValueError,'already exists'):
            self.plan()
        target.rmdir()
        duplicate=self.path.with_name('extra.flac');duplicate.write_bytes(self.path.read_bytes())
        with self.assertRaisesRegex(ValueError,'extra/unmapped'):
            self.plan()
        duplicate.unlink()
        if os.name!='nt':
            os.link(self.path,duplicate)
            with self.assertRaisesRegex(ValueError,'hardlinked'):
                self.plan()

    def test_byte_or_timestamp_changes_after_command_fail_verification(self):
        payload=self.plan()
        r.apply(self.client,self.nav,self.root,payload,self.journal)
        path=self.root/'Artist/Album (2003)/renamed.flac'
        stamp=path.stat();os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns+1000000000))
        self.scanned()
        with self.assertRaisesRegex(ValueError,'timestamp'):
            r.verify(self.client,self.nav,self.root,payload,self.journal)

    def test_missing_sidecar_cannot_be_reported_as_success(self):
        payload=self.plan()
        original=self.client.finish
        def destructive():
            original();self.art.unlink()
        self.client.finish=destructive
        with self.assertRaisesRegex(ValueError,'sidecar'):
            r.apply(self.client,self.nav,self.root,payload,self.journal)
        self.assertEqual(self.record()['status'],'needs_review')
        backups=Path(self.record()['backups'])
        self.assertIn(b'original artwork',[p.read_bytes() for p in backups.iterdir()])

    def test_full_scan_checks_persistent_ids_ratings_and_playlist_membership(self):
        payload=self.plan();r.apply(self.client,self.nav,self.root,payload,self.journal)
        with self.assertRaisesRegex(ValueError,'full'):
            r.verify(self.client,self.nav,self.root,payload,self.journal)
        self.scanned();self.nav.rows[0]['userRating']=0
        self.assertEqual(r.verify(self.client,self.nav,self.root,payload,self.journal),1)
        self.assertEqual(self.record()['status'],'preservation_failed')

    def test_backup_failure_never_submits_rename(self):
        payload=self.plan()
        with patch.object(r.shutil,'copy2',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                r.apply(self.client,self.nav,self.root,payload,self.journal)
        self.assertEqual(self.client.posts,[])
        self.assertTrue(self.path.exists())

    def test_cue_references_and_sidecar_target_conflicts_require_review(self):
        cue=self.path.parent/'album.cue';cue.write_text('FILE song.flac WAVE')
        with self.assertRaisesRegex(ValueError,'playlist/cue'):
            self.plan()
        cue.unlink()
        self.client.rename['newPath']='/music/Artist/Album/new.flac'
        self.path.with_name('new.lrc').write_text('different lyric copy')
        with self.assertRaisesRegex(ValueError,'sidecar destination'):
            self.plan()

    def test_tracked_sidecar_already_moved_by_lidarr_is_verified(self):
        payload=self.plan();original=self.client.finish
        def move_tracked():
            original();self.lyrics.rename(self.root/'Artist/Album (2003)/renamed.lrc')
        self.client.finish=move_tracked
        self.assertEqual(r.apply(self.client,self.nav,self.root,payload,self.journal),0)
        self.assertEqual(self.record()['status'],'awaiting_full_scan')

    def test_interrupted_sidecar_copy_resumes_without_another_command(self):
        payload=self.plan()
        from navidrome_scripts import duplicates
        original=duplicates.copy_exclusive
        def interrupted(source,target,expected):
            original(source,target,expected)
            raise OSError('interrupted after copying')
        with patch.object(duplicates,'copy_exclusive',side_effect=interrupted):
            with self.assertRaises(OSError):
                r.apply(self.client,self.nav,self.root,payload,self.journal)
        self.assertEqual(len(self.client.posts),1)
        self.assertEqual(r.apply(self.client,self.nav,self.root,payload,self.journal),0)
        self.assertEqual(len(self.client.posts),1)

    def test_index_alias_and_preexisting_destination_identity_block_rename(self):
        self.nav.rows.append(dict(self.nav.rows[0],id='alias',path=self.nav.rows[0]['path'].upper().replace('MUSIC-LIBRARY','Music-Library')))
        with self.assertRaisesRegex(ValueError,'one exact indexed'):
            self.plan()
        self.nav.rows[-1]['path']='Music-Library/Artist/Album (2003)/renamed.flac'
        with self.assertRaisesRegex(ValueError,'destination already has'):
            self.plan()

    def test_destination_race_during_backup_stops_before_post(self):
        payload=self.plan();original=r.shutil.copy2
        def race(source,target):
            result=original(source,target)
            (self.root/'Artist/Album (2003)').mkdir(exist_ok=True)
            return result
        with patch.object(r.shutil,'copy2',side_effect=race):
            with self.assertRaisesRegex(ValueError,'destination appeared'):
                r.apply(self.client,self.nav,self.root,payload,self.journal)
        self.assertEqual(self.client.posts,[])
        self.assertTrue(self.path.exists())


if __name__=='__main__':
    unittest.main()
