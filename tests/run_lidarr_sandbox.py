"""Run real-service acceptance in disposable Docker volumes, without host mounts.

Run from the repository: python tests/run_lidarr_sandbox.py
Requires Docker and internet access for images, Python test dependencies and
Lidarr's metadata service. No production service configuration is read.
"""
import json
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile


LIDARR_IMAGE = 'lscr.io/linuxserver/lidarr@sha256:d8549ff9ea29b4015c32257360652c1000c31ea471a2b3f0835ae77292faf35b'
NAVIDROME_IMAGE = 'deluan/navidrome@sha256:38dc2727bfcfd5ede290f8ada114fc90368146f265ae4701ddddbcbe2a44ee52'


def docker(*args, capture=False):
    return subprocess.run(['docker', *args], check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def main():
    repo = Path(__file__).resolve().parents[1]
    prefix = 'nav-fixture-' + secrets.token_hex(5)
    token, key = secrets.token_hex(24), secrets.token_hex(16)
    output = repo/'logs'/prefix;output.mkdir(parents=True)
    lidarr, nav, runner = (prefix+'-'+name for name in ('lidarr','navidrome','runner'))
    volumes = {name: prefix+'-'+name for name in ('music','lidarr-data','nav-data','artifacts')}
    image = 'navidrome-scripts:'+prefix
    containers, created_volumes = [], []
    network_created = False
    result = dict(output=str(output), isolated=True, production_untouched=True,
                  lidarr_image=LIDARR_IMAGE, navidrome_image=NAVIDROME_IMAGE)
    try:
        docker('build','--tag',image,str(repo))
        docker('network','create',prefix,capture=True);network_created=True
        for name in volumes.values():
            docker('volume','create',name,capture=True);created_volumes.append(name)
        docker('run','--rm','--network','none','--entrypoint','sh','-v',volumes['music']+':/music',
               '-e','SANDBOX_TOKEN='+token,image,'-c','printf %s "$SANDBOX_TOKEN" > /music/.integration-sandbox')
        with tempfile.TemporaryDirectory(prefix='lidarr-fixture-config-') as directory:
            config=Path(directory)/'config.xml'
            config.write_text('<Config><BindAddress>*</BindAddress><Port>8686</Port><EnableSsl>False</EnableSsl>'
                '<LaunchBrowser>False</LaunchBrowser><ApiKey>'+key+'</ApiKey><AuthenticationMethod>External</AuthenticationMethod>'
                '<AuthenticationRequired>DisabledForLocalAddresses</AuthenticationRequired><AnalyticsEnabled>False</AnalyticsEnabled>'
                '<UpdateAutomatically>False</UpdateAutomatically><UpdateMechanism>External</UpdateMechanism></Config>')
            docker('create','--name',lidarr,'--network',prefix,'-e','PUID=0','-e','PGID=0',
                   '-v',volumes['music']+':/music','-v',volumes['lidarr-data']+':/config',LIDARR_IMAGE,capture=True)
            containers.append(lidarr);docker('cp',str(config),lidarr+':/config/config.xml')
        docker('create','--name',nav,'--network',prefix,'--user','0:0',
               '-v',volumes['music']+':/music:ro','-v',volumes['nav-data']+':/data',
               '-e','ND_SCANSCHEDULE=','-e','ND_ENABLEEXTERNALSERVICES=false','-e','ND_ENABLEGRAVATAR=false',
               NAVIDROME_IMAGE,capture=True)
        containers.append(nav)
        docker('start',lidarr,nav,capture=True)
        docker('create','--name',runner,'--network',prefix,'--entrypoint','sh',
               '-v',volumes['music']+':/music','-v',volumes['artifacts']+':/artifacts',
               '-v',volumes['nav-data']+':/navdata',
               '-e','INTEGRATION_SANDBOX_TOKEN='+token,'-e','LIDARR_TEST_URL=http://'+lidarr+':8686',
               '-e','LIDARR_TEST_KEY='+key,'-e','NAVIDROME_TEST_URL=http://'+nav+':4533',
               image,'-c','apt-get update >/tmp/test-dependencies.log 2>&1 && apt-get install -y --no-install-recommends rsgain >>/tmp/test-dependencies.log 2>&1 && python -m pip install imageio-ffmpeg >>/tmp/test-dependencies.log 2>&1 && cd /work && python -B /app/tests/integration_lidarr.py',capture=True)
        containers.append(runner)
        with tempfile.TemporaryDirectory(prefix='arr-fixture-hooks-') as directory:
            hooks=Path(directory)/'arr-hooks';(hooks/'python').mkdir(parents=True)
            for source,target in ((repo.parent/'arr-scripts/lidarr/ReplayGainTagger.bash',hooks/'ReplayGainTagger.bash'),
                                  (repo.parent/'arr-scripts/lidarr/python/Lyrics.py',hooks/'python/Lyrics.py')):
                shutil.copy2(source,target)
            docker('cp',str(hooks),runner+':/tmp/arr-hooks')
            native=Path(directory)/'native-navidrome';loader=Path(directory)/'native-musl'
            docker('cp',nav+':/app/navidrome',str(native));docker('cp','-L',nav+':/lib/ld-musl-x86_64.so.1',str(loader))
            docker('cp',str(native),runner+':/tmp/native-navidrome');docker('cp',str(loader),runner+':/tmp/native-musl')
        # docker start -a can itself return zero when the attached program fails.
        docker('start','-a',runner)
        status=int(docker('inspect','--format','{{.State.ExitCode}}',runner,capture=True).strip())
        docker('cp',runner+':/artifacts/.',str(output))
        if status:
            raise RuntimeError('sandbox runner failed with status '+str(status)+'; inspect '+str(output))
        result['success']=True
    finally:
        if runner in containers:
            subprocess.run(['docker','cp',runner+':/artifacts/.',str(output)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            subprocess.run(['docker','cp',runner+':/tmp/test-dependencies.log',str(output/'test-dependencies.log')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for container in containers:
            with (output/(container+'.log')).open('w',encoding='utf8') as log:
                subprocess.run(['docker','logs',container],stdout=log,stderr=log)
            subprocess.run(['docker','rm','-f',container],stdout=subprocess.DEVNULL,check=False)
        if network_created:
            subprocess.run(['docker','network','rm',prefix],stdout=subprocess.DEVNULL,check=False)
        for volume in created_volumes:
            subprocess.run(['docker','volume','rm',volume],stdout=subprocess.DEVNULL,check=False)
        subprocess.run(['docker','image','rm',image],stdout=subprocess.DEVNULL,check=False)
        result['cleanup_attempted']=True
        (output/'sandbox.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result),flush=True)


if __name__=='__main__':
    main()
