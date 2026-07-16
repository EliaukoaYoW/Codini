"""
一次 ask() 运行过程中的状态机快照。

它回答两个层面的问题：
1. 运行时语义：这次用户请求当前进行到哪了、调了多少次工具、最后为什么停下。
2. 可视化 trace 语义：这次 run 是谁启的（parent_run_id）、在树里的层级路径（agent_rope）、
   可共享给 UI 渲染的聚合小计（tokens / latency / tools）以及 run 级共享字段
   （approval_policy / read_only / sandbox / model ...）。

这个对象会被不断写入 task_state.json，供运行中观察和运行后复盘。
trace 数据层约定：每个 trace event 会被 append_run_inherited 挂上这里的
_run_inherited 字段，于是单个 event 自包含，跨 run 跳到子 trace 时无需回头翻索引。
"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_STOPPED = "stopped"
STATUS_FAILED = "failed"

STOP_REASON_FINAL_ANSWER_RETURNED = "final_answer_returned"
STOP_REASON_STEP_LIMIT_REACHED = "step_limit_reached"
STOP_REASON_RETRY_LIMIT_REACHED = "retry_limit_reached"
STOP_REASON_MODEL_ERROR = "model_error"
STOP_REASON_TOOL_TIMEOUT = "tool_timeout"
STOP_REASON_APPROVAL_DENIED = "approval_denied"
STOP_REASON_DELEGATE_FAILED = "delegate_failed"
STOP_REASON_PERSISTENCE_ERROR = "persistence_error"
STOP_REASON_RESUME_LOAD_ERROR = "resume_load_error"
STOP_REASON_RUNTIME_ERROR = "runtime_error"


@dataclass
class TaskState:
    run_id: str
    task_id: str
    user_request: str
    status: str = STATUS_RUNNING
    tool_steps: int = 0
    attempts: int = 0
    last_tool: str = ""
    stop_reason: str = ""
    final_answer: str = ""
    error: dict = field(default_factory=dict)
    checkpoint_id: str = ""
    resume_status: str = ""

    session_id: str = ""
    parent_run_id: str = ""
    parent_tool_event_index: int = -1
    parent_span_id: str = ""
    agent_rope: str = ""
    depth: int = 0
    # 子 task_state 归子 run 自己的 session_store 管，session_id 给 viewer 用来跳到父 session
    created_at: str = ""

    # run 级共享字段，会跟随每个 trace event 下发
    approval_policy: str = ""
    read_only: bool = False
    sandbox: str = "none"
    model: str = ""
    provider: str = ""

    # viewer 可渲染的聚合小计
    summary: dict = field(default_factory=dict)
    # 内部复用：给 Codini 注入跨 run 共享字段用
    _run_inherited: dict = field(default_factory=dict)

    @classmethod
    def create(cls, task_id, user_request, run_id="", **kwargs):
        if not run_id:
            run_id = "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
        parent_agent_rope = kwargs.pop("agent_rope", None)
        agent_rope = (parent_agent_rope + "::" + run_id) if parent_agent_rope else run_id
        return cls(
            run_id=run_id,
            task_id=task_id,
            user_request=user_request,
            agent_rope=agent_rope,
            created_at=_now_iso(),
            **kwargs,
        )

    @classmethod
    def from_dict(cls, data):
        base = {
            "run_id": str(data.get("run_id", "")),
            "task_id": str(data.get("task_id", "")),
            "user_request": str(data.get("user_request", "")),
            "status": str(data.get("status", STATUS_RUNNING)),
            "tool_steps": int(data.get("tool_steps", 0)),
            "attempts": int(data.get("attempts", 0)),
            "last_tool": str(data.get("last_tool", "")),
            "stop_reason": str(data.get("stop_reason", "")),
            "final_answer": str(data.get("final_answer", "")),
            "error": dict(data.get("error") or {}),
            "checkpoint_id": str(data.get("checkpoint_id", "")),
            "resume_status": str(data.get("resume_status", "")),
            "session_id": str(data.get("session_id", "")),
            "parent_run_id": str(data.get("parent_run_id", "")),
            "parent_tool_event_index": int(data.get("parent_tool_event_index", -1)),
            "parent_span_id": str(data.get("parent_span_id", "")),
            "agent_rope": str(data.get("agent_rope", "")),
            "depth": int(data.get("depth", 0)),
            "created_at": str(data.get("created_at", "")),
            "approval_policy": str(data.get("approval_policy", "")),
            "read_only": bool(data.get("read_only", False)),
            "sandbox": str(data.get("sandbox", "none")),
            "model": str(data.get("model", "")),
            "provider": str(data.get("provider", "")),
            "summary": dict(data.get("summary") or {}),
        }
        return cls(**base)

    def record_attempt(self):
        # attempt 统计的是"模型被调用了几轮"，不等于 tool_steps。
        self.attempts += 1
        return self

    def record_tool(self, tool_name):
        self.tool_steps += 1
        self.last_tool = str(tool_name or "")
        return self

    def stop(self, stop_reason, status=STATUS_STOPPED, final_answer=""):
        # stop_reason 和 status 分开存，是为了区分"怎么停的"和"停下时是什么状态"。
        self.status = status
        self.stop_reason = stop_reason
        if final_answer != "":
            self.final_answer = final_answer
        return self

    def stop_step_limit(self, final_answer=""):
        return self.stop(STOP_REASON_STEP_LIMIT_REACHED, final_answer=final_answer)

    def stop_retry_limit(self, final_answer=""):
        return self.stop(STOP_REASON_RETRY_LIMIT_REACHED, final_answer=final_answer)

    def stop_model_error(self, final_answer=""):
        return self.stop(STOP_REASON_MODEL_ERROR, status=STATUS_FAILED, final_answer=final_answer)

    def stop_runtime_error(self, final_answer=""):
        return self.stop(STOP_REASON_RUNTIME_ERROR, status=STATUS_FAILED, final_answer=final_answer)

    def finish_success(self, final_answer):
        self.status = STATUS_COMPLETED
        self.stop_reason = STOP_REASON_FINAL_ANSWER_RETURNED
        self.final_answer = str(final_answer)
        return self

    def to_dict(self):
        # 非运行时字段也一并写盘，方便 viewer 真空读取。
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "user_request": self.user_request,
            "status": self.status,
            "tool_steps": self.tool_steps,
            "attempts": self.attempts,
            "last_tool": self.last_tool,
            "stop_reason": self.stop_reason,
            "final_answer": self.final_answer,
            "error": self.error,
            "checkpoint_id": self.checkpoint_id,
            "resume_status": self.resume_status,
            "parent_run_id": self.parent_run_id,
            "parent_tool_event_index": self.parent_tool_event_index,
            "parent_span_id": self.parent_span_id,
            "agent_rope": self.agent_rope,
            "depth": self.depth,
            "created_at": self.created_at,
            "approval_policy": self.approval_policy,
            "read_only": self.read_only,
            "sandbox": self.sandbox,
            "model": self.model,
            "provider": self.provider,
            "summary": self.summary,
        }
