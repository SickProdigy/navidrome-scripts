"""Export Lidarr unmapped files and suspicious dates for manual catalog review."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlencode, quote

import requests
from mutagen import File

from dotenv import load_dotenv
from .catalog import CLIENT, MusicBrainzClient, lucene_quote, normalize, without_trailing_parenthetical


def get_json(session, base, endpoint, **params):
    response = session.get(base.rstrip('/') + '/api/v1/' + endpoint,
                           params=params, timeout=(10, 90))
    response.raise_for_status()
    return response.json()


def read_tags(container_path, library_root,remote_root='/music'):
    relative = PurePosixPath(container_path)
    if not relative.is_relative_to(remote_root):
        raise ValueError('file is outside '+remote_root)
    root = Path(library_root).resolve()
    path = root.joinpath(*relative.relative_to(remote_root).parts)
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('symlink/outside library root')
    audio = File(path)
    if audio is None:
        raise ValueError('unreadable audio format')
    tags = {}
    for key, values in (audio.tags or {}).items():
        name = str(key).casefold().removeprefix('----:com.apple.itunes:')
        name = name.replace(' ', '_')
        aliases = {'tpe1': 'artist', 'tpe2': 'albumartist', 'talb': 'album',
                   'tit2': 'title', '\xa9art': 'artist', 'aart': 'albumartist',
                   '\xa9alb': 'album', '\xa9nam': 'title', '\xa9day': 'date'}
        name = aliases.get(name, name.removeprefix('txxx:'))
        if name not in {'artist', 'albumartist', 'album', 'title', 'date', 'year',
                        'originaldate', 'originalyear', 'musicbrainz_releasegroupid',
                        'musicbrainz_albumid', 'musicbrainz_trackid',
                        'musicbrainz_release_group_id', 'musicbrainz_album_id',
                        'musicbrainz_track_id'}:
            continue
        if hasattr(values, 'text'):
            values = values.text
        if not isinstance(values, (list, tuple)):
            values = [values]
        tags[name] = '; '.join(v.decode('utf8', errors='replace') if isinstance(v, bytes)
                               else str(v) for v in values)
    return tags


def make_unmapped(row, root,remote_root='/music'):
    path = str(row['path'])
    parts = PurePosixPath(path).parts
    tags, error = {}, ''
    try:
        tags = read_tags(path, root,remote_root)
    except Exception as exc:
        # Keep unreadable files visible without guessing their recording identity.
        error = type(exc).__name__ + ': ' + str(exc)
    return dict(kind='unmapped', artist=tags.get('albumartist') or tags.get('artist') or
                (parts[2] if len(parts) > 2 else ''), album=tags.get('album') or '',
                path=path, title=tags.get('title', ''), track_file_id=row.get('id'),
                release_group_mbid=tags.get('musicbrainz_releasegroupid') or
                tags.get('musicbrainz_release_group_id', ''),
                release_mbid=tags.get('musicbrainz_albumid') or tags.get('musicbrainz_album_id', ''),
                reason='Lidarr has not assigned this file to an album/track',
                tag_read_error=error, tags=tags)


def add_date_seeds(rows):
    result = []
    for row in rows:
        year = row.get('year') or 0
        original = row.get('original_year') or 0
        path = str(row.get('path', ''))
        if not (0 < int(year) < 1000 or 0 < int(original) < 1000 or
                re.search(r' \(0*1\)(?:/|$)', path) or
                str(row.get('original_date', '')).startswith('0001')):
            continue
        if path.startswith('Music-Library/'):
            path = '/music/' + path[len('Music-Library/'):]
        result.append(dict(kind='date-review', artist=row.get('album_artist') or row.get('artist', ''),
                           album=row.get('album', ''), path=path, title='',
                           release_group_mbid=row.get('mbz_release_group_id', ''),
                           release_mbid=row.get('mbz_album_id', ''),
                           reason='Suspicious date/year-1 folder in supplied snapshot; verify current state',
                           tag_read_error='', snapshot_dates={k: row.get(k) for k in
                           ['year', 'original_year', 'date', 'original_date']}))
    return result


def group_rows(rows):
    groups = {}
    for row in rows:
        # Distinct physical editions stay distinct even when artist/title match.
        folder = str(PurePosixPath(row['path']).parent)
        if re.fullmatch(r'(?:CD|Disc)[ _-]*\d+', PurePosixPath(folder).name, re.I):
            folder = str(PurePosixPath(folder).parent)
        key = (row['kind'], row['artist'], row['album'], row['release_mbid'], folder)
        group = groups.setdefault(key, {k: row[k] for k in
            ['kind', 'artist', 'album', 'release_group_mbid', 'release_mbid', 'reason']})
        group.setdefault('files', []).append(row)
        group['folder'] = folder
        group['catalog_status'] = 'not checked; unmapped does not establish absence from MusicBrainz'
    return sorted(groups.values(), key=lambda g: (g['kind'] != 'unmapped', g['artist'].casefold(), g['album'].casefold()))


def links(group):
    term = ' '.join(filter(None, [group['artist'], group['album']]))
    result = {'MusicBrainz search': 'https://musicbrainz.org/search?' + urlencode(
              {'query': term, 'type': 'release', 'method': 'indexed'}),
              'Apple Music search': 'https://music.apple.com/us/search?' + urlencode({'term': term}),
              'Spotify search': 'https://open.spotify.com/search/' + quote(term, safe='')}
    for field, entity in [('release_group_mbid', 'release-group'), ('release_mbid', 'release')]:
        mbid = str(group.get(field, ''))
        if re.fullmatch(r'[0-9a-fA-F-]{36}', mbid):
            result['Tagged ' + entity] = 'https://musicbrainz.org/' + entity + '/' + mbid
    if group.get('lidarr_album_url'):
        result['Lidarr album'] = group['lidarr_album_url']
    return result


def check_catalog(group, mb, apple, country):
    if not group['artist'] or not group['album']:
        group['catalog_status'] = 'Insufficient identifying tags; inspect files manually'
        return
    query = f'releasegroup:{lucene_quote(group["album"])} AND artist:{lucene_quote(group["artist"])}'
    try:
        payload = mb.search('release-group', query, 10)
        candidates = payload.get('release-groups', [])
        simpler_title = without_trailing_parenthetical(group['album'])
        if not candidates and simpler_title != group['album']:
            query = f'releasegroup:{lucene_quote(simpler_title)} AND artist:{lucene_quote(group["artist"])}'
            candidates = mb.search('release-group', query, 10).get('release-groups', [])
        group['musicbrainz_candidates'] = [dict(id=r.get('id'), title=r.get('title'),
            first_release_date=r.get('first-release-date', ''),
            artist=''.join(p.get('name', '') + p.get('joinphrase', '') for p in r.get('artist-credit', [])))
            for r in candidates]
        response = apple.get('https://itunes.apple.com/search', params={
            'term': group['artist'] + ' ' + group['album'], 'entity': 'album',
            'country': country, 'limit': 10}, timeout=30)
        response.raise_for_status()
        group['apple_candidates'] = [dict(artist=r.get('artistName'), title=r.get('collectionName'),
            date=r.get('releaseDate', '')[:10], url=r.get('collectionViewUrl', ''),
            artist_url=r.get('artistViewUrl', ''),
            exact=normalize(r.get('artistName', '')) == normalize(group['artist']) and
                  normalize(r.get('collectionName', '')) == normalize(group['album']))
            for r in response.json().get('results', []) if r.get('collectionId')]
        if candidates:
            group['catalog_status'] = 'MusicBrainz candidates found; verify edition/track list before matching'
        elif group.get('release_group_mbid') or group.get('release_mbid'):
            group['catalog_status'] = 'Tagged MusicBrainz reference exists; verify it before considering a new entry'
        elif any(r['exact'] for r in group['apple_candidates']):
            group['catalog_status'] = 'Possible missing MusicBrainz release; Apple match requires manual review'
        else:
            group['catalog_status'] = 'No catalog match in this limited search; not proof of absence'
    except requests.RequestException as exc:
        group['catalog_status'] = 'Catalog lookup failed: ' + type(exc).__name__ + '; not a missing-entry result'


def write_report(output, groups, source):
    output.mkdir(parents=True, exist_ok=False)
    payload = dict(created=datetime.now(timezone.utc).isoformat(), source=source,
                   note='Read-only review. No files, Lidarr records or MusicBrainz entries were changed.',
                   groups=groups)
    (output / 'review.json').write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf8')
    fields = ['kind', 'artist', 'album', 'file_count', 'folder', 'reason', 'catalog_status',
              'release_group_mbid', 'release_mbid', 'MusicBrainz search', 'Apple Music search', 'Spotify search']
    with (output / 'albums.csv').open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for group in groups:
            row = {k: group.get(k, '') for k in fields}
            row.update(file_count=len(group['files']), **{k: v for k, v in links(group).items() if k in fields})
            writer.writerow({k: "'" + str(v) if str(v).startswith(('=', '+', '-', '@')) else v for k, v in row.items()})
    escape = lambda value: html.escape(str(value), quote=True)
    parts = ['<!doctype html><meta charset="utf-8"><title>Library review</title>',
             '<style>body{font:16px sans-serif;max-width:1100px;margin:2em auto;padding:1em}section{border-top:1px solid #aaa;padding:1em 0}li{overflow-wrap:anywhere}a{margin-right:1em}</style>',
             '<h1>Library review</h1><p>Unmapped files need matching; they are not automatically missing from MusicBrainz. '
             'Search results are candidates, not verified editions. Nothing was changed or submitted.</p>',
             '<p>For a verified Apple Music release missing from MusicBrainz, use '
             '<code>musicbrainz-new-artist APPLE_ALBUM_URL</code> to prepare the existing editor review workflow.</p>']
    for group in groups:
        parts.append('<section><h2>' + escape(group['artist'] + ' — ' + (group['album'] or '(album tag missing)')) + '</h2>')
        parts.append('<p>' + escape(group['kind'] + ': ' + group['reason']) + '</p><p>' + escape(group['catalog_status']) + '</p>')
        parts.append('<p>' + ''.join('<a href="' + escape(url) + '">' + escape(label) + '</a>' for label, url in links(group).items()) + '</p>')
        for row in group.get('musicbrainz_candidates', []):
            label = escape(row['artist'] + ' — ' + str(row['title']) + ' (' + row['first_release_date'] + ')')
            mbid = str(row.get('id', ''))
            if re.fullmatch(r'[0-9a-fA-F-]{36}', mbid):
                label = '<a href="https://musicbrainz.org/release-group/' + escape(mbid) + '">' + label + '</a>'
            parts.append('<p>MusicBrainz: ' + label + '</p>')
        for row in group.get('apple_candidates', []):
            url = row['url'] if str(row['url']).startswith('https://music.apple.com/') else links(group)['Apple Music search']
            parts.append('<p>Apple candidate: <a href="' + escape(url) + '">' + escape(str(row['artist']) + ' — ' + str(row['title'])) + '</a> ' + escape(row['date']) + '</p>')
        parts.append('<details><summary>' + str(len(group['files'])) + ' files</summary><ul>')
        parts.extend('<li>' + escape(row['path']) + (' — ' + escape(row['tag_read_error']) if row.get('tag_read_error') else '') + '</li>' for row in group['files'])
        parts.append('</ul></details></section>')
    (output / 'review.html').write_text('\n'.join(parts), encoding='utf8')


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lidarr-url', default=os.environ.get('LIDARR_URL'))
    parser.add_argument('--unmapped-json', type=Path, help='Previously exported Lidarr trackfile?unmapped=true response')
    parser.add_argument('--library-root', type=Path, default=Path('/music'), help='Local path corresponding to Lidarr /music')
    parser.add_argument('--remote-root',default='/music',help='Music root reported by Lidarr (default: /music)')
    parser.add_argument('--date-seeds', type=Path, help='Optional JSON rows from an offline date audit (never opens a database)')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check-catalog', type=int, default=0, metavar='N', help='Check the first N unmapped groups against MusicBrainz and Apple; default is links only')
    parser.add_argument('--country', default='us')
    args = parser.parse_args(argv)
    if args.check_catalog < 0:
        parser.error('--check-catalog must be nonnegative')
    if not args.unmapped_json and not args.lidarr_url:
        parser.error('provide --lidarr-url or --unmapped-json')
    if args.unmapped_json:
        unmapped = json.loads(args.unmapped_json.read_text(encoding='utf-8-sig'))
    else:
        key = os.environ.get('LIDARR_API_KEY')
        if not key:
            parser.error('set LIDARR_API_KEY in the environment or .env')
        session = requests.Session()
        session.headers.update({'X-Api-Key': key, 'User-Agent': CLIENT})
        unmapped = get_json(session, args.lidarr_url, 'trackfile', unmapped='true')
    rows = []
    for index, row in enumerate(unmapped, 1):
        rows.append(make_unmapped(row, args.library_root,args.remote_root))
        if index % 50 == 0:
            print(f'Read metadata for {index}/{len(unmapped)} unmapped files', flush=True)
    if args.date_seeds:
        rows.extend(add_date_seeds(json.loads(args.date_seeds.read_text(encoding='utf-8-sig'))))
    groups = group_rows(rows)
    if args.date_seeds and args.lidarr_url:
        key = os.environ.get('LIDARR_API_KEY')
        if not key:
            parser.error('set LIDARR_API_KEY to verify date candidates against live Lidarr')
        session = requests.Session()
        session.headers.update({'X-Api-Key': key, 'User-Agent': CLIENT})
        for group in [g for g in groups if g['kind'] == 'date-review']:
            mbid = group.get('release_group_mbid', '')
            if not re.fullmatch(r'[0-9a-fA-F-]{36}', mbid):
                continue
            try:
                albums = get_json(session, args.lidarr_url, 'album', foreignAlbumId=mbid)
                if len(albums) == 1:
                    group['lidarr_release_date'] = albums[0].get('releaseDate', '')
                    group['lidarr_album_id'] = albums[0]['id']
                    group['lidarr_album_url'] = args.lidarr_url.rstrip('/') + '/album/' + mbid
                    group['reason'] += '; current Lidarr release date: ' + str(group['lidarr_release_date'] or 'missing')
                else:
                    group['reason'] += '; no unique current Lidarr album match'
            except requests.RequestException as exc:
                group['reason'] += '; current Lidarr lookup failed: ' + type(exc).__name__
    if args.check_catalog:
        mb, apple = MusicBrainzClient(), requests.Session()
        apple.headers['User-Agent'] = CLIENT
        for group in [g for g in groups if g['kind'] == 'unmapped'][:args.check_catalog]:
            print('Checking catalog: ' + group['artist'] + ' — ' + group['album'], flush=True)
            check_catalog(group, mb, apple, args.country)
    output = args.output or Path('logs/library-review') / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    write_report(output, groups, str(args.unmapped_json or args.lidarr_url))
    print(json.dumps(dict(output=str(output.resolve()), unmapped_files=len(unmapped),
                         album_groups=len(groups), date_groups=sum(g['kind'] == 'date-review' for g in groups))), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
