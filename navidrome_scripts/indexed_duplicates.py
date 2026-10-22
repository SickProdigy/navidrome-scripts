"""Review indexed copies using official Navidrome backups and missing-file remap.

SQLite is opened only on completed, uniquely created backup files, never the
service database. All users' references are checked, including private playlists.
"""
import json
import html
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from datetime import datetime

from . import dates, duplicates, navidrome_guard as guard, renames
from .locking import library_lock
from .metadata import identify
from .playlists import hash_json, normalized, save


class NativeCLI:
    def __init__(self, command, backups, native_backups=None):
        if not isinstance(command, list) or not command or any(not isinstance(v, str) or not v for v in command):
            raise ValueError('native command must be a nonempty JSON argument array')
        self.command = command
        self.backups = Path(backups).resolve()
        self.native_backups = str(native_backups or self.backups)

    def run(self, arguments, log):
        log.parent.mkdir(mode=0o700,parents=True, exist_ok=True)
        with log.open('a', encoding='utf8') as stream:
            try:
                result = subprocess.run(self.command+arguments, stdin=subprocess.DEVNULL, stdout=stream,
                                        stderr=stream, timeout=1200, shell=False,
                                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            except subprocess.TimeoutExpired:
                raise ValueError('native command timed out; outcome unknown. Resume the same journal to reconcile; do not repeat the remap manually.') from None
        if result.returncode:
            raise ValueError('Navidrome native command failed; inspect '+str(log))

    def backup(self, label):
        directory = self.backups/(label+'-'+uuid.uuid4().hex)
        directory.mkdir(mode=0o700,parents=True, exist_ok=False)
        remote = str(PurePosixPath(self.native_backups)/directory.name)
        self.run(['backup','create','--backup-dir',remote], directory/'native.log')
        files = list(directory.glob('navidrome_backup_*.db'))
        if len(files)!=1 or files[0].is_symlink() or any(Path(str(files[0])+suffix).exists() for suffix in ('-wal','-shm')):
            raise ValueError('one completed native backup is required; verify local/native backup-directory mapping')
        return dict(path=str(files[0]), native_path=str(PurePosixPath(remote)/files[0].name),sha256=dates.digest(files[0]))


def history(backup, identities):
    """Inspect a completed offline backup through a local immutable copy."""
    source = Path(backup['path'])
    if source.is_symlink() or dates.digest(source)!=backup['sha256']:
        raise ValueError('native backup changed')
    with tempfile.TemporaryDirectory(prefix='navidrome-backup-review-') as directory:
        local = Path(directory)/'snapshot.db';shutil.copyfile(source,local)
        if dates.digest(local)!=backup['sha256'] or dates.digest(source)!=backup['sha256']:
            raise ValueError('native backup changed during copy')
        db = sqlite3.connect(local.as_uri()+'?mode=ro&immutable=1',uri=True)
        db.row_factory = sqlite3.Row
        try:
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise ValueError('native backup integrity check failed')
            marks = ','.join('?' for _ in identities)
            def rows(sql, args=()):
                return [dict(row) for row in db.execute(sql,args)]
            media = rows('SELECT id,path,library_id,album_id,size,missing,created_at FROM media_file WHERE id IN ('+marks+') ORDER BY id',identities)
            annotations = rows('SELECT * FROM annotation WHERE item_type=? AND item_id IN ('+marks+') ORDER BY user_id,item_id', ['media_file',*identities])
            bookmarks = rows('SELECT * FROM bookmark WHERE item_type=? AND item_id IN ('+marks+') ORDER BY user_id,item_id', ['media_file',*identities])
            playlists = rows('SELECT * FROM playlist WHERE id IN (SELECT playlist_id FROM playlist_tracks WHERE media_file_id IN ('+marks+')) ORDER BY id',identities)
            # Derived counts/timestamps may legitimately be refreshed by a scan.
            for row in playlists:
                for key in ('duration','song_count','size','updated_at','evaluated_at','average_rating'):
                    row.pop(key,None)
            playlist_ids = [row['id'] for row in playlists]
            entries = rows('SELECT * FROM playlist_tracks WHERE playlist_id IN ('+','.join('?' for _ in playlist_ids)+') ORDER BY id',playlist_ids) if playlist_ids else []
            scrobbles = rows('SELECT * FROM scrobbles WHERE media_file_id IN ('+marks+') ORDER BY id',identities)
            buffered = rows('SELECT * FROM scrobble_buffer WHERE media_file_id IN ('+marks+') ORDER BY user_id,service,play_time,media_file_id',identities)
            users = rows('SELECT id,user_name,is_admin FROM user ORDER BY id')
            access = rows('SELECT user_id,library_id FROM user_library ORDER BY user_id,library_id')
            return dict(media=media,users=users,annotations=annotations,bookmarks=bookmarks,
                        playlists=playlists,entries=entries,scrobbles=scrobbles,buffered=buffered,access=access)
        except sqlite3.Error as exc:
            raise ValueError('unsupported native backup schema: '+str(exc)) from None
        finally:
            db.close()


def conflict_check(snapshot):
    # Even a zero-valued annotation row conflicts: upstream retains the source
    # row rather than merging its values with the target user's row.
    for table in ('annotations','bookmarks'):
        owners = [row['user_id'] for row in snapshot[table]]
        if len(owners)!=len(set(owners)):
            raise ValueError('both copies have '+table+' for the same user; explicit history-conflict review required')
    keys = [(r['user_id'],r['service'],r['play_time']) for r in snapshot['buffered']]
    if len(keys)!=len(set(keys)):
        raise ValueError('overlapping buffered scrobbles require separate review')
    if any(row.get('path') or row.get('rules') for row in snapshot['playlists']):
        raise ValueError('file-backed or smart playlist references require source review before remap')


def references(snapshot):
    return {key:value for key,value in snapshot.items() if key!='media'}


def access_check(snapshot,keep_id,drop_id):
    rows={row['id']:row for row in snapshot['media']}
    target,source=(rows[identity]['library_id'] for identity in (keep_id,drop_id))
    for user in snapshot['users']:
        granted={row['library_id'] for row in snapshot['access'] if row['user_id']==user['id']}
        if not user['is_admin'] and source in granted and target not in granted:
            raise ValueError('cross-library remap would remove a user’s access; reconcile library permissions first')


def merged(snapshot, retained):
    result = json.loads(json.dumps(references(snapshot)))
    for table,field in (('annotations','item_id'),('bookmarks','item_id'),('entries','media_file_id'),
                        ('scrobbles','media_file_id'),('buffered','media_file_id')):
        selected = {row['id'] for row in snapshot['media']}
        for row in result[table]:
            if row[field] in selected:
                row[field] = retained
    for key in result:
        result[key] = sorted(result[key],key=hash_json)
    return result


def canonical_refs(snapshot):
    return {key:sorted(value,key=hash_json) for key,value in references(snapshot).items()}


def native_file(nav, identity):
    row = nav._filesystem_row(nav.native_get('song/'+str(identity)),require_present=False)
    if str(row['id'])!=str(identity):raise ValueError('native song identity differs')
    return row


def paths(payload):
    result = {}
    for role,row in payload['songs'].items():
        configured = Path(payload['roots'].get(str(row['libraryId']),''))
        if str(row['libraryId']) not in payload['roots']:
            raise ValueError('an explicit local root is required for each selected Navidrome library')
        root = configured.resolve(strict=True)
        if root!=configured or not root.is_dir():raise ValueError('configured library root changed or is not a directory')
        result[role] = dates.contained(root,row['path'])
    return result


def validate_files(payload, allow_missing=False):
    result = paths(payload)
    for role,path in result.items():
        if role=='drop' and allow_missing and not path.exists():continue
        if renames.file_state(path)!=payload['files'][role]:
            raise ValueError('indexed duplicate file changed: '+role)
    return result


def lidarr_assignments(client,payload):
    local = paths(payload);root = Path(payload['root'])
    try:
        keep,drop = (local[role].relative_to(root).as_posix() for role in ('keep','drop'))
    except ValueError:
        raise ValueError('both copies need an explicit mapping inside the configured Lidarr library root') from None
    return duplicates.assignments(client,root,keep,[drop],payload['remote_root'])


def plan(client,nav,root,roots,keep_id,drop_id,quarantine,cli,allow_alternate=False,remote_root='/music'):
    if keep_id==drop_id:raise ValueError('choose two different indexed identities')
    if nav.request('getUser',dict(username=nav.username)).get('user',{}).get('adminRole') is not True:
        raise ValueError('indexed cleanup requires administrator access')
    scan = guard.scan_identity(nav)
    if scan.get('scanType')!='full':raise ValueError('complete a full Navidrome scan before duplicate review')
    songs = {role:native_file(nav,identity) for role,identity in (('keep',keep_id),('drop',drop_id))}
    if any(row['missing'] for row in songs.values()):raise ValueError('both selected copies must still exist before quarantine review')
    if songs['keep']['albumId']!=songs['drop']['albumId']:
        raise ValueError('different album identities/editions require separate history review')
    payload = dict(schema=1,kind='indexed-duplicate-cleanup',server=client.url,nav_server=nav.base_url,username=nav.username,
                   root=str(root.resolve()),roots={str(key):str(Path(value).resolve(strict=True)) for key,value in roots.items()},
                   remote_root=remote_root,songs=songs,scan=scan,created=dates.utc(),files={},alternate=allow_alternate,
                   native_command=cli.command,backups=str(cli.backups),native_backups=cli.native_backups,
                   quarantine=str(quarantine.resolve()))
    local = paths(payload)
    if guard.path_key(str(local['keep']))==guard.path_key(str(local['drop'])):
        raise ValueError('selected identities alias the same local path')
    for protected in [root,*map(Path,payload['roots'].values())]:
        if quarantine.resolve().is_relative_to(protected.resolve()) or cli.backups.is_relative_to(protected.resolve()):
            raise ValueError('quarantine and native backups must be outside every music root')
    for role,path in local.items():
        payload['files'][role]=renames.file_state(path)
        if songs[role]['size']!=payload['files'][role]['size']:
            raise ValueError('native indexed file size differs from the local mapping')
    tags = {role:identify(path) for role,path in local.items()}
    for role,row in songs.items():
        if (row.get('musicBrainzId')!=tags[role].get('recording_mbid')
                or any(normalized(row.get(key,''))!=normalized(tags[role].get(key,'')) for key in ('title','artist','album'))
                or abs(float(row.get('duration') or 0)*1000-tags[role]['duration_ms'])>2500):
            raise ValueError('indexed metadata differs from the selected file; scan/reconcile first')
    for key in ('release_mbid','group_mbid','recording_mbid','release_track_mbid','album_artist_mbid','title','album','artist','tracknumber','discnumber'):
        if not tags['keep'].get(key) or tags['keep'].get(key)!=tags['drop'].get(key):
            raise ValueError('copies lack one exact release/recording/position identity: '+key)
    if abs(tags['keep']['duration_ms']-tags['drop']['duration_ms'])>2500:
        raise ValueError('copy durations differ; alternate master review required')
    if payload['files']['keep']['sha256']!=payload['files']['drop']['sha256'] and not allow_alternate:
        raise ValueError('different bytes require explicit alternate-encode review')
    duplicates.sidecar_guard(root,local['keep'].relative_to(root).as_posix(),local['drop'].relative_to(root).as_posix())
    payload['lidarr']=lidarr_assignments(client,payload)
    cli.run(['missing','fix','--help'],cli.backups/'capabilities.log')
    payload['backup']=cli.backup('before-review')
    snapshot=history(payload['backup'],[keep_id,drop_id])
    if len(snapshot['media'])!=2:raise ValueError('native backup belongs to another instance or changed identities')
    for row in snapshot['media']:
        native = next(value for value in songs.values() if str(value['id'])==row['id'])
        if any(row[key]!=native[other] for key,other in (('path','path'),('library_id','libraryId'),('album_id','albumId'),('size','size'))):
            raise ValueError('native backup and API file mappings differ')
    conflict_check(snapshot)
    access_check(snapshot,keep_id,drop_id)
    if guard.scan_identity(nav)!=scan:raise ValueError('Navidrome scanned during duplicate review')
    payload['history']=snapshot
    payload['retained_id']=drop_id
    payload['note']='Keep the chosen audio file; retain the quarantined copy’s Navidrome ID and reassign both copies’ references through the official remap. Conflicting user rows require manual review.'
    return payload


def validate(client,nav,root,payload,cli):
    if (payload.get('kind')!='indexed-duplicate-cleanup' or payload.get('schema')!=1 or payload['server']!=client.url
            or payload['nav_server']!=nav.base_url or payload['username']!=nav.username or Path(payload['root']).resolve()!=root.resolve()
            or payload['native_command']!=cli.command or payload['backups']!=str(cli.backups) or payload['native_backups']!=cli.native_backups):
        raise ValueError('indexed plan server/root/native-command differs')
    directory=Path(payload['quarantine'])
    if directory.resolve()!=directory:raise ValueError('quarantine path changed through a symlink')


def write_html(payload,output):
    escape=lambda value:html.escape(str(value),quote=True)
    lines=['<!doctype html><meta charset="utf-8"><title>Indexed duplicate review</title>',
           '<h1>Indexed duplicate review</h1><p>'+escape(payload['note'])+'</p>',
           '<table><tr><th>Audio choice</th><th>Library</th><th>Current path</th><th>Current ID</th></tr>']
    for role in ('keep','drop'):
        row=payload['songs'][role]
        lines.append('<tr>'+''.join('<td>'+escape(value)+'</td>' for value in
                     ('Keep audio' if role=='keep' else 'Quarantine audio',row['libraryId'],row['path'],row['id']))+'</tr>')
    lines.extend(['</table><p>Retained Navidrome ID: '+escape(payload['retained_id'])+'</p>',
                  '<p>All-user backup: '+escape(payload['backup']['path'])+'</p>',
                  '<p>Quarantine: '+escape(payload['quarantine'])+'</p>',
                  '<p>Overlapping annotation/bookmark rows are refused. Private playlist order and repeated entries are checked.</p>',
                  '<p>This plan and the database backups contain private account data. Keep them outside the music library and source control.</p>'])
    output.write_text('\n'.join(lines),encoding='utf8')


def keeper_assignment(client,payload):
    original=payload['lidarr']['keeper']
    row=client.get('trackfile/'+str(original['id']))
    if any(row.get(key)!=value for key,value in original.items()) or row.get('size')!=payload['files']['keep']['size']:
        raise ValueError('Lidarr keeper assignment changed; cleanup stopped')


def newer_full_scan(scan, finished):
    try:
        scanned=datetime.fromisoformat(str(scan['lastScan']).replace('Z','+00:00'))
        completed=datetime.fromisoformat(finished.replace('Z','+00:00'))
        return scan.get('scanType')=='full' and scanned>completed
    except (KeyError,ValueError,TypeError):
        return False


def apply(client,nav,root,payload,journal,cli):
    validate(client,nav,root,payload,cli)
    keep_id,drop_id=(payload['songs'][role]['id'] for role in ('keep','drop'))
    with library_lock(root):
        record=json.loads(journal.read_text(encoding='utf8')) if journal.exists() else {}
        if record and record.get('plan')!=hash_json(payload):raise ValueError('indexed cleanup journal belongs to another plan')
        if record.get('recovery_audio_restored'):raise ValueError('offline recovery has started; do not replay cleanup')
        if record.get('status') in ('awaiting_verification','verified'):
            validate_files(payload,allow_missing=True);keeper_assignment(client,payload)
            if paths(payload)['drop'].exists():raise ValueError('quarantined source path reappeared')
            return 0
        if not record:
            local=validate_files(payload)
            if guard.scan_identity(nav)!=payload['scan'] or lidarr_assignments(client,payload)!=payload['lidarr']:
                raise ValueError('service state changed since duplicate review')
            fresh=cli.backup('before-quarantine')
            if history(fresh,[keep_id,drop_id])!=payload['history']:
                raise ValueError('private history changed since duplicate review; preview again')
            directory=Path(payload['quarantine']);directory.mkdir(mode=0o700,parents=True,exist_ok=False)
            record=dict(plan=hash_json(payload),status='copying',backup=fresh,quarantine=str(directory/'original-audio'),started=dates.utc())
            save(journal,record,exclusive=True)
        if record['status'] in ('copying','quarantining'):
            local=validate_files(payload,allow_missing=record['status']=='quarantining');saved=Path(record['quarantine'])
            if not saved.exists():duplicates.copy_exclusive(local['drop'],saved,payload['files']['drop']['sha256'])
            if renames.file_state(saved)!=payload['files']['drop']:
                raise ValueError('quarantine copy changed; originals retained')
            if guard.scan_identity(nav)!=payload['scan'] or (local['drop'].exists() and lidarr_assignments(client,payload)!=payload['lidarr']):
                raise ValueError('services changed during quarantine; both copies retained')
            record['status']='quarantining';save(journal,record)
            if local['drop'].exists():local['drop'].unlink()
            record.update(status='awaiting_scan',quarantined_at=dates.utc());save(journal,record)
        if record['status']=='awaiting_scan':
            scan=guard.scan_identity(nav)
            if not newer_full_scan(scan,record['quarantined_at']):
                print('Copy quarantined. Complete a new full Navidrome scan, then repeat this command.');return 2
            local=validate_files(payload,allow_missing=True)
            if local['drop'].exists():raise ValueError('quarantined path reappeared; review required')
            keeper_assignment(client,payload)
            drop=native_file(nav,drop_id);keep=native_file(nav,keep_id)
            if not drop['missing'] or keep['missing'] or any(drop[k]!=payload['songs']['drop'][k] or keep[k]!=payload['songs']['keep'][k] for k in ('path','libraryId')):
                raise ValueError('scan changed duplicate identities; reconcile the native remap manually from the backup')
            fresh=cli.backup('before-remap')
            if canonical_refs(history(fresh,[keep_id,drop_id]))!=canonical_refs(payload['history']):
                raise ValueError('private references changed after quarantine; remap stopped for review')
            if guard.scan_identity(nav)!=scan:raise ValueError('Navidrome scanned during pre-remap backup; review required')
            record.update(status='remapping',before_remap=fresh,scan=scan);save(journal,record)
            cli.run(['missing','fix',drop_id,keep_id],Path(record['quarantine']).parent/'remap.log')
        # A crash/timeout after submission is reconciled by a new complete backup.
        # Never repeat the irreversible native remap blindly.
        after=cli.backup('after-remap');snapshot=history(after,[keep_id,drop_id])
        if canonical_refs(snapshot)!=merged(payload['history'],drop_id):
            raise ValueError('native remap references differ; retain backups and review offline recovery')
        if (len(snapshot['media'])!=1 or snapshot['media'][0]['id']!=drop_id
                or any(snapshot['media'][0][key]!=payload['songs']['keep'][other] for key,other in (('path','path'),('library_id','libraryId'),('size','size')))
                or snapshot['media'][0]['missing']):
            raise ValueError('native remap outcome unknown; inspect backup, no command repeated')
        record.update(status='awaiting_verification',after_remap=after,remapped_at=dates.utc());save(journal,record)
        print('Indexed references remapped. Complete a new full Navidrome scan, then run indexed-verify.')
        return 0


def verify(client,nav,root,payload,journal,cli):
    validate(client,nav,root,payload,cli)
    record=json.loads(journal.read_text(encoding='utf8'))
    if record.get('recovery_audio_restored'):raise ValueError('offline recovery has started; do not verify cleanup')
    if record.get('plan')!=hash_json(payload) or record.get('status') not in ('awaiting_verification','verified'):
        raise ValueError('reconcile the indexed cleanup journal before verification')
    scan=guard.scan_identity(nav)
    if not newer_full_scan(scan,record['remapped_at']):
        raise ValueError('complete a new full scan after native remap')
    validate_files(payload,allow_missing=True)
    if paths(payload)['drop'].exists():raise ValueError('quarantined source path reappeared')
    keeper_assignment(client,payload)
    ids=[payload['songs'][role]['id'] for role in ('keep','drop')]
    backup=cli.backup('verified');snapshot=history(backup,ids)
    if canonical_refs(snapshot)!=merged(payload['history'],payload['retained_id']):
        raise ValueError('private ratings/history/playlists changed; cleanup remains unresolved')
    row=native_file(nav,payload['retained_id'])
    if row['missing'] or any(row[key]!=payload['songs']['keep'][key] for key in ('path','libraryId','size')) or len(snapshot['media'])!=1:
        raise ValueError('retained identity is missing or remap incomplete')
    record.update(status='verified',verification_backup=backup,ended=dates.utc());save(journal,record)
    print('Indexed cleanup verified for all users, including private playlist references. Backups and quarantined audio retained.')
    return 0


def restore_files(client,nav,root,payload,journal,cli):
    validate(client,nav,root,payload,cli)
    record=json.loads(journal.read_text(encoding='utf8'))
    if record.get('plan')!=hash_json(payload):raise ValueError('indexed cleanup journal belongs to another plan')
    with library_lock(root):
        target=paths(payload)['drop'];saved=Path(record['quarantine'])
        if renames.file_state(saved)!=payload['files']['drop']:raise ValueError('quarantined audio changed')
        if target.exists():
            if renames.file_state(target)!=payload['files']['drop']:raise ValueError('original path now contains another file; no overwrite allowed')
        else:
            duplicates.copy_exclusive(saved,target,payload['files']['drop']['sha256'])
        if renames.file_state(target)!=payload['files']['drop']:raise ValueError('restored audio differs')
        record['recovery_audio_restored']=dates.utc();save(journal,record)
    print('Audio restored; quarantine retained. Database recovery is a separate offline operation: stop Navidrome first and review changes since the backup.')
    print(json.dumps(cli.command+['backup','restore','--backup-file',record['backup']['native_path']]))
