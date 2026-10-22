"""Plan scoped, in-place matching of Lidarr unmapped files; apply only reviewed plans."""
import argparse
from collections import Counter
import json
from pathlib import Path, PurePosixPath
import time
from mutagen import MutagenError

from dotenv import load_dotenv

from .dates import contained, digest, utc
from .lidarr import from_env
from .locking import library_lock
from .metadata import identify
from .playlists import hash_json, normalized, save
from .subsonic import ApiError


def local_path(root, remote, remote_root):
    remote, remote_root = PurePosixPath(remote), PurePosixPath(remote_root)
    if not remote.is_absolute() or not remote.is_relative_to(remote_root):
        raise ValueError('file outside configured remote music root')
    return contained(root, remote.relative_to(remote_root).as_posix())


def safety(client):
    metadata = client.get('config/metadataprovider')
    media = client.get('config/mediamanagement')
    # ExistingFile imports call WriteTags(newDownload:false); NewFiles also returns
    # without writing. AllFiles/Sync could overwrite Beets metadata and are refused.
    if str(metadata.get('writeAudioTags', '')).casefold() not in ('no', 'newfiles'):
        raise ValueError('Lidarr audio-tag writing must be disabled for existing files before in-place mapping')
    if str(media.get('fileDate', '')).casefold() != 'none':
        raise ValueError('Lidarr File Date must be None before in-place mapping')


def album_identity(album):
    return dict(id=album['id'], foreignAlbumId=album['foreignAlbumId'], artistId=album['artistId'],
                releases=[{k: r.get(k) for k in ('id', 'foreignReleaseId', 'monitored')} for r in album.get('releases', [])])


def select_track(tags, tracks):
    if not tags.get('title') or tags['tracknumber'] < 1 or tags['discnumber'] < 1 or tags['duration_ms'] <= 0:
        raise ValueError('complete title, position and duration are required')
    matches = []
    for track in tracks:
        if normalized(track.get('title', '')) != normalized(tags.get('title', '')):
            continue
        if int(track.get('mediumNumber', 0)) != tags['discnumber'] or str(track.get('trackNumber', '')) != str(tags['tracknumber']):
            continue
        if not track.get('duration') or abs(int(track['duration']) - tags['duration_ms']) > 2500:
            continue
        if tags.get('recording_mbid') and tags['recording_mbid'] != track.get('foreignRecordingId'):
            continue
        if tags.get('release_track_mbid') and tags['release_track_mbid'] != track.get('foreignTrackId'):
            continue
        matches.append(track)
    if len(matches) != 1:
        raise ValueError('no unique edition/position/title/duration match')
    if matches[0].get('hasFile') or matches[0].get('trackFileId'):
        raise ValueError('target track already has a file; duplicate review required')
    return matches[0]


def plan(client, root, scope, remote_root='/music'):
    root = root.resolve(strict=True)
    scope_path = PurePosixPath(scope)
    local_path(root, scope, remote_root)
    safety(client)
    artists = client.get('artist')
    rows, release_cache, album_cache, used = [], {}, {}, {}
    for file in client.get('trackfile', unmapped='true'):
        remote = PurePosixPath(file['path'])
        if remote != scope_path and not remote.is_relative_to(scope_path):
            continue
        row = dict(path=file['path'], source_id=file['id'], status='review')
        try:
            path = local_path(root, file['path'], remote_root)
            tags = identify(path)
            row['tags'] = tags
            row.update(sha256=digest(path), size=path.stat().st_size, mtime_ns=path.stat().st_mtime_ns)
            if not tags.get('release_mbid') or not tags.get('group_mbid'):
                raise ValueError('exact MusicBrainz release and release-group tags are required')
            if tags['group_mbid'] not in album_cache:
                album_cache[tags['group_mbid']] = client.get('album', foreignAlbumId=tags['group_mbid'])
            local_albums = [a for a in album_cache[tags['group_mbid']] if a['foreignAlbumId'] == tags['group_mbid']]
            if not local_albums:
                row.update(status='catalog_missing', reason='existing release group must be added to Lidarr before matching')
                lookup = client.get('album/lookup', term='lidarr:' + tags['group_mbid'])
                exact = [a for a in lookup if a.get('foreignAlbumId') == tags['group_mbid'] and any(r.get('foreignReleaseId') == tags['release_mbid'] for r in a.get('releases', []))]
                if len(exact) == 1:
                    candidate = exact[0]
                    foreign_artist = (candidate.get('artist') or {}).get('foreignArtistId')
                    existing_artists = [a for a in artists if a['foreignArtistId'] == foreign_artist and remote.is_relative_to(PurePosixPath(a['path']))]
                    if len(existing_artists) == 1:
                        row['catalog_proposal'] = dict(group_mbid=tags['group_mbid'], release_mbid=tags['release_mbid'],
                            artist_id=existing_artists[0]['id'], artist_mbid=foreign_artist, artist_path=existing_artists[0]['path'], title=candidate['title'])
                    else:
                        row.update(status='review', reason='catalog artist ID does not match the existing artist folder; review release/release-group credits before adding',
                                   catalog_artist_mbid=foreign_artist,
                                   review_links=dict(release='https://musicbrainz.org/release/' + tags['release_mbid'],
                                                     release_group='https://musicbrainz.org/release-group/' + tags['group_mbid']))
                rows.append(row); continue
            if len(local_albums) != 1:
                raise ValueError('multiple local album matches')
            album = local_albums[0]
            artist = next(a for a in artists if a['id'] == album['artistId'])
            if not remote.is_relative_to(PurePosixPath(artist['path'])):
                raise ValueError('file is outside the matched artist folder; move requires separate review')
            relative_to_artist = remote.relative_to(PurePosixPath(artist['path']))
            if len(relative_to_artist.parts) < 2:
                raise ValueError('loose artist-level file needs album-folder review before metadata processing')
            if tags.get('album_artist_mbid') and tags['album_artist_mbid'] != artist['foreignArtistId']:
                raise ValueError('tagged album artist differs from Lidarr')
            releases = [r for r in album.get('releases', []) if r.get('foreignReleaseId') == tags['release_mbid']]
            if len(releases) != 1:
                raise ValueError('exact tagged release is not available locally')
            release = releases[0]
            if not release.get('monitored') and client.get('trackfile', albumId=album['id']):
                raise ValueError('another edition already has files; release switching requires review')
            if release['id'] not in release_cache:
                release_cache[release['id']] = client.get('track', albumReleaseId=release['id'])
            track = select_track(tags, release_cache[release['id']])
            if track.get('albumId') != album['id'] or track.get('artistId') != artist['id']:
                raise ValueError('track belongs to a different artist or album')
            if not file.get('quality'):
                raise ValueError('Lidarr quality information unavailable')
            row.update(status='ready',
                album_folder=str(PurePosixPath(artist['path']) / relative_to_artist.parts[0]),
                artist=dict(id=artist['id'], foreignArtistId=artist['foreignArtistId'], path=artist['path']),
                album=album_identity(album), track=track,
                command=dict(path=file['path'], artistId=artist['id'], albumId=album['id'], albumReleaseId=release['id'],
                    trackIds=[track['id']], quality=file['quality'], releaseGroup=file.get('releaseGroup', ''),
                    indexerFlags=file.get('indexerFlags', 0), disableReleaseSwitching=False))
            used.setdefault(track['id'], []).append(row)
        except (ValueError, OSError, ApiError, MutagenError, StopIteration) as exc:
            row.update(status='review', reason=str(exc) or 'missing artist/catalog data')
        rows.append(row)
    for group in used.values():
        if len(group) > 1:
            for row in group:
                row.update(status='review', reason='multiple source files match the same target track; duplicate review required')
    editions = {}
    for row in rows:
        if row['status'] == 'ready':
            editions.setdefault(row['command']['albumId'], []).append(row)
    for group in editions.values():
        if len({r['command']['albumReleaseId'] for r in group}) > 1:
            for row in group:
                row.update(status='review', reason='multiple editions of the same album in one plan require separate review')
    return dict(schema=1, kind='lidarr-in-place', server=client.url, root=str(root), remote_root=remote_root,
                scope=scope, created=utc(), rows=rows, summary=dict(Counter(r['status'] for r in rows)),
                note='Mapping does not move or retag files. Catalog additions and metadata processing are separate reviewed steps.')


def apply(client, root, payload, journal, wait_seconds=60):
    if payload.get('schema') != 1 or payload.get('kind') != 'lidarr-in-place' or payload['server'] != client.url or Path(payload['root']).resolve() != root.resolve():
        raise ValueError('plan server/root/schema differs')
    ready = [r for r in payload['rows'] if r['status'] == 'ready']
    if not ready:
        raise ValueError('plan has no confidently matched files')
    journal.parent.mkdir(parents=True, exist_ok=True)
    with library_lock(root):
        safety(client)
        record = json.loads(journal.read_text(encoding='utf8')) if journal.exists() else {}
        signature = hash_json(payload)
        if record and record.get('plan') != signature:
            raise ValueError('journal belongs to a different plan')
        # Revalidate the entire proposal before issuing the first command.
        fresh = plan(client, root, payload['scope'], payload['remote_root']) if not record or record.get('status') == 'prepared' else None
        if fresh:
            by_path = {r['path']: r for r in fresh['rows']}
            for row in ready:
                current = by_path.get(row['path'], {})
                if current.get('status') != 'ready' or any(current.get(k) != row.get(k) for k in ('sha256', 'mtime_ns', 'source_id', 'command', 'album', 'track', 'artist')):
                    raise ValueError('import candidate changed since preview: ' + row['path'])
        for row in ready:
            path = local_path(root, row['path'], payload['remote_root'])
            if digest(path) != row['sha256'] or path.stat().st_mtime_ns != row['mtime_ns']:
                raise ValueError('file changed since preview: ' + row['path'])
        if not record:
            record = dict(plan=signature, status='prepared', created=utc(), files=[r['path'] for r in ready])
            save(journal, record, exclusive=True)
        if record['status'] == 'prepared':
            record.update(status='pending', updated=utc()); save(journal, record)
            result = client.post('command', dict(name='ManualImport', importMode='Copy', replaceExistingFiles=False, files=[r['command'] for r in ready]))
            if not result.get('id'):
                raise ApiError('Lidarr returned no command ID; reconcile the pending operation before retrying')
            record.update(command_id=result['id']); save(journal, record)
        if record.get('command_id'):
            deadline = time.monotonic() + wait_seconds
            while True:
                command = client.get('command/' + str(record['command_id']))
                if command.get('status') in ('completed', 'failed', 'aborted', 'cancelled'):
                    record['command_status'] = command['status']; break
                if time.monotonic() >= deadline:
                    save(journal, record)
                    print('Import is still queued/running; rerun apply with the same plan and journal to check it.')
                    return 2
                time.sleep(min(2, max(0, deadline - time.monotonic())))
        verified, unresolved = [], []
        for row in ready:
            track = client.get('track/' + str(row['track']['id']))
            mapped = client.get('trackfile/' + str(track['trackFileId'])) if track.get('trackFileId') else {}
            path = local_path(root, row['path'], payload['remote_root'])
            if mapped.get('path') == row['path'] and mapped.get('albumId') == row['command']['albumId'] and digest(path) == row['sha256'] and path.stat().st_mtime_ns == row['mtime_ns']:
                verified.append(row['path'])
            else:
                unresolved.append(row['path'])
        record.update(status='complete' if not unresolved else 'needs_review', verified=verified, unresolved=unresolved, updated=utc())
        save(journal, record)
        if unresolved:
            raise ValueError('some imports could not be verified; inspect the journal; no command was retried')
        # Existing-file imports do not send regular download notifications.
        handoff = dict(schema=1, server=client.url, remote_root=payload['remote_root'], plan=signature,
            status='awaiting_metadata_review', albums=list({str(r['command']['albumId']):dict(album_id=r['command']['albumId'],
                artist_id=r['command']['artistId'], album_folder=r['album_folder'],
                release_mbid=r['tags']['release_mbid'], group_mbid=r['tags']['group_mbid'],
                files=[{k:x[k] for k in ('path','sha256','mtime_ns')} for x in ready if x['command']['albumId'] == r['command']['albumId']],
                paths=[x['path'] for x in ready if x['command']['albumId'] == r['command']['albumId']]) for r in ready}.values()))
        handoff_path = journal.with_suffix('.metadata-queue.json')
        if not handoff_path.exists():
            save(handoff_path, handoff, exclusive=True)
        elif json.loads(handoff_path.read_text(encoding='utf8')).get('plan') != signature:
            raise ValueError('metadata handoff belongs to a different plan')
        print(f'Verified {len(verified)} in-place imports; metadata handoff saved separately.')
        return 0


def add_catalog(client, root, payload, journal):
    if payload.get('schema') != 1 or payload.get('kind') != 'lidarr-in-place' or payload['server'] != client.url or Path(payload['root']).resolve() != root.resolve():
        raise ValueError('plan server/root/schema differs')
    proposals = {r['catalog_proposal']['group_mbid']: r['catalog_proposal'] for r in payload['rows'] if r.get('catalog_proposal')}
    if not proposals:
        raise ValueError('no verified existing-catalog proposals in this plan')
    journal.parent.mkdir(parents=True, exist_ok=True)
    with library_lock(root):
        safety(client)
        signature = hash_json(payload)
        record = json.loads(journal.read_text(encoding='utf8')) if journal.exists() else dict(plan=signature, groups={})
        if record.get('plan') != signature:
            raise ValueError('journal belongs to a different plan')
        exclusions = client.get('importlistexclusion')
        excluded = {str(x.get('foreignId', '')) for x in exclusions}
        for group, proposal in proposals.items():
            if group in excluded or proposal['artist_mbid'] in excluded:
                raise ValueError('catalog entry is excluded in Lidarr; review the exclusion manually')
            artist = client.get('artist/' + str(proposal['artist_id']))
            if artist['foreignArtistId'] != proposal['artist_mbid'] or artist['path'] != proposal['artist_path']:
                raise ValueError('artist changed since preview')
            for row in payload['rows']:
                if row.get('catalog_proposal', {}).get('group_mbid') == group:
                    path = local_path(root, row['path'], payload['remote_root'])
                    if digest(path) != row['sha256']:
                        raise ValueError('file changed since catalog preview')
            existing = client.get('album', foreignAlbumId=group)
            if len(existing) == 1 and existing[0]['artistId'] == proposal['artist_id']:
                record['groups'][group] = dict(status='present', album_id=existing[0]['id'])
                save(journal, record); continue
            if existing:
                raise ValueError('unexpected existing album state')
            if record['groups'].get(group, {}).get('status') == 'pending':
                raise ValueError('catalog addition outcome unknown; inspect Lidarr before retrying')
            lookup = client.get('album/lookup', term='lidarr:' + group)
            candidates = [a for a in lookup if a.get('foreignAlbumId') == group and
                          (a.get('artist') or {}).get('foreignArtistId') == proposal['artist_mbid'] and
                          any(r.get('foreignReleaseId') == proposal['release_mbid'] for r in a.get('releases', []))]
            if len(candidates) != 1 or candidates[0]['title'] != proposal['title']:
                raise ValueError('catalog candidate changed since preview')
            # Adding an unmonitored album to an existing artist only; never add artists or search.
            request = dict(candidates[0], artist=dict(artist, addOptions=dict(searchForMissingAlbums=False)),
                artistId=artist['id'], monitored=False, addOptions=dict(searchForNewAlbum=False))
            request.pop('id', None)
            record['groups'][group] = dict(status='pending'); save(journal, record)
            added = client.post('album', request)
            if added.get('foreignAlbumId') != group or added.get('artistId') != artist['id'] or not added.get('id') or added.get('monitored'):
                raise ApiError('catalog addition was not verified; inspect Lidarr and the journal')
            record['groups'][group] = dict(status='added', album_id=added['id']); save(journal, record)
        print('Catalog proposals reconciled. Wait for Lidarr metadata refresh, then generate a fresh import plan.')
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--library-root', type=Path, required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    preview = sub.add_parser('plan'); preview.add_argument('--path', required=True); preview.add_argument('--remote-root', default='/music'); preview.add_argument('--output', type=Path, required=True)
    execute = sub.add_parser('apply'); execute.add_argument('--plan', type=Path, required=True); execute.add_argument('--journal', type=Path, required=True); execute.add_argument('--wait-seconds', type=int, default=60)
    catalog = sub.add_parser('add-catalog'); catalog.add_argument('--plan', type=Path, required=True); catalog.add_argument('--journal', type=Path, required=True)
    refresh_plan=sub.add_parser('refresh-plan');refresh_plan.add_argument('--album-id',type=int,required=True);refresh_plan.add_argument('--remote-root',default='/music');refresh_plan.add_argument('--output',type=Path,required=True)
    refresh=sub.add_parser('refresh');refresh.add_argument('--plan',type=Path,required=True);refresh.add_argument('--journal',type=Path,required=True)
    args = parser.parse_args(argv)
    load_dotenv(args.env_file)
    try:
        root = args.library_root.resolve(strict=True); client = from_env()
        if args.action == 'plan':
            payload = plan(client, root, args.path, args.remote_root)
            save(args.output, payload, exclusive=True); print(json.dumps(payload['summary']))
            return 0
        if args.action == 'add-catalog':
            return add_catalog(client, root, json.loads(args.plan.read_text(encoding='utf8')), args.journal)
        if args.action in ('refresh-plan','refresh'):
            from . import reconciliation
            if args.action=='refresh-plan':
                payload=reconciliation.plan(client,root,args.album_id,args.remote_root)
                save(args.output,payload,exclusive=True);reconciliation.write_html(payload,args.output.with_suffix('.html'));print(json.dumps(dict(status=payload['status'],notes=payload['notes'])));return 0
            return reconciliation.reconcile(client,root,json.loads(args.plan.read_text(encoding='utf8')),args.journal)
        if args.wait_seconds < 0 or args.wait_seconds > 60:
            raise ValueError('--wait-seconds must be between 0 and 60; resume to poll longer')
        return apply(client, root, json.loads(args.plan.read_text(encoding='utf8')), args.journal, args.wait_seconds)
    except (ValueError, OSError, ApiError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
