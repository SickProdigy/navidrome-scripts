"""Read playlist metadata through Spotify OAuth; no audio downloads."""
import os
import json
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit

import requests

from .subsonic import ApiError
from .locking import library_lock


def playlist_id(value):
    if value.startswith('spotify:playlist:'):
        value = value.split(':')[-1]
    elif value.startswith('https://open.spotify.com/'):
        parts = urlsplit(value).path.strip('/').split('/')
        if len(parts) != 2 or parts[0] != 'playlist':
            raise ValueError('expected a Spotify playlist URL')
        value = parts[1]
    if not re.fullmatch('[A-Za-z0-9]{22}', value):
        raise ValueError('expected a Spotify playlist ID, URI or URL')
    return value


class Spotify:
    def __init__(self, session=None, token_file=None):
        self.session = session or requests.Session()
        self.token_file = Path(token_file or os.environ.get('SPOTIFY_TOKEN_FILE', 'logs/spotify-playlists/spotify-token.json'))
        self.stored = json.loads(self.token_file.read_text(encoding='utf8')) if self.token_file.exists() else {}
        if self.stored and self.stored.get('client_id') != os.environ.get('SPOTIFY_CLIENT_ID'):
            raise ApiError('Spotify token file belongs to a different client; choose a separate SPOTIFY_TOKEN_FILE')
        self.token = os.environ.get('SPOTIFY_ACCESS_TOKEN', '')
        self.token = self.token or self.stored.get('access_token', '')
        self.refreshed = False

    def refresh(self):
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        with library_lock(self.token_file.parent):
            self.stored = json.loads(self.token_file.read_text(encoding='utf8')) if self.token_file.exists() else {}
            return self._refresh()

    def _refresh(self):
        refresh = self.stored.get('refresh_token') or os.environ.get('SPOTIFY_REFRESH_TOKEN')
        client_id = os.environ.get('SPOTIFY_CLIENT_ID')
        secret = os.environ.get('SPOTIFY_CLIENT_SECRET')
        if self.stored and self.stored.get('client_id') != client_id:
            raise ApiError('Spotify token file belongs to a different client')
        if not refresh or not client_id:
            raise ApiError('set SPOTIFY_ACCESS_TOKEN or SPOTIFY_CLIENT_ID and SPOTIFY_REFRESH_TOKEN')
        try:
            response = self.session.post('https://accounts.spotify.com/api/token',
                data={'grant_type': 'refresh_token', 'refresh_token': refresh, 'client_id': client_id},
                auth=(client_id, secret) if secret else None, timeout=(10, 30))
            payload = response.json()
        except (requests.RequestException, ValueError):
            raise ApiError('Spotify token refresh failed') from None
        if response.status_code != 200 or not isinstance(payload, dict) or not payload.get('access_token'):
            raise ApiError(f'Spotify token refresh failed (HTTP {response.status_code})')
        record = dict(client_id=client_id, access_token=payload['access_token'],
                      refresh_token=payload.get('refresh_token') or refresh)
        descriptor, temporary = tempfile.mkstemp(prefix='.spotify-token-', dir=self.token_file.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf8') as stream:
                json.dump(record, stream)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, self.token_file)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.stored = record
        self.token = payload['access_token']
        self.refreshed = True

    def get(self, path, **params):
        if not self.token:
            self.refresh()
        for attempt in range(3):
            try:
                response = self.session.get('https://api.spotify.com/v1/' + path,
                    params=params, headers={'Authorization': 'Bearer ' + self.token}, timeout=(10, 60))
            except requests.RequestException:
                raise ApiError('Spotify connection failed') from None
            if response.status_code == 401 and not self.refreshed:
                self.refresh(); continue
            if response.status_code == 429:
                try:
                    delay = int(response.headers.get('Retry-After', '5'))
                except ValueError:
                    delay = 5
                if 0 <= delay <= 60 and attempt < 2:
                    time.sleep(delay); continue
            if response.status_code != 200:
                raise ApiError(f'Spotify returned HTTP {response.status_code}; check token scopes and playlist access')
            try:
                payload = response.json()
            except ValueError:
                raise ApiError('Spotify returned invalid JSON') from None
            if not isinstance(payload, dict):
                raise ApiError('Spotify returned invalid JSON structure')
            return payload
        raise ApiError('Spotify request did not complete')

    def playlist(self, value):
        identity = playlist_id(value)
        metadata = self.get('playlists/' + identity, fields='id,name,snapshot_id')
        if metadata.get('id') != identity or not metadata.get('snapshot_id') or not metadata.get('name'):
            raise ApiError('Spotify playlist metadata is incomplete')
        rows, expected = [], None
        for offset in range(0, 100001, 50):
            page = self.get('playlists/' + identity + '/items', limit=50, offset=offset)
            if not isinstance(page.get('items'), list) or not isinstance(page.get('total'), int) or page.get('offset') != offset:
                raise ApiError('Spotify playlist page is incomplete')
            if expected is None:
                expected = page['total']
            if page['total'] != expected:
                raise ApiError('Spotify playlist changed while reading')
            rows.extend(page['items'])
            if not page.get('next'):
                break
            if len(page['items']) != 50:
                raise ApiError('Spotify playlist pagination is incomplete')
        if len(rows) != expected:
            raise ApiError('Spotify playlist item count differs from its total')
        final = self.get('playlists/' + identity, fields='id,name,snapshot_id')
        if final != metadata:
            raise ApiError('Spotify playlist changed while reading; retry the preview')
        return {**metadata, 'items': rows}
