"""Opt-in integration: own local Navidrome process, generated audio, API-only checks.

Run with NAVIDROME_TEST_EXE pointing at a trusted Navidrome binary. No production
configuration, users, music or databases are used. Artifacts stay in a local temp
directory; the process is stopped on exit.
"""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import Mock

import imageio_ffmpeg
import requests

from navidrome_scripts import dates,date_evidence,navidrome_guard as guard,renames,tag_repair as tags,playlists
from navidrome_scripts.subsonic import Subsonic
from test_date_evidence import catalog
from test_rename_rollback import RollbackLidarr
from navidrome_scripts import rename_rollback
from test_tag_repair import exported
import test_unmapped as fixtures


def full_scan(nav):
    before=nav.request('getScanStatus')['scanStatus'].get('lastScan')
    nav.request('startScan',{'fullScan':'true'})
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        row=nav.request('getScanStatus')['scanStatus']
        if row.get('scanning') is False and row.get('lastScan')!=before and row.get('scanType')=='full':
            return row
        time.sleep(0.2)
    raise AssertionError('isolated full scan did not finish')


def initial_scan(nav):
    # A newly migrated database schedules its own full scan. Observe that real
    # startup scan instead of racing it with an immediate on-demand scan.
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        row=nav.request('getScanStatus')['scanStatus']
        if row.get('scanning') is False and row.get('lastScan') and row.get('scanType')=='full' and row.get('count')==1:
            return row
        time.sleep(0.2)
    raise AssertionError('isolated initial migration scan did not finish')


def main():
    binary=Path(os.environ['NAVIDROME_TEST_EXE']).resolve(strict=True)
    base=Path(tempfile.mkdtemp(prefix='navidrome-preservation-integration-'))
    root=base/'music';path=root/'Artist/Album/song.flac';path.parent.mkdir(parents=True)
    ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-t','1','-c:a','flac',str(path)],check=True)
    audio=tags.media(path)
    audio.title='Song';audio.artist='Artist';audio.album='Album';audio.track=1;audio.disc=1;audio.year=2003
    audio.mb_albumid=fixtures.RELEASE;audio.mb_releasegroupid=fixtures.GROUP;audio.mb_trackid=fixtures.RECORDING
    audio.mb_albumartistid=fixtures.ARTIST;audio.lyrics='Preserve fixture lyrics'
    audio.original_year=1;audio.original_month=1;audio.original_day=1
    audio.rg_track_gain=-5;audio.rg_album_gain=-5;audio.rg_track_peak=0.5;audio.rg_album_peak=0.5;audio.save()
    (path.parent/'cover.jpg').write_bytes(b'fixture sidecar bytes')
    path.with_suffix('.lrc').write_text('[00:00]fixture lyrics')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    config=base/'navidrome.toml'
    config.write_text('\n'.join(f'{key} = {json.dumps(value)}' for key,value in dict(
        MusicFolder=str(root),DataFolder=str(base/'data'),Address='127.0.0.1',Port=port,
        ScanSchedule='',EnableExternalServices=False,EnableGravatar=False,FFmpegPath=ffmpeg,LogLevel='info').items()),encoding='utf8')
    env={k:v for k,v in os.environ.items() if not k.startswith('ND_')}
    log=(base/'console.log').open('w',encoding='utf8')
    process=subprocess.Popen([str(binary),'--configfile',str(config)],cwd=base,env=env,stdout=log,stderr=log,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    url=f'http://127.0.0.1:{port}'
    result=dict(artifacts=str(base),isolated=True,production_untouched=True)
    try:
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if process.poll() is not None:
                raise AssertionError('isolated Navidrome exited; inspect '+str(base/'console.log'))
            try:
                if requests.get(url+'/ping',timeout=1).status_code<500:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.2)
        password=secrets.token_urlsafe(24)
        response=requests.post(url+'/auth/createAdmin',json=dict(username='fixture-admin',password=password),timeout=10)
        response.raise_for_status()
        admin_headers={'X-ND-Authorization':'Bearer '+response.json()['token']}
        nav=Subsonic(url,'fixture-admin',password)
        result['server_version']=nav.request('ping').get('serverVersion')
        initial_scan(nav)
        rows=nav.request('search3',dict(query='',songCount=10,artistCount=0,albumCount=0)).get('searchResult3',{}).get('song',[])
        if len(rows)!=1:
            raise AssertionError('fixture library must have exactly one indexed song')
        song=rows[0];identity=song['id']
        result['original_song_id']=identity;result['original_path']=song['path']
        nav.request('setRating',{'id':identity,'rating':5},write=True)
        nav.request('star',{'id':identity},write=True)
        nav.request('scrobble',{'id':identity,'submission':'true','time':int(time.time()*1000)},write=True)
        ratings_env=base/'fixture-ratings.env'
        ratings_env.write_text(f'BASE_URL={url}\nUSERNAME=fixture-admin\nPASSWORD={password}\n',encoding='utf8')
        rating_command=[sys.executable,'-B','-m','navidrome_scripts.artist_ratings','--env-file',str(ratings_env),
            '--artist',song['artistId'],'--artist-id','--log-dir',str(base/'ratings')]
        with (base/'ratings-console.log').open('w',encoding='utf8') as rating_log:
            for source,target,dry in [(5,4,True),(5,4,False),(5,4,False),(4,5,False)]:
                command=rating_command+['--from-rating',str(source),'--to-rating',str(target)]+(['--dry-run'] if dry else [])
                subprocess.run(command,cwd=base,env=env,stdout=rating_log,stderr=rating_log,check=True,timeout=30)
                if nav.song(identity).get('userRating')!=(source if dry else target):
                    raise AssertionError('packaged artist rating preview/update/resume failed')
        result['artist_ratings_preview_update_and_resume']=True
        playlist=nav.replace_playlist([identity,identity],name='Preservation fixture')
        listener_password=secrets.token_urlsafe(24)
        created=requests.post(url+'/api/user',headers=admin_headers,json=dict(userName='fixture-listener',name='Fixture Listener',
            password=listener_password,isAdmin=False),timeout=10)
        created.raise_for_status();listener_id=created.json()['id']
        library_id=int(nav.request('getMusicFolders')['musicFolders']['musicFolder'][0]['id'])
        granted=requests.put(url+'/api/user/'+listener_id+'/library',headers=admin_headers,json=dict(libraryIds=[library_id]),timeout=10)
        granted.raise_for_status()
        listener=Subsonic(url,'fixture-listener',listener_password)
        listener.request('setRating',{'id':identity,'rating':2},write=True)
        listener.request('star',{'id':identity},write=True)
        for offset in (2000,1000):
            listener.request('scrobble',{'id':identity,'submission':'true','time':int(time.time()*1000)-offset},write=True)
        listener_playlist=listener.replace_playlist([identity],name='Private listener fixture')
        listener.request('updatePlaylist',{'playlistId':listener_playlist['id'],'public':'false'},write=True)
        listener_baseline=guard.capture(listener,[identity])
        baseline=guard.capture(nav,[identity]);result['playlist_id']=playlist['id']
        source=dict(id='0123456789ABCDEFGHIJKL',name='Fixture',snapshot_id='fixed',items=[dict(item=dict(type='track',id='fixture',
            name='Song',artists=[dict(name='Artist')],album=dict(name='Album'),duration_ms=1000,external_ids={}))])
        playlist_state=base/'playlist-state.json'
        playlist_plan=playlists.preview(nav,source,playlist_state,target_id=playlist['id'])
        if playlists.apply(nav,playlist_plan,playlist_state)!=playlist['id'] or len(nav.playlist(playlist['id']).get('entry',[]))!=1:
            raise AssertionError('reviewed playlist synchronization failed')
        playlists.apply(nav,playlist_plan,playlist_state)
        playlists.restore(nav,playlist_plan,playlist_state)
        if len(nav.playlist(playlist['id']).get('entry',[]))!=2:
            raise AssertionError('playlist restore did not retain repeated entries')
        result['playlist_apply_resume_and_restore']=True
        playlists.export_m3u(playlist_plan,base/'fixture.m3u8',nav)
        result['m3u_actual_path']=(base/'fixture.m3u8').read_text().splitlines()[-1]
        if result['m3u_actual_path']!='Artist/Album/song.flac':
            raise AssertionError('M3U export used a virtual filename')
        client=RollbackLidarr(root);catalog_client=Mock();catalog_client.lookup_release.return_value=catalog()
        output=base/'tags.json';journal=base/'tags-journal.jsonl'
        def runner(command,log_path,env,**kwargs):
            if 'list' in command:
                return fixtures.RELEASE+'\n'
            if 'export' in command:
                Path(command[command.index('-o')+1]).write_text(json.dumps([exported(path)]))
            return ''
        payload=tags.make_plan(client,root,2,output,catalog=catalog_client,run=runner)
        if len(payload['entries'])!=1:
            raise AssertionError('fixture missing tags were not selected')
        tags.apply(client,root,payload,output,journal,catalog_client)
        date_audit=base/'dates.jsonl';date_plan=base/'dates.json';date_journal=base/'date-journal.jsonl'
        dates.audit(root,date_audit)
        date_evidence.make_plan(root,date_audit,date_plan,catalog_client)
        dates.apply_plan(root,date_plan,date_journal,catalog_client)
        result['date_repair_original_date']=date_evidence.semantic_dates(path)['original_date']
        if result['date_repair_original_date']!='1985-05':
            raise AssertionError('date repair did not preserve the reviewed month precision')
        try:
            renames.plan(client,nav,root,2)
        except ValueError as exc:
            if 'new full' not in str(exc):
                raise
            result['old_full_scan_rejected']=True
        else:
            raise AssertionError('rename was allowed without a post-tag full scan')
        full_scan(nav)
        result['after_tag_scan']=guard.verify(nav,baseline)
        if not result['after_tag_scan']['preserved']:
            raise AssertionError('tag scan lost personal data')
        result['listener_after_tag_scan']=guard.verify(listener,listener_baseline)
        if not result['listener_after_tag_scan']['preserved']:
            raise AssertionError('tag scan lost the other user personal data')
        rename_plan=renames.plan(client,nav,root,2)
        rename_journal=base/'rename-journal.json'
        renames.apply(client,nav,root,rename_plan,rename_journal)
        full_scan(nav)
        if renames.verify(client,nav,root,rename_plan,rename_journal):
            raise AssertionError('rename scan lost personal data')
        result['after_rename_scan']=json.loads(rename_journal.read_text())['verification']
        result['listener_after_rename_scan']=guard.verify(listener,listener_baseline,rename_plan['path_map'])
        if not result['listener_after_rename_scan']['preserved']:
            raise AssertionError('rename scan lost the other user personal data')
        result['final_song']=nav.song(identity)
        result['final_physical_path']=nav.filesystem_song(identity)['path']
        if result['final_song'].get('userRating')!=5 or int(result['final_song'].get('playCount',0))<1 or not result['final_song'].get('starred'):
            raise AssertionError('fixture personal metadata was not established/preserved')
        listener_song=listener.song(identity)
        if listener_song.get('userRating')!=2 or listener_song.get('playCount')!=2 or not listener_song.get('starred'):
            raise AssertionError('other user rating/history/star was not established/preserved')
        result['two_accounts_preserved']=True
        rollback_plan=rename_rollback.plan(client,nav,root,rename_plan,rename_journal)
        rollback_journal=base/'rollback-journal.json'
        rename_rollback.apply(client,nav,root,rollback_plan,rollback_journal)
        full_scan(nav)
        if rename_rollback.verify(client,nav,root,rollback_plan,rollback_journal):
            raise AssertionError('rollback scan lost personal data')
        result['after_rollback_scan']=json.loads(rollback_journal.read_text())['verification']
        result['listener_after_rollback_scan']=guard.verify(listener,listener_baseline)
        if not result['listener_after_rollback_scan']['preserved']:
            raise AssertionError('rollback scan lost the other user personal data')
        result['rollback_physical_path']=nav.filesystem_song(identity)['path']
        if result['rollback_physical_path']!='Artist/Album/song.flac':
            raise AssertionError('rollback did not restore the original physical path')
        final_baseline=guard.capture(nav,[identity])
        (root/'Artist/Album/song.flac').unlink()
        full_scan(nav)
        result['missing_file_detected']=not guard.verify(nav,final_baseline)['preserved']
        missing_index=guard.export_index(nav)
        if not any(row['id']==identity and row.get('missing') is True for row in missing_index['songs']):
            raise AssertionError('global absence check dropped an existing missing-file identity')
        result['missing_identity_protected_from_cleanup']=True
        result['native_missing_index_probe']=[]
        for missing in ('false','true'):
            response=requests.get(url+'/api/song',headers=admin_headers,params=dict(_start=0,_end=10,missing=missing),timeout=10)
            response.raise_for_status()
            result['native_missing_index_probe'].append(dict(missing_filter=missing,total=response.headers.get('X-Total-Count'),
                rows=[{k:r.get(k) for k in ('id','path','missing')} for r in response.json()]))
        if not result['missing_file_detected']:
            raise AssertionError('missing original identity was reported as preserved')
        result['success']=True
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=10)
        log.close()
        result['process_stopped']=process.poll() is not None
        (base/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
        print(json.dumps(result))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
