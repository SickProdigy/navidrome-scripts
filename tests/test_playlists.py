import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock, patch

from navidrome_scripts import playlists as p
from navidrome_scripts.spotify import Spotify, playlist_id
from navidrome_scripts.subsonic import ApiError, Subsonic

SOURCE_ID = '0123456789ABCDEFGHIJKL'


def track(name='Song', album='Album', **kwargs):
    return dict(type='track', id='spotify-track', name=name, artists=[dict(name='Artist')],
                album=dict(name=album), duration_ms=180000, external_ids=dict(isrc='USABC1234567'), **kwargs)


def song(identity='song1', **kwargs):
    return dict(id=identity, title='Song', artist='Artist', album='Album', duration=180,
                path='Artist/Album/Song.flac', **kwargs)


class FakeServer:
    base_url, username = 'http://navidrome', 'user'

    def __init__(self):
        self.songs = {'song1': song()}
        self.data = {}
        self.writes = 0
        self.timeout = False

    def search(self, title, artist):
        return list(self.songs.values())

    def song(self, identity):
        return copy.deepcopy(self.songs[identity])

    def filesystem_song(self,identity):
        return dict(self.song(identity),libraryId=1)

    def playlist(self, identity):
        return copy.deepcopy(self.data[identity])

    def playlists(self):
        return [copy.deepcopy(x) for x in self.data.values()]

    def replace_playlist(self, ids, *, playlist_id=None, name=None):
        self.writes += 1
        identity = playlist_id or 'new-playlist'
        prior = self.data.get(identity, dict(id=identity, name=name, owner=self.username, public=False, comment=''))
        self.data[identity] = dict(prior, entry=[dict(id=i) for i in ids], songCount=len(ids))
        if self.timeout:
            raise ApiError('unknown write outcome')
        return copy.deepcopy(self.data[identity])


class PlaylistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'state.json'
        self.client = FakeServer()
        self.source = dict(id=SOURCE_ID, name='A playlist', snapshot_id='version1',
                           items=[dict(item=track()), dict(track=track())])

    def test_unique_match_and_duplicate_source_order_preserved(self):
        plan = p.preview(self.client, self.source, self.state)
        self.assertEqual(plan['summary'], {'matched': 2})
        identity = p.apply(self.client, plan, self.state)
        self.assertEqual(self.client.data[identity]['entry'], [dict(id='song1'), dict(id='song1')])
        self.assertEqual(p.apply(self.client, plan, self.state), identity)
        self.assertEqual(self.client.writes, 1)

    def test_new_source_snapshot_updates_same_playlist(self):
        plan = p.preview(self.client, self.source, self.state)
        identity = p.apply(self.client, plan, self.state)
        self.source['snapshot_id'] = 'version2'; self.source['items'].pop()
        second = p.preview(self.client, self.source, self.state)
        self.assertEqual(second['baseline']['id'], identity)
        self.assertEqual(p.apply(self.client, second, self.state), identity)
        self.assertEqual(len(self.client.data), 1)
        self.assertEqual(self.client.data[identity]['songCount'], 1)

    def test_ambiguous_copies_are_not_picked_by_bitrate(self):
        self.client.songs['other'] = song('other', bitRate=999)
        plan = p.preview(self.client, self.source, self.state)
        self.assertEqual(plan['summary'], {'ambiguous': 2})
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            p.apply(self.client, plan, self.state)
        self.assertEqual(self.client.writes, 0)

    def test_versions_duration_and_collaborators_remain_distinct(self):
        base = p.source_track(dict(item=track()))
        bad = [song('a', artists=[dict(name='Other')]),
               dict(song('b'), title='Song (Live)'), dict(song('c'), duration=30),
               dict(song('d'), album='Other release')]
        for candidate in bad:
            self.assertEqual(p.match(base, [candidate])['status'], 'missing')
        self.assertEqual(p.match(base, [dict(song(), album='Other release', isrc=['USABC1234567'])])['status'], 'matched')

    def test_unavailable_items_and_partial_preview(self):
        self.source['items'].append(dict(item=None))
        plan = p.preview(self.client, self.source, self.state)
        self.assertEqual(plan['summary']['unsupported'], 1)
        with self.assertRaises(ValueError):
            p.apply(self.client, plan, self.state)
        p.apply(self.client, plan, self.state, allow_partial=True)

    def test_network_errors_cannot_be_treated_as_missing_even_with_partial(self):
        self.client.search = Mock(side_effect=ApiError('search failed'))
        plan = p.preview(self.client, self.source, self.state)
        with self.assertRaisesRegex(ValueError, 'lookup errors'):
            p.apply(self.client, plan, self.state, allow_partial=True)

    def test_creation_timeout_recovers_without_duplicate_write(self):
        plan = p.preview(self.client, self.source, self.state)
        self.client.timeout = True
        with self.assertRaises(ApiError):
            p.apply(self.client, plan, self.state)
        self.client.timeout = False
        identity = p.apply(self.client, plan, self.state)
        self.assertEqual(identity, 'new-playlist')
        self.assertEqual(self.client.writes, 1)

    def test_unknown_creation_without_visible_playlist_never_retries_blindly(self):
        plan = p.preview(self.client, self.source, self.state)
        self.client.replace_playlist = Mock(side_effect=ApiError('timeout'))
        with self.assertRaises(ApiError):
            p.apply(self.client, plan, self.state)
        with self.assertRaisesRegex(ValueError, 'outcome unknown'):
            p.apply(self.client, plan, self.state)
        self.assertEqual(self.client.replace_playlist.call_count, 1)

    def test_external_edits_and_song_changes_reject_stale_plan(self):
        first = p.preview(self.client, self.source, self.state)
        identity = p.apply(self.client, first, self.state)
        plan = p.preview(self.client, self.source, self.state)
        self.client.data[identity]['comment'] = 'user edited'
        with self.assertRaisesRegex(ValueError, 'playlist changed'):
            p.apply(self.client, plan, self.state)
        self.client.songs['song1']['duration'] = 30
        with self.assertRaisesRegex(ValueError, 'song changed'):
            p.apply(self.client, plan, self.state)

    def test_account_binding_owner_and_empty_protection(self):
        plan = p.preview(self.client, self.source, self.state)
        self.client.username = 'other'
        with self.assertRaisesRegex(ValueError, 'different server'):
            p.apply(self.client, plan, self.state)
        self.client.username = 'user'
        self.source['items'] = []
        empty = p.preview(self.client, self.source, self.state)
        with self.assertRaisesRegex(ValueError, 'empty playlist'):
            p.apply(self.client, empty, self.state)

    def test_incomplete_export_refused(self):
        file = Path(self.temp.name) / 'export.json'
        file.write_text(json.dumps(dict(self.source, tracks=dict(items=[], total=20, next='next'), items=None)))
        with self.assertRaises(ValueError):
            p.load_source(file)

    def test_restore_preserves_existing_playlist_id_and_metadata(self):
        original = dict(id='old', name='My playlist', owner='user', comment='keep me', public=True,
                        entry=[], songCount=0)
        self.client.data['old'] = copy.deepcopy(original)
        plan = p.preview(self.client, self.source, self.state, target_id='old')
        p.apply(self.client, plan, self.state)
        p.restore(self.client, plan, self.state)
        self.assertEqual(self.client.data['old'], original)
        self.assertEqual(p.restore(self.client, plan, self.state), 'old')

    def test_restore_refuses_later_user_edits(self):
        self.client.data['old'] = dict(id='old', name='My playlist', owner='user', comment='',
                                        public=False, entry=[], songCount=0)
        plan = p.preview(self.client, self.source, self.state, target_id='old')
        p.apply(self.client, plan, self.state)
        self.client.data['old']['name'] = 'User changed it'
        with self.assertRaisesRegex(ValueError, 'changed after'):
            p.restore(self.client, plan, self.state)

    def test_local_m3u_contains_only_matched_safe_library_paths(self):
        plan = p.preview(self.client, self.source, self.state)
        output = Path(self.temp.name) / 'playlist.m3u8'
        p.export_m3u(plan, output, self.client)
        self.assertEqual(output.read_text().count('Artist/Album/Song.flac'), 2)
        plan['rows'][0]['song']['path'] = '../escape'
        with self.assertRaises(ValueError):
            p.export_m3u(plan, Path(self.temp.name) / 'bad.m3u8', self.client)


class TransportTests(unittest.TestCase):
    def test_real_http_repeated_song_parameters_and_unknown_result_recovery(self):
        backend = FakeServer()
        backend.timeout = True

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond(False)

            def do_POST(self):
                self.respond(True)

            def respond(self, write):
                params = parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode() if write else urlsplit(self.path).query)
                endpoint = urlsplit(self.path).path.split('/')[-1].removesuffix('.view')
                if 'p' in params or 't' not in params:
                    self.send_error(403); return
                result = dict(status='ok')
                if endpoint == 'getPlaylists':
                    result['playlists'] = dict(playlist=backend.playlists())
                elif endpoint == 'search3':
                    result['searchResult3'] = dict(song=list(backend.songs.values()))
                elif endpoint == 'getSong':
                    result['song'] = backend.song(params['id'][0])
                elif endpoint == 'getPlaylist':
                    result['playlist'] = backend.playlist(params['id'][0])
                elif endpoint == 'getOpenSubsonicExtensions':
                    result['openSubsonicExtensions'] = [dict(name='formPost', versions=[1])]
                elif endpoint == 'createPlaylist' and write:
                    try:
                        result['playlist'] = backend.replace_playlist(params.get('songId', []),
                            playlist_id=params.get('playlistId', [None])[0], name=params.get('name', [None])[0])
                    except ApiError:
                        self.close_connection = True
                        return  # Mutation happened, but the client never receives confirmation.
                else:
                    self.send_error(404); return
                data = json.dumps({'subsonic-response': result}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                client = Subsonic('http://127.0.0.1:' + str(server.server_port), 'user', 'password')
                state = Path(directory) / 'state.json'
                source = dict(id=SOURCE_ID, name='Test', snapshot_id='1', items=[dict(item=track())] * 2)
                plan = p.preview(client, source, state)
                with self.assertRaises(ApiError):
                    p.apply(client, plan, state)
                self.assertEqual(backend.writes, 1)
                self.assertEqual(p.apply(client, plan, state), 'new-playlist')
                self.assertEqual(backend.writes, 1)
                self.assertEqual([x['id'] for x in backend.data['new-playlist']['entry']], ['song1', 'song1'])
        finally:
            server.shutdown(); server.server_close(); worker.join()

    def test_subsonic_post_has_no_automatic_mutation_retry_and_no_password(self):
        session = Mock(); session.headers = {}
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {'subsonic-response': {'status': 'ok'}}
        client = Subsonic('http://server', 'user', 'secret-password', session)
        client.request('createPlaylist', {'songId': ['1', '1']}, write=True)
        data = session.post.call_args.kwargs['data']
        self.assertNotIn('p', data)
        self.assertEqual(data['songId'], ['1', '1'])
        self.assertNotIn('secret-password', str(data))
        self.assertEqual(session.post.call_count, 1)

    def test_playlist_id_validation(self):
        self.assertEqual(playlist_id('https://open.spotify.com/playlist/' + SOURCE_ID + '?si=x'), SOURCE_ID)
        with self.assertRaises(ValueError):
            playlist_id('https://evil.invalid/playlist/' + SOURCE_ID)

    @patch.dict(os.environ, {'SPOTIFY_ACCESS_TOKEN': 'test-token'}, clear=True)
    def test_current_api_pagination_and_snapshot_verification(self):
        spotify = Spotify(); spotify.get = Mock(side_effect=[
            dict(id=SOURCE_ID, name='Name', snapshot_id='v1'),
            dict(items=[dict(item=track())] * 50, total=51, next='page2', offset=0),
            dict(items=[dict(item=track())], total=51, next=None, offset=50),
            dict(id=SOURCE_ID, name='Name', snapshot_id='v1')])
        result = spotify.playlist(SOURCE_ID)
        self.assertEqual(len(result['items']), 51)
        self.assertEqual(spotify.get.call_args_list[1].args[0], 'playlists/' + SOURCE_ID + '/items')
        self.assertEqual(spotify.get.call_args_list[2].kwargs['offset'], 50)

    @patch.dict(os.environ, {'SPOTIFY_ACCESS_TOKEN': 'test-token'}, clear=True)
    def test_source_changed_during_fetch_rejected(self):
        spotify = Spotify(); spotify.get = Mock(side_effect=[
            dict(id=SOURCE_ID, name='Name', snapshot_id='v1'), dict(items=[], total=0, offset=0),
            dict(id=SOURCE_ID, name='Name', snapshot_id='v2')])
        with self.assertRaisesRegex(ApiError, 'changed while reading'):
            spotify.playlist(SOURCE_ID)

    @patch.dict(os.environ, {'SPOTIFY_CLIENT_ID': 'client', 'SPOTIFY_REFRESH_TOKEN': 'refresh-old'}, clear=True)
    def test_rotated_refresh_token_is_saved_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / 'token.json'
            session = Mock(); session.post.return_value.status_code = 200
            session.post.return_value.json.return_value = dict(access_token='access', refresh_token='refresh-new')
            spotify = Spotify(session, token_file=token_file)
            spotify.refresh()
            self.assertEqual(json.loads(token_file.read_text())['refresh_token'], 'refresh-new')
            second = Spotify(session, token_file=token_file)
            second.refresh()
            self.assertEqual(session.post.call_args.kwargs['data']['refresh_token'], 'refresh-new')

    @patch.dict(os.environ, {'SPOTIFY_ACCESS_TOKEN': 'test-token'}, clear=True)
    def test_wrong_page_offset_rejected(self):
        spotify = Spotify(); spotify.get = Mock(side_effect=[
            dict(id=SOURCE_ID, name='Name', snapshot_id='v1'), dict(items=[], total=0, offset=50)])
        with self.assertRaisesRegex(ApiError, 'incomplete'):
            spotify.playlist(SOURCE_ID)
