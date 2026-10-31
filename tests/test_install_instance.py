"""Installer recovery and private single-instance coordination without downloads."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install_environment as install
import instance_control as instance
import web_server as web


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        self.env = root/'.venv'; self.backup = root/'.venv.cutcut-backup'; self.runtime = root/'runtime'; self.runtime.mkdir()
        self.patches = [patch.object(install, 'ENV', self.env), patch.object(install, 'BACKUP', self.backup), patch.object(install, 'RUNTIME', self.runtime), patch.object(install, 'MARKER', self.runtime/'install-pending'), patch.object(instance, 'RUNTIME', self.runtime)]
        for p in self.patches: p.start()
    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()
    def old_environment(self):
        self.env.mkdir(); (self.env/'original').write_text('keep')
    def test_valid_environment_is_preserved(self):
        self.old_environment()
        with patch.object(install, 'valid', return_value=True), patch.object(install.subprocess, 'run') as run:
            self.assertEqual(install.main(), 0); run.assert_not_called()
        self.assertEqual((self.env/'original').read_text(), 'keep')
    def test_install_success_replaces_old_after_check(self):
        self.old_environment()
        def run(args, **kwargs):
            if '-m' in args and 'venv' in args:
                (self.env/'bin').mkdir(parents=True)
            return subprocess.CompletedProcess(args, 0)
        with patch.object(install, 'valid', return_value=False), patch.object(install.subprocess, 'run', side_effect=run):
            self.assertEqual(install.main(), 0)
        self.assertTrue(self.env.exists()); self.assertFalse(self.backup.exists()); self.assertFalse(install.MARKER.exists())
    def test_install_failure_restores_environment(self):
        self.old_environment()
        def run(*args, **kwargs):
            self.env.mkdir(); (self.env/'partial').touch()
            raise subprocess.CalledProcessError(1, args[0])
        with patch.object(install, 'valid', return_value=False), patch.object(install.subprocess, 'run', side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError): install.main()
        self.assertTrue((self.env/'original').exists()); self.assertFalse(self.backup.exists()); self.assertFalse(install.MARKER.exists())
    def test_interrupted_install_recovers_on_next_run(self):
        self.old_environment(); self.env.rename(self.backup); self.env.mkdir(); (self.env/'partial').touch()
        install.MARKER.write_text(json.dumps({'had_environment': True}))
        with patch.object(install, 'valid', return_value=True): self.assertEqual(install.main(), 0)
        self.assertTrue((self.env/'original').exists()); self.assertFalse((self.env/'partial').exists())
    def test_running_service_blocks_install(self):
        guard = instance.lock('install.lock', exclusive=False)
        try: self.assertEqual(install.main(), 1)
        finally: guard.close()


class InstanceTests(unittest.TestCase):
    def test_private_record_and_authenticated_reopen(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(instance, 'RUNTIME', Path(temp)):
            server = web.ThreadingHTTPServer(('127.0.0.1', 0), web.handler(web.Workspace(), 'token'))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                instance.publish(server.server_port, 'token')
                self.assertEqual((Path(temp)/'server.json').stat().st_mode & 0o777, 0o600)
                with patch.object(instance.webbrowser, 'open') as opened:
                    self.assertTrue(instance.reopen()); opened.assert_called_once()
                instance.publish(server.server_port, 'wrong')
                self.assertFalse(instance.reopen())
            finally:
                server.shutdown(); server.server_close(); thread.join()
    def test_lock_conflicts_and_releases(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(instance, 'RUNTIME', Path(temp)):
            first = instance.lock('server.lock')
            with self.assertRaises(BlockingIOError): instance.lock('server.lock')
            first.close(); second = instance.lock('server.lock'); second.close()

if __name__ == '__main__': unittest.main()
