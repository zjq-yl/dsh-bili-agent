"""Exercise the real opener across stale credentials and a not-yet-ready worker."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


@unittest.skipUnless(os.name == 'nt' and shutil.which('powershell'), 'Windows opener')
class PanelOpenerTests(unittest.TestCase):
    def test_opener_rereads_rotated_address_until_worker_is_ready(self):
        policy = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', 'Get-ExecutionPolicy'],
            check=True, capture_output=True, timeout=15,
        )
        if policy.stdout.strip() == b'Restricted':
            self.skipTest('PowerShell execution policy prohibits scripts; policy is not changed by tests')
        root = Path(__file__).resolve().parents[2]
        old_token, new_token = 'a' * 64, 'b' * 64
        calls = {'old': 0, 'new': 0}
        with tempfile.TemporaryDirectory(prefix='panel-opener-') as directory:
            address = Path(directory) / 'panel.json'

            class ReadyHandler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    calls['new'] += 1
                    authorized = self.headers.get('Authorization') == 'Bearer ' + new_token
                    self.send_response(200 if authorized else 401)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'ready': authorized and calls['new'] >= 2}).encode())

            new_server = ThreadingHTTPServer(('127.0.0.1', 0), ReadyHandler)
            new_url = f'http://127.0.0.1:{new_server.server_port}/#{new_token}'

            class StaleHandler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    calls['old'] += 1
                    address.write_text(json.dumps({'url': new_url}), encoding='utf-8')
                    self.send_response(401)
                    self.end_headers()

            old_server = ThreadingHTTPServer(('127.0.0.1', 0), StaleHandler)
            address.write_text(json.dumps({'url': f'http://127.0.0.1:{old_server.server_port}/#{old_token}'}), encoding='utf-8')
            threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (old_server, new_server)]
            for thread in threads:
                thread.start()
            try:
                result = subprocess.run(
                    ['powershell', '-NoProfile', '-NonInteractive', '-File', str(root / 'scripts/open-panel.ps1'),
                     '-DataDir', directory, '-WaitSeconds', '5', '-CheckOnly'],
                    capture_output=True, timeout=20,
                )
                output = (result.stdout + result.stderr).decode(errors='replace')
                self.assertEqual(result.returncode, 0, output)
                self.assertIn('Panel is ready.', output)
                self.assertEqual(calls['old'], 1)
                self.assertGreaterEqual(calls['new'], 2)
                self.assertNotIn(old_token, output)
                self.assertNotIn(new_token, output)
            finally:
                for server in (old_server, new_server):
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(timeout=2)
