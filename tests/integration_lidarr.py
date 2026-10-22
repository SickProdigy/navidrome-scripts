"""Opt-in Docker sandbox acceptance; never point this at existing services.

Requires fresh Lidarr/Navidrome containers on a private fixture network, an empty
/music volume bearing the supplied sandbox token, and /artifacts for results.
Only generated silence is imported. Real metadata lookups require networking.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import time
from urllib.parse import urlsplit

import imageio_ffmpeg
from mutagen.flac import FLAC
import requests

from navidrome_scripts import unmapped, renames, rename_rollback, maintenance, reconciliation, indexed_duplicates, navidrome_guard as guard
from navidrome_scripts.lidarr import Lidarr
from navidrome_scripts.subsonic import Subsonic


ARTIST = '5b11f4ce-a62d-471e-81fc-a69a8278c7da'


def wait(call, predicate=lambda value: bool(value), seconds=120):
    deadline = time.monotonic() + seconds
    while True:
        try:
            value = call()
            if predicate(value):
                return value
        except requests.RequestException:
            pass
        if time.monotonic() >= deadline:
            raise AssertionError('sandbox operation timed out')
        time.sleep(.5)


def full_scan(nav):
    before = nav.request('getScanStatus')['scanStatus'].get('lastScan')
    nav.request('startScan', dict(fullScan='true'))
    wait(lambda: nav.request('getScanStatus')['scanStatus'],
         lambda row: row.get('scanning') is False and row.get('lastScan') != before and row.get('scanType') == 'full')


def completed(row):
    if row.get('status') in ('failed','aborted','cancelled'):
        raise AssertionError('sandbox Lidarr command failed: '+str(row.get('message') or row.get('name')))
    return row.get('status')=='completed'


def main():
    root, artifacts = Path('/music'), Path('/artifacts')
    token = os.environ['INTEGRATION_SANDBOX_TOKEN']
    if not token or (root/'.integration-sandbox').read_text().strip() != token:
        raise AssertionError('fresh sandbox marker required')
    lidarr_url, nav_url = os.environ['LIDARR_TEST_URL'], os.environ['NAVIDROME_TEST_URL']
    if any(not urlsplit(url).hostname.startswith('nav-fixture-') for url in (lidarr_url, nav_url)):
        raise AssertionError('only dedicated fixture service names are allowed')
    if list(root.iterdir()) != [root/'.integration-sandbox']:
        raise AssertionError('fixture music volume must start empty')
    artifacts.mkdir(exist_ok=True)
    key = os.environ['LIDARR_TEST_KEY']
    client = Lidarr(lidarr_url, key)
    session = requests.Session(); session.headers['X-Api-Key'] = key
    def api(method, endpoint, body=None, **params):
        response = session.request(method, lidarr_url+'/api/v1/'+endpoint, json=body, params=params, timeout=(10,90))
        response.raise_for_status()
        return response.json()
    result = dict(isolated=True, production_untouched=True)
    try:
        wait(lambda: api('GET', 'qualityprofile'))
        lookup = wait(lambda: api('GET', 'artist/lookup', term='lidarr:'+ARTIST))
        if api('GET','artist') or api('GET','rootfolder'):
            raise AssertionError('Lidarr is not a fresh disposable instance')
        result['lidarr_version'] = api('GET','system/status')['version']
        print('Configuring actual isolated Lidarr '+result['lidarr_version'], flush=True)
        quality = api('GET','qualityprofile')[0]['id']
        metadata = api('GET','metadataprofile')[0]['id']
        api('POST','rootfolder',dict(name='Fixture',path='/music',defaultQualityProfileId=quality,
            defaultMetadataProfileId=metadata,defaultMonitorOption='none',defaultNewItemMonitorOption='none',defaultTags=[]))
        artist = lookup[0]
        artist.update(path='/music/Nirvana',rootFolderPath='/music',qualityProfileId=quality,
                      metadataProfileId=metadata,monitored=False,addOptions=dict(monitor='none',searchForMissingAlbums=False))
        artist = api('POST','artist',artist)
        albums = wait(lambda: api('GET','album',artistId=artist['id']),
                      lambda rows: any(row['title']=='Nevermind' and row.get('releases') for row in rows))
        album = next(row for row in albums if row['title']=='Nevermind')
        release = next(row for row in album['releases'] if row['trackCount']==12)
        tracks = api('GET','track',albumReleaseId=release['id'])
        track = next(row for row in tracks if row.get('mediumNumber')==1 and str(row.get('trackNumber'))=='1')
        for endpoint, updates in (
            ('config/metadataprovider',dict(writeAudioTags='no')),
            ('config/mediamanagement',dict(fileDate='none')),
            ('config/naming',dict(renameTracks=True,standardTrackFormat='{Album Title} ({Release Year})/{track:00} - {Track Title}',
                                  multiDiscTrackFormat='{Album Title} ({Release Year})/{medium:00}-{track:00} - {Track Title}')),
        ):
            config=api('GET',endpoint);config.update(updates);api('PUT',endpoint,config)
        sources=[]
        for index,track in enumerate(tracks):
            path=root/('Nirvana/Fixture source/source-'+str(index+1)+'.flac');path.parent.mkdir(parents=True,exist_ok=True)
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=44100',
                            '-t',str(track['duration']/1000),'-c:a','flac',str(path)],check=True,timeout=60)
            blank=FLAC(path);blank['title']='Unmapped fixture '+str(index);blank['album']='Unmapped fixture';blank['artist']='Fixture';blank.save()
            sources.append((path,track))
        command=api('POST','command',dict(name='RescanFolders',folders=[str(path.parent)],filter='known',addNewArtists=False,artistIds=[artist['id']]))
        wait(lambda: api('GET','command/'+str(command['id'])),completed)
        files=api('GET','trackfile',unmapped='true')
        if len(files)!=len(sources) or {row['path'] for row in files}!={str(path) for path,_ in sources}:
            raise AssertionError('generated files were not all listed as unmapped')
        for path,track in sources:
            audio=FLAC(path)
            values=dict(title=track['title'],artist='Nirvana',album='Nevermind',albumartist='Nirvana',
                        tracknumber=str(track['trackNumber']),discnumber='1',musicbrainz_albumid=release['foreignReleaseId'],
                        musicbrainz_releasegroupid=album['foreignAlbumId'],musicbrainz_trackid=track['foreignRecordingId'],
                        musicbrainz_releasetrackid=track['foreignTrackId'],musicbrainz_albumartistid=ARTIST,musicbrainz_artistid=ARTIST)
            for field,value in values.items():audio[field]=value
            audio.save()
        plan=unmapped.plan(client,root,str(path.parent))
        if plan['summary']!={'ready':len(sources)}:raise AssertionError('actual unmapped plan was not ready: '+json.dumps(plan['summary']))
        (artifacts/'import-plan.json').write_text(json.dumps(plan,indent=2))
        wait(lambda: unmapped.apply(client,root,plan,artifacts/'import-journal.json',wait_seconds=1),lambda status: status==0)
        if len(api('GET','artist'))!=1:raise AssertionError('import created an unexpected artist')
        result['real_manual_import']=True
        result['metadata_handoff']=(artifacts/'import-journal.metadata-queue.json').is_file()
        print('Actual existing-file import and metadata handoff passed',flush=True)
        config=artifacts/'config';config.mkdir()
        (config/'extended.conf').write_text('enableReplaygainTags=true\nreplaygainPreserveMtime=true\n')
        os.environ.update(EXTENDED_CONF=str(config/'extended.conf'),FUNCTIONS_PATH='/not-present',REPLAYGAIN_STATE_DIR=str(config/'extended/replaygain'))
        state=artifacts/'maintenance'
        maintenance.approve(client,root,artifacts/'import-journal.metadata-queue.json',state,maintenance.STEPS)
        if maintenance.work(client,root,state,Path('/tmp/arr-hooks'),config)!=0:
            raise AssertionError('actual metadata worker failed; inspect maintenance job and logs')
        jobs=[json.loads(p.read_text()) for p in (state/'jobs').glob('*.json')]
        if len(jobs)!=1 or jobs[0]['status']!='complete_with_review' or set(jobs[0]['results'])!=set(maintenance.STEPS):
            raise AssertionError('metadata worker did not finish all approved steps')
        if not all(maintenance.gain_complete(source) for source,_ in sources):
            raise AssertionError('real ReplayGain did not produce all four tags')
        with sqlite3.connect(config/'extended/lyrics/jobs.sqlite') as db:
            count=db.execute('SELECT count(*) FROM jobs').fetchone()[0]
        if count!=len(sources):raise AssertionError('lyrics queue did not receive every imported file')
        result['real_beets_preview']=True;result['real_replaygain']=True;result['lyrics_enqueued']=count
        print('Actual Beets preview, ReplayGain scanner and lyrics enqueue passed',flush=True)
        refresh=reconciliation.plan(client,root,album['id'])
        if refresh['status']!='ready':raise AssertionError('refresh requires source review: '+json.dumps(refresh['notes']))
        refresh_journal=artifacts/'refresh-journal.json'
        wait(lambda: reconciliation.reconcile(client,root,refresh,refresh_journal),
             lambda _: json.loads(refresh_journal.read_text())['status']=='reconciled')
        result['real_metadata_refresh']=True
        path.with_suffix('.lrc').write_text('[00:00]Generated fixture')
        (path.parent/'cover.jpg').write_bytes(b'fixture-sidecar-not-a-real-image')
        wait(lambda: requests.get(nav_url+'/ping',timeout=2).status_code,lambda status: status==200)
        password=secrets.token_urlsafe(24)
        response=requests.post(nav_url+'/auth/createAdmin',json=dict(username='fixture-admin',password=password),timeout=10)
        response.raise_for_status()
        headers={'X-ND-Authorization':'Bearer '+response.json()['token']}
        nav=Subsonic(nav_url,'fixture-admin',password)
        wait(lambda: nav.request('getScanStatus')['scanStatus'],lambda row: not row.get('scanning'))
        full_scan(nav)
        songs=nav.request('search3',dict(query='',songCount=100,artistCount=0,albumCount=0)).get('searchResult3',{}).get('song',[])
        if len(songs)!=len(sources):raise AssertionError('Navidrome must index only the generated tracks')
        identity=songs[0]['id'];result['navidrome_version']=nav.request('ping')['serverVersion']
        accounts=[nav]
        response=requests.post(nav_url+'/api/user',headers=headers,json=dict(userName='fixture-listener',name='Fixture listener',password=password,isAdmin=False),timeout=10)
        response.raise_for_status();user_id=response.json()['id']
        library_id=int(nav.request('getMusicFolders')['musicFolders']['musicFolder'][0]['id'])
        requests.put(nav_url+'/api/user/'+user_id+'/library',headers=headers,json=dict(libraryIds=[library_id]),timeout=10).raise_for_status()
        accounts.append(Subsonic(nav_url,'fixture-listener',password))
        for index,account in enumerate(accounts):
            account.request('setRating',dict(id=identity,rating=5-index*3),write=True)
            account.request('star',dict(id=identity),write=True)
            account.request('scrobble',dict(id=identity,submission='true',time=int(time.time()*1000)),write=True)
            playlist=account.replace_playlist([identity,identity],name='Private fixture '+str(index))
            account.request('updatePlaylist',dict(playlistId=playlist['id'],public='false'),write=True)
        snapshots=[guard.capture(account,[row['id'] for row in songs]) for account in accounts]
        wait(lambda: api('GET','command'),lambda rows: not any(row['status'] in ('queued','started') for row in rows))
        rename=renames.plan(client,nav,root,album['id'])
        rename_journal=artifacts/'rename-journal.json'
        wait(lambda: renames.apply(client,nav,root,rename,rename_journal),
             lambda _: json.loads(rename_journal.read_text())['status']=='awaiting_full_scan')
        full_scan(nav)
        if renames.verify(client,nav,root,rename,rename_journal):raise AssertionError('real rename lost personal data')
        for account,snapshot in zip(accounts,snapshots):
            if not guard.verify(account,snapshot,rename['path_map'])['preserved']:raise AssertionError('real rename lost another account data')
        result['real_rename_preserved_two_users']=True
        wait(lambda: api('GET','command'),lambda rows: not any(row['status'] in ('queued','started') for row in rows))
        reverse=rename_rollback.plan(client,nav,root,rename,rename_journal)
        reverse_journal=artifacts/'rollback-journal.json'
        (artifacts/'rollback-plan.json').write_text(json.dumps(reverse,indent=2))
        wait(lambda: rename_rollback.apply(client,nav,root,reverse,reverse_journal),lambda status: status==0)
        full_scan(nav)
        if rename_rollback.verify(client,nav,root,reverse,reverse_journal):raise AssertionError('real rollback lost personal data')
        for account,snapshot in zip(accounts,snapshots):
            if not guard.verify(account,snapshot)['preserved']:raise AssertionError('real rollback lost another account data')
        result['real_rollback_preserved_two_users']=True
        # Exercise indexed consolidation with disjoint per-user history. Existing
        # users own the keeper's history; a third user owns only the extra copy.
        keeper_path=root/nav.filesystem_song(identity)['path']
        duplicate=root/'Nirvana/Duplicate fixture'/keeper_path.name
        alternate=artifacts/'alternate.flac'
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-v','error','-i',str(keeper_path),'-map_metadata','0',
                        '-c:a','flac','-compression_level','0',str(alternate)],check=True,timeout=60)
        duplicate.parent.mkdir();shutil.copy2(alternate,duplicate)
        # Discover it as genuinely unmapped before restoring exact tags/bytes.
        # Asking Lidarr 3.1 to identify a partial duplicate of a mapped album can
        # expand the group to existing paths and hit TrackFiles.Path uniqueness.
        blank=FLAC(duplicate);blank.clear();blank['title']='Unmapped fixture';blank['album']='Unmapped fixture';blank['artist']='Fixture'
        blank.save(padding=lambda info:max(0,info.padding))
        command=api('POST','command',dict(name='RescanFolders',folders=[str(duplicate.parent)],filter='known',addNewArtists=False,artistIds=[artist['id']]))
        wait(lambda: api('GET','command/'+str(command['id'])),completed)
        if not any(row['path']==str(duplicate) for row in api('GET','trackfile',unmapped='true')):
            raise AssertionError('duplicate fixture was not left unmapped in Lidarr')
        shutil.copy2(alternate,duplicate)
        full_scan(nav)
        page=nav.filesystem_page(0,100,False)['rows']
        copies=[row for row in page if row['path']==duplicate.relative_to(root).as_posix()]
        if len(copies)!=1:raise AssertionError('duplicate fixture does not have its own indexed identity')
        duplicate_id=copies[0]['id']
        response=requests.post(nav_url+'/api/user',headers=headers,json=dict(userName='fixture-third',name='Third fixture',password=password,isAdmin=False),timeout=10)
        response.raise_for_status();third_id=response.json()['id']
        requests.put(nav_url+'/api/user/'+third_id+'/library',headers=headers,json=dict(libraryIds=[library_id]),timeout=10).raise_for_status()
        third=Subsonic(nav_url,'fixture-third',password)
        third.request('setRating',dict(id=duplicate_id,rating=4),write=True)
        third.request('star',dict(id=duplicate_id),write=True)
        third.request('scrobble',dict(id=duplicate_id,submission='true',time=int(time.time()*1000)),write=True)
        third.request('createBookmark',dict(id=duplicate_id,position=1000,comment='Fixture bookmark'),write=True)
        private=third.replace_playlist([duplicate_id,identity,duplicate_id],name='Third private fixture')
        third.request('updatePlaylist',dict(playlistId=private['id'],public='false'),write=True)
        native=indexed_duplicates.NativeCLI(['/tmp/native-musl','/tmp/native-navidrome','--datafolder','/navdata','--musicfolder','/music','--nobanner','--loglevel','error'],artifacts/'native-backups')
        try:
            indexed_duplicates.plan(client,nav,root,{str(library_id):str(root)},identity,duplicate_id,artifacts/'quarantine',native)
        except ValueError as exc:
            if 'alternate-encode review' not in str(exc):raise
        else:
            raise AssertionError('alternate bytes were not held for explicit review')
        cleanup=indexed_duplicates.plan(client,nav,root,{str(library_id):str(root)},identity,duplicate_id,artifacts/'quarantine',native,allow_alternate=True)
        cleanup_journal=artifacts/'indexed-cleanup.json'
        (artifacts/'indexed-plan.json').write_text(json.dumps(cleanup,indent=2))
        if indexed_duplicates.apply(client,nav,root,cleanup,cleanup_journal,native)!=2:
            raise AssertionError('indexed cleanup should wait for its first new scan')
        full_scan(nav)
        indexed_duplicates.apply(client,nav,root,cleanup,cleanup_journal,native)
        full_scan(nav)
        indexed_duplicates.verify(client,nav,root,cleanup,cleanup_journal,native)
        for account,rating in zip([*accounts,third],[5,2,4]):
            row=account.song(duplicate_id)
            if row.get('userRating')!=rating or row.get('playCount')!=1 or not row.get('starred'):
                raise AssertionError('native remap lost one user rating/history/star')
        if [row['id'] for row in third.playlist(private['id'])['entry']]!=[duplicate_id]*3:
            raise AssertionError('native remap lost private repeated playlist references')
        result['real_indexed_cleanup_preserved_three_users']=True
        result['alternate_encode_required_explicit_review']=True
        result['success']=True
        print('Actual Lidarr rename, rollback and two-user Navidrome verification passed',flush=True)
    finally:
        (artifacts/'result.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result),flush=True)


if __name__=='__main__':
    main()
