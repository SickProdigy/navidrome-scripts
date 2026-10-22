"""Reviewed restoration of an album rename and its Lidarr track assignments."""
from datetime import datetime
import json
from pathlib import Path, PurePosixPath

from . import dates, navidrome_guard as guard, renames
from .duplicates import copy_exclusive
from .locking import library_lock
from .playlists import hash_json, save
from .subsonic import ApiError
from .unmapped import album_identity, local_path


ACTIVE = {'queued', 'started'}


def quiet(client):
    # A scoped rescan must not race a still-running rename or another disk job.
    commands = client.get('command')
    if not isinstance(commands, list) or any(not isinstance(row, dict) for row in commands):
        raise ValueError('cannot verify the Lidarr command queue')
    if any(str(row.get('status', '')).casefold() in ACTIVE for row in commands):
        raise ValueError('wait for queued/running Lidarr commands before rollback')
    return commands


def expected(entry):
    return {key: entry[key] for key in ('sha256', 'size', 'mtime_ns')}


def validate_files(root, entries):
    known = set()
    for entry in entries:
        backup = Path(entry['backup'])
        if backup.is_symlink() or not backup.is_file() or renames.file_state(backup) != expected(entry):
            raise ValueError('rollback backup missing or changed: ' + entry['old'])
        for key in ('old', 'new'):
            path = dates.contained(root, entry[key])
            known.add(path)
            if path.exists() and renames.file_state(path) != expected(entry):
                raise ValueError('changed rollback path; refusing overwrite/removal: ' + entry[key])
    # Unknown files are retained. They cannot share a reviewed target filename.
    return known


def validate_contents(root, payload):
    known = validate_files(root, payload['entries'])
    for key in ('source_folder', 'destination_folder'):
        folder = dates.contained(root, payload[key])
        if not folder.exists():
            continue
        for path in folder.rglob('*'):
            dates.contained(root, path.relative_to(root).as_posix())
            if path.is_file() and path not in known:
                raise ValueError('album contents changed; review the extra file before rollback: ' + str(path))


def assignments(client, payload, restored=False):
    album = client.get('album/' + str(payload['album']['id']))
    artist = client.get('artist/' + str(payload['artist']['id']))
    if album_identity(album) != payload['album'] or any(artist.get(k) != v for k, v in payload['artist'].items()):
        raise ValueError('artist/album edition changed since rollback review')
    rows = client.get('trackfile', albumId=payload['album']['id'])
    by_path = {row['path']: row for row in rows}
    if len(by_path) != len(rows):
        raise ValueError('duplicate Lidarr paths during rollback')
    if restored:
        wanted = {row['old'] for row in payload['audio']}
        if set(by_path) != wanted:
            raise ValueError('restored Lidarr file assignments need review')
        for entry in payload['audio']:
            row = by_path[entry['old']]
            if row.get('albumId') != payload['album']['id']:
                raise ValueError('restored file belongs to another album')
    else:
        by_id = {row['id']: row for row in rows}
        if set(by_id) != {row['id'] for row in payload['audio']}:
            raise ValueError('Lidarr file identities changed since rollback review')
        for entry in payload['audio']:
            if by_id[entry['id']]['path'] not in (entry['old'], entry['new']):
                raise ValueError('Lidarr path moved outside the reviewed rename')
    return by_path


def plan(client, nav, root, forward, forward_journal):
    renames.validate_plan(client, nav, root, forward)
    renames.settings(client)
    quiet(client)
    record = json.loads(forward_journal.read_text(encoding='utf8'))
    if record.get('plan') != hash_json(forward) or not record.get('originals'):
        raise ValueError('forward journal/backups do not match the rename plan')
    if record.get('status') not in ('submitted', 'outcome_unknown', 'needs_review', 'awaiting_full_scan', 'verified', 'preservation_failed'):
        raise ValueError('reconcile the forward rename before rollback review')
    original = {row['path']: row for row in record['originals']}
    pairs = [(local_path(root, row['old'], forward['remote_root']).relative_to(root).as_posix(),
              local_path(root, row['new'], forward['remote_root']).relative_to(root).as_posix(), row)
             for row in forward['files']]
    pairs += [(row['path'], row['target'], row) for row in forward['sidecars']]
    entries = []
    for old, new, row in pairs:
        backup = original.get(old, {})
        if any(backup.get(k) != v for k, v in expected(row).items()):
            raise ValueError('forward backup manifest differs from the reviewed file')
        entries.append(dict(old=old, new=new, backup=backup['backup'], **expected(row)))
    if len(original) != len(entries) or len({e['old'] for e in entries}) != len(entries):
        raise ValueError('incomplete/ambiguous forward backup manifest')
    validate_files(root, entries)
    payload = dict(schema=1, kind='lidarr-rename-rollback', root=str(root.resolve()), server=client.url,
                   forward=hash_json(forward), forward_journal=str(forward_journal.resolve()),
                   album=forward['album'], artist=forward['artist'], remote_root=forward['remote_root'],
                   source_folder=forward['source_folder'], destination_folder=forward['destination_folder'],
                   audio=forward['files'], entries=entries, created=dates.utc())
    assignments(client, payload)
    # Capture the latest personal data, rather than overwriting ratings changed
    # since the forward preview. Every retained ID must still name its old/new file.
    baseline = guard.capture(nav, forward['baseline']['state']['songs'])
    path_map = {}
    for identity, song in baseline['state']['songs'].items():
        old = forward['baseline']['state']['songs'][identity]['path']
        if song['path'] not in (old, forward['path_map'][old]):
            raise ValueError('Navidrome identity moved outside the reviewed rename')
        path_map[song['path']] = old
    if baseline['scan'].get('scanType') != 'full':
        raise ValueError('complete a full Navidrome scan before rollback review')
    payload.update(baseline=baseline, path_map=path_map)
    validate_contents(root, payload)
    # Preserve track assignments even if the rescan creates new track-file IDs.
    releases = [r for r in forward['album']['releases'] if r.get('monitored')]
    if len(releases) != 1:
        raise ValueError('one selected Lidarr edition is required')
    tracks = client.get('track', albumReleaseId=releases[0]['id'])
    bindings = {row['id']: [] for row in payload['audio']}
    for track in tracks:
        if track.get('trackFileId') in bindings:
            bindings[track['trackFileId']].append({k: track.get(k) for k in ('id', 'foreignRecordingId', 'foreignTrackId')})
    if any(not rows for rows in bindings.values()):
        raise ValueError('each renamed file needs known Lidarr track assignments')
    payload['bindings'] = {str(key): value for key, value in bindings.items()}
    return payload


def validate(client, nav, root, payload):
    if (payload.get('schema') != 1 or payload.get('kind') != 'lidarr-rename-rollback'
            or Path(payload['root']).resolve() != root.resolve() or payload['server'] != client.url):
        raise ValueError('rollback plan schema/root/server differs')
    state = payload['baseline']['state']
    if state['server'] != nav.base_url or state['username'] != nav.username:
        raise ValueError('rollback baseline belongs to another Navidrome account')


def check_restored(client, root, payload):
    mapped = assignments(client, payload, restored=True)
    for entry in payload['entries']:
        if renames.file_state(dates.contained(root, entry['old'])) != expected(entry):
            raise ValueError('restored bytes/timestamp differ: ' + entry['old'])
        if entry['old'] != entry['new'] and dates.contained(root, entry['new']).exists():
            raise ValueError('renamed copy still exists after rollback')
    for entry in payload['audio']:
        for binding in payload['bindings'][str(entry['id'])]:
            track = client.get('track/' + str(binding['id']))
            if (any(track.get(k) != v for k, v in binding.items())
                    or track.get('trackFileId') != mapped[entry['old']]['id']):
                raise ValueError('restored Lidarr track binding needs review')


def command_body(payload):
    folders = {str(PurePosixPath(payload['remote_root']) / payload[key])
               for key in ('source_folder', 'destination_folder')}
    return dict(name='RescanFolders', folders=sorted(folders), filter='known',
                addNewArtists=False, artistIds=[payload['artist']['id']])


def apply(client, nav, root, payload, journal):
    validate(client, nav, root, payload)
    with library_lock(root):
        renames.settings(client)
        signature = hash_json(payload)
        record = json.loads(journal.read_text(encoding='utf8')) if journal.exists() else {}
        if record and record.get('plan') != signature:
            raise ValueError('rollback journal belongs to another plan')
        if record.get('status') in ('awaiting_full_scan', 'verified', 'preservation_failed'):
            check_restored(client, root, payload)
            return 0
        if not record:
            quiet(client)
            assignments(client, payload)
            if (guard.scan_identity(nav) != payload['baseline']['scan']
                    or guard.user_snapshot(nav, payload['baseline']['state']['songs']) != payload['baseline']['state']):
                raise ValueError('Navidrome changed since rollback review; create a fresh plan')
            validate_contents(root, payload)
            record = dict(plan=signature, status='restoring', started=dates.utc())
            save(journal, record, exclusive=True)
        forward_journal = Path(payload['forward_journal'])
        forward = json.loads(forward_journal.read_text(encoding='utf8'))
        if forward.get('plan') != payload['forward'] or forward.get('rollback') not in (None, str(journal.resolve())):
            raise ValueError('forward journal changed or another rollback is active')
        forward['rollback'] = str(journal.resolve()); save(forward_journal, forward)
        if record['status'] == 'restoring':
            quiet(client)
            scan = guard.scan_identity(nav)
            validate_contents(root, payload)
            for entry in payload['entries']:
                old, new = (dates.contained(root, entry[key]) for key in ('old', 'new'))
                if not old.exists():
                    copy_exclusive(Path(entry['backup']), old, entry['sha256'])
                if renames.file_state(old) != expected(entry):
                    raise ValueError('restored file changed; both copies retained')
                if old != new and new.exists():
                    if renames.file_state(new) != expected(entry):
                        raise ValueError('renamed copy changed; both copies retained')
                    new.unlink()
            # Lidarr skips an existing empty folder without cleaning stale file
            # rows. Remove only empty directories in the reviewed destination;
            # a missing destination lets its scoped rescan clean those mappings.
            destination = dates.contained(root, payload['destination_folder'])
            source_folder = dates.contained(root, payload['source_folder'])
            if destination != source_folder and destination.exists():
                for directory in sorted((p for p in destination.rglob('*') if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                    if not directory.is_symlink() and not any(directory.iterdir()):
                        directory.rmdir()
                if not any(destination.iterdir()):
                    destination.rmdir()
            if guard.scan_identity(nav) != scan:
                raise ValueError('Navidrome scanned during rollback; review before continuing')
            # Journal intent before POST. Unknown responses are never retried.
            record.update(status='submitting', restored_at=dates.utc(), command_body=command_body(payload),
                          previous_commands=[r['id'] for r in quiet(client)])
            save(journal, record)
            try:
                response = client.post('command', record['command_body'])
                if type(response.get('id')) is not int:
                    raise ApiError('rollback rescan returned no command ID; outcome unknown')
                record.update(status='submitted', command=response['id']); save(journal, record)
            except ApiError:
                record['status'] = 'outcome_unknown'; save(journal, record)
                raise
        if not record.get('command'):
            candidates = [row for row in client.get('command') if row.get('id') not in record['previous_commands']
                          and all(row.get('body', {}).get(k) == v for k, v in record['command_body'].items())]
            if len(candidates) != 1:
                raise ValueError('rollback rescan outcome unknown; inspect commands, do not resubmit')
            record['command'] = candidates[0]['id']; save(journal, record)
        command = client.get('command/' + str(record['command']))
        if str(command.get('status', '')).casefold() in ACTIVE:
            print('Rollback rescan queued/running. Repeat this command to check it; no new command is submitted.')
            return 2
        if str(command.get('status', '')).casefold() != 'completed':
            raise ValueError('rollback rescan failed; restored files retained for assignment review')
        check_restored(client, root, payload)
        record.update(status='awaiting_full_scan', updated=dates.utc()); save(journal, record)
        print('Original paths and Lidarr assignments restored. Complete a new full Navidrome scan, then verify rollback.')
        return 0


def verify(client, nav, root, payload, journal):
    validate(client, nav, root, payload)
    record = json.loads(journal.read_text(encoding='utf8'))
    if record.get('plan') != hash_json(payload) or record.get('status') not in ('awaiting_full_scan', 'verified', 'preservation_failed'):
        raise ValueError('reconcile rollback assignments before verifying preservation')
    check_restored(client, root, payload)
    scan = guard.scan_identity(nav)
    try:
        scanned = datetime.fromisoformat(str(scan['lastScan']).replace('Z', '+00:00'))
        restored = datetime.fromisoformat(record['restored_at'].replace('Z', '+00:00'))
        if scan.get('scanType') != 'full' or scanned <= restored:
            raise ValueError('old scan')
    except (KeyError, TypeError, ValueError):
        raise ValueError('complete a new full Navidrome scan after rollback') from None
    result = guard.verify(nav, payload['baseline'], payload['path_map'])
    record.update(status='verified' if result['preserved'] else 'preservation_failed', verification=result)
    save(journal, record)
    print(json.dumps(result))
    return 0 if result['preserved'] else 1
