"""Review and reconcile one album's Lidarr rename, separately from tag repairs."""
from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path, PurePosixPath
import shutil

from dotenv import load_dotenv

from . import dates, navidrome_guard as guard
from .lidarr import from_env as lidarr_from_env
from .locking import library_lock,require_tag_scan,tag_state
from .maintenance import physical_files
from .metadata import identify
from .playlists import hash_json, normalized, save
from .subsonic import ApiError, from_env as nav_from_env
from .unmapped import album_identity, local_path


def settings(client):
    if str(client.get('config/mediamanagement').get('fileDate', '')).casefold() != 'none':
        raise ValueError('Lidarr File Date must be None before renaming')
    if str(client.get('config/metadataprovider').get('writeAudioTags', '')).casefold() != 'no':
        raise ValueError('disable Lidarr audio-tag writing before this separate rename operation')
    return client.get('config/naming')


def previews(client, artist, album):
    rows = client.get('rename', artistId=artist, albumId=album)
    fields = ('artistId', 'albumId', 'trackFileId', 'existingPath', 'newPath')
    if any(r.get('artistId') != artist or r.get('albumId') != album for r in rows):
        raise ValueError('rename preview contains another artist/album')
    result = sorted([{k:r[k] for k in fields} for r in rows], key=lambda r:r['trackFileId'])
    if len({r['trackFileId'] for r in result}) != len(result):
        raise ValueError('duplicate rename preview file IDs')
    return result


def file_state(path):
    stat = path.stat()
    if not path.is_file() or stat.st_nlink > 1:
        raise ValueError('non-file/hardlinked input requires separate review')
    return dict(sha256=dates.digest(path), size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def folder_for(paths):
    folder = Path(os.path.commonpath([str(p.parent) for p in paths]))
    import re
    if re.fullmatch(r'(?:CD|Disc)[ _-]*\d+', folder.name, re.I):
        folder = folder.parent
    return folder


def plan(client, nav, root, album_id, remote_root='/music', prefix=''):
    root = root.resolve(strict=True)
    naming = settings(client)
    album = client.get('album/' + str(album_id))
    artist = client.get('artist/' + str(album['artistId']))
    if not album.get('releaseDate') or dates.date_problem(str(album['releaseDate'])):
        raise ValueError('correct the current Lidarr album date before planning a folder rename')
    rows = previews(client, artist['id'], album_id)
    if not rows:
        raise ValueError('Lidarr proposes no renames for this album')
    mapped = client.get('trackfile', albumId=album_id)
    by_id = {r['id']:r for r in mapped}
    if len(by_id) != len(mapped) or any(r.get('albumId') != album_id for r in mapped):
        raise ValueError('inconsistent album file mapping')
    artist_root = local_path(root, artist['path'], remote_root)
    selected = {r['trackFileId']:r for r in rows}
    if not set(selected).issubset(by_id):
        raise ValueError('preview refers to an unmapped file')
    files = []
    for identity, row in sorted(by_id.items()):
        proposed = selected.get(identity)
        if proposed and proposed['existingPath'] != row['path']:
            raise ValueError('preview path differs from the mapped file')
        source = local_path(root, row['path'], remote_root)
        target_remote = proposed['newPath'] if proposed else row['path']
        target = local_path(root, target_remote, remote_root)
        if not source.is_relative_to(artist_root) or not target.is_relative_to(artist_root):
            raise ValueError('rename leaves the existing artist folder')
        if source != target and (guard.path_key(str(source)) == guard.path_key(str(target)) or target.exists()):
            raise ValueError('destination exists or aliases the source; review separately')
        files.append(dict(id=identity, old=row['path'], new=target_remote, **file_state(source)))
    if len({guard.path_key(r['new']) for r in files}) != len(files):
        raise ValueError('multiple files share a destination')
    sources = [local_path(root, r['old'], remote_root) for r in files]
    folder = folder_for(sources)
    if folder == artist_root or not folder.is_relative_to(artist_root):
        raise ValueError('album spans multiple folders or loose artist-level files')
    physical_files(folder, sources)
    destination = folder_for([local_path(root, r['new'], remote_root) for r in files])
    if destination == artist_root:
        raise ValueError('destination must retain an album folder')
    # A pre-existing destination album may contain another edition or artwork.
    if destination != folder and destination.exists():
        raise ValueError('destination album folder already exists; review the merge separately')
    sidecars = []
    for path in sorted(folder.rglob('*')):
        if path.is_file() and path.suffix.lower() not in dates.AUDIO:
            if path.suffix.lower() in ('.cue','.m3u','.m3u8','.pls'):
                raise ValueError('local playlist/cue references require separate review before renaming')
            # Track-named lyrics follow the reviewed audio stem. Album artwork
            # and other files retain their relative location within the album.
            candidates = [row for row in files if local_path(root,row['old'],remote_root).with_suffix('') == path.with_suffix('')]
            if len(candidates)>1:
                raise ValueError('sidecar has multiple possible audio owners')
            target = local_path(root,candidates[0]['new'],remote_root).with_suffix(path.suffix) if candidates else destination/path.relative_to(folder)
            if path != target and (guard.path_key(str(path)) == guard.path_key(str(target)) or target.exists()):
                raise ValueError('sidecar destination conflict requires review')
            sidecars.append(dict(path=path.relative_to(root).as_posix(), target=target.relative_to(root).as_posix(), **file_state(path)))
    destinations = [guard.path_key(row['target']) for row in sidecars]
    if len(set(destinations)) != len(destinations) or set(destinations).intersection(guard.path_key(local_path(root,row['new'],remote_root).relative_to(root).as_posix()) for row in files):
        raise ValueError('sidecar destinations collide')
    relatives = [p.relative_to(root).as_posix() for p in sources]
    new_relatives=[local_path(root,row['new'],remote_root).relative_to(root).as_posix() for row in files]
    index = guard.export_index(nav, set(relatives+new_relatives), prefix)
    if index['scan'].get('scanType') != 'full':
        raise ValueError('complete a full Navidrome scan after tag repairs before planning path changes')
    repair_state=require_tag_scan(root,index['scan'])
    indexed = guard.indexed_paths(index, prefix)
    song_ids = []
    path_map = {}
    for entry, relative in zip(files, relatives):
        found = indexed.get(relative, [])
        aliases=[song for key,songs in indexed.items() if guard.path_key(key)==guard.path_key(relative) for song in songs]
        target_relative=local_path(root,entry['new'],remote_root).relative_to(root).as_posix()
        if relative != target_relative and any(guard.path_key(key)==guard.path_key(target_relative) for key in indexed):
            raise ValueError('destination already has a Navidrome identity; reconcile history before renaming')
        if len(found) != 1 or len(aliases)!=1 or found[0].get('size') != entry['size']:
            raise ValueError('each album file needs one exact indexed Navidrome identity/size')
        tags=identify(sources[len(song_ids)])
        if any(normalized(found[0].get(key) or '') != normalized(tags.get(key) or '') for key in ('title','artist','album')) or abs(int(found[0].get('duration') or 0)*1000-tags['duration_ms'])>2500:
            raise ValueError('indexed Navidrome metadata differs from the file; scan or reconcile it first')
        if found[0].get('musicBrainzId') and tags.get('recording_mbid') and found[0]['musicBrainzId'].casefold()!=tags['recording_mbid'].casefold():
            raise ValueError('indexed recording identity differs from the file')
        song_ids.append(found[0]['id'])
        path_map[found[0]['path']] = '/'.join(filter(None, [prefix.strip('/'), local_path(root,entry['new'],remote_root).relative_to(root).as_posix()]))
    baseline = guard.capture(nav, song_ids)
    if baseline['scan'] != index['scan']:
        raise ValueError('Navidrome changed while preparing the rename')
    if {s['path'] for s in baseline['state']['songs'].values()} != set(path_map):
        raise ValueError('Navidrome paths changed while capturing personal references')
    return dict(schema=1, kind='lidarr-album-rename', created=dates.utc(), root=str(root), server=client.url,
        remote_root=remote_root, prefix=prefix, album=album_identity(album), artist={k:artist[k] for k in ('id','path','foreignArtistId')},
        naming=naming, preview=rows, files=files, sidecars=sidecars, baseline=baseline, path_map=path_map,tag_state=repair_state,
        source_folder=folder.relative_to(root).as_posix(), destination_folder=destination.relative_to(root).as_posix(),
        note='Separate path operation. Backups precede the scoped Lidarr command. Verify after a new full Navidrome scan; other users need their own preservation snapshots.')


def write_html(payload, output):
    escape = lambda value:html.escape(str(value), quote=True)
    rows = ['<!doctype html><meta charset="utf-8"><title>Album rename review</title>',
        '<h1>Album rename review</h1><p>'+escape(payload['note'])+'</p><table><tr><th>Current path</th><th>Proposed path</th></tr>']
    rows.extend('<tr><td>'+escape(r['old'])+'</td><td>'+escape(r['new'])+'</td></tr>' for r in payload['files'])
    rows.append('</table><h2>Artwork and other files</h2><table><tr><th>Current path</th><th>Proposed path</th></tr>')
    rows.extend('<tr><td>'+escape(r['path'])+'</td><td>'+escape(r['target'])+'</td></tr>' for r in payload['sidecars'])
    rows.append('</table><p>All selected audio and sidecar files are backed up before the command.</p>')
    output.write_text('\n'.join(rows), encoding='utf8')


def validate_plan(client, nav, root, payload):
    if payload.get('schema') != 1 or payload.get('kind') != 'lidarr-album-rename' or Path(payload['root']).resolve() != root.resolve() or payload['server'] != client.url:
        raise ValueError('rename plan schema/server/root differs')
    if payload['baseline']['state']['server'] != nav.base_url or payload['baseline']['state']['username'] != nav.username:
        raise ValueError('rename baseline belongs to another Navidrome server/account')


def check_files(client, root, payload):
    if album_identity(client.get('album/'+str(payload['album']['id'])))!=payload['album']:
        raise ValueError('album identity changed after the rename preview')
    mapped = {r['id']:r for r in client.get('trackfile', albumId=payload['album']['id'])}
    if set(mapped) != {r['id'] for r in payload['files']}:
        raise ValueError('album file assignments changed')
    for entry in payload['files']:
        path = local_path(root, entry['new'], payload['remote_root'])
        expected = {k:entry[k] for k in ('sha256','size','mtime_ns')}
        if mapped[entry['id']]['path'] != entry['new'] or file_state(path) != expected:
            raise ValueError('rename incomplete or file content/timestamp changed: '+entry['new'])
        if entry['old'] != entry['new'] and local_path(root,entry['old'],payload['remote_root']).exists():
            raise ValueError('old audio path still exists after rename')


def sidecar_locations(root, payload):
    result = []
    for entry in payload['sidecars']:
        target=dates.contained(root,entry['target'])
        if not target.is_file() or file_state(target)!={k:entry[k] for k in ('sha256','size','mtime_ns')}:
            raise ValueError('sidecar missing or changed after rename: '+entry['path'])
        result.append(dict(original=entry['path'], found=entry['target']))
    return result


def move_sidecars(root,payload,journal,record):
    from .duplicates import copy_exclusive
    for entry in payload['sidecars']:
        source,target=(dates.contained(root,entry[key]) for key in ('path','target'))
        expected={k:entry[k] for k in ('sha256','size','mtime_ns')}
        if source==target:
            continue
        if target.exists():
            if file_state(target)!=expected:
                raise ValueError('sidecar destination changed; refusing overwrite')
        else:
            if not source.is_file() or file_state(source)!=expected:
                raise ValueError('sidecar missing or changed after rename: '+entry['path'])
            record.update(active_sidecar=entry['path']);save(journal,record)
            copy_exclusive(source,target,entry['sha256'])
        # Either Lidarr moved the tracked extra, or our exact copy completed.
        if source.exists():
            if file_state(source)!=expected or file_state(target)!=expected:
                raise ValueError('sidecar changed during move; both copies retained')
            source.unlink()
        record.setdefault('moved_sidecars',[])
        if entry['path'] not in record['moved_sidecars']:
            record['moved_sidecars'].append(entry['path'])
        record.pop('active_sidecar',None);save(journal,record)


def apply(client, nav, root, payload, journal):
    validate_plan(client,nav,root,payload)
    with library_lock(root):
        signature = hash_json(payload)
        if journal.exists():
            record = json.loads(journal.read_text(encoding='utf8'))
            if record.get('plan') != signature:
                raise ValueError('rename journal belongs to another plan')
            if record.get('rollback'):
                raise ValueError('rollback has started; resume the rollback journal instead')
            if record.get('status')=='verified':
                check_files(client,root,payload);sidecar_locations(root,payload)
                print('Rename and preservation check already completed. No command was repeated.')
                return 0
            if record.get('command'):
                command = client.get('command/'+str(record['command']))
                if command.get('status','').casefold() in ('queued','started'):
                    print('The original rename command is still queued/running. No command was repeated.')
                    return 0
            # Unknown POST results can be reconciled by exact final assignments.
            # An incomplete/failed command is never blindly issued again.
        else:
            fresh = plan(client,nav,root,payload['album']['id'],payload['remote_root'],payload['prefix'])
            # Capture freshness includes all visible playlist references; listening
            # or a new scan requires a fresh preview rather than weakening evidence.
            for key in payload:
                actual, expected = fresh.get(key), payload[key]
                if key == 'baseline':
                    actual = {k:v for k,v in actual.items() if k != 'created'}
                    expected = {k:v for k,v in expected.items() if k != 'created'}
                if key != 'created' and actual != expected:
                    raise ValueError('rename preview became stale: '+key)
            backup_dir = journal.with_suffix(journal.suffix+'.backups')
            backup_dir.mkdir(parents=True,exist_ok=False)
            records = [(local_path(root,r['old'],payload['remote_root']),r) for r in payload['files']]
            records += [(dates.contained(root,r['path']),r) for r in payload['sidecars']]
            for number,(source,entry) in enumerate(records):
                target = backup_dir/str(number)
                shutil.copy2(source,target)
                if dates.digest(target) != entry['sha256'] or file_state(source) != {k:entry[k] for k in ('sha256','size','mtime_ns')}:
                    raise ValueError('file changed while backing up; no rename command submitted')
            record = dict(plan=signature,status='prepared',started=dates.utc(),backups=str(backup_dir.resolve()),
                originals=[dict(path=source.relative_to(root).as_posix(),backup=str((backup_dir/str(number)).resolve()),
                    **{k:entry[k] for k in ('sha256','size','mtime_ns')}) for number,(source,entry) in enumerate(records)])
            save(journal,record,exclusive=True)
            # Recheck service settings and paths after potentially lengthy backup.
            if settings(client) != payload['naming'] or previews(client,payload['artist']['id'],payload['album']['id']) != payload['preview'] or guard.scan_identity(nav) != payload['baseline']['scan'] or guard.user_snapshot(nav,payload['baseline']['state']['songs']) != payload['baseline']['state']:
                raise ValueError('service state changed during backup; no command submitted')
            if tag_state(root)!=payload.get('tag_state',{}):
                raise ValueError('tag repair state changed during backup; regenerate the rename preview')
            for source,entry in records:
                if file_state(source) != {k:entry[k] for k in ('sha256','size','mtime_ns')}:
                    raise ValueError('file changed after backup; no command submitted')
            folder=dates.contained(root,payload['source_folder'])
            expected_paths={source.resolve() for source,_ in records}
            if {p.resolve() for p in folder.rglob('*') if p.is_file()}!=expected_paths:
                raise ValueError('album contents changed during backup; no command submitted')
            destination=dates.contained(root,payload['destination_folder'])
            if destination!=folder and destination.exists():
                raise ValueError('destination appeared during backup; no command submitted')
            for entry in payload['files']:
                if entry['old']!=entry['new'] and local_path(root,entry['new'],payload['remote_root']).exists():
                    raise ValueError('audio destination appeared during backup; no command submitted')
            for entry in payload['sidecars']:
                if entry['path']!=entry['target'] and dates.contained(root,entry['target']).exists():
                    raise ValueError('sidecar destination appeared during backup; no command submitted')
            record.update(status='submitting');save(journal,record)
            try:
                command = client.post('command',dict(name='RenameFiles',artistId=payload['artist']['id'],files=[r['trackFileId'] for r in payload['preview']]))
                if not isinstance(command.get('id'),int):
                    raise ApiError('rename command outcome unknown: missing command ID')
                record.update(status='submitted',command=command['id']);save(journal,record)
            except ApiError:
                record.update(status='outcome_unknown');save(journal,record)
                raise
            command = client.get('command/'+str(record['command']))
            if command.get('status','').casefold() in ('queued','started'):
                print('Rename queued. Rerun apply with the same plan/journal to check it; no new command is needed.')
                return 0
        try:
            check_files(client,root,payload)
            move_sidecars(root,payload,journal,record)
            locations = sidecar_locations(root,payload)
        except (ValueError,OSError) as exc:
            record.update(status='needs_review',error=str(exc));save(journal,record)
            raise
        record.update(status='awaiting_full_scan',sidecars=locations,updated=dates.utc());save(journal,record)
        print('Lidarr paths, file bytes, timestamps and sidecars verified. Complete a full Navidrome scan, then run verify.')
        return 0


def verify(client, nav, root, payload, journal):
    validate_plan(client,nav,root,payload)
    record = json.loads(journal.read_text(encoding='utf8'))
    if record.get('rollback'):
        raise ValueError('rollback has started; verify the rollback journal instead')
    if record.get('plan') != hash_json(payload) or record.get('status') not in ('awaiting_full_scan','verified','preservation_failed'):
        raise ValueError('reconcile the rename command before verifying the Navidrome scan')
    check_files(client,root,payload)
    sidecar_locations(root,payload)
    result = guard.verify(nav,payload['baseline'],payload['path_map'])
    record.update(status='verified' if result['preserved'] else 'preservation_failed',verification=result);save(journal,record)
    print(json.dumps(result));return 0 if result['preserved'] else 1


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,default=Path('.env'))
    parser.add_argument('--library-root',type=Path,required=True)
    sub=parser.add_subparsers(dest='action',required=True)
    preview=sub.add_parser('plan');preview.add_argument('--album-id',type=int,required=True);preview.add_argument('--remote-root',default='/music');preview.add_argument('--navidrome-prefix',default='');preview.add_argument('--output',type=Path,required=True)
    reverse=sub.add_parser('rollback-plan');reverse.add_argument('--plan',type=Path,required=True);reverse.add_argument('--journal',type=Path,required=True);reverse.add_argument('--output',type=Path,required=True)
    for name in ('apply','verify','rollback','rollback-verify'):
        action=sub.add_parser(name);action.add_argument('--plan',type=Path,required=True);action.add_argument('--journal',type=Path,required=True)
    args=parser.parse_args(argv);load_dotenv(args.env_file)
    try:
        client,nav=lidarr_from_env(),nav_from_env();root=args.library_root.resolve(strict=True)
        if args.action=='plan':
            payload=plan(client,nav,root,args.album_id,args.remote_root,args.navidrome_prefix)
            save(args.output,payload,exclusive=True);write_html(payload,args.output.with_suffix('.html'));print('Review '+str(args.output.with_suffix('.html')));return 0
        payload=json.loads(args.plan.read_text(encoding='utf8'))
        if args.action.startswith('rollback'):
            from . import rename_rollback
            if args.action=='rollback-plan':
                reverse=rename_rollback.plan(client,nav,root,payload,args.journal)
                save(args.output,reverse,exclusive=True)
                review=dict(note='Restore reviewed original paths and reconcile Lidarr with one scoped rescan. Stop other writers. Backups remain intact.',
                    files=[dict(old=row['new'],new=row['old']) for row in reverse['entries']],sidecars=[])
                write_html(review,args.output.with_suffix('.html'))
                print('Review '+str(args.output.with_suffix('.html')));return 0
            return (rename_rollback.apply if args.action=='rollback' else rename_rollback.verify)(client,nav,root,payload,args.journal)
        return (apply if args.action=='apply' else verify)(client,nav,root,payload,args.journal)
    except (ValueError,OSError,ApiError) as exc:
        parser.exit(1,str(exc)+'\n')


if __name__=='__main__':
    raise SystemExit(main())
