import copy
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from mutagen.flac import FLAC

from navidrome_scripts import duplicates as d
from navidrome_scripts import navidrome_guard as guard
import test_unmapped as fixtures


class FakeNavidrome:
    base_url, username = 'http://navidrome', 'admin'

    def __init__(self, path):
        self.rows = [dict(id='kept-id', path='Music-Library/Artist/Album/song.flac', title='Song',
                         artist='Artist', album='Album', size=path.stat().st_size, duration=1,
                         userRating=5, playCount=12, starred='2020-01-01')]
        self.page_size = 500
        self.admin = True
        self.scanning = False
        self.last_scan = 'fixed'
        self.scan_type = 'quick'

    def request(self, endpoint, params=None):
        if endpoint == 'getUser':
            return dict(user=dict(adminRole=self.admin))
        if endpoint == 'getMusicFolders':
            return dict(musicFolders=dict(musicFolder=[dict(id=1,name='Music')]))
        if endpoint == 'getScanStatus':
            return dict(scanStatus=dict(scanning=self.scanning,count=len(self.rows),lastScan=self.last_scan,scanType=self.scan_type))
        if endpoint == 'search3':
            offset=params['songOffset']
            return dict(searchResult3=dict(song=copy.deepcopy(self.rows[offset:offset+self.page_size])))
        raise AssertionError(endpoint)

    def song(self, identity):
        return copy.deepcopy(next(r for r in self.rows if r['id']==identity))

    def playlists(self):
        return [dict(id='playlist')]

    def playlist(self, identity):
        return dict(id=identity,name='Personal',owner='admin',songCount=2,
                    entry=[dict(id='kept-id'),dict(id='kept-id')])


class DuplicateTests(unittest.TestCase):
    def setUp(self):
        fixtures.UnmappedTests.setUp(self)
        self.copy = self.path.with_name('duplicate.flac'); shutil.copy2(self.path,self.copy)
        self.keep = self.path.relative_to(self.root).as_posix()
        self.drop = self.copy.relative_to(self.root).as_posix()
        self.client.files = [dict(id=8,path='/music/'+self.drop,size=self.copy.stat().st_size)]
        self.client.mapped[6] = dict(id=6,path='/music/'+self.keep,albumId=2,size=self.path.stat().st_size)
        self.nav = FakeNavidrome(self.path)
        self.quarantine = Path(self.temp.name)/'quarantine'

    def plan(self):
        return d.plan(self.client,self.nav,self.root,d.audit(self.root,'Artist/Album'),self.keep,[self.drop],self.quarantine,prefix='Music-Library')

    def test_read_only_audit_and_plan_require_explicit_keeper(self):
        before=self.path.read_bytes()
        report=d.audit(self.root,'Artist/Album')
        self.assertEqual(len(report['identical']),1)
        self.assertEqual(len(report['same_recording']),1)
        self.assertEqual(self.path.read_bytes(),before)
        payload=self.plan()
        self.assertTrue(self.copy.exists())
        self.assertEqual(payload['keeper'],self.keep)

    def test_html_review_escapes_tag_content(self):
        report=d.audit(self.root,'Artist/Album')
        report['files'][0]['tags']['title']='<script>bad</script>'
        output=Path(self.temp.name)/'review.html'
        d.write_html(report,output)
        rendered=output.read_text()
        self.assertIn('&lt;script&gt;',rendered)
        self.assertNotIn('<script>',rendered)

    def test_quarantine_and_exact_restore_preserve_keeper_and_references(self):
        payload=self.plan()
        state=guard.user_snapshot(self.nav,['kept-id'])
        original=self.copy.read_bytes();mtime=self.copy.stat().st_mtime_ns
        self.assertEqual(d.apply(self.client,self.nav,self.root,payload,self.journal),0)
        self.assertFalse(self.copy.exists())
        self.assertEqual(self.path.read_bytes(),original)
        self.assertEqual(guard.user_snapshot(self.nav,['kept-id']),state)
        self.assertEqual(d.apply(self.client,self.nav,self.root,payload,self.journal),0)
        d.restore(self.root,payload,self.journal)
        self.assertEqual(self.copy.read_bytes(),original)
        self.assertEqual(self.copy.stat().st_mtime_ns,mtime)
        d.restore(self.root,payload,self.journal)
        record=json.loads(self.journal.read_text())
        self.assertTrue((Path(record['directory'])/self.drop).exists())

    def test_indexed_duplicate_is_blocked_even_without_visible_rating(self):
        self.nav.rows.append(dict(self.nav.rows[0],id='other-id',path='Music-Library/'+self.drop,userRating=0,playCount=0))
        with self.assertRaisesRegex(ValueError,'Navidrome identity'):
            self.plan()

    def test_case_alias_of_indexed_path_is_not_treated_as_unindexed(self):
        self.nav.rows.append(dict(self.nav.rows[0],id='other-id',path='Music-Library/'+self.drop.upper()))
        with self.assertRaisesRegex(ValueError,'Navidrome identity'):
            self.plan()

    def test_mapped_copy_and_wrong_prefix_cannot_be_quarantined(self):
        self.client.files=[]
        with self.assertRaisesRegex(ValueError,'not confirmed unmapped'):
            self.plan()
        self.client.files=[dict(id=8,path='/music/'+self.drop,size=self.copy.stat().st_size)]
        with self.assertRaisesRegex(ValueError,'prefix does not cover'):
            d.plan(self.client,self.nav,self.root,d.audit(self.root,'Artist/Album'),self.keep,[self.drop],self.quarantine,prefix='Wrong-Library')

    def test_different_tags_are_not_byte_identical_or_auto_selected_by_recording_id(self):
        audio=FLAC(self.copy);audio['comment']='different metadata';audio.save()
        report=d.audit(self.root,'Artist/Album')
        self.assertEqual(report['identical'],[])
        self.assertEqual(len(report['same_recording']),1)
        with self.assertRaisesRegex(ValueError,'byte-identical'):
            d.plan(self.client,self.nav,self.root,report,self.keep,[self.drop],self.quarantine,prefix='Music-Library')

    def test_unique_lyrics_and_local_playlists_require_review(self):
        lyrics=self.copy.with_suffix('.lrc');lyrics.write_text('unique lyrics')
        with self.assertRaisesRegex(ValueError,'sidecar'):
            self.plan()
        lyrics.unlink();(self.copy.parent/'album.m3u8').write_text('duplicate.flac')
        with self.assertRaisesRegex(ValueError,'playlist/cue'):
            self.plan()

    def test_changed_file_after_preview_is_not_moved(self):
        payload=self.plan()
        audio=FLAC(self.copy);audio['title']='changed';audio.save()
        with self.assertRaisesRegex(ValueError,'changed since'):
            d.apply(self.client,self.nav,self.root,payload,self.journal)
        self.assertTrue(self.copy.exists())
        self.assertFalse(self.journal.exists())

    def test_user_state_change_during_move_restores_paths_without_overwriting_user_data(self):
        payload=self.plan();original=d.copy_exclusive
        def concurrent(source,target,expected):
            original(source,target,expected)
            self.nav.rows[0]['playCount']+=1
        with patch.object(d,'copy_exclusive',side_effect=concurrent):
            with self.assertRaisesRegex(ValueError,'changed during'):
                d.apply(self.client,self.nav,self.root,payload,self.journal)
        self.assertTrue(self.copy.exists())
        self.assertGreater(self.nav.rows[0]['playCount'],12)
        self.assertEqual(json.loads(self.journal.read_text())['status'],'restored')

    def test_restore_refuses_to_replace_new_content_at_original_path(self):
        payload=self.plan();d.apply(self.client,self.nav,self.root,payload,self.journal)
        self.copy.write_bytes(b'new user file')
        with self.assertRaisesRegex(ValueError,'overwrite'):
            d.restore(self.root,payload,self.journal)
        self.assertEqual(self.copy.read_bytes(),b'new user file')

    def test_quarantine_inside_library_and_hardlinks_are_refused(self):
        report=d.audit(self.root,'Artist/Album')
        with self.assertRaisesRegex(ValueError,'separate'):
            d.plan(self.client,self.nav,self.root,report,self.keep,[self.drop],self.root/'quarantine',prefix='Music-Library')
        self.copy.unlink()
        try:
            os.link(self.path,self.copy)
        except OSError:
            self.skipTest('hardlinks unavailable on this filesystem')
        with self.assertRaisesRegex(ValueError,'hardlinked'):
            self.plan()


class IndexGuardTests(unittest.TestCase):
    def setUp(self):
        self.nav=FakeNavidrome.__new__(FakeNavidrome)
        self.nav.rows=[dict(id='1',path='one.flac'),dict(id='2',path='two.flac')]
        self.nav.admin=True;self.nav.scanning=False;self.nav.last_scan='fixed';self.nav.page_size=1
        self.nav.scan_type='quick'

    def test_pagination_continues_when_server_caps_page_size(self):
        index=guard.export_index(self.nav)
        self.assertEqual(len(index['songs']),2)
        self.assertIn('elapsed_seconds',index)

    def test_selective_index_retains_only_relevant_paths_but_counts_every_song(self):
        index=guard.export_index(self.nav,select_paths={'one.flac'})
        self.assertEqual(len(index['songs']),1)
        self.assertEqual(index['coverage_count'],2)

    def test_native_global_absence_includes_retained_missing_identities(self):
        self.nav.rows[0]['missing']=False;self.nav.rows[1]['missing']=True
        def page(offset,limit,missing):
            rows=[row for row in self.nav.rows if row['missing']==missing]
            return dict(rows=rows[offset:offset+limit],total=len(rows))
        self.nav.filesystem_page=page
        index=guard.export_index(self.nav,select_paths={'two.flac'})
        self.assertEqual(index['coverage_count'],2)
        self.assertEqual(index['counts'],dict(active=1,missing=1))
        self.assertEqual(index['songs'][0]['id'],'2')
        self.assertTrue(index['songs'][0]['missing'])

    def test_non_admin_or_active_scan_cannot_prove_global_absence(self):
        self.nav.admin=False
        with self.assertRaisesRegex(ValueError,'administrator'):
            guard.export_index(self.nav)
        self.nav.admin=True;self.nav.scanning=True
        with self.assertRaisesRegex(ValueError,'scan is active'):
            guard.export_index(self.nav)

    def test_duplicate_ids_or_incomplete_count_are_rejected(self):
        self.nav.rows[1]['id']='1'
        with self.assertRaisesRegex(ValueError,'duplicate'):
            guard.export_index(self.nav)
        self.nav.rows[1]['id']='2'
        original=self.nav.request
        def request(endpoint,params=None):
            if endpoint=='search3' and params['songOffset']>0:
                return dict(searchResult3=dict(song=[]))
            return original(endpoint,params)
        self.nav.request=request
        with self.assertRaisesRegex(ValueError,'incomplete'):
            guard.export_index(self.nav)


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.nav=FakeNavidrome.__new__(FakeNavidrome)
        self.nav.rows=[dict(id='kept-id',path='Artist/Album/Song.flac',title='Song',album='Album',artist='Artist',
                            userRating=5,playCount=12,starred='2020-01-01',played='2026-01-01')]
        self.nav.admin=True;self.nav.scanning=False;self.nav.last_scan='before';self.nav.scan_type='full'
        self.baseline=guard.capture(self.nav,['kept-id'])

    def test_stale_scan_cannot_be_claimed_as_preservation_acceptance(self):
        with self.assertRaisesRegex(ValueError,'completed full'):
            guard.verify(self.nav,self.baseline)

    def test_tag_changes_and_increased_listening_preserve_identity(self):
        self.nav.last_scan='after';self.nav.rows[0].update(title='Corrected title',playCount=13,played='2026-02-01')
        self.assertTrue(guard.verify(self.nav,self.baseline)['preserved'])

    def test_rating_or_history_loss_is_reported(self):
        self.nav.last_scan='after';self.nav.rows[0].update(userRating=0,playCount=0)
        result=guard.verify(self.nav,self.baseline)
        self.assertFalse(result['preserved'])
        self.assertTrue(any('rating' in x for x in result['errors']))
        self.assertTrue(any('history' in x for x in result['errors']))

    def test_last_played_timestamp_cannot_regress_with_unchanged_count(self):
        self.nav.last_scan='after';self.nav.rows[0]['played']='2025-01-01'
        self.assertFalse(guard.verify(self.nav,self.baseline)['preserved'])

    def test_only_explicit_reviewed_path_changes_are_allowed(self):
        self.nav.last_scan='after';self.nav.rows[0]['path']='Artist/Album/New Song.flac'
        self.assertFalse(guard.verify(self.nav,self.baseline)['preserved'])
        self.assertTrue(guard.verify(self.nav,self.baseline,{'Artist/Album/Song.flac':'Artist/Album/New Song.flac'})['preserved'])
