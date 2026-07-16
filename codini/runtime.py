"""
Agent 运行时核心逻辑。

Codini 就是包在模型外面的控制循环：负责组 prompt、解析模型输出、
校验并执行工具、写 trace、更新工作记忆，以及在合适的时候停下来。
"""

import json
import os
import re
import textwrap
import uuid
import hashlib
import time
import difflib
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


from . import memory as memorylib
from .context_manager import ContextManager
from .run_store import RunStore
from .task_state import TaskState
from .sandbox import NoSandbox
from . import tools as toolkit
from .workspace import IGNORED_PATH_NAMES, MAX_HISTORY, WorkspaceContext, clip, now
from .trace import Tracer, TraceSpanProcessor, FileSpanExporter, Span

SENSITIVE_ENV_NAME_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
REDACTED_VALUE = "<redacted>"
DEFAULT_SHELL_ENV_ALLOWLIST = ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "PWD", "SHELL", "TERM", "TMPDIR", "TMP", "TEMP", "USER")
DEFAULT_FEATURE_FLAGS = {
    "memory": True,
    "relevant_memory": True,
    "context_reduction": True,
    "prompt_cache": True,
}
CHECKPOINT_SCHEMA_VERSION = "phase1-v1"
CHECKPOINT_NONE_STATUS = "no-checkpoint"
CHECKPOINT_FULL_VALID_STATUS = "full-valid"
CHECKPOINT_PARTIAL_STALE_STATUS = "partial-stale"
CHECKPOINT_WORKSPACE_MISMATCH_STATUS = "workspace-mismatch"
CHECKPOINT_SCHEMA_MISMATCH_STATUS = "schema-mismatch"
DURABLE_MEMORY_INTENT_PATTERN = re.compile(r"(?i)\b(capture|remember|save|store|persist|note)\b")
DURABLE_MEMORY_INTENT_ZH_PATTERN = re.compile(r"(记住|保存|记录|沉淀|长期记忆|持久记忆)")
DURABLE_MEMORY_LINE_PATTERNS = (
    ("project-conventions", re.compile(r"(?i)^Project convention:\s*(.+)$")),
    ("key-decisions", re.compile(r"(?i)^Decision:\s*(.+)$")),
    ("dependency-facts", re.compile(r"(?i)^Dependency:\s*(.+)$")),
    ("user-preferences", re.compile(r"(?i)^Preference:\s*(.+)$")),
    ("project-conventions", re.compile(r"^项目约定：\s*(.+)$")),
    ("key-decisions", re.compile(r"^决策：\s*(.+)$")),
    ("dependency-facts", re.compile(r"^依赖：\s*(.+)$")),
    ("user-preferences", re.compile(r"^偏好：\s*(.+)$")),
)
SECRET_SHAPED_TEXT_PATTERN = re.compile(r"(?i)(\b(api[_ -]?key|token|secret|password)\b|sk-[A-Za-z0-9_-]{6,})")


def _extract_run_usage_fields(completion_metadata):
    """把后端返回的 usage / cache 字段归一化成 viewer 可渲染的平坦字段。

    provider 字段名可能不一样（prompt_tokens / input_tokens），这里做一层适配。
    """
    completion_metadata = completion_metadata or {}
    usage = completion_metadata.get("usage") or {}
    input_details = (
        usage.get("input_tokens_details") or usage.get("prompt_tokens_details")
        or completion_metadata.get("prompt_cache_details") or {}
    )
    prompt_tokens = (
        completion_metadata.get("prompt_tokens")
        or completion_metadata.get("input_tokens") or usage.get("prompt_tokens")
        or usage.get("input_tokens") or 0
    )
    completion_tokens = (
        completion_metadata.get("completion_tokens")
        or completion_metadata.get("output_tokens") or usage.get("completion_tokens")
        or usage.get("output_tokens") or 0
    )
    total_tokens = (
        completion_metadata.get("total_tokens")
        or usage.get("total_tokens")
        or (int(prompt_tokens or 0) + int(completion_tokens or 0))
    )
    cached_tokens = (
        completion_metadata.get("cached_tokens")
        or input_details.get("cached_tokens")
        or 0
    )
    return {
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(total_tokens or 0),
        "cached_tokens": int(cached_tokens or 0),
        "cache_hit": int(cached_tokens or 0) > 0,
    }


def _extract_error_payload(message):
    text = str(message or "")
    start = text.find("{")
    if start < 0:
        return {}
    try:
        payload = json.loads(text[start:])
    except Exception:
        return {}
    if isinstance(payload, dict):
        nested = payload.get("error")
        if isinstance(nested, dict):
            return nested
        return payload
    return {}


@dataclass
class PromptPrefix:
    text: str
    hash: str
    workspace_fingerprint: str
    tool_signature: str
    built_at: str

class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id):
        return self.root / session_id / "session.json"

    def save(self, session):
        path = self.path(session["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def load(self, session_id):
        return json.loads(self.path(session_id).read_text(encoding="utf-8"))

    def latest(self):
        files = sorted(self.root.glob("*/session.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].parent.name if files else None

class Codini:
    def __init__(
            self,
            model_client,
            workspace,
            session_store,
            session=None,
            run_store=None,
            approval_policy="ask",
            max_steps=6,
            max_new_tokens=512,
            depth=0,
            max_depth=1,
            read_only=False,
            shell_env_allowlist=None,
            secret_env_names=None,
            feature_flags=None,
            sandbox=None,
            trace=None,
    ):
        self.trace = trace
        self.model_client = model_client
        self.workspace = workspace
        self.root = Path(workspace.repo_root)
        self.session_store = session_store
        self.approval_policy = approval_policy
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        self.depth = depth
        self.max_depth = max_depth
        self.read_only = read_only
        self.shell_env_allowlist = tuple(shell_env_allowlist or DEFAULT_SHELL_ENV_ALLOWLIST)
        self.secret_env_names = {str(name).upper() for name in (secret_env_names or ())}
        self.feature_flags = dict(DEFAULT_FEATURE_FLAGS)
        if feature_flags:
            self.feature_flags.update({str(key): bool(value) for key,value in feature_flags.items()})
        self.run_store = run_store or RunStore(Path(workspace.repo_root) / ".codini" / "runs")
        self.sandbox = sandbox or NoSandbox()
        self.session = session or {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "created_at": now(),
            "workspace_root": workspace.repo_root,
            "history": [],
            "memory": memorylib.default_memory_state(),
        }
        self._ensure_session_shape()
        self.memory = memorylib.LayeredMemory(
            self.session.setdefault("memory", memorylib.default_memory_state()),
            workspace_root = self.root
        )
        self.session["memory"] = self.memory.to_dict()
        self.tools = self.build_tools()
        self.prefix_state = self.build_prefix()
        self.prefix = self.prefix_state.text
        self.context_manager = ContextManager(self)
        self.resume_state = self.evaluate_resume_state()
        self.session_path = self.session_store.save(self.session)
        self.parent_run_id = ""
        self.parent_tool_event_index = -1
        self.agent_rope = ""
        self.current_task_state = None
        self.current_run_dir = None
        self.last_prompt_metadata = {}
        self.last_completion_metadata = {}
        self.last_durable_promotions = []
        self.last_durable_rejections = []
        self.last_durable_superseded = []
        self._last_tool_result_metadata = {}
        self._last_prefix_refresh = {
            "workspace_changed": False,
            "prefix_changed": False,
        }
        # run 级共享字段，会在 emit_trace 时自动挂进每个 event 的 inherited 段。
        # 这样 viewer 渲染单个 event 无需回头翻 task_state。
        self._run_inherited_seed = {
            "approval_policy": self.approval_policy,
            "read_only": bool(self.read_only),
            "sandbox": getattr(self.sandbox, "name", "none") or "none",
            "model": getattr(model_client, "model", "") or "",
            "provider": model_client.__class__.__name__,
            "session_id": (session or {}).get("id", "") if session else "",
        }
        self.tracer = Tracer()
        if self.trace:
            self.trace_span_processor = TraceSpanProcessor(self.trace)
            self.tracer.register_processor(self.trace_span_processor)

    @classmethod
    def from_session(cls, model_client, workspace, session_store, session_id, **kwargs):
        """ 从已有会话ID恢复一个 Codini 实例 用于 --resume 场景"""
        return cls(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            session=session_store.load(session_id),
            **kwargs,
        )

    def _ensure_session_shape(self):
        """ 确保会话 Session 拥有所有必要的顶层字段 history/memory/checkpoint 等"""
        self.session.setdefault("history",[])
        self.session.setdefault("memory", memorylib.default_memory_state())
        checkpoints = self.session.setdefault("checkpoints", [])
        if not isinstance(checkpoints, dict):
            checkpoints = {}
            self.session["checkpoints"] = checkpoints
        checkpoints.setdefault("current_id", "")
        checkpoints.setdefault("items",{})

        runtime_identity = self.session.setdefault("runtime_identity", {})
        if not isinstance(runtime_identity, dict):
            self.session["runtime_identity"] = {}

        resume_state = self.session.setdefault("resume_state", {})
        if not isinstance(resume_state, dict):
            self.session["resume_state"] = {}

    def current_runtime_identity(self):
        """ 返回当前运行时的”身份“快照 用在 checkpoint 中检测环境是否发生变化"""
        return {
            "session_id": self.session.get("id", ""),
            "cwd": str(self.root),
            "model": str(getattr(self.model_client, "model", "")),
            "model_client": self.model_client.__class__.__name__,
            "approval_policy": self.approval_policy,
            "read_only": bool(self.read_only),
            "max_steps": int(self.max_steps),
            "max_new_tokens": int(self.max_new_tokens),
            "feature_flags": dict(self.feature_flags),
            "shell_env_allowlist": list(self.shell_env_allowlist),
            "workspace_fingerprint": getattr(getattr(self, "prefix_state", None), "workspace_fingerprint",self.workspace.fingerprint()),
            "tool_signature": self.tool_signature(),
        }

    def checkpoint_state(self):
        self._ensure_session_shape()
        return self.session["checkpoints"]

    def current_checkpoint(self):
        state = self.checkpoint_state()
        checkpoint_id = str(state.get("current_id","")).strip()
        if not checkpoint_id:
            return None
        return state.get("items",{}).get(checkpoint_id)

    def invalidate_stale_memory(self):
        """
        对比文件内容的 hash 将磁盘上已经被修改过的文件的 file_summaries 标记为无效
        输出: 失效的文件路径集合
        """
        invalidated = self.memory.invalidate_stale_file_summaries()
        self.session["memory"] = self.memory.to_dict()
        return invalidated

    def evaluate_resume_state(self):
        """
        评估当前 checkpoint 是否依然有效 返回恢复状态摘要
        状态: no-checkpoint / full-valid / partial-stale / workspace-mismatch / schema-mismatch

        """
        previous_resume_state = dict(self.session.get("resume_state",{}) or {})
        invalidated = self.invalidate_stale_memory()
        checkpoint = self.current_checkpoint()
        status = CHECKPOINT_NONE_STATUS
        stale_paths = list(invalidated)
        mismatch_fields = []
        if checkpoint:
            if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
                status = CHECKPOINT_SCHEMA_MISMATCH_STATUS
            else:
                for item in checkpoint.get("key_files", []):
                    path = str(item.get("path", "")).strip()
                    if not path:
                        continue
                    expected = item.get("freshness")
                    current = memorylib.file_freshness(path, self.root)
                    if expected != current and path not in stale_paths:
                        stale_paths.append(path)
                saved_identity = dict(checkpoint.get("runtime_identity", {}))
                current_identity = self.current_runtime_identity()
                identity_keys = (
                    "cwd",
                    "model",
                    "model_client",
                    "approval_policy",
                    "read_only",
                    "max_steps",
                    "max_new_tokens",
                    "feature_flags",
                    "shell_env_allowlist",
                    "workspace_fingerprint",
                    "tool_signature",
                )
                for key in identity_keys:
                    if key not in saved_identity:
                        continue
                    if saved_identity.get(key) != current_identity.get(key):
                        mismatch_fields.append(key)
                mismatch_fields.sort()
                if stale_paths:
                    status = CHECKPOINT_PARTIAL_STALE_STATUS
                elif mismatch_fields:
                    status = CHECKPOINT_WORKSPACE_MISMATCH_STATUS
                else:
                    status = CHECKPOINT_FULL_VALID_STATUS

        resume_state = {
            "status": status,
            "stale_paths": stale_paths,
            "runtime_identity_mismatch_fields": mismatch_fields,
            "stale_summary_invalidations": max(
                len(invalidated),
                int(previous_resume_state.get("stale_summary_invalidations", 0))
                if status == CHECKPOINT_PARTIAL_STALE_STATUS
                else 0,
            ),
        }
        self.session["resume_state"] = resume_state
        self.session["runtime_identity"] = self.current_runtime_identity()
        return resume_state


    def render_checkpoint_text(self):
        """
        将当前 checkpoint 的关键字段渲染成文字摘要 并追加到 Prompt Prefix 的末尾
        输出: 格式化后的 checkpoint 文字摘要
        """
        checkpoint = self.checkpoint_state()
        if not checkpoint:
            return ""
        lines = [
            "Task checkpoint:",
            f"- Resume status: {self.resume_state.get('status', CHECKPOINT_NONE_STATUS)}",
            f"- Current goal: {checkpoint.get('current_goal', '-') or '-'}",
            f"- Current blocker: {checkpoint.get('current_blocker', '-') or '-'}",
            f"- Next step: {checkpoint.get('next_step', '-') or '-'}",
        ]
        key_files = [str(item.get("path","")).strip() for item in checkpoint.get("key_files",[]) if str(item.get("path", "")).strip()]
        lines.append(f"- Key files: {', '.join(key_files) or '-'}")

        if checkpoint.get("completed"):
            lines.append("- Completed: " + " | ".join(str(item) for item in checkpoint.get("completed", [])))
        if checkpoint.get("excluded"):
            lines.append("- Excluded: " + " | ".join(str(item) for item in checkpoint.get("excluded", [])))

        if self.resume_state.get("stale_paths"):
            lines.append("- Stale paths: " + ", ".join(self.resume_state["stale_paths"]))

        summary = str(checkpoint.get("summary", "")).strip()
        if summary:
            lines.append(f"- Summary: {summary}")
        return '\n'.join(lines)

    @staticmethod
    def remember(bucket, item, limit):
        """

        """
        if not item:
            return
        if item in bucket:
            bucket.remove(item)
        bucket.append(item)
        del bucket[:-limit]

    def build_tools(self):
        return toolkit.build_tool_registry(self)

    def _sandbox_notes(self):
        notes = {
            "none": "The workspace directory is writable. You can read and write files directly.",
            "bubblewrap": "The workspace directory is mounted into the sandbox. Files you create or modify in the sandbox are visible on the host. System directories like /usr and /etc are read-only. Network access is blocked by default.",
        }
        return notes.get(self.sandbox.name, "")

    def tool_signature(self):
        """ 计算当前工具的唯一签名 用于在 checkpoint 中检测工具变化 """
        payload = []
        for name in sorted(self.tools):
            tool = self.tools[name]
            payload.append(
                {
                    "name": name,
                    "schema": tool["schema"],
                    "risky": tool["risky"],
                    "description": tool["description"],
                }
            )
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


    def build_prefix(self):
        """
        构建并返回 Agent 的“工作手册”: PromptPrefix 对象
        内容包括: 系统指令、工具列表、使用样例和工作区快照
        """
        tool_lines = []
        for name, tool in self.tools.items():
            fields = ", ".join(f"{key}: {value}" for key, value in tool["schema"].items())
            risk = "approval required" if tool["risky"] else "safe"
            tool_lines.append(f"- {name}({fields}) [{risk}] {tool['description']}")
        tool_text = "\n".join(tool_lines)
        examples = "\n".join(
            [
                '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
                '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
                '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
                '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
                '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
                "<final>Done.</final>",
            ]
        )
        # 提示词
        model_name = getattr(self.model_client, "model", "unknown")
        text = textwrap.dedent(
            """\
            You are Codini, a versatile local agent and master of problem-solving, inspired by the legendary Houdini.
            Powered by {model_name}, you navigate complex constraints, escape bottlenecks, and unlock elegant solutions in this workspace.
            Much like Houdini resolving the most challenging constraints with absolute precision and ingenuity, you rely on tools to analyze your environment and accomplish your goals.

            Rules:
            - Use your toolbelt to inspect the workspace instead of guessing. Just like Houdini relied on precise tools to resolve any lock, you must examine the facts to guide your decisions.
            - Return exactly one <tool>...</tool> or one <final>...</final>.
            - Tool calls must look like:
              <tool>{{"name":"tool_name","args":{{...}}}}</tool>
            - For write_file and patch_file with multi-line text, prefer XML style:
              <tool name="write_file" path="file.py"><content>...</content></tool>
            - Final answers must look like:
              <final>your answer</final>
            - Never invent tool results or assume facts without verifying them.
            - Keep answers concise and concrete.
            - If the user asks you to create or update a specific file and the path is clear, use write_file or patch_file instead of repeatedly listing files.
            - Before writing tests for existing code, read the implementation first.
            - When writing tests, match the current implementation unless the user explicitly asked you to change the code.
            - New files should be complete and runnable, including obvious imports.
            - Do not repeat the same tool call with the same arguments if it did not help. Choose a different tool or return a final answer.
            - When using delegate, pass the task directly without pre-reading files yourself first — the sub-agent has the same tools and will read what it needs.
            - Required tool arguments must not be empty. Do not call read_file, write_file, patch_file, run_shell, or delegate with args={{}}.
            - If the user asks you to remember, save, or store a fact/decision/preference/convention, you must format it on a new line in your final answer using one of these formats to ensure it is persisted:
                - Decision: <content> (or 决策：<content>),
                - Preference: <content> (or 偏好：<content>),
                - Project convention: <content> (or 项目约定：<content>),
                - Dependency: <content> (or 依赖：<content>).

            Sandbox: shell commands run inside a {sandbox_name} sandbox.
            {sandbox_notes}

            Tools:
            {tool_text}

            Valid response examples:
            {examples}

            {workspace_text}
            """
        ).format(
            model_name=model_name,
            tool_text=tool_text,
            examples=examples,
            workspace_text=self.workspace.text(),
            sandbox_name=self.sandbox.name,
            sandbox_notes=self._sandbox_notes(),
        ).strip()

        return PromptPrefix(
            text = text,
            hash = hashlib.sha256(text.encode("utf-8")).hexdigest(),
            workspace_fingerprint = self.workspace.fingerprint(),
            tool_signature = self.tool_signature(),
            built_at = now()
        )

    def _apply_prefix_state(self, prefix_state):
        self.prefix_state = prefix_state
        self.prefix = prefix_state.text


    def refresh_prefix(self, force = False):
        previous_hash = getattr(getattr(self, "prefix_state", None), "hash", None)
        previous_workspace_fingerprint = getattr(getattr(self, "prefix_state", None), "workspace_fingerprint", None)

        # 工作区事实相对稳定，所以这里按整体刷新；只有这些事实真的变化了，才重建完整 prefix。
        refreshed_workspace = WorkspaceContext.build(self.root)
        refreshed_workspace_fingerprint = refreshed_workspace.fingerprint()
        workspace_changed = force or refreshed_workspace_fingerprint != previous_workspace_fingerprint
        if workspace_changed:
            self.workspace = refreshed_workspace

        prefix_state = self.build_prefix() if workspace_changed or force or previous_hash is None else self.prefix_state
        prefix_changed = force or previous_hash != prefix_state.hash
        if prefix_changed:
            self._apply_prefix_state(prefix_state)
        self._last_prefix_refresh = {
            "workspace_changed": workspace_changed,
            "prefix_changed": prefix_changed
        }
        return dict(self._last_prefix_refresh)

    def memory_text(self):
        return self.memory.render_memory_text()


    def history_text(self):
        history = self.session["history"]
        if not history:
            return "- empty"

        lines = []
        seen_reads = set()
        recent_start = max(0, len(history)-6)
        for index, item in enumerate(history):
            recent = index >= recent_start
            if item["role"] == "tool" and item["name"] == "read_file" and not recent:
                path = str(item["args"].get("path", ""))
                if path in seen_reads:
                    continue
                seen_reads.add(path)

            if item["role"] == "tool":
                limit = 10000 if recent else 180
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(clip(item["content"], limit))
            else:
                limit = 10000 if recent else 220
                if item.get("role") == "assistant" and item.get("status") == "failed":
                    lines.append(self.render_error_history_item(item, limit))
                else:
                    lines.append(f"[{item['role']}] {clip(item['content'], limit)}")

        return clip("\n".join(lines), MAX_HISTORY)

    def render_error_history_item(self, item, limit):
        error = item.get("error") if isinstance(item.get("error"), dict) else {}
        parts = [
            "[assistant:error]",
            f"type={error.get('error_type') or item.get('stop_reason') or 'runtime_error'}",
        ]
        if error.get("http_status") is not None:
            parts.append(f"http_status={error.get('http_status')}")
        if error.get("error_code"):
            parts.append(f"code={error.get('error_code')}")
        if error.get("retryable") is not None:
            parts.append(f"retryable={str(bool(error.get('retryable'))).lower()}")
        message = error.get("message") or item.get("content") or ""
        parts.append(f"message={clip(str(message), limit)}")
        return " ".join(parts)

    def feature_enabled(self, name):
        return bool(self.feature_flags.get(str(name),False))

    def prompt(self, user_message):
        prompt, _ = self._build_prompt_and_metadata(user_message)
        return prompt

    def record(self, item):
        self.session["history"].append(item)
        self.session_path = self.session_store.save(self.session)

    def build_error_info(self, error, stop_reason=""):
        message = str(error)
        payload = _extract_error_payload(message)
        http_match = re.search(r"\bHTTP\s+(\d{3})\b", message, re.I)
        http_status = int(http_match.group(1)) if http_match else None
        message_lower = message.lower()
        provider = str(getattr(self.model_client, "provider", "") or "")
        if not provider:
            if message.startswith("OpenAI-compatible"):
                provider = "OpenAI-compatible"
            elif message.startswith("Siliconflow"):
                provider = "Siliconflow"
            elif message.startswith("Ollama"):
                provider = "Ollama"
            else:
                provider = self.model_client.__class__.__name__
        error_code = str(payload.get("code") or "").strip()
        provider_error_type = str(payload.get("type") or "").strip()
        provider_message = str(payload.get("message") or "").strip()
        retryable = (
            http_status in {408, 409, 425, 429, 500, 502, 503, 504}
            or "timeout" in message_lower
            or "overloaded" in message_lower
            or "service_unavailable" in message_lower
            or "rate_limit" in message_lower
        )
        if http_status:
            error_type = "provider_http_error"
        elif "timeout" in message_lower:
            error_type = "provider_timeout"
        elif provider_error_type:
            error_type = provider_error_type
        else:
            error_type = stop_reason or "runtime_error"
        return {
            "error_type": error_type,
            "error_code": error_code,
            "provider_error_type": provider_error_type,
            "provider": provider,
            "http_status": http_status,
            "retryable": bool(retryable),
            "message": provider_message or message,
            "raw_message": message,
        }

    def record_error_response(self, task_state, error, error_info=None):
        message = str(error)
        run_id = getattr(task_state, "run_id", "")
        error_info = dict(error_info or self.build_error_info(error, getattr(task_state, "stop_reason", "")))
        history = self.session.setdefault("history", [])
        if history:
            last = history[-1]
            if (
                isinstance(last, dict)
                and last.get("role") == "assistant"
                and last.get("status") == "failed"
                and last.get("run_id") == run_id
            ):
                return
        self.record(
            {
                "role": "assistant",
                "content": message,
                "created_at": now(),
                "status": getattr(task_state, "status", "") or "failed",
                "stop_reason": getattr(task_state, "stop_reason", "") or "runtime_error",
                "run_id": run_id,
                "trace_id": run_id,
                "task_id": getattr(task_state, "task_id", ""),
                "error": error_info,
            }
        )

    @staticmethod
    def looks_sensitive_env_name(name):
        """ 根据命名规律判断一个环境变量名是否看起来像敏感信息（如 API_KEY、TOKEN）"""
        upper = str(name).upper()
        return any(upper == marker or upper.endswith(marker) or upper.endswith(f"_{marker}") for marker in SENSITIVE_ENV_NAME_MARKERS)

    def is_secret_env_name(self, name):
        upper = str(name).upper()
        return upper in self.secret_env_names or self.looks_sensitive_env_name(upper)

    def configured_secret_env_items(self):
        items = [
            (name, value)
            for name, value in os.environ.items()
            if str(name).upper() in self.secret_env_names and value
        ]
        items.sort(key=lambda item: item[0])
        return items

    def detected_secret_env_items(self):
        items = [
            (name, value)
            for name, value in os.environ.items()
            if self.is_secret_env_name(name) and value
        ]
        items.sort(key=lambda item: item[0])
        return items

    def secret_env_summary(self):
        names = [name for name, _ in self.configured_secret_env_items()]
        return {
            "secret_env_count": len(names),
            "secret_env_names": names,
        }

    def detected_secret_env_summary(self):
        names = [name for name, _ in self.detected_secret_env_items()]
        return {
            "secret_env_count": len(names),
            "secret_env_names": names,
        }

    def redact_text(self, text):
        """  将文字中出现的敏感信息（模式与特定值）实际值替换为 "<redacted>" """
        text = str(text)

        # 1. Regex 模式扫描（防范未注册环境变量的 API Key 泄露）
        high_confidence_patterns = [
            re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
            re.compile(r"\b(ghp|xoxb|xoxp|live|test|sk_live|sk_test)_[A-Za-z0-9]{20,}\b"),
        ]
        for pattern in high_confidence_patterns:
            text = pattern.sub(REDACTED_VALUE, text)

        # 2. 保护性值脱敏：敏感值长度必须大于 4，才全局替换，防止误杀短单词/数字
        for _, value in sorted(self.detected_secret_env_items(), key=lambda item: len(item[1]), reverse=True):
            if len(str(value)) > 4:
                text = text.replace(value, REDACTED_VALUE)
        return text

    def redact_artifact(self, value, key = None):
        # 3. 敏感键名字典层级精准替换。这里只处理真正的凭证字段，不要误伤 token 统计类指标。
        is_sensitive_key = False
        if key:
            key_lower = str(key).lower()
            sensitive_exact_keys = {
                "api_key",
                "apikey",
                "access_token",
                "refresh_token",
                "auth_token",
                "authorization",
                "password",
                "secret",
                "token",
            }
            sensitive_suffixes = (
                "_api_key",
                "_apikey",
                "_password",
                "_secret",
                "_token",
            )
            if self.is_secret_env_name(key) or key_lower in sensitive_exact_keys or key_lower.endswith(sensitive_suffixes):
                is_sensitive_key = True

        if is_sensitive_key:
            return REDACTED_VALUE

        if isinstance(value, dict):
            return {
                str(item_key): self.redact_artifact(item_value, key=item_key)
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            return [self.redact_artifact(item, key=key) for item in value]
        if isinstance(value, tuple):
            return [self.redact_artifact(item, key=key) for item in value]
        if isinstance(value, str):
            redacted = self.redact_text(value)
            return redacted
        return value

    def shell_env(self):
        env = {
            name: os.environ[name]
            for name in self.shell_env_allowlist
            if name in os.environ
        }
        env["PWD"] = str(self.root)
        if "PATH" not in env and os.environ.get("PATH"):
            env["PATH"] = os.environ["PATH"]
        return env

    def _build_prompt_and_metadata(self, user_message):
        refresh = self.refresh_prefix()
        self.resume_state = self.evaluate_resume_state()
        prompt, metadata = self.context_manager.build(user_message)
        prompt_cache_supported = bool(getattr(self.model_client, "supports_prompt_cache", False))
        prompt_cache_key = self.prefix_state.hash if prompt_cache_supported else None
        metadata.update(
            {
                "prefix_chars": len(self.prefix),
                "workspace_chars": len(self.workspace.text()),
                "memory_chars": len(self.memory_text()),
                "history_chars": len(self.history_text()),
                "request_chars": len(user_message),
                "tool_count": len(self.tools),
                "workspace_docs": len(self.workspace.project_docs),
                "recent_commits": len(self.workspace.recent_commits),
                "prefix_hash": self.prefix_state.hash,
                "prompt_cache_key": prompt_cache_key,
                "prompt_cache_requested": bool(prompt_cache_key),
                "prompt_cache_retention": "in_memory" if prompt_cache_key else None,
                "workspace_fingerprint": self.prefix_state.workspace_fingerprint,
                "tool_signature": self.prefix_state.tool_signature,
                "workspace_changed": refresh["workspace_changed"],
                "prefix_changed": refresh["prefix_changed"],
                "prompt_cache_supported": prompt_cache_supported,
                "resume_status": self.resume_state.get("status", CHECKPOINT_NONE_STATUS),
                "stale_summary_invalidations": int(self.resume_state.get("stale_summary_invalidations", 0)),
                "stale_paths": list(self.resume_state.get("stale_paths", [])),
                "runtime_identity_mismatch_fields": list(self.resume_state.get("runtime_identity_mismatch_fields", [])),
            }
        )
        metadata.update(self.detected_secret_env_summary())
        return prompt, metadata

    def _complete_with_heartbeat(self, prompt, prompt_cache_key, prompt_cache_retention):
        model_started_at = time.monotonic()
        try:
            raw = self.model_client.complete(
                prompt,
                self.max_new_tokens,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
            )
        except Exception as e:
            err_msg = str(e)
            if "read operation timed out" in err_msg or "timeout" in err_msg.lower():
                err_msg = f"LLM API request timed out (network read operation timed out). The model provider is likely overloaded: {e}"
            raise RuntimeError(err_msg) from e
        model_duration_ms = int((time.monotonic() - model_started_at) * 1000)
        return raw

    def _accum_model(self, completion_metadata, duration_ms=0):
        """把后端的 usage / cache 字段累加到当前 run 的累加器。"""
        accum = getattr(self, "_run_accum", None)
        if accum is None:
            self._run_accum = {
                "tokens": {"prompt": 0, "completion": 0, "total": 0, "cached": 0},
                "latency": {"model_ms": 0, "tool_ms": 0, "count_model": 0, "count_tool": 0},
                "tools": {},
            }
            accum = self._run_accum
        usage = _extract_run_usage_fields(completion_metadata)
        tokens = accum["tokens"]
        tokens["prompt"] += usage["prompt_tokens"]
        tokens["completion"] += usage["completion_tokens"]
        tokens["total"] += usage["total_tokens"]
        tokens["cached"] += usage["cached_tokens"]
        latency = accum["latency"]
        latency["model_ms"] += int(duration_ms or 0)
        latency["count_model"] += 1

    def _accum_tool(self, tool_name, duration_ms=0):
        """把一次工具执行累加进当前 run 的累加器。"""
        accum = getattr(self, "_run_accum", None)
        if accum is None:
            self._run_accum = {
                "tokens": {"prompt": 0, "completion": 0, "total": 0, "cached": 0},
                "latency": {"model_ms": 0, "tool_ms": 0, "count_model": 0, "count_tool": 0},
                "tools": {},
            }
            accum = self._run_accum
        accum["tools"][tool_name] = accum["tools"].get(tool_name, 0) + 1
        accum["latency"]["tool_ms"] += int(duration_ms or 0)
        accum["latency"]["count_tool"] += 1

    def record_run_summary(self, task_state, write_task_state=True, trigger="summary_updated", related_span_id="", related_event=""):
        """根据运行中的累加器，算一份 viewer 可渲染的聚合小计。

        聚合小计包括：
        - tokens: prompt/completion/total/cached
        - latency: model_ms/tool_ms 以及各自的 count
        - tools: 各工具执行次数 top 列表
        然后重写到 task_state.json（viewer 真空读取）+ 更新 runs/index.jsonl。
        """
        accum = getattr(self, "_run_accum", None) or {}
        tokens = accum.get("tokens") or {"prompt": 0, "completion": 0, "total": 0, "cached": 0}
        latency = accum.get("latency") or {"model_ms": 0, "tool_ms": 0, "count_model": 0, "count_tool": 0}
        tools = accum.get("tools") or {}
        # 兜底：如果累加器没被初始化（比如某些异常路径），回退到扫 history。
        if not tools:
            for item in self.session["history"]:
                if item.get("role") != "tool":
                    continue
                name = item.get("name") or ""
                tools[name] = tools.get(name, 0) + 1

        summary = {
            "tokens": tokens,
            "latency": latency,
            "tools": sorted(tools.items(), key=lambda kv: kv[1], reverse=True),
            "attempts": int(task_state.attempts or 0),
            "tool_steps": int(task_state.tool_steps or 0),
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
        }
        task_state.summary = summary
        self.session.setdefault("_run_summaries", {})[task_state.run_id] = summary
        try:
            self.run_store.record_run_summary(
                task_state,
                summary,
                write_task_state=write_task_state,
                trigger=trigger,
                related_span_id=related_span_id,
                related_event=related_event,
            )
        except Exception:
            # 聚合小计只是 viewer 的锦上添花；写坏了也不应该阻塞主流程。
            pass
        return summary

    def capture_workspace_snapshot(self):
        snapshot = {}
        for path in self.root.rglob("*"):
            try:
                relative_parts = path.relative_to(self.root).parts
            except ValueError:
                continue
            if any(part in IGNORED_PATH_NAMES for part in relative_parts):
                continue
            if not path.is_file():
                continue
            try:
                snapshot[path.relative_to(self.root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            except Exception:
                continue
        return snapshot

    @staticmethod
    def diff_workspace_snapshots(before, after):
        """ 比较两个工作区快照 找出发生变化的文件列表 """
        changed_paths = []
        summaries = []
        all_paths = sorted(set(before) | set(after))
        for path in all_paths:
            if before.get(path) == after.get(path):
                continue
            changed_paths.append(path)
            if path not in before:
                summaries.append(f"created: {path}")
            elif path not in after:
                summaries.append(f"deleted: {path}")
            else:
                summaries.append(f"modified: {path}")
        return changed_paths, summaries

    def _generate_diffs(self, affected_paths, before_snapshot, after_snapshot, rel_target_path, before_content):
        diffs = []
        for path in affected_paths:
            p = self.root / path
            diff_text = ""
            diff_type = "modified"
            if path not in before_snapshot:
                diff_type = "created"
            elif path not in after_snapshot:
                diff_type = "deleted"

            try:
                if diff_type == "deleted":
                    if rel_target_path == path and before_content is not None:
                        diff_lines = list(difflib.unified_diff(
                            before_content.splitlines(keepends=True),
                            [],
                            fromfile=f"a/{path}",
                            tofile="/dev/null"
                        ))
                        diff_text = "".join(diff_lines)
                elif diff_type == "created":
                    if p.is_file():
                        after_content = p.read_text(encoding="utf-8", errors="replace")
                        diff_lines = list(difflib.unified_diff(
                            [],
                            after_content.splitlines(keepends=True),
                            fromfile="/dev/null",
                            tofile=f"b/{path}"
                        ))
                        diff_text = "".join(diff_lines)
                else: # modified
                    git_diff_success = False
                    if shutil.which("git") and (self.root / ".git").is_dir():
                        try:
                            git_result = subprocess.run(
                                ["git", "diff", "--", str(p)],
                                cwd=self.root,
                                capture_output=True,
                                text=True,
                                encoding="utf-8",
                                errors="replace",
                                timeout=5
                            )
                            if git_result.returncode == 0 and git_result.stdout.strip():
                                diff_text = git_result.stdout
                                git_diff_success = True
                        except Exception:
                            pass

                    if not git_diff_success:
                        if rel_target_path == path and before_content is not None:
                            if p.is_file():
                                after_content = p.read_text(encoding="utf-8", errors="replace")
                                diff_lines = list(difflib.unified_diff(
                                    before_content.splitlines(keepends=True),
                                    after_content.splitlines(keepends=True),
                                    fromfile=f"a/{path}",
                                    tofile=f"b/{path}"
                                ))
                                diff_text = "".join(diff_lines)
            except Exception as diff_exc:
                diff_text = f"Error generating diff: {diff_exc}"

            diffs.append({
                "path": path,
                "type": diff_type,
                "diff_text": diff_text
            })
        return diffs

    def create_checkpoint(self, task_state, user_message, trigger):
        """ 创建并保存一个任务检查点 记录当前目标、关键文件、下一步计划等信息"""
        state = self.checkpoint_state()
        current = self.current_checkpoint()
        checkpoint_id = "ckpt_" + uuid.uuid4().hex[:8]
        key_files = []
        freshness = {}
        for path in self.memory.to_dict()["working"]["recent_files"]:
            file_freshness = memorylib.file_freshness(path, self.root)
            freshness[path] = file_freshness
            key_files.append({"path": path, "freshness": file_freshness})
        checkpoint = {
            "checkpoint_id": checkpoint_id,
            "parent_checkpoint_id": current.get("checkpoint_id", "") if current else "",
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "created_at": now(),
            "current_goal": str(user_message),
            "completed": [task_state.final_answer] if task_state.final_answer else [],
            "excluded": [],
            "current_blocker": "" if str(task_state.stop_reason or "") in ("", "final_answer_returned") else str(task_state.stop_reason),
            "next_step": self.infer_next_step(task_state),
            "key_files": key_files,
            "freshness": freshness,
            "summary": f"{trigger}: {clip(str(user_message), 120)}",
            "runtime_identity": self.current_runtime_identity()
        }
        state["items"][checkpoint_id] = checkpoint
        state["current_id"] = checkpoint_id
        task_state.checkpoint_id = checkpoint_id
        self.session["runtime_identity"] = checkpoint["runtime_identity"]
        self.session_path = self.session_store.save(self.session)
        return checkpoint

    def infer_next_step(self, task_state):
        if task_state.status == "completed":
            return "No next step recorded."
        if task_state.stop_reason == "step_limit_reached":
            return "Resume from the latest checkpoint and continue the task."
        if task_state.last_tool:
            return f"Decide the next action after {task_state.last_tool}."
        return "Continue the task from the latest checkpoint."

    def update_memory_after_tool(self, name, args, result):
        """
        把少量高价值工具结果沉淀到 working memory
        发生在 `run_tool()` 真正执行完工具之后、下一轮 prompt 组装之前
        也就是说：工具结果先进入完整历史 再由这个函数择优沉淀成轻量记忆
        """
        if not self.feature_enabled("memory"):
            return
        path = args.get("path")
        if not path:
            return

        canonical_path = self.memory.canonical_path(path)
        if name in {"read_file", "write_file", "patch_file"}:
            self.memory.remember_file(canonical_path)
        if name == "read_file":
            summary = memorylib.summarize_read_result(result)
            self.memory.set_file_summary(canonical_path, summary)
            self.memory.append_note(summary, tags=(canonical_path,), source=canonical_path)
        elif name in {"write_file", "patch_file"}:
            self.memory.invalidate_file_summary(canonical_path)

    def note_tool(self, name, args, result):
        self.update_memory_after_tool(name, args,result)


    def record_process_note_for_tool(self, name, metadata):
        status = str(metadata.get("tool_results","")).strip()
        if status not in {"partial_success", "error", "rejected"}:
            return
        affected_paths = [str(path).strip() for path in metadata.get("affected_paths", []) if str(path).strip()]
        path_text = ", ".join(affected_paths) or "workspace"
        if status == "partial_success":
            text = f"{name} partial_success on {path_text}; inspect diff before retry"
        elif status == "error":
            text = f"{name} error on {path_text}; check the failure before retry"
        else:
            text = f"{name} rejected; choose a different action before retry"
        tags = ["process", status, *affected_paths]
        self.memory.append_note(text, tags=tuple(tags), source=name, kind="process")
        self.session["memory"] = self.memory.to_dict()

    def reject_durable_reason(self, note_text):
        text = str(note_text or "").strip()
        lowered = text.lower()
        if not text:
            return "empty"
        if REDACTED_VALUE in text or SECRET_SHAPED_TEXT_PATTERN.search(text):
            return "secret_shaped"
        checkpoint_like_prefixes = (
            "current goal",
            "current blocker",
            "next step",
            "current phase",
            "key files",
            "freshness",
            "当前目标",
            "当前卡点",
            "下一步",
            "当前阶段",
            "关键文件",
            "已完成",
            "已排除",
        )
        if any(lowered.startswith(prefix) for prefix in checkpoint_like_prefixes):
            return "transient_task_state"
        if re.search(r"(?i)\b(stdout|stderr|traceback|exit_code)\b", text) or len(text) > 220:
            return "noisy_output"
        return ""

    def extract_durable_promotions(self, user_message, final_answer):
        user_text = str(user_message or "")
        if not (DURABLE_MEMORY_INTENT_PATTERN.search(user_text) or DURABLE_MEMORY_INTENT_ZH_PATTERN.search(user_text)):
            return [],[]
        promotions = []
        rejections = []
        for line in str(final_answer or "").splitlines():
            text = line.strip()
            if not text or REDACTED_VALUE in text:
                continue
            for topic, pattern in DURABLE_MEMORY_LINE_PATTERNS:
                match = pattern.match(text)
                if not match:
                    continue
                note_text = match.group(1).strip()
                if note_text:
                    reason = self.reject_durable_reason(note_text)
                    if reason:
                        rejections.append(f"{topic}:{reason}")
                        break
                    promotions.append((topic,note_text))
                break
        return promotions,rejections

    def promote_durable_memory(self, user_message, final_answer):
        promotions, rejections = self.extract_durable_promotions(user_message, final_answer)
        promoted, superseded = self.memory.promote_durable(promotions)
        self.session["memory"] = self.memory.to_dict()
        self.last_durable_promotions = promoted
        self.last_durable_rejections = rejections
        self.last_durable_superseded = superseded
        return promoted, rejections, superseded

    def ask(self, user_message):
        """
        CLI 收到用户输入后基本只做一件事：调用 `agent.ask()`
        `ask()` 内部去驱动 `ContextManager`组 prompt、
        `model_client.complete()` 调模型、
        `run_tool()` 执行动作
        """
        run_started_at = time.monotonic()
        self._trace_started_at = run_started_at
        self.memory.set_task_summary(user_message)
        self.record({"role": "user", "content": user_message, "created_at": now()})

        task_state = TaskState.create(
            run_id=self.new_run_id(),
            task_id=self.new_task_id(),
            user_request=user_message,
            parent_run_id=getattr(self, "parent_run_id", ""),
            parent_tool_event_index=getattr(self, "parent_tool_event_index", -1),
            parent_span_id=getattr(self, "parent_span_id", ""),
            agent_rope=getattr(self, "agent_rope", ""),
        )
        task_state.resume_status = self.resume_state.get("status", CHECKPOINT_NONE_STATUS)
        seed = getattr(self, "_run_inherited_seed", {}) or {}
        task_state.session_id = seed.get("session_id", "") or self.session.get("id", "")
        task_state.approval_policy = seed.get("approval_policy", "")
        task_state.read_only = bool(seed.get("read_only", False))
        task_state.sandbox = seed.get("sandbox", "none")
        task_state.model = seed.get("model", "")
        task_state.provider = seed.get("provider", "")
        task_state.depth = int(getattr(self, "depth", 0) or 0)
        task_state._run_inherited = dict(seed)
        task_state._run_inherited["depth"] = task_state.depth
        task_state._run_inherited["read_only"] = task_state.read_only
        task_state._run_inherited["agent_rope"] = task_state.agent_rope
        task_state._run_inherited["session_id"] = task_state.session_id
        task_state._run_inherited["parent_span_id"] = task_state.parent_span_id
        task_state._trace_started_at = run_started_at
        self.current_task_state = task_state
        self.current_run_dir = self.run_store.start_run(task_state)
        self._run_accum = {
            "tokens": {"prompt": 0, "completion": 0, "total": 0, "cached": 0},
            "latency": {
                "model_ms": 0,
                "tool_ms": 0,
                "count_model": 0,
                "count_tool": 0,
            },
            "tools": {},
        }

        self.span_exporter = FileSpanExporter(self)
        self.tracer.register_processor(self.span_exporter)

        run_span = self.tracer.start_span("agent.ask", {
            "trace_id": task_state.run_id,
            "parent_span_id": task_state.parent_span_id,
            "depth": task_state.depth,
            "task_id": task_state.task_id,
            "user_request": user_message
        })
        run_span_token = self.tracer._active_span_var.set(run_span)

        tool_steps = 0
        attempts = 0
        max_attempts = max(self.max_steps * 3, self.max_steps + 4)
        pending_error = None

        try:
            # Agent 运行时的主循环
            # 1. 感知：重组 Prompt，把当前状态整理给模型看
            # 2. 决策：让模型返回一个工具调用，或一个最终答案
            # 3. 行动：如果是工具调用，就执行工具
            # 4. 记录：把结果写回 history / task_state / trace / memory
            # 然后进入下一轮，直到停机条件满足
            while tool_steps < self.max_steps and attempts < max_attempts:
                attempts += 1
                task_state.record_attempt()
                self.run_store.write_task_state(
                    task_state,
                    trigger="attempt_started",
                    related_span_id=run_span.span_id,
                    related_event="model_requested",
                )
                prompt_started_at = time.monotonic()
                prompt, prompt_metadata = self._build_prompt_and_metadata(user_message)
                redacted_prompt = self.redact_artifact(prompt)
                redacted_prompt_without_current_request = self.redact_artifact(
                    prompt_metadata.get("prompt_without_current_request", "")
                )
                prompt_metadata_for_trace = dict(prompt_metadata)
                prompt_metadata_for_trace.pop("prompt_without_current_request", None)
                run_span.add_event(
                    "prompt_built",
                    {
                        "prompt_metadata": prompt_metadata_for_trace,
                        "prompt": redacted_prompt,
                        "prompt_without_current_request": redacted_prompt_without_current_request,
                        "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                    }
                )
                if prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
                    checkpoint = self.create_checkpoint(task_state, user_message, trigger="freshness_mismatch")
                    self.run_store.write_task_state(
                        task_state,
                        trigger="freshness_mismatch",
                        related_span_id=run_span.span_id,
                        related_event="checkpoint_created",
                    )
                    run_span.add_event(
                        "checkpoint_created",
                        {
                            "checkpoint_id": checkpoint["checkpoint_id"],
                            "trigger": "freshness_mismatch",
                        }
                    )
                elif prompt_metadata.get("resume_status") == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
                    run_span.add_event(
                        "runtime_identity_mismatch",
                        {
                            "fields": list(prompt_metadata.get("runtime_identity_mismatch_fields", [])),
                        },
                    )
                    checkpoint = self.create_checkpoint(task_state, user_message, trigger="workspace_mismatch")
                    self.run_store.write_task_state(
                        task_state,
                        trigger="workspace_mismatch",
                        related_span_id=run_span.span_id,
                        related_event="checkpoint_created",
                    )
                    run_span.add_event(
                        "checkpoint_created",
                        {
                            "checkpoint_id": checkpoint["checkpoint_id"],
                            "trigger": "workspace_mismatch",
                        },
                    )

                if prompt_metadata.get("budget_reductions"):
                    checkpoint = self.create_checkpoint(task_state, user_message, trigger="context_reduction")
                    self.run_store.write_task_state(
                        task_state,
                        trigger="context_reduction",
                        related_span_id=run_span.span_id,
                        related_event="checkpoint_created",
                    )
                    run_span.add_event(
                        "checkpoint_created",
                        {
                            "checkpoint_id": checkpoint["checkpoint_id"],
                            "trigger": "context_reduction",
                        },
                    )
                prompt_cache_key = None
                prompt_cache_retention = None
                if getattr(self.model_client, "supports_prompt_cache", False):
                    prompt_cache_key = prompt_metadata.get("prompt_cache_key")
                    prompt_cache_retention = "in_memory"
                prompt_metadata["prompt_cache_key"] = prompt_cache_key
                prompt_metadata["prompt_cache_requested"] = bool(prompt_cache_key)
                prompt_metadata["prompt_cache_retention"] = prompt_cache_retention
                self.last_prompt_metadata = {
                    key: value for key, value in prompt_metadata.items()
                    if key != "prompt_without_current_request"
                }
                self.last_completion_metadata = {}

                llm_attrs = {
                    "attempts": task_state.attempts,
                    "tool_steps": task_state.tool_steps,
                    "prompt_cache_supported": bool(getattr(self.model_client, "supports_prompt_cache", False)),
                    "prompt_cache_requested": bool(prompt_cache_key),
                    "prompt_cache_key": prompt_cache_key,
                    "prompt_cache_retention": prompt_cache_retention,
                    "prompt_chars": int(prompt_metadata.get("prompt_chars", len(prompt)) or len(prompt)),
                    "prompt": redacted_prompt,
                }

                with self.tracer.span_scope("llm.complete", llm_attrs) as llm_span:
                    model_started_at = time.monotonic()
                    raw = self._complete_with_heartbeat(prompt, prompt_cache_key, prompt_cache_retention)
                    model_duration_ms = int((time.monotonic() - model_started_at) * 1000)

                    completion_metadata = dict(getattr(self.model_client, "last_completion_metadata", {}) or {})
                    if completion_metadata:
                        for key, value in completion_metadata.items():
                            if value is not None:
                                prompt_metadata[key] = value
                        usage_fields = _extract_run_usage_fields(completion_metadata)
                        prompt_metadata["provider_cache_hit"] = usage_fields["cache_hit"]
                        prompt_metadata["provider_cached_tokens"] = usage_fields["cached_tokens"]
                    self.last_completion_metadata = completion_metadata
                    self.last_prompt_metadata = {
                        key: value for key, value in prompt_metadata.items()
                        if key != "prompt_without_current_request"
                    }
                    model_duration_ms = int((time.monotonic() - model_started_at) * 1000)
                    self._accum_model(completion_metadata, duration_ms=model_duration_ms)
                    kind, payload = self.parse(raw)

                    llm_span.set_attributes({
                        "kind": kind,
                        "raw": clip(raw, 600),
                        **_extract_run_usage_fields(completion_metadata),
                    })
                if kind == "tool":
                    tool_steps += 1
                    name = payload.get("name", "")
                    args = payload.get("args", {})
                    task_state.record_tool(name)
                    risky = bool(self.tools.get(name, {}).get("risky", False))

                    tool_attrs = {
                        "name": name,
                        "args": args,
                        "risky": risky,
                        "risk_level": "high" if risky else "low",
                    }

                    with self.tracer.span_scope(f"tool.{name or 'unknown'}", tool_attrs) as tool_span:
                        tool_started_at = time.monotonic()
                        self.current_tool_span_id = tool_span.span_id
                        try:
                            result = self.run_tool(name, args)
                        finally:
                            self.current_tool_span_id = ""
                        tool_duration_ms = int((time.monotonic() - tool_started_at) * 1000)

                        tool_status = (self._last_tool_result_metadata or {}).get("tool_status", "ok")
                        success = tool_status not in ("error", "rejected", "partial_success")

                        self._accum_tool(name, duration_ms=tool_duration_ms)
                        self.record(
                            {
                                "role": "tool",
                                "name": name,
                                "args": args,
                                "content": result,
                                "created_at": now(),
                            }
                        )
                        self.run_store.write_task_state(
                            task_state,
                            trigger="tool_executed",
                            related_span_id=tool_span.span_id,
                            related_event="tool_executed",
                        )
                        tool_result_meta = dict(self._last_tool_result_metadata or {})
                        exit_code = tool_result_meta.get("exit_code")
                        if exit_code is None and name == "run_shell":
                            match = re.search(r"exit_code:\s*(-?\d+)", result)
                            exit_code = int(match.group(1)) if match else 0
                        security_event = tool_result_meta.get("security_event_type") or ""
                        full_error = ""
                        if tool_result_meta.get("tool_status") in {"error", "partial_success", "rejected"}:
                            full_error = str(result or "")

                        tool_span.set_attributes({
                            "name": name,
                            "args": args,
                            "result": clip(result, 800),
                            "result_full": full_error or clip(result, 2000),
                            "full_error": full_error,
                            "exit_code": exit_code,
                            "security_event_type": security_event,
                            "risk_level": tool_result_meta.get("risk_level"),
                            "approval_policy": tool_result_meta.get("approval_policy"),
                            "approved": tool_result_meta.get("approved"),
                            "workspace_changed": tool_result_meta.get("workspace_changed"),
                            "affected_paths": tool_result_meta.get("affected_paths"),
                            "tool_status": tool_result_meta.get("tool_status"),
                            "tool_error_code": tool_result_meta.get("tool_error_code"),
                            "child_session_id": tool_result_meta.get("child_session_id"),
                            "child_run_id": tool_result_meta.get("child_run_id"),
                            "child_trace_id": tool_result_meta.get("child_trace_id"),
                            "diffs": tool_result_meta.get("diffs", []),
                            "success": success
                        })

                    checkpoint = self.create_checkpoint(task_state, user_message, trigger="tool_executed")
                    self.run_store.write_task_state(
                        task_state,
                        trigger="checkpoint_created",
                        related_span_id=run_span.span_id,
                        related_event="checkpoint_created",
                    )
                    run_span.add_event(
                        "checkpoint_created",
                        {
                            "checkpoint_id": checkpoint["checkpoint_id"],
                            "trigger": "tool_executed",
                        },
                    )
                    continue

                if kind == "retry":
                    self.record({"role": "assistant", "content": payload, "created_at": now()})
                    self.run_store.write_task_state(
                        task_state,
                        trigger="response_correction",
                        related_span_id=run_span.span_id,
                        related_event="response_correction",
                    )
                    run_span.add_event("response_correction", {"attempt": attempts})
                    continue
                final = (payload or raw).strip()
                self.record({"role": "assistant", "content": final, "created_at": now()})
                task_state.finish_success(final)
                self.promote_durable_memory(user_message, final)

                tools_viz = []
                tool_count = task_state.tool_steps
                for item in self.session["history"][-(tool_count + 1):]:
                    if item.get("role") == "tool":
                        tools_viz.append({
                            "name": item.get("name", ""),
                            "success": True,
                            "summary": "",
                            "duration_ms": 0,
                        })
                if tools_viz:
                    tools_viz[0]["duration_ms"] = self._run_accum["latency"]["tool_ms"]

                checkpoint = self.create_checkpoint(task_state, user_message, trigger="run_finished")
                self.record_run_summary(
                    task_state,
                    write_task_state=False,
                    trigger="run_finished",
                    related_span_id=run_span.span_id,
                    related_event="run_finished",
                )
                self.run_store.write_task_state(
                    task_state,
                    trigger="run_finished",
                    related_span_id=run_span.span_id,
                    related_event="run_finished",
                )
                run_span.add_event(
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "run_finished",
                    },
                )
                run_span.set_attributes({
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": final,
                    "promotions": list(self.last_durable_promotions),
                    "rejections": list(self.last_durable_rejections),
                    "tools_summary": tools_viz,
                    "prompt_metadata": self.last_prompt_metadata,
                    "completion_metadata": self.last_completion_metadata
                })
                self.run_store.write_report(
                    task_state,
                    self.redact_artifact(self.build_report(task_state)),
                    trigger="run_finished",
                    related_span_id=run_span.span_id,
                    related_event="run_finished",
                )
                run_span.finish()
                return final

            if attempts >= max_attempts and tool_steps < self.max_steps:
                final = "Stopped after too many malformed model responses without a valid tool call or final answer."
                task_state.stop_retry_limit(final)
            else:
                final = "Stopped after reaching the step limit without a final answer."
                task_state.stop_step_limit(final)

            self.record({"role": "assistant", "content": final, "created_at": now()})
            self.promote_durable_memory(user_message, final)
            self.record_run_summary(
                task_state,
                write_task_state=False,
                trigger=task_state.stop_reason or "run_stopped",
                related_span_id=run_span.span_id,
                related_event="run_finished",
            )
            self.run_store.write_task_state(
                task_state,
                trigger=task_state.stop_reason or "run_stopped",
                related_span_id=run_span.span_id,
                related_event="run_finished",
            )
            checkpoint = self.create_checkpoint(task_state, user_message, trigger=task_state.stop_reason or "run_stopped")
            run_span.add_event(
                "checkpoint_created",
                {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "trigger": task_state.stop_reason or "run_stopped",
                },
            )
            run_span.set_attributes({
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "promotions": list(self.last_durable_promotions),
                "rejections": list(self.last_durable_rejections),
                "tools_summary": [],
                "prompt_metadata": self.last_prompt_metadata,
                "completion_metadata": self.last_completion_metadata
            })
            self.run_store.write_report(
                task_state,
                self.redact_artifact(self.build_report(task_state)),
                trigger=task_state.stop_reason or "run_stopped",
                related_span_id=run_span.span_id,
                related_event="run_finished",
            )
            run_span.finish()
            return final
        except Exception as exc:
            pending_error = exc
            if getattr(task_state, "stop_reason", "") == "":
                task_state.stop_runtime_error(str(exc))
            error_info = self.build_error_info(exc, task_state.stop_reason)
            task_state.error = error_info
            task_state._error_info = error_info
            self.record_error_response(task_state, exc, error_info=error_info)
            run_span.set_attributes({
                "status": task_state.status or "failed",
                "stop_reason": task_state.stop_reason or "runtime_error",
                "final_answer": task_state.final_answer,
                "error": str(exc),
                "error_info": error_info,
                **error_info,
                "prompt_metadata": self.last_prompt_metadata,
                "completion_metadata": self.last_completion_metadata,
            })
            try:
                self.record_run_summary(
                    task_state,
                    write_task_state=False,
                    trigger=task_state.stop_reason or "runtime_error",
                    related_span_id=run_span.span_id,
                    related_event="run_finished",
                )
                self.run_store.write_task_state(
                    task_state,
                    trigger=task_state.stop_reason or "runtime_error",
                    related_span_id=run_span.span_id,
                    related_event="run_finished",
                )
                self.run_store.write_report(
                    task_state,
                    self.redact_artifact(self.build_report(task_state)),
                    trigger=task_state.stop_reason or "runtime_error",
                    related_span_id=run_span.span_id,
                    related_event="run_finished",
                )
            except Exception:
                pass
            raise
        finally:
            if run_span.end_time is None:
                run_span.finish(status="ERROR" if pending_error else "OK")
            if run_span_token is not None:
                self.tracer._active_span_var.reset(run_span_token)
            self.tracer.unregister_processor(self.span_exporter)
            self.span_exporter = None
            self.current_tool_span_id = ""

    def run_tool(self, name, args):
        """
        执行一次工具调用，并在执行前后套上完整护栏
        Harness 本质所在 限定工具的执行边界
        """
        # 工具执行不是“直接调函数”，而是一条带护栏的流水线：
        # 工具是否存在 -> 参数是否合法 -> 是否重复调用 -> 是否通过审批 -> 真正执行 -> 更新记忆
        tool = self.tools.get(name)
        if tool is None:
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "unknown_tool",
                "security_event_type": "",
                "risk_level": "high",
                "read_only": False,
                "affected_paths": [],
                "workspace_changed": False,
                "diff_summary": [],
            }
            return f"error: unknown tool '{name}'"
        try:
            self.validate_tool(name,args)
        except Exception as e:
            example = self.tool_example(name)
            message = f"error: invalid arguments for {name}: {e}"
            if example:
                message += f"\nexample: {example}"
            security_event_type = "path_escape" if "path escapes workspace" in str(e) else ""
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "invalid_arguments",
                "security_event_type": security_event_type,
                "risk_level": "high" if tool["risky"] else "low",
                "read_only": not tool["risky"],
                "affected_paths": [],
                "workspace_changed": False,
                "diff_summary": [],
            }
            return message

        if self.repeated_tool_call(name, args):
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "repeated_identical_call",
                "security_event_type": "",
                "risk_level": "high" if tool["risky"] else "low",
                "read_only": not tool["risky"],
                "affected_paths": [],
                "workspace_changed": False,
                "diff_summary": [],
            }
            return f"error: repeated identical tool call for {name}; choose a different tool or return a final answer"
        approved = True
        if tool["risky"]:
            approval_span = self.tracer.get_current_span() if hasattr(self, "tracer") else None
            if approval_span:
                approval_span.add_event(
                    "approval_requested",
                    {
                        "name": name,
                        "approval_policy": self.approval_policy,
                        "read_only": self.read_only,
                    },
                )
            approved = self.approve(name, args)
            if approval_span:
                approval_span.add_event(
                    "approval_result",
                    {
                        "name": name,
                        "approval_policy": self.approval_policy,
                        "approved": approved,
                        "denial_reason": "" if approved else ("read_only_block" if self.read_only else "approval_denied"),
                    },
                )
        if tool["risky"] and not approved:
            self._last_tool_result_metadata = {
                "tool_status": "rejected",
                "tool_error_code": "approval_denied",
                "security_event_type": "read_only_block" if self.read_only else "approval_denied",
                "risk_level": "high",
                "approval_policy": self.approval_policy,
                "approved": False,
                "read_only": self.read_only,
                "affected_paths": [],
                "workspace_changed": False,
                "diff_summary": [],
            }
            return f"error: approval denied for {name}"

        target_path = args.get("path") if name in {"write_file", "patch_file"} else None
        before_content = None
        rel_target_path = None
        if target_path:
            try:
                abs_target_path = self.path(target_path)
                if abs_target_path.is_file():
                    before_content = abs_target_path.read_text(encoding="utf-8", errors="replace")
                rel_target_path = abs_target_path.relative_to(self.root).as_posix()
            except Exception:
                pass

        before_snapshot = self.capture_workspace_snapshot() if tool["risky"] else {}
        after_snapshot = before_snapshot
        try:
            if name == "delegate":
                self._last_delegate_child_info = {}
            result = clip(tool["run"](args))
            after_snapshot = self.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            affected_paths, diff_summary = self.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            diffs = self._generate_diffs(affected_paths, before_snapshot, after_snapshot, rel_target_path, before_content)
            tool_status = "ok"
            tool_error_code = ""
            if name == "run_shell":
                match = re.search(r"exit_code:\s*(-?\d+)", result)
                exit_code = int(match.group(1)) if match else 0
                if exit_code != 0 and workspace_changed:
                    tool_status = "partial_success"
                    tool_error_code = "tool_partial_success"
                elif exit_code != 0:
                    tool_status = "error"
                    tool_error_code = "tool_failed"
            self.update_memory_after_tool(name, args, result)
            child_info = {}
            if name == "delegate":
                child_info = dict(getattr(self, "_last_delegate_child_info", {}) or {})
                self._last_delegate_child_info = {}
            self._last_tool_result_metadata = {
                "tool_status": tool_status,
                "tool_error_code": tool_error_code,
                "security_event_type": "",
                "risk_level": "high" if tool["risky"] else "low",
                "approval_policy": self.approval_policy,
                "approved": approved,
                "read_only": not tool["risky"],
                "affected_paths": affected_paths,
                "workspace_changed": workspace_changed,
                "workspace_fingerprint": self.workspace.fingerprint(),
                "diff_summary": diff_summary,
                "diffs": diffs,
                **child_info,
            }
            self.record_process_note_for_tool(name, self._last_tool_result_metadata)
            return result
        except Exception as exc:
            after_snapshot = self.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            affected_paths, diff_summary = self.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            diffs = self._generate_diffs(affected_paths, before_snapshot, after_snapshot, rel_target_path, before_content)
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            self._last_tool_result_metadata = {
                "tool_status": "partial_success" if workspace_changed else "error",
                "tool_error_code": "tool_partial_success" if workspace_changed else "tool_failed",
                "security_event_type": security_event_type,
                "risk_level": "high" if tool["risky"] else "low",
                "approval_policy": self.approval_policy,
                "approved": approved,
                "read_only": not tool["risky"],
                "affected_paths": affected_paths,
                "workspace_changed": workspace_changed,
                "workspace_fingerprint": self.workspace.fingerprint(),
                "diff_summary": diff_summary,
                "diffs": diffs,
            }
            self.record_process_note_for_tool(name, self._last_tool_result_metadata)
            return f"error: tool {name} failed: {exc}"

    def repeated_tool_call(self, name, args):
        """ 检测最近两次工具调用是否完全相同"""
        # Agent 很常见的一种死循环 即在没有新信息的情况下反复发起同一调用
        tool_events = [item for item in self.session["history"] if item["role"] == "tool"]
        if len(tool_events) < 2:
            return False
        recent = tool_events[-2:]
        return all(item["name"] == name and item["args"] == args for item in recent)

    @staticmethod
    def new_task_id():
        return "task_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    @staticmethod
    def new_run_id():
        return "run_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

    def build_report(self, task_state):
        """ 构建最终运行报告 """
        # report 是一次运行的最终摘要；和 trace 的区别在于，trace 关注过程，report 关注结果与关键指标。
        return {
            "run_id": task_state.run_id,
            "trace_id": task_state.run_id,
            "session_id": task_state.session_id,
            "task_id": task_state.task_id,
            "status": task_state.status,
            "stop_reason": task_state.stop_reason,
            "final_answer": task_state.final_answer,
            "tool_steps": task_state.tool_steps,
            "attempts": task_state.attempts,
            "summary": task_state.summary,
            "checkpoint_id": task_state.checkpoint_id,
            "resume_status": task_state.resume_status,
            "task_state": task_state.to_dict(),
            "prompt_metadata": self.last_prompt_metadata,
            "error": dict(getattr(task_state, "error", {}) or {}),
            "durable_promotions": list(self.last_durable_promotions),
            "durable_rejections": list(self.last_durable_rejections),
            "durable_superseded": list(self.last_durable_superseded),
            "redacted_env": self.detected_secret_env_summary(),
        }

    def tool_example(self, name):
        return toolkit.tool_example(name)

    def validate_tool(self, name, args):
        """ 把通用工具校验和 runtime 级额外约束串起来。 """
        toolkit.validate_tool(self, name, args)
        if name == "delegate":
            if self.depth >= self.max_depth:
                raise ValueError("delegate depth exceeded")

    def tool_list_files(self, args):
        return toolkit.tool_list_files(self, args)

    def tool_read_file(self, args):
        return toolkit.tool_read_file(self, args)

    def tool_search(self, args):
        return toolkit.tool_search(self, args)

    def tool_run_shell(self, args):
        return toolkit.tool_run_shell(self, args)

    def tool_write_file(self, args):
        return toolkit.tool_write_file(self, args)

    def tool_patch_file(self, args):
        return toolkit.tool_patch_file(self, args)

    def tool_delegate(self, args):
        return toolkit.tool_delegate(self, args)

    def approve(self, name, args):
        if self.read_only:
            return name == "delegate"
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        try:
            answer = input(f"approve {name} {json.dumps(args, ensure_ascii=False)}? [y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    @staticmethod
    def parse(raw):
        """把模型原始输出解析成 runtime 可执行的动作或最终答案。

        为什么存在：
        模型输出首先是自然语言文本，而 runtime 需要的是结构化决策：
        “这是工具调用”还是“这是最终答案”。如果没有这层解析，后面的工具校验、
        审批和执行链路就没法可靠工作。

        输入 / 输出：
        - 输入：模型返回的原始文本 `raw`
        - 输出：`(kind, payload)`，其中 `kind` 可能是 `tool`、`final`、`retry`

        在 agent 链路里的位置：
        它位于 `model_client.complete()` 之后、`run_tool()` 之前，是模型输出
        进入平台控制流的第一道结构化关口。
        """
        raw = str(raw)
        # 这里支持两种工具格式：
        # 1. <tool>...</tool> 里包 JSON，适合简短调用
        # 2. XML 风格属性/子标签，适合写文件这类多行内容
        if "<tool>" in raw and ("<final>" not in raw or raw.find("<tool>") < raw.find("<final>")):
            body = Codini.extract(raw, "tool")
            try:
                payload = json.loads(body)
            except Exception:
                return "retry", Codini.retry_notice("model returned malformed tool JSON")
            if not isinstance(payload, dict):
                return "retry", Codini.retry_notice("tool payload must be a JSON object")
            if not str(payload.get("name", "")).strip():
                return "retry", Codini.retry_notice("tool payload is missing a tool name")
            args = payload.get("args", {})
            if args is None:
                payload["args"] = {}
            elif not isinstance(args, dict):
                return "retry", Codini.retry_notice()
            return "tool", payload
        if "<tool" in raw and ("<final>" not in raw or raw.find("<tool") < raw.find("<final>")):
            payload = Codini.parse_xml_tool(raw)
            if payload is not None:
                return "tool", payload
            return "retry", Codini.retry_notice()
        if "<longcat_tool_call>" in raw and ("<final>" not in raw or raw.find("<longcat_tool_call>") < raw.find("<final>")):
            payload = Codini.parse_longcat_tool_call(raw)
            if payload is not None:
                return "tool", payload
            return "retry", Codini.retry_notice("model returned malformed LongCat tool call")
        if "<final>" in raw:
            final = Codini.extract(raw, "final").strip()
            if final:
                return "final", final
            return "retry", Codini.retry_notice("model returned an empty <final> answer")
        raw = raw.strip()
        if raw:
            return "final", raw
        return "retry", Codini.retry_notice("model returned an empty response")

    @staticmethod
    def retry_notice(problem=None):
        """ 生成一条反馈给模型的重试提示 告知模型输出格式有问题 """
        prefix = "Runtime notice"
        if problem:
            prefix += f": {problem}"
        else:
            prefix += ": model returned malformed tool output"
        return (
            f"{prefix}. Reply with a valid <tool> call or a non-empty <final> answer. "
            'For multi-line files, prefer <tool name="write_file" path="file.py"><content>...</content></tool>.'
        )

    @staticmethod
    def parse_xml_tool(raw):
        """ 解析 XML 属性风格的工具调用 """
        match = re.search(r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", raw, re.S)
        if not match:
            return None
        attrs = Codini.parse_attrs(match.group("attrs"))
        name = str(attrs.pop("name", "")).strip()
        if not name:
            return None

        body = match.group("body")
        args = dict(attrs)
        for key in ("content", "old_text", "new_text", "command", "task", "pattern", "path"):
            if f"<{key}>" in body:
                args[key] = Codini.extract_raw(body, key)

        body_text = body.strip("\n")
        if name == "write_file" and "content" not in args and body_text:
            args["content"] = body_text
        if name == "delegate" and "task" not in args and body_text:
            args["task"] = body_text.strip()
        return {"name": name, "args": args}

    @staticmethod
    def parse_longcat_tool_call(raw):
        # TODO: remove this experimental LongCat compatibility once provider output is normalized upstream.
        match = re.search(r"<longcat_tool_call>(?P<body>.*?)(?:</longcat_tool_call>|$)", str(raw), re.S)
        if not match:
            return None
        body = match.group("body").strip()
        body = re.sub(r"/\s*$", "", body).strip()
        call_match = re.match(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<args>.*)\)\s*$", body, re.S)
        if call_match:
            name = call_match.group("name")
            args_text = call_match.group("args")
        else:
            attr_match = re.match(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+(?P<args>.*)$", body, re.S)
            if not attr_match:
                return None
            name = attr_match.group("name")
            args_text = attr_match.group("args")
        args = Codini.parse_call_args(args_text)
        return {"name": name, "args": args}

    @staticmethod
    def parse_call_args(text):
        args = {}
        pattern = re.compile(
            r"""(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>"(?:\\.|[^"])*"|'(?:\\.|[^'])*'|[^,\s]+)"""
        )
        for match in pattern.finditer(str(text)):
            value = match.group("value").strip()
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                try:
                    value = json.loads(value) if value.startswith('"') else value[1:-1].replace("\\'", "'")
                except Exception:
                    value = value[1:-1]
            elif re.fullmatch(r"-?\d+", value):
                value = int(value)
            elif re.fullmatch(r"-?\d+\.\d+", value):
                value = float(value)
            elif value.lower() == "true":
                value = True
            elif value.lower() == "false":
                value = False
            elif value.lower() == "none" or value.lower() == "null":
                value = None
            args[match.group("key")] = value
        return args

    @staticmethod
    def parse_attrs(text):
        attrs = {}
        for match in re.finditer(r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", text):
            attrs[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
        return attrs

    @staticmethod
    def extract(text, tag):
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return text
        start += len(start_tag)
        end = text.find(end_tag, start)
        if end == -1:
            return text[start:].strip()
        return text[start:end].strip()

    @staticmethod
    def extract_raw(text, tag):
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return text
        start += len(start_tag)
        end = text.find(end_tag, start)
        if end == -1:
            return text[start:]
        return text[start:end]


    def reset(self):
        """ 重置 Agent 状态 """
        self.session["history"] = []
        self.session["memory"].clear()
        self.session["memory"].update(memorylib.default_memory_state())
        self.memory = memorylib.LayeredMemory(self.session["memory"], workspace_root = self.root)
        self.session_store.save(self.session)

    def switch_model(self, new_model):
        """切换当前会话使用的模型名称（同一 provider 内）"""
        new_model = str(new_model).strip()
        if not new_model:
            return getattr(self.model_client, "model", "")
        self.model_client.model = new_model
        self.prefix_state = self.build_prefix()
        self.prefix = self.prefix_state.text
        return new_model

    def path(self, raw_path):
        """ 路径安全解析: 将任意路径标准化成 workspace 内的绝对路径 （防 ../ 逃逸）"""
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved

MiniAgent = Codini
