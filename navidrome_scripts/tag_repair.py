"""Review and reversibly fill missing core tags from an exact Beets/catalog match."""
from __future__ import annotations

import argparse
import hashlib
import html
import inspect
import json
import os
from pathlib import Path
import shutil
import tempfile
import subprocess

from dotenv import load_dotenv
from mutagen import File,MutagenError
import requests

from . import dates, maintenance
from .catalog import MusicBrainzClient
from .date_evidence import artist_ids
from .lidarr import from_env
from .locking import library_lock,begin_tag_write,finish_tag_write
from .metadata import CORE_TAG_FIELDS
from .playlists import hash_json, normalized, save
from .renames import folder_for
from .subsonic import ApiError
from .unmapped import album_identity, local_path

FIELDS=CORE_TAG_FIELDS
SUPPORTED={'.flac','.mp3','.m4a','.ogg','.oga','.opus'}


def media(path):
    try:
        from mediafile import MediaFile
        from mediafile.exceptions import MediaFileError
    except ImportError:
        raise ValueError('install navidrome-scripts[tags] to repair metadata') from None
    if path.suffix.lower() not in SUPPORTED:
        raise ValueError('format requires separate tag-repair review: '+path.suffix)
    original=File(path)
    if original is None or not getattr(original,'info',None):
        raise ValueError('unreadable audio file')
    version=getattr(original.tags,'version',None)
    try:
        return MediaFile(str(path),id3v23=bool(version and version[1]==3))
    except MediaFileError as exc:
        raise ValueError('cannot read tag backend: '+str(exc)) from None


def semantic(path):
    audio=media(path)
    raw=File(path)
    for field in ('title','album','mb_albumid','mb_releasegroupid','mb_trackid','mb_releasetrackid'):
        keys=style_keys(audio,[field]);seen=set()
        for key,value in (raw.tags or {}).items():
            if str(key).casefold() in keys:
                seen.update(v.strip() for v in dates.values(getattr(value,'data',value)) if v.strip())
        if len(seen)>1:
            raise ValueError('multiple conflicting identifying values: '+field)
    return {key:getattr(audio,key) for key in FIELDS}


def style_keys(audio,fields):
    keys=set()
    for field in fields:
        descriptor=inspect.getattr_static(type(audio),field)
        for style in descriptor.styles(audio.mgfile):
            key=style.key
            if key=='TXXX':
                key+=':'+style.description
            keys.add(key.casefold())
    return keys


def protected(path,fields):
    audio=media(path);raw=File(path);allowed=style_keys(audio,fields)
    return dict(raw=hash_json({str(k):repr(v) for k,v in (raw.tags or {}).items() if str(k).casefold() not in allowed}),
        pictures=[hashlib.sha256(p.write()).hexdigest() for p in getattr(raw,'pictures',[])],
        version=getattr(raw.tags,'version',None),
        unknown=[hashlib.sha256(x).hexdigest() for x in getattr(raw.tags,'unknown_frames',[])],
        audio={key:getattr(raw.info,key,None) for key in ('length','sample_rate','channels','bits_per_sample')},
        fields={k:getattr(audio,k) for k in FIELDS if k not in fields})


def credits(rows):
    names=[r.get('name') or r.get('artist',{}).get('name','') for r in rows]
    text=''.join(name+r.get('joinphrase','') for name,r in zip(names,rows))
    if not names or not all(names) or len(artist_ids(rows))!=len(names):
        raise ValueError('catalog artist identities/credits are incomplete')
    return names,text,artist_ids(rows)


def catalog_values(payload,current,manifest,duration):
    release,group=manifest['release_mbid'],manifest['group_mbid']
    if payload.get('id')!=release or (payload.get('release-group') or {}).get('id')!=group:
        raise ValueError('catalog edition/release-group identity differs')
    album_names,album_artist,album_ids=credits(payload.get('artist-credit',[]))
    if album_ids!=artist_ids(payload['release-group'].get('artist-credit',[])) or manifest['artist_mbid'] not in album_ids:
        raise ValueError('release and release-group artists conflict with the mapped artist')
    for field,expected in [('mb_albumid',release),('mb_releasegroupid',group),('mb_albumartistids',album_ids)]:
        if current[field] and current[field]!=expected:
            raise ValueError('existing identity contradicts the mapped release: '+field)
    strong=bool(current['mb_trackid'] or current['mb_releasetrackid'])
    if not strong and not (current['mb_albumid']==release and current['title'] and current['track'] and current['disc']):
        raise ValueError('insufficient independent recording/position/title evidence')
    if not strong and normalized(current['albumartist'] or current['artist'])!=normalized(album_artist) and current['mb_albumartistids']!=album_ids:
        raise ValueError('artist identity cannot be independently verified')
    matches=[]
    for medium in payload.get('media',[]):
        for row in medium.get('tracks',[]):
            recording=row.get('recording') or {};title=row.get('title') or recording.get('title') or ''
            length=row.get('length') or recording.get('length') or 0
            if not length or abs(int(length)-duration)>2500:
                continue
            if current['mb_trackid'] and current['mb_trackid']!=recording.get('id'):
                continue
            if current['mb_releasetrackid'] and current['mb_releasetrackid']!=row.get('id'):
                continue
            if current['title'] and normalized(current['title'])!=normalized(title):
                continue
            if current['track'] and str(current['track'])!=str(row.get('number')):
                continue
            if current['disc'] and current['disc']!=medium.get('position'):
                continue
            matches.append((medium,row,recording,title))
    if len(matches)!=1:
        raise ValueError('no unique independently verified catalog track')
    medium,row,recording,title=matches[0]
    names,artist,ids=credits(row.get('artist-credit') or recording.get('artist-credit') or payload.get('artist-credit',[]))
    if current['mb_artistids'] and current['mb_artistids']!=ids:
        raise ValueError('existing recording artist IDs conflict with the catalog')
    number=str(row.get('number',''))
    if not number.isdigit() or not row.get('id') or not recording.get('id'):
        raise ValueError('catalog track positions/identifiers need manual review')
    return dict(title=title,artist=artist,artists=names,album=payload.get('title') or '',albumartist=album_artist,albumartists=album_names,
        track=int(number),tracktotal=int(medium.get('track-count') or len(medium.get('tracks',[]))),disc=int(medium['position']),
        disctotal=len(payload['media']),mb_trackid=recording['id'],mb_releasetrackid=row['id'],mb_albumid=release,
        mb_releasegroupid=group,mb_artistids=ids,mb_albumartistids=album_ids)


def choose_targets(current,beets,canonical):
    targets={};conflicts=[]
    for field in FIELDS:
        source=beets.get(field)
        if source and source!=canonical[field]:
            raise ValueError('Beets proposal differs from exact catalog evidence: '+field)
        if not current[field] and source:
            targets[field]=source
        elif current[field] and source and current[field]!=source:
            conflicts.append(field)
    return targets,conflicts


def beets_values(row,canonical):
    """Decode known formatting by comparison; never split artist names on ';'."""
    result={}
    for field in FIELDS:
        value=row.get(field)
        expected=canonical[field]
        if value and isinstance(expected,list) and isinstance(value,str):
            if value!='; '.join(expected):
                raise ValueError('formatted Beets credit differs from catalog: '+field)
            value=list(expected)
        elif value and isinstance(expected,int) and isinstance(value,str):
            if not value.isdigit():
                raise ValueError('invalid Beets numeric field: '+field)
            value=int(value)
        result[field]=value
    return result


def manifest_for(client,root,album_id,remote_root):
    # No tag writers or timestamp backdating may race a reviewed tag repair.
    if str(client.get('config/metadataprovider').get('writeAudioTags','')).casefold()!='no' or str(client.get('config/mediamanagement').get('fileDate','')).casefold()!='none':
        raise ValueError('set Lidarr audio-tag writing to Never and File Date=None before tag repair')
    album=client.get('album/'+str(album_id));artist=client.get('artist/'+str(album['artistId']))
    selected=[r for r in album.get('releases',[]) if r.get('monitored')]
    if len(selected)!=1 or not selected[0].get('foreignReleaseId'):
        raise ValueError('select one exact Lidarr release edition first')
    rows=maintenance.mapped_files(client,album_id)
    paths=[local_path(root,r['path'],remote_root) for r in rows]
    folder=folder_for(paths);artist_folder=local_path(root,artist['path'],remote_root)
    if not folder.is_relative_to(artist_folder) or folder==artist_folder:
        raise ValueError('loose artist-level or mixed album folders require review')
    maintenance.physical_files(folder,paths)
    return dict(server=client.url,root=str(root.resolve()),remote_root=remote_root,album=album_identity(album),files=rows,
        album_folder=remote_root.rstrip('/')+'/'+folder.relative_to(root).as_posix(),release_mbid=selected[0]['foreignReleaseId'],
        group_mbid=album['foreignAlbumId'],artist_mbid=artist['foreignArtistId'])


def make_plan(client,root,album_id,output,remote_root='/music',catalog=None,run=maintenance.execute):
    root=root.resolve(strict=True);catalog=catalog or MusicBrainzClient()
    manifest=manifest_for(client,root,album_id,remote_root)
    before={r['path']:dict(sha256=dates.digest(local_path(root,r['path'],remote_root)),mtime_ns=local_path(root,r['path'],remote_root).stat().st_mtime_ns) for r in manifest['files']}
    output.parent.mkdir(parents=True,exist_ok=True)
    workdir=output.parent/(output.stem+'-beets');workdir.mkdir(exist_ok=False)
    result=maintenance.beets_preview(manifest,root,workdir,dict(os.environ),run)
    if manifest_for(client,root,album_id,remote_root)!=manifest:
        raise ValueError('album mapping changed during Beets preview')
    for row in manifest['files']:
        path=local_path(root,row['path'],remote_root)
        if before[row['path']]!=dict(sha256=dates.digest(path),mtime_ns=path.stat().st_mtime_ns):
            raise ValueError('read-only Beets preview changed a file')
    entries=[];unresolved=[]
    exported=json.loads(Path(result['export']).read_text(encoding='utf8'))
    if not isinstance(exported,list):
        raise ValueError('invalid Beets export')
    by_path={Path(r['path']).resolve():r for r in exported}
    if len(by_path)!=len(exported):
        raise ValueError('Beets export contains duplicate file paths')
    expected_paths={local_path(root,r['path'],remote_root).resolve() for r in manifest['files']}
    if set(by_path)!=expected_paths or result['status']!='review_ready':
        unresolved.append(dict(reason='Beets did not match every mapped file to the exact release; no tags selected'))
    else:
        source=catalog.lookup_release(manifest['release_mbid'])
        for row in manifest['files']:
            path=local_path(root,row['path'],remote_root)
            try:
                current=semantic(path)
                duration=round(float(File(path).info.length)*1000)
                canonical=catalog_values(source,current,manifest,duration)
                beets=beets_values(by_path[path.resolve()],canonical)
                for key in ('mb_albumid','mb_releasegroupid','mb_trackid','mb_releasetrackid','track','disc'):
                    if beets[key]!=canonical[key]:
                        raise ValueError('Beets track/edition mapping is incomplete or differs: '+key)
                targets,conflicts=choose_targets(current,beets,canonical)
                if conflicts:
                    unresolved.append(dict(path=row['path'],reason='existing values retained for review',fields=conflicts))
                if targets:
                    if path.stat().st_nlink>1:
                        raise ValueError('hardlinked input requires separate review')
                    entries.append(dict(path=path.relative_to(root).as_posix(),sha256=before[row['path']]['sha256'],mtime_ns=before[row['path']]['mtime_ns'],
                        current=current,beets=beets,targets=targets,canonical=canonical,duration_ms=duration))
            except (ValueError,MutagenError) as exc:
                unresolved.append(dict(path=row['path'],reason=str(exc)))
    payload=dict(schema=1,kind='beets-missing-tags',created=dates.utc(),manifest=manifest,entries=entries,unresolved=unresolved,
        export=result['export'],note='Fill missing core tags only. Existing values, dates, lyrics, gain, art and paths are preserved; conflicting values remain for review.')
    save(output,payload,exclusive=True);write_html(payload,output.with_suffix('.html'))
    print(f'Tag plan: {len(entries)} files, {len(unresolved)} review notes; open {output.with_suffix(".html")}')
    return payload


def write_html(payload,output):
    escape=lambda value:html.escape(str(value),quote=True)
    parts=['<!doctype html><meta charset="utf-8"><title>Missing metadata review</title>','<h1>Missing metadata review</h1><p>'+escape(payload['note'])+'</p>']
    for entry in payload['entries']:
        parts.append('<h2>'+escape(entry['path'])+'</h2><table><tr><th>Field</th><th>Current</th><th>Proposed</th></tr>')
        parts.extend('<tr><td>'+escape(key)+'</td><td>'+escape(entry['current'][key])+'</td><td>'+escape(value)+'</td></tr>' for key,value in entry['targets'].items())
        parts.append('</table><p><a href="https://musicbrainz.org/release/'+escape(payload['manifest']['release_mbid'])+'">Exact source release</a></p>')
    parts.append('<h2>Needs review</h2>')
    parts.extend('<p>'+escape(json.dumps(row,ensure_ascii=False))+'</p>' for row in payload['unresolved'])
    output.write_text('\n'.join(parts),encoding='utf8')


def write_fields(path,entry):
    audio=media(path);audio.update(entry['targets']);audio.save()


def verify_write(path,entry,baseline):
    if protected(path,entry['targets'])!=baseline:
        raise ValueError('tag writer changed unapproved metadata')
    current=semantic(path)
    for key,value in entry['targets'].items():
        if current[key]!=value:
            raise ValueError('tag field did not round-trip: '+key)


def validate_plan(client,root,payload):
    if payload.get('schema')!=1 or payload.get('kind')!='beets-missing-tags' or payload['manifest']['server']!=client.url or Path(payload['manifest']['root']).resolve()!=root.resolve():
        raise ValueError('tag plan schema/server/root differs')
    manifest=payload['manifest']
    if manifest_for(client,root,manifest['album']['id'],manifest['remote_root'])!=manifest:
        raise ValueError('album mapping/edition changed since tag preview')
    allowed={local_path(root,r['path'],manifest['remote_root']).relative_to(root).as_posix() for r in manifest['files']}
    if any(r['path'] not in allowed for r in payload['entries']) or len({r['path'] for r in payload['entries']})!=len(payload['entries']):
        raise ValueError('tag plan contains duplicate or unmapped paths')


def apply(client,root,payload,plan_path,journal,catalog=None):
    catalog=catalog or MusicBrainzClient()
    with library_lock(root):
        if json.loads(plan_path.read_text(encoding='utf8'))!=payload:
            raise ValueError('in-memory tag plan differs from the reviewed file')
        validate_plan(client,root,payload)
        if not payload['entries']:
            print('No verified missing tags selected; no files changed.');return 0
        signature=dates.digest(plan_path);previous={}
        if journal.exists():
            for line in journal.read_text(encoding='utf8').splitlines():
                row=json.loads(line)
                if row.get('plan_sha256')!=signature or Path(row['root']).resolve()!=root.resolve():
                    raise ValueError('tag journal belongs to another plan/root')
                previous[row['path']]=row
        if not set(previous).issubset(r['path'] for r in payload['entries']):
            raise ValueError('journal contains paths outside this plan')
        source=None
        for entry in payload['entries']:
            path=dates.contained(root,entry['path']);prior=previous.get(entry['path'])
            if prior and prior['original_sha256']!=entry['sha256']:
                raise ValueError('journal original differs from the tag plan')
            if prior and prior['state']=='applied':
                if dates.digest(path)!=prior['updated_sha256'] or path.stat().st_mtime_ns!=prior['mtime_ns']:
                    raise ValueError('file changed after tag repair')
                continue
            if dates.digest(path)!=entry['sha256'] or path.stat().st_mtime_ns!=entry['mtime_ns'] or semantic(path)!=entry['current'] or path.stat().st_nlink>1:
                raise ValueError('file changed since tag plan: '+entry['path'])
            if round(float(File(path).info.length)*1000)!=entry['duration_ms']:
                raise ValueError('reviewed duration differs from the audio file')
            if source is None:
                source=catalog.lookup_release(payload['manifest']['release_mbid'])
            canonical=catalog_values(source,entry['current'],payload['manifest'],entry['duration_ms'])
            expected,_=choose_targets(entry['current'],entry['beets'],canonical)
            if canonical!=entry['canonical'] or not entry['targets'] or not set(entry['targets']).issubset(expected) or any(value!=expected[key] for key,value in entry['targets'].items()):
                raise ValueError('catalog evidence or selected missing fields changed')
        backups=journal.with_suffix(journal.suffix+'.backups');backups.mkdir(parents=True,exist_ok=True)
        staged={}
        for index,entry in enumerate(payload['entries']):
            if previous.get(entry['path'],{}).get('state')=='applied':
                continue
            path=dates.contained(root,entry['path']);backup=backups/str(index)
            if not backup.exists():
                shutil.copy2(path,backup)
            if backup.is_symlink() or dates.digest(backup)!=entry['sha256']:
                raise ValueError('original backup differs; refusing overwrite')
            fd,name=tempfile.mkstemp(prefix='tag-stage-',suffix=path.suffix,dir=backups);os.close(fd)
            stage=Path(name);shutil.copy2(backup,stage)
            baseline=protected(stage,entry['targets'])
            write_fields(stage,entry);verify_write(stage,entry,baseline)
            staged[entry['path']]=(stage,baseline)
        # Every selected format has passed staging before the first original write.
        validate_plan(client,root,payload)
        with journal.open('a' if journal.exists() else 'x',encoding='utf8') as stream:
            for index,entry in enumerate(payload['entries']):
                if previous.get(entry['path'],{}).get('state')=='applied':
                    continue
                path=dates.contained(root,entry['path']);stat=path.stat();backup=backups/str(index)
                if dates.digest(path)!=entry['sha256'] or stat.st_mtime_ns!=entry['mtime_ns']:
                    raise ValueError('file changed during tag staging')
                record=dict(path=entry['path'],backup=str(backup.resolve()),original_sha256=entry['sha256'],root=str(root.resolve()),
                    plan_sha256=signature,mtime_ns=stat.st_mtime_ns,atime_ns=stat.st_atime_ns,state='prepared',started=dates.utc())
                begin_tag_write(root,journal,'missing core tags')
                def append():
                    stream.write(json.dumps(record)+'\n');stream.flush();os.fsync(stream.fileno())
                append()
                try:
                    write_fields(path,entry);os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns));verify_write(path,entry,staged[entry['path']][1])
                except Exception:
                    shutil.copy2(backup,path);raise
                record.update(state='applied',updated_sha256=dates.digest(path),ended=dates.utc());append()
                try:
                    staged[entry['path']][0].unlink()
                except OSError:
                    print('Staged copy retained: '+str(staged[entry['path']][0]))
                print('APPLIED missing tags '+entry['path'],flush=True)
        finish_tag_write(root,journal)
    return 0


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,default=Path('.env'));parser.add_argument('--library-root',type=Path,required=True)
    sub=parser.add_subparsers(dest='action',required=True)
    preview=sub.add_parser('plan');preview.add_argument('--album-id',type=int,required=True);preview.add_argument('--remote-root',default='/music');preview.add_argument('--output',type=Path,required=True)
    write=sub.add_parser('apply');write.add_argument('--plan',type=Path,required=True);write.add_argument('--journal',type=Path,required=True)
    undo=sub.add_parser('rollback');undo.add_argument('--journal',type=Path,required=True)
    args=parser.parse_args(argv);load_dotenv(args.env_file)
    try:
        root=args.library_root.resolve(strict=True)
        if args.action=='rollback':
            dates.rollback(root,args.journal);return 0
        client=from_env()
        if args.action=='plan':
            make_plan(client,root,args.album_id,args.output,args.remote_root);return 0
        return apply(client,root,json.loads(args.plan.read_text(encoding='utf8')),args.plan,args.journal)
    except (ValueError,OSError,ApiError,MutagenError,requests.RequestException,subprocess.SubprocessError) as exc:
        parser.exit(1,str(exc)+'\n')


if __name__=='__main__':
    raise SystemExit(main())
