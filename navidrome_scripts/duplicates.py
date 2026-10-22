"""Review byte-identical copies and quarantine only unindexed, unmapped duplicates.

Indexed copies require a separate history/playlist migration review. Nothing is
selected by bitrate, filename, or recording ID alone; quarantine never deletes data.
"""
import argparse
import html
from collections import defaultdict
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import time

from dotenv import load_dotenv
from mutagen import MutagenError

from .dates import AUDIO, contained, digest, utc
from .lidarr import from_env as lidarr_from_env
from .locking import library_lock
from .metadata import identify
from .navidrome_guard import export_index, indexed_paths, path_key, scan_identity, user_snapshot
from .playlists import hash_json, normalized, save
from .subsonic import ApiError, from_env as navidrome_from_env


def stamp(path):
    stat = path.stat()
    return dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns, links=stat.st_nlink)


def audit(root, scope):
    started, clock = utc(), time.monotonic()
    folder = contained(root, scope)
    if not folder.is_dir():
        raise ValueError('scope must be a library directory')
    files, errors, buckets = [], [], defaultdict(list)
    def walk_error(exc):
        errors.append(dict(path=str(exc.filename), error=type(exc).__name__))
    for current, dirs, names in os.walk(folder, onerror=walk_error):
        rejected = []
        for name in dirs:
            child = Path(current) / name
            if child.is_symlink() or getattr(child, 'is_junction', lambda: False)() or not child.resolve().is_relative_to(root.resolve()):
                errors.append(dict(path=str(Path(current) / name), error='symlink directory not scanned'))
                rejected.append(name)
        dirs[:] = [name for name in dirs if name not in rejected]
        for name in names:
            path = Path(current) / name
            if path.suffix.lower() not in AUDIO:
                continue
            relative = path.relative_to(root).as_posix()
            try:
                contained(root, relative)
                row = dict(path=relative, **stamp(path))
                try:
                    row['tags'] = identify(path)
                except (ValueError, OSError, MutagenError) as exc:
                    row['tag_error'] = type(exc).__name__
                files.append(row); buckets[row['size']].append(row)
            except (ValueError, OSError) as exc:
                errors.append(dict(path=relative, error=type(exc).__name__))
    for bucket in buckets.values():
        if len(bucket) < 2:
            continue
        for row in bucket:
            path = contained(root, row['path'])
            try:
                value = digest(path)
                if stamp(path) != {k:row[k] for k in ('size','mtime_ns','links')}:
                    raise ValueError('file changed while hashing')
                row['sha256'] = value
            except (ValueError, OSError) as exc:
                errors.append(dict(path=row['path'], error=str(exc)))
    exact, recordings = defaultdict(list), defaultdict(list)
    for row in files:
        if row.get('sha256'):
            exact[row['sha256']].append(row['path'])
        recording = row.get('tags', {}).get('recording_mbid')
        if recording:
            recordings[recording].append(row['path'])
    return dict(schema=1, kind='duplicate-audit', root=str(root.resolve()), scope=scope,
                started=started, ended=utc(), elapsed_seconds=round(time.monotonic()-clock, 2),
                files=files, errors=errors,
                identical=[dict(sha256=key, paths=value) for key,value in exact.items() if len(value)>1],
                same_recording=[dict(recording_mbid=key, paths=value, note='May be different editions/encodes; not a deletion decision')
                                for key,value in recordings.items() if len(value)>1])


def sidecar_guard(root, keeper, source):
    keep, drop = contained(root, keeper), contained(root, source)
    for suffix in ('.lrc', '.lyrics', '.txt', '.cue'):
        candidate, counterpart = drop.with_suffix(suffix), keep.with_suffix(suffix)
        if candidate.exists() and (not counterpart.is_file() or candidate.is_symlink() or counterpart.is_symlink() or digest(candidate) != digest(counterpart)):
            raise ValueError('unique/conflicting sidecar requires manual review: ' + str(candidate))
    for folder in {keep.parent, drop.parent}:
        if any(p.suffix.casefold() in ('.cue', '.m3u', '.m3u8', '.pls') for p in folder.iterdir()):
            raise ValueError('local playlist/cue references require review before quarantine')


def assignments(lidarr, root, keeper, drops, remote_root):
    remote_keep = str(PurePosixPath(remote_root) / keeper)
    artists = [a for a in lidarr.get('artist') if PurePosixPath(remote_keep).is_relative_to(PurePosixPath(a['path']))]
    if len(artists) != 1:
        raise ValueError('keeper must have one unambiguous Lidarr artist folder')
    mapped = [r for r in lidarr.get('trackfile', artistId=artists[0]['id']) if r['path'] == remote_keep]
    if len(mapped) != 1 or not mapped[0].get('albumId'):
        raise ValueError('keeper must already be mapped in Lidarr')
    if mapped[0].get('size') != contained(root, keeper).stat().st_size:
        raise ValueError('Lidarr keeper size differs from the local file')
    unmapped = {r['path']:r for r in lidarr.get('trackfile', unmapped='true')}
    result = {}
    for drop in drops:
        path = str(PurePosixPath(remote_root) / drop)
        if path not in unmapped:
            raise ValueError('duplicate is mapped or not confirmed unmapped in Lidarr: ' + drop)
        if unmapped[path].get('size') != contained(root, drop).stat().st_size:
            raise ValueError('Lidarr duplicate size differs from the local file')
        result[drop] = dict(id=unmapped[path]['id'], path=path)
    return dict(keeper=dict(id=mapped[0]['id'], albumId=mapped[0]['albumId'], path=remote_keep), drops=result)


def nav_protection(nav, root, keeper, drops, prefix):
    index = export_index(nav, select_paths={keeper, *drops}, prefix=prefix)
    paths = indexed_paths(index, prefix)
    keep_rows = paths.get(keeper, [])
    if len(keep_rows) != 1:
        raise ValueError('keeper must have one indexed Navidrome identity; verify the path prefix')
    keep = keep_rows[0]
    tags = identify(contained(root, keeper))
    if int(keep.get('size') or 0) != contained(root, keeper).stat().st_size or normalized(keep.get('title','')) != normalized(tags.get('title','')) or abs(int(keep.get('duration') or 0)*1000-tags['duration_ms']) > 2500:
        raise ValueError('Navidrome keeper metadata/size does not match the local file; verify the root mapping')
    for path in drops:
        if any(path_key(candidate) == path_key(path) for candidate in paths):
            raise ValueError('duplicate has a Navidrome identity; ratings/history migration requires separate review: ' + path)
    state = user_snapshot(nav, [str(keep['id'])])
    if scan_identity(nav) != index['scan']:
        raise ValueError('Navidrome scan changed while checking references')
    return dict(server=nav.base_url, username=nav.username, prefix=prefix, keeper_id=str(keep['id']),
                scan=index['scan'], state=state, indexed_count=index['coverage_count'])


def write_html(report, output):
    escape = lambda text: html.escape(str(text), quote=True)
    rows = {row['path']:row for row in report['files']}
    parts = ['<!doctype html><meta charset="utf-8"><title>Duplicate review</title>',
        '<style>body{font:16px sans-serif;max-width:1200px;margin:2em auto;padding:1em}table{border-collapse:collapse;width:100%}td,th{padding:.5em;border:1px solid #bbb;text-align:left;overflow-wrap:anywhere}code{overflow-wrap:anywhere}</style>',
        '<h1>Duplicate review</h1><p>Read-only report. No keeper was chosen and nothing was moved or deleted.</p>',
        '<p>Scope: <code>' + escape(report['scope']) + '</code>. Files: ' + str(len(rows)) +
        '. Byte-identical groups: ' + str(len(report['identical'])) + '. Same-recording groups: ' + str(len(report['same_recording'])) + '.</p>',
        '<p>Same recording IDs, similar names and higher bitrates do not establish a safe duplicate. Compare editions, durations and actual recordings before choosing a keeper.</p>']
    for label, groups in [('Byte-identical copies',report['identical']),('Same recording ID — review editions and encodes',report['same_recording'])]:
        parts.append('<h2>' + label + '</h2>')
        if not groups:
            parts.append('<p>None found.</p>')
        for index, group in enumerate(groups,1):
            parts.append('<h3>Group '+str(index)+'</h3><table><tr><th>Path</th><th>Bytes</th><th>Title / album</th><th>Duration</th><th>Release ID</th></tr>')
            for path in group['paths']:
                row=rows[path]; tags=row.get('tags',{})
                parts.append('<tr><td><code>'+escape(path)+'</code></td><td>'+str(row['size'])+'</td><td>'+escape(tags.get('title',''))+' / '+escape(tags.get('album',''))+'</td><td>'+escape(tags.get('duration_ms',''))+' ms</td><td>'+escape(tags.get('release_mbid',''))+'</td></tr>')
            parts.append('</table>')
    parts.append('<h2>Read errors</h2><pre>'+escape(json.dumps(report['errors'],indent=2,ensure_ascii=False))+'</pre>')
    with output.open('x',encoding='utf8') as stream:
        stream.write('\n'.join(parts))


def plan(lidarr, nav, root, report, keeper, drops, quarantine, remote_root='/music', prefix=''):
    if report.get('schema') != 1 or report.get('kind') != 'duplicate-audit' or Path(report['root']).resolve() != root.resolve():
        raise ValueError('duplicate audit root/schema differs')
    if not drops or len(set(drops)) != len(drops) or keeper in drops:
        raise ValueError('choose one keeper and distinct duplicate paths')
    selected = {keeper, *drops}
    candidates = [g for g in report['identical'] if selected.issubset(g['paths'])]
    if len(candidates) != 1:
        raise ValueError('all selections must belong to one verified byte-identical group')
    quarantine = quarantine.resolve()
    if quarantine.is_relative_to(root.resolve()) or root.resolve().is_relative_to(quarantine):
        raise ValueError('quarantine must be separate from the music library')
    records = {r['path']:r for r in report['files']}
    for relative in selected:
        path = contained(root, relative)
        row = records[relative]
        if stamp(path) != {k:row[k] for k in ('size','mtime_ns','links')} or digest(path) != candidates[0]['sha256']:
            raise ValueError('file changed since duplicate audit: ' + relative)
        if path.stat().st_nlink > 1:
            raise ValueError('hardlinked files require separate review')
    for drop in drops:
        if os.path.samefile(contained(root, keeper), contained(root, drop)):
            raise ValueError('keeper and duplicate refer to the same underlying file')
        sidecar_guard(root, keeper, drop)
    lidarr_state = assignments(lidarr, root, keeper, drops, remote_root)
    nav_state = nav_protection(nav, root, keeper, drops, prefix)
    return dict(schema=1, kind='duplicate-quarantine', root=str(root.resolve()), lidarr=lidarr.url,
                remote_root=remote_root, created=utc(), keeper=keeper, drops=drops,
                files={r:{k:records[r][k] for k in ('sha256','size','mtime_ns','links')} for r in selected},
                quarantine=str(quarantine), lidarr_state=lidarr_state, nav_state=nav_state,
                note='Only unindexed/unmapped byte-identical copies. Sidecars/artwork stay in place. No database records are changed.')


def copy_exclusive(source, target, expected):
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create avoids POSIX rename() silently replacing a destination.
    with source.open('rb') as src, target.open('xb') as dst:
        shutil.copyfileobj(src, dst, length=1024*1024)
        dst.flush(); os.fsync(dst.fileno())
    shutil.copystat(source, target)
    if digest(source) != expected or digest(target) != expected:
        raise ValueError('file changed during copy; both copies retained for review')


def restore(root, payload, journal):
    if payload.get('schema') != 1 or payload.get('kind') != 'duplicate-quarantine' or Path(payload['root']).resolve() != root.resolve():
        raise ValueError('quarantine plan root/schema differs')
    with library_lock(root):
        _restore(root, payload, journal)


def _restore(root, payload, journal):
    record = json.loads(journal.read_text(encoding='utf8'))
    if record.get('plan') != hash_json(payload):
        raise ValueError('journal belongs to a different quarantine plan')
    for entry in record['entries']:
        source = contained(root, entry['path'])
        expected = payload['files'][entry['path']]['sha256']
        saved = contained(Path(record['directory']), entry['path'])
        if source.exists():
            if digest(source) != expected:
                raise ValueError('restore refuses to overwrite a changed original path')
        else:
            if not saved.is_file() or digest(saved) != expected:
                raise ValueError('quarantine copy is missing or changed')
            copy_exclusive(saved, source, expected)
        entry['state'] = 'restored'; save(journal, record)
    record.update(status='restored', ended=utc()); save(journal, record)
    print('Restored original paths; quarantine backups retained.')


def apply(lidarr, nav, root, payload, journal):
    if payload.get('schema') != 1 or payload.get('kind') != 'duplicate-quarantine' or Path(payload['root']).resolve() != root.resolve() or payload['lidarr'] != lidarr.url or payload['nav_state']['server'] != nav.base_url or payload['nav_state']['username'] != nav.username:
        raise ValueError('plan server/user/root/schema differs')
    signature = hash_json(payload)
    with library_lock(root):
        record = json.loads(journal.read_text(encoding='utf8')) if journal.exists() else None
        if record and record.get('plan') != signature:
            raise ValueError('journal belongs to a different plan')
        if record and record['status'] == 'quarantined':
            for entry in record['entries']:
                if contained(root, entry['path']).exists() or digest(contained(Path(record['directory']), entry['path'])) != payload['files'][entry['path']]['sha256']:
                    raise ValueError('completed quarantine state changed; inspect before continuing')
            return 0
        if record:
            raise ValueError('interrupted quarantine requires restore/review before a new apply; no moves repeated')
        keeper, drops = payload['keeper'], payload['drops']
        for relative in [keeper, *drops]:
            path = contained(root, relative)
            row = payload['files'][relative]
            if stamp(path) != {k:row[k] for k in ('size','mtime_ns','links')} or digest(path) != row['sha256']:
                raise ValueError('file changed since quarantine preview: ' + relative)
        for drop in drops:
            sidecar_guard(root, keeper, drop)
        if assignments(lidarr, root, keeper, drops, payload['remote_root']) != payload['lidarr_state']:
            raise ValueError('Lidarr assignments changed since preview')
        current_nav = nav_protection(nav, root, keeper, drops, payload['nav_state']['prefix'])
        if current_nav != payload['nav_state']:
            raise ValueError('Navidrome identities/references changed since preview')
        base = Path(payload['quarantine']).resolve()
        if base.is_relative_to(root.resolve()) or root.resolve().is_relative_to(base):
            raise ValueError('quarantine must remain outside the music library')
        directory = base / signature
        directory.mkdir(parents=True, exist_ok=False)
        record = dict(plan=signature, status='prepared', started=utc(), directory=str(directory),
                      entries=[dict(path=p, state='pending') for p in drops])
        save(journal, record, exclusive=True)
        try:
            for entry in record['entries']:
                source, target = contained(root, entry['path']), contained(directory, entry['path'])
                expected = payload['files'][entry['path']]
                if digest(contained(root, keeper)) != payload['files'][keeper]['sha256']:
                    raise ValueError('keeper changed; quarantine stopped')
                entry['state']='copying'; save(journal, record)
                copy_exclusive(source, target, expected['sha256'])
                if source.stat().st_mtime_ns != expected['mtime_ns'] or digest(source) != expected['sha256']:
                    raise ValueError('source changed before quarantine; copies retained')
                source.unlink()
                entry['state']='quarantined'; save(journal, record)
            if scan_identity(nav) != payload['nav_state']['scan'] or user_snapshot(nav,[payload['nav_state']['keeper_id']]) != payload['nav_state']['state']:
                raise ValueError('Navidrome changed during quarantine; restore/review this batch before scanning')
            record.update(status='quarantined', ended=utc()); save(journal, record)
        except Exception as exc:
            record.update(status='needs_review', error=type(exc).__name__, updated=utc()); save(journal, record)
            try:
                _restore(root, payload, journal)
            except Exception as recovery:
                record = json.loads(journal.read_text(encoding='utf8'))
                record.update(status='needs_review', restore_error=type(recovery).__name__); save(journal, record)
            raise
        print(f'Quarantined {len(drops)} unindexed duplicate copy/copies. Keeper and service records were untouched.')
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--library-root', type=Path, required=True)
    sub=parser.add_subparsers(dest='action',required=True)
    scan=sub.add_parser('audit'); scan.add_argument('--path',required=True);scan.add_argument('--output',type=Path,required=True)
    preview=sub.add_parser('plan');preview.add_argument('--audit',type=Path,required=True);preview.add_argument('--keep',required=True)
    preview.add_argument('--quarantine-file',action='append',required=True);preview.add_argument('--quarantine-dir',type=Path,required=True)
    preview.add_argument('--remote-root',default='/music');preview.add_argument('--navidrome-prefix',default='');preview.add_argument('--output',type=Path,required=True)
    indexed=sub.add_parser('indexed-plan')
    indexed.add_argument('--roots',type=Path,required=True,help='JSON object mapping Navidrome library IDs to local roots')
    indexed.add_argument('--keep-id',required=True);indexed.add_argument('--quarantine-id',required=True)
    indexed.add_argument('--quarantine-dir',type=Path,required=True);indexed.add_argument('--native-command',type=Path,required=True,help='JSON argument array for the local official Navidrome CLI')
    indexed.add_argument('--backup-root',type=Path,required=True);indexed.add_argument('--native-backup-root')
    indexed.add_argument('--allow-alternate-encode',action='store_true');indexed.add_argument('--remote-root',default='/music');indexed.add_argument('--output',type=Path,required=True)
    for command in ('indexed-apply','indexed-verify','indexed-restore-files'):
        action=sub.add_parser(command);action.add_argument('--plan',type=Path,required=True);action.add_argument('--journal',type=Path,required=True)
    for command in ('apply','restore'):
        execute=sub.add_parser(command);execute.add_argument('--plan',type=Path,required=True);execute.add_argument('--journal',type=Path,required=True)
    args=parser.parse_args(argv);load_dotenv(args.env_file)
    try:
        root=args.library_root.resolve(strict=True)
        if args.action=='audit':
            report=audit(root,args.path);save(args.output,report,exclusive=True)
            write_html(report,args.output.with_suffix('.html'))
            print(json.dumps(dict(files=len(report['files']),identical_groups=len(report['identical']),same_recording_groups=len(report['same_recording']),errors=len(report['errors']),elapsed_seconds=report['elapsed_seconds'])))
            return 1 if report['errors'] else 0
        if args.action.startswith('indexed-'):
            from . import indexed_duplicates
            lidarr,nav=lidarr_from_env(),navidrome_from_env()
            if args.action=='indexed-plan':
                cli=indexed_duplicates.NativeCLI(json.loads(args.native_command.read_text(encoding='utf8')),args.backup_root,args.native_backup_root)
                result=indexed_duplicates.plan(lidarr,nav,root,json.loads(args.roots.read_text(encoding='utf8')),
                    args.keep_id,args.quarantine_id,args.quarantine_dir,cli,args.allow_alternate_encode,args.remote_root)
                save(args.output,result,exclusive=True);indexed_duplicates.write_html(result,args.output.with_suffix('.html'))
                print('Review indexed cleanup plan before applying: '+str(args.output.with_suffix('.html')));return 0
            payload=json.loads(args.plan.read_text(encoding='utf8'))
            cli=indexed_duplicates.NativeCLI(payload['native_command'],payload['backups'],payload['native_backups'])
            if args.action=='indexed-restore-files':
                indexed_duplicates.restore_files(lidarr,nav,root,payload,args.journal,cli);return 0
            return (indexed_duplicates.apply if args.action=='indexed-apply' else indexed_duplicates.verify)(lidarr,nav,root,payload,args.journal,cli)
        payload=json.loads((args.audit if args.action=='plan' else args.plan).read_text(encoding='utf8'))
        if args.action=='restore':
            restore(root,payload,args.journal);return 0
        lidarr,nav=lidarr_from_env(),navidrome_from_env()
        if args.action=='plan':
            result=plan(lidarr,nav,root,payload,args.keep,args.quarantine_file,args.quarantine_dir,args.remote_root,args.navidrome_prefix)
            save(args.output,result,exclusive=True);print('Review plan before applying: '+str(args.output));return 0
        return apply(lidarr,nav,root,payload,args.journal)
    except (ValueError,OSError,ApiError,MutagenError) as exc:
        parser.exit(1,str(exc)+'\n')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
