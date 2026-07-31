"""Trace viewer for Codini session artifacts."""

from __future__ import annotations

import argparse
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


SESSION_FILES = (
    "session.json",
    "trace.jsonl",
    "task_state.json",
    "task_state_history.jsonl",
    "report.json",
    "report_history.jsonl",
    "trace_manifest.json",
)


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def child_session_dir(session_dir: Path, child: dict[str, Any]) -> Path | None:
    child_session_id = str(child.get("child_session_id") or "").strip()
    if child_session_id:
        candidate = session_dir.parent / child_session_id
        if candidate.exists():
            return candidate
    child_run_id = str(child.get("child_run_id") or child.get("child_trace_id") or "").strip()
    if not child_run_id:
        return None
    for candidate in session_dir.parent.iterdir() if session_dir.parent.exists() else []:
        if not candidate.is_dir():
            continue
        trace_path = candidate / "trace.jsonl"
        if not trace_path.exists():
            continue
        for event in read_jsonl(trace_path):
            if str(event.get("trace_id") or event.get("run_id") or "").strip() == child_run_id:
                return candidate
    return None


def load_child_trace_events(session_dir: Path, manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[Path]]:
    events: list[dict[str, Any]] = []
    child_dirs: list[Path] = []
    for child in manifest.get("children") or []:
        if not isinstance(child, dict):
            continue
        child_dir = child_session_dir(session_dir, child)
        if child_dir is None:
            continue
        child_dirs.append(child_dir)
        parent_run_id = str(child.get("parent_run_id") or manifest.get("root_run_id") or "").strip()
        child_run_id = str(child.get("child_run_id") or child.get("child_trace_id") or "").strip()
        for event in read_jsonl(child_dir / "trace.jsonl"):
            item = dict(event)
            inherited = dict(item.get("inherited") or {})
            item["parent_run_id"] = item.get("parent_run_id") or inherited.get("parent_run_id") or parent_run_id
            item["child_run_id"] = item.get("child_run_id") or child_run_id
            item["child_session_id"] = item.get("child_session_id") or child.get("child_session_id", "")
            item["_viewer_source"] = "child_trace"
            inherited.setdefault("parent_run_id", item["parent_run_id"])
            if child.get("parent_span_id"):
                inherited.setdefault("parent_span_id", child.get("parent_span_id"))
            item["inherited"] = inherited
            events.append(item)
    return events, child_dirs


def resolve_session_dir(raw: str | Path, workspace_root: str | Path | None = None) -> Path:
    root = Path(workspace_root or ".").resolve()
    if str(raw).strip().lower() == "latest":
        return latest_session_dir(root)
    value = Path(raw)
    if value.exists():
        return value.resolve()
    candidate = root / ".codini" / "sessions" / str(raw)
    if candidate.exists():
        return candidate.resolve()
    raise FileNotFoundError(f"session not found: {raw}")


def latest_session_dir(workspace_root: str | Path | None = None) -> Path:
    root = Path(workspace_root or ".").resolve()
    sessions_root = root / ".codini" / "sessions"
    candidates = [path for path in sessions_root.iterdir() if path.is_dir()] if sessions_root.exists() else []
    if not candidates:
        raise FileNotFoundError(f"no sessions found under {sessions_root}")
    return max(candidates, key=lambda path: path.stat().st_mtime).resolve()


def list_session_options(workspace_root: str | Path | None = None) -> list[dict[str, Any]]:
    root = Path(workspace_root or ".").resolve()
    sessions_root = root / ".codini" / "sessions"
    candidates = [path for path in sessions_root.iterdir() if path.is_dir()] if sessions_root.exists() else []
    options: list[dict[str, Any]] = []
    for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
        session = read_json(path / "session.json", {})
        options.append(
            {
                "id": str(session.get("id") or path.name),
                "label": str(session.get("id") or path.name),
                "path": str(path.resolve()),
                "updated_at": path.stat().st_mtime,
            }
        )
    return options


def build_viewer_data(session_dir: Path, workspace_root: str | Path | None = None) -> dict[str, Any]:
    session = read_json(session_dir / "session.json", {})
    trace = read_jsonl(session_dir / "trace.jsonl")
    primary_trace = list(trace)
    task_state = read_json(session_dir / "task_state.json", {})
    task_history = read_jsonl(session_dir / "task_state_history.jsonl")
    report = read_json(session_dir / "report.json", {})
    report_history = read_jsonl(session_dir / "report_history.jsonl")
    manifest = read_json(session_dir / "trace_manifest.json", {})
    child_trace, child_dirs = load_child_trace_events(session_dir, manifest)
    trace = sorted(
        [*trace, *child_trace],
        key=lambda item: str(item.get("created_at") or item.get("snapshot_at") or ""),
    )
    run_ids = sorted(
        {
            str(item.get("trace_id") or item.get("run_id") or "")
            for item in [*primary_trace, *task_history, *report_history]
            if str(item.get("trace_id") or item.get("run_id") or "").strip()
        }
    )
    revision, revisions = session_revision(session_dir, child_dirs)
    return {
        "session_dir": str(session_dir),
        "session": session,
        "trace": trace,
        "task_state": task_state,
        "task_history": task_history,
        "report": report,
        "report_history": report_history,
        "manifest": manifest,
        "run_ids": run_ids,
        "available_sessions": list_session_options(workspace_root),
        "available_files": [name for name in SESSION_FILES if (session_dir / name).exists()],
        "revision": revision,
        "revisions": revisions,
    }


def _script_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


@lru_cache(maxsize=1)
def _read_viewer_template() -> str:
    """功能：读取独立的 Viewer 页面模板；输入：无；输出：HTML 模板文本。"""
    return (
        files(__package__)
        .joinpath("templates", "viewer.html")
        .read_text(encoding="utf-8")
    )


def session_revision(session_dir: Path, extra_dirs: list[Path] | None = None) -> tuple[str, dict[str, str]]:
    revisions: dict[str, str] = {}
    for base_index, base_dir in enumerate([session_dir, *(extra_dirs or [])]):
        for name in SESSION_FILES:
            path = base_dir / name
            if path.exists():
                try:
                    stat = path.stat()
                except OSError:
                    continue
                key = name if base_index == 0 else f"{base_dir.name}/{name}"
                revisions[key] = f"{stat.st_mtime_ns}:{stat.st_size}"
    revision = "|".join(f"{name}={value}" for name, value in sorted(revisions.items()))
    return revision, revisions


def render_html(data: dict[str, Any], live: bool = False, poll_ms: int = 1500) -> str:
    session_id = str(data.get("session", {}).get("id") or Path(data.get("session_dir", "")).name)
    template = _read_viewer_template()
    return (
        template.replace("__SESSION_ID__", session_id)
        .replace("__DATA__", _script_json(data))
        .replace("__LIVE__", "true" if live else "false")
        .replace("__POLL_MS__", str(int(poll_ms)))
    )


def write_viewer(session_dir: Path, output: str | Path | None = None) -> Path:
    raise RuntimeError("static HTML export has been removed; use --serve to view traces live")


def make_viewer_server(session: str, workspace_root: str | Path = ".", host: str = "127.0.0.1", port: int = 8765, poll_ms: int = 1500) -> tuple[ThreadingHTTPServer, str]:
    def current_session_dir(raw_session: str | None = None) -> Path:
        return resolve_session_dir(raw_session or session, workspace_root)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed_url = urlparse(self.path)
            route = parsed_url.path
            try:
                query = parse_qs(parsed_url.query)
                requested_session = (query.get("session") or [""])[0].strip()
                session_dir = current_session_dir(requested_session or None)
                if route in {"", "/"}:
                    body = render_html(build_viewer_data(session_dir, workspace_root), live=True, poll_ms=poll_ms).encode("utf-8")
                    self._send(200, body, "text/html; charset=utf-8")
                    return
                if route == "/data":
                    client_revision = (query.get("revision") or [""])[0]
                    child_trace, child_dirs = load_child_trace_events(session_dir, read_json(session_dir / "trace_manifest.json", {}))
                    revision, _ = session_revision(session_dir, child_dirs)
                    if client_revision and client_revision == revision:
                        self._send(204, b"", "text/plain; charset=utf-8")
                        return
                    body = json.dumps(build_viewer_data(session_dir, workspace_root), ensure_ascii=False).encode("utf-8")
                    self._send(200, body, "application/json; charset=utf-8")
                    return
                self._send(404, b"not found", "text/plain; charset=utf-8")
            except Exception as exc:
                body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                self._send(500, body, "application/json; charset=utf-8")

    url = f"http://{host}:{int(port)}/"
    server = ThreadingHTTPServer((host, int(port)), Handler)
    return server, url


def serve_viewer(session: str, workspace_root: str | Path = ".", host: str = "127.0.0.1", port: int = 8765, poll_ms: int = 1500) -> int:
    server, url = make_viewer_server(session, workspace_root, host, port, poll_ms)
    print(url)
    print(f"watching session: {session}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("")
        return 0
    finally:
        server.server_close()
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="View Codini trace artifacts.")
    parser.add_argument("session", nargs="?", default="latest", help="Session id, 'latest', or path to a .codini/sessions/<id> directory.")
    parser.add_argument("--cwd", default=".", help="Workspace root used when session is an id.")
    parser.add_argument("--serve", action="store_true", help="Start a live local viewer that polls session artifacts. This is now the default.")
    parser.add_argument("--host", default="127.0.0.1", help="Host for --serve.")
    parser.add_argument("--port", type=int, default=8765, help="Port for --serve.")
    parser.add_argument("--poll-ms", type=int, default=1500, help="Browser polling interval for --serve.")
    parser.add_argument("-o", "--output", default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        if args.output:
            raise RuntimeError("static HTML export has been removed; use --serve to view traces live")
        return serve_viewer(args.session, args.cwd, args.host, args.port, args.poll_ms)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
