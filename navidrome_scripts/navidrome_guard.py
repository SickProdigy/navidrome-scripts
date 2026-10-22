"""Read-only native filesystem and Subsonic user snapshots, never live SQLite."""
import time
import hashlib
import unicodedata
import argparse
import json
from pathlib import Path, PurePosixPath
from datetime import datetime, timezone
from dotenv import load_dotenv

from .dates import utc
from .playlists import playlist_snapshot
from .subsonic import ApiError, from_env

INDEX_FIELDS = ('id', 'path', 'size', 'title', 'album', 'artist', 'duration', 'musicBrainzId', 'missing')


def path_key(path):
    return unicodedata.normalize('NFC', path).casefold()


def scan_identity(client):
    row = client.request('getScanStatus').get('scanStatus')
    if not isinstance(row, dict) or row.get('scanning') is not False or not isinstance(row.get('count'), int):
        raise ValueError('Navidrome scan is active or cannot be verified')
    return {k: row.get(k) for k in ('scanning', 'count', 'folderCount', 'lastScan', 'scanType')}


def export_index(client, select_paths=None, prefix=''):
    user = client.request('getUser', {'username': client.username}).get('user', {})
    if user.get('adminRole') is not True:
        raise ValueError('duplicate quarantine requires a complete administrator index view')
    folders = client.request('getMusicFolders').get('musicFolders', {}).get('musicFolder', [])
    if len(folders) != 1:
        raise ValueError('multiple/missing Navidrome libraries need explicit root reconciliation before quarantine')
    before = scan_identity(client)
    songs, seen, covered = [], set(), 0
    started, clock = utc(), time.monotonic()
    wanted = {path_key(p) for p in select_paths} if select_paths is not None else None
    native=hasattr(client,'filesystem_page')
    totals={}
    # Missing records still hold IDs, ratings and playlist references. Include
    # both categories when proving that a quarantine candidate is unindexed.
    for missing in ((False,True) if native else (None,)):
        offset,expected=0,None
        while True:
            if native:
                page=client.filesystem_page(offset,500,missing)
                rows,total=page['rows'],page['total']
            else:
                rows=client.request('search3',dict(query='',artistCount=0,albumCount=0,songCount=500,songOffset=offset)).get('searchResult3',{}).get('song',[])
                total=before['count']
            if not isinstance(rows,list) or type(total) is not int or total<0:
                raise ApiError('Navidrome returned malformed index rows/counts')
            if expected is None:
                expected=total
            if total!=expected:
                raise ValueError('index count changed during export')
            for row in rows:
                if 'libraryId' in row and str(row['libraryId'])!=str(folders[0]['id']):
                    raise ValueError('native index includes an unexpected library; reconcile the root mapping')
                identity=str(row.get('id',''));token=hashlib.blake2b(identity.encode(),digest_size=16).digest()
                if not identity or not row.get('path') or token in seen:
                    raise ValueError('index has missing/duplicate IDs or paths; retry after the library settles')
                seen.add(token);raw_path=str(row['path'])
                if PurePosixPath(raw_path).is_absolute() or '..' in PurePosixPath(raw_path).parts or '\\' in raw_path:
                    raise ValueError('index path cannot be mapped safely')
                relative=relative_nav_path(raw_path,prefix)
                if select_paths is not None and relative is None:
                    raise ValueError('configured Navidrome prefix does not cover the complete index; explicit library reconciliation required')
                if wanted is None or path_key(relative) in wanted:
                    songs.append({key:row.get(key) for key in INDEX_FIELDS})
            offset+=len(rows)
            if offset>expected:
                raise ValueError('index count changed during export')
            if not rows:
                if offset!=expected:
                    raise ValueError('Navidrome index coverage/count changed or is incomplete')
                break
            if offset%5000==0:
                print(f'Navidrome {"missing" if missing else "active"} index: {offset}/{expected}',flush=True)
        totals['missing' if missing else 'active']=offset
        covered+=offset
    after = scan_identity(client)
    if before != after:
        raise ValueError('Navidrome index coverage/count changed or is incomplete')
    return dict(schema=1, kind='admin-index', server=client.base_url, username=client.username,
                started=started, ended=utc(), elapsed_seconds=round(time.monotonic() - clock, 2),
                scan=after, folders=folders, songs=songs, coverage_count=covered,counts=totals)


def relative_nav_path(path, prefix=''):
    path = PurePosixPath(path)
    prefix = PurePosixPath(prefix) if prefix else PurePosixPath('.')
    if path.is_absolute() or '..' in path.parts or not path.is_relative_to(prefix):
        return None
    return path.relative_to(prefix).as_posix()


def indexed_paths(index, prefix=''):
    result = {}
    for song in index['songs']:
        path = relative_nav_path(song['path'], prefix)
        if path is not None:
            result.setdefault(path, []).append(song)
    return result


def user_snapshot(client, song_ids):
    songs = {}
    for identity in sorted(set(song_ids)):
        row = client.song(identity)
        if hasattr(client,'filesystem_song'):
            row=dict(row,path=client.filesystem_song(identity)['path'])
        songs[identity] = {key: row.get(key) for key in
            ('id', 'path', 'title', 'album', 'artist', 'userRating', 'starred', 'playCount', 'played')}
    playlists = {}
    for row in client.playlists():
        full = playlist_snapshot(client.playlist(row['id']))
        # Preserve ordered membership, including repeated tracks, for playlists
        # visible to this account. Other users' private data is not exported.
        playlists[full['id']] = full
    return dict(server=client.base_url, username=client.username, songs=songs, playlists=playlists)


def capture(client, song_ids):
    before = scan_identity(client)
    state = user_snapshot(client, song_ids)
    if scan_identity(client) != before:
        raise ValueError('Navidrome changed while capturing the baseline')
    return dict(schema=1, kind='navidrome-preservation', created=utc(), scan=before, state=state)


def verify(client, baseline, path_map=None):
    if baseline.get('schema') != 1 or baseline.get('kind') != 'navidrome-preservation':
        raise ValueError('invalid preservation snapshot')
    before = baseline['state']
    if before['server'] != client.base_url or before['username'] != client.username:
        raise ValueError('preservation snapshot belongs to a different server/user')
    scan = scan_identity(client)
    if not scan.get('lastScan') or scan['lastScan'] == baseline['scan'].get('lastScan') or scan.get('scanType') != 'full':
        raise ValueError('a completed full Navidrome scan after the baseline is required for verification')
    errors = []
    try:
        after = user_snapshot(client, before['songs'])
    except ApiError:
        return dict(preserved=False, errors=['an original song or playlist identity is no longer retrievable'])
    path_map = path_map or {}
    known_paths = {song['path'] for song in before['songs'].values()}
    if not set(path_map).issubset(known_paths):
        raise ValueError('path map includes a path outside this reviewed snapshot')
    for identity, original in before['songs'].items():
        current = after['songs'][identity]
        if current['id'] != original['id'] or current['path'] != path_map.get(original['path'], original['path']):
            errors.append('song identity/path changed: ' + identity)
        if (current.get('userRating') or 0) != (original.get('userRating') or 0) or current.get('starred') != original.get('starred'):
            errors.append('personal rating/star changed: ' + identity)
        if int(current.get('playCount') or 0) < int(original.get('playCount') or 0) or (original.get('played') and not current.get('played')):
            errors.append('play history regressed: ' + identity)
        if original.get('played') and current.get('played'):
            try:
                old = datetime.fromisoformat(original['played'].replace('Z','+00:00'))
                new = datetime.fromisoformat(current['played'].replace('Z','+00:00'))
                old = old if old.tzinfo else old.replace(tzinfo=timezone.utc)
                new = new if new.tzinfo else new.replace(tzinfo=timezone.utc)
                if new < old:
                    errors.append('last-played timestamp regressed: ' + identity)
            except ValueError:
                errors.append('last-played timestamp could not be verified: ' + identity)
    if after['playlists'] != before['playlists']:
        errors.append('visible playlist IDs, metadata or ordered membership changed')
    if scan_identity(client) != scan:
        errors.append('scan state changed during verification')
    return dict(preserved=not errors, errors=errors, checked_songs=len(before['songs']),
                checked_playlists=len(before['playlists']), verified=utc(),
                note='Checks this account only; other users require their own snapshots. Listening may increase play counts.')


def main(argv=None):
    parser=argparse.ArgumentParser(description='Snapshot and verify Navidrome identities and personal data through its API')
    parser.add_argument('--env-file',type=Path,default=Path('.env'))
    sub=parser.add_subparsers(dest='action',required=True)
    snapshot=sub.add_parser('capture');snapshot.add_argument('--song-id',action='append',required=True);snapshot.add_argument('--output',type=Path,required=True)
    check=sub.add_parser('verify');check.add_argument('--snapshot',type=Path,required=True);check.add_argument('--path-map',type=Path)
    args=parser.parse_args(argv);load_dotenv(args.env_file)
    try:
        client=from_env()
        if args.action=='capture':
            payload=capture(client,args.song_id)
            with args.output.open('x',encoding='utf8') as stream:
                json.dump(payload,stream,indent=2,ensure_ascii=False)
            print('Preservation snapshot saved: '+str(args.output));return 0
        payload=json.loads(args.snapshot.read_text(encoding='utf8'))
        paths=json.loads(args.path_map.read_text(encoding='utf8')) if args.path_map else None
        result=verify(client,payload,paths);print(json.dumps(result));return 0 if result['preserved'] else 1
    except (ValueError,OSError,ApiError) as exc:
        parser.exit(1,str(exc)+'\n')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
