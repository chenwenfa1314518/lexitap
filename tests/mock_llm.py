"""A tiny OpenAI-compatible server for the tests: streams a fake translation (one fake paragraph per
source paragraph), answers in one JSON piece under /plain, fails under /bad (HTTP 401) and /sseerr,
and under /geo refuses requests that do not come through a proxy (HTTP 403)."""
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    last, count, keep = None, 0, 99  # last request, requests so far, paragraphs to answer

    def log_message(self, *a):
        pass

    def send_json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        Handler.last, Handler.count = (self.path, dict(self.headers), body), Handler.count + 1
        if self.path.startswith('/bad'):
            return self.send_json(401, {'error': {'message': 'Authentication Fails, your api key is invalid'}})
        if self.path.startswith('/geo'):  # a proxy sends the whole address, starting with http://
            return self.send_json(403, {'error': {'message': 'Country, region, or territory not supported'}})
        paras = body['messages'][-1]['content'].split('\n\n')
        reply = '\n\n'.join(f'【译{i + 1}】{s[:20]}' for i, s in enumerate(paras[:Handler.keep]))
        if self.path.startswith('/plain'):
            return self.send_json(200, {'choices': [{'message': {'content': reply}}]})
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()

        def chunk(s):
            b = s.encode()
            self.wfile.write(b'%x\r\n%s\r\n' % (len(b), b))
            self.wfile.flush()
        chunk(': keep-alive\n\n')
        if self.path.startswith('/sseerr'):
            chunk('data: ' + json.dumps({'error': {'message': 'rate limit reached'}}) + '\n\n')
        else:
            for i in range(0, len(reply), 5):
                chunk('data: ' + json.dumps({'choices': [{'delta': {'content': reply[i:i + 5]}}]}, ensure_ascii=False) + '\n\n')
            chunk('data: [DONE]\n\n')
        self.wfile.write(b'0\r\n\r\n')
        self.wfile.flush()


def start():
    srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f'http://127.0.0.1:{srv.server_address[1]}'
