import contextvars
import time
import uuid
import json
from datetime import datetime
from typing import Optional, List, Dict, Any


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")

class Span:
    def __init__(self, trace_id: str, span_id: str, parent_span_id: Optional[str], name: str, depth: int = 0):
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.name = name
        self.depth = depth
        self.start_time = time.monotonic()
        self.start_time_iso = _now_iso()
        self.end_time: Optional[float] = None
        self.duration_ms: Optional[int] = None
        self.attributes: Dict[str, Any] = {}
        self.events: List[Dict[str, Any]] = []
        self._tracer: Optional["Tracer"] = None

    def set_attribute(self, key: str, value: Any):
        self.attributes[key] = value

    def set_attributes(self, attrs: Dict[str, Any]):
        self.attributes.update(attrs)

    def add_event(self, name: str, attributes: Optional[Dict[str, Any]] = None):
        event_time = time.monotonic()
        event_data = {
            "name": name,
            "time": event_time,
            "time_iso": _now_iso(),
            "attributes": attributes or {}
        }
        self.events.append(event_data)
        if self._tracer:
            self._tracer.on_span_event(self, name, event_data["attributes"])

    def finish(self, status: str = "OK"):
        self.end_time = time.monotonic()
        self.duration_ms = int((self.end_time - self.start_time) * 1000)
        self.attributes["span_status"] = status
        if self._tracer:
            self._tracer.end_span(self)


class SpanProcessor:
    def on_span_start(self, span: Span):
        pass

    def on_span_end(self, span: Span):
        pass

    def on_span_event(self, span: Span, name: str, attributes: Dict[str, Any]):
        pass


class Tracer:
    def __init__(self):
        self.processors: List[SpanProcessor] = []
        self._active_span_var = contextvars.ContextVar(f"active_span_{uuid.uuid4().hex[:8]}", default=None)

    def register_processor(self, processor: SpanProcessor):
        self.processors.append(processor)

    def unregister_processor(self, processor: SpanProcessor):
        if processor in self.processors:
            self.processors.remove(processor)

    def start_span(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> Span:
        attributes = attributes or {}
        parent: Optional[Span] = self._active_span_var.get()
        
        span_id = "span_" + uuid.uuid4().hex[:12]
        
        if parent:
            trace_id = parent.trace_id
            parent_span_id = parent.span_id
            # depth 只有在是根层级嵌套 ask 时才递增，普通的同级 Span 继承父级 depth
            depth = parent.depth + (1 if name == "agent.ask" else 0)
        else:
            trace_id = attributes.get("trace_id") or ("run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
            parent_span_id = attributes.get("parent_span_id")
            depth = attributes.get("depth", 0)

        span = Span(trace_id=trace_id, span_id=span_id, parent_span_id=parent_span_id, name=name, depth=depth)
        span._tracer = self
        span.set_attributes(attributes)
        
        for p in self.processors:
            try:
                p.on_span_start(span)
            except Exception:
                pass
        return span

    def end_span(self, span: Span):
        for p in self.processors:
            try:
                p.on_span_end(span)
            except Exception:
                pass

    def on_span_event(self, span: Span, name: str, attributes: Dict[str, Any]):
        for p in self.processors:
            try:
                p.on_span_event(span, name, attributes)
            except Exception:
                pass

    def span_scope(self, name: str, attributes: Optional[Dict[str, Any]] = None) -> "SpanScope":
        return SpanScope(self, name, attributes)

    def get_current_span(self) -> Optional[Span]:
        return self._active_span_var.get()


class SpanScope:
    def __init__(self, tracer: Tracer, name: str, attributes: Optional[Dict[str, Any]] = None):
        self.tracer = tracer
        self.name = name
        self.attributes = attributes or {}
        self.span: Optional[Span] = None
        self.token = None

    def __enter__(self) -> Span:
        self.span = self.tracer.start_span(self.name, self.attributes)
        self.token = self.tracer._active_span_var.set(self.span)
        return self.span

    def __exit__(self, exc_type, exc_val, exc_tb):
        status = "OK"
        if exc_type:
            status = "ERROR"
            self.span.set_attribute("error", str(exc_val))
        self.span.finish(status=status)
        self.tracer._active_span_var.reset(self.token)


class FileSpanExporter(SpanProcessor):
    def __init__(self, agent):
        self.agent = agent

    def _build_inherited(self, task_state, span):
        inherited = {"depth": span.depth}
        if not task_state:
            return inherited
        inherited.update(getattr(task_state, "_run_inherited", None) or {})
        inherited["agent_rope"] = getattr(task_state, "agent_rope", "") or inherited.get("agent_rope", "")
        inherited["approval_policy"] = getattr(task_state, "approval_policy", "") or inherited.get("approval_policy", "")
        inherited["depth"] = span.depth
        inherited["model"] = getattr(task_state, "model", "") or inherited.get("model", "")
        inherited["parent_run_id"] = getattr(task_state, "parent_run_id", "") or inherited.get("parent_run_id", "")
        inherited["parent_span_id"] = getattr(task_state, "parent_span_id", "") or inherited.get("parent_span_id", "")
        inherited["parent_tool_event_index"] = int(getattr(task_state, "parent_tool_event_index", -1) or -1)
        inherited["provider"] = getattr(task_state, "provider", "") or inherited.get("provider", "")
        inherited["read_only"] = bool(getattr(task_state, "read_only", False))
        inherited["sandbox"] = str(getattr(task_state, "sandbox", "none") or "none")
        inherited["session_id"] = getattr(task_state, "session_id", "") or inherited.get("session_id", "")
        return inherited

    def _write_event(self, event_name, span, extra=None):
        task_state = getattr(self.agent, "current_task_state", None)
        payload = {
            "event": event_name,
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "parent_span_id": span.parent_span_id or "",
            "span_name": span.name,
            "depth": span.depth,
            "created_at": _now_iso(),
        }
        started_at = getattr(self.agent, "_trace_started_at", None)
        if started_at:
            payload["elapsed_ms"] = int((time.monotonic() - started_at) * 1000)
        inherited = self._build_inherited(task_state, span)
        payload["inherited"] = inherited
        payload["created_inherited_fields"] = sorted(inherited.keys())

        if extra:
            payload.update(extra)

        redacted_payload = self.agent.redact_artifact(payload)

        if task_state and hasattr(self.agent, "run_store"):
            session_id = getattr(task_state, "session_id", "")
            if session_id:
                self.agent.run_store.append_trace(task_state, redacted_payload)

    def on_span_start(self, span):
        if span.name == "agent.ask":
            self._write_event("run_started", span, {
                "task_id": span.attributes.get("task_id", ""),
                "user_request": span.attributes.get("user_request", "")
            })
        elif span.name == "llm.complete":
            self._write_event("model_requested", span, {
                "attempts": span.attributes.get("attempts", 1),
                "tool_steps": span.attributes.get("tool_steps", 0),
                "prompt_cache_supported": bool(span.attributes.get("prompt_cache_supported", False)),
                "prompt_cache_requested": bool(span.attributes.get("prompt_cache_requested", False)),
                "prompt_cache_key": span.attributes.get("prompt_cache_key"),
                "prompt_cache_retention": span.attributes.get("prompt_cache_retention"),
                "prompt_chars": span.attributes.get("prompt_chars", 0),
                "prompt": span.attributes.get("prompt", ""),
            })
        elif span.name.startswith("tool."):
            self._write_event("tool_requested", span, {
                "name": span.attributes.get("name"),
                "args": span.attributes.get("args"),
                "risk_level": span.attributes.get("risk_level", "low"),
            })

    def on_span_end(self, span):
        if span.name == "agent.ask":
            self._write_event("run_finished", span, {
                "status": span.attributes.get("status", "completed"),
                "span_status": span.attributes.get("span_status", "OK"),
                "stop_reason": span.attributes.get("stop_reason", ""),
                "final_answer": span.attributes.get("final_answer", ""),
                "error": span.attributes.get("error_info", {}),
                "error_type": span.attributes.get("error_type", ""),
                "error_code": span.attributes.get("error_code", ""),
                "provider": span.attributes.get("provider", ""),
                "http_status": span.attributes.get("http_status"),
                "retryable": span.attributes.get("retryable"),
                "run_duration_ms": span.duration_ms,
            })
        elif span.name == "llm.complete":
            error_info = {}
            if span.attributes.get("span_status") == "ERROR" and span.attributes.get("error"):
                error_info = self.agent.build_error_info(
                    span.attributes.get("error", ""),
                    getattr(getattr(self.agent, "current_task_state", None), "stop_reason", "") or "runtime_error",
                )
            self._write_event("model_parsed", span, {
                "kind": span.attributes.get("kind"),
                "raw": span.attributes.get("raw"),
                "span_status": span.attributes.get("span_status", "OK"),
                "error": error_info,
                "error_type": error_info.get("error_type", ""),
                "error_code": error_info.get("error_code", ""),
                "provider": error_info.get("provider", ""),
                "http_status": error_info.get("http_status"),
                "retryable": error_info.get("retryable"),
                "duration_ms": span.duration_ms,
                "cache_hit": bool(span.attributes.get("cache_hit", False)),
                "prompt_tokens": span.attributes.get("prompt_tokens", 0),
                "completion_tokens": span.attributes.get("completion_tokens", 0),
                "total_tokens": span.attributes.get("total_tokens", 0),
                "cached_tokens": span.attributes.get("cached_tokens", 0),
            })
        elif span.name.startswith("tool."):
            self._write_event("tool_executed", span, {
                "name": span.attributes.get("name"),
                "args": span.attributes.get("args"),
                "result": span.attributes.get("result"),
                "result_full": span.attributes.get("result_full"),
                "full_error": span.attributes.get("full_error"),
                "span_status": span.attributes.get("span_status", "OK"),
                "duration_ms": span.duration_ms,
                "exit_code": span.attributes.get("exit_code"),
                "security_event_type": span.attributes.get("security_event_type"),
                "risk_level": span.attributes.get("risk_level"),
                "approval_policy": span.attributes.get("approval_policy"),
                "approved": span.attributes.get("approved"),
                "workspace_changed": span.attributes.get("workspace_changed"),
                "affected_paths": span.attributes.get("affected_paths"),
                "tool_status": span.attributes.get("tool_status"),
                "tool_error_code": span.attributes.get("tool_error_code"),
                "child_session_id": span.attributes.get("child_session_id"),
                "child_run_id": span.attributes.get("child_run_id"),
                "child_trace_id": span.attributes.get("child_trace_id"),
                "diffs": span.attributes.get("diffs", []),
            })

    def on_span_event(self, span, name, attributes):
        extra = {}
        if name == "checkpoint_created":
            checkpoint_id = attributes.get("checkpoint_id")
            if checkpoint_id and hasattr(self.agent, "checkpoint_state"):
                state = self.agent.checkpoint_state()
                ckpt = state.get("items", {}).get(checkpoint_id)
                if ckpt:
                    extra = {
                        "current_goal": ckpt.get("current_goal", ""),
                        "current_blocker": ckpt.get("current_blocker", ""),
                        "next_step": ckpt.get("next_step", ""),
                        "key_files": [kf.get("path") for kf in ckpt.get("key_files", []) if kf.get("path")],
                    }
        attrs_copy = dict(attributes)
        attrs_copy.update(extra)
        self._write_event(name, span, attrs_copy)
