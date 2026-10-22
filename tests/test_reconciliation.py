import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock
from unittest.mock import patch

from navidrome_scripts import reconciliation as r
from navidrome_scripts.subsonic import ApiError
from test_date_evidence import catalog
import test_unmapped as fixtures


class RefreshLidarr(fixtures.FakeLidarr):
    def __init__(self):
        super().__init__()
        self.album['releaseDate']='0001-01-01T00:00:00Z'
        self.catalog=dict(copy.deepcopy(self.album),artist=copy.deepcopy(self.artist),releaseDate='2003-06-12T00:00:00Z')
        self.commands=[]
        self.running=False;self.lost=False
        self.mapped[6]=dict(id=6,path='/music/Artist/Album/song.flac',albumId=2)

    def get(self,endpoint,**params):
        if endpoint=='command':
            return copy.deepcopy(self.commands)
        if endpoint=='command/11':
            return dict(id=11,status='started' if self.running else 'completed')
        return super().get(endpoint,**params)

    def post(self,endpoint,body):
        assert endpoint=='command'
        assert body==dict(name='RefreshAlbum',albumId=2,isNewAlbum=False)
        self.posts.append((endpoint,copy.deepcopy(body)))
        self.commands.append(dict(id=11,name='RefreshAlbum',body=copy.deepcopy(body),status='started' if self.running else 'completed'))
        if not self.running:
            self.album['releaseDate']=self.catalog['releaseDate']
        if self.lost:
            raise ApiError('unknown refresh outcome')
        return dict(id=11)


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        fixtures.UnmappedTests.setUp(self)
        self.client=RefreshLidarr();self.catalog=Mock();self.catalog.lookup_release.return_value=catalog(date='2003-06-12',original='2003-06-12')
        self.art=self.path.parent/'cover.jpg';self.art.write_bytes(b'keep artwork')

    def plan(self):
        return r.plan(self.client,self.root,2,catalog=self.catalog)

    def apply(self,payload):
        return r.reconcile(self.client,self.root,payload,self.journal,self.catalog)

    def state(self):
        return json.loads(self.journal.read_text())

    def test_plan_is_read_only_and_refresh_is_scoped_with_backups(self):
        before=self.path.read_bytes();mtime=self.path.stat().st_mtime_ns
        payload=self.plan()
        self.assertEqual(payload['status'],'ready');self.assertEqual(self.client.posts,[])
        self.assertEqual(self.apply(payload),0)
        self.assertEqual(len(self.client.posts),1)
        self.assertEqual(self.path.read_bytes(),before);self.assertEqual(self.path.stat().st_mtime_ns,mtime)
        self.assertEqual(self.state()['status'],'reconciled');self.assertEqual(len(self.state()['originals']),2)
        self.assertEqual(Path(self.state()['originals'][0]['backup']).read_bytes(),before)
        self.assertEqual(self.apply(payload),0);self.assertEqual(len(self.client.posts),1)

    def test_queued_refresh_is_polled_without_resubmission(self):
        payload=self.plan();self.client.running=True
        self.apply(payload);self.apply(payload)
        self.assertEqual(len(self.client.posts),1)
        self.client.running=False;self.client.album['releaseDate']=self.client.catalog['releaseDate']
        self.assertEqual(self.apply(payload),0)

    def test_unknown_post_is_reconciled_by_new_exact_command(self):
        payload=self.plan();self.client.lost=True
        with self.assertRaises(ApiError):
            self.apply(payload)
        self.assertEqual(self.state()['status'],'outcome_unknown')
        self.assertEqual(self.apply(payload),0);self.assertEqual(len(self.client.posts),1)

    def test_ambiguous_unknown_commands_are_never_reposted(self):
        payload=self.plan();self.client.lost=True
        with self.assertRaises(ApiError):
            self.apply(payload)
        self.client.commands.append(dict(self.client.commands[0],id=12))
        with self.assertRaisesRegex(ValueError,'uniquely identified'):
            self.apply(payload)
        self.assertEqual(len(self.client.posts),1)

    def test_homonym_artist_reassignment_is_blocked_before_refresh(self):
        self.client.catalog['artist']['foreignArtistId']='other-person'
        payload=self.plan()
        self.assertEqual(payload['status'],'review');self.assertIn('another artist',payload['notes'][0])
        with self.assertRaisesRegex(ValueError,'needs review'):
            self.apply(payload)
        self.assertEqual(self.client.posts,[])

    def test_release_group_artist_conflict_is_blocked(self):
        self.catalog.lookup_release.return_value['release-group']['artist-credit']=[dict(artist=dict(id='other-person'))]
        payload=self.plan();self.assertEqual(payload['status'],'review')
        self.assertIn('artist conflicts',payload['notes'][0]);self.assertEqual(self.client.posts,[])

    def test_unknown_or_stale_upstream_date_is_reported_not_invented(self):
        self.catalog.lookup_release.return_value['release-group']['first-release-date']=''
        payload=self.plan();self.assertEqual(self.apply(payload),1)
        self.assertEqual(self.state()['status'],'needs_source_review')
        self.assertEqual(self.state()['source_date'],'')

    def test_changed_plan_files_or_proposal_stops_before_command(self):
        payload=self.plan();self.client.catalog['releaseDate']='2004-01-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError,'stale'):
            self.apply(payload)
        self.assertEqual(self.client.posts,[])

    def test_running_existing_refresh_cannot_be_duplicated(self):
        payload=self.plan()
        self.client.commands.append(dict(id=10,name='RefreshAlbum',body=dict(albumId=2,isNewAlbum=False),status='started'))
        with self.assertRaisesRegex(ValueError,'already queued'):
            self.apply(payload)
        self.assertEqual(self.client.posts,[])

    def test_changed_audio_or_artwork_after_queue_requires_review(self):
        payload=self.plan();self.client.running=True;self.apply(payload)
        self.client.running=False;self.art.write_bytes(b'changed externally')
        with self.assertRaisesRegex(ValueError,'sidecars changed'):
            self.apply(payload)
        self.assertEqual(self.state()['status'],'needs_review')
        self.assertEqual(len(self.client.posts),1)

    def test_unsafe_lidarr_tag_writer_is_a_review_result(self):
        self.client.write_tags='newFiles'
        payload=self.plan();self.assertEqual(payload['status'],'review')
        self.assertIn('Never',payload['notes'][0]);self.assertEqual(self.client.posts,[])

    def test_artist_changed_during_backup_cannot_receive_a_refresh(self):
        payload=self.plan();original=r.shutil.copy2
        def changed(source,target):
            result=original(source,target)
            self.client.catalog['artist']['foreignArtistId']='different-person'
            return result
        with patch.object(r.shutil,'copy2',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'changed during backup'):
                self.apply(payload)
        self.assertEqual(self.client.posts,[])


if __name__=='__main__':
    unittest.main()
