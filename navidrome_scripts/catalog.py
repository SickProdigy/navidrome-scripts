"""Read-only MusicBrainz search used by library review."""
import re
import time
import unicodedata
import requests
from . import __version__

CLIENT = f"navidrome-scripts/{__version__} (https://gitea.rcs1.top/sickprodigy/navidrome-scripts)"

def normalize(value):
    return re.sub(r"[^\w]+", "", unicodedata.normalize("NFKC", value).casefold())

def without_trailing_parenthetical(value):
    return re.sub(r"\s*\([^()]*\)\s*$", "", value).strip()

def lucene_quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'

class MusicBrainzClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = CLIENT
        self.last_request = 0.0

    def search(self, entity, query, limit=10):
        time.sleep(max(0, 1.1 - (time.monotonic() - self.last_request)))
        try:
            response = self.session.get('https://musicbrainz.org/ws/2/' + entity,
                params={'query': query, 'limit': limit, 'fmt': 'json'}, timeout=(10, 60))
            response.raise_for_status()
            return response.json()
        finally:
            self.last_request = time.monotonic()

    def lookup_release(self, identity):
        if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', identity):
            raise ValueError('a valid exact release MBID is required')
        time.sleep(max(0, 1.1 - (time.monotonic() - self.last_request)))
        try:
            response = self.session.get('https://musicbrainz.org/ws/2/release/' + identity,
                params={'fmt':'json','inc':'artist-credits+release-groups+recordings'}, timeout=(10,60))
            response.raise_for_status()
            return response.json()
        finally:
            self.last_request = time.monotonic()
