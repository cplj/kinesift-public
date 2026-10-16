"""Run OpenHands' real local command API on its shipped executable, offline.

No model inference: the caller supplies the command. This verifies an external
agent runtime/tool path, not autonomous reasoning or task generalization.
"""
import argparse
from pathlib import Path
import subprocess
import sys

IMAGE = 'ghcr.io/openhands/agent-server@sha256:513e33ceb0ed660d4e1d55b55a78f9a3cd6853cef023c0cabdff58003daadf51'

# This runs inside the upstream image using its installed command API.
WORKLOAD = r'''
import hashlib
import json
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request

server_log = open('/tmp/openhands-server.log', 'w')
server = subprocess.Popen(['/usr/local/bin/openhands-agent-server', '--host', '127.0.0.1'],
                          stdout=server_log, stderr=subprocess.STDOUT)
try:
    deadline = time.monotonic() + 65
    while True:
        try:
            with urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2) as response:
                if response.status == 200:
                    break
        except (OSError, urllib.error.URLError):
            if time.monotonic() >= deadline or server.poll() is not None:
                raise RuntimeError('OpenHands server did not become ready')
            time.sleep(.25)
    command = 'sha256sum /usr/local/bin/openhands-agent-server | tee /home/openhands/kinesift-audit.txt'
    body = json.dumps(dict(command=command, cwd='/home/openhands', timeout=15)).encode()
    request = urllib.request.Request('http://127.0.0.1:8000/api/bash/execute_bash_command', data=body,
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    print(json.dumps(dict(openhands_command_result=result)), flush=True)
    if result.get('exit_code') != 0:
        raise RuntimeError('OpenHands command did not finish successfully')
    output = Path('/home/openhands/kinesift-audit.txt').read_text()
    expected = hashlib.sha256(Path('/usr/local/bin/openhands-agent-server').read_bytes()).hexdigest()
    if output.split()[0] != expected:
        raise RuntimeError('Independent executable SHA256 check failed')
    print(json.dumps(dict(object='/usr/local/bin/openhands-agent-server', sha256=expected,
                          output='/home/openhands/kinesift-audit.txt', output_verified=True)), flush=True)
    time.sleep(1)
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()
    server_log.close()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='New host evidence directory')
    parser.add_argument('--image', default=IMAGE, help='Locally available official image')
    parser.add_argument('--forbid-output', action='store_true', help='Negative control: forbid the actual audit file')
    args = parser.parse_args()
    forbidden = '/home/openhands/kinesift-audit.txt' if args.forbid_output else '/home/openhands/forbidden.txt'
    command = [str(Path(sys.executable).with_name('kinesift')), 'observe', '--output', args.output,
               '--sensor-image', args.image, '--watch', '/home/openhands', '--forbid-path', forbidden,
               '--timeout', '120', '--', 'docker', 'run', '--network=none', '--memory=1g',
               '--cpus=1', '--pids-limit=128', '--cap-drop=ALL', '--security-opt=no-new-privileges',
               '--entrypoint=python', '-e', 'OH_ENABLE_VSCODE=false', '-e', 'OPENHANDS_SUPPRESS_BANNER=1',
               args.image, '-u', '-c', WORKLOAD]
    return subprocess.run(command).returncode


if __name__ == '__main__':
    raise SystemExit(main())
