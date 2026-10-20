import http.client
import json
import os
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import auth
import server
import uploads


class UploadStorageTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.root = Path(self.folder.name) / 'uploads'
        self.addCleanup(self.folder.cleanup)

    def test_names_are_plain_and_keep_their_extension(self):
        for given, expected in (('shot.png', 'shot.png'), ('../../etc/passwd', 'passwd'), ('C:\\Users\\me\\a b.jpg', 'a_b.jpg'),
                                ('', 'file'), (None, 'file'), ('..', 'file'), ('.hidden', 'hidden'), ('wei rd (1).PNG', 'wei_rd_1.PNG')):
            self.assertEqual(uploads.safe_name(given), expected, given)
        long = uploads.safe_name('x' * 300 + '.png')
        self.assertLessEqual(len(long), uploads.NAME_LENGTH)
        self.assertTrue(long.endswith('.png'))

    def test_saved_files_share_one_private_folder_without_colliding(self):
        first = uploads.save('a.txt', b'one', root=self.root)
        second = uploads.save('a.txt', b'two', root=self.root)
        self.assertNotEqual(first, second)
        self.assertEqual((first.read_bytes(), second.read_bytes()), (b'one', b'two'))
        self.assertEqual(first.parent, self.root)
        self.assertTrue(first.name.endswith('-a.txt'))
        self.assertEqual(oct(first.stat().st_mode & 0o777), '0o600')
        self.assertEqual(oct(self.root.stat().st_mode & 0o777), '0o700')

    def test_empty_and_oversized_files_are_refused(self):
        with self.assertRaises(ValueError):
            uploads.save('a.txt', b'', root=self.root)
        with patch.object(uploads, 'LIMIT', 4), self.assertRaises(ValueError):
            uploads.save('a.txt', b'12345', root=self.root)

    def test_old_uploads_are_removed(self):
        old = uploads.save('old.txt', b'x', root=self.root)
        recent = uploads.save('new.txt', b'x', root=self.root)
        stale = time.time() - uploads.KEEP_SECONDS - 60
        os.utime(old, (stale, stale))
        uploads.prune(root=self.root)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())


class UploadEndpointTests(unittest.TestCase):
    CONFIG = {'id': '%1', 'agent': 'claude', 'thread': 'abc', 'writable': True}

    def setUp(self):
        self.folder = TemporaryDirectory()
        self.patches = [patch.object(auth, 'MODE', 'proxy'), patch.object(uploads, 'ROOT', Path(self.folder.name))]
        for item in self.patches:
            item.start()
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.addCleanup(lambda: [item.stop() for item in self.patches])

    def post(self, body=b'data', config=None, **headers):
        sent = {'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1', 'X-Session': '%1', 'X-Thread': 'abc',
                'X-Filename': 'My%20Shot.png', 'Content-Type': 'image/png'}
        sent.update(headers)
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_address[1], timeout=5)
        self.addCleanup(connection.close)
        with patch.object(server, 'choose', return_value=config or self.CONFIG):
            connection.request('POST', '/mobile/api/upload', body=body, headers=sent)
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b'{}')

    def test_an_upload_is_saved_and_its_path_returned(self):
        status, data = self.post(b'\x89PNG...')
        self.assertEqual(status, 200)
        self.assertTrue(data['name'].endswith('-My_Shot.png'))
        self.assertEqual(Path(data['path']).read_bytes(), b'\x89PNG...')
        self.assertTrue(Path(data['path']).resolve().is_relative_to(Path(self.folder.name).resolve()))

    def test_it_needs_the_same_origin_and_the_page_header(self):
        self.assertEqual(self.post(Origin='http://evil.test')[0], 403)
        self.assertEqual(self.post(**{'X-Codex-Mobile': '0'})[0], 403)

    def test_it_is_bound_to_the_current_conversation(self):
        self.assertEqual(self.post(**{'X-Thread': 'other'})[0], 409)

    def test_a_plain_shell_tab_or_read_only_session_cannot_take_attachments(self):
        self.assertEqual(self.post(config=dict(self.CONFIG, agent='shell'))[0], 503)
        self.assertEqual(self.post(config=dict(self.CONFIG, writable=False))[0], 503)
        self.assertEqual(list(Path(self.folder.name).iterdir()), [])

    def test_oversized_and_empty_bodies_are_refused_before_saving(self):
        with patch.object(uploads, 'LIMIT', 3):
            self.assertEqual(self.post(b'1234')[0], 413)
        self.assertEqual(self.post(b'')[0], 413)
        self.assertEqual(list(Path(self.folder.name).iterdir()), [])


class UploadSignInTests(unittest.TestCase):
    def test_signed_out_requests_are_refused(self):
        with patch.object(auth, 'MODE', 'token'):
            http_server = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            threading.Thread(target=http_server.serve_forever, daemon=True).start()
            self.addCleanup(http_server.server_close)
            self.addCleanup(http_server.shutdown)
            connection = http.client.HTTPConnection('127.0.0.1', http_server.server_address[1], timeout=5)
            self.addCleanup(connection.close)
            connection.request('POST', '/mobile/api/upload', body=b'x', headers={'Host': 'phone.test', 'Origin': 'http://phone.test', 'X-Codex-Mobile': '1'})
            self.assertEqual(connection.getresponse().status, 401)


if __name__ == '__main__':
    unittest.main()
