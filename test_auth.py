import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.parse import urlencode

import auth
import server


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.patches = [mock.patch.object(auth, 'MODE', 'token'), mock.patch.object(auth, 'TOKEN_FILE', Path(self.folder.name) / 'token'),
                        mock.patch.dict(auth._failures, clear=True)]
        for patch in self.patches:
            patch.start()
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.port = self.http.server_address[1]

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        for patch in self.patches:
            patch.stop()
        self.folder.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port)
        connection.request(method, path, body=body, headers=dict({'Host': 'phone.test'}, **(headers or {})))
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def sign_in(self, token):
        return self.request('POST', '/mobile/login', urlencode({'token': token}),
                            {'Origin': 'http://phone.test', 'Content-Type': 'application/x-www-form-urlencoded'})

    def test_token_file_is_created_private_and_stable(self):
        first = auth.token()
        self.assertEqual(first, auth.token())
        self.assertEqual(auth.TOKEN_FILE.stat().st_mode & 0o777, 0o600)

    def test_pages_and_api_need_sign_in(self):
        page, _ = self.request('GET', '/mobile/')
        self.assertEqual((page.status, page.getheader('Location')), (302, '/mobile/login'))
        for path in ('/mobile/api/sessions', '/mobile/api/chat?session=%251', '/mobile/api/stream', '/mobile/app.js'):
            self.assertIn(self.request('GET', path)[0].status, (302, 401), path)
        post, _ = self.request('POST', '/mobile/api/message', json.dumps({'message': 'x'}),
                               {'Origin': 'http://phone.test', 'X-Codex-Mobile': '1', 'Content-Length': '15'})
        self.assertEqual(post.status, 401)
        self.assertEqual(self.request('DELETE', '/mobile/api/session', '{}', {'Origin': 'http://phone.test', 'X-Codex-Mobile': '1'})[0].status, 401)

    def test_login_page_and_icons_are_public(self):
        self.assertEqual(self.request('GET', '/mobile/login')[0].status, 200)
        self.assertEqual(self.request('GET', '/mobile/icon-192.png')[0].status, 200)

    def test_right_token_sets_a_session_cookie(self):
        response, _ = self.sign_in(auth.token())
        cookie = response.getheader('Set-Cookie')
        self.assertEqual((response.status, response.getheader('Location')), (303, '/mobile/'))
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertNotIn(auth.token(), cookie)
        page, _ = self.request('GET', '/mobile/', headers={'Cookie': cookie.split(';')[0]})
        self.assertEqual(page.status, 200)

    def test_wrong_token_and_forged_cookie_are_refused(self):
        self.assertEqual(self.sign_in('nope')[0].status, 401)
        self.assertEqual(self.request('GET', '/mobile/api/sessions', headers={'Cookie': auth.COOKIE + '=forged'})[0].status, 401)

    def test_repeated_failures_are_blocked_even_for_the_right_token(self):
        for _ in range(auth.LIMIT):
            self.sign_in('nope')
        response, data = self.sign_in(auth.token())
        self.assertEqual(response.status, 401)
        self.assertIn(b'Too many', data)

    def test_login_from_another_site_is_refused(self):
        response, _ = self.request('POST', '/mobile/login', urlencode({'token': auth.token()}), {'Origin': 'https://evil.test'})
        self.assertEqual(response.status, 403)

    def test_origin_must_match_host_but_either_scheme_works(self):
        self.assertTrue(auth.same_origin('http://a:1', 'a:1'))
        self.assertTrue(auth.same_origin('https://a', 'a'))
        self.assertFalse(auth.same_origin('https://b', 'a'))
        self.assertFalse(auth.same_origin('', 'a'))
        self.assertFalse(auth.same_origin('null', 'a'))

    def test_proxy_mode_trusts_the_proxy_only_on_loopback(self):
        with mock.patch.object(auth, 'MODE', 'proxy'):
            self.assertTrue(auth.signed_in(None))
            auth.check_startup('127.0.0.1')
            with self.assertRaises(SystemExit):
                auth.check_startup('0.0.0.0')

    def test_live_terminal_is_proxied_only_after_sign_in(self):
        import socket
        upstream = socket.socket()
        upstream.bind(('127.0.0.1', 0))
        upstream.listen(1)
        seen = []

        def serve():
            connection, _ = upstream.accept()
            seen.append(connection.recv(4096).decode())
            connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi')
            connection.close()
        threading.Thread(target=serve, daemon=True).start()
        with mock.patch.object(server, 'TTYD_PORT', upstream.getsockname()[1]):
            self.assertEqual(self.request('GET', '/terminal/live/')[0].status, 302)
            self.assertEqual(seen, [])
            cookie = self.sign_in(auth.token())[0].getheader('Set-Cookie').split(';')[0]
            response, body = self.request('GET', '/terminal/live/?arg=%241', headers={'Cookie': cookie})
        upstream.close()
        self.assertEqual((response.status, body), (200, b'hi'))
        self.assertIn('GET /terminal/live/?arg=%241 HTTP/1.1', seen[0])
        self.assertNotIn(auth.COOKIE, seen[0])   # the sign-in cookie is not handed on to ttyd


if __name__ == '__main__':
    unittest.main()
