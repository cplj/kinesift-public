"""Loopback-only HTTP transport for the reset/replay command."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from .service import MAX_BYTES, error, execute


class Handler(BaseHTTPRequestHandler):
    timeout = 5

    def log_message(self, format, *args):
        pass

    def reply(self, code, payload):
        body = (json.dumps(payload, allow_nan=False) + '\n').encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != '/run':
            self.reply(404, error('NOT_FOUND', 'Use POST /run.'))
            return
        if self.headers.get('Transfer-Encoding'):
            self.reply(400, error('INVALID_FRAMING', 'Use Content-Length; transfer encoding is unsupported.'))
            return
        lengths = self.headers.get_all('Content-Length', [])
        if not lengths:
            self.reply(411, error('LENGTH_REQUIRED', 'Content-Length is required.'))
            return
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            self.reply(400, error('INVALID_LENGTH', 'Use a single non-negative Content-Length.'))
            return
        # Bound before integer conversion, including maliciously long numerals.
        if len(lengths[0]) > 8 or int(lengths[0]) > MAX_BYTES:
            self.reply(413, error('REQUEST_TOO_LARGE', 'Request exceeds 65536 bytes.'))
            return
        if self.headers.get_content_type() != 'application/json':
            self.reply(415, error('CONTENT_TYPE', 'Use Content-Type: application/json.'))
            return
        self.connection.settimeout(5)
        try:
            raw = self.rfile.read(int(lengths[0]))
        except TimeoutError:
            self.reply(408, error('BODY_TIMEOUT', 'Request body was not received within five seconds.'))
            return
        if len(raw) != int(lengths[0]):
            self.reply(400, error('INCOMPLETE_BODY', 'Request body is shorter than Content-Length.'))
            return
        payload = execute(raw)
        code = 400 if payload['status'] == 'ERROR' else 200
        if payload.get('reason') == 'EVALUATION_INTERRUPTED':
            code = 500
        self.reply(code, payload)

    def send_error(self, code, message=None, explain=None):
        self.reply(code, error('HTTP_ERROR', 'Use POST /run with a JSON request.'))


def make_server(port):
    return HTTPServer(('127.0.0.1', port), Handler)
