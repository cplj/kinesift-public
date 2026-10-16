import argparse
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from kinesift.cli import main
from kinesift.harbor import import_harbor, load_observation
from kinesift.observe import Chain, observation, validate_command, verify_chain


class ObservationTests(unittest.TestCase):
    def test_requires_docker_run_and_retains_evidence(self):
        for command in (['sh', '-c', 'true'], ['docker', 'run'],
                        ['docker', 'run', '--rm', 'external'],
                        ['docker', 'run', '--pull=always', 'external'],
                        ['docker', 'run', '-dit', 'external']):
            with self.subTest(command=command), self.assertRaises(ValueError):
                validate_command(command)
        self.assertEqual(validate_command(['--', 'docker', 'run', '--network=none', 'external', 'true']),
                         ['--network=none', 'external', 'true'])

    def test_unavailable_docker_is_unknown_with_sealed_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(output=str(Path(tmp) / 'evidence'), timeout=1,
                                      sensor_image='external', watch=['/tmp'], forbid_path=[],
                                      container_command=['docker', 'run', 'external'])
            with patch('kinesift.observe.docker', side_effect=FileNotFoundError('docker')):
                result = observation(args)
            self.assertEqual(result['status'], 'UNKNOWN')
            self.assertIsNone(result['accepted'])
            self.assertTrue(result['source_errors'])
            self.assertEqual(load_observation(Path(args.output) / 'report.json'), result)

    def test_bad_timeout_and_reused_directory_do_not_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            for seconds in (float('nan'), float('inf'), 0, -1, 3601, 1):
                args = argparse.Namespace(output=tmp, timeout=seconds, sensor_image='external',
                                          watch=['/tmp'], forbid_path=[],
                                          container_command=['docker', 'run', 'external'])
                with patch('kinesift.observe.docker') as docker:
                    self.assertEqual(observation(args)['status'], 'ERROR')
                    docker.assert_not_called()

    def test_chain_drains_concurrent_and_late_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            chain = Chain(path / 'events.jsonl')
            threads = [threading.Thread(target=lambda: [chain.add('source', {'n': n}) for n in range(50)])
                       for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            chain.add('late_record', {'present': True})
            anchor = chain.close()
            (path / 'chain_anchor.json').write_text(json.dumps(anchor))
            self.assertEqual(verify_chain(path)['records'], 151)
            self.assertIn('late_record', (path / 'events.jsonl').read_text())

    def test_chain_detects_mutation_truncation_and_reordering(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            chain = Chain(path / 'events.jsonl')
            chain.add('external', {'path': '/tmp/first'})
            chain.add('external', {'path': '/tmp/second'})
            (path / 'chain_anchor.json').write_text(json.dumps(chain.close()))
            raw = (path / 'events.jsonl').read_text()
            lines = raw.splitlines(True)
            for changed in (raw.replace('first', 'other'), lines[0], ''.join(reversed(lines))):
                (path / 'events.jsonl').write_text(changed)
                with self.assertRaises(ValueError):
                    verify_chain(path)

    def test_cli_unknown_and_error_have_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmp, patch('sys.stdout', new_callable=io.StringIO) as stdout:
            with patch('kinesift.observe.docker', side_effect=OSError('offline')):
                code = main(['observe', '--output', str(Path(tmp) / 'new'), '--', 'docker', 'run', 'external'])
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(stdout.getvalue())['status'], 'UNKNOWN')


class HarborTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trial = self.root / 'trial'
        self.trial.mkdir()
        self.result = {'task_name': 'external-task', 'trial_name': 'external-attempt',
                       'exception_info': None, 'verifier_result': {'rewards': {'reward': 1}}}
        self.path = self.trial / 'result.json'
        self.path.write_text(json.dumps(self.result))
        self.original = self.path.read_bytes()

    def invoke(self, observation=None, root=None):
        return import_harbor(argparse.Namespace(directory=str(root or self.trial),
                                                observation=observation, output=None))

    def attach(self, status='UNKNOWN'):
        folder = self.trial / 'kinesift-observation'
        folder.mkdir()
        report = dict(schema='kinesift.observe.v1', version='0.2.0', status=status,
                      accepted=False if status == 'FAILED' else None, side_effects=[],
                      forbidden_hits=[], source_errors=[])
        chain = Chain(folder / 'events.jsonl')
        chain.add('report', report)
        report['chain'] = chain.close()
        (folder / 'chain_anchor.json').write_text(json.dumps(report['chain']))
        path = folder / 'report.json'
        path.write_text(json.dumps(report))
        return path

    def test_rewards_do_not_imply_observation_success(self):
        response = self.invoke()
        self.assertEqual(response['status'], 'UNKNOWN')
        self.assertIsNone(response['accepted'])
        self.assertEqual(response['trials'][0]['harbor_rewards'], {'reward': 1})
        self.assertEqual(response['trials'][0]['harbor_result'], self.result)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_automatic_observation_is_attached_and_failed_is_preserved(self):
        self.attach('FAILED')
        response = self.invoke()
        self.assertEqual(response['status'], 'FAILED')
        self.assertFalse(response['accepted'])
        self.assertEqual(response['trials'][0]['observation']['status'], 'FAILED')
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_exception_is_failure_even_if_reward_positive(self):
        self.result['exception_info'] = {'exception_type': 'TimeoutError'}
        self.path.write_text(json.dumps(self.result))
        self.assertEqual(self.invoke()['status'], 'FAILED')

    def test_tampered_report_rejected(self):
        path = self.attach()
        obj = json.loads(path.read_text())
        obj['side_effects'] = ['invented']
        path.write_text(json.dumps(obj))
        self.assertEqual(self.invoke()['status'], 'ERROR')

    def test_explicit_missing_observation_and_multiple_trials_rejected(self):
        self.assertEqual(self.invoke(str(self.root / 'missing.json'))['status'], 'ERROR')
        observation = self.attach()
        other = self.root / 'other'
        other.mkdir()
        (other / 'result.json').write_bytes(self.original)
        self.assertEqual(self.invoke(str(observation), self.root)['status'], 'ERROR')

    def test_malformed_duplicate_nonfinite_input_and_no_trials(self):
        for raw in ('{', '{"task_name":"x","task_name":"y"}',
                    '{"task_name":"x","trial_name":"y","cost":NaN}', '{}', '[]'):
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                self.assertEqual(self.invoke()['status'], 'ERROR')

    def test_never_overwrites_append_output(self):
        first = self.invoke()
        saved = (self.trial / 'kinesift-import.json').read_bytes()
        self.assertEqual(first['status'], 'UNKNOWN')
        self.assertEqual(self.invoke()['status'], 'ERROR')
        self.assertEqual((self.trial / 'kinesift-import.json').read_bytes(), saved)

    def test_external_result_symlink_is_rejected(self):
        other = self.root / 'outside.json'
        other.write_bytes(self.original)
        (self.trial / 'extra').mkdir()
        (self.trial / 'extra' / 'result.json').symlink_to(other)
        self.assertEqual(self.invoke()['status'], 'ERROR')


if __name__ == '__main__':
    unittest.main()
