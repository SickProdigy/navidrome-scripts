"""Optional storefront date context for human review, never a write authority."""
import re
import time
from urllib.parse import urlencode,urlsplit

import requests

from .catalog import CLIENT,normalize
from .dates import date_problem


class AppleReviewClient:
    def __init__(self,country='us',session=None,clock=time.monotonic,sleep=time.sleep):
        if not re.fullmatch('[a-zA-Z]{2}',country):
            raise ValueError('Apple storefront needs a two-letter country code')
        self.country=country.lower();self.session=session or requests.Session()
        self.session.headers['User-Agent']=CLIENT
        self.clock,self.sleep,self.last=clock,sleep,None

    def get(self,endpoint,**params):
        # Apple's documented search allowance is approximately 20/minute.
        if self.last is not None:
            self.sleep(max(0,3.1-(self.clock()-self.last)))
        try:
            response=self.session.get('https://itunes.apple.com/'+endpoint,params=dict(country=self.country,**params),timeout=(10,30))
            response.raise_for_status();payload=response.json()
            if not isinstance(payload,dict) or not isinstance(payload.get('results'),list):
                raise ValueError('malformed Apple results')
            return payload['results']
        finally:
            self.last=self.clock()


def credited(rows):
    return ''.join((r.get('name') or r.get('artist',{}).get('name',''))+r.get('joinphrase','') for r in rows)


def comparison(release,collection,rows):
    expected={}
    for medium in release.get('media',[]):
        if medium.get('track-count',len(medium.get('tracks',[])))!=len(medium.get('tracks',[])):
            return dict(complete=False,compatible=False,reason='catalog track list is incomplete')
        for track in medium.get('tracks',[]):
            number=str(track.get('number',''))
            if not number.isdigit():
                return dict(complete=False,compatible=False,reason='non-numeric catalog track numbers require review')
            key=(medium.get('position'),int(number))
            if key in expected:
                return dict(complete=False,compatible=False,reason='duplicate catalog positions')
            expected[key]=track
    tracks=[r for r in rows if r.get('wrapperType')=='track' and r.get('kind')=='song' and r.get('collectionId')==collection['collectionId']]
    observed={(r.get('discNumber'),r.get('trackNumber')):r for r in tracks}
    count=collection.get('trackCount')
    if not expected or not isinstance(count,int) or len(tracks)!=count or len(observed)!=len(tracks) or len(tracks)!=len(expected):
        return dict(complete=False,compatible=False,reason='incomplete or differing track counts; no date conclusion')
    differences=[]
    for key,track in expected.items():
        row=observed.get(key)
        recording=track.get('recording') or {}
        length=track.get('length') or recording.get('length')
        artists=track.get('artist-credit') or recording.get('artist-credit') or release.get('artist-credit',[])
        if not row:
            differences.append(dict(position=list(key),reason='missing position'));continue
        if normalize(row.get('trackName') or '')!=normalize(track.get('title') or recording.get('title') or ''):
            differences.append(dict(position=list(key),reason='title differs',catalog=track.get('title') or recording.get('title'),storefront=row.get('trackName')))
        if normalize(row.get('artistName') or '')!=normalize(credited(artists)):
            differences.append(dict(position=list(key),reason='artist credit differs',catalog=credited(artists),storefront=row.get('artistName')))
        if not length or not row.get('trackTimeMillis') or abs(int(length)-int(row['trackTimeMillis']))>2500:
            differences.append(dict(position=list(key),reason='duration differs or is unavailable',catalog_ms=length,storefront_ms=row.get('trackTimeMillis')))
    return dict(complete=True,compatible=not differences,differences=differences,
        note='Compatible tracks support review only; they do not establish the same release event or original release date.')


def review(release,client):
    identity=release['id'];group=release.get('release-group') or {}
    artist=credited(release.get('artist-credit',[]));title=release.get('title') or ''
    result=dict(release_mbid=identity,group_mbid=group.get('id'),artist=artist,title=title,storefront=client.country,
        catalog_release_date=release.get('date') or '',catalog_original_date=group.get('first-release-date') or '',
        edit_release='https://musicbrainz.org/release/'+identity+'/edit',
        edit_group='https://musicbrainz.org/release-group/'+str(group.get('id',''))+'/edit',
        apple_search='https://music.apple.com/'+client.country+'/search?'+urlencode({'term':artist+' '+title}),
        candidates=[],note='External dates are manual context only. Verify edition, country and release event before editing the existing catalog entry; no edits are submitted.')
    if not artist or not title:
        result.update(status='insufficient_catalog_identity');return result
    try:
        rows=client.get('search',term=artist+' '+title,media='music',entity='album',limit=10)
        seen=set()
        for row in rows:
            cid=row.get('collectionId')
            if type(cid) is not int or cid<1 or cid in seen:
                continue
            seen.add(cid)
            name=row.get('collectionName') or ''
            # Retain actual storefront spelling and flag suffix normalization.
            simple=re.sub(r' - (?:Single|EP)$','',name,flags=re.I)
            same_artist=normalize(row.get('artistName') or '')==normalize(artist)
            same_title=normalize(name)==normalize(title)
            suffix_match=normalize(simple)==normalize(title)
            if not same_artist or not (same_title or suffix_match):
                continue
            url=row.get('collectionViewUrl') or ''
            if urlsplit(url).scheme!='https' or urlsplit(url).hostname!='music.apple.com':
                continue
            candidate=dict(collection_id=cid,artist=row.get('artistName'),title=name,url=url,
                date=str(row.get('releaseDate') or '')[:10],track_count=row.get('trackCount'),
                title_exact=same_title,store_suffix_normalized=not same_title and suffix_match)
            candidate['date_valid']=bool(candidate['date']) and not bool(date_problem(candidate['date']))
            details=client.get('lookup',id=cid,entity='song',limit=200)
            albums=[r for r in details if r.get('wrapperType')=='collection' and r.get('collectionId')==cid]
            if len(albums)!=1:
                candidate['track_comparison']=dict(complete=False,compatible=False,reason='lookup did not return one album')
            else:
                album=albums[0]
                if any(album.get(k)!=row.get(k) for k in ('artistName','collectionName','releaseDate','trackCount')):
                    candidate['track_comparison']=dict(complete=False,compatible=False,reason='storefront album changed between search and lookup')
                else:
                    candidate['track_comparison']=comparison(release,album,details)
            result['candidates'].append(candidate)
        result['status']='manual_review' if result['candidates'] else 'no_exact_storefront_candidates_in_limited_search'
    except (requests.RequestException,ValueError,TypeError,KeyError) as exc:
        result.update(status='lookup_failed',error=type(exc).__name__)
    return result
