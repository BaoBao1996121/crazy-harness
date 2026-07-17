from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class BrowserEvidence:
    url: str
    title: str
    screenshot_path: Path
    dom_path: Path
    console_path: Path
    network_path: Path


class BrowserRuntime:
    """Collect browser evidence from explicitly allowed hosts."""

    def __init__(self, *, allowed_hosts: set[str] | None = None) -> None:
        self.allowed_hosts = allowed_hosts or {"127.0.0.1", "localhost"}

    def inspect(self, url: str, output_dir: Path) -> BrowserEvidence:
        host = urlparse(url).hostname or ""
        if host not in self.allowed_hosts:
            raise PermissionError(f"browser host is not allowed: {host}")

        from playwright.sync_api import sync_playwright

        output_dir.mkdir(parents=True, exist_ok=True)
        screenshot = output_dir / "page.png"
        dom = output_dir / "dom.html"
        console_file = output_dir / "console.json"
        network_file = output_dir / "network.json"
        console_events: list[dict[str, str]] = []
        network_events: list[dict[str, str]] = []

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.on("console", lambda message: console_events.append({"type": message.type, "text": message.text}))
            page.on("request", lambda request: network_events.append({"method": request.method, "url": request.url}))
            page.goto(url, wait_until="networkidle", timeout=30_000)
            title = page.title()
            dom.write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(screenshot), full_page=True)
            browser.close()

        console_file.write_text(json.dumps(console_events, ensure_ascii=False, indent=2), encoding="utf-8")
        network_file.write_text(json.dumps(network_events, ensure_ascii=False, indent=2), encoding="utf-8")
        return BrowserEvidence(url, title, screenshot, dom, console_file, network_file)
