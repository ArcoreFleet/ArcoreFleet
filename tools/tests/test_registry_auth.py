import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'configure-registry-auth.py'


class RegistryAuthTests(unittest.TestCase):
    def test_failed_replace_preserves_old_file_and_removes_temporary_credentials(self):
        spec = importlib.util.spec_from_file_location('registry_auth', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            original = '{"auths":{"other.example":{"auth":"existing"}}}'
            path.write_text(original)
            with mock.patch.dict(os.environ, dict(DOCKER_CONFIG=folder, REGISTRY='registry.example',
                                REGISTRY_USERNAME='build', REGISTRY_PASSWORD='test-value')), \
                    mock.patch.object(module.os, 'replace', side_effect=PermissionError('locked')):
                with self.assertRaises(PermissionError):
                    module.main()
            self.assertEqual(path.read_text(), original)
            self.assertEqual(list(Path(folder).glob('.registry-auth-*')), [])

    def test_writes_credentials_without_logging_them_and_preserves_other_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps({'auths': {'other.example': {'auth': 'existing'}}, 'plugins': {}}))
            if os.name != 'nt':
                path.chmod(0o644)
            env = dict(os.environ, DOCKER_CONFIG=folder, REGISTRY='registry.example:30500',
                       REGISTRY_USERNAME='build', REGISTRY_PASSWORD='test-password-with-"quotes')
            result = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout + result.stderr, '')
            data = json.loads(path.read_text())
            self.assertEqual(data['auths']['other.example']['auth'], 'existing')
            self.assertEqual(base64.b64decode(data['auths'][env['REGISTRY']]['auth']).decode(),
                             env['REGISTRY_USERNAME'] + ':' + env['REGISTRY_PASSWORD'])
            if os.name != 'nt':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(folder).glob('.registry-auth-*')), [])

    def test_missing_password_does_not_write_auth_file(self):
        with tempfile.TemporaryDirectory() as folder:
            env = dict(os.environ, DOCKER_CONFIG=folder, REGISTRY='registry.example',
                       REGISTRY_USERNAME='build', REGISTRY_PASSWORD='')
            result = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(folder) / 'config.json').exists())
