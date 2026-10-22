import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mutagen.flac import FLAC
from mutagen.id3 import ID3, TXXX, UFID, TRCK, TPOS, TIT2

from navidrome_scripts import unmapped as u
from navidrome_scripts import metadata
from navidrome_scripts.subsonic import ApiError
from test_dates import fixture

RELEASE = '11111111-1111-4111-8111-111111111111'
GROUP = '22222222-2222-4222-8222-222222222222'
ARTIST = '33333333-3333-4333-8333-333333333333'
RECORDING = '44444444-4444-4444-8444-444444444444'


class FakeLidarr:
    url = 'http://lidarr'

    def __init__(self):
        self.artist = dict(id=1, foreignArtistId=ARTIST, path='/music/Artist', qualityProfileId=1, metadataProfileId=1)
        self.album = dict(id=2, foreignAlbumId=GROUP, artistId=1, title='Album',
                          releases=[dict(id=3, foreignReleaseId=RELEASE, monitored=True)])
        self.catalog = dict(self.album, artist=self.artist)
        self.present = True
        self.track = dict(id=4, albumId=2, artistId=1, foreignRecordingId=RECORDING,
                          foreignTrackId='release-track', title='Song', duration=1000,
                          trackNumber='1', mediumNumber=1, hasFile=False, trackFileId=0)
        self.files = [dict(id=5, path='/music/Artist/Album/song.flac', quality={'quality': 1})]
        self.mapped = {}
        self.posts = []
        self.running = False
        self.unknown = False
        self.exclusions = []
        self.write_tags = 'no'

    def get(self, endpoint, **params):
        if endpoint == 'config/metadataprovider':
            return dict(writeAudioTags=self.write_tags)
        if endpoint == 'config/mediamanagement':
            return dict(fileDate='none')
        if endpoint in ('artist', 'artist/1'):
            return copy.deepcopy([self.artist] if endpoint == 'artist' else self.artist)
        if endpoint == 'album':
            return copy.deepcopy([self.album] if self.present else [])
        if endpoint == 'album/2':
            return copy.deepcopy(self.album)
        if endpoint == 'album/lookup':
            return copy.deepcopy([self.catalog])
        if endpoint == 'importlistexclusion':
            return self.exclusions
        if endpoint == 'trackfile':
            return copy.deepcopy(self.files if 'unmapped' in params else list(self.mapped.values()))
        if endpoint in ('track', 'track/4'):
            return copy.deepcopy([self.track] if endpoint == 'track' else self.track)
        if endpoint == 'trackfile/6':
            return copy.deepcopy(self.mapped.get(6, {}))
        if endpoint == 'command/7':
            return dict(id=7, status='started' if self.running else 'completed')
        raise AssertionError((endpoint, params))

    def post(self, endpoint, body):
        self.posts.append((endpoint, copy.deepcopy(body)))
        if endpoint == 'album':
            assert body['monitored'] is False
            assert body['addOptions']['searchForNewAlbum'] is False
            self.present = True
            if self.unknown:
                raise ApiError('write outcome unknown')
            return dict(self.album, monitored=False)
        assert endpoint == 'command'
        assert body['name'] == 'ManualImport'
        assert body['replaceExistingFiles'] is False
        assert body['importMode'] == 'Copy'
        if not self.running:
            self.track.update(hasFile=True, trackFileId=6)
            self.mapped[6] = dict(id=6, path=body['files'][0]['path'], albumId=2)
            self.files = []
        if self.unknown:
            raise ApiError('write outcome unknown')
        return dict(id=7)


class UnmappedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'music'
        self.path = self.root / 'Artist/Album/song.flac'
        fixture(self.path)
        audio = FLAC(self.path)
        audio['title'] = 'Song'; audio['album'] = 'Album'; audio['artist'] = 'Artist'
        audio['tracknumber'] = '1'; audio['discnumber'] = '1'
        audio['musicbrainz_albumid'] = RELEASE; audio['musicbrainz_releasegroupid'] = GROUP
        audio['musicbrainz_trackid'] = RECORDING; audio['musicbrainz_albumartistid'] = ARTIST
        audio.save()
        self.client = FakeLidarr()
        self.journal = Path(self.temp.name) / 'journal.json'

    def preview(self):
        return u.plan(self.client, self.root, '/music/Artist/Album')

    def test_read_only_preview_and_in_place_import_preserve_file_and_resume(self):
        before = self.path.read_bytes(); mtime = self.path.stat().st_mtime_ns
        plan = self.preview()
        self.assertEqual(plan['summary'], {'ready': 1})
        self.assertEqual(self.client.posts, [])
        self.assertEqual(u.apply(self.client, self.root, plan, self.journal), 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, mtime)
        handoff_path = self.journal.with_suffix('.metadata-queue.json')
        handoff = json.loads(handoff_path.read_text()); handoff['status'] = 'processed'
        handoff_path.write_text(json.dumps(handoff))
        self.assertEqual(u.apply(self.client, self.root, plan, self.journal), 0)
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(json.loads(handoff_path.read_text())['status'], 'processed')

    def test_multiple_copies_and_existing_track_files_require_review(self):
        other = self.path.with_name('copy.flac'); other.write_bytes(self.path.read_bytes())
        self.client.files.append(dict(self.client.files[0], id=8, path='/music/Artist/Album/copy.flac'))
        self.assertEqual(self.preview()['summary'], {'review': 2})
        self.client.files.pop(); self.client.track.update(hasFile=True, trackFileId=100)
        self.assertIn('already has a file', self.preview()['rows'][0]['reason'])

    def test_wrong_recording_and_wrong_artist_folder_rejected(self):
        self.client.track['foreignRecordingId'] = 'wrong'
        self.assertEqual(self.preview()['summary'], {'review': 1})
        self.client.track['foreignRecordingId'] = RECORDING
        self.client.artist['path'] = '/music/Other Artist'
        self.assertIn('outside', self.preview()['rows'][0]['reason'])

    def test_missing_catalog_is_separate_unmonitored_add_then_fresh_plan(self):
        self.client.present = False
        plan = self.preview()
        self.assertEqual(plan['summary'], {'catalog_missing': 1})
        self.assertIn('catalog_proposal', plan['rows'][0])
        u.add_catalog(self.client, self.root, plan, self.journal)
        self.assertEqual(self.client.posts[0][0], 'album')
        self.assertEqual(len(self.client.posts), 1)
        self.assertFalse(self.client.posts[0][1]['monitored'])
        self.assertEqual(self.preview()['summary'], {'ready': 1})

    def test_unknown_catalog_write_reconciles_without_reposting(self):
        self.client.present = False; self.client.unknown = True
        plan = self.preview()
        with self.assertRaises(ApiError):
            u.add_catalog(self.client, self.root, plan, self.journal)
        u.add_catalog(self.client, self.root, plan, self.journal)
        self.assertEqual(len(self.client.posts), 1)

    def test_excluded_catalog_entry_not_added(self):
        self.client.present = False
        plan = self.preview(); self.client.exclusions = [dict(foreignId=GROUP)]
        with self.assertRaisesRegex(ValueError, 'excluded'):
            u.add_catalog(self.client, self.root, plan, self.journal)
        self.assertEqual(self.client.posts, [])

    def test_same_name_different_catalog_artist_cannot_create_proposal(self):
        self.client.present = False
        self.client.catalog = dict(self.client.catalog, artist=dict(self.client.artist, foreignArtistId='different-artist'))
        row = self.preview()['rows'][0]
        self.assertEqual(row['status'], 'review')
        self.assertNotIn('catalog_proposal', row)
        self.assertIn('artist ID', row['reason'])

    def test_new_files_tag_mode_does_not_write_existing_imports(self):
        self.client.write_tags = 'newFiles'
        self.assertEqual(self.preview()['summary'], {'ready': 1})

    def test_queued_command_is_polled_without_reissuing(self):
        plan = self.preview(); self.client.running = True
        self.assertEqual(u.apply(self.client, self.root, plan, self.journal, wait_seconds=0), 2)
        self.assertEqual(u.apply(self.client, self.root, plan, self.journal, wait_seconds=0), 2)
        self.assertEqual(len(self.client.posts), 1)

    def test_unknown_import_write_is_verified_not_retried(self):
        plan = self.preview(); self.client.unknown = True
        with self.assertRaises(ApiError):
            u.apply(self.client, self.root, plan, self.journal)
        self.assertEqual(u.apply(self.client, self.root, plan, self.journal), 0)
        self.assertEqual(len(self.client.posts), 1)

    def test_changed_tags_or_unsafe_lidarr_settings_block_writes(self):
        plan = self.preview()
        audio = FLAC(self.path); audio['title'] = 'Different'; audio.save()
        with self.assertRaisesRegex(ValueError, 'changed since preview'):
            u.apply(self.client, self.root, plan, self.journal)
        self.assertEqual(self.client.posts, [])
        self.client.write_tags = 'allFiles'
        with self.assertRaisesRegex(ValueError, 'disabled'):
            self.preview()

    def test_edition_switch_with_existing_files_requires_review(self):
        self.client.album['releases'][0]['monitored'] = False
        self.client.mapped[9] = dict(id=9, path='/music/Artist/Album/other.flac')
        self.assertIn('edition already', self.preview()['rows'][0]['reason'])

    def test_two_editions_in_one_album_plan_are_not_combined(self):
        second = self.path.with_name('second.flac'); second.write_bytes(self.path.read_bytes())
        release = '55555555-5555-4555-8555-555555555555'
        audio = FLAC(second); audio['musicbrainz_albumid'] = release
        audio['title'] = 'Second song'; audio['tracknumber'] = '2'; audio.save()
        self.client.album['releases'].append(dict(id=9, foreignReleaseId=release, monitored=False))
        self.client.files.append(dict(self.client.files[0], id=10, path='/music/Artist/Album/second.flac'))
        original = self.client.get
        def get(endpoint, **params):
            if endpoint == 'track' and params.get('albumReleaseId') == 9:
                return [dict(self.client.track, id=11, title='Second song', trackNumber='2')]
            return original(endpoint, **params)
        self.client.get = get
        rows = self.preview()['rows']
        self.assertEqual([r['status'] for r in rows], ['review', 'review'])
        self.assertTrue(all('multiple editions' in r['reason'] for r in rows))

    def test_remote_path_escape_rejected(self):
        with self.assertRaises(ValueError):
            u.local_path(self.root, '/music/../outside.flac', '/music')


class MetadataTests(unittest.TestCase):
    def test_id3_recording_ufid_and_release_track_are_not_confused(self):
        tags = ID3()
        tags.add(UFID(owner='http://musicbrainz.org', data=RECORDING.encode()))
        tags.add(TXXX(encoding=3, desc='MusicBrainz Album Id', text=[RELEASE]))
        tags.add(TXXX(encoding=3, desc='MusicBrainz Release Track Id', text=['release-track']))
        tags.add(TRCK(encoding=3, text=['2/10'])); tags.add(TPOS(encoding=3, text=['1/2']))
        tags.add(TIT2(encoding=3, text=['Song']))
        with patch.object(metadata, 'File', return_value=SimpleNamespace(tags=tags, info=SimpleNamespace(length=180))):
            row = metadata.identify(Path('unused'))
        self.assertEqual(row['recording_mbid'], RECORDING)
        self.assertEqual(row['release_track_mbid'], 'release-track')
        self.assertEqual(row['release_mbid'], RELEASE)
        self.assertEqual(row['tracknumber'], 2)

    def test_mp4_freeform_bytes_and_positions(self):
        tags = {'----:com.apple.iTunes:MusicBrainz Album Id': [RELEASE.encode()], 'trkn': [(2, 10)], 'disk': [(1, 2)]}
        with patch.object(metadata, 'File', return_value=SimpleNamespace(tags=tags, info=SimpleNamespace(length=180))):
            row = metadata.identify(Path('unused'))
        self.assertEqual(row['release_mbid'], RELEASE)
        self.assertEqual(row['tracknumber'], 2)
