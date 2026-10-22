"""Read identifying tags without changing audio or trusting folder names."""
import re
from mutagen import File

from .dates import values

CORE_TAG_FIELDS=('title','artist','artists','album','albumartist','albumartists','track','tracktotal','disc','disctotal',
                 'mb_trackid','mb_releasetrackid','mb_albumid','mb_releasegroupid','mb_artistids','mb_albumartistids')

ALIASES = {
    'tpe1': 'artist', '©art': 'artist', 'tpe2': 'albumartist', 'aart': 'albumartist',
    'talb': 'album', '©alb': 'album', 'tit2': 'title', '©nam': 'title',
    'trck': 'tracknumber', 'trkn': 'tracknumber', 'tpos': 'discnumber', 'disk': 'discnumber',
    'musicbrainzalbumid': 'release_mbid', 'musicbrainzreleasegroupid': 'group_mbid',
    'musicbrainztrackid': 'recording_mbid', 'musicbrainzreleasetrackid': 'release_track_mbid',
    'musicbrainzalbumartistid': 'album_artist_mbid', 'musicbrainzartistid': 'artist_mbid',
}


def identify(path):
    audio = File(path)
    if audio is None or not getattr(audio, 'info', None):
        raise ValueError('unreadable or unsupported audio')
    result = {}
    for raw, value in (audio.tags or {}).items():
        key = str(raw).casefold().removeprefix('txxx:').removeprefix('----:com.apple.itunes:')
        key = re.sub(r'[ _-]', '', key)
        key = ALIASES.get(key, key)
        if str(raw).casefold() == 'ufid:http://musicbrainz.org':
            key, value = 'recording_mbid', [value.data]
        if key not in {'artist', 'albumartist', 'album', 'title', 'tracknumber', 'discnumber',
                       'release_mbid', 'group_mbid', 'recording_mbid', 'release_track_mbid',
                       'album_artist_mbid', 'artist_mbid'}:
            continue
        if key in ('discnumber', 'tracknumber') and isinstance(value, list) and value and isinstance(value[0], tuple):
            value = [value[0][0]]
        text = values(value)
        if len(text) != 1:
            raise ValueError('multiple identifying values require manual review: ' + key)
        result[key] = text[0].strip()
    for key in ('discnumber', 'tracknumber'):
        raw = result.get(key, '1' if key == 'discnumber' else '')
        match = re.fullmatch(r'(\d+)(?:/\d+)?', raw)
        result[key] = int(match[1]) if match else 0
    result['duration_ms'] = round(float(audio.info.length) * 1000)
    return result
