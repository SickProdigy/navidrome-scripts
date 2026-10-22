"""Preview Spotify playlist matches, then explicitly sync a reviewed Navidrome playlist."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import unicodedata

from dotenv import load_dotenv

from .dates import utc
from .locking import library_lock
from .spotify import Spotify, playlist_id
from .subsonic import ApiError, from_env


def normalized(value):
    return ' '.join(re.sub(r'[^\w]+', ' ', unicodedata.normalize('NFKC', str(value)).casefold()).split())


def hash_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def save(path, payload, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        with path.open('x', encoding='utf8') as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
        return
    descriptor, temporary = tempfile.mkstemp(prefix='.playlist-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf8') as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def song_identity(song):
    return {key: song.get(key) for key in ('id', 'title', 'artist', 'artists', 'album', 'duration', 'isrc', 'path')}


def source_track(item):
    # Current API uses item; older complete JSON exports use track.
    track = item.get('item', item.get('track')) if isinstance(item, dict) else None
    if not isinstance(track, dict) or track.get('type') != 'track' or item.get('is_local') or track.get('is_local'):
        return None
    return dict(id=track.get('id'), title=track.get('name', ''),
                artists=[x.get('name', '') for x in track.get('artists', [])],
                album=(track.get('album') or {}).get('name', ''), duration_ms=track.get('duration_ms'),
                isrc=(track.get('external_ids') or {}).get('isrc', ''))


def match(track, candidates):
    if not track or not track['title'] or not track['album'] or not track['artists'] or not track.get('duration_ms'):
        return dict(status='unsupported', reason='Unavailable/local/non-track item or incomplete identifying metadata', candidates=[])
    artists = {normalized(x) for x in track['artists']}
    matches = []
    for song in candidates:
        if song.get('isVideo') or song.get('type', 'music') not in ('music', None):
            continue
        if normalized(song.get('title', '')) != normalized(track['title']):
            continue
        local_artists = {normalized(x.get('name', '')) for x in song.get('artists', [])} or {normalized(song.get('artist', ''))}
        if local_artists != artists:
            continue
        if not song.get('duration') or abs(float(song['duration']) * 1000 - float(track['duration_ms'])) > 2500:
            continue
        codes = song.get('isrc') or []
        codes = [codes] if isinstance(codes, str) else codes
        same_isrc = bool(track['isrc'] and track['isrc'].upper() in {str(x).upper() for x in codes})
        if not same_isrc and normalized(song.get('album', '')) != normalized(track['album']):
            continue
        matches.append(song_identity(song))
    # Identical recordings in multiple paths still require an explicit keeper choice.
    if len(matches) == 1:
        return dict(status='matched', reason='Unique title/artist/duration and album or ISRC match', song=matches[0], candidates=matches)
    return dict(status='ambiguous' if matches else 'missing',
                reason='Multiple library copies/editions' if matches else 'No sufficiently exact local match', candidates=matches)


def playlist_snapshot(row):
    entries = row.get('entry', [])
    if not isinstance(entries, list) or int(row.get('songCount', len(entries))) != len(entries):
        raise ApiError('playlist contents are incomplete')
    return dict(id=str(row['id']), name=row['name'], owner=row.get('owner'),
                comment=row.get('comment', ''), public=row.get('public', False),
                songs=[str(x['id']) for x in entries])


def binding(client, spotify_id):
    return dict(server=client.base_url, username=client.username, spotify_id=spotify_id)


def load_source(path):
    payload = json.loads(path.read_text(encoding='utf-8-sig'))
    playlist_id(payload['id'])
    if not payload.get('name') or not payload.get('snapshot_id'):
        raise ValueError('complete Spotify export requires id, name and snapshot_id')
    items = payload.get('items', payload.get('tracks'))
    if isinstance(items, dict):
        if items.get('next') or items.get('offset', 0) != 0 or items.get('total') != len(items.get('items', [])):
            raise ValueError('Spotify export is a partial page')
        items = items.get('items')
    if not isinstance(items, list):
        raise ValueError('Spotify export must include all playlist items')
    return dict(id=payload['id'], name=payload['name'], snapshot_id=payload['snapshot_id'], items=items)


def preview(client, source, state_path, *, target_id=None):
    context = binding(client, source['id'])
    key = hash_json(context)
    state = json.loads(state_path.read_text(encoding='utf8')) if state_path.exists() else {}
    mapping = state.get(key, {})
    if mapping.get('status') == 'pending':
        raise ValueError('pending playlist write: recover by applying the previous plan first')
    target_id = target_id or mapping.get('id')
    baseline = playlist_snapshot(client.playlist(target_id)) if target_id else None
    if baseline and baseline['owner'] != client.username:
        raise ValueError('target playlist must belong to the configured user')
    name = 'Spotify - ' + source['name'] + ' [spotify:' + source['id'] + ']'
    if not target_id and any(x.get('name') == name and x.get('owner') == client.username for x in client.playlists()):
        raise ValueError('destination name already exists; review it and pass --playlist-id explicitly')
    rows, cache = [], {}
    for index, item in enumerate(source['items']):
        track = source_track(item)
        row = dict(position=index + 1, source=track)
        if track and track['title'] and track['artists']:
            search_key = (track['title'], track['artists'][0])
            try:
                if search_key not in cache:
                    cache[search_key] = client.search(*search_key)
                row.update(match(track, cache[search_key]))
            except ApiError as exc:
                row.update(status='error', reason=str(exc), candidates=[])
        else:
            row.update(match(track, []))
        rows.append(row)
        if (index + 1) % 25 == 0:
            print(f'Matched {index + 1}/{len(source["items"])} playlist items', flush=True)
    return dict(schema=1, kind='spotify-playlist', created=utc(), binding=context,
                source=dict(id=source['id'], name=source['name'], snapshot_id=source['snapshot_id'],
                            url='https://open.spotify.com/playlist/' + source['id']),
                destination_name=name, baseline=baseline, rows=rows,
                summary=dict(Counter(row['status'] for row in rows)))


def apply(client, plan, state_path, *, allow_partial=False, allow_empty=False):
    if plan.get('schema') != 1 or plan.get('kind') != 'spotify-playlist' or plan['binding'] != binding(client, plan['source']['id']):
        raise ValueError('plan belongs to a different server, user or source')
    if any(x['status'] == 'error' for x in plan['rows']):
        raise ValueError('plan has lookup errors; generate a successful preview first')
    if not allow_partial and any(x['status'] != 'matched' for x in plan['rows']):
        raise ValueError('unresolved playlist items require review or --allow-partial')
    desired = [str(x['song']['id']) for x in plan['rows'] if x['status'] == 'matched']
    if not desired and not allow_empty:
        raise ValueError('empty playlist update requires --allow-empty')
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with library_lock(state_path.parent):
        state = json.loads(state_path.read_text(encoding='utf8')) if state_path.exists() else {}
        key, signature = hash_json(plan['binding']), hash_json(plan)
        record = state.get(key, {})
        baseline = plan['baseline']
        target_id = baseline['id'] if baseline else None
        if record.get('status') == 'pending' and record.get('plan') != signature:
            raise ValueError('a different plan has an unresolved write')
        if record.get('plan') == signature:
            target_id = record.get('id') or target_id
            if not target_id and record.get('status') == 'pending':
                found = [x for x in client.playlists() if x.get('name') == plan['destination_name'] and x.get('owner') == client.username]
                if len(found) != 1:
                    raise ValueError('creation outcome unknown; inspect Navidrome before retrying')
                target_id = str(found[0]['id'])
            if target_id:
                current = playlist_snapshot(client.playlist(target_id))
                if current['owner'] == client.username and current['songs'] == desired:
                    record.update(status='complete', id=target_id, updated=utc())
                    state[key] = record; save(state_path, state)
                    return target_id
        for row in plan['rows']:
            if row['status'] == 'matched' and song_identity(client.song(row['song']['id'])) != row['song']:
                raise ValueError('matched song changed since preview; regenerate the plan')
        if target_id:
            if playlist_snapshot(client.playlist(target_id)) != baseline:
                raise ValueError('playlist changed since preview; regenerate the plan')
        elif any(x.get('name') == plan['destination_name'] and x.get('owner') == client.username for x in client.playlists()):
            raise ValueError('destination name appeared after preview; inspect it before creating')
        # Save the original playlist and write intent before making a network mutation.
        backup = state_path.parent / ('playlist-backup-' + signature + '.json')
        backup_payload = dict(baseline=baseline, plan=signature)
        if backup.exists():
            if json.loads(backup.read_text(encoding='utf8')) != backup_payload:
                raise ValueError('saved playlist backup differs; inspect it before continuing')
        else:
            save(backup, backup_payload, exclusive=True)
        state[key] = dict(status='pending', plan=signature, id=target_id, updated=utc())
        save(state_path, state)
        result = client.replace_playlist(desired, playlist_id=target_id,
                                         name=plan['destination_name'] if not target_id else None)
        target_id = target_id or str(result.get('id') or '')
        if not target_id:
            raise ApiError('playlist creation returned no ID; recover with the same reviewed plan')
        current = playlist_snapshot(client.playlist(target_id))
        if current['songs'] != desired or current['owner'] != client.username:
            raise ApiError('playlist verification failed; inspect the pending state and saved backup')
        state[key].update(status='complete', id=target_id, updated=utc())
        save(state_path, state)
        return target_id


def restore(client, plan, state_path):
    """Restore an existing playlist's saved contents without deleting new playlists."""
    if plan.get('schema') != 1 or plan.get('kind') != 'spotify-playlist' or plan['binding'] != binding(client, plan['source']['id']):
        raise ValueError('plan belongs to a different server, user or source')
    baseline = plan['baseline']
    if not baseline:
        raise ValueError('this plan created a new playlist; review/delete it manually instead of restoring an existing one')
    desired = [str(x['song']['id']) for x in plan['rows'] if x['status'] == 'matched']
    with library_lock(state_path.parent):
        key, signature = hash_json(plan['binding']), hash_json(plan)
        state = json.loads(state_path.read_text(encoding='utf8'))
        record = state.get(key, {})
        if record.get('plan') != signature or record.get('id') != baseline['id']:
            raise ValueError('state no longer refers to this plan')
        backup = json.loads((state_path.parent / ('playlist-backup-' + signature + '.json')).read_text(encoding='utf8'))
        if backup != dict(baseline=baseline, plan=signature):
            raise ValueError('saved playlist backup differs')
        current = playlist_snapshot(client.playlist(baseline['id']))
        if current == baseline:
            state[key].update(status='restored', updated=utc()); save(state_path, state)
            return baseline['id']
        if current != dict(baseline, songs=desired):
            raise ValueError('playlist changed after synchronization; review before restoring')
        for identity in set(baseline['songs']):
            client.song(identity)
        state[key].update(status='pending', updated=utc()); save(state_path, state)
        client.replace_playlist(baseline['songs'], playlist_id=baseline['id'])
        if playlist_snapshot(client.playlist(baseline['id'])) != baseline:
            raise ApiError('restored playlist verification failed')
        state[key].update(status='restored', updated=utc()); save(state_path, state)
        return baseline['id']


def export_m3u(plan, output, client):
    if plan['binding']!=binding(client,plan['source']['id']):
        raise ValueError('playlist export belongs to a different server/account')
    lines = ['#EXTM3U', '# Source: ' + plan['source']['url']]
    libraries=set()
    for row in plan['rows']:
        if row['status'] != 'matched':
            continue
        if song_identity(client.song(row['song']['id']))!=row['song']:
            raise ValueError('matched song changed since preview; regenerate the plan')
        if not hasattr(client,'filesystem_song'):
            raise ApiError('filesystem export requires Navidrome native API access')
        physical=client.filesystem_song(row['song']['id'])
        path=physical['path'];libraries.add(physical['libraryId'])
        if len(libraries)>1:
            raise ValueError('playlist spans multiple library roots; export them separately')
        if not path or '\n' in path or '\r' in path or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or '\\' in path or ':' in path or path.startswith('#'):
            raise ValueError('playlist song has no safe relative library path')
        lines.append(path)
    with output.open('x', encoding='utf8', newline='\n') as stream:
        stream.write('\n'.join(lines) + '\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--state', type=Path, default=Path('logs/spotify-playlists/state.json'))
    sub = parser.add_subparsers(dest='command', required=True)
    plan = sub.add_parser('plan')
    source = plan.add_mutually_exclusive_group(required=True)
    source.add_argument('--spotify-playlist'); source.add_argument('--source-json', type=Path)
    plan.add_argument('--playlist-id'); plan.add_argument('--output', type=Path, required=True)
    execute = sub.add_parser('apply'); execute.add_argument('--plan', type=Path, required=True)
    execute.add_argument('--allow-partial', action='store_true'); execute.add_argument('--allow-empty', action='store_true')
    undo = sub.add_parser('restore'); undo.add_argument('--plan', type=Path, required=True)
    export = sub.add_parser('export'); export.add_argument('--plan', type=Path, required=True); export.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    load_dotenv(args.env_file)
    try:
        if args.command == 'export':
            export_m3u(json.loads(args.plan.read_text(encoding='utf8')), args.output, from_env())
            return 0
        client = from_env()
        if args.command == 'plan':
            source = load_source(args.source_json) if args.source_json else Spotify().playlist(args.spotify_playlist)
            payload = preview(client, source, args.state, target_id=args.playlist_id)
            save(args.output, payload, exclusive=True)
            print(json.dumps(payload['summary']), flush=True)
        elif args.command == 'restore':
            identity = restore(client, json.loads(args.plan.read_text(encoding='utf8')), args.state)
            print('Playlist restored: ' + identity)
        else:
            identity = apply(client, json.loads(args.plan.read_text(encoding='utf8')), args.state,
                             allow_partial=args.allow_partial, allow_empty=args.allow_empty)
            print('Playlist synchronized: ' + identity)
    except (ApiError, ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
