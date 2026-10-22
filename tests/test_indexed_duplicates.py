import copy
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from mutagen.flac import FLAC
from navidrome_scripts import indexed_duplicates as indexed, dates, duplicates
from navidrome_scripts.subsonic import Subsonic
import test_unmapped as fixtures


class Nav:
    base_url='http://nav-fixture';username='admin'
    _filesystem_row=staticmethod(Subsonic._filesystem_row)

    def request(self,endpoint,params=None):
        if endpoint=='getUser':return dict(user=dict(adminRole=True))
        if endpoint=='getScanStatus':return dict(scanStatus=dict(scanning=False,count=len(self.rows),lastScan=self.scan,scanType='full'))
        raise AssertionError(endpoint)

    def native_get(self,endpoint):
        return copy.deepcopy(self.rows[endpoint.split('/')[-1]])


class CLI(indexed.NativeCLI):
    def __init__(self,backups,source,nav):
        super().__init__(['navidrome-fixture'],backups)
        self.source=source;self.nav=nav;self.calls=[];self.unknown=False

    def run(self,args,log):
        self.calls.append(args)
        if args[:2]==['backup','create']:
            shutil.copy2(self.source,Path(args[-1])/'navidrome_backup_fixture.db')
        elif args[:2]==['missing','fix'] and args[-1]!='--help':
            drop,keep=args[-2:]
            with closing(sqlite3.connect(self.source)) as db, db:
                target=db.execute('SELECT path,library_id,size FROM media_file WHERE id=?',(keep,)).fetchone()
                db.execute('UPDATE media_file SET path=?,library_id=?,size=?,missing=0 WHERE id=?',(*target,drop))
                for table,column in [('annotation','item_id'),('bookmark','item_id'),('playlist_tracks','media_file_id'),('scrobbles','media_file_id'),('scrobble_buffer','media_file_id')]:
                    db.execute('UPDATE '+table+' SET '+column+'=? WHERE '+column+'=?',(drop,keep))
                db.execute('DELETE FROM media_file WHERE id=?',(keep,))
            self.nav.rows[drop]=dict(self.nav.rows[keep],id=drop)
            self.nav.rows.pop(keep)
            if self.unknown:raise ValueError('native command timed out after completion')


class IndexedTests(unittest.TestCase):
    def setUp(self):
        fixtures.UnmappedTests.setUp(self)
        self.keep=self.path
        audio=FLAC(self.keep);audio['musicbrainz_releasetrackid']='55555555-5555-4555-8555-555555555555';audio.save()
        self.drop=self.root/'Artist/Other/song.flac';self.drop.parent.mkdir();shutil.copy2(self.keep,self.drop)
        self.client.mapped[6]=dict(id=6,path='/music/Artist/Album/song.flac',albumId=2,size=self.keep.stat().st_size)
        self.client.files=[dict(id=5,path='/music/Artist/Other/song.flac',size=self.drop.stat().st_size)]
        self.nav=Nav();self.nav.scan='2020-01-01T00:00:00+00:00'
        self.nav.rows={identity:dict(id=identity,path=path.relative_to(self.root).as_posix(),libraryId=1,albumId='one-album',
            missing=False,size=path.stat().st_size,mbzRecordingID=fixtures.RECORDING,title='Song',artist='Artist',album='Album',duration=1)
            for identity,path in [('keeper',self.keep),('dropped',self.drop)]}
        self.database=Path(self.temp.name)/'offline-fixture.db'
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript('''
                CREATE TABLE media_file(id TEXT PRIMARY KEY,path TEXT,library_id INT,album_id TEXT,size INT,missing INT,created_at TEXT);
                CREATE TABLE user(id TEXT,user_name TEXT,is_admin INT DEFAULT 0);
                CREATE TABLE user_library(user_id TEXT,library_id INT);
                CREATE TABLE annotation(user_id TEXT,item_id TEXT,item_type TEXT,play_count INT,rating INT,starred INT);
                CREATE TABLE bookmark(user_id TEXT,item_id TEXT,item_type TEXT,position INT);
                CREATE TABLE playlist(id TEXT,name TEXT,owner_id TEXT,public INT);
                CREATE TABLE playlist_tracks(id INT,playlist_id TEXT,media_file_id TEXT);
                CREATE TABLE scrobbles(id INT,media_file_id TEXT,user_id TEXT,submission_time INT);
                CREATE TABLE scrobble_buffer(user_id TEXT,service TEXT,media_file_id TEXT,play_time INT,enqueue_time INT,id INT);
                INSERT INTO user(id,user_name) VALUES('a','private-owner-a'),('b','private-owner-b');
                INSERT INTO user_library VALUES('a',1),('b',1);
                INSERT INTO annotation VALUES('a','dropped','media_file',5,5,1),('b','keeper','media_file',3,2,1);
                INSERT INTO bookmark VALUES('a','dropped','media_file',125);
                INSERT INTO playlist VALUES('private-a','Private playlist A','a',0),('private-b','Private playlist B','b',0);
                INSERT INTO playlist_tracks VALUES(1,'private-a','keeper'),(2,'private-a','dropped'),(3,'private-a','keeper'),(4,'private-b','keeper');
                INSERT INTO scrobbles VALUES(1,'keeper','b',100),(2,'dropped','a',200);
            ''')
            for identity,row in self.nav.rows.items():
                db.execute('INSERT INTO media_file VALUES(?,?,?,?,?,?,?)',(identity,row['path'],1,'one-album',row['size'],0,'2020-01-01'))
        self.cli=CLI(Path(self.temp.name)/'backups',self.database,self.nav)
        self.quarantine=Path(self.temp.name)/'quarantine'

    def plan(self,**kwargs):
        return indexed.plan(self.client,self.nav,self.root,{'1':str(self.root)},'keeper','dropped',self.quarantine,self.cli,**kwargs)

    def scan(self,missing=False):
        self.nav.scan=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()
        if missing:
            self.nav.rows['dropped']['missing']=True
            with closing(sqlite3.connect(self.database)) as db, db:db.execute("UPDATE media_file SET missing=1 WHERE id='dropped'")

    def test_complete_remap_preserves_all_users_and_repeated_private_playlist_entries(self):
        plan=self.plan()
        self.assertTrue(self.keep.exists() and self.drop.exists())
        self.assertEqual(indexed.apply(self.client,self.nav,self.root,plan,self.journal,self.cli),2)
        self.assertFalse(self.drop.exists())
        self.scan(missing=True)
        self.assertEqual(indexed.apply(self.client,self.nav,self.root,plan,self.journal,self.cli),0)
        self.scan()
        self.assertEqual(indexed.verify(self.client,self.nav,self.root,plan,self.journal,self.cli),0)
        record=json.loads(self.journal.read_text());self.assertEqual(record['status'],'verified')
        final=indexed.history(record['verification_backup'],['keeper','dropped'])
        self.assertEqual([r['media_file_id'] for r in final['entries']],['dropped']*4)
        self.assertEqual([(r['user_id'],r['rating'],r['play_count']) for r in final['annotations']],[('a',5,5),('b',2,3)])
        self.assertTrue(Path(record['quarantine']).is_file())
        self.assertEqual(sum(call[:2]==['missing','fix'] and call[-1]!='--help' for call in self.cli.calls),1)

    def test_zero_valued_annotation_conflict_is_not_silently_discarded(self):
        with closing(sqlite3.connect(self.database)) as db, db:db.execute("INSERT INTO annotation VALUES('a','keeper','media_file',0,0,0)")
        with self.assertRaisesRegex(ValueError,'same user'):
            self.plan()
        self.assertTrue(self.drop.exists());self.assertFalse(self.quarantine.exists())

    def test_bookmark_and_buffer_conflicts_require_review(self):
        snapshot=indexed.history(self.cli.backup('test'),['keeper','dropped'])
        snapshot['bookmarks'].append(dict(snapshot['bookmarks'][0],item_id='keeper'))
        with self.assertRaisesRegex(ValueError,'bookmarks'):indexed.conflict_check(snapshot)
        snapshot['bookmarks'].pop()
        snapshot['buffered']=[dict(user_id='a',service='lastfm',play_time=10,media_file_id=identity) for identity in ('keeper','dropped')]
        with self.assertRaisesRegex(ValueError,'buffered'):indexed.conflict_check(snapshot)

    def test_cross_library_permissions_must_preserve_every_users_access(self):
        snapshot=indexed.history(self.cli.backup('test'),['keeper','dropped'])
        next(row for row in snapshot['media'] if row['id']=='dropped')['library_id']=2
        snapshot['access']=[dict(user_id='a',library_id=2),dict(user_id='b',library_id=1)]
        with self.assertRaisesRegex(ValueError,'remove a user'):indexed.access_check(snapshot,'keeper','dropped')
        snapshot['access'].append(dict(user_id='a',library_id=1))
        indexed.access_check(snapshot,'keeper','dropped')

    def test_changed_private_history_stops_before_quarantine(self):
        plan=self.plan()
        with closing(sqlite3.connect(self.database)) as db, db:db.execute("UPDATE annotation SET rating=4 WHERE user_id='a'")
        with self.assertRaisesRegex(ValueError,'history changed'):
            indexed.apply(self.client,self.nav,self.root,plan,self.journal,self.cli)
        self.assertTrue(self.drop.exists())

    def test_lost_native_response_reconciles_without_second_remap(self):
        plan=self.plan();indexed.apply(self.client,self.nav,self.root,plan,self.journal,self.cli);self.scan(missing=True)
        self.cli.unknown=True
        with self.assertRaisesRegex(ValueError,'timed out'):
            indexed.apply(self.client,self.nav,self.root,plan,self.journal,self.cli)
        self.assertEqual(indexed.apply(self.client,self.nav,self.root,plan,self.journal,self.cli),0)
        self.assertEqual(sum(call[:2]==['missing','fix'] and call[-1]!='--help' for call in self.cli.calls),1)

    def test_native_backup_corruption_is_refused(self):
        backup=self.cli.backup('test');Path(backup['path']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'backup changed'):indexed.history(backup,['keeper','dropped'])

    def test_native_command_timeout_is_reported_without_automatic_retry(self):
        cli=indexed.NativeCLI(['navidrome-fixture'],Path(self.temp.name)/'native-timeout')
        with patch.object(indexed.subprocess,'run',side_effect=subprocess.TimeoutExpired('native',1)) as run:
            with self.assertRaisesRegex(ValueError,'outcome unknown'):
                cli.run(['missing','fix','old','new'],cli.backups/'timeout.log')
            self.assertEqual(run.call_count,1)

    def test_files_and_local_library_mappings_are_explicit(self):
        with self.assertRaisesRegex(ValueError,'explicit local root'):
            indexed.plan(self.client,self.nav,self.root,{},'keeper','dropped',self.quarantine,self.cli)
        self.drop.write_bytes(b'another file')
        with self.assertRaisesRegex(ValueError,'size differs'):self.plan()

    def test_no_cleanup_inside_music_root_or_of_another_edition(self):
        self.quarantine=self.root/'quarantine'
        with self.assertRaisesRegex(ValueError,'outside every music root'):self.plan()
        self.quarantine=Path(self.temp.name)/'quarantine'
        self.nav.rows['dropped']['albumId']='different-edition'
        with self.assertRaisesRegex(ValueError,'album identities'):self.plan()

    def test_changed_file_after_preview_is_not_removed(self):
        plan=self.plan();self.drop.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'file changed'):
            indexed.apply(self.client,self.nav,self.root,plan,self.journal,self.cli)
        self.assertEqual(self.drop.read_bytes(),b'changed')

    def test_different_bytes_need_explicit_review_of_the_same_recording(self):
        audio=FLAC(self.drop);audio['comment']='Reviewed alternate copy';audio.save()
        size=self.drop.stat().st_size
        self.nav.rows['dropped']['size']=size;self.client.files[0]['size']=size
        with closing(sqlite3.connect(self.database)) as db, db:db.execute("UPDATE media_file SET size=? WHERE id='dropped'",(size,))
        with self.assertRaisesRegex(ValueError,'alternate-encode review'):self.plan()
        payload=self.plan(allow_alternate=True)
        self.assertTrue(payload['alternate'])
        self.assertNotEqual(payload['files']['keep']['sha256'],payload['files']['drop']['sha256'])

    def test_two_library_roots_are_mapped_independently(self):
        for name,path in [('LibraryOne',self.keep),('LibraryTwo',self.drop)]:
            target=self.root/name/path.relative_to(self.root)
            target.parent.mkdir(parents=True);path.rename(target)
        self.keep=self.root/'LibraryOne/Artist/Album/song.flac'
        self.drop=self.root/'LibraryTwo/Artist/Other/song.flac'
        self.client.artist['path']='/music/LibraryOne/Artist'
        self.client.mapped[6]['path']='/music/LibraryOne/Artist/Album/song.flac'
        self.client.files[0]['path']='/music/LibraryTwo/Artist/Other/song.flac'
        self.nav.rows['dropped']['libraryId']=2
        with closing(sqlite3.connect(self.database)) as db, db:db.execute("UPDATE media_file SET library_id=2 WHERE id='dropped'")
        payload=indexed.plan(self.client,self.nav,self.root,
            {'1':str(self.root/'LibraryOne'),'2':str(self.root/'LibraryTwo')},
            'keeper','dropped',self.quarantine,self.cli)
        self.assertEqual(indexed.paths(payload),dict(keep=self.keep,drop=self.drop))

    def test_new_scan_required_after_quarantine_and_after_remap(self):
        plan=self.plan();self.assertEqual(indexed.apply(self.client,self.nav,self.root,plan,self.journal,self.cli),2)
        self.assertEqual(indexed.apply(self.client,self.nav,self.root,plan,self.journal,self.cli),2)
        self.scan(missing=True);indexed.apply(self.client,self.nav,self.root,plan,self.journal,self.cli)
        self.nav.scan='2020-01-01T00:00:00+00:00'
        with self.assertRaisesRegex(ValueError,'new full scan'):
            indexed.verify(self.client,self.nav,self.root,plan,self.journal,self.cli)

    def test_interrupted_unlink_resumes_from_quarantine_intent(self):
        plan=self.plan();original=Path.unlink
        def interrupted(path,*args,**kwargs):
            result=original(path,*args,**kwargs)
            if path==self.drop:raise OSError('interrupted after unlink')
            return result
        with patch.object(Path,'unlink',interrupted):
            with self.assertRaises(OSError):indexed.apply(self.client,self.nav,self.root,plan,self.journal,self.cli)
        self.assertEqual(indexed.apply(self.client,self.nav,self.root,plan,self.journal,self.cli),2)
        self.assertFalse(self.drop.exists())

    def test_restore_audio_never_overwrites_new_file_and_freezes_cleanup(self):
        plan=self.plan();indexed.apply(self.client,self.nav,self.root,plan,self.journal,self.cli)
        self.drop.write_bytes(b'new file')
        with self.assertRaisesRegex(ValueError,'no overwrite'):
            indexed.restore_files(self.client,self.nav,self.root,plan,self.journal,self.cli)
        self.drop.unlink()
        indexed.restore_files(self.client,self.nav,self.root,plan,self.journal,self.cli)
        self.assertEqual(dates.digest(self.drop),plan['files']['drop']['sha256'])
        with self.assertRaisesRegex(ValueError,'recovery has started'):
            indexed.apply(self.client,self.nav,self.root,plan,self.journal,self.cli)


if __name__=='__main__':unittest.main()
