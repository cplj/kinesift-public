"""Stateless reset/replay adapter over the existing environment and oracle."""
import hashlib
import json
from pathlib import Path

from . import __version__
from .environment import Environment
from .oracle import verify

CONTRACT_BYTES = Path(__file__).with_name('contract.json').read_bytes()
CONTRACT = json.loads(CONTRACT_BYTES)
TASKS = {task['id']: task for task in CONTRACT['tasks']}
MAX_BYTES = 65536
MAX_ACTIONS = 128


def result(status, **fields):
    return {'schema': 'kinesift.run.v1', 'version': __version__,
            'data_mode': 'SYNTHETIC', 'status': status, 'accepted': None,
            'contract_sha256': hashlib.sha256(CONTRACT_BYTES).hexdigest(), **fields}


def error(code, message, next_action=None):
    return result('ERROR', error={'code': code, 'message': message},
                  next_action=next_action or (message + ' See the request and transport contract: '
                              'https://github.com/cplj/kinesift-public#result-states'))


def interrupted():
    return result('UNKNOWN', reason='EVALUATION_INTERRUPTED',
                  next_action='Retry the same synthetic reset/replay request; no session is retained.')


def _object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('Duplicate JSON keys are not supported.')
        obj[key] = value
    return obj


def _constant(value):
    raise ValueError('Non-finite JSON numbers are not supported.')


def decode(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError('Request exceeds 65536 bytes.')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_object, parse_constant=_constant)


def evaluate(request):
    if not isinstance(request, dict):
        return error('INVALID_INPUT', 'Request must be a JSON object.')
    if set(request) - {'task', 'seed', 'actions'}:
        return error('INVALID_INPUT', 'Only task, seed and actions are accepted.')
    task_id = request.get('task')
    if not isinstance(task_id, str) or task_id not in TASKS:
        return error('INVALID_TASK', 'task must be one of: ' + ', '.join(TASKS))
    seed = request.get('seed', 11)
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        return error('INVALID_SEED', 'seed must be an integer from 0 through 2147483647.')
    actions = request.get('actions')
    if 'actions' in request:
        if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
            return error('INVALID_ACTIONS', 'actions must be a list of at most 128 objects.')
        for action in actions:
            if (not isinstance(action, dict) or set(action) != {'action', 'value'}
                    or not isinstance(action['action'], str) or not 1 <= len(action['action']) <= 64
                    or not isinstance(action['value'], str) or len(action['value']) > 256):
                return error('INVALID_ACTIONS', 'Each action needs an action string (1-64 characters) and value string (0-256 characters).')
    try:
        env = Environment()
        observation = env.reset(seed, TASKS[task_id])
        common = {'task': task_id, 'seed': seed, 'observation': observation}
        if 'actions' not in request:
            return result('UNKNOWN', **common, reason='OBSERVATION_ONLY',
                          next_action='Let your agent choose actions, then submit the full actions list with the same task and seed.')
        transitions = [env.step(item['action'], item['value']) for item in actions]
        verification = verify(env.initial, env.state, env.task, CONTRACT, env.forbidden_attempts)
        return result('SUCCEEDED' if verification['accepted'] else 'FAILED', **common,
                      accepted=verification['accepted'], transitions=transitions,
                      final_observation=env.observe(), verification=verification)
    except Exception:
        # An interrupted evaluator must never imply a task failure or success.
        return interrupted()


def execute(raw):
    try:
        request = decode(raw)
    except (ValueError, UnicodeError, RecursionError):
        return error('INVALID_JSON', 'Provide one UTF-8 JSON object, at most 65536 bytes, without duplicate keys or non-finite numbers.')
    return evaluate(request)
