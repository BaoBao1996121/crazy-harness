from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import uuid4

from crazy_harness.core.runtime.browser import BrowserRuntime
from crazy_harness.core.tools import ToolRegistry, ToolResult, ToolSpec

SOURCES_LIST_TOOL_NAME = "research.sources.list"
SOURCE_OPEN_TOOL_NAME = "research.source.open"
REPORT_WRITE_TOOL_NAME = "research.report.write"
REPORT_VALIDATE_TOOL_NAME = "research.report.validate"

_IDENTIFIER = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_CITATION = re.compile(
    r"\[source:([a-z0-9]+(?:-[a-z0-9]+)*)#([a-z0-9]+(?:-[a-z0-9]+)*)\]"
)
_MAX_REPORT_CHARS = 50_000
_MAX_SOURCE_BYTES = 1_000_000
_ATOMIC_REPLACE_RETRY_SECONDS = (0.02, 0.05)
_REQUIRED_HEADINGS = ("# Recommendation", "## Findings", "## Risks", "## Sources")


@dataclass(frozen=True)
class ResearchEvidenceItem:
    evidence_id: str
    text: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.evidence_id):
            raise ValueError(f"invalid evidence_id: {self.evidence_id!r}")
        if not self.text.strip():
            raise ValueError("evidence text must not be empty")


@dataclass(frozen=True)
class ResearchSource:
    source_id: str
    title: str
    summary: str
    tags: tuple[str, ...]
    evidence: tuple[ResearchEvidenceItem, ...]

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.source_id):
            raise ValueError(f"invalid source_id: {self.source_id!r}")
        if not self.title.strip() or not self.summary.strip():
            raise ValueError("source title and summary must not be empty")
        ids = [item.evidence_id for item in self.evidence]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError(f"source {self.source_id!r} must have unique evidence IDs")

    def metadata(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "summary": self.summary,
            "tags": list(self.tags),
            "canonical_uri": f"source://{self.source_id}",
        }


def render_source_html(source: ResearchSource) -> str:
    items = "\n".join(
        f'<li data-evidence-id="{html.escape(item.evidence_id)}">{html.escape(item.text)}</li>'
        for item in source.evidence
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>{html.escape(source.title)}</title></head><body>"
        f'<main data-source-id="{html.escape(source.source_id)}">'
        f"<h1>{html.escape(source.title)}</h1><p>{html.escape(source.summary)}</p>"
        f"<ul>{items}</ul></main></body></html>"
    )


def build_research_tools(
    workspace: Path,
    sources: tuple[ResearchSource, ...],
) -> ToolRegistry:
    root = workspace.resolve()
    source_by_id = _source_map(sources)
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name=SOURCES_LIST_TOOL_NAME,
            description="List allowlisted research source metadata without disclosing page bodies.",
            input_schema={"type": "object", "additionalProperties": False},
            use_when="Need to discover which independent sources may answer the assignment.",
            side_effect_level="none",
            is_read_only=True,
            is_concurrency_safe=True,
        ),
        lambda _: _list_sources(source_by_id),
    )
    registry.register(
        ToolSpec(
            name=SOURCE_OPEN_TOOL_NAME,
            description="Open one allowlisted source in real Chromium and return a structured evidence capsule.",
            input_schema={
                "type": "object",
                "properties": {"source_id": {"type": "string", "minLength": 1}},
                "required": ["source_id"],
                "additionalProperties": False,
            },
            use_when="Need source body evidence after reviewing source metadata.",
            do_not_use_when="The source_id is absent from research.sources.list.",
            side_effect_level="browser_read",
            output_offload_policy="offload_if_large",
            is_read_only=True,
            is_concurrency_safe=False,
        ),
        lambda args: _open_source(root, source_by_id, str(args.get("source_id", ""))),
    )
    registry.register(
        ToolSpec(
            name=REPORT_WRITE_TOOL_NAME,
            description="Atomically write the candidate research report to the only allowlisted report path.",
            input_schema={
                "type": "object",
                "properties": {"content": {"type": "string", "minLength": 1}},
                "required": ["content"],
                "additionalProperties": False,
            },
            use_when="Evidence collection is complete and a cited report is ready to validate.",
            side_effect_level="workspace_write",
            approval_required=True,
            is_destructive=True,
            is_concurrency_safe=False,
        ),
        lambda args: _write_report(root, str(args.get("content", ""))),
    )
    registry.register(
        ToolSpec(
            name=REPORT_VALIDATE_TOOL_NAME,
            description="Validate report structure, canonical citations, multi-source coverage, and content hash.",
            input_schema={"type": "object", "additionalProperties": False},
            use_when="Need machine evidence before submitting the final research artifact.",
            side_effect_level="none",
            is_read_only=True,
            is_concurrency_safe=True,
        ),
        lambda _: _validate_report(root, source_by_id),
    )
    return registry


def _source_map(sources: tuple[ResearchSource, ...]) -> dict[str, ResearchSource]:
    result: dict[str, ResearchSource] = {}
    for source in sources:
        if source.source_id in result:
            raise ValueError(f"duplicate source_id: {source.source_id}")
        result[source.source_id] = source
    if not result:
        raise ValueError("at least one research source is required")
    return result


def _list_sources(source_by_id: dict[str, ResearchSource]) -> ToolResult:
    payload = {
        "version": 1,
        "disclosure": "metadata_only",
        "sources": [
            source_by_id[source_id].metadata() for source_id in sorted(source_by_id)
        ],
    }
    return ToolResult(
        name=SOURCES_LIST_TOOL_NAME,
        status="ok",
        output=json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )


def _open_source(
    root: Path,
    source_by_id: dict[str, ResearchSource],
    source_id: str,
) -> ToolResult:
    source = source_by_id.get(source_id)
    if source is None:
        return ToolResult(
            name=SOURCE_OPEN_TOOL_NAME,
            status="error",
            error=f"unknown source_id: {source_id!r}",
        )
    try:
        source_root = (root / "sources").resolve()
        source_path = (source_root / f"{source.source_id}.html").resolve()
        if not source_path.is_relative_to(source_root):
            raise ValueError(f"source fixture escapes workspace: {source_path}")
        if source_path.stat().st_size > _MAX_SOURCE_BYTES:
            raise ValueError(f"source fixture exceeds {_MAX_SOURCE_BYTES} bytes")
        page = source_path.read_bytes()
    except (OSError, ValueError) as exc:
        return ToolResult(
            name=SOURCE_OPEN_TOOL_NAME,
            status="error",
            error=f"browser source inspection failed: {type(exc).__name__}: {exc}",
        )

    class SourceHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/source.html":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, _format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        output_dir = root / "browser" / source.source_id
        evidence = BrowserRuntime().inspect(
            f"http://127.0.0.1:{server.server_port}/source.html",
            output_dir,
        )
        dom = evidence.dom_path.read_text(encoding="utf-8")
        observed = _extract_evidence(dom)
        expected = {item.evidence_id: item.text for item in source.evidence}
        if observed != expected:
            raise ValueError(
                "browser DOM evidence does not match the immutable source catalog"
            )
        artifacts = {
            "screenshot": _relative_artifact(root, evidence.screenshot_path),
            "dom": _relative_artifact(root, evidence.dom_path),
            "console": _relative_artifact(root, evidence.console_path),
            "network": _relative_artifact(root, evidence.network_path),
        }
        payload = {
            "source_id": source.source_id,
            "canonical_uri": f"source://{source.source_id}",
            "title": evidence.title,
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "text": observed[item.evidence_id],
                    "citation": f"source:{source.source_id}#{item.evidence_id}",
                }
                for item in source.evidence
            ],
            "artifacts": artifacts,
        }
        return ToolResult(
            name=SOURCE_OPEN_TOOL_NAME,
            status="ok",
            output=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
    except Exception as exc:
        return ToolResult(
            name=SOURCE_OPEN_TOOL_NAME,
            status="error",
            error=f"browser source inspection failed: {type(exc).__name__}: {exc}",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _extract_evidence(dom: str) -> dict[str, str]:
    from html.parser import HTMLParser

    class EvidenceParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.current: str | None = None
            self.parts: list[str] = []
            self.items: dict[str, str] = {}

        def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            attributes = dict(attrs)
            if tag == "li" and attributes.get("data-evidence-id"):
                self.current = str(attributes["data-evidence-id"])
                self.parts = []

        def handle_data(self, data: str) -> None:
            if self.current is not None:
                self.parts.append(data)

        def handle_endtag(self, tag: str) -> None:
            if tag == "li" and self.current is not None:
                self.items[self.current] = " ".join("".join(self.parts).split())
                self.current = None
                self.parts = []

    parser = EvidenceParser()
    parser.feed(dom)
    return parser.items


def _relative_artifact(root: Path, path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"browser artifact escapes workspace: {path}")
    return resolved.relative_to(root).as_posix()


def _write_report(root: Path, content: str) -> ToolResult:
    if not content.strip():
        return ToolResult(
            name=REPORT_WRITE_TOOL_NAME, status="error", error="report is empty"
        )
    if len(content) > _MAX_REPORT_CHARS:
        return ToolResult(
            name=REPORT_WRITE_TOOL_NAME,
            status="error",
            error=f"report exceeds {_MAX_REPORT_CHARS} characters",
        )
    target = root / "report.md"
    temporary = root / f".report-{uuid4().hex}.tmp"
    root.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temporary, target)
    except (OSError, UnicodeError) as exc:
        return ToolResult(name=REPORT_WRITE_TOOL_NAME, status="error", error=str(exc))
    finally:
        temporary.unlink(missing_ok=True)
    return ToolResult(
        name=REPORT_WRITE_TOOL_NAME,
        status="ok",
        output=f"updated report.md ({len(content)} chars)",
    )


def _replace_with_retry(source: Path, target: Path) -> None:
    for delay in (*_ATOMIC_REPLACE_RETRY_SECONDS, None):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)


def _validate_report(
    root: Path,
    source_by_id: dict[str, ResearchSource],
) -> ToolResult:
    report_path = root / "report.md"
    try:
        report = report_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return ToolResult(
            name=REPORT_VALIDATE_TOOL_NAME, status="error", error=str(exc)
        )
    failures: list[str] = []
    if len(report) > _MAX_REPORT_CHARS:
        failures.append(f"report exceeds {_MAX_REPORT_CHARS} characters")
    for heading in _REQUIRED_HEADINGS:
        if not re.search(rf"^{re.escape(heading)}\s*$", report, flags=re.MULTILINE):
            failures.append(f"missing heading: {heading}")
    citations = tuple(dict.fromkeys(_CITATION.findall(report)))
    known = {
        (source.source_id, item.evidence_id)
        for source in source_by_id.values()
        for item in source.evidence
    }
    unknown = sorted(set(citations) - known)
    if unknown:
        failures.append(
            "unknown citation: "
            + ", ".join(
                f"source:{source_id}#{evidence_id}"
                for source_id, evidence_id in unknown
            )
        )
    if len(citations) < 3:
        failures.append("report requires at least three unique citations")
    source_ids = sorted({source_id for source_id, _ in citations})
    if len(source_ids) < 2:
        failures.append("report requires citations from at least two sources")
    if failures:
        return ToolResult(
            name=REPORT_VALIDATE_TOOL_NAME,
            status="error",
            error="; ".join(failures),
        )
    payload = {
        "report_path": "report.md",
        "report_sha256": hashlib.sha256(report.encode("utf-8")).hexdigest(),
        "citations": [
            f"source:{source_id}#{evidence_id}" for source_id, evidence_id in citations
        ],
        "source_ids": source_ids,
    }
    return ToolResult(
        name=REPORT_VALIDATE_TOOL_NAME,
        status="ok",
        output=json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )
