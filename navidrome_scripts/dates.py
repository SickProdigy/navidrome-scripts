"""Audit embedded dates and apply reviewed cleanup or exact-release replacements.

Never changes names or filesystem dates. Unknown dates remain unknown.
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import tempfile
from datetime import datetime, timezone

from mutagen import File
from dotenv import load_dotenv
from .locking import library_lock,begin_tag_write,finish_tag_write

AUDIO = {'.flac', '.mp3', '.m4a', '.aac', '.ogg', '.oga', '.opus', '.wav', '.wma', '.aiff', '.ape', '.wv', '.dsf'}
DATE_KEYS = {'date', 'year', 'originaldate', 'originalyear', 'tdrc', 'tdor', 'tyer', 'tory', 'tdat', '©day',
             'wm/year','wm/originalreleasetime','wm/originalreleaseyear'}


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def contained(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('relative library path required')
    path = root / relative
    if any((root / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)) or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('symlink or path outside library root')
    return path


def date_key(key):
    key = str(key).casefold().removeprefix('txxx:').removeprefix('----:com.apple.itunes:')
    return key.replace('_', '').replace(' ', '') in DATE_KEYS


def values(value):
    value = getattr(value, 'text', value)
    if not isinstance(value, (tuple, list)):
        value = [value]
    return [v.decode('utf8', errors='replace') if isinstance(v, bytes) else str(v) for v in value]


def date_problem(value):
    """Retain year-only precision; never construct a full date from a year."""
    value = value.strip()
    if not value:
        return ''
    match = re.match(r'^(\d{1,4})(?:$|[-/T ])', value)
    if not match:
        return 'unrecognized date; review manually'
    year = int(match[1])
    if year <= 1:
        return 'invalid placeholder year'
    if year < 1000:
        return 'unusual early year; review manually'
    parts = re.fullmatch(r'(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?', value)
    if parts:
        month, day = parts[2], parts[3]
        if month and not 1 <= int(month) <= 12:
            return 'invalid calendar date'
        if day and not 1 <= int(day) <= calendar.monthrange(year, int(month))[1]:
            return 'invalid calendar date'
    else:
        try:
            datetime.fromisoformat(value.replace('Z','+00:00'))
        except ValueError:
            return 'unrecognized or invalid date; review manually'
    return ''


def inspect(path, root):
    audio = File(path)
    if audio is None:
        raise ValueError('unreadable or unsupported audio')
    tags = audio.tags or {}
    dates = {str(k): values(v) for k, v in tags.items() if date_key(k)}
    problems = {k: [dict(value=v, reason=date_problem(v)) for v in vv if date_problem(v)]
                for k, vv in dates.items()}
    problems = {k: v for k, v in problems.items() if v}
    # Never erase a valid value sharing a multi-value date field.
    removable = [k for k, vv in dates.items() if vv and all(
        date_problem(v) == 'invalid placeholder year' for v in vv)]
    return dict(path=path.relative_to(root).as_posix(), dates=dates, problems=problems,
                remove_keys=removable,
                stale_folder=any(re.search(r'\(0*1\)$', part) for part in (root.name, *path.relative_to(root).parts[:-1])))


def audit(root, output):
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError('library root must be a directory')
    started, clock = utc(), time.monotonic()
    counts = dict(scanned=0, flagged=0, errors=0)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf8') as stream:
        stream.write(json.dumps(dict(type='start', started=started, root=str(root))) + '\n')
        def walk_error(error):
            counts['errors'] += 1
            stream.write(json.dumps(dict(type='error', path=str(error.filename), error=str(error))) + '\n')
        for current, dirs, files in os.walk(root, onerror=walk_error):
            for directory in dirs:
                candidate = Path(current) / directory
                if candidate.is_symlink():
                    counts['errors'] += 1
                    stream.write(json.dumps(dict(type='error', path=str(candidate), error='symlink directory not scanned')) + '\n')
            dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink()]
            for name in files:
                path = Path(current) / name
                if path.suffix.lower() not in AUDIO:
                    continue
                counts['scanned'] += 1
                try:
                    contained(root, path.relative_to(root))
                    row = inspect(path, root)
                    if row['problems'] or row['stale_folder']:
                        counts['flagged'] += 1
                        stream.write(json.dumps(dict(type='file', **row), ensure_ascii=False) + '\n')
                except Exception as exc:
                    counts['errors'] += 1
                    stream.write(json.dumps(dict(type='error', path=str(path), error=str(exc))) + '\n')
                if counts['scanned'] % 1000 == 0:
                    stream.flush()
                    print(json.dumps(counts), flush=True)
        summary = dict(type='end', started=started, ended=utc(), elapsed_seconds=round(time.monotonic() - clock, 2), **counts)
        stream.write(json.dumps(summary) + '\n')
    print(json.dumps(summary), flush=True)
    return 1 if counts['errors'] else 0


def make_plan(root, report, output):
    rows = [json.loads(line) for line in report.read_text(encoding='utf8').splitlines()]
    if not rows or rows[0].get('type') != 'start' or rows[-1].get('type') != 'end':
        raise ValueError('audit is incomplete; rerun it before planning')
    if Path(rows[0]['root']).resolve() != root.resolve():
        raise ValueError('audit library root differs')
    entries = []
    for row in rows:
        if row.get('type') != 'file' or not row['remove_keys']:
            continue
        path = contained(root, row['path'])
        current = inspect(path, root)
        if current['dates'] != row['dates']:
            raise ValueError('date tags changed since audit: ' + row['path'])
        entries.append(dict(path=row['path'], sha256=digest(path), dates=row['dates'], remove_keys=row['remove_keys']))
    payload = dict(schema=1, root=str(root.resolve()), created=utc(), action='remove-invalid-placeholder-dates',
                   note='Only invalid year-0/1 fields are removed. Valid dates and file paths are untouched.', entries=entries)
    with output.open('x', encoding='utf8') as stream:
        json.dump(payload, stream, indent=2, ensure_ascii=False)
    print(f'Plan: {len(entries)} files; review {output} before apply', flush=True)


def apply_plan(root, plan_path, journal_path, evidence_client=None):
    with library_lock(root):
        return _apply_plan(root, plan_path, journal_path, evidence_client)


def _apply_plan(root, plan_path, journal_path, evidence_client=None):
    plan = json.loads(plan_path.read_text(encoding='utf8'))
    evidence = plan.get('schema') == 2 and plan.get('action') == 'repair-dates-from-evidence'
    if not (evidence or (plan.get('schema') == 1 and plan.get('action') == 'remove-invalid-placeholder-dates')) or Path(plan['root']).resolve() != root.resolve():
        raise ValueError('wrong plan schema, action or root')
    if evidence:
        from . import date_evidence
        from .catalog import MusicBrainzClient
        evidence_client=evidence_client or MusicBrainzClient()
    evidence_cache={}
    # Full preflight before the first write; never silently follow a moved file.
    entries = plan['entries']
    if not entries:
        print('No verified date changes selected; no files were changed.')
        return
    paths = [contained(root, e['path']) for e in entries]
    if len(set(paths)) != len(paths):
        raise ValueError('duplicate plan paths')
    previous = {}
    if journal_path.exists():
        for line in journal_path.read_text(encoding='utf8').splitlines():
            row = json.loads(line)
            if row.get('plan_sha256') != digest(plan_path):
                raise ValueError('journal belongs to a different plan')
            previous[row['path']] = row
        if not set(previous).issubset(e['path'] for e in entries):
            raise ValueError('journal belongs to a different plan')
    for entry, path in zip(entries, paths):
        prior = previous.get(entry['path'])
        if prior and prior['original_sha256'] != entry['sha256']:
            raise ValueError('journal belongs to a different plan')
        if prior and prior['state'] == 'applied':
            if digest(path) != prior['updated_sha256']:
                raise ValueError('file changed after repair: ' + entry['path'])
            continue
        current = inspect(path, root)
        if digest(path) != entry['sha256'] or current['dates'] != entry['dates']:
            raise ValueError('file changed since plan: ' + entry['path'])
        if not (entry['remove_keys'] or (evidence and entry.get('targets'))) or not set(entry['remove_keys']).issubset(current['remove_keys']):
            raise ValueError('plan attempts to remove a valid or unverified date')
        if evidence:
            date_evidence.validate_entry(entry,evidence_client,evidence_cache)
        if path.stat().st_nlink > 1:
            raise ValueError('hardlinked file requires separate review: ' + entry['path'])
    backups = journal_path.with_suffix(journal_path.suffix + '.backups')
    backups.mkdir(parents=True, exist_ok=True)
    staged={}
    # Validate all replacement formats on temporary copies before touching any
    # original. The files and SQLite-free tag backend remain local to this run.
    if evidence:
        for index,(entry,path) in enumerate(zip(entries,paths)):
            if previous.get(entry['path'],{}).get('state')=='applied':
                continue
            backup=backups/str(index)
            if backup.exists():
                if backup.is_symlink() or digest(backup)!=entry['sha256']:
                    raise ValueError('original backup differs; refusing to overwrite it')
            else:
                shutil.copy2(path,backup)
            fd,name=tempfile.mkstemp(prefix='date-stage-',suffix=path.suffix,dir=backups)
            os.close(fd);stage=Path(name);shutil.copy2(backup,stage)
            protected=date_evidence.protected_metadata(backup)
            date_evidence.write_dates(stage,entry)
            date_evidence.verify_write(stage,entry,protected)
            staged[entry['path']]=(stage,protected)
    with journal_path.open('a' if journal_path.exists() else 'x', encoding='utf8') as journal:
        for index, (entry, path) in enumerate(zip(entries, paths)):
            if previous.get(entry['path'], {}).get('state') == 'applied':
                print('ALREADY_APPLIED ' + entry['path'], flush=True)
                continue
            before = path.stat()
            backup = backups / str(index)
            if backup.exists():
                if backup.is_symlink() or digest(backup)!=entry['sha256']:
                    raise ValueError('original backup differs; refusing to overwrite it')
            else:
                shutil.copy2(path, backup)
            if digest(backup) != entry['sha256'] or digest(path) != entry['sha256']:
                raise ValueError('file changed while backing up: ' + entry['path'])
            record = dict(path=entry['path'], backup=str(backup.resolve()), original_sha256=entry['sha256'],
                          root=str(root.resolve()), plan_sha256=digest(plan_path),
                          mtime_ns=before.st_mtime_ns, atime_ns=before.st_atime_ns, state='prepared')
            begin_tag_write(root,journal_path,'embedded dates')
            journal.write(json.dumps(record) + '\n'); journal.flush(); os.fsync(journal.fileno())
            try:
                if evidence:
                    _,protected=staged[entry['path']]
                    date_evidence.write_dates(path,entry)
                    os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
                    date_evidence.verify_write(path,entry,protected)
                    record.update(state='applied',updated_sha256=digest(path))
                    journal.write(json.dumps(record)+'\n');journal.flush();os.fsync(journal.fileno())
                    try:
                        staged[entry['path']][0].unlink()
                    except OSError:
                        print('Temporary staged copy retained for review: '+str(staged[entry['path']][0]),flush=True)
                    print('APPLIED reviewed date fields '+entry['path'],flush=True)
                    continue
                audio = File(path)
                unrelated = {str(k): repr(v) for k, v in audio.tags.items() if str(k) not in entry['remove_keys']}
                for key in entry['remove_keys']:
                    del audio.tags[key]
                audio.save()
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                after = inspect(path, root)
                expected = {k: v for k, v in entry['dates'].items() if k not in entry['remove_keys']}
                if after['dates'] != expected:
                    raise ValueError('date verification failed')
                saved = File(path)
                if {str(k): repr(v) for k, v in (saved.tags or {}).items()} != unrelated:
                    raise ValueError('unrelated metadata changed; restoring backup')
            except Exception:
                shutil.copy2(backup, path)
                raise
            record.update(state='applied', updated_sha256=digest(path))
            journal.write(json.dumps(record) + '\n'); journal.flush(); os.fsync(journal.fileno())
            print('APPLIED ' + entry['path'], flush=True)
    finish_tag_write(root,journal_path)


def rollback(root, journal_path):
    with library_lock(root):
        return _rollback(root, journal_path)


def _rollback(root, journal_path):
    latest = {}
    for line in journal_path.read_text(encoding='utf8').splitlines():
        row = json.loads(line)
        if Path(row['root']).resolve() != root.resolve():
            raise ValueError('journal library root differs')
        latest[row['path']] = row
    for relative, row in latest.items():
        path = contained(root, relative)
        if digest(path) == row['original_sha256']:
            continue  # Already rolled back.
        if row['state'] != 'applied':
            raise ValueError('interrupted write requires manual backup review: ' + relative)
        if digest(path) != row['updated_sha256'] or path.stat().st_nlink > 1:
            raise ValueError('file changed after repair: ' + relative)
        backup = Path(row['backup'])
        if digest(backup) != row['original_sha256']:
            raise ValueError('backup changed: ' + relative)
        begin_tag_write(root,journal_path,'tag rollback')
        shutil.copy2(backup, path)
        os.utime(path, ns=(row['atime_ns'], row['mtime_ns']))
        print('RESTORED ' + relative, flush=True)
    if latest:
        finish_tag_write(root,journal_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,default=Path('.env'))
    parser.add_argument('--library-root', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    scan = sub.add_parser('audit'); scan.add_argument('--output', type=Path, required=True)
    plan = sub.add_parser('plan'); plan.add_argument('--audit', type=Path, required=True); plan.add_argument('--output', type=Path, required=True)
    plan.add_argument('--lookup-dates',action='store_true',help='Use exact-release catalog evidence for invalid date replacements')
    plan.add_argument('--fill-missing',action='store_true',help='Also propose missing date fields from verified evidence (requires --lookup-dates)')
    plan.add_argument('--apple-review',action='store_true',help='Include read-only storefront date/track-list context; never uses it as write authority')
    plan.add_argument('--country',default='us',help='Apple storefront country (default: us)')
    apply = sub.add_parser('apply'); apply.add_argument('--plan', type=Path, required=True); apply.add_argument('--journal', type=Path, required=True)
    apply.add_argument('--check-lidarr',action='store_true',help='Require Lidarr tag writing disabled and File Date=None before applying')
    undo = sub.add_parser('rollback'); undo.add_argument('--journal', type=Path, required=True)
    args = parser.parse_args(argv)
    load_dotenv(args.env_file)
    root = args.library_root.resolve(strict=True)
    if args.command == 'audit':
        return audit(root, args.output)
    if args.command == 'plan':
        if args.fill_missing and not args.lookup_dates:
            parser.error('--fill-missing requires --lookup-dates')
        if args.apple_review and not args.lookup_dates:
            parser.error('--apple-review requires --lookup-dates')
        if args.lookup_dates:
            from .date_evidence import make_plan as evidence_plan
            evidence_plan(root,args.audit,args.output,fill_missing=args.fill_missing,apple_review=args.apple_review,country=args.country)
        else:
            make_plan(root, args.audit, args.output)
    elif args.command == 'apply':
        if args.check_lidarr:
            from .lidarr import from_env
            client=from_env()
            if str(client.get('config/metadataprovider').get('writeAudioTags','')).casefold()!='no' or str(client.get('config/mediamanagement').get('fileDate','')).casefold()!='none':
                parser.error('set Lidarr Tag Audio Files=Never and File Date=None before date repair')
        apply_plan(root, args.plan, args.journal)
    else:
        rollback(root, args.journal)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
