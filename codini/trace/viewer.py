"""Trace viewer for Codini session artifacts."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    template = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Agent Trace Panel · __SESSION_ID__</title>
  <style>
    :root {
      --bg: #f4f5f6;
      --surface: #ffffff;
      --border: #dcdfe4;
      --border-soft: #ebedf0;
      --text: #1d1e20;
      --muted: #66686b;
      --orange: #e25816;
      --green: #198038;
      --green-soft: #defbe6;
      --blue: #0f62fe;
      --blue-soft: #edf5ff;
      --tool: #8a3ffc;
      --red: #da1e28;
      --red-soft: #fff1f1;
      --gold: #b25000;
      --gold-soft: #fff3e2;
      --shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
    }
    * { box-sizing: border-box; }
    html, body {
      width: 100%;
      height: 100%;
      margin: 0;
      overflow: hidden;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", Arial, sans-serif;
      font-size: 14px;
      letter-spacing: 0;
    }
    button, input, select { font: inherit; }
    button { color: inherit; }
    .app {
      width: 100vw;
      height: 100vh;
      padding: 18px 20px;
      display: grid;
      grid-template-columns: minmax(290px, 24%) minmax(640px, 1fr) minmax(290px, 21.5%);
      gap: 12px;
    }
    .panel {
      min-width: 0;
      min-height: 0;
      display: flex;
      flex-direction: column;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .panel-header {
      height: 58px;
      flex: 0 0 58px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 20px;
    }
    .panel-header h2 { margin: 0; font-size: 19px; letter-spacing: -0.2px; font-weight: 750; }
    .count, .live-label { color: #74655a; font-size: 14px; white-space: nowrap; }
    .live-dot { display:inline-block; width:8px; height:8px; border-radius:999px; background:var(--green); margin-right:7px; }
    .icon {
      width: 18px;
      height: 18px;
      stroke: currentColor;
      stroke-width: 1.9;
      fill: none;
      stroke-linecap: round;
      stroke-linejoin: round;
      flex: 0 0 auto;
    }
    .icon.fill { fill: currentColor; stroke: none; }
    .session-list, .trace-scroll, .details-content {
      min-height: 0;
      flex: 1;
      overflow-y: auto;
      scrollbar-width: thin;
      scrollbar-color: #aaa39d transparent;
    }
    .session-list { padding: 6px 14px 10px; }
    .trace-scroll { padding-right: 8px; }
    .details-content { padding: 18px 15px; }
    .message-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 13px 14px;
      margin-bottom: 12px;
      box-shadow: 0 1px 4px rgba(81, 57, 35, .025);
      cursor: pointer;
    }
    .message-card.active, .step.active, .run-block.active { border-color: #ef9a68; box-shadow: inset 0 0 0 1px rgba(230,93,24,.08); }
    .message-card.failed, .step.failed, .run-block.failed { background: var(--red-soft); border-color: #edb5ac; }
    .message-top { display: flex; align-items: center; gap: 9px; margin-bottom: 9px; }
    .role-icon { width: 22px; height: 22px; display: grid; place-items: center; border-radius: 7px; }
    .role-icon .icon { width: 20px; height: 20px; }
    .role-user { color: var(--blue); }
    .role-assistant { color: var(--green); }
    .role-tool { color: var(--tool); }
    .role-label { font-weight: 760; font-size: 13px; letter-spacing: .05px; }
    .timestamp { margin-left: auto; color: #7b746e; font-size: 12px; white-space: nowrap; }
    .message-body { font-size: 14.5px; line-height: 1.62; color: #27231f; overflow-wrap: anywhere; }
    .message-body p { margin: 0 0 8px; }
    .message-body p:last-child { margin-bottom: 0; }
    .message-body code {
      padding: 2px 5px;
      border-radius: 6px;
      background: #fff2e5;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
      font-size: 12px;
    }
    .code-box, .tool-code, pre {
      border: 1px solid #eadfce;
      border-radius: 8px;
      background: linear-gradient(180deg, #fffaf3 0%, #fffdf9 100%);
      padding: 8px 10px;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
      font-size: 12px;
      line-height: 1.55;
      white-space: pre-wrap;
      overflow: auto;
      overflow-wrap: anywhere;
    }
    .code-box, .tool-code { display: grid; grid-template-columns: 26px 1fr; gap: 7px; }
    .line-numbers { color: #6f6861; text-align: right; user-select: none; }
    .code-lines { white-space: pre-wrap; word-break: break-word; }
    .execution { padding-bottom: 14px; }
    .execution-content { min-height: 0; flex: 1; display: flex; flex-direction: column; padding: 0 20px; overflow: hidden; }
    .toolbar {
      display: grid;
      grid-template-columns: minmax(250px, 300px) minmax(170px, 230px) max-content;
      gap: 10px;
      align-items: center;
      margin-bottom: 17px;
    }
    .select-wrap, .search-wrap {
      height: 42px;
      border: 1px solid var(--border);
      background: var(--surface);
      border-radius: 8px;
      display: flex;
      align-items: center;
    }
    .select-wrap { padding-left: 14px; position: relative; }
    .session-picker { position: relative; min-width: 0; }
    .session-trigger {
      width: 100%;
      height: 42px;
      border: 1px solid #cfd8d2;
      border-radius: 8px;
      background: #f8faf9;
      display: grid;
      grid-template-columns: auto minmax(0, 1fr) 18px;
      align-items: center;
      gap: 9px;
      padding: 0 12px 0 14px;
      cursor: pointer;
      box-shadow: inset 0 0 0 1px rgba(25,128,56,.03);
    }
    .session-trigger:hover { border-color: #b8c7bd; background: #fbfdfb; }
    .session-trigger:focus { outline: none; border-color: #ef9a68; box-shadow: 0 0 0 3px rgba(239,154,104,.14); }
    .session-kicker { color: #4f6f5d; font-size: 11px; font-weight: 760; letter-spacing: .02em; text-transform: uppercase; }
    .session-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #17261e; font-weight: 720; text-align: left; }
    .session-trigger .chevron { color: var(--green); width: 16px; justify-self: end; }
    .session-menu {
      position: absolute;
      z-index: 20;
      top: calc(100% + 6px);
      left: 0;
      right: 0;
      max-height: 292px;
      overflow-y: auto;
      border: 1px solid #c8cdd3;
      border-radius: 9px;
      background: var(--surface);
      box-shadow: 0 10px 28px rgba(31, 27, 23, .16);
      padding: 5px;
      display: none;
    }
    .session-picker.open .session-menu { display: block; }
    .session-option {
      width: 100%;
      min-height: 34px;
      border: 0;
      border-radius: 6px;
      background: transparent;
      padding: 0 9px;
      text-align: left;
      cursor: pointer;
      color: #26211d;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .session-option:hover { background: #fff4ea; }
    .session-option.active { background: #e9f6ee; color: #145c35; font-weight: 720; }
    .select-wrap:hover { border-color: #c9b9aa; background: #fffdf9; }
    .select-wrap:focus-within { border-color: #ef9a68; box-shadow: 0 0 0 3px rgba(239,154,104,.14); }
    .run-select {
      appearance: none;
      width: 100%;
      height: 100%;
      padding: 0 38px 0 0;
      border: none;
      outline: none;
      color: #302821;
      background: transparent;
      cursor: pointer;
      font-weight: 650;
    }
    .select-wrap .chevron { position: absolute; right: 12px; width: 16px; color: #655e58; pointer-events: none; }
    .search-wrap { padding: 0 12px; gap: 9px; }
    .search-wrap .icon { color: #8c7e73; }
    .search-wrap input { width: 100%; border: none; outline: none; background: transparent; color: #3d3732; }
    .filters { display: flex; align-items: center; flex-wrap: nowrap; gap: 6px; min-width: 0; }
    .filter-btn {
      height: 34px;
      padding: 0 7px;
      border-radius: 7px;
      border: 1px solid var(--border);
      background: #fbf8f3;
      cursor: pointer;
      font-size: 11.5px;
      white-space: nowrap;
    }
    .filter-btn.active { border-color: #ef9a68; color: #d85512; background: #fff7f1; }
    .stats {
      min-height: 80px;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--surface);
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      margin-bottom: 16px;
      overflow: hidden;
    }
    .stat { display: flex; align-items: center; gap: 12px; padding: 12px 17px; position: relative; min-width: 0; }
    .stat:not(:last-child)::after { content: ""; position: absolute; top: 16px; right: 0; height: 48px; width: 1px; background: var(--border-soft); }
    .stat-icon { width: 39px; height: 39px; display: grid; place-items: center; border-radius: 999px; background: var(--gold-soft); color: var(--gold); }
    .stat-value { font-size: 17px; font-weight: 760; line-height: 1.12; overflow: hidden; text-overflow: ellipsis; }
    .stat-label { margin-top: 4px; color: #6f665f; font-size: 12.5px; }
    .run-block { border: 1px solid var(--border); border-radius: 10px; background: var(--surface); margin-bottom: 15px; }
    .run-header {
      min-height: 72px;
      padding: 14px 17px 13px 13px;
      display: grid;
      grid-template-columns: 24px minmax(0, 1fr);
      align-items: flex-start;
      gap: 0 12px;
    }
    .play { width: 24px; height: 24px; border-radius: 999px; background: var(--green); color: white; display: grid; place-items: center; margin-top: 1px; }
    .play .icon { width: 12px; height: 12px; fill: currentColor; stroke: none; margin-left: 1px; }
    .complete-icon { width: 24px; height: 24px; border-radius: 999px; background: var(--green); color: white; display: grid; place-items: center; flex: 0 0 auto; }
    .complete-icon .icon { width: 14px; height: 14px; stroke: currentColor; stroke-width: 2.4; }
    .run-title { font-weight: 760; font-size: 15px; margin-bottom: 4px; }
    .run-subtitle { color: #746b64; font-size: 13px; overflow-wrap: anywhere; }
    .run-right {
      grid-column: 2;
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px 12px;
      padding-top: 8px;
      font-size: 12.5px;
      color: #554e48;
      min-width: 0;
    }
    .status { padding: 3px 9px; border-radius: 999px; color: #28744e; background: var(--green-soft); font-size: 11.5px; font-weight: 700; }
    .status.error { color: var(--red); background: var(--red-soft); }
    .status.running { color: #8a5f18; background: var(--gold-soft); }
    .run-chevron { display: none; }
    .trace-body { position: relative; padding: 0 16px 14px 40px; }
    .trace-body::before { content: ""; position: absolute; left: 21px; top: -14px; bottom: 22px; border-left: 1px dashed #c9c1b9; }
    .step { position: relative; border: 1px solid var(--border); border-radius: 9px; background: var(--surface); padding: 13px 14px 12px; margin: 0 0 14px calc(var(--depth, 0) * 42px); cursor: pointer; }
    .step::before { content: ""; position: absolute; width: 9px; height: 9px; border-radius: 50%; background: #8e8e8e; left: -25px; top: 29px; z-index: 1; box-shadow: 0 0 0 3px #faf7f2; }
    .step.reached::before { background: var(--green); }
    .step.active::before { background: var(--green); box-shadow: 0 0 0 3px #faf7f2, 0 0 0 6px rgba(49, 145, 91, .18); }
    .step.failed::before { background: var(--red); }
    .step.child-step { border-left-color: #b7aaa0; }
    .branch-link { display: none; }
    .step.child-step .branch-link { display: block; position: absolute; left: -20.5px; top: -14px; bottom: -14px; width: 21px; pointer-events: none; z-index: 0; }
    .step.child-step .branch-link::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; border-left: 1px dashed #c9c1b9; }
    .step.child-step .branch-link::after { content: ""; position: absolute; left: 0; top: 47.5px; width: 21px; border-top: 1px dashed #c9c1b9; }
    .step-head {
      display: grid;
      grid-template-columns: 30px minmax(0, 1fr);
      align-items: start;
      gap: 0 11px;
      min-width: 0;
    }
    .step-kind { width: 30px; height: 30px; flex: 0 0 30px; border-radius: 999px; display: grid; place-items: center; background: var(--blue-soft); color: var(--blue); }
    .step-kind.tool-kind { background: #fff4e9; color: #ef7a14; }
    .step-kind.agent-kind { background: var(--green-soft); color: var(--green); }
    .step-main { min-width: 0; flex: 1; }
    .step-title { font-size: 14.5px; font-weight: 750; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .step-sub { margin-top: 3px; color: #69615b; font-size: 12.5px; overflow-wrap: anywhere; }
    .step-metrics {
      grid-column: 2;
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 8px 12px;
      margin-top: 8px;
      font-size: 12px;
      color: #5e5751;
      min-width: 0;
    }
    .tool-content { margin-left: 41px; margin-top: 12px; max-width: calc(100% - 41px); }
    .tool-result { margin-bottom: 10px; color: #524b45; font-size: 13px; overflow-wrap: anywhere; }
    .tags { margin-top: 12px; display: flex; flex-wrap: wrap; gap: 9px; }
    .tag { display: inline-flex; align-items: center; min-height: 31px; border: 1px solid #eee2d5; border-radius: 7px; background: #fffaf3; overflow: hidden; font-size: 12px; }
    .tag-label { padding: 6px 10px; color: #6e655e; background: #fff5e8; }
    .tag-value { padding: 6px 11px; color: #4e4741; border-left: 1px solid #eee2d5; }
    .completed { min-height: 42px; display: grid; grid-template-columns: 40px minmax(0, 1fr) auto; align-items: center; padding: 0 16px 0 0; column-gap: 16px; margin: -2px 0 16px 0; }
    .completed .complete-icon { justify-self: center; }
    .completed strong { min-width: 0; }
    .completed .duration { grid-column: 3; color: #6b625b; margin-top: 0; white-space: nowrap; justify-self: end }
    .pin-btn { border: none; background: transparent; padding: 3px; color: #655c55; cursor: pointer; }
    .pin-btn.pinned { color: var(--orange); transform: rotate(-28deg); }
    .tabs { height: 51px; display: flex; align-items: flex-end; padding: 0 18px; border-bottom: 1px solid var(--border); }
    .tab { min-width: 95px; height: 43px; border: 1px solid transparent; border-bottom: none; border-radius: 8px 8px 0 0; background: #eaecf0; cursor: pointer; color: var(--text); font-size: 13.5px; }
    .tab.active { color: var(--orange); font-weight: 700; background: var(--surface); border-color: var(--border); position: relative; bottom: -1px; }
    .detail-card { border: 1px solid var(--border); border-radius: 9px; background: var(--surface); overflow: hidden; margin-bottom: 17px; }
    .detail-card-title { height: 47px; display: flex; align-items: center; padding: 0 15px; border-bottom: 1px solid var(--border); background: #f8f9fa; font-weight: 760; font-size: 13px; color: var(--text); }
    .kv-row { min-height: 41px; display: grid; grid-template-columns: 93px 1fr; align-items: center; gap: 10px; padding: 8px 14px; border-bottom: 1px solid var(--border-soft); font-size: 11.6px; }
    .kv-row:last-child { border-bottom: 0; }
    .kv-key { font-weight: 700; color: #2b2521; }
    .kv-value { min-width: 0; color: #514a44; word-break: break-all; display: flex; align-items: center; gap: 6px; }
    .dot { width: 8px; height: 8px; border-radius: 999px; background: var(--green); flex: 0 0 auto; }
    .dot.error { background: var(--red); }
    .copy-mini { margin-left: auto; border: none; padding: 0; color: #8d8178; background: transparent; cursor: pointer; }
    .message-detail { padding: 14px 15px 17px; min-height: 185px; font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace; font-size: 11.8px; line-height: 1.68; color: var(--text); white-space: pre-wrap; word-break: break-word; }
    .copy-btn-wrap { padding: 0 15px 15px; }
    .copy-message { height: 39px; width: 100%; display: flex; align-items: center; justify-content: center; gap: 8px; border: 1px solid var(--border); border-radius: 8px; background: #f8f9fa; color: var(--text); cursor: pointer; font-weight: 600; }
    .json-pane { margin: 0; padding: 16px; border: 1px solid var(--border); border-radius: 9px; background: #f8f9fa; font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace; font-size: 11.5px; line-height: 1.65; color: var(--text); white-space: pre-wrap; word-break: break-word; }
    .empty { padding: 24px 10px; color: var(--muted); text-align: center; }
    .toast { position: fixed; left: 50%; bottom: 24px; transform: translate(-50%, 12px); padding: 9px 14px; background: #302a25; color: white; border-radius: 7px; opacity: 0; pointer-events: none; transition: .2s ease; box-shadow: 0 8px 28px rgba(0,0,0,.18); font-size: 12.5px; z-index: 999; }
    .toast.show { opacity: 1; transform: translate(-50%, 0); }
    @media (max-width: 1250px) { html, body { overflow: auto; } .app { min-width: 1180px; min-height: 720px; } }
    @media (max-height: 760px) {
      html, body { font-size: 13px; }
      .app { padding-top: 12px; padding-bottom: 12px; }
      .panel-header { height: 50px; flex-basis: 50px; }
      .message-card { padding-top: 10px; padding-bottom: 10px; margin-bottom: 9px; }
      .stats { min-height: 67px; }
      .run-header { min-height: 62px; }
    }
  </style>
</head>
<body>
  <svg aria-hidden="true" style="position:absolute;width:0;height:0;overflow:hidden">
    <symbol id="i-user" viewBox="0 0 24 24"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></symbol>
    <symbol id="i-bot" viewBox="0 0 24 24"><rect width="18" height="10" x="3" y="11" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" x2="8" y1="16" y2="16"/><line x1="16" x2="16" y1="16" y2="16"/></symbol>
    <symbol id="i-tool" viewBox="0 0 24 24"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 9.36l-7.19 7.19a2.12 2.12 0 0 1-3-3l7.19-7.19a6 6 0 0 1 9.36-7.94z"/></symbol>
    <symbol id="i-plus" viewBox="0 0 24 24"><path d="M5 12h14"/><path d="M12 5v14"/></symbol>
    <symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></symbol>
    <symbol id="i-chevron-down" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></symbol>
    <symbol id="i-copy" viewBox="0 0 24 24"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></symbol>
    <symbol id="i-pin" viewBox="0 0 24 24"><line x1="12" x2="12" y1="17" y2="22"/><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.68V6a3 3 0 0 0-3-3 3 3 0 0 0-3 3v4.68a2 2 0 0 1-1.11 1.87l-1.78.89A2 2 0 0 0 5 15.24Z"/></symbol>
    <symbol id="i-play" viewBox="0 0 24 24"><polygon points="6 3 20 12 6 21 6 3"/></symbol>
    <symbol id="i-cloud" viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z M8 7h8 M8 11h8 M8 15h6"/></symbol>
    <symbol id="i-check" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></symbol>
    <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></symbol>
    <symbol id="i-tokens" viewBox="0 0 24 24"><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><line x1="9" x2="9" y1="3" y2="21"/></symbol>
    <symbol id="i-model" viewBox="0 0 24 24"><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M12 8v8"/><path d="m8 12 4 4 4-4"/></symbol>
    <symbol id="i-model-calls" viewBox="0 0 24 24"><path d="M5.5 5.5h9A3.5 3.5 0 0 1 18 9v3a3.5 3.5 0 0 1-3.5 3.5H10L6 19v-3.7A3.5 3.5 0 0 1 3 12V9a3.5 3.5 0 0 1 2.5-3.5Z"/><path d="m15.7 3 .45 1.15L17.3 4.6l-1.15.45-.45 1.15-.45-1.15-1.15-.45 1.15-.45L15.7 3Z"/><circle cx="8" cy="10.5" r=".8" fill="currentColor" stroke="none"/><circle cx="11" cy="10.5" r=".8" fill="currentColor" stroke="none"/><circle cx="14" cy="10.5" r=".8" fill="currentColor" stroke="none"/></symbol>
    <symbol id="i-stat-tokens" viewBox="0 0 24 24"><rect x="3" y="6" width="5" height="5" rx="1.4"/><rect x="9.5" y="6" width="5" height="5" rx="1.4"/><rect x="16" y="6" width="5" height="5" rx="1.4"/><rect x="5.5" y="13" width="5" height="5" rx="1.4"/><rect x="12" y="13" width="6.5" height="5" rx="1.4"/><path d="M4.8 8.5h1.4M11.3 8.5h1.4M17.8 8.5h1.4M7.3 15.5h1.4M14 15.5h2.5"/></symbol>
    <symbol id="i-tools" viewBox="0 0 24 24"><path d="M8 7V5.8A1.8 1.8 0 0 1 9.8 4h4.4A1.8 1.8 0 0 1 16 5.8V7"/><rect x="3.5" y="7" width="17" height="12" rx="2.5"/><path d="M3.5 11.5h17"/><path d="M10 11.5v2h4v-2"/></symbol>
    <symbol id="i-duration" viewBox="0 0 24 24"><circle cx="12" cy="13" r="7.5"/><path d="M9.5 3h5"/><path d="M12 3v2.5"/><path d="m17.5 6.5 1.5-1.5"/><path d="M12 9v4l2.8 1.7"/></symbol>
  </svg>

  <main class="app">
    <section class="panel session-panel">
      <header class="panel-header">
        <h2>Session</h2>
        <span class="count" id="messageCount">0 messages</span>
      </header>
      <div class="session-list" id="sessionList"></div>
    </section>

    <section class="panel execution">
      <header class="panel-header">
        <h2>Execution</h2>
        <span class="live-label" id="liveLabel"></span>
      </header>
      <div class="execution-content">
        <div class="toolbar">
          <div class="session-picker" id="sessionPicker">
            <button class="session-trigger" id="sessionTrigger" type="button" aria-label="Session selector" aria-expanded="false">
              <span class="session-kicker">Session</span>
              <span class="session-label" id="sessionLabel"></span>
              <svg class="icon chevron"><use href="#i-chevron-down"/></svg>
            </button>
            <div class="session-menu" id="sessionMenu"></div>
          </div>
          <label class="search-wrap">
            <svg class="icon"><use href="#i-search"/></svg>
            <input id="traceSearch" type="text" placeholder="过滤事件..." />
          </label>
          <div class="filters" id="filters">
            <button class="filter-btn active" data-filter="all">All</button>
            <button class="filter-btn" data-filter="llm">LLM</button>
            <button class="filter-btn" data-filter="tool">Tools</button>
            <button class="filter-btn" data-filter="subagent">Agents</button>
            <button class="filter-btn" data-filter="system">System</button>
            <button class="filter-btn" data-filter="error">Errors</button>
          </div>
        </div>
        <div class="stats" id="stats"></div>
        <div class="trace-scroll" id="traceScroll"></div>
      </div>
    </section>

    <section class="panel details-panel">
      <header class="panel-header">
        <h2>Details</h2>
        <button class="pin-btn" id="pinBtn" title="固定面板"><svg class="icon"><use href="#i-pin"/></svg></button>
      </header>
      <div class="tabs">
        <button class="tab active" data-tab="summary">Summary</button>
        <button class="tab" data-tab="json">JSON</button>
      </div>
      <div class="details-content" id="detailsContent"></div>
    </section>
  </main>
  <div class="toast" id="toast">已复制</div>

  <script>
    const initialData = __DATA__;
    const live = __LIVE__;
    const pollMs = __POLL_MS__;
    let data = initialData;
    let selectedRun = '';
    let selectedSession = String(data.session?.id || data.session_dir?.split(/[\\/]/).pop() || '');
    let filter = 'all';
    let query = '';
    let activeTab = 'summary';
    let selected = {kind: 'session', item: null};
    let toastTimer;
    let lastRevision = data.revision || '';
    let lastRendered = {session: '', runs: '', trace: '', details: ''};

    const $ = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    const clip = (value, max = 220) => {
      const text = String(value ?? '');
      return text.length > max ? text.slice(0, max - 1) + '…' : text;
    };
    const runIdOf = (item) => String(item?.trace_id || item?.run_id || item?.inherited?.run_id || '');
    const parentRunIdOf = (item) => String(item?.parent_run_id || item?.inherited?.parent_run_id || '');
    const statusOf = (item) => String(item?.span_status || item?.status || '').toLowerCase();
    const isFailed = (item) => ['error', 'failed'].includes(statusOf(item)) || Boolean(item?.error_type || item?.error?.error_type || item?.full_error);
    const formatTime = (value) => {
      if (!value) return '';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return String(value);
      const pad = (n) => String(n).padStart(2, '0');
      return `${pad(date.getMonth()+1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
    };
    const formatMs = (value) => {
      const n = Number(value || 0);
      if (!n) return '';
      return n >= 1000 ? `${(n / 1000).toFixed(1)}s` : `${n}ms`;
    };
    const historyItems = () => Array.isArray(data.session?.history) ? data.session.history : [];
    const traceItems = () => Array.isArray(data.trace) ? data.trace : [];
    function isChildTraceEvent(item) {
      return item?._viewer_source === 'child_trace' || (parentRunIdOf(item) && runIdOf(item) !== parentRunIdOf(item));
    }
    function compactSignature(items, fields) {
      const last = items.length ? items[items.length - 1] : {};
      return `${items.length}:${fields.map(field => String(last?.[field] ?? '')).join('|')}`;
    }
    function selectedKey() {
      const item = selected.item || {};
      return `${selected.kind}:${item.span_id || item.event_index || item.snapshot_index || item.run_id || item.trace_id || item.created_at || ''}:${activeTab}`;
    }

    function showToast(text) {
      const toast = $('toast');
      toast.textContent = text;
      toast.classList.add('show');
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => toast.classList.remove('show'), 1300);
    }
    async function copyText(text) {
      try { await navigator.clipboard.writeText(String(text ?? '')); }
      catch (_) {
        const temp = document.createElement('textarea');
        temp.value = String(text ?? '');
        document.body.appendChild(temp);
        temp.select();
        document.execCommand('copy');
        temp.remove();
      }
      showToast('已复制');
    }
    function markdown(text) {
      let safe = esc(text || '');
      safe = safe.replace(/```([\s\S]*?)```/g, (_, code) => `<pre>${code.trim()}</pre>`);
      safe = safe.replace(/`([^`]+)`/g, '<code>$1</code>');
      safe = safe.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      safe = safe.replace(/\*([^*]+)\*/g, '<em>$1</em>');
      safe = safe.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
      return safe.split(/\n{2,}/).map(part => `<p>${part.replace(/\n/g, '<br>')}</p>`).join('');
    }
    function roleIcon(role) {
      if (role === 'tool') return 'i-tool';
      if (role === 'assistant') return 'i-bot';
      return 'i-user';
    }
    function roleClass(role) {
      if (role === 'tool') return 'role-tool';
      if (role === 'assistant') return 'role-assistant';
      return 'role-user';
    }
    function lineBox(text) {
      const lines = String(text || '').split('\n').slice(0, 10);
      const numbers = lines.map((_, index) => index + 1).join('<br>');
      return `<div class="code-box"><div class="line-numbers">${numbers}</div><div class="code-lines">${esc(lines.join('\n'))}</div></div>`;
    }
    function latestByRun(items, runId) {
      const matches = (items || []).filter(item => runIdOf(item) === runId);
      return matches[matches.length - 1] || null;
    }
    function runIds() {
      const ids = new Set(data.run_ids || []);
      traceItems().forEach(item => { if (!isChildTraceEvent(item) && runIdOf(item)) ids.add(runIdOf(item)); });
      if (data.task_state?.run_id) ids.add(data.task_state.run_id);
      return Array.from(ids).sort();
    }
    function runStartedAt(runId) {
      const start = traceItems().find(item => runIdOf(item) === runId && item.event === 'run_started');
      return String(start?.created_at || latestByRun(data.task_history, runId)?.created_at || latestByRun(data.report_history, runId)?.created_at || runId);
    }
    function eventType(item) {
      const event = String(item.event || '');
      const spanName = String(item.span_name || '');
      if (event.startsWith('approval_')) return 'approval';
      if (isChildTraceEvent(item)) {
        if (event.startsWith('tool_') || spanName.startsWith('tool.')) return 'subagent_tool';
        if (event.startsWith('model_') || spanName === 'llm.complete') return 'subagent_llm';
        if (event.startsWith('run_') || event === 'prompt_built') return 'subagent_run';
        return 'subagent_meta';
      }
      if (item.name === 'delegate' || item.child_run_id || item.child_trace_id || item.child_session_id) return 'subagent';
      if (event === 'checkpoint_created' || event.startsWith('checkpoint_')) return 'checkpoint';
      if (event.startsWith('run_')) return 'run';
      if (event.startsWith('model_') || item.span_name === 'llm.complete') return 'llm';
      if (event.startsWith('tool_') || spanName.startsWith('tool.')) return 'tool';
      return 'system';
    }
    function eventTitle(item) {
      const type = eventType(item);
      if (type === 'subagent') return item.event === 'tool_executed' ? 'subagent.response' : 'subagent.call';
      if (type === 'subagent_tool') return item.event === 'tool_executed' ? 'subagent.tool.response' : 'subagent.tool.call';
      if (type === 'subagent_llm') return item.event === 'model_parsed' ? 'subagent.llm.response' : 'subagent.llm.request';
      if (type === 'subagent_run') return (item.event || 'subagent').replaceAll('_', '.');
      if (type === 'subagent_meta') return (item.event || 'subagent.meta').replaceAll('_', '.');
      if (type === 'approval') return (item.event || 'approval').replaceAll('_', '.');
      if (type === 'checkpoint') return (item.event || 'checkpoint').replaceAll('_', '.');
      if (type === 'tool') return item.event === 'tool_executed' ? 'tool.response' : 'tool.call';
      if (type === 'llm') return item.event === 'model_parsed' ? 'llm.response' : 'llm.request';
      if (type === 'run') return item.event === 'run_finished' ? '执行已完成' : 'run';
      return (item.event || item.span_name || 'system').replaceAll('_', '.');
    }
    function eventSummary(item) {
      if (item.event === 'run_started') return item.user_request || '';
      if (item.event === 'run_finished') return item.final_answer || item.stop_reason || '';
      if (item.event === 'model_requested') return `prompt chars: ${item.prompt_chars || 0}`;
      if (item.event === 'model_parsed') return item.kind ? `model responded: ${item.kind}` : clip(item.raw || '', 140);
      if (item.event === 'tool_requested') return `${item.name || 'tool'} requested`;
      if (item.event === 'tool_executed') return item.result || item.full_error || `${item.name || 'tool'} completed`;
      if (['subagent', 'subagent_tool', 'subagent_llm', 'subagent_meta'].includes(eventType(item))) return item.child_run_id || item.child_trace_id || item.child_session_id || item.result || item.name || '';
      if (eventType(item) === 'checkpoint') return item.current_goal || item.next_step || item.checkpoint_id || '';
      return item.message || item.name || item.stop_reason || '';
    }
    function findPromptBuiltForRun(runId) {
      if (!runId) return null;
      return (traceItems() || []).find(item => runIdOf(item) === runId && item.event === 'prompt_built');
    }
    function promptWithoutCurrentRequest(item) {
      const promptBuilt = item.event === 'prompt_built' ? item : findPromptBuiltForRun(runIdOf(item));
      return promptBuilt?.prompt_without_current_request || promptBuilt?.prompt_metadata?.prompt_without_current_request || '';
    }
    function detailTextFor(item) {
      const type = eventType(item);
      if (item.event === 'model_requested') return item.prompt || 'No detail';
      if (item.event === 'prompt_built') return item.prompt || promptWithoutCurrentRequest(item) || 'No detail';
      if (type === 'run' && item.event === 'run_started') return promptWithoutCurrentRequest(item) || 'No detail';
      if (item.event === 'model_parsed') return item.raw || 'No detail';
      if (item.event === 'tool_requested') return toolRequestDetail(item);
      if (['subagent', 'subagent_tool', 'subagent_llm', 'subagent_meta'].includes(type)) return subagentDetail(item);
      const error = item.error || {};
      return error.message || item.final_answer || item.content || item.result_full || item.result || item.raw || eventSummary(item) || 'No detail';
    }
    function toolRequestDetail(item) {
      const args = item.args || {};
      if (item.name === 'delegate') {
        return [
          `delegate task: ${args.task || ''}`,
          `max_steps: ${args.max_steps ?? '-'}`,
        ].join('\n');
      }
      return JSON.stringify(args, null, 2);
    }
    function subagentDetail(item) {
      const lines = [];
      if (item.name === 'delegate') lines.push(toolRequestDetail(item));
      if (eventType(item) === 'subagent_tool' && item.event === 'tool_requested') lines.push(toolRequestDetail(item));
      if (item.child_run_id) lines.push(`child_run_id: ${item.child_run_id}`);
      if (item.child_trace_id) lines.push(`child_trace_id: ${item.child_trace_id}`);
      if (item.child_session_id) lines.push(`child_session_id: ${item.child_session_id}`);
      const result = item.result_full || item.result || item.full_error || '';
      if (result) lines.push('', result);
      return lines.filter(line => line !== undefined && line !== null).join('\n') || 'No detail';
    }
    function eventIcon(type) {
      if (type === 'subagent' || type === 'subagent_llm' || type === 'subagent_meta') return 'i-bot';
      if (type === 'tool' || type === 'subagent_tool') return 'i-tool';
      if (type === 'llm') return 'i-model';
      if (type === 'checkpoint' || type === 'run' || type === 'approval') return 'i-check';
      return 'i-clock';
    }
    function isToolLikeType(type) {
      return type === 'tool' || type === 'subagent_tool';
    }
    function searchText(item) {
      if (!item) return '';
      if (item._searchText) return item._searchText;
      const parts = [
        item.event,
        item.span_name,
        item.name,
        item.kind,
        item.status,
        item.span_status,
        item.user_request,
        item.stop_reason,
        item.error_type,
        item.error?.error_type,
        item.error?.message,
        item.result,
        item.full_error,
        item.final_answer,
        item.child_run_id,
        item.child_trace_id,
        item.child_session_id,
        item.checkpoint_id,
        item.current_goal,
        item.current_blocker,
        item.next_step,
        Array.isArray(item.affected_paths) ? item.affected_paths.join(' ') : '',
      ];
      item._searchText = parts.filter(Boolean).join(' ').toLowerCase();
      return item._searchText;
    }
    function tokenText(item) {
      const prompt = item.prompt_tokens || item.summary?.tokens?.prompt;
      const completion = item.completion_tokens || item.summary?.tokens?.completion;
      if (prompt || completion) return `${prompt || 0} / ${completion || 0}`;
      if (item.total_tokens) return item.total_tokens;
      return '';
    }
    function isTerminalRun(item) {
      const status = String(item?.status || '').toLowerCase();
      return ['completed', 'failed', 'stopped'].includes(status) || Boolean(item?.stop_reason || item?.final_answer);
    }
    function runStatusLabel(item, terminal) {
      if (!terminal) return 'RUNNING';
      return isFailed(item) ? 'ERROR' : 'OK';
    }
    function sessionIdOfCurrentData() {
      return String(data.session?.id || data.session_dir?.split(/[\\/]/).pop() || '');
    }

    function renderMessages() {
      const allHistory = historyItems();
      const history = allHistory.filter(item => ['user', 'assistant'].includes(String(item.role || '').toLowerCase()));
      const signature = `${data.revisions?.['session.json'] || ''}:${compactSignature(history, ['role', 'content', 'created_at'])}`;
      if (lastRendered.session === signature) return;
      lastRendered.session = signature;
      $('messageCount').textContent = `${history.length} messages`;
      $('sessionList').innerHTML = history.length ? history.map((item, index) => {
        const role = String(item.role || 'message').toLowerCase();
        const body = role === 'assistant' ? markdown(item.content || '') : esc(item.content || '');
        const code = role === 'tool' ? lineBox(item.content || '') : '';
        return `<article class="message-card ${isFailed(item) ? 'failed' : ''}" data-kind="message" data-index="${index}">
          <div class="message-top ${roleClass(role)}">
            <span class="role-icon"><svg class="icon ${role === 'user' ? 'fill' : ''}"><use href="#${roleIcon(role)}"/></svg></span>
            <span class="role-label">${esc(role.toUpperCase())}${item.name ? '.' + esc(item.name) : ''}</span>
            <time class="timestamp">${esc(formatTime(item.created_at))}</time>
          </div>
          <div class="message-body">${body}</div>
          ${code}
        </article>`;
      }).join('') : '<div class="empty">No messages</div>';
      document.querySelectorAll('[data-kind="message"]').forEach(node => {
        node.addEventListener('click', () => selectItem('message', history[Number(node.dataset.index)], node));
      });
    }
    function renderSessionSelect() {
      const sessions = Array.isArray(data.available_sessions) ? data.available_sessions : [];
      const current = sessionIdOfCurrentData();
      selectedSession = current;
      $('sessionLabel').textContent = current || 'current session';
      $('sessionMenu').innerHTML = sessions.length
        ? sessions.map(item => `<button class="session-option ${item.id === current ? 'active' : ''}" type="button" data-session-id="${esc(item.id)}" title="${esc(item.label || item.id)}">${esc(item.label || item.id)}</button>`).join('')
        : `<button class="session-option active" type="button" data-session-id="${esc(current)}">${esc(current || 'current session')}</button>`;
      document.querySelectorAll('.session-option').forEach(option => {
        option.addEventListener('click', () => switchSession(option.dataset.sessionId || ''));
      });
    }
    function metricValues(events) {
      const modelCalls = events.filter(item => item.event === 'model_parsed').length;
      const tokens = events.reduce((sum, item) => sum + Number(item.total_tokens || 0), 0);
      const tools = events.filter(item => item.event === 'tool_executed').length;
      const elapsed = Math.max(0, ...events.map(item => Number(item.run_duration_ms || item.duration_ms || item.elapsed_ms || 0)));
      return [
        ['i-model-calls', modelCalls, 'model calls'],
        ['i-stat-tokens', tokens.toLocaleString(), 'tokens'],
        ['i-tools', tools, 'tools'],
        ['i-duration', formatMs(elapsed) || '0ms', '总耗时'],
      ];
    }
    function renderStats(events) {
      $('stats').innerHTML = metricValues(events).map(([icon, value, label]) => `
        <div class="stat"><span class="stat-icon"><svg class="icon"><use href="#${icon}"/></svg></span><div><div class="stat-value">${esc(value)}</div><div class="stat-label">${esc(label)}</div></div></div>
      `).join('');
    }
    function filteredEvents() {
      return traceItems().filter(item => {
        const type = eventType(item);
        if (filter === 'error' && !isFailed(item)) return false;
        if (filter === 'system') {
          if (!['system', 'checkpoint'].includes(type)) return false;
        } else if (filter === 'subagent') {
          if (!type.startsWith('subagent') || type === 'subagent_meta') return false;
        } else if (filter === 'tool') {
          if (!['tool', 'subagent_tool'].includes(type)) return false;
        } else if (filter === 'llm') {
          if (!['llm', 'subagent_llm'].includes(type)) return false;
        } else if (!['all', 'error'].includes(filter) && type !== filter) return false;
        if (query && !searchText(item).includes(query)) return false;
        return true;
      });
    }
    function timelineEvents(items) {
      const hiddenSubagentLlmSpans = new Set();
      (items || []).forEach(item => {
        if (eventType(item) === 'subagent_llm' && item.event === 'model_parsed' && item.kind === 'final' && item.span_id) {
          hiddenSubagentLlmSpans.add(item.span_id);
        }
      });
      return (items || []).filter(item => {
        const type = eventType(item);
        if (['run', 'subagent_run', 'subagent_meta', 'approval'].includes(type) || item.event === 'prompt_built') return false;
        if (type === 'subagent_llm' && item.span_id && hiddenSubagentLlmSpans.has(item.span_id)) return false;
        return true;
      });
    }
    function eventDepth(item) {
      const inherited = item.inherited || {};
      const depth = Number(inherited.depth ?? item.depth ?? 0);
      if (Number.isFinite(depth) && depth > 0) return Math.min(depth, 4);
      const rope = String(inherited.agent_rope || item.agent_rope || '');
      if (rope.includes('/')) return Math.min(rope.split('/').length - 1, 4);
      if (parentRunIdOf(item)) return 1;
      return 0;
    }
    function renderTrace() {
      const events = filteredEvents();
      const ids = runIds().sort((a, b) => runStartedAt(a).localeCompare(runStartedAt(b)));
      const traceSignature = [
        data.revisions?.['trace.jsonl'] || '',
        data.revisions?.['task_state.json'] || '',
        data.revisions?.['report.json'] || '',
        ids.join('|'),
        filter,
        query,
      ].join(':');
      if (lastRendered.trace === traceSignature) return;
      lastRendered.trace = traceSignature;
      renderStats(events);
      if (!events.length || !ids.length) {
        $('traceScroll').innerHTML = '<div class="empty">No trace events</div>';
        return;
      }
      const visibleEvents = timelineEvents(events);
      const activeEventIndex = visibleEvents.reduce((latest, item, index) => isFailed(item) ? latest : index, -1);
      const renderStep = (item) => {
        const index = visibleEvents.indexOf(item);
        const type = eventType(item);
        const failed = isFailed(item);
        const depth = eventDepth(item);
        const reached = index >= 0 && index <= activeEventIndex;
        const active = index === activeEventIndex;
        const result = item.result_full || item.result || item.full_error || '';
        const tags = [
          ['工具类型', item.name],
          ['文件', Array.isArray(item.affected_paths) ? item.affected_paths[0] : ''],
          ['范围', item.args?.start || item.args?.end ? `lines ${item.args?.start || '?'}-${item.args?.end || '?'}` : ''],
        ].filter(([, value]) => value);
        return `<article class="step ${isToolLikeType(type) ? 'tool-step' : ''} ${depth > 0 ? 'child-step' : ''} ${reached ? 'reached' : ''} ${active ? 'active' : ''} ${failed ? 'failed' : ''}" style="--depth:${depth}" data-kind="event" data-index="${index}">
          <span class="branch-link" aria-hidden="true"></span>
          <div class="step-head">
            <span class="step-kind ${isToolLikeType(type) ? 'tool-kind' : ''} ${type === 'subagent' || type === 'subagent_llm' ? 'agent-kind' : ''}"><svg class="icon"><use href="#${eventIcon(type)}"/></svg></span>
            <div class="step-main">
              <div class="step-title">${esc(eventTitle(item))}</div>
              <div class="step-sub">${esc(clip(eventSummary(item), 180))}</div>
            </div>
            <div class="step-metrics">
              ${formatMs(item.duration_ms || item.run_duration_ms || item.elapsed_ms) ? `<span>${esc(formatMs(item.duration_ms || item.run_duration_ms || item.elapsed_ms))}</span>` : ''}
              ${item.total_tokens ? `<span>${esc(item.total_tokens.toLocaleString())} tok</span>` : ''}
              ${item.cached_tokens ? `<span>${esc(item.cached_tokens.toLocaleString())} cached</span>` : ''}
              <span class="status ${failed ? 'error' : ''}">${failed ? 'ERROR' : 'OK'}</span>
            </div>
          </div>
          ${result ? `<div class="tool-content"><div class="tool-result">${esc(clip(result, 260))}</div>${lineBox(result)}</div>` : ''}
          ${tags.length ? `<div class="tags">${tags.map(([key, value]) => `<span class="tag"><span class="tag-label">${esc(key)}</span><span class="tag-value">${esc(value)}</span></span>`).join('')}</div>` : ''}
        </article>`;
      };
      const blocks = ids.map(runId => {
        const allRunEvents = traceItems().filter(item => runIdOf(item) === runId || parentRunIdOf(item) === runId);
        const runEvents = events.filter(item => runIdOf(item) === runId || parentRunIdOf(item) === runId);
        const runFinishedEvent = allRunEvents.find(item => item.event === 'run_finished' && runIdOf(item) === runId) || null;
        const latestReport = latestByRun(data.report_history, runId);
        const latestTask = latestByRun(data.task_history, runId) || (data.task_state?.run_id === runId ? data.task_state : {});
        const terminalRun = runFinishedEvent || (isTerminalRun(latestReport) ? latestReport : null) || (isTerminalRun(latestTask) ? latestTask : null);
        const headerRun = terminalRun || latestTask || {};
        const runStart = allRunEvents.find(item => eventType(item) === 'run' && item.event === 'run_started' && runIdOf(item) === runId) || latestTask || {};
        const runVisible = visibleEvents.filter(item => runIdOf(item) === runId || parentRunIdOf(item) === runId);
        const stepHtml = runVisible.map(renderStep).join('');
        const failed = isFailed(headerRun);
        const terminalFailed = isFailed(terminalRun);
        const completionHtml = terminalRun ? `
          <div class="completed" data-kind="run-finish">
            <span class="complete-icon"><svg class="icon"><use href="#i-check"/></svg></span>
            <strong>${terminalFailed ? '执行失败' : '执行已完成'}</strong>
            <div class="duration">总耗时 <b>${esc(formatMs(terminalRun.run_duration_ms || terminalRun.duration_ms || terminalRun.elapsed_ms) || '-')}</b></div>
          </div>` : '';
        return `<section class="run-block ${failed ? 'failed' : ''}" data-kind="run" data-run-id="${esc(runId)}">
          <div class="run-header">
            <span class="play"><svg class="icon"><use href="#i-play"/></svg></span>
            <div>
              <div class="run-title">run</div>
              <div class="run-subtitle">${esc(runStart.user_request || headerRun.user_request || runId || 'No request')}</div>
            </div>
            <div class="run-right">
              <span>${esc(formatMs(headerRun.run_duration_ms || headerRun.duration_ms || headerRun.elapsed_ms))}</span>
              <span class="status ${terminalRun ? (failed ? 'error' : '') : 'running'}">${esc(runStatusLabel(headerRun, Boolean(terminalRun)))}</span>
            </div>
          </div>
          <div class="trace-body">${stepHtml || '<div class="empty">No events match filters</div>'}</div>
          ${completionHtml}
        </section>`;
      }).join('');
      $('traceScroll').innerHTML = blocks || '<div class="empty">No trace events</div>';
      const eventNodes = Array.from(document.querySelectorAll('[data-kind="event"]'));
      eventNodes.forEach(node => {
        node.addEventListener('click', () => selectItem('event', visibleEvents[Number(node.dataset.index)], node));
      });
    }
    function renderDetails() {
      const item = selected.item || latestByRun(data.report_history, selectedRun) || latestByRun(data.task_history, selectedRun) || data.task_state || data.session || {};
      const detailSignature = [
        data.revision || '',
        selectedRun,
        selectedKey(),
        item.span_id || item.event_index || item.snapshot_index || item.run_id || item.trace_id || item.created_at || '',
        activeTab,
      ].join(':');
      if (lastRendered.details === detailSignature) return;
      lastRendered.details = detailSignature;
      if (activeTab === 'json') {
        $('detailsContent').innerHTML = `<pre class="json-pane">${esc(JSON.stringify(item, null, 2))}</pre>`;
        return;
      }
      const error = item.error || {};
      const rows = [
        ['run_id', item.run_id || item.trace_id],
        ['task_id', item.task_id],
        ['status', item.status || item.span_status],
        ['stop_reason', item.stop_reason],
        ['child_run_id', item.child_run_id],
        ['child_trace_id', item.child_trace_id],
        ['child_session_id', item.child_session_id],
        ['created_at', item.created_at || item.snapshot_at],
        ['elapsed_ms', item.elapsed_ms],
        ['duration_ms', item.duration_ms || item.run_duration_ms],
        ['tokens', tokenText(item)],
        ['event', eventTitle(item)],
        ['tool', item.name],
        ['error_type', error.error_type || item.error_type],
        ['http_status', error.http_status || item.http_status],
        ['retryable', error.retryable ?? item.retryable],
      ].filter(([, value]) => value !== undefined && value !== null && value !== '');
      const detailText = detailTextFor(item);
      $('detailsContent').innerHTML = `
        <section class="detail-card">
          <div class="detail-card-title">基本信息</div>
          ${rows.map(([key, value]) => `<div class="kv-row"><div class="kv-key">${esc(key)}</div><div class="kv-value">${key === 'status' ? `<span class="dot ${isFailed(item) ? 'error' : ''}"></span>` : ''}${esc(value)}${['run_id','task_id'].includes(key) ? `<button class="copy-mini" data-copy="${esc(value)}"><svg class="icon"><use href="#i-copy"/></svg></button>` : ''}</div></div>`).join('')}
        </section>
        <section class="detail-card">
          <div class="detail-card-title">消息内容</div>
          <div class="message-detail" id="messageDetail">${esc(detailText)}</div>
          ${renderDiffs(item)}
          <div class="copy-btn-wrap"><button class="copy-message" id="copyMessage"><svg class="icon"><use href="#i-copy"/></svg><span>复制消息内容</span></button></div>
        </section>`;
      document.querySelectorAll('[data-copy]').forEach(btn => btn.addEventListener('click', (event) => {
        event.stopPropagation();
        copyText(btn.dataset.copy);
      }));
      $('copyMessage')?.addEventListener('click', () => copyText($('messageDetail')?.innerText || ''));
    }
    function renderDiffs(item) {
      const diffs = item?.diffs;
      if (!Array.isArray(diffs) || !diffs.length) return '';
      return diffs.map(diff => `<div class="message-detail">${esc(diff.diff || diff.text || JSON.stringify(diff, null, 2))}</div>`).join('');
    }
    function selectItem(kind, item, node) {
      selected = {kind, item};
      lastRendered.details = '';
      document.querySelectorAll('.message-card,.step,.run-block').forEach(element => element.classList.remove('active'));
      node?.classList.add('active');
      renderDetails();
    }
    function refreshSelectedItem() {
      if (!selected.item) return;
      if (selected.kind === 'run') {
        selected.item = data.task_state || selected.item;
        return;
      }
      if (selected.kind === 'event') {
        const spanId = selected.item.span_id;
        const eventIndex = selected.item.event_index;
        selected.item = traceItems().find(item => (spanId && item.span_id === spanId) || (eventIndex !== undefined && item.event_index === eventIndex)) || selected.item;
        return;
      }
      if (selected.kind === 'message') {
        const history = historyItems();
        const index = history.findIndex(item => item.created_at === selected.item.created_at && item.role === selected.item.role);
        if (index >= 0) selected.item = history[index];
      }
    }
    function renderAll() {
      $('liveLabel').innerHTML = live ? '<span class="live-dot"></span>Live' : 'Snapshot';
      renderSessionSelect();
      renderMessages();
      renderTrace();
      if (!selected.item) selected = {kind: 'session', item: data.task_state || data.session};
      renderDetails();
    }
    async function loadSessionData(sessionId, revision = '') {
      const params = new URLSearchParams();
      if (sessionId) params.set('session', sessionId);
      if (revision) params.set('revision', revision);
      const response = await fetch(`/data?${params.toString()}`, {cache: 'no-store'});
      if (response.status === 204) return null;
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    }
    async function switchSession(sessionId) {
      selectedSession = sessionId;
      $('sessionPicker')?.classList.remove('open');
      $('sessionTrigger')?.setAttribute('aria-expanded', 'false');
      try {
        const nextData = await loadSessionData(selectedSession);
        if (!nextData) return;
        data = nextData;
        lastRevision = data.revision || '';
        selectedRun = '';
        selected = {kind: 'session', item: data.task_state || data.session};
        lastRendered = {session: '', runs: '', trace: '', details: ''};
        renderAll();
      } catch (error) {
        $('liveLabel').textContent = `load failed: ${error.message}`;
      }
    }
    $('sessionTrigger').addEventListener('click', (event) => {
      event.stopPropagation();
      const picker = $('sessionPicker');
      const open = !picker.classList.contains('open');
      picker.classList.toggle('open', open);
      $('sessionTrigger').setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('click', (event) => {
      if (!event.target.closest('#sessionPicker')) {
        $('sessionPicker')?.classList.remove('open');
        $('sessionTrigger')?.setAttribute('aria-expanded', 'false');
      }
    });
    $('traceSearch').addEventListener('input', (event) => {
      query = event.target.value.trim().toLowerCase();
      lastRendered.trace = '';
      renderTrace();
    });
    document.querySelectorAll('.filter-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('.filter-btn').forEach(item => item.classList.remove('active'));
        btn.classList.add('active');
        filter = btn.dataset.filter || 'all';
        lastRendered.trace = '';
        renderTrace();
      });
    });
    document.querySelectorAll('.tab').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(item => item.classList.remove('active'));
        tab.classList.add('active');
        activeTab = tab.dataset.tab;
        lastRendered.details = '';
        renderDetails();
      });
    });
    $('pinBtn').addEventListener('click', (event) => {
      event.currentTarget.classList.toggle('pinned');
      showToast(event.currentTarget.classList.contains('pinned') ? '面板已固定' : '已取消固定');
    });
    async function poll() {
      if (!live) return;
      try {
        const nextData = await loadSessionData(selectedSession, lastRevision);
        if (nextData && (nextData.revision || '') !== lastRevision) {
          data = nextData;
          lastRevision = data.revision || '';
          refreshSelectedItem();
          renderAll();
        }
      } catch (error) {
        $('liveLabel').textContent = `poll failed: ${error.message}`;
      } finally {
        setTimeout(poll, pollMs);
      }
    }
    renderAll();
    poll();
  </script>
</body>
</html>
"""
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
