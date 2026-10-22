"""Approve and process metadata handoffs; periodic audits remain read-only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import time
import uuid

from dotenv import load_dotenv
from mutagen import File, MutagenError

from . import dates, library_review
from .lidarr import from_env
from .metadata import CORE_TAG_FIELDS
from .locking import library_lock
from .playlists import hash_json, save
from .subsonic import ApiError
from .unmapped import album_identity, local_path

STEPS = ('beets-preview', 'replaygain', 'lyrics')
TERMINAL = ('complete', 'complete_with_review', 'needs_review')
SUPPORTED_GAIN = {'.flac', '.mp3', '.m4a', '.aac', '.opus'}


class ReviewRequired(ValueError):
    pass


def snapshot(path):
    audio = File(path)
    if audio is None:
        raise ValueError('unreadable audio: ' + str(path))
    tags = {str(k): repr(v) for k, v in (audio.tags or {}).items() if
            'replaygain' not in str(k).casefold().replace('_', '') and 'r128' not in str(k).casefold()}
    pictures = [hashlib.sha256(p.write()).hexdigest() for p in getattr(audio, 'pictures', [])]
    non_lyrics = {k:v for k,v in tags.items() if not any(marker in k.casefold() for marker in ('lyrics', 'uslt', 'sylt', '©lyr'))}
    return dict(sha256=dates.digest(path), mtime_ns=path.stat().st_mtime_ns,
                unrelated=hash_json(dict(tags=tags, pictures=pictures)), non_lyrics=hash_json(dict(tags=non_lyrics, pictures=pictures)))


def physical_files(folder, expected):
    actual = []
    for path in folder.rglob('*'):
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('album contains a symlink/junction; review separately')
        if path.is_file() and path.suffix.lower() in dates.AUDIO:
            actual.append(path.resolve())
    if len(actual) != len(expected) or set(actual) != {p.resolve() for p in expected}:
        raise ReviewRequired('album folder contains extra/unmapped audio; duplicate/edition review required')


def mapped_files(client, album_id):
    rows = client.get('trackfile', albumId=album_id)
    if not rows or any(r.get('albumId') != album_id for r in rows):
        raise ValueError('album file assignments are missing or inconsistent')
    return sorted([dict(id=r['id'], path=r['path'], albumId=r['albumId']) for r in rows], key=lambda r: r['path'])


def approve(client, root, handoff_path, state, steps, profile=''):
    handoff = json.loads(handoff_path.read_text(encoding='utf8'))
    if handoff.get('schema') != 1 or handoff.get('server') != client.url or handoff.get('status') != 'awaiting_metadata_review':
        raise ValueError('handoff is not a verified import handoff for this server')
    if any(not all(key in a for key in ('release_mbid', 'group_mbid', 'files', 'album_folder')) for a in handoff.get('albums', [])):
        raise ValueError('handoff lacks edition/file evidence; regenerate it from a verified import')
    remote_root = handoff.get('remote_root', '/music')
    steps = [s for s in STEPS if s in steps]
    if not steps:
        raise ValueError('select at least one metadata step')
    state.mkdir(parents=True, exist_ok=True)
    with library_lock(state), library_lock(root):
        count = 0
        for group in handoff['albums']:
            existing = [json.loads(p.read_text(encoding='utf8')) for p in (state / 'jobs').glob('*.json')]
            if any(j.get('id') == hash_json(j.get('manifest')) and
                   j['manifest'].get('source_plan') == handoff['plan'] and
                   j['manifest'].get('album', {}).get('id') == group['album_id'] and
                   j['manifest'].get('server') == client.url and j['manifest'].get('root') == str(root.resolve()) and
                   j['manifest'].get('steps') == steps and j['manifest'].get('profile') == profile for j in existing):
                continue
            album = client.get('album/' + str(group['album_id']))
            if album['artistId'] != group['artist_id'] or album['foreignAlbumId'] != group['group_mbid']:
                raise ValueError('album identity changed since import')
            selected = [r for r in album['releases'] if r.get('monitored')]
            if len(selected) != 1 or selected[0]['foreignReleaseId'] != group['release_mbid']:
                raise ValueError('album edition changed since import')
            folder = local_path(root, group['album_folder'], remote_root)
            rows = mapped_files(client, group['album_id'])
            by_path = {r['path']: r for r in rows}
            for original in group['files']:
                path = local_path(root, original['path'], remote_root)
                if original['path'] not in by_path or dates.digest(path) != original['sha256'] or path.stat().st_mtime_ns != original['mtime_ns']:
                    raise ValueError('imported file changed before metadata approval')
            snapshots = {}
            for row in rows:
                path = local_path(root, row['path'], remote_root)
                if not path.is_relative_to(folder):
                    raise ValueError('mapped album spans multiple physical folders; review it separately')
                snapshots[row['path']] = snapshot(path)
            physical_files(folder, [local_path(root, p, remote_root) for p in snapshots])
            manifest = dict(schema=1, server=client.url, root=str(root.resolve()), remote_root=remote_root, profile=profile,
                source_plan=handoff['plan'], steps=steps, album=album_identity(album),
                album_folder=group['album_folder'], release_mbid=group['release_mbid'], files=rows, snapshots=snapshots)
            identity = hash_json(manifest)
            path = state / 'jobs' / (identity + '.json')
            if not path.exists():
                save(path, dict(id=identity, manifest=manifest, expected=snapshots, created=dates.utc(), status='pending',
                                attempts=0, next_run=0, results={}), exclusive=True)
                count += 1
        print(f'Approved {count} new album job(s).')
        return count


def execute(command, log, env, timeout=1800, capture=False):
    command = [shutil.which(command[0], path=env.get('PATH')) or command[0], *command[1:]]
    with log.open('a', encoding='utf8') as stream:
        stream.write('\nSTART ' + dates.utc() + '\n'); stream.flush()
        result = subprocess.run(command, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else stream, stderr=stream,
            text=True, encoding='utf8', errors='replace', env=env, timeout=timeout, shell=False)
        stream.write('END ' + dates.utc() + ' status=' + str(result.returncode) + '\n')
    if result.returncode:
        raise ValueError('metadata command failed; see ' + str(log))
    return result.stdout or ''


def beets_preview(manifest, root, workdir, env, run):
    folder = local_path(root, manifest['album_folder'], manifest['remote_root'])
    scratch = workdir / ('beets-' + uuid.uuid4().hex); scratch.mkdir()
    config = scratch / 'config.json'
    # JSON is also valid YAML; paths remain literal on Windows and Linux.
    save(config, {'plugins': ['musicbrainz', 'export'], 'directory': str(folder),
        'import': dict(write=False, copy=False, move=False, resume=False, incremental=False, quiet_fallback='skip')})
    env = dict(env, BEETSDIR=str(scratch))
    command = ['beet', '-c', str(config), '-l', str(scratch / 'preview.db')]
    run(command + ['import', '-q', '-C', '-M', '-W', '-P', '-I', '--flat', '--search-id', manifest['release_mbid'], str(folder)],
        workdir / 'beets-preview.log', env)
    output = run(command + ['list', '-f', '$mb_albumid'], workdir / 'beets-preview.log', env, capture=True)
    matched = output.splitlines()
    export = workdir / 'beets-export.json'
    run(command + ['export', '-l', '-f', 'json', '-i', ','.join(('path',*CORE_TAG_FIELDS)), '-o', str(export)], workdir / 'beets-preview.log', env)
    rows = json.loads(export.read_text(encoding='utf8'))
    complete = isinstance(rows,list) and len(rows)==len(manifest['files']) and all(r.get('mb_albumid')==manifest['release_mbid'] for r in rows)
    return dict(status='review_ready' if complete and len(matched) == len(manifest['files']) and all(x.strip() == manifest['release_mbid'] for x in matched) else 'review_unmatched',
                export=str(export),
                log=str(workdir / 'beets-preview.log'), note='No tags or paths were changed; Beets tag application still requires a separate reviewed operation.')


def gain_complete(path):
    audio = File(path)
    if audio is None:
        raise ValueError('unreadable ReplayGain input')
    keys = [str(k).upper().replace('-', '_') for k in (audio.tags or {})]
    return all(any(name in key for key in keys) for name in
               ('REPLAYGAIN_TRACK_GAIN', 'REPLAYGAIN_TRACK_PEAK', 'REPLAYGAIN_ALBUM_GAIN', 'REPLAYGAIN_ALBUM_PEAK'))


def work(client, root, state, hook_dir, config_dir, allowed=STEPS, profile='', run=execute, now=time.time):
    state.mkdir(parents=True, exist_ok=True)
    with library_lock(state), library_lock(root):
        for path in sorted((state / 'jobs').glob('*.json')):
            job = json.loads(path.read_text(encoding='utf8'))
            if job['status'] in TERMINAL or job.get('next_run', 0) > now():
                continue
            manifest = job['manifest']
            if hash_json(manifest) != job['id'] or job['id'] != path.stem or manifest['server'] != client.url or Path(manifest['root']).resolve() != root.resolve():
                job.update(status='needs_review', error='job identity/server/root differs')
                save(path, job)
                return 1
            if manifest['profile'] != profile or not set(manifest['steps']).issubset(allowed):
                continue  # Disabled configuration pauses the job without discarding it.
            workdir = state / 'runs' / job['id']; workdir.mkdir(parents=True, exist_ok=True)
            active = None
            try:
                if album_identity(client.get('album/' + str(manifest['album']['id']))) != manifest['album'] or mapped_files(client, manifest['album']['id']) != manifest['files']:
                    raise ValueError('album mapping changed after approval')
                current = {p: snapshot(local_path(root, p, manifest['remote_root'])) for p in job['expected']}
                physical_files(local_path(root, manifest['album_folder'], manifest['remote_root']), [local_path(root, p, manifest['remote_root']) for p in job['expected']])
                if job.get('active') == 'replaygain':
                    job['retry_gain'] = True
                for name, previous in job['expected'].items():
                    item = current[name]
                    interrupted_gain = job.get('active') == 'replaygain'
                    if item != previous and not (interrupted_gain and item['unrelated'] == previous['unrelated'] and item['mtime_ns'] == previous['mtime_ns']):
                        raise ValueError('file changed outside the approved operation: ' + name)
                job['expected'] = current
                env = dict(os.environ, LYRICS_CONFIG=str(config_dir), LYRICS_ROOTS=str(root), REPLAYGAIN_MUSIC_ROOT=str(root))
                for step in manifest['steps']:
                    if step in job['results']:
                        continue
                    active = step
                    job.update(status='running', active=step, updated=dates.utc()); save(path, job)
                    before = {p: snapshot(local_path(root, p, manifest['remote_root'])) for p in job['expected']}
                    if step == 'beets-preview':
                        result = beets_preview(manifest, root, workdir, env, run)
                    elif step == 'replaygain':
                        files = [local_path(root, p, manifest['remote_root']) for p in job['expected']]
                        if any(p.suffix.lower() not in SUPPORTED_GAIN for p in files):
                            raise ReviewRequired('unsupported ReplayGain format requires separate review')
                        # Force a whole-album rerun after interruption even if every tag is
                        # present: a partially written album can mix old/new gain values.
                        audit = workdir / 'gain-audit.log'
                        audit.write_text(''.join('PARTIAL\t' + str(p) + '\n' for p in files), encoding='utf8')
                        if not all(gain_complete(p) for p in files) or job.get('retry_gain'):
                            run(['bash', str(hook_dir / 'ReplayGainTagger.bash'), '--repair-audit', str(audit), '--force', '--path', str(local_path(root, manifest['album_folder'], manifest['remote_root']))], workdir / 'replaygain.log', env)
                        if not all(gain_complete(p) for p in files):
                            raise ValueError('ReplayGain command returned without complete track/album tags')
                        result = dict(status='verified', log=str(workdir / 'replaygain.log'))
                    else:
                        run([sys.executable, str(hook_dir / 'python' / 'Lyrics.py'), '--enqueue',
                            str(local_path(root, manifest['album_folder'], manifest['remote_root'])), '--album-id', str(manifest['album']['id']), '--strict-enqueue'], workdir / 'lyrics.log', env)
                        result = dict(status='enqueued', log=str(workdir / 'lyrics.log'), note='Embedding completion is tracked by the lyrics worker.')
                    after = {p: snapshot(local_path(root, p, manifest['remote_root'])) for p in job['expected']}
                    physical_files(local_path(root, manifest['album_folder'], manifest['remote_root']), [local_path(root, p, manifest['remote_root']) for p in job['expected']])
                    if album_identity(client.get('album/' + str(manifest['album']['id']))) != manifest['album'] or mapped_files(client, manifest['album']['id']) != manifest['files']:
                        raise ReviewRequired('album mapping changed while metadata was processing')
                    for name in before:
                        preserve = 'non_lyrics' if step == 'lyrics' else 'unrelated'
                        if after[name]['mtime_ns'] != before[name]['mtime_ns'] or after[name][preserve] != before[name][preserve]:
                            raise ReviewRequired('metadata operation changed unrelated tags or mtime; review required')
                        if step == 'beets-preview' and after[name]['sha256'] != before[name]['sha256']:
                            raise ReviewRequired('read-only preview/enqueue changed an audio file')
                    job['expected'] = after
                    job['results'][step] = result
                    job.pop('active', None); job.pop('retry_gain', None)
                    save(path, job)
                job.update(status='complete_with_review' if 'beets-preview' in manifest['steps'] else 'complete', ended=dates.utc())
                job.pop('error', None)
                save(path, job)
                print(json.dumps(dict(job=job['id'], status=job['status'], results=job['results'])), flush=True)
                return 0
            except (ValueError, OSError, ApiError, MutagenError, subprocess.SubprocessError) as exc:
                job['attempts'] += 1
                job.update(status='needs_review', error=str(exc), updated=dates.utc())
                # ReplayGain retries are safe only when unrelated tags and mtime stayed
                # intact. Retain the original checkpoints until the next validation.
                if active == 'replaygain' and job['attempts'] < 3 and not isinstance(exc, ReviewRequired):
                    job.update(status='retry', retry_gain=True, next_run=now() + 3600)
                save(path, job)
                print(json.dumps(dict(job=job['id'], status=job['status'], error=job['error'])), flush=True)
                return 1
    return 0


def audit(client, root, state, days=7, include_dates=False, clock=time.time,remote_root='/music'):
    if days < 1:
        raise ValueError('audit interval must be positive')
    state.mkdir(parents=True, exist_ok=True)
    with library_lock(state):
        marker = state / 'audit-status.json'
        previous = json.loads(marker.read_text(encoding='utf8')) if marker.exists() else {}
        # Failed or interrupted attempts get a bounded retry, not a week-long
        # successful-audit interval. Preserve the original failure for diagnosis.
        interval = days * 86400 if previous.get('status') in ('complete', 'complete_with_errors') else 3600
        if previous.get('server') == client.url and previous.get('root') == str(root.resolve()) and previous.get('remote_root','/music')==remote_root and clock() - previous.get('attempt', 0) < interval:
            return 0
        started = clock()
        record = dict(attempt=started, started=dates.utc(), status='running', server=client.url, root=str(root.resolve()),remote_root=remote_root)
        save(marker, record)
        output = state / 'reports' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        try:
            files = client.get('trackfile', unmapped='true')
            groups = library_review.group_rows([library_review.make_unmapped(row, root,remote_root) for row in files])
            library_review.write_report(output, groups, client.url)
            errors = sum(bool(f.get('tag_read_error')) for g in groups for f in g['files'])
            date_status = dates.audit(root, output / 'dates.jsonl') if include_dates else 0
            record.update(status='complete_with_errors' if errors or date_status else 'complete',
                          unmapped_files=len(files), album_groups=len(groups), read_errors=errors, output=str(output))
        except Exception as exc:
            record.update(status='failed', error=type(exc).__name__)
            raise
        finally:
            record.update(ended=dates.utc(), elapsed_seconds=round(clock() - started, 2)); save(marker, record)
            print(json.dumps(record), flush=True)
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--library-root', type=Path, required=True)
    parser.add_argument('--state', type=Path, default=Path('logs/maintenance'))
    parser.add_argument('--profile', default='')
    sub = parser.add_subparsers(dest='action', required=True)
    enqueue = sub.add_parser('approve'); enqueue.add_argument('--handoff', type=Path, required=True); enqueue.add_argument('--steps', default='beets-preview,replaygain,lyrics')
    worker = sub.add_parser('work'); worker.add_argument('--hook-dir', type=Path, default=Path('/config/extended')); worker.add_argument('--config-dir', type=Path, default=Path('/config')); worker.add_argument('--steps', default=','.join(STEPS))
    scan = sub.add_parser('audit'); scan.add_argument('--days', type=int, default=7); scan.add_argument('--include-dates', action='store_true')
    scan.add_argument('--remote-root',default='/music')
    args = parser.parse_args(argv); load_dotenv(args.env_file)
    try:
        root = args.library_root.resolve(strict=True); client = from_env()
        if args.action == 'audit':
            return audit(client, root, args.state, args.days, args.include_dates,remote_root=args.remote_root)
        steps = args.steps.split(',')
        if not set(steps).issubset(STEPS):
            raise ValueError('unknown metadata step')
        if args.action == 'approve':
            approve(client, root, args.handoff, args.state, steps, args.profile); return 0
        return work(client, root, args.state, args.hook_dir, args.config_dir, steps, args.profile)
    except (ValueError, OSError, ApiError, MutagenError) as exc:
        parser.exit(1, str(exc) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
