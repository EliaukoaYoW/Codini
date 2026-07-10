"""
运行工件落盘。

session.json 负责保存“可恢复的会话状态”；RunStore 负责保存“单次运行的审计工件”，
例如 task_state、trace 和 report。两者分开后，恢复现场和复盘证据不会混在一起。

在 trace 数据层里，RunStore 同时承担两个角色：
1. 原有的 "写 trace.jsonl / task_state.json / report.json"（一次 ask 一组工件）
2. 新增的 "runs/index.jsonl" 运行索引，记录每次 run 是谁启的（parent_run_id）、
   当前 run 的层级路径（agent_rope）、以及可在 viewer 里直接展示的聚合小计
   （token / latency / tool_steps / stop_reason）。有了这条索引，静态 viewer 和
   CLI 查询就不需要每次都扫目录。

每个 trace event 都会通过 "inherited_fields" 自动挂上 run 级共享字段
（parent_run_id, agent_rope, read_only, approval_policy, ...），这样：
- 单个 event 自包含，跨 run 跳到子 trace 时不需要回头翻索引
- viewer 切父/子 trace 时能立刻拿到上下文
"""

import json
import tempfile
import time
from pathlib import Path


def _run_id(value):
    if hasattr(value, "run_id"):
        return value.run_id
    return str(value)

class RunStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
    
    def run_dir(self, run_id):
        return self.root / _run_id(run_id)
    
    def task_state_path(self, run_id):
        return self.run_dir(run_id) / "task_state.json"
    
    def trace_path(self, run_id):
        return self.run_dir(run_id) / "trace.jsonl"
    
    def report_path(self, run_id):
        return self.run_dir(run_id) / "report.json"

    def index_path(self):
        return self.root / "runs" / "index.jsonl"

    def runs_dir(self):
        return self.root / "runs"

    def start_run(self, task_state):
        # 每次 ask() 都会生成一个 run 目录。
        # 这样一次用户请求对应一组独立工件，后续排查更容易。
        self.root.mkdir(parents=True, exist_ok=True)
        self.runs_dir().mkdir(parents=True, exist_ok=True)
        run_dir = self.run_dir(task_state)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.write_task_state(task_state)
        self._index_run(task_state)
        return run_dir

    def write_task_state(self, task_state):
        path = self.task_state_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, task_state.to_dict())
        return path
    
    def append_trace(self, task_state, event):
        path = self.trace_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, ensure_ascii=True))
            handle.write("\n")
        return path
    
    def write_report(self, task_state, report):
        path = self.report_path(task_state)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, report)
        return path
    
    def record_run_summary(self, task_state, summary):
        """把聚合小计重写到 task_state.json，并同步更新索引。"""
        path = self.task_state_path(task_state)
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = task_state.to_dict()
        current["summary"] = summary
        self._write_json_atomic(path, current)
        # 同步更新索引，这样 viewer 直接扫索引就能看到一行 run
        self._index_run(task_state, summary=summary)
        return path

    def load_report(self, task_id):
        return json.loads(self.report_path(task_id).read_text(encoding="utf-8"))
    
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
            handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=True))
            handle.write("\n")
        return entry

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
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp_name = handle.name
        Path(temp_name).replace(path)