from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import requests

from navidrome_scripts.subsonic import Subsonic,ApiError
from navidrome_scripts import playlists
import test_playlists as fixtures


class FilesystemApiTests(unittest.TestCase):
    def setUp(self):
        self.session=Mock()
        self.session.headers={}
        self.client=Subsonic('http://navidrome','user','private-fixture-password',session=self.session)
        response=Mock(status_code=200)
        response.json.return_value=dict(token='fixture-jwt')
        self.session.post.return_value=response

    def row(self):
        return dict(id='song1',path='Artist/Album/real.flac',missing=False,libraryId=1,size=100)

    def response(self,data,status=200):
        result=Mock(status_code=status);result.json.return_value=data
        return result

    def test_real_paths_use_authenticated_native_api_and_reuse_token(self):
        self.session.get.return_value=self.response(self.row())
        self.assertEqual(self.client.filesystem_song('song1')['path'],'Artist/Album/real.flac')
        self.client.filesystem_song('song1')
        self.assertEqual(self.session.post.call_count,1)
        self.assertEqual(self.session.post.call_args.args[0],'http://navidrome/auth/login')
        self.assertEqual(self.session.get.call_args.kwargs['headers'],{'X-ND-Authorization':'Bearer fixture-jwt'})

    def test_expired_native_token_refreshes_once_for_a_get(self):
        self.session.get.side_effect=[self.response({},401),self.response(self.row())]
        self.client.filesystem_song('song1')
        self.assertEqual(self.session.post.call_count,2)
        self.assertEqual(self.session.get.call_count,2)

    def test_missing_unsafe_or_mismatched_native_paths_are_rejected(self):
        for changes in (dict(missing=True),dict(path='../outside.flac'),dict(path='/music/absolute.flac'),dict(path='C:\\music\\absolute.flac'),dict(id='wrong-song')):
            self.session.get.return_value=self.response(dict(self.row(),**changes))
            with self.assertRaises(ApiError):
                self.client.filesystem_song('song1')

    def test_native_index_uses_explicit_sorted_paging_and_active_files(self):
        self.session.get.return_value=self.response([self.row()])
        self.assertEqual(len(self.client.filesystem_songs(500,500)),1)
        self.assertEqual(self.session.get.call_args.kwargs['params'],dict(_start=500,_end=1000,_sort='id',_order='ASC',missing='false'))

    def test_login_failure_does_not_expose_credentials_or_token(self):
        self.session.post.side_effect=requests.ConnectionError('contains private-fixture-password')
        with self.assertRaises(ApiError) as error:
            self.client.filesystem_song('song1')
        self.assertNotIn('private-fixture-password',str(error.exception))

    def test_missing_index_rows_are_counted_without_allowing_missing_playback_rows(self):
        response=self.response([dict(self.row(),missing=True)])
        response.headers={'X-Total-Count':'1'};self.session.get.return_value=response
        page=self.client.filesystem_page(0,500,True)
        self.assertEqual(page['total'],1);self.assertTrue(page['rows'][0]['missing'])
        self.assertEqual(self.session.get.call_args.kwargs['params']['missing'],'true')

    def test_absence_checks_reject_missing_total_headers_or_wrong_filters(self):
        response=self.response([self.row()]);response.headers={};self.session.get.return_value=response
        with self.assertRaisesRegex(ApiError,'total cannot be verified'):
            self.client.filesystem_page(0,500,False)
        response.headers={'X-Total-Count':'1'}
        with self.assertRaisesRegex(ApiError,'missing-file filter'):
            self.client.filesystem_page(0,500,True)


class PhysicalPlaylistTests(unittest.TestCase):
    def test_m3u_uses_native_filename_instead_of_the_virtual_subsonic_name(self):
        with tempfile.TemporaryDirectory() as directory:
            client=fixtures.FakeServer()
            client.filesystem_song=Mock(return_value=dict(id='song1',path='Artist/Album/actual-file.flac',libraryId=1))
            source=dict(id=fixtures.SOURCE_ID,name='List',snapshot_id='fixed',items=[dict(item=fixtures.track())])
            payload=playlists.preview(client,source,Path(directory)/'state.json')
            output=Path(directory)/'playlist.m3u8'
            playlists.export_m3u(payload,output,client)
            self.assertEqual(output.read_text().splitlines()[-1],'Artist/Album/actual-file.flac')

    def test_mixed_library_roots_cannot_create_an_ambiguous_m3u(self):
        with tempfile.TemporaryDirectory() as directory:
            client=fixtures.FakeServer()
            client.filesystem_song=Mock(side_effect=[dict(path='one.flac',libraryId=1),dict(path='two.flac',libraryId=2)])
            source=dict(id=fixtures.SOURCE_ID,name='List',snapshot_id='fixed',items=[dict(item=fixtures.track()),dict(item=fixtures.track())])
            payload=playlists.preview(client,source,Path(directory)/'state.json')
            with self.assertRaisesRegex(ValueError,'multiple library roots'):
                playlists.export_m3u(payload,Path(directory)/'playlist.m3u8',client)


if __name__=='__main__':
    unittest.main()
