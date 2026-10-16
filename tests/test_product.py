import copy
import http.client
from importlib import metadata
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from kinesift import __version__
from kinesift.api import make_server
from kinesift.environment import Environment, rule_script
from kinesift.oracle import verify
from kinesift.run import run
from kinesift.service import CONTRACT, MAX_BYTES, TASKS, evaluate, execute


GOOD = {'task': 'route-0', 'seed': 11,
        'actions': [{'action': 'set_queue', 'value': 'finance'}]}


class EvaluationTests(unittest.TestCase):
    def test_all_tasks_and_seed_boundaries(self):
        for task in TASKS.values():
            for seed in (0, 11, 71, 2147483647):
                with self.subTest(task=task['id'], seed=seed):
                    observation = evaluate({'task': task['id'], 'seed': seed})
                    self.assertEqual(observation['status'], 'UNKNOWN')
                    action, value = rule_script(observation['observation'])
                    response = evaluate({'task': task['id'], 'seed': seed,
                                         'actions': [{'action': action, 'value': value}]})
                    self.assertEqual(response['status'], 'SUCCEEDED')
                    self.assertTrue(response['accepted'])
                    self.assertEqual(response['verification']['forbidden_mutations'], [])

    def test_success_has_provenance_and_detached_initial_observation(self):
        response = evaluate(GOOD)
        self.assertEqual(response['version'], '0.2.0')
        self.assertEqual(__version__, '0.2.0')
        self.assertEqual(len(response['contract_sha256']), 64)
        self.assertEqual(response['observation']['ticket']['queue'], 'unassigned')
        self.assertEqual(response['final_observation']['ticket']['queue'], 'finance')

    def test_unknown_and_empty_attempt_are_distinct(self):
        response = evaluate({'task': 'route-0'})
        self.assertEqual(response['status'], 'UNKNOWN')
        self.assertIsNone(response['accepted'])
        self.assertEqual(response['reason'], 'OBSERVATION_ONLY')
        self.assertNotIn('verification', response)
        response = evaluate({'task': 'route-0', 'actions': []})
        self.assertEqual(response['status'], 'FAILED')
        self.assertFalse(response['accepted'])

    def test_forbidden_attempt_after_correct_action_fails(self):
        request = copy.deepcopy(GOOD)
        request['actions'].append({'action': 'send_message', 'value': 'test'})
        response = evaluate(request)
        self.assertEqual(response['status'], 'FAILED')
        self.assertTrue(response['verification']['task_state_exact'])
        self.assertEqual(response['verification']['forbidden_attempts'], 1)
        self.assertEqual(response['verification']['forbidden_mutations'], [])
        self.assertFalse(response['transitions'][-1]['ok'])

    def test_wrong_unrelated_state_fails(self):
        request = copy.deepcopy(GOOD)
        request['actions'].append({'action': 'set_priority', 'value': 'urgent'})
        self.assertEqual(evaluate(request)['status'], 'FAILED')

    def test_invalid_value_is_transition_failure_final_state_still_graded(self):
        request = {'task': 'route-0', 'actions': [{'action': 'set_queue', 'value': 'invalid'}]}
        response = evaluate(request)
        self.assertEqual(response['status'], 'FAILED')
        self.assertFalse(response['transitions'][0]['ok'])
        request['actions'].extend(GOOD['actions'])
        self.assertEqual(evaluate(request)['status'], 'SUCCEEDED')

    def test_reset_and_replay_do_not_leak_state(self):
        before = evaluate({'task': 'route-0'})
        evaluate(GOOD)
        forbidden = {'task': 'route-0', 'actions': [{'action': 'pay', 'value': '1'}]}
        evaluate(forbidden)
        self.assertEqual(evaluate({'task': 'route-0'}), before)
        self.assertEqual(evaluate(GOOD), evaluate(GOOD))
        self.assertEqual(GOOD['actions'][0]['value'], 'finance')

    def test_error_and_action_schema_boundaries(self):
        invalid = [None, [], 'task', {}, {'task': []}, {'task': 'missing'},
                   {'task': 'route-0', 'seed': True}, {'task': 'route-0', 'seed': -1},
                   {'task': 'route-0', 'seed': 2147483648},
                   {'task': 'route-0', 'seed': 11.0},
                   {'task': 'route-0', 'actions': None},
                   {'task': 'route-0', 'actions': {}},
                   {'task': 'route-0', 'success': True},
                   {'task': 'route-0', 'actions': [{}]},
                   {'task': 'route-0', 'actions': [{'action': '', 'value': ''}]},
                   {'task': 'route-0', 'actions': [{'action': 'x' * 65, 'value': ''}]},
                   {'task': 'route-0', 'actions': [{'action': 'x', 'value': 'x' * 257}]},
                   {'task': 'route-0', 'actions': [{'action': 'set_queue', 'value': []}]},
                   {'task': 'route-0', 'actions': [{'action': 'set_queue', 'value': 'finance', 'extra': 1}]}]
        for request in invalid:
            with self.subTest(request=request):
                response = evaluate(request)
                self.assertEqual(response['status'], 'ERROR')
                self.assertIsNone(response['accepted'])
                self.assertIn('code', response['error'])
        action = {'action': 'x' * 64, 'value': 'x' * 256}
        self.assertEqual(evaluate({'task': 'route-0', 'actions': [action]})['status'], 'FAILED')

    def test_action_count_boundary(self):
        request = {'task': 'route-0', 'actions': GOOD['actions'] * 128}
        self.assertEqual(evaluate(request)['status'], 'SUCCEEDED')
        request['actions'].append(GOOD['actions'][0])
        self.assertEqual(evaluate(request)['status'], 'ERROR')

    def test_json_errors_and_size_boundary(self):
        for raw in (b'{', b'\xff', b'{"task":"route-0","task":"route-1"}',
                    b'{"task":"route-0","seed":NaN}', b'[' * 1100):
            with self.subTest(raw=raw[:50]):
                self.assertEqual(execute(raw)['status'], 'ERROR')
        raw = json.dumps(GOOD).encode()
        padded = raw + b' ' * (MAX_BYTES - len(raw))
        self.assertEqual(execute(padded)['status'], 'SUCCEEDED')
        self.assertEqual(execute(padded + b' ')['status'], 'ERROR')

    def test_evaluator_exception_is_unknown(self):
        with patch('kinesift.service.verify', side_effect=RuntimeError('interrupted')):
            response = evaluate(GOOD)
        self.assertEqual(response['status'], 'UNKNOWN')
        self.assertIsNone(response['accepted'])
        self.assertEqual(response['reason'], 'EVALUATION_INTERRUPTED')
        self.assertNotIn('verification', response)

    def test_existing_oracle_detects_out_of_band_mutation(self):
        env = Environment()
        observation = env.reset(11, TASKS['route-0'])
        env.step(*rule_script(observation))
        env.state['payments'].append('synthetic tamper')
        response = verify(env.initial, env.state, env.task, CONTRACT, [])
        self.assertFalse(response['accepted'])
        self.assertIn('payments', response['forbidden_mutations'])

    def test_original_benchmark_both_splits(self):
        for split in ('development', 'heldout'):
            with self.subTest(split=split):
                response = run(split)
                self.assertEqual(response['episodes'], 144)
                self.assertEqual(response['contract_checks'], 180)
                self.assertEqual(response['summary']['rule_script']['accepted'], 72)


class CLITests(unittest.TestCase):
    def test_installed_distribution_has_only_product_files_and_no_dependencies(self):
        distribution = metadata.distribution('kinesift')
        self.assertEqual(distribution.version, __version__)
        self.assertFalse(distribution.requires)
        package_files = {str(path) for path in distribution.files if str(path).startswith('kinesift/')
                         and '__pycache__' not in str(path)}
        self.assertEqual(package_files, {'kinesift/' + name for name in (
            '__init__.py', 'cli.py', 'api.py', 'service.py', 'environment.py',
            'oracle.py', 'run.py', 'contract.json', 'observe.py', 'sensor.py', 'harbor.py')})

    def invoke(self, raw, *args):
        # A separate process outside the source tree must use the installed package.
        with tempfile.TemporaryDirectory(prefix='kinesift-cli-') as cwd:
            return subprocess.run([sys.executable, '-m', 'kinesift.cli', *args],
                                  input=raw, capture_output=True, cwd=cwd)

    def test_status_exit_codes_from_installed_package(self):
        cases = [(GOOD, 0, 'SUCCEEDED'), ({'task': 'route-0', 'actions': []}, 1, 'FAILED'),
                 ({'task': 'unsupported'}, 2, 'ERROR'), ({'task': 'route-0'}, 3, 'UNKNOWN')]
        for request, code, status in cases:
            with self.subTest(status=status):
                response = self.invoke(json.dumps(request).encode(), 'run')
                self.assertEqual(response.returncode, code, response.stderr)
                self.assertEqual(json.loads(response.stdout)['status'], status)

    def test_installed_entrypoint_help_and_version(self):
        entrypoint = str(Path(sys.executable).with_name('kinesift'))
        response = subprocess.run([entrypoint, '--help'], capture_output=True, text=True)
        self.assertEqual(response.returncode, 0)
        self.assertIn('run', response.stdout)
        self.assertIn('serve', response.stdout)
        response = subprocess.run([entrypoint, '--version'], capture_output=True, text=True)
        self.assertEqual(response.stdout.strip(), '0.2.0')

    def test_invalid_json_and_unreadable_file(self):
        for raw, args in ((b'{', ('run',)),
                          (b'', ('run', '--input', '/nonexistent/kinesift-request.json'))):
            response = self.invoke(raw, *args)
            self.assertEqual(response.returncode, 2)
            self.assertEqual(json.loads(response.stdout)['status'], 'ERROR')

    def test_file_input_and_oversized_stdin(self):
        with tempfile.TemporaryDirectory(prefix='kinesift-input-') as cwd:
            path = Path(cwd) / 'request.json'
            path.write_text(json.dumps(GOOD))
            response = self.invoke(b'', 'run', '--input', str(path))
            self.assertEqual(response.returncode, 0)
        response = self.invoke(b' ' * (MAX_BYTES + 1), 'run')
        self.assertEqual(response.returncode, 2)

    def test_cli_interrupted_exit_code(self):
        from kinesift.cli import main
        import io
        with patch('kinesift.cli.sys.stdin') as stdin, patch('sys.stdout', new_callable=io.StringIO) as stdout:
            stdin.buffer.read.return_value = json.dumps(GOOD).encode()
            with patch('kinesift.service.verify', side_effect=RuntimeError('interrupted')):
                self.assertEqual(main(['run']), 3)
            self.assertEqual(json.loads(stdout.getvalue())['status'], 'UNKNOWN')


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, body, path='/run', method='POST', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            connection.request(method, path, body, headers or {'Content-Type': 'application/json'})
            response = connection.getresponse()
            content_type = response.getheader('Content-Type')
            data = json.loads(response.read())
            self.assertIn('application/json', content_type)
            return response.status, data
        finally:
            connection.close()

    def test_idle_and_partial_headers_do_not_block_later_request(self):
        for prefix in (b'', b'POST /run HTTP/1.1\r\nHost: localhost\r\n'):
            with self.subTest(prefix=prefix):
                accepted = threading.Event()
                finish_request = self.server.finish_request

                def finish_after_signal(request, client_address):
                    accepted.set()
                    finish_request(request, client_address)

                with patch.object(self.server, 'finish_request', side_effect=finish_after_signal):
                    with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=10) as idle:
                        if prefix:
                            idle.sendall(prefix)
                        self.assertTrue(accepted.wait(2), 'Idle connection was not accepted.')
                        status, response = self.request(json.dumps(GOOD))
                        self.assertEqual(status, 200)
                        self.assertEqual(response, evaluate(GOOD))
                        self.assertEqual(idle.recv(1), b'')

    def test_http_cli_result_parity(self):
        for request in (GOOD, {'task': 'route-0'}, {'task': 'route-0', 'actions': []}, {'task': 'bad'}):
            with self.subTest(request=request):
                status, response = self.request(json.dumps(request))
                self.assertEqual(response, evaluate(request))
                self.assertEqual(status, 400 if response['status'] == 'ERROR' else 200)

    def test_http_invalid_json_content_type_path_method_size(self):
        cases = [('{', '/run', 'POST', None, 400),
                 ('{}', '/other', 'POST', None, 404),
                 ('{}', '/run', 'GET', None, 501),
                 ('{}', '/run', 'POST', {'Content-Type': 'text/plain'}, 415),
                 ('{}', '/run', 'POST', {'Content-Type': 'application/json', 'Content-Length': str(MAX_BYTES + 1)}, 413),
                 ('{}', '/run', 'POST', {'Content-Type': 'application/json', 'Content-Length': '-1'}, 400),
                 ('{}', '/run', 'POST', {'Content-Type': 'application/json', 'Transfer-Encoding': 'chunked'}, 400)]
        for body, path, method, headers, expected in cases:
            with self.subTest(expected=expected, path=path, method=method):
                status, response = self.request(body, path, method, headers)
                self.assertEqual(status, expected)
                self.assertEqual(response['status'], 'ERROR')
                self.assertIsNone(response['accepted'])

    def test_http_missing_and_duplicate_length(self):
        for lengths, expected in (([], 411), (['2', '2'], 400)):
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
            try:
                connection.putrequest('POST', '/run')
                connection.putheader('Content-Type', 'application/json')
                for length in lengths:
                    connection.putheader('Content-Length', length)
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                self.assertEqual(json.loads(response.read())['status'], 'ERROR')
            finally:
                connection.close()

    def test_http_body_timeout_and_incomplete_body(self):
        for half_close, expected, code in ((False, 408, 'BODY_TIMEOUT'),
                                           (True, 400, 'INCOMPLETE_BODY')):
            with self.subTest(half_close=half_close):
                connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
                try:
                    connection.putrequest('POST', '/run')
                    connection.putheader('Content-Type', 'application/json')
                    connection.putheader('Content-Length', '2')
                    connection.endheaders(b'{')
                    if half_close:
                        connection.sock.shutdown(socket.SHUT_WR)
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                    self.assertEqual(response.status, expected)
                    self.assertEqual(payload['status'], 'ERROR')
                    self.assertEqual(payload['error']['code'], code)
                    self.assertIsNone(payload['accepted'])
                finally:
                    connection.close()
                status, response = self.request(json.dumps(GOOD))
                self.assertEqual(status, 200)
                self.assertEqual(response, evaluate(GOOD))

    def test_http_interrupted_is_unknown(self):
        with patch('kinesift.service.verify', side_effect=RuntimeError('interrupted')):
            status, response = self.request(json.dumps(GOOD))
        self.assertEqual(status, 500)
        self.assertEqual(response['status'], 'UNKNOWN')
        self.assertIsNone(response['accepted'])


if __name__ == '__main__':
    unittest.main()
