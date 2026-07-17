from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from tempfile import TemporaryDirectory

from crazy_harness.core.runtime.browser import BrowserRuntime


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<title>Source A</title><p data-evidence-id='fact-1'>verified fact</p>"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


with TemporaryDirectory() as tmp:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    evidence = BrowserRuntime().inspect(
        f"http://127.0.0.1:{server.server_port}", Path(tmp)
    )
    server.shutdown()
    assert (
        evidence.title == "Source A"
        and evidence.dom_path.exists()
        and evidence.screenshot_path.exists()
    )
