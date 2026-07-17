from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from crazy_harness.core.runtime.browser import BrowserRuntime


class PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html><head><title>Crazy Lab</title></head><body><h1>ok</h1><script>console.log('ready')</script></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def test_browser_runtime_collects_real_evidence(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        evidence = BrowserRuntime().inspect(f"http://127.0.0.1:{server.server_port}", tmp_path / "browser")
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert evidence.title == "Crazy Lab"
    assert evidence.screenshot_path.exists()
    assert "<h1>ok</h1>" in evidence.dom_path.read_text(encoding="utf-8")
    assert "ready" in evidence.console_path.read_text(encoding="utf-8")
    assert "127.0.0.1" in evidence.network_path.read_text(encoding="utf-8")


def test_browser_runtime_denies_unapproved_host(tmp_path):
    with pytest.raises(PermissionError):
        BrowserRuntime().inspect("https://example.com", tmp_path / "browser")
