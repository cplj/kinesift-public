"""Seeded generative parser properties; no additional test dependency.

Reproduce: python -m unittest discover -s tests -p test_dx.py -v
Seed 20261015; 200 cases per generated property, nesting <= 4, width <= 5.
"""
import argparse
import io
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from kinesift.cli import main
from kinesift.harbor import import_harbor, read_json
from kinesift.observe import observation, validate_command
from kinesift.service import MAX_BYTES, TASKS, decode, execute

SEED = 20261015
CASES = 200


def json_value(rng, depth=0):
    if depth < 4 and rng.randrange(3) == 0:
        children = [json_value(rng, depth + 1) for _ in range(rng.randrange(6))]
        return children if rng.randrange(2) else {str(n): v for n, v in enumerate(children)}
    return rng.choice([None, True, False, rng.randint(-2**53, 2**53),
                       rng.uniform(-1e100, 1e100),
                       ''.join(rng.choices('abc"\\\n\x00é漢🙂', k=rng.randrange(65)))])


class ParserProperties(unittest.TestCase):
    def test_json_roundtrip_through_both_readers(self):
        rng = random.Random(SEED)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'result.json'
            for case in range(CASES):
                value = json_value(rng)
                raw = json.dumps(value, ensure_ascii=bool(case % 2), allow_nan=False).encode()
                with self.subTest(seed=SEED, case=case):
                    self.assertEqual(decode(raw), value)
                    path.write_bytes(raw)
                    self.assertEqual(read_json(path), value)

    def test_request_encoding_does_not_change_outcome(self):
        rng = random.Random(SEED + 1)
        for case in range(CASES):
            request = {'task': rng.choice(list(TASKS)), 'seed': rng.randrange(2**31)}
            if case % 3:
                request['actions'] = [{'action': rng.choice(['set_queue', 'set_status', 'send_message']),
                                       'value': rng.choice(['finance', 'resolved', '', '漢🙂'])}
                                      for _ in range(rng.randrange(8))]
            compact = json.dumps(request, separators=(',', ':')).encode()
            reordered = json.dumps(dict(reversed(list(request.items()))), indent=2,
                                   ensure_ascii=False).encode()
            with self.subTest(seed=SEED + 1, case=case):
                self.assertEqual(decode(reordered), request)
                self.assertEqual(execute(compact), execute(reordered))
                if 'actions' not in request:
                    self.assertEqual(execute(compact)['status'], 'UNKNOWN')
                    self.assertIsNone(execute(compact)['accepted'])

    def test_malformed_inputs_never_become_accepted(self):
        rng = random.Random(SEED + 2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'result.json'
            for case in range(CASES):
                key = json.dumps('key' + str(rng.randrange(10000)))
                variants = [f'{{{key}:1,{key}:2}}'.encode(),
                            b'{"seed":' + rng.choice([b'NaN', b'Infinity', b'-Infinity']) + b'}',
                            json.dumps(json_value(rng)).encode() + b'\xff',
                            json.dumps(json_value(rng)).encode() + b'!']
                for raw in variants:
                    with self.subTest(seed=SEED + 2, case=case, raw=raw[:80]):
                        response = execute(raw)
                        self.assertEqual(response['error']['code'], 'INVALID_JSON')
                        self.assertIsNone(response['accepted'])
                        path.write_bytes(raw)
                        with self.assertRaises((ValueError, UnicodeError)):
                            read_json(path)
        self.assertEqual(execute(b'[' * 1100)['status'], 'ERROR')

    def test_external_schema_and_byte_boundaries(self):
        rng = random.Random(SEED + 3)
        for case in range(CASES):
            invalid_seed = rng.choice([True, False, None, -rng.randrange(1, 2**31),
                                       2**31 + rng.randrange(2**31), rng.random(), '11'])
            request = {'task': rng.choice(list(TASKS)), 'seed': invalid_seed}
            with self.subTest(seed=SEED + 3, case=case):
                response = execute(json.dumps(request).encode())
                self.assertEqual(response['error']['code'], 'INVALID_SEED')
                self.assertIsNone(response['accepted'])
                valid = json.dumps({'task': request['task']}).encode()
                padded = valid + b' ' * (MAX_BYTES - len(valid))
                self.assertEqual(execute(padded)['status'], 'UNKNOWN')
                oversized = padded + b' ' * rng.randrange(1, 8193)
                self.assertEqual(execute(oversized)['error']['code'], 'INVALID_JSON')
                action = {'action': 'x' * rng.randrange(65, 129), 'value': ''}
                response = execute(json.dumps({'task': request['task'], 'actions': [action]}).encode())
                self.assertEqual(response['error']['code'], 'INVALID_ACTIONS')
        self.assertEqual(execute(b' ' * (2 * 1024 * 1024))['status'], 'ERROR')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'result.json'
            with path.open('wb') as stream:
                stream.seek(16 * 1024 * 1024)
                stream.write(b' ')
            with self.assertRaisesRegex(ValueError, '16 MiB'):
                read_json(path)

    def test_docker_command_tokens_and_rejected_options(self):
        rng = random.Random(SEED + 4)
        for case in range(CASES):
            payload = ['local-image', 'echo', str(rng.randrange(2**31)), '漢🙂']
            command = ['docker', 'run', '--network=none', *payload]
            with self.subTest(seed=SEED + 4, case=case):
                self.assertEqual(validate_command(command), command[2:])
                self.assertEqual(validate_command(['--', *command]), command[2:])
                option = rng.choice(['--rm', '--pull=always', '--cidfile=x', '-dit', '--detach'])
                with self.assertRaises(ValueError):
                    validate_command([*command[:2], option, *command[2:]])


class GuidanceTests(unittest.TestCase):
    def test_input_io_names_path_and_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / 'missing request.json')
            with patch('sys.stdout', new_callable=io.StringIO) as stdout:
                code = main(['run', '--input', missing])
            result = json.loads(stdout.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(result['error']['code'], 'INPUT_IO')
            self.assertIn(missing, result['error']['message'])
            self.assertIn('readable file', result['next_action'])
            self.assertIn('--input -', result['next_action'])
            self.assertIn('docs/schemas/request.json', result['next_action'])

    def test_http_guidance_addresses_transport(self):
        from kinesift.service import error
        result = error('CONTENT_TYPE', 'Use Content-Type: application/json.')
        self.assertIn('Content-Type: application/json', result['next_action'])

    def test_missing_docker_has_prerequisite_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(output=str(Path(tmp) / 'evidence'), timeout=1,
                                      sensor_image='external', watch=['/tmp'], forbid_path=[],
                                      container_command=['docker', 'run', 'external'])
            with patch('kinesift.observe.docker', side_effect=FileNotFoundError('docker')):
                result = observation(args)
            self.assertEqual(result['status'], 'UNKNOWN')
            self.assertIsNone(result['accepted'])
            self.assertIn('docker info', result['next_action'])
            self.assertIn('docker image inspect', result['next_action'])

    def test_harbor_error_explains_paths_and_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = import_harbor(argparse.Namespace(directory=tmp, observation=None, output=None))
        self.assertEqual(result['status'], 'ERROR')
        self.assertIn('result.json', result['next_action'])
        self.assertIn('new writable output', result['next_action'])
