"""Exact-edition date evidence and date-only writes through Beets' MediaFile backend."""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
import re

import requests
from mutagen import File, MutagenError

from .catalog import MusicBrainzClient
from . import dates
from .metadata import identify
from .playlists import hash_json, normalized


def partial_date(value):
    if not re.fullmatch(r'\d{4}(?:-\d{2}(?:-\d{2})?)?', value) or dates.date_problem(value):
        raise ValueError('source date is blank, invalid or has unsupported precision')
    return tuple(int(x) for x in value.split('-'))


def family(key):
    key=str(key).casefold().removeprefix('txxx:').removeprefix('----:com.apple.itunes:').replace('_','').replace(' ','')
    return 'original_date' if key.startswith('original') or key in ('tdor','tory','wm/originalreleasetime','wm/originalreleaseyear') else 'release_date'


def artist_ids(credits):
    return [str(x.get('artist',{}).get('id','')).casefold() for x in credits if x.get('artist',{}).get('id')]


def verified_evidence(payload, tags):
    if not tags.get('title') or tags.get('tracknumber',0)<1 or tags.get('discnumber',0)<1 or tags.get('duration_ms',0)<=0:
        raise ValueError('complete title, position and duration are required for date evidence')
    release_id=tags.get('release_mbid','').casefold()
    group_id=tags.get('group_mbid','').casefold()
    group=payload.get('release-group') or {}
    if not release_id or not group_id or str(payload.get('id','')).casefold()!=release_id or str(group.get('id','')).casefold()!=group_id:
        raise ValueError('tagged exact release/release-group differs from catalog data')
    release_artists=artist_ids(payload.get('artist-credit',[]))
    group_artists=artist_ids(group.get('artist-credit',[]))
    if not release_artists or not group_artists or release_artists!=group_artists:
        raise ValueError('release and release-group artist credits conflict or are unavailable')
    if tags.get('album_artist_mbid') and tags['album_artist_mbid'].casefold() not in release_artists:
        raise ValueError('tagged album artist differs from exact catalog release')
    if not tags.get('album_artist_mbid'):
        credited=''.join((c.get('name') or c.get('artist',{}).get('name',''))+c.get('joinphrase','') for c in payload.get('artist-credit',[]))
        if normalized(tags.get('albumartist') or tags.get('artist',''))!=normalized(credited):
            raise ValueError('album artist name cannot be verified against the exact catalog release')
    matches=[]
    for medium in payload.get('media',[]):
        if medium.get('position')!=tags['discnumber']:
            continue
        for row in medium.get('tracks',[]):
            recording=row.get('recording') or {}
            length=row.get('length') or recording.get('length')
            if str(row.get('number'))!=str(tags['tracknumber']) or not length or abs(int(length)-tags['duration_ms'])>2500:
                continue
            if normalized(row.get('title') or recording.get('title',''))!=normalized(tags.get('title','')):
                continue
            if tags.get('recording_mbid') and tags['recording_mbid'].casefold()!=str(recording.get('id','')).casefold():
                continue
            if tags.get('release_track_mbid') and tags['release_track_mbid'].casefold()!=str(row.get('id','')).casefold():
                continue
            matches.append(dict(id=row.get('id'),recording_id=recording.get('id'),title=row.get('title') or recording.get('title'),
                                length=length,disc=medium['position'],number=str(row['number'])))
    if len(matches)!=1:
        raise ValueError('no unique exact-edition track/position/title/duration match')
    result=dict(kind='musicbrainz-exact-release',release_id=release_id,group_id=group_id,
                release_artists=release_artists,group_artists=group_artists,track=matches[0],
                release_date=payload.get('date') or '',original_date=group.get('first-release-date') or '',
                release_title=payload.get('title'),source='https://musicbrainz.org/release/'+release_id)
    return result


def choose_targets(current, evidence, fill_missing=False):
    targets, unresolved={},[]
    for kind in ('release_date','original_date'):
        existing=[v for key,vv in current['dates'].items() if family(key)==kind for v in vv]
        invalid=[v for v in existing if dates.date_problem(v)]
        if not invalid and (existing or not fill_missing):
            continue
        try:
            source=partial_date(evidence[kind])
        except ValueError:
            unresolved.append(kind+': no valid exact catalog date; no replacement invented')
            continue
        blocked=False
        for value in existing:
            problem=dates.date_problem(value)
            if problem and problem not in ('invalid placeholder year','invalid calendar date'):
                unresolved.append(kind+': ambiguous existing date requires review');blocked=True;break
            if not problem:
                try:
                    valid=partial_date(value)
                except ValueError:
                    unresolved.append(kind+': existing timestamp/precision requires review');blocked=True;break
                common=min(len(source),len(valid))
                if source[:common]!=valid[:common]:
                    unresolved.append(kind+': valid existing date conflicts with the catalog');blocked=True;break
        if not blocked:
            targets[kind]=evidence[kind]
    return targets,unresolved


def make_plan(root, report, output, client=None, fill_missing=False, apple_review=False, country='us', apple_client=None):
    client=client or MusicBrainzClient()
    if apple_review:
        from .provider_review import AppleReviewClient,review
        apple_client=apple_client or AppleReviewClient(country)
    rows=[json.loads(line) for line in report.read_text(encoding='utf8').splitlines()]
    if not rows or rows[0].get('type')!='start' or rows[-1].get('type')!='end' or Path(rows[0]['root']).resolve()!=root.resolve():
        raise ValueError('date audit is incomplete or belongs to a different root')
    entries,unresolved,cache,providers=[],[],{},{}
    for row in rows:
        if row.get('type')!='file':
            continue
        try:
            path=dates.contained(root,row['path'])
            current=dates.inspect(path,root)
            if current['dates']!=row['dates']:
                raise ValueError('date tags changed since audit')
            tags=identify(path)
            release=tags.get('release_mbid','').casefold()
            if not release or not tags.get('group_mbid'):
                raise ValueError('exact release and release-group tags are required for date lookup')
            if release not in cache:
                print('Looking up exact release '+release,flush=True)
                cache[release]=client.lookup_release(release)
            evidence=verified_evidence(cache[release],tags)
            if apple_review and release not in providers:
                print('Reviewing Apple storefront context for '+release,flush=True)
                providers[release]=review(cache[release],apple_client)
            targets,notes=choose_targets(current,evidence,fill_missing)
            unresolved.extend(dict(path=row['path'],reason=note) for note in notes)
            if targets or current['remove_keys']:
                entries.append(dict(path=row['path'],sha256=dates.digest(path),dates=current['dates'],
                    remove_keys=current['remove_keys'],targets=targets,identifiers=tags,evidence=evidence))
            elif current['stale_folder']:
                unresolved.append(dict(path=row['path'],reason='embedded dates need no verified change; folder rename is a separate operation'))
        except (ValueError,OSError,MutagenError,requests.RequestException) as exc:
            unresolved.append(dict(path=row['path'],reason=str(exc) if not isinstance(exc,requests.RequestException) else 'catalog lookup failed: '+type(exc).__name__))
    payload=dict(schema=2,action='repair-dates-from-evidence',root=str(root.resolve()),created=dates.utc(),
                 entries=entries,unresolved=unresolved,provider_reviews=list(providers.values()),
                 note='Reviewed date-only edits. Edition and original dates remain distinct; unknown precision is never expanded.')
    with output.open('x',encoding='utf8') as stream:
        json.dump(payload,stream,indent=2,ensure_ascii=False)
    write_html(payload,output.with_suffix('.html'))
    print(f'Date plan: {len(entries)} files, {len(unresolved)} unresolved notes; review {output}',flush=True)
    return payload


def write_html(payload,output):
    escape=lambda value:html.escape(str(value),quote=True)
    parts=['<!doctype html><meta charset="utf-8"><title>Date repair review</title>',
           '<style>body{font:16px sans-serif;max-width:1100px;margin:2em auto;padding:1em}pre,code{white-space:pre-wrap;overflow-wrap:anywhere}section{border-top:1px solid #aaa;padding:1em 0}</style>',
           '<h1>Date repair review</h1><p>No files were changed. Edition dates and original dates stay separate; missing precision is never invented.</p>']
    for entry in payload['entries']:
        parts.append('<section><h2>'+escape(entry['path'])+'</h2><p>Current date tags:</p><pre>'+escape(json.dumps(entry['dates'],indent=2,ensure_ascii=False))+'</pre>')
        parts.append('<p>Reviewed replacement values:</p><pre>'+escape(json.dumps(entry['targets'],indent=2))+'</pre>')
        parts.append('<p>Invalid placeholder aliases eligible for cleanup: '+escape(', '.join(entry['remove_keys']))+'</p>')
        parts.append('<p><a href="'+escape(entry['evidence']['source'])+'">Exact MusicBrainz release evidence</a></p></section>')
    parts.append('<h2>Unresolved review</h2><pre>'+escape(json.dumps(payload['unresolved'],indent=2,ensure_ascii=False))+'</pre>')
    for review in payload.get('provider_reviews',[]):
        parts.append('<section><h2>Storefront context: '+escape(review['artist']+' — '+review['title'])+'</h2><p>'+escape(review['note'])+'</p>')
        for label,key in [('Review existing release','edit_release'),('Review existing release group','edit_group'),('Apple search','apple_search')]:
            parts.append('<p><a href="'+escape(review[key])+'">'+label+'</a></p>')
        for candidate in review['candidates']:
            parts.append('<p><a href="'+escape(candidate['url'])+'">'+escape(candidate['title'])+'</a> — '+escape(candidate['date'])+'</p><pre>'+escape(json.dumps(candidate['track_comparison'],indent=2))+'</pre>')
        parts.append('<p>'+escape(review['status'])+'</p></section>')
    with output.open('x',encoding='utf8') as stream:
        stream.write('\n'.join(parts))


def validate_entry(entry, client, cache):
    if entry.get('evidence',{}).get('kind')!='musicbrainz-exact-release':
        raise ValueError('exact-release date evidence is required')
    if not set(entry.get('targets',{})).issubset({'release_date','original_date'}):
        raise ValueError('unknown date target field')
    for kind,value in entry.get('targets',{}).items():
        partial_date(value)
        if value!=entry['evidence'].get(kind):
            raise ValueError('replacement date differs from reviewed source precision/value')
    allowed,_=choose_targets({'dates':entry['dates']},entry['evidence'],fill_missing=True)
    if any(allowed.get(kind)!=value for kind,value in entry.get('targets',{}).items()):
        raise ValueError('plan would overwrite a valid or conflicting existing date')
    if entry.get('targets'):
        release=entry['identifiers']['release_mbid'].casefold()
        if release not in cache:
            cache[release]=client.lookup_release(release)
        if verified_evidence(cache[release],entry['identifiers'])!=entry['evidence']:
            raise ValueError('exact catalog evidence changed since review; generate a fresh plan')


def protected_metadata(path):
    audio=File(path)
    if audio is None:
        raise ValueError('unreadable audio during date verification')
    return hash_json(dict(tags={str(k):repr(v) for k,v in (audio.tags or {}).items() if not dates.date_key(k)},
                          pictures=[hashlib.sha256(p.write()).hexdigest() for p in getattr(audio,'pictures',[])],
                          id3_version=getattr(audio.tags,'version',None),
                          unknown_frames=[hashlib.sha256(frame).hexdigest() for frame in getattr(audio.tags,'unknown_frames',[])]))


def semantic_dates(path):
    try:
        from mediafile import MediaFile
    except ImportError:
        raise ValueError('date replacement requires the tags extra: install navidrome-scripts[tags]') from None
    audio=MediaFile(str(path))
    result={}
    for name,prefix in [('release_date',''),('original_date','original_')]:
        year=getattr(audio,prefix+'year') or 0
        month=getattr(audio,prefix+'month') or 0
        day=getattr(audio,prefix+'day') or 0
        if not year:
            result[name]='';continue
        result[name]=f'{year:04d}'+(f'-{month:02d}' if month else '')+(f'-{day:02d}' if month and day else '')
    return result


def write_dates(path, entry):
    if entry.get('targets'):
        try:
            from mediafile import MediaFile
        except ImportError:
            raise ValueError('date replacement requires the tags extra: install navidrome-scripts[tags]') from None
        raw=File(path)
        original_version=getattr(raw.tags,'version',None)
        audio=MediaFile(str(path),id3v23=bool(original_version and original_version[1]==3))
        updates={}
        for kind,date in entry['targets'].items():
            parts=partial_date(date)
            year,month,day=(*parts,0,0)[:3]
            prefix='original_' if kind=='original_date' else ''
            updates.update({prefix+'year':year,prefix+'month':month,prefix+'day':day})
        audio.update(updates);audio.save()
    raw=File(path)
    changed=False
    # Clear remaining legacy placeholder aliases, but never remove a valid field
    # that the backend just replaced with the reviewed value.
    for key in entry['remove_keys']:
        if key in (raw.tags or {}) and all(dates.date_problem(v)=='invalid placeholder year' for v in dates.values(raw.tags[key])):
            del raw.tags[key];changed=True
    if changed:
        raw.save()


def verify_write(path, entry, protected):
    if protected_metadata(path)!=protected:
        raise ValueError('date writer changed unrelated metadata')
    actual=dates.inspect(path,path.parent)['dates']
    untouched={k:v for k,v in entry['dates'].items() if k not in entry['remove_keys'] and family(k) not in entry.get('targets',{})}
    observed={k:v for k,v in actual.items() if family(k) not in entry.get('targets',{})}
    if observed!=untouched:
        raise ValueError('date writer changed an unapproved date field')
    if entry.get('targets'):
        semantic=semantic_dates(path)
        for kind,value in entry['targets'].items():
            if semantic[kind]!=value:
                raise ValueError('date value/precision did not round-trip through the tag backend')
            if any(dates.date_problem(v) for k,vv in actual.items() if family(k)==kind for v in vv):
                raise ValueError('a conflicting invalid date alias remains')
