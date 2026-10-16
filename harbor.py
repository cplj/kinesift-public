"""Append local Kinesift evidence to Harbor trial results without changing them."""
import hashlib
import json
from pathlib import Path

from . import __version__
from .observe import verify_chain
from .service import _object, _constant


def read_json(path):
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('JSON file exceeds 16 MiB')
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_object, parse_constant=_constant)


def load_observation(path):
    folder = path.parent
    report = read_json(path)
    if not isinstance(report, dict) or report.get('schema') != 'kinesift.observe.v1' or report.get('status') not in ('FAILED', 'UNKNOWN'):
        raise ValueError('Unsupported observation schema or status')
    anchor = verify_chain(folder)
    if anchor != report['chain']:
        raise ValueError('Report chain does not match anchor')
    last = None
    with (folder / 'events.jsonl').open() as stream:
        for line in stream:
            last = json.loads(line)
    sealed = dict(report)
    sealed.pop('chain')
    if not last or last['source'] != 'report' or last['payload'] != sealed:
        raise ValueError('Report does not match sealed report record')
    return report


def import_harbor(args):
    try:
        root = Path(args.directory).resolve(strict=True)
        if not root.is_dir():
            raise ValueError('Harbor input must be a trial or job directory')
        paths = sorted(root.rglob('result.json'))
        trials = []
        for path in paths:
            if path.is_symlink() or root not in path.resolve().parents:
                raise ValueError('Result symlinks outside input are unsupported')
            raw = path.read_bytes()
            data = read_json(path)
            if not isinstance(data, dict):
                raise ValueError('Harbor result must be an object')
            if 'task_name' not in data and 'trial_name' not in data:
                continue  # Harbor job aggregate, not a trial.
            if not all(isinstance(data.get(k), str) and data[k] for k in ('task_name', 'trial_name')):
                raise ValueError('Harbor trial needs task_name and trial_name')
            trials.append((path, data, raw))
        if not trials:
            raise ValueError('No Harbor trial result.json with task_name and trial_name found')
        if args.observation and len(trials) != 1:
            raise ValueError('An explicit observation requires exactly one trial; point at its directory')
        rows = []
        for path, data, raw in trials:
            observation_path = Path(args.observation).resolve() if args.observation else path.parent / 'kinesift-observation' / 'report.json'
            report = load_observation(observation_path) if observation_path.exists() else None
            if args.observation and report is None:
                raise ValueError('Explicit observation file does not exist')
            exception = data.get('exception_info')
            verifier = data.get('verifier_result')
            rewards = verifier.get('rewards') if isinstance(verifier, dict) else None
            # Rewards are preserved, never translated into an unprovided success oracle.
            status = 'FAILED' if exception or (report and report['status'] == 'FAILED') else 'UNKNOWN'
            rows.append(dict(result_path=str(path.relative_to(root)),
                             result_sha256=hashlib.sha256(raw).hexdigest(), harbor_result=data,
                             harbor_rewards=rewards, observation=report, status=status,
                             accepted=False if status == 'FAILED' else None,
                             reason='HARBOR_EXCEPTION_OR_OBSERVED_FAILURE' if status == 'FAILED' else 'NO_COMPLETE_INDEPENDENT_OBSERVATION'))
        status = 'FAILED' if any(row['status'] == 'FAILED' for row in rows) else 'UNKNOWN'
        output = dict(schema='kinesift.harbor.v1', version=__version__, status=status,
                      accepted=False if status == 'FAILED' else None, trials=rows,
                      next_action='Review Harbor rewards separately; observation cannot prove full completion or safety.')
        destination = Path(args.output) if args.output else root / 'kinesift-import.json'
        with destination.open('x') as stream:
            json.dump(output, stream, indent=2, allow_nan=False)
            stream.write('\n')
        return output
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return dict(schema='kinesift.harbor.v1', version=__version__, status='ERROR',
                    accepted=None, error=str(exc),
                    next_action='Check the input directory, result.json and observation paths; use a new writable output file. '
                    'See kinesift import harbor --help and README.md#import-harbor-results-with-observations.')
