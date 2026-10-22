"""Strict Subsonic client for reviewed playlist operations.

Writes are never automatically retried: a timeout may follow a successful write.
"""
import hashlib
import os
import secrets
from urllib.parse import urlsplit,quote
from pathlib import PurePosixPath
import re

import requests

from .catalog import CLIENT


class ApiError(RuntimeError):
    pass


class Subsonic:
    def __init__(self, base_url, username, password, session=None):
        parsed = urlsplit(base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('provide a plain Navidrome http(s) URL without credentials or query parameters')
        if not username or not password:
            raise ValueError('Navidrome username and password are required')
        self.base_url = base_url.rstrip('/')
        self.username, self.password = username, password
        self.session = session or requests.Session()
        self.session.headers['User-Agent'] = CLIENT
        self._native_token = None

    def _native_login(self):
        try:
            response=self.session.post(self.base_url+'/auth/login',json=dict(username=self.username,password=self.password),timeout=(10,30))
            response.raise_for_status()
            token=response.json().get('token')
            if not isinstance(token,str) or not token:
                raise ValueError('missing token')
        except (requests.RequestException,ValueError,TypeError):
            raise ApiError('Navidrome native login failed; filesystem checks require native API access') from None
        self._native_token=token

    def native_get(self,endpoint,params=None,with_total=False):
        if self._native_token is None:
            self._native_login()
        for attempt in range(2):
            try:
                response=self.session.get(self.base_url+'/api/'+endpoint,params=params,
                    headers={'X-ND-Authorization':'Bearer '+self._native_token},timeout=(10,90))
                if response.status_code==401 and attempt==0:
                    self._native_login();continue
                response.raise_for_status()
                if with_total:
                    total=response.headers.get('X-Total-Count')
                    if not isinstance(total,str) or not total.isdigit():
                        raise ApiError('Navidrome native index total cannot be verified')
                    return response.json(),int(total)
                return response.json()
            except (requests.RequestException,ValueError):
                raise ApiError('Navidrome native filesystem lookup failed') from None
        raise ApiError('Navidrome native authentication failed')

    @staticmethod
    def _filesystem_row(row,require_present=True):
        if not isinstance(row,dict) or not row.get('id') or type(row.get('missing')) is not bool or (require_present and row['missing']) or type(row.get('libraryId')) is not int or row['libraryId']<1:
            raise ApiError('Navidrome filesystem row is missing/unverifiable')
        path=str(row.get('path') or '').replace('\\','/')
        if not path or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or re.match(r'^[A-Za-z]:',path):
            raise ApiError('Navidrome did not return a safe library-relative filesystem path')
        return dict(row,path=path,musicBrainzId=row.get('mbzRecordingID'))

    def filesystem_song(self,identity):
        row=self._filesystem_row(self.native_get('song/'+quote(str(identity),safe='')))
        if str(row['id'])!=str(identity):
            raise ApiError('Navidrome filesystem lookup returned another song')
        return row

    def filesystem_songs(self,offset,limit):
        rows=self.native_get('song',dict(_start=offset,_end=offset+limit,_sort='id',_order='ASC',missing='false'))
        if not isinstance(rows,list):
            raise ApiError('Navidrome filesystem index returned malformed rows')
        return [self._filesystem_row(row) for row in rows]

    def filesystem_page(self,offset,limit,missing=False):
        rows,total=self.native_get('song',dict(_start=offset,_end=offset+limit,_sort='id',_order='ASC',missing=str(missing).lower()),with_total=True)
        if not isinstance(rows,list):
            raise ApiError('Navidrome filesystem index returned malformed rows')
        result=[self._filesystem_row(row,require_present=not missing) for row in rows]
        if any(row['missing'] is not missing for row in result):
            raise ApiError('Navidrome index did not honor its missing-file filter')
        return dict(rows=result,total=total)

    def request(self, endpoint, params=None, write=False):
        salt = secrets.token_hex(8)
        auth = dict(u=self.username, t=hashlib.md5((self.password + salt).encode()).hexdigest(),
                    s=salt, v='1.16.1', c='navidrome-scripts', f='json')
        data = {**auth, **(params or {})}
        try:
            if write:
                response = self.session.post(self.base_url + '/rest/' + endpoint + '.view', data=data, timeout=(10, 90))
            else:
                response = self.session.get(self.base_url + '/rest/' + endpoint + '.view', params=data, timeout=(10, 90))
        except requests.RequestException:
            raise ApiError(endpoint + ' connection failed; write outcome may be unknown' if write else endpoint + ' connection failed') from None
        if response.status_code != 200:
            raise ApiError(f'{endpoint} returned HTTP {response.status_code}')
        try:
            payload = response.json()['subsonic-response']
        except (ValueError, KeyError, TypeError):
            raise ApiError(endpoint + ' returned an invalid response') from None
        if not isinstance(payload, dict):
            raise ApiError(endpoint + ' returned an invalid response')
        if payload.get('status') != 'ok':
            raise ApiError(endpoint + ' failed (Subsonic code ' + str((payload.get('error') or {}).get('code', 'unknown')) + ')')
        return payload

    def playlist(self, playlist_id):
        payload = self.request('getPlaylist', {'id': playlist_id})
        row = payload.get('playlist')
        if not isinstance(row, dict) or str(row.get('id')) != str(playlist_id):
            raise ApiError('getPlaylist did not return the requested playlist')
        return row

    def playlists(self):
        rows = self.request('getPlaylists').get('playlists', {}).get('playlist', [])
        if not isinstance(rows, list):
            raise ApiError('getPlaylists returned malformed data')
        return rows

    def search(self, title, artist):
        found, seen = [], set()
        for offset in range(0, 1001, 100):
            rows = self.request('search3', dict(query=title + ' ' + artist, artistCount=0,
                albumCount=0, songCount=100, songOffset=offset)).get('searchResult3', {}).get('song', [])
            if not isinstance(rows, list):
                raise ApiError('search3 returned malformed songs')
            for row in rows:
                key = str(row.get('id', ''))
                if not key or key in seen:
                    raise ApiError('search3 returned duplicate/missing IDs; pagination cannot be verified')
                seen.add(key); found.append(row)
            if len(rows) < 100:
                return found
        raise ApiError('search3 result limit reached; narrow the search before matching')

    def song(self, song_id):
        row = self.request('getSong', {'id': song_id}).get('song')
        if not isinstance(row, dict) or str(row.get('id')) != str(song_id):
            raise ApiError('getSong did not return the requested track')
        return row

    def replace_playlist(self, song_ids, *, playlist_id=None, name=None):
        extensions = self.request('getOpenSubsonicExtensions').get('openSubsonicExtensions', [])
        if not any(x.get('name') == 'formPost' for x in extensions):
            raise ApiError('server must support formPost for safe playlist updates')
        params = {'songId': song_ids}
        params.update({'playlistId': playlist_id} if playlist_id else {'name': name})
        return self.request('createPlaylist', params, write=True).get('playlist', {})


def from_env():
    return Subsonic(os.environ.get('NAVIDROME_URL') or os.environ.get('BASE_URL', ''),
                    os.environ.get('NAVIDROME_USERNAME') or os.environ.get('USERNAME', ''),
                    os.environ.get('NAVIDROME_PASSWORD') or os.environ.get('PASSWORD', ''))
