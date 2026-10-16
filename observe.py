"""Host-owned, positive-evidence observation of a retained Docker container."""
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from . import __version__


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


class Chain:
    def __init__(self, path):
        self.file = path.open('x', encoding='utf-8')
        self.lock = threading.Lock()
        self.head, self.count = '0' * 64, 0

    def add(self, source, payload):
        with self.lock:
            item = dict(seq=self.count, source=source, payload=payload,
                        received_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        prev=self.head)
            digest = hashlib.sha256(canonical(item)).hexdigest()
            self.file.write(json.dumps(dict(item, hash=digest), ensure_ascii=False) + '\n')
            self.file.flush()
            self.head, self.count = digest, self.count + 1

    def close(self):
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()
        return dict(head=self.head, records=self.count, independent_anchor=False)


def verify_chain(folder):
    anchor = json.loads((folder / 'chain_anchor.json').read_text())
    head, count = '0' * 64, 0
    with (folder / 'events.jsonl').open() as stream:
        for line in stream:
            record = json.loads(line)
            digest = record.pop('hash')
            if record['prev'] != head or record['seq'] != count or hashlib.sha256(canonical(record)).hexdigest() != digest:
                raise ValueError('Invalid observation chain')
            head, count = digest, count + 1
    if head != anchor['head'] or count != anchor['records']:
        raise ValueError('Observation anchor mismatch')
    return anchor


def docker(*args, timeout=20):
    reply = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    if reply.returncode:
        raise RuntimeError('docker ' + args[0] + ': ' + reply.stderr.strip()[:1000])
    return reply.stdout


def validate_command(command):
    if command[:1] == ['--']:
        command = command[1:]
    if command[:2] != ['docker', 'run'] or len(command) < 3:
        raise ValueError('Expected -- docker run [OPTIONS] IMAGE [COMMAND...]')
    # Docker itself parses image arguments. Never allow auto-removal, attachment,
    # implicit pulls or a caller-supplied cidfile in this retained-container path.
    forbidden = ('--rm', '--cidfile', '--pull', '--detach', '--interactive', '--tty')
    for arg in command[2:]:
        if arg.split('=', 1)[0] in forbidden or (arg.startswith('-') and not arg.startswith('--') and any(c in arg[1:] for c in 'dit')):
            raise ValueError('Omit --rm, --pull, --cidfile and -d/-i/-t; observe retains the container and never pulls.')
    return command[2:]


def observation(args):
    folder = Path(args.output).resolve()
    try:
        command = validate_command(args.container_command)
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600:
            raise ValueError('timeout must be finite, greater than zero and at most 3600 seconds')
        for path in [*args.watch, *args.forbid_path]:
            if not path.startswith('/') or '..' in Path(path).parts:
                raise ValueError('watch and forbidden paths must be absolute container paths without ..')
        folder.mkdir(parents=True, exist_ok=False)
    except (OSError, ValueError) as exc:
        return dict(schema='kinesift.observe.v1', version=__version__, status='ERROR',
                    accepted=None, error=str(exc),
                    next_action='Check the command and paths with kinesift observe --help; use a new writable output directory.')
    chain = Chain(folder / 'events.jsonl')
    cid = sid = None
    streams = []
    issues, effects, hits = [], [], []
    start = time.monotonic()
    timed_out = False
    exit_code = None

    def launch_stream(source, argv):
        errors = (folder / (source + '.stderr')).open('x')
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=errors, text=True)
        state = dict(ready=False, count=0, errors=[], cadence=[])

        def collect():
            try:
                for line in proc.stdout:
                    item = json.loads(line)
                    chain.add(source, item)
                    state['count'] += 1
                    if item.get('kind') == 'ready':
                        state['ready'] = True
                    if item.get('kind') == 'source_error':
                        state['errors'].append(item)
                    if item.get('kind') == 'cadence':
                        state['cadence'].append(item)
                    if item.get('kind') in ('filesystem', 'processes', 'network_counters'):
                        effects.append(dict(source=source, **item))
                    if item.get('kind') == 'filesystem' and item.get('path'):
                        # Access/open events are observations, not mutations.
                        if item.get('mask', 0) & (0x2 | 0x4 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200):
                            match_path(item['path'], source)
            except (OSError, ValueError) as exc:
                state['errors'].append(str(exc))
        thread = threading.Thread(target=collect)
        thread.start()
        streams.append((source, proc, thread, errors, state))
        return state

    def match_path(path, source):
        for forbidden in args.forbid_path:
            if path == forbidden or path.startswith(forbidden.rstrip('/') + '/'):
                hit = dict(kind='forbidden_path', path=path, rule=forbidden, source=source)
                if hit not in hits:
                    hits.append(hit)

    try:
        docker('image', 'inspect', args.sensor_image)
        cid = docker('create', '--pull=never', '--label', 'kinesift.observe=' + uuid.uuid4().hex, *command).strip()
        chain.add('container', dict(id=cid, operation='created'))
        info = json.loads(docker('inspect', cid))[0]
        for mount in info['Mounts']:
            if mount['Type'] == 'bind':
                source = Path(mount['Source']).resolve()
                if source == folder or source in folder.parents or folder in source.parents:
                    raise ValueError('Evidence directory overlaps a target bind mount')
        chain.add('configuration', dict(image=info['Image'], mounts=info['Mounts'],
                                        path=info['Path'], args=info['Args']))
        events = launch_stream('docker_events', ['docker', 'events', '--since', str(int(time.time()) - 1),
                               '--filter', 'container=' + cid, '--format', '{{json .}}'])
        docker('start', cid)
        state = json.loads(docker('inspect', '--format', '{{json .State}}', cid))
        if state['Running']:
            code = Path(__file__).with_name('sensor.py').read_text()
            # Same broad VM privilege and per-target /proc source as the research.
            # No host directory or socket is mounted into this sensor.
            sid = docker('create', '--pull=never', '--privileged', '--pid=host', '--network=none',
                         '--user=0', '--memory=192m', '--cpus=0.5', '--pids-limit=32', '--entrypoint=python',
                         args.sensor_image, '-u', '-c', code, str(state['Pid']), json.dumps(args.watch)).strip()
            sensor = launch_stream('sensor', ['docker', 'start', '-a', sid])
            ready_until = min(start + args.timeout, time.monotonic() + 15)
            while not sensor['ready'] and time.monotonic() < ready_until:
                if streams[-1][1].poll() is not None:
                    break
                time.sleep(.02)
            if not sensor['ready']:
                issues.append('sensor_not_ready')
            while state['Running'] and time.monotonic() - start < args.timeout:
                for source, cmd in [('overlay_diff', ['diff', cid]), ('docker_top', ['top', cid, '-eo', 'pid,ppid,comm'])]:
                    try:
                        raw = docker(*cmd)
                        chain.add(source, dict(text=raw))
                        if source == 'overlay_diff':
                            for line in raw.splitlines():
                                if len(line) > 2:
                                    effect = dict(source=source, change=line[0], path=line[2:])
                                    if effect not in effects:
                                        effects.append(effect)
                                    match_path(line[2:], source)
                    except RuntimeError as exc:
                        current = json.loads(docker('inspect', '--format', '{{json .State}}', cid))
                        chain.add(source + '_unavailable', dict(error=str(exc), target_running=current['Running']))
                        if current['Running']:
                            issues.append(str(exc))
                time.sleep(.1)
                state = json.loads(docker('inspect', '--format', '{{json .State}}', cid))
            timed_out = state['Running']
            if timed_out:
                issues.append('timeout_target_stopped_no_retry')
                docker('stop', '--time', '1', cid)
        else:
            issues.append('target_exited_before_sensor_attachment')
        final = json.loads(docker('inspect', '--format', '{{json .State}}', cid))
        exit_code = final['ExitCode'] if not final['Running'] else None
        chain.add('final_state', final)
        raw = docker('diff', cid)
        chain.add('overlay_diff_final', dict(text=raw))
        for line in raw.splitlines():
            if len(line) > 2:
                effect = dict(source='overlay_diff_final', change=line[0], path=line[2:])
                effects.append(effect)
                match_path(line[2:], 'overlay_diff_final')
        logs = subprocess.run(['docker', 'logs', cid], capture_output=True, timeout=20)
        (folder / 'container.stdout').write_bytes(logs.stdout)
        (folder / 'container.stderr').write_bytes(logs.stderr)
        chain.add('logs', dict(returncode=logs.returncode,
                              stdout_sha256=hashlib.sha256(logs.stdout).hexdigest(),
                              stderr_sha256=hashlib.sha256(logs.stderr).hexdigest()))
        if logs.returncode:
            issues.append('container_logs_failed')
        if not events['count']:
            issues.append('docker_events_empty')
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        issues.append(type(exc).__name__ + ': ' + str(exc))
    finally:
        # Stop producers, then drain to EOF before sealing: no late record loss.
        for target in (sid, cid):
            if target:
                try:
                    docker('stop', '--time', '1', target)
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    issues.append('stop_unconfirmed:' + str(exc))
        for source, proc, thread, errors, state in streams:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                issues.append(source + '_forced_termination')
            thread.join()
            errors.close()
            if state['errors']:
                issues.append(source + '_source_errors')
            if source == 'sensor' and (not state['cadence'] or any(x['hz'] < 50 for x in state['cadence'])):
                issues.append('proc_cadence_unconfirmed_or_below_50hz')
            chain.add('source_summary', dict(source=source, **state))
    # Positive evidence can fail an attempt; a sampled observer cannot prove clean.
    status = 'FAILED' if hits or (exit_code not in (None, 0) and not timed_out) else 'UNKNOWN'
    report = dict(schema='kinesift.observe.v1', version=__version__, status=status,
                  accepted=False if status == 'FAILED' else None, container_id=cid,
                  sensor_container_id=sid, container_exit_code=exit_code, timed_out=timed_out,
                  elapsed_seconds=round(time.monotonic() - start, 3), side_effects=effects,
                  forbidden_hits=hits, source_errors=issues,
                  policy=dict(forbid_path=args.forbid_path, watch=args.watch),
                  coverage=dict(complete=False, startup_gap=True, process_sampling=True,
                                network_payloads=False, bind_mount_diff=False),
                  reason='POSITIVE_FAILURE_EVIDENCE' if status == 'FAILED' else 'INCOMPLETE_OBSERVATION_COVERAGE',
                  next_action=('Inspect evidence and reconcile the retained container; do not blindly rerun.' if cid else
                               'Check docker --version and docker info; verify the sensor and target images with '
                               'docker image inspect IMAGE. No image is pulled automatically. Inspect this evidence '
                               'before retrying with a new output directory.'),
                  evidence_directory=str(folder))
    chain.add('report', report)
    report['chain'] = chain.close()
    (folder / 'chain_anchor.json').write_text(json.dumps(report['chain'], indent=2) + '\n')
    (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report
