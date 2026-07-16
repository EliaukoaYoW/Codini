"""
运行工件落盘。

session.json 负责保存"可恢复的会话状态"；RunStore 负责保存"单次运行的审计工件"，
例如 task_state、trace 和 report。两者分开后，恢复现场和复盘证据不会混在一起。

每个 trace event 都会通过 "inherited_fields" 自动挂上 run 级共享字段
（parent_run_id, agent_rope, read_only, approval_policy, ...），这样：
- 单个 event 自包含，跨 run 跳到子 trace 时不需要回头翻索引
- viewer 切父/子 trace 时能立刻拿到上下文
"""

import json
import tempfile
import time
from datetime import datetime
from pathlib import Path


def _run_id(value):
    if hasattr(value, "run_id"):
        return value.run_id
    return str(value)


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class RunStore:
    def __init__(self, root):
        self.root = Path(root)
        (self.root.parent / "sessions").mkdir(parents=True, exist_ok=True)

    def session_dir(self, run_id):
        session_id = getattr(run_id, "session_id", "")
        if not session_id and isinstance(run_id, str):
            for p in self.root.parent.glob("sessions/*/task_state_history.jsonl"):
                for entry in self._iter_jsonl(p):
                    if entry.get("run_id") == run_id:
                        return p.parent
            # 一个 session 下会有多个 run；优先从 report_history 里反查 run_id 对应的 session。
            for p in self.root.parent.glob("sessions/*/report_history.jsonl"):
                for entry in self._iter_jsonl(p):
                    if entry.get("run_id") == run_id:
                        return p.parent
            for p in self.root.parent.glob("sessions/*/report.json"):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if data.get("run_id") == run_id:
                    return p.parent
        return self.root.parent / "sessions" / session_id

    def task_state_path(self, run_id):
        return self.session_dir(run_id) / "task_state.json"

    def task_state_history_path(self, run_id):
        return self.session_dir(run_id) / "task_state_history.jsonl"

    def trace_path(self, run_id):
        return self.session_dir(run_id) / "trace.jsonl"

    def report_path(self, run_id):
        return self.session_dir(run_id) / "report.json"

    def report_history_path(self, run_id):
        return self.session_dir(run_id) / "report_history.jsonl"

    def trace_manifest_path(self, run_id):
        return self.session_dir(run_id) / "trace_manifest.json"

    def index_path(self):
        return self.root / "index.jsonl"

    def runs_dir(self):
        return self.root

    def start_run(self, task_state):
        sess_dir = self.session_dir(task_state)
        sess_dir.mkdir(parents=True, exist_ok=True)
        self.write_task_state(task_state, append_history=False)
        self._index_run(task_state)
        return sess_dir

    def write_task_state(self, task_state, append_history=True, trigger="", related_span_id="", related_event=""):
        path = self.task_state_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = task_state.to_dict()
        self._write_json_atomic(path, payload)
        if append_history:
            self._append_history_jsonl_if_changed(
                self.task_state_history_path(task_state),
                self._history_entry(
                    payload,
                    history_kind="task_state",
                    task_state=task_state,
                    trigger=trigger,
                    related_span_id=related_span_id,
                    related_event=related_event,
                ),
            )
        return path

    def append_trace(self, task_state, event):
        path = self.trace_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)
        if "event_index" not in event:
            event["event_index"] = self._next_jsonl_index(path, key="event_index")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=False))
            handle.write("\n")
        return path

    def write_report(self, task_state, report, trigger="report_written", related_span_id="", related_event=""):
        path = self.report_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, report)
        self._append_history_jsonl_if_changed(
            self.report_history_path(task_state),
            self._history_entry(
                report,
                history_kind="report",
                task_state=task_state,
                trigger=trigger,
                related_span_id=related_span_id,
                related_event=related_event,
            ),
        )
        return path

    def record_run_summary(self, task_state, summary, write_task_state=True, trigger="summary_updated", related_span_id="", related_event=""):
        """把聚合小计重写到 task_state.json，并同步更新索引。"""
        if write_task_state:
            path = self.task_state_path(task_state)
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                current = task_state.to_dict()
            current["summary"] = summary
            self._write_json_atomic(path, current)
            self._append_history_jsonl_if_changed(
                self.task_state_history_path(task_state),
                self._history_entry(
                    current,
                    history_kind="task_state",
                    task_state=task_state,
                    trigger=trigger,
                    related_span_id=related_span_id,
                    related_event=related_event,
                ),
            )
        else:
            path = self.task_state_path(task_state)
        self._index_run(task_state, summary=summary)
        return path

    def record_child_run(self, parent_task_state, child_info):
        manifest_path = self.trace_manifest_path(parent_task_state)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {
                "session_id": getattr(parent_task_state, "session_id", ""),
                "root_run_id": _run_id(parent_task_state),
                "children": [],
                "updated_at": _now_iso(),
            }
        children = list(manifest.get("children") or [])
        child_run_id = child_info.get("child_run_id") or child_info.get("child_trace_id")
        parent_span_id = child_info.get("parent_span_id", "")
        existing_index = next(
            (
                index for index, item in enumerate(children)
                if item.get("child_run_id") == child_run_id and item.get("parent_span_id") == parent_span_id
            ),
            None,
        )
        entry = dict(child_info)
        entry["recorded_at"] = _now_iso()
        if existing_index is None:
            children.append(entry)
        else:
            children[existing_index] = {**children[existing_index], **entry}
        manifest["children"] = children
        manifest["updated_at"] = _now_iso()
        self._write_json_atomic(manifest_path, manifest)
        return manifest_path

    def load_task_state(self, run_id):
        history_path = self.task_state_history_path(run_id)
        latest = None
        for entry in self._iter_jsonl(history_path):
            if entry.get("run_id") == _run_id(run_id):
                latest = entry
        if latest is not None:
            latest.pop("history_kind", None)
            latest.pop("snapshot_at", None)
            latest.pop("snapshot_index", None)
            latest.pop("changed_fields", None)
            latest.pop("elapsed_ms", None)
            latest.pop("trigger", None)
            latest.pop("related_span_id", None)
            latest.pop("related_event", None)
            latest.pop("trace_id", None)
            return latest
        return json.loads(self.task_state_path(run_id).read_text(encoding="utf-8"))

    def load_report(self, run_id):
        history_path = self.report_history_path(run_id)
        latest = None
        for entry in self._iter_jsonl(history_path):
            if entry.get("run_id") == _run_id(run_id):
                latest = entry
        if latest is not None:
            latest.pop("history_kind", None)
            latest.pop("snapshot_at", None)
            latest.pop("snapshot_index", None)
            latest.pop("changed_fields", None)
            latest.pop("elapsed_ms", None)
            latest.pop("trigger", None)
            latest.pop("related_span_id", None)
            latest.pop("related_event", None)
            latest.pop("trace_id", None)
            return latest
        return json.loads(self.report_path(run_id).read_text(encoding="utf-8"))

    def load_trace_events(self, run_id):
        trace_id = _run_id(run_id)
        return [
            event
            for event in self._iter_jsonl(self.trace_path(run_id))
            if event.get("trace_id") == trace_id
        ]

    def _index_run(self, task_state, summary=None):
        """把一行 run 摘要追加到 runs/index.jsonl。"""
        index_path = self.index_path()
        index_path.parent.mkdir(parents=True, exist_ok=True)

        entry = {
            "run_id": _run_id(task_state),
            "task_id": getattr(task_state, "task_id", ""),
            "session_id": getattr(task_state, "session_id", ""),
            "parent_run_id": getattr(task_state, "parent_run_id", "") or "",
            "parent_tool_event_index": int(getattr(task_state, "parent_tool_event_index", -1) or -1),
            "parent_span_id": getattr(task_state, "parent_span_id", "") or "",
            "agent_rope": getattr(task_state, "agent_rope", "") or task_state.run_id,
            "depth": int(getattr(task_state, "depth", 0) or 0),
            "created_at": getattr(task_state, "created_at", "") or int(time.time()),
            "user_request": str(getattr(task_state, "user_request", "") or "")[:200],
            "status": getattr(task_state, "status", ""),
            "stop_reason": getattr(task_state, "stop_reason", ""),
            "tool_steps": int(getattr(task_state, "tool_steps", 0) or 0),
            "attempts": int(getattr(task_state, "attempts", 0) or 0),
        }
        inherited = getattr(task_state, "_run_inherited", None) or {}
        for key in ("approval_policy", "read_only", "sandbox", "model", "provider"):
            if inherited.get(key) is not None:
                entry[key] = inherited[key]
        if summary:
            entry["summary"] = summary

        with index_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False))
            handle.write("\n")
        return entry

    def _history_entry(self, payload, history_kind, task_state, trigger="", related_span_id="", related_event=""):
        entry = dict(payload)
        entry["history_kind"] = history_kind
        entry["snapshot_at"] = _now_iso()
        entry["created_at"] = entry.get("created_at") or _now_iso()
        entry["trace_id"] = _run_id(task_state)
        entry["session_id"] = getattr(task_state, "session_id", "") or entry.get("session_id", "")
        entry["trigger"] = trigger or history_kind
        entry["related_span_id"] = related_span_id or ""
        entry["related_event"] = related_event or ""
        started_at = getattr(task_state, "_trace_started_at", None)
        entry["elapsed_ms"] = int((time.monotonic() - started_at) * 1000) if started_at else None
        return entry

    def _append_jsonl(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=False))
            handle.write("\n")
        return path

    def _append_history_jsonl_if_changed(self, path, payload):
        last_entry = self._read_last_jsonl_entry(path)
        if self._normalized_history_entry(last_entry) == self._normalized_history_entry(payload):
            return path
        payload = dict(payload)
        payload["snapshot_index"] = self._next_jsonl_index(path, key="snapshot_index")
        payload["changed_fields"] = self._changed_fields(last_entry, payload)
        return self._append_jsonl(path, payload)

    def _iter_jsonl(self, path):
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
        except OSError:
            return

    def _read_last_jsonl_entry(self, path):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                return json.loads(line)
            except ValueError:
                continue
        return None

    def _normalized_history_entry(self, payload):
        if payload is None:
            return None
        normalized = dict(payload)
        normalized.pop("history_kind", None)
        normalized.pop("snapshot_at", None)
        normalized.pop("snapshot_index", None)
        normalized.pop("changed_fields", None)
        normalized.pop("elapsed_ms", None)
        normalized.pop("trigger", None)
        normalized.pop("related_span_id", None)
        normalized.pop("related_event", None)
        return normalized

    def _changed_fields(self, old_payload, new_payload):
        old = self._normalized_history_entry(old_payload) or {}
        new = self._normalized_history_entry(new_payload) or {}
        return sorted(key for key in set(old) | set(new) if old.get(key) != new.get(key))

    def _next_jsonl_index(self, path, key):
        last_entry = self._read_last_jsonl_entry(path)
        try:
            return int(last_entry.get(key, 0)) + 1
        except (AttributeError, TypeError, ValueError):
            return 1

    def _write_json_atomic(self, path, payload):
        # 原子写：先写临时文件，再 replace。这样即使中途异常，也不容易留下半截 JSON。
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(path.parent),
            prefix=path.name + ".",
            suffix=".tmp",
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            temp_name = handle.name
        Path(temp_name).replace(path)