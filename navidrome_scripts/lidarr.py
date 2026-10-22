"""Small strict Lidarr API client; network writes are never blindly retried."""
import os
from urllib.parse import urlsplit

import requests

from .catalog import CLIENT
from .subsonic import ApiError


class Lidarr:
    def __init__(self, url, key, session=None):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('plain Lidarr http(s) URL required')
        if not key:
            raise ValueError('LIDARR_API_KEY is required')
        self.url = url.rstrip('/')
        self.session = session or requests.Session()
        self.session.headers.update({'X-Api-Key': key, 'User-Agent': CLIENT})

    def request(self, endpoint, *, params=None, body=None):
        write = body is not None
        try:
            response = self.session.post(self.url + '/api/v1/' + endpoint, json=body, timeout=(10, 90)) if write else self.session.get(self.url + '/api/v1/' + endpoint, params=params, timeout=(10, 90))
        except requests.RequestException:
            raise ApiError('Lidarr ' + endpoint + (' write outcome unknown; verify before retrying' if write else ' request failed')) from None
        if not 200 <= response.status_code < 300:
            raise ApiError(f'Lidarr {endpoint} returned HTTP {response.status_code}')
        try:
            return response.json()
        except ValueError:
            raise ApiError('Lidarr ' + endpoint + ' returned invalid JSON') from None

    def get(self, endpoint, **params):
        return self.request(endpoint, params=params)

    def post(self, endpoint, body):
        return self.request(endpoint, body=body)


def from_env():
    return Lidarr(os.environ.get('LIDARR_URL', ''), os.environ.get('LIDARR_API_KEY', ''))
