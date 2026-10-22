"""Reviewed, one-album Lidarr metadata refresh without tag writes or searches."""
import json
import html
from pathlib import Path
import shutil

import requests

from . import dates
from .catalog import MusicBrainzClient
from .date_evidence import artist_ids,partial_date
from .locking import library_lock,tag_state
from .playlists import hash_json,save
from .subsonic import ApiError
from .unmapped import album_identity,local_path


def check_settings(client):
    if str(client.get('config/metadataprovider').get('writeAudioTags','')).casefold()!='no' or str(client.get('config/mediamanagement').get('fileDate','')).casefold()!='none':
        raise ValueError('metadata reconciliation requires Lidarr Tag Audio Files=Never and File Date=None')


def files(client,root,album,remote_root):
    rows=client.get('trackfile',albumId=album)
    if len({r['id'] for r in rows})!=len(rows) or any(r.get('albumId')!=album for r in rows):
        raise ValueError('inconsistent mapped album files')
    result=[]
    for row in sorted(rows,key=lambda r:r['id']):
        path=local_path(root,row['path'],remote_root);stat=path.stat()
        if stat.st_nlink>1:
            raise ValueError('hardlinked audio requires separate refresh review')
        result.append(dict(id=row['id'],path=row['path'],sha256=dates.digest(path),mtime_ns=stat.st_mtime_ns,size=stat.st_size))
    return result


def source_details(payload,album,artist,release):
    group=payload.get('release-group') or {}
    if payload.get('id')!=release or group.get('id')!=album['foreignAlbumId']:
        raise ValueError('exact MusicBrainz release/group differs from the mapped edition')
    credits=artist_ids(payload.get('artist-credit',[]));group_credits=artist_ids(group.get('artist-credit',[]))
    if not credits or credits!=group_credits or artist['foreignArtistId'] not in credits:
        raise ValueError('catalog release/group artist conflicts with the existing Lidarr artist; no refresh allowed')
    return dict(release_mbid=release,group_mbid=group['id'],artists=credits,title=payload.get('title'),
        edition_date=payload.get('date') or '',original_date=group.get('first-release-date') or '')


def sidecars(root,mapped,artist,remote_root):
    if not mapped:
        return []
    from .renames import folder_for
    paths=[local_path(root,r['path'],remote_root) for r in mapped]
    folder=folder_for(paths);artist_folder=local_path(root,artist['path'],remote_root)
    if folder==artist_folder or not folder.is_relative_to(artist_folder):
        raise ValueError('album files span artist-level/mixed folders; review refresh separately')
    result=[]
    for path in sorted(folder.rglob('*')):
        if path.is_symlink() or getattr(path,'is_junction',lambda:False)():
            raise ValueError('album sidecar path uses a symlink/junction')
        if path.is_file() and path.suffix.lower() not in dates.AUDIO:
            stat=path.stat()
            result.append(dict(path=remote_root.rstrip('/')+'/'+path.relative_to(root).as_posix(),sha256=dates.digest(path),mtime_ns=stat.st_mtime_ns,size=stat.st_size))
    return result


def plan(client,root,album_id,remote_root='/music',catalog=None):
    root=root.resolve(strict=True)
    album=client.get('album/'+str(album_id));artist=client.get('artist/'+str(album['artistId']))
    if album.get('id')!=album_id or artist.get('id')!=album['artistId']:
        raise ValueError('Lidarr did not return the requested album/artist')
    payload=dict(schema=1,kind='lidarr-metadata-refresh',created=dates.utc(),server=client.url,root=str(root),remote_root=remote_root,
        album=album_identity(album),artist={k:artist[k] for k in ('id','foreignArtistId','path')},current_date=album.get('releaseDate'),
        title=album.get('title'),artist_name=artist.get('artistName') or artist['path'].rstrip('/').rsplit('/',1)[-1],
        files=[],sidecars=[],status='review',notes=[])
    try:
        check_settings(client)
        if tag_state(root).get('status')=='writing':
            raise ValueError('finish or roll back the interrupted tag repair before refreshing metadata')
        payload['files']=files(client,root,album_id,remote_root)
        payload['sidecars']=sidecars(root,payload['files'],artist,remote_root)
        selected=[r for r in album.get('releases',[]) if r.get('monitored')]
        if len(selected)!=1 or not selected[0].get('foreignReleaseId'):
            raise ValueError('one exact monitored release edition is required')
        release=selected[0]['foreignReleaseId']
        source=(catalog or MusicBrainzClient()).lookup_release(release)
        payload['catalog']=source_details(source,album,artist,release)
        lookup=client.get('album/lookup',term='lidarr:'+album['foreignAlbumId'])
        matches=[r for r in lookup if r.get('foreignAlbumId')==album['foreignAlbumId']]
        if len(matches)!=1:
            raise ValueError('upstream Lidarr metadata does not identify one existing album')
        candidate=matches[0]
        if (candidate.get('artist') or {}).get('foreignArtistId')!=artist['foreignArtistId']:
            raise ValueError('Lidarr lookup would reassign this album to another artist; no refresh allowed')
        if not any(r.get('foreignReleaseId')==release for r in candidate.get('releases',[])):
            raise ValueError('upstream Lidarr metadata no longer includes the selected edition')
        payload['proposed']=dict(title=candidate.get('title'),date=candidate.get('releaseDate'),artist_mbid=artist['foreignArtistId'])
        payload['status']='ready'
        try:
            partial_date(payload['catalog']['original_date'])
        except ValueError:
            payload['notes'].append('MusicBrainz first-release date remains unknown; refresh cannot manufacture a date. Review the existing catalog entry.')
    except (ValueError,OSError,ApiError,requests.RequestException) as exc:
        payload['notes'].append(str(exc) if not isinstance(exc,requests.RequestException) else 'catalog lookup failed: '+type(exc).__name__)
    return payload


def write_html(payload,output):
    escape=lambda value:html.escape(str(value),quote=True)
    proposed=payload.get('proposed',{});catalog=payload.get('catalog',{})
    parts=['<!doctype html><meta charset="utf-8"><title>Album metadata refresh</title>',
        '<h1>'+escape(payload['artist_name']+' — '+str(payload['title']))+'</h1>',
        '<p>Status: '+escape(payload['status'])+'</p>',
        '<p>Requested operation: refresh this existing album only. No searches, file tag writes or renames are requested.</p>',
        '<table><tr><th>Current Lidarr date</th><th>Upstream Lidarr date</th><th>MusicBrainz first-release date</th><th>Exact edition date</th></tr>',
        '<tr>'+''.join('<td>'+escape(v or '(unknown)')+'</td>' for v in (payload['current_date'],proposed.get('date'),catalog.get('original_date'),catalog.get('edition_date')))+'</tr></table>',
        '<p>'+str(len(payload['files']))+' mapped audio files and '+str(len(payload['sidecars']))+' sidecars will be backed up and checked.</p>']
    parts.extend('<p>'+escape(note)+'</p>' for note in payload['notes'])
    with output.open('x',encoding='utf8') as stream:
        stream.write('\n'.join(parts))


def same_plan(first,second):
    return {k:v for k,v in first.items() if k!='created'}=={k:v for k,v in second.items() if k!='created'}


def matching_commands(rows,album_id):
    if not isinstance(rows,list) or any(not isinstance(row,dict) or type(row.get('id')) is not int or not isinstance(row.get('body') or {},dict) for row in rows):
        raise ApiError('Lidarr command queue returned invalid rows')
    return [row for row in rows if (row.get('name') or '').casefold()=='refreshalbum' and
        (row.get('body') or {}).get('albumId')==album_id and not (row.get('body') or {}).get('isNewAlbum',False)]


def reconcile(client,root,payload,journal,catalog=None):
    catalog=catalog or MusicBrainzClient()
    if payload.get('schema')!=1 or payload.get('kind')!='lidarr-metadata-refresh' or payload['server']!=client.url or Path(payload['root']).resolve()!=root.resolve() or payload.get('status')!='ready':
        raise ValueError('refresh plan server/root/status differs or still needs review')
    with library_lock(root):
        signature=hash_json(payload)
        record=json.loads(journal.read_text(encoding='utf8')) if journal.exists() else None
        if record and record.get('plan')!=signature:
            raise ValueError('refresh journal belongs to another plan')
        if record is None:
            fresh=plan(client,root,payload['album']['id'],payload['remote_root'],catalog)
            if not same_plan(payload,fresh):
                raise ValueError('refresh preview became stale; generate a new plan')
            commands=client.get('command')
            matching_commands(commands,payload['album']['id'])  # Validate the queue before filtering.
            if matching_commands([r for r in commands if str(r.get('status','')).casefold() in ('queued','started')],payload['album']['id']):
                raise ValueError('another refresh for this album is already queued/running; inspect it before submitting')
            backups=journal.with_suffix(journal.suffix+'.backups');backups.mkdir(parents=True,exist_ok=False)
            originals=[*payload['files'],*payload['sidecars']]
            for index,row in enumerate(originals):
                backup=backups/str(index);source=local_path(root,row['path'],payload['remote_root'])
                shutil.copy2(source,backup)
                if dates.digest(backup)!=row['sha256']:
                    raise ValueError('refresh backup could not be verified; no command submitted')
            if not same_plan(payload,plan(client,root,payload['album']['id'],payload['remote_root'],catalog)):
                raise ValueError('refresh inputs changed during backup; no command submitted')
            commands=client.get('command')
            matching_commands(commands,payload['album']['id'])
            if matching_commands([r for r in commands if str(r.get('status','')).casefold() in ('queued','started')],payload['album']['id']):
                raise ValueError('another album refresh appeared during backup; no command submitted')
            record=dict(plan=signature,status='submitting',started=dates.utc(),known_commands=[r['id'] for r in commands],backups=str(backups.resolve()),
                originals=[dict(row,backup=str((backups/str(index)).resolve())) for index,row in enumerate(originals)])
            save(journal,record,exclusive=True)
            try:
                command=client.post('command',dict(name='RefreshAlbum',albumId=payload['album']['id'],isNewAlbum=False))
                if type(command.get('id')) is not int:
                    raise ApiError('refresh write outcome unknown: missing command ID')
                record.update(status='submitted',command=command['id']);save(journal,record)
            except ApiError:
                record['status']='outcome_unknown';save(journal,record);raise
        check_settings(client)
        if not record.get('command'):
            new=[r for r in matching_commands(client.get('command'),payload['album']['id']) if r['id'] not in record['known_commands']]
            if len(new)!=1:
                raise ValueError('refresh outcome cannot be uniquely identified; inspect the journal and command queue, no request repeated')
            record['command']=new[0]['id'];save(journal,record)
        command=client.get('command/'+str(record['command']))
        status=str(command.get('status','')).casefold()
        if status in ('queued','started'):
            print('Original album refresh remains queued/running; rerun with the same plan/journal to check it.');return 0
        if status!='completed':
            record.update(status='needs_review',error='original refresh command did not complete');save(journal,record)
            raise ValueError(record['error'])
        current=client.get('album/'+str(payload['album']['id']))
        selected=[r.get('foreignReleaseId') for r in current.get('releases',[]) if r.get('monitored')]
        if current.get('artistId')!=payload['album']['artistId'] or current.get('foreignAlbumId')!=payload['album']['foreignAlbumId'] or selected!=[payload['catalog']['release_mbid']]:
            record.update(status='needs_review',error='artist/group/edition changed during refresh');save(journal,record)
            raise ValueError(record['error'])
        if files(client,root,payload['album']['id'],payload['remote_root'])!=payload['files']:
            record.update(status='needs_review',error='file assignments, bytes or timestamps changed during refresh');save(journal,record)
            raise ValueError(record['error'])
        if sidecars(root,payload['files'],payload['artist'],payload['remote_root'])!=payload['sidecars']:
            record.update(status='needs_review',error='artwork/sidecars changed during refresh');save(journal,record)
            raise ValueError(record['error'])
        latest=catalog.lookup_release(payload['catalog']['release_mbid'])
        latest=source_details(latest,current,payload['artist'],payload['catalog']['release_mbid'])
        if latest!=payload['catalog']:
            record.update(status='needs_review',error='MusicBrainz metadata changed during refresh; preview again');save(journal,record)
            raise ValueError(record['error'])
        try:
            expected=partial_date(latest['original_date'])
            actual=partial_date(str(current.get('releaseDate') or '')[:10])
            aligned=actual[:len(expected)]==expected
        except ValueError:
            aligned=False
        record.update(status='reconciled' if aligned else 'needs_source_review',ended=dates.utc(),current_date=current.get('releaseDate'),
            source_date=latest['original_date'],files_verified=True);save(journal,record)
        print('Album metadata refreshed; files preserved. '+('Generate a fresh tag/import/rename preview.' if aligned else 'The source date is missing or still differs; review MusicBrainz and wait for upstream metadata propagation.'))
        return 0 if aligned else 1
