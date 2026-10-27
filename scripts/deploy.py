"""Single-host Docker promotion with automatic readiness-failure rollback.

Run on the operator's Docker host, never from a bot command. Requires a built
immutable image, a local .env, and an existing/new container managed by this
script. A previous container is kept stopped after success for manual recovery.
"""
import argparse
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def docker(*args, check=True):
    return subprocess.run(['docker', *args], check=check, capture_output=True, text=True)


def ready(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status == 200 and json.load(response).get('ready') is True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def deploy(image, env_file, name, port, timeout=180):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,50}', name):
        raise ValueError('Invalid container name')
    if not 1024 <= port <= 65535 or not Path(env_file).is_file():
        raise ValueError('Provide a local .env and a port from 1024 to 65535')
    if not image or image.startswith('-') or any(c.isspace() for c in image):
        raise ValueError('Invalid image reference')
    # Require the artifact to already exist; no implicit registry authentication.
    docker('image', 'inspect', image)
    inspect = docker('container', 'inspect', name, check=False)
    old = json.loads(inspect.stdout)[0] if inspect.returncode == 0 else None
    if old and (old['Config'].get('Labels') or {}).get('vibetarana.managed') != 'true':
        raise ValueError('Existing container is not managed by this deploy script; migrate it manually first')
    backup = name + '-previous-' + str(int(time.time()))
    if old:
        docker('stop', '--time', '30', name)
        try:
            docker('rename', name, backup)
        except BaseException:
            docker('start', name)
            raise
    promoted = False
    try:
        docker('run', '-d', '--name', name, '--label', 'vibetarana.managed=true',
               '--restart', 'unless-stopped', '--env-file', str(Path(env_file).resolve()),
               '-e', 'PORT=8080', '-p', f'127.0.0.1:{port}:8080', image)
        deadline = time.monotonic() + timeout
        consecutive = 0
        while time.monotonic() < deadline:
            consecutive = consecutive + 1 if ready(f'http://127.0.0.1:{port}/health') else 0
            if consecutive >= 3:
                promoted = True
                return backup if old else None
            time.sleep(3)
        raise RuntimeError('New deployment failed readiness; previous container restored if available')
    finally:
        if not promoted:
            docker('rm', '-f', name, check=False)
            if old:
                docker('rename', backup, name)
                docker('start', name)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--name', default='vibetarana')
    parser.add_argument('--port', type=int, default=8080)
    args=parser.parse_args()
    backup=deploy(args.image,args.env_file,args.name,args.port)
    print('Ready. Previous stopped container: ' + (backup or 'none (first deployment)'))

if __name__=='__main__':
    main()
