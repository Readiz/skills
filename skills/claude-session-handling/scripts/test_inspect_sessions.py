import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('inspector', Path(__file__).with_name('inspect_sessions.py'))
inspector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspector)


class InspectorTests(unittest.TestCase):
    def test_project_filter_and_no_sensitive_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'sessions').mkdir()
            (root / 'settings.json').write_text(json.dumps({'model': 'opus', 'env': {'API_KEY': 'secret-value'}}))
            for name, cwd in [('one', '/example/work'), ('other', '/example/other')]:
                (root / 'sessions' / (name + '.json')).write_text(json.dumps({
                    'pid': 123, 'sessionId': name, 'bridgeSessionId': 'remote-' + name,
                    'cwd': cwd, 'status': 'idle', 'accessToken': 'secret-value',
                }))
            calls = []
            def run(args):
                calls.append(args)
                return 0, 'claude --model claude-fable-5-1 --sdk-url https://invalid.example/secret-value --access-token secret-value'
            result = inspector.inspect(root, Path('/example/work'), run=run)
            self.assertEqual([r['sessionId'] for r in result['sessions']], ['one'])
            self.assertEqual(result['sessions'][0]['model_argument'], 'claude-fable-5-1')
            self.assertNotIn('secret-value', json.dumps(result))
            self.assertTrue(all(args[0] == '/bin/ps' for args in calls))

    def test_restricted_process_lookup_is_unknown(self):
        result = inspector.process_metadata(123, lambda args: (None, ''))
        self.assertEqual(result['liveness'], 'unknown')
        with patch.object(inspector.os, 'kill', side_effect=PermissionError):
            self.assertEqual(inspector.process_metadata(123, lambda args: (1, ''))['liveness'], 'unknown')

    def test_definitely_exited_process_is_distinguished(self):
        with patch.object(inspector.os, 'kill', side_effect=ProcessLookupError):
            self.assertEqual(inspector.process_metadata(123, lambda args: (1, ''))['liveness'], 'exited')

    def test_malformed_registry_is_reported_without_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'sessions').mkdir()
            (root / 'sessions/broken.json').write_text('secret-content')
            result = inspector.inspect(root)
            self.assertEqual(result['errors'][0]['kind'], 'unreadable-or-invalid')
            self.assertNotIn('secret-content', json.dumps(result))

    def test_service_metadata_excludes_environment_and_nested_state(self):
        body = '\tstate = running\n\tpid = 123\n\tenvironment = {\n\t\tTOKEN = secret-value\n\t}\n\tresource = {\n\t\tstate = active\n\t}\n'
        result = inspector.service_metadata('com.example.claude', lambda args: (0, body))
        self.assertEqual(result['state'], 'running')
        self.assertNotIn('secret-value', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
