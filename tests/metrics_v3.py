import copy
import json
import os
import random
import tempfile
import time
from datetime import datetime
from pathlib import Path

from codini import memory as memorylib
from codini.context_manager import (
    DEFAULT_SECTION_BUDGET_RATIOS,
    DEFAULT_TOTAL_BUDGET,
    ContextManager,
    section_budgets_for_total,
)
from codini.models import OpenAICompatibleModelClient, SiliconflowModelClient
from codini.runtime import Codini, SessionStore
from codini.workspace import WorkspaceContext

METRICS_SCHEMA_VERSION = 3
DEFAULT_CONTEXT_ALLOCATION_V3_PATH = Path("artifacts/context-allocation-v3.json")
DEFAULT_MEMORY_MECHANISM_V3_PATH = Path("artifacts/memory-mechanism-v3.json")


# V3 tests the complete three-tier memory mechanism with real model clients.
# Typed Notes are the representation used by Episodic Memory, not a fourth tier.
MEMORY_MECHANISM_TASKS_V3 = (
    {
        "id": "working_recent_file",
        "category": "working",
        "description": "Continue from the most recently inspected file.",
        "fixtures": {
            "services/alpha.env": "DEPLOY_CHANNEL=amber-17\n",
            "services/beta.env": "DEPLOY_CHANNEL=violet-82\n",
        },
        "expected": "violet-82",
        "history_markers": ("DEPLOY_CHANNEL=violet-82",),
        "expected_note_marker": "violet-82",
        "allowed_support_tools": 0,
        "followup": (
            "Continue the deployment-channel task for the file inspected most recently. "
            "Write the channel value to memory_v3_result.txt. The file must contain the value and nothing else."
        ),
    },
    {
        "id": "working_multi_file_synthesis",
        "category": "working",
        "description": "Combine cached facts from several recently inspected files.",
        "fixtures": {
            "deploy/region.txt": "REGION=eu-north-4\n",
            "deploy/rollout.txt": "RING=ring-3\n",
            "deploy/owner.txt": "OWNER=team-kite\n",
        },
        "expected": "eu-north-4|ring-3|team-kite",
        "history_markers": ("REGION=eu-north-4", "RING=ring-3", "OWNER=team-kite"),
        "expected_note_marker": "eu-north-4",
        "allowed_support_tools": 0,
        "followup": (
            "Use the deployment region, rollout ring, and owner inspected earlier. "
            "Write them to memory_v3_result.txt as region|ring|owner without labels."
        ),
    },
    {
        "id": "working_stale_file",
        "category": "working",
        "description": "Reject a stale file summary after the underlying file changes.",
        "fixtures": {"config/runtime.cfg": "TIMEOUT=90\n"},
        "expected": "135",
        "history_markers": ("TIMEOUT=90",),
        "expected_note_marker": "",
        "allowed_support_tools": 1,
        "followup": (
            "Use the current timeout from config/runtime.cfg, not an obsolete value. "
            "Write the numeric timeout to memory_v3_result.txt."
        ),
    },
    {
        "id": "working_capacity_pressure",
        "category": "working",
        "description": "Retain the newest entry when the bounded working set is full.",
        "fixtures": {
            **{
                f"cache/node-{index:02d}.cfg": f"SHARD=shard-{index:02d}-indigo-{index * 7 + 3}\n"
                for index in range(1, 11)
            }
        },
        "expected": "shard-10-indigo-73",
        "history_markers": (),
        "expected_note_marker": "",
        "allowed_support_tools": 0,
        "followup": (
            "Use the retained working-set value for cache/node-10.cfg. "
            "Write its SHARD value to memory_v3_result.txt without rereading the file."
        ),
    },
    {
        "id": "episodic_constraint_priority",
        "category": "episodic",
        "description": "Prefer an explicit constraint over a conflicting observation.",
        "fixtures": {"records/deployment.txt": "APPROVAL_MODE=manual\n"},
        "expected": "guarded-approval",
        "history_markers": ("APPROVAL_MODE=manual", "guarded-approval"),
        "expected_note_marker": "guarded-approval",
        "allowed_support_tools": 0,
        "followup": (
            "Apply the established deployment approval constraint. "
            "Write the approval mode to memory_v3_result.txt."
        ),
    },
    {
        "id": "episodic_decision_supersession",
        "category": "episodic",
        "description": "Use a newer decision and reject its superseded predecessor.",
        "fixtures": {},
        "expected": "jade-v3",
        "history_markers": ("quartz-v1", "jade-v3"),
        "expected_note_marker": "jade-v3",
        "allowed_support_tools": 0,
        "followup": (
            "Use the current payment export serializer decision. "
            "Write the serializer name to memory_v3_result.txt."
        ),
    },
    {
        "id": "episodic_preference_reuse",
        "category": "episodic",
        "description": "Apply a previously stated output preference indirectly.",
        "fixtures": {},
        "expected": "status=ready",
        "history_markers": ("compact key=value",),
        "expected_note_marker": "key=value",
        "allowed_support_tools": 0,
        "followup": (
            "Prepare a release summary whose status is ready using the established formatting choice. "
            "Write the summary to memory_v3_result.txt."
        ),
    },
    {
        "id": "episodic_error_resolution",
        "category": "episodic",
        "description": "Reuse a verified resolution instead of repeating a failed route.",
        "fixtures": {},
        "expected": "RETRY_SAME_COMMAND",
        "history_markers": ("recovery/runtime.lock",),
        "expected_note_marker": "run_shell succeeded on workspace",
        "allowed_support_tools": 0,
        "followup": (
            "A runtime-lock inspection command previously failed and was later resolved. "
            "Write RETRY_SAME_COMMAND or SWITCH_COMMAND to memory_v3_result.txt according to the verified resolution."
        ),
    },
    {
        "id": "episodic_scope_isolation",
        "category": "episodic",
        "description": "Select a file-scoped constraint over a newer project default.",
        "fixtures": {"services/payments.cfg": "service=payments\n"},
        "expected": "2",
        "history_markers": (),
        "expected_note_marker": "retry limit is 2",
        "allowed_support_tools": 0,
        "followup": (
            "Configure the retry limit for services/payments.cfg using the applicable scoped constraint. "
            "Write the numeric limit to memory_v3_result.txt."
        ),
    },
    {
        "id": "durable_project_convention",
        "category": "durable",
        "description": "Recall an explicitly promoted project convention in a new session.",
        "fixtures": {},
        "expected": "aurora-19",
        "history_markers": ("aurora-19",),
        "expected_note_marker": "aurora-19",
        "allowed_support_tools": 0,
        "followup": (
            "Use the saved project convention for the release lane. "
            "Write the lane value to memory_v3_result.txt."
        ),
    },
    {
        "id": "durable_decision_update",
        "category": "durable",
        "description": "Recall only the latest promoted project decision.",
        "fixtures": {},
        "expected": "jade-v3",
        "history_markers": ("quartz-v1", "jade-v3"),
        "expected_note_marker": "jade-v3",
        "allowed_support_tools": 0,
        "followup": (
            "Use the saved current project decision for the export serializer. "
            "Write the serializer name to memory_v3_result.txt."
        ),
    },
    {
        "id": "durable_persistence_boundary",
        "category": "durable",
        "description": "Do not leak a session-only preference into a new session.",
        "fixtures": {},
        "expected": "NONE",
        "forbidden_persistence_markers": ("magenta-61",),
        "history_markers": ("magenta-61",),
        "expected_note_marker": "",
        "allowed_support_tools": 0,
        "followup": (
            "Check established project memory for a scratch-report theme. "
            "Write its value to memory_v3_result.txt, or write NONE when no project-level value exists."
        ),
    },
)

MEMORY_V3_VARIANTS_BY_CATEGORY = {
    "working": ("full_memory", "memory_off", "working_off"),
    "episodic": ("full_memory", "memory_off", "episodic_off", "untyped_episodic"),
    "durable": ("full_memory", "memory_off", "durable_off"),
}


def _safe_mean(values):
    """ 安全计算平均值，避免除0错误 """
    values = list(values)
    if not values:
        return 0.0
    return sum(values) / len(values)


def _safe_ratio(numerator, denominator):
    """ 安全计算比率，避免除0错误 """
    if denominator == 0:
        return 0.0
    return numerator / denominator


def _provider_profile(provider):
    """功能：读取评估 Provider 配置；输入：Provider 名称；输出：ready 或 blocked 的配置字典。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None
    if load_dotenv is not None:
        load_dotenv()
    provider = str(provider).strip().lower()
    if provider == "openai":
        env_names = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")
        values = {name: os.environ.get(name, "").strip() for name in env_names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            return {
                "provider": provider,
                "status": "blocked",
                "reason": f"missing required environment variables: {', '.join(missing)}",
            }
        return {
            "provider": provider,
            "status": "ready",
            "model": values["OPENAI_MODEL"],
            "base_url": values["OPENAI_BASE_URL"],
            "api_key": values["OPENAI_API_KEY"],
        }
    if provider == "siliconflow":
        env_names = (
            "SILICONFLOW_API_KEY",
            "SILICONFLOW_BASE_URL",
            "SILICONFLOW_MODEL",
        )
        values = {name: os.environ.get(name, "").strip() for name in env_names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            return {
                "provider": provider,
                "status": "blocked",
                "reason": f"missing required environment variables: {', '.join(missing)}",
            }
        return {
            "provider": provider,
            "status": "ready",
            "model": values["SILICONFLOW_MODEL"],
            "base_url": values["SILICONFLOW_BASE_URL"],
            "api_key": values["SILICONFLOW_API_KEY"],
        }
    return {
        "provider": provider,
        "status": "blocked",
        "reason": f"unsupported provider: {provider}",
    }


def _make_provider_client(provider, timeout=None):
    profile = _provider_profile(provider)
    if profile["status"] != "ready":
        raise RuntimeError(profile["reason"])
    timeout = max(
        30,
        int(timeout or os.environ.get("CODINI_EXPERIMENT_TIMEOUT", "180")),
    )
    if profile["provider"] == "openai":
        return OpenAICompatibleModelClient(
            model=profile["model"],
            base_url=profile["base_url"],
            api_key=profile["api_key"],
            temperature=0.0,
            timeout=timeout,
        )

    return SiliconflowModelClient(
        model=profile["model"],
        base_url=profile["base_url"],
        api_key=profile["api_key"],
        temperature=0.0,
        timeout=timeout,
    )


def _normalize_text(value):
    text = str(value).strip().lower()
    while text.endswith((".", "!", "?", "\"", "'")):
        text = text[:-1].strip()
    return text


def _write_json_artifact(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def _memory_v3_write_fixtures(workspace_root, task):
    (workspace_root / "README.md").write_text("Codini memory benchmark fixture.\n", encoding="utf-8")
    for relative_path, content in task.get("fixtures", {}).items():
        path = workspace_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")


def _memory_v3_build_agent(workspace_root, provider, request_timeout):
    workspace = WorkspaceContext.build(workspace_root, repo_root_override=workspace_root)
    store = SessionStore(workspace_root / ".codini" / "sessions")
    return Codini(
        model_client=_make_provider_client(provider, timeout=request_timeout),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        max_steps=8,
        max_new_tokens=512,
    )


def _memory_v3_resume_agent(agent, provider, request_timeout, new_session=False):
    workspace_root = Path(agent.workspace.repo_root)
    if new_session:
        return _memory_v3_build_agent(workspace_root, provider, request_timeout)
    return Codini.from_session(
        model_client=_make_provider_client(provider, timeout=request_timeout),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=agent.session["id"],
        approval_policy="auto",
        max_steps=8,
        max_new_tokens=512,
    )


def _memory_v3_ask(agent, prompt, run_refs):
    answer = agent.ask(prompt)
    task_state = getattr(agent, "current_task_state", None)
    if task_state is not None and str(getattr(task_state, "run_id", "")).strip():
        run_refs.append((agent, task_state.run_id))
    return answer


def _memory_v3_seed_task(agent, task, workspace_root, run_refs):
    task_id = task["id"]

    def ask(prompt):
        return _memory_v3_ask(agent, prompt, run_refs)

    if task_id == "working_recent_file":
        ask("Inspect services/alpha.env, then inspect services/beta.env. Reply Done.")
    elif task_id == "working_multi_file_synthesis":
        ask("Inspect deploy/region.txt, deploy/rollout.txt, and deploy/owner.txt. Reply Done.")
    elif task_id == "working_stale_file":
        ask("Inspect config/runtime.cfg and note its current timeout. Reply Done.")
        (workspace_root / "config/runtime.cfg").write_text("TIMEOUT=135\n", encoding="utf-8")
        agent = _memory_v3_resume_agent(agent, task["_provider"], task["_request_timeout"])
    elif task_id == "working_capacity_pressure":
        for index in range(1, 11):
            path = f"cache/node-{index:02d}.cfg"
            content = (workspace_root / path).read_text(encoding="utf-8")
            agent.memory.remember_file(path)
            agent.memory.set_file_summary(path, memorylib.summarize_read_result(content))
        agent.session["memory"] = agent.memory.to_dict()
        agent.session_path = agent.session_store.save(agent.session)
    elif task_id == "episodic_constraint_priority":
        ask("Inspect records/deployment.txt and note the observed approval mode. Reply Done.")
        ask("For this session, deployment approval mode must be guarded-approval. Acknowledge the constraint.")
        for text in (
            "deployment approval dashboard displays the manual record",
            "deployment approval audit status is archived",
            "approval mode documentation mentions inherited defaults",
            "deployment records use a separate reporting mode",
        ):
            agent.memory.append_note(text, note_type="observation", scope="session")
    elif task_id == "episodic_decision_supersession":
        ask("We decided to use quartz-v1 for the payment export serializer. Acknowledge the decision.")
        ask("We decided to use jade-v3 instead. Acknowledge the updated decision.")
        for text in (
            "payment export serializer benchmark covers quartz fixtures",
            "payment export serializer documentation is under review",
            "export serializer compatibility report is ready",
            "payment serializer migration has a separate owner",
        ):
            agent.memory.append_note(text, note_type="observation", scope="session")
    elif task_id == "episodic_preference_reuse":
        ask("I prefer release summaries in compact key=value format. Acknowledge the preference.")
        for text in (
            "release summary archive contains verbose status reports",
            "release status dashboard is ready for review",
            "summary generation job publishes release metadata",
            "ready status appears in the release audit log",
        ):
            agent.memory.append_note(text, note_type="observation", scope="session")
    elif task_id == "episodic_error_resolution":
        command = (
            'python -c "from pathlib import Path; '
            "print(Path('recovery/runtime.lock').read_text())\""
        )
        ask(f"Run this exact command and observe the result: {command}\nReply Done afterwards.")
        lock_path = workspace_root / "recovery/runtime.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("RECOVERY=retry-same-path\n", encoding="utf-8")
        ask(f"Run the same exact command again: {command}\nReply Done afterwards.")
        for text in (
            "runtime lock failure dashboard contains historical errors",
            "recovery inspection report is awaiting review",
            "runtime lock documentation lists alternate files",
            "failure recovery metrics are exported nightly",
        ):
            agent.memory.append_note(text, note_type="observation", scope="session")
    elif task_id == "episodic_scope_isolation":
        agent.memory.append_note(
            "retry limit is 2",
            tags=("retry", "payments", "services/payments.cfg"),
            source="services/payments.cfg",
            note_type="constraint",
            scope="file",
            scope_refs=("services/payments.cfg",),
            evidence=("fixture:file_constraint", "services/payments.cfg"),
            freshness_paths=("services/payments.cfg",),
        )
        agent.memory.append_note(
            "retry limit is 5",
            tags=("retry", "project"),
            source="project_policy",
            note_type="constraint",
            scope="project",
            evidence=("fixture:project_default",),
        )
        agent.session["memory"] = agent.memory.to_dict()
        agent.session_path = agent.session_store.save(agent.session)
    elif task_id == "durable_project_convention":
        ask(
            "Remember this project convention for future sessions. "
            "Respond with this exact line:\nProject convention: release lane is aurora-19"
        )
    elif task_id == "durable_decision_update":
        ask(
            "Remember this project decision for future sessions. "
            "Respond with this exact line:\nDecision: export serializer uses quartz-v1"
        )
        ask(
            "Replace the saved project decision and remember the update. "
            "Respond with this exact line:\nDecision: export serializer uses jade-v3"
        )
    elif task_id == "durable_persistence_boundary":
        ask("For this session I prefer scratch-report themes in magenta-61. Acknowledge the preference.")
    else:
        raise ValueError(f"unknown memory V3 task: {task_id}")
    return agent


def _memory_v3_setup_valid(agent, task):
    state = agent.memory.to_dict()
    task_id = task["id"]
    notes = list(state.get("episodic_notes", []))
    active_notes = [note for note in notes if note.get("status", "active") == "active"]

    if task_id == "working_recent_file":
        return "violet-82" in json.dumps(state.get("file_summaries", {}))
    if task_id == "working_multi_file_synthesis":
        summaries = json.dumps(state.get("file_summaries", {}))
        return all(marker in summaries for marker in ("eu-north-4", "ring-3", "team-kite"))
    if task_id == "working_stale_file":
        summaries = json.dumps(state.get("file_summaries", {}))
        stale_old_note = any(
            "TIMEOUT=90" in note.get("text", "") and note.get("status") == "stale"
            for note in notes
        )
        return "TIMEOUT=90" not in summaries and stale_old_note
    if task_id == "working_capacity_pressure":
        return (
            len(state.get("working", {}).get("recent_files", [])) == memorylib.WORKING_FILE_LIMIT
            and "shard-10-indigo-73" in json.dumps(state.get("file_summaries", {}))
        )
    if task_id == "episodic_constraint_priority":
        return any(
            note.get("note_type") == "constraint" and "guarded-approval" in note.get("text", "")
            for note in active_notes
        )
    if task_id == "episodic_decision_supersession":
        has_current = any(
            note.get("note_type") == "decision"
            and note.get("status") == "active"
            and "jade-v3" in note.get("text", "")
            for note in notes
        )
        has_superseded = any(
            note.get("note_type") == "decision"
            and note.get("status") == "superseded"
            and "quartz-v1" in note.get("text", "")
            for note in notes
        )
        return has_current and has_superseded
    if task_id == "episodic_preference_reuse":
        return any(
            note.get("note_type") == "preference" and "key=value" in note.get("text", "")
            for note in active_notes
        )
    if task_id == "episodic_error_resolution":
        return any(
            note.get("note_type") == "error_resolution"
            and "run_shell succeeded on workspace" in note.get("text", "")
            for note in active_notes
        )
    if task_id == "episodic_scope_isolation":
        scopes = {
            (
                note.get("scope"),
                tuple(note.get("scope_refs", [])),
                note.get("text"),
            )
            for note in active_notes
            if note.get("note_type") == "constraint"
        }
        return (
            ("file", ("services/payments.cfg",), "retry limit is 2") in scopes
            and ("project", (), "retry limit is 5") in scopes
        )

    durable_root = Path(agent.workspace.repo_root) / ".codini" / "memory"
    durable_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in durable_root.rglob("*.md")
    ) if durable_root.exists() else ""
    if task_id == "durable_project_convention":
        return "aurora-19" in durable_text
    if task_id == "durable_decision_update":
        return "jade-v3" in durable_text and "quartz-v1" not in durable_text
    if task_id == "durable_persistence_boundary":
        has_session_preference = any(
            note.get("note_type") == "preference" and "magenta-61" in note.get("text", "")
            for note in active_notes
        )
        return has_session_preference and "magenta-61" not in durable_text
    return False


def _memory_v3_inject_history_pressure(agent, entries=12, chars_per_entry=1800):
    for index in range(int(entries)):
        role = "user" if index % 2 == 0 else "assistant"
        unit = f"unrelated-history-{index:02d} "
        content = (unit * ((int(chars_per_entry) // len(unit)) + 1))[: int(chars_per_entry)]
        agent.session["history"].append(
            {
                "role": role,
                "content": content,
                "created_at": f"2026-07-29T12:{index:02d}:00+00:00",
            }
        )
    agent.session_path = agent.session_store.save(agent.session)


def _memory_v3_rollover_seed_history(agent):
    rolled = []
    for item in agent.session.get("history", []):
        replacement = dict(item)
        replacement["content"] = "(seed evidence rolled out of transcript)"
        rolled.append(replacement)
    agent.session["history"] = rolled
    agent.session_path = agent.session_store.save(agent.session)


def _memory_v3_apply_variant(agent, variant):
    if variant == "memory_off":
        agent.memory.state = memorylib.default_memory_state()
        agent.session["memory"] = agent.memory.to_dict()
        agent.session_path = agent.session_store.save(agent.session)
        agent.feature_flags["memory"] = False
        agent.feature_flags["relevant_memory"] = False
        return

    state = agent.memory.to_dict()
    if variant == "working_off":
        state["working"] = {"task_summary": "", "recent_files": []}
        state["file_summaries"] = {}
        state["task"] = ""
        state["files"] = []
    elif variant == "episodic_off":
        state["episodic_notes"] = []
        state["notes"] = []
    elif variant == "untyped_episodic":
        flattened = []
        for index, note in enumerate(state.get("episodic_notes", [])):
            text = str(note.get("text", "")).strip()
            if not text:
                continue
            flattened.append(
                {
                    "text": text,
                    "tags": [],
                    "source": "",
                    "created_at": note.get("created_at", ""),
                    "note_index": index,
                    "kind": "episodic",
                    "note_type": "observation",
                    "scope": "session",
                    "scope_refs": [],
                    "evidence": [],
                    "freshness": {},
                    "status": "active",
                }
            )
        state["episodic_notes"] = flattened
        state["notes"] = [note["text"] for note in flattened]

    agent.memory.state = memorylib.normalize_memory_state(state, agent.workspace.repo_root)
    agent.session["memory"] = agent.memory.to_dict()
    agent.session_path = agent.session_store.save(agent.session)


def _memory_v3_disable_durable(workspace_root):
    durable_root = workspace_root / ".codini" / "memory"
    if not durable_root.exists():
        return None
    quarantine_root = Path(tempfile.mkdtemp(prefix="codini-memory-v3-durable-off-"))
    disabled_root = quarantine_root / "memory"
    durable_root.rename(disabled_root)
    return disabled_root


def _memory_v3_restore_durable(workspace_root, disabled_root):
    if disabled_root is None:
        return
    durable_root = workspace_root / ".codini" / "memory"
    if disabled_root.exists() and not durable_root.exists():
        durable_root.parent.mkdir(parents=True, exist_ok=True)
        disabled_root.rename(durable_root)
    try:
        disabled_root.parent.rmdir()
    except OSError:
        pass


def _memory_v3_raw_history_contains(agent, markers):
    history_text = "\n".join(
        str(item.get("content", ""))
        for item in agent.session.get("history", [])
    ).lower()
    return any(
        str(marker).lower() in history_text
        for marker in markers
        if str(marker).strip()
    )


def _memory_v3_trace_events(run_refs):
    events = []
    for agent, run_id in run_refs:
        try:
            events.extend(agent.run_store.load_trace_events(run_id))
        except (FileNotFoundError, OSError, ValueError):
            continue
    return events


def _memory_v3_usage(events):
    model_events = [event for event in events if event.get("event") == "model_parsed"]
    return {
        "model_calls": len(model_events),
        "input_tokens": sum(int(event.get("prompt_tokens", 0) or 0) for event in model_events),
        "completion_tokens": sum(int(event.get("completion_tokens", 0) or 0) for event in model_events),
        "total_tokens": sum(int(event.get("total_tokens", 0) or 0) for event in model_events),
        "cached_tokens": sum(int(event.get("cached_tokens", 0) or 0) for event in model_events),
        "latency_ms": sum(float(event.get("duration_ms", 0.0) or 0.0) for event in model_events),
    }


def _memory_v3_followup_tools(agent, run_id, task):
    try:
        events = agent.run_store.load_trace_events(run_id)
    except (FileNotFoundError, OSError, ValueError):
        events = []
    tool_events = [event for event in events if event.get("event") == "tool_executed"]
    support_tools = []
    for event in tool_events:
        name = str(event.get("name", ""))
        args = event.get("args", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (TypeError, ValueError):
                args = {}
        path = str((args or {}).get("path", "")).replace("\\", "/")
        if name == "write_file" and path.endswith("memory_v3_result.txt"):
            continue
        support_tools.append(event)
    allowed = int(task.get("allowed_support_tools", 0))
    return {
        "tool_calls": len(tool_events),
        "support_tool_calls": len(support_tools),
        "redundant_tool_calls": max(0, len(support_tools) - allowed),
        "read_calls": sum(1 for event in support_tools if event.get("name") == "read_file"),
        "failed_tool_calls": sum(
            1
            for event in support_tools
            if str(event.get("tool_status", "")).lower() in {"failed", "rejected", "error"}
            or str(event.get("span_status", "")).upper() == "ERROR"
        ),
    }


def _memory_v3_empty_row(task, variant, repetition):
    row = {
        "task_id": task["id"],
        "category": task["category"],
        "variant": variant,
        "repetition": int(repetition),
        "expected": task["expected"],
        "result": "",
        "task_success": False,
        "setup_valid": False,
        "history_leak": False,
        "provider_error": "",
        "expected_note_recalled": False,
        "wrong_scope_selection": False,
        "superseded_note_reused": False,
        "stale_memory_error": False,
        "false_persistence": False,
        "persistence_boundary_no_result": False,
        "persistence_boundary_wrong_value": False,
        "cross_session_task": task["category"] == "durable",
        "history_rollover_applied": task["category"] != "durable",
        "selected_notes": [],
        "selected_note_types": [],
        "selected_scopes": [],
        "selected_scope_refs": [],
    }
    usage_defaults = {
        "model_calls": 0,
        "input_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "latency_ms": 0.0,
    }
    tool_defaults = {
        "tool_calls": 0,
        "support_tool_calls": 0,
        "redundant_tool_calls": 0,
        "read_calls": 0,
        "failed_tool_calls": 0,
    }
    row.update(usage_defaults)
    row.update(tool_defaults)
    return row


def _memory_v3_evaluate_followup(agent, task, row, result_path):
    if result_path.exists():
        row["result"] = result_path.read_text(encoding="utf-8").strip()
    metadata = dict(getattr(agent, "last_prompt_metadata", {}) or {})
    relevant = dict(metadata.get("relevant_memory", {}) or {})
    row["selected_notes"] = list(relevant.get("selected_notes", []))
    row["selected_note_types"] = list(relevant.get("selected_note_types", []))
    row["selected_scopes"] = list(relevant.get("selected_scopes", []))
    row["selected_scope_refs"] = list(relevant.get("selected_scope_refs", []))
    note_marker = str(task.get("expected_note_marker", "")).lower()
    row["expected_note_recalled"] = bool(note_marker) and any(
        note_marker in str(note).lower()
        for note in row["selected_notes"]
    )

    normalized_result = _normalize_text(row["result"])
    normalized_expected = _normalize_text(task["expected"])
    row["wrong_scope_selection"] = (
        task["id"] == "episodic_scope_isolation" and normalized_result == "5"
    )
    row["superseded_note_reused"] = (
        task["id"] in {"episodic_decision_supersession", "durable_decision_update"}
        and normalized_result == "quartz-v1"
    )
    row["stale_memory_error"] = (
        task["id"] == "working_stale_file" and normalized_result == "90"
    )
    if task["id"] == "durable_persistence_boundary":
        forbidden_markers = {
            _normalize_text(marker)
            for marker in task.get("forbidden_persistence_markers", ())
            if str(marker).strip()
        }
        row["false_persistence"] = any(
            marker and marker in normalized_result
            for marker in forbidden_markers
        )
        row["persistence_boundary_no_result"] = not normalized_result
        row["persistence_boundary_wrong_value"] = bool(
            normalized_result
            and normalized_result != normalized_expected
            and not row["false_persistence"]
        )
    row["task_success"] = bool(
        row["setup_valid"]
        and not row["history_leak"]
        and normalized_result == normalized_expected
    )


def _memory_v3_run_followup(agent, task, row, workspace_root, run_refs):
    row["history_leak"] = _memory_v3_raw_history_contains(
        agent,
        task.get("history_markers", ()),
    )
    result_path = workspace_root / "memory_v3_result.txt"
    if result_path.exists():
        result_path.unlink()
    _memory_v3_ask(agent, task["followup"], run_refs)
    followup_run_id = agent.current_task_state.run_id
    row.update(_memory_v3_followup_tools(agent, followup_run_id, task))
    _memory_v3_evaluate_followup(agent, task, row, result_path)


def _run_memory_v3_scenario(task, variant, provider, repetition, request_timeout):
    """Compatibility path for running one isolated scenario, including its Seed."""
    row = _memory_v3_empty_row(task, variant, repetition)

    with tempfile.TemporaryDirectory(prefix="codini-memory-v3-") as temp_dir:
        workspace_root = Path(temp_dir)
        run_refs = []
        agents = []
        durable_quarantine = None
        try:
            _memory_v3_write_fixtures(workspace_root, task)
            agent = _memory_v3_build_agent(workspace_root, provider, request_timeout)
            agents.append(agent)
            runtime_task = dict(task)
            runtime_task["_provider"] = provider
            runtime_task["_request_timeout"] = request_timeout
            agent = _memory_v3_seed_task(agent, runtime_task, workspace_root, run_refs)
            if agent not in agents:
                agents.append(agent)
            row["setup_valid"] = _memory_v3_setup_valid(agent, task)

            if task["category"] != "durable":
                _memory_v3_rollover_seed_history(agent)
                _memory_v3_inject_history_pressure(agent)
                _memory_v3_apply_variant(agent, variant)
            else:
                if variant in {"durable_off", "memory_off"}:
                    durable_quarantine = _memory_v3_disable_durable(workspace_root)
                agent = _memory_v3_resume_agent(
                    agent,
                    provider,
                    request_timeout,
                    new_session=True,
                )
                agents.append(agent)
                if variant == "memory_off":
                    _memory_v3_apply_variant(agent, variant)

            _memory_v3_run_followup(
                agent,
                task,
                row,
                workspace_root,
                run_refs,
            )
        except Exception as exc:
            row["provider_error"] = f"{exc.__class__.__name__}: {exc}"
        finally:
            _memory_v3_restore_durable(workspace_root, durable_quarantine)
            row.update(_memory_v3_usage(_memory_v3_trace_events(run_refs)))
    return row


def _memory_v3_prepare_shared_seed(task, provider, repetition, request_timeout, workspace_root):
    run_refs = []
    agent = None
    seed_error = ""
    setup_valid = False
    try:
        _memory_v3_write_fixtures(workspace_root, task)
        agent = _memory_v3_build_agent(workspace_root, provider, request_timeout)
        runtime_task = dict(task)
        runtime_task["_provider"] = provider
        runtime_task["_request_timeout"] = request_timeout
        agent = _memory_v3_seed_task(
            agent,
            runtime_task,
            workspace_root,
            run_refs,
        )
        setup_valid = _memory_v3_setup_valid(agent, task)
    except Exception as exc:
        seed_error = f"{exc.__class__.__name__}: {exc}"
    seed_row = {
        "seed_id": f"{task['id']}:r{int(repetition)}",
        "task_id": task["id"],
        "category": task["category"],
        "repetition": int(repetition),
        "setup_valid": bool(setup_valid),
        "provider_error": seed_error,
        **_memory_v3_usage(_memory_v3_trace_events(run_refs)),
    }
    return agent, seed_row


def _memory_v3_clone_seed_agent(seed_agent, provider, request_timeout, variant, repetition):
    session = copy.deepcopy(seed_agent.session)
    session["id"] = (
        f"{seed_agent.session['id']}-{variant}-r{int(repetition)}"
    )
    return Codini(
        model_client=_make_provider_client(provider, timeout=request_timeout),
        workspace=seed_agent.workspace,
        session_store=seed_agent.session_store,
        session=session,
        approval_policy="auto",
        max_steps=8,
        max_new_tokens=512,
    )


def _run_memory_v3_variant_from_seed(
    seed_agent,
    seed_row,
    task,
    variant,
    provider,
    repetition,
    request_timeout,
    workspace_root,
):
    row = _memory_v3_empty_row(task, variant, repetition)
    row["setup_valid"] = bool(seed_row["setup_valid"])
    row["shared_seed_id"] = seed_row["seed_id"]
    run_refs = []
    durable_quarantine = None
    try:
        if task["category"] == "durable":
            if variant in {"durable_off", "memory_off"}:
                durable_quarantine = _memory_v3_disable_durable(workspace_root)
            agent = _memory_v3_resume_agent(
                seed_agent,
                provider,
                request_timeout,
                new_session=True,
            )
            if variant == "memory_off":
                _memory_v3_apply_variant(agent, variant)
        else:
            agent = _memory_v3_clone_seed_agent(
                seed_agent,
                provider,
                request_timeout,
                variant,
                repetition,
            )
            _memory_v3_rollover_seed_history(agent)
            _memory_v3_inject_history_pressure(agent)
            _memory_v3_apply_variant(agent, variant)

        _memory_v3_run_followup(
            agent,
            task,
            row,
            workspace_root,
            run_refs,
        )
    except Exception as exc:
        row["provider_error"] = f"{exc.__class__.__name__}: {exc}"
    finally:
        _memory_v3_restore_durable(workspace_root, durable_quarantine)
        row.update(_memory_v3_usage(_memory_v3_trace_events(run_refs)))
    return row


def _memory_v3_variant_summary(rows):
    rows = list(rows)
    completed = [row for row in rows if not row.get("provider_error")]
    note_rows = [row for row in rows if row.get("expected_note_marker", "")]
    return {
        "runs": len(rows),
        "completed_runs": len(completed),
        "task_success_rate": _safe_ratio(sum(1 for row in rows if row["task_success"]), len(rows)),
        "setup_valid_rate": _safe_ratio(sum(1 for row in rows if row["setup_valid"]), len(rows)),
        "history_leak_rate": _safe_ratio(sum(1 for row in rows if row["history_leak"]), len(rows)),
        "provider_error_rate": _safe_ratio(sum(1 for row in rows if row["provider_error"]), len(rows)),
        "relevant_note_recall_at_3": _safe_ratio(
            sum(1 for row in note_rows if row["expected_note_recalled"]),
            len(note_rows),
        ),
        "avg_tool_calls": _safe_mean(row["tool_calls"] for row in completed),
        "avg_redundant_tool_calls": _safe_mean(row["redundant_tool_calls"] for row in completed),
        "avg_read_calls": _safe_mean(row["read_calls"] for row in completed),
        "wrong_scope_selection_rate": _safe_ratio(
            sum(1 for row in rows if row["wrong_scope_selection"]),
            sum(1 for row in rows if row["task_id"] == "episodic_scope_isolation"),
        ),
        "superseded_note_reuse_rate": _safe_ratio(
            sum(1 for row in rows if row["superseded_note_reused"]),
            sum(
                1
                for row in rows
                if row["task_id"] in {"episodic_decision_supersession", "durable_decision_update"}
            ),
        ),
        "stale_memory_error_rate": _safe_ratio(
            sum(1 for row in rows if row["stale_memory_error"]),
            sum(1 for row in rows if row["task_id"] == "working_stale_file"),
        ),
        "false_persistence_rate": _safe_ratio(
            sum(1 for row in rows if row["false_persistence"]),
            sum(1 for row in rows if row["task_id"] == "durable_persistence_boundary"),
        ),
        "persistence_boundary_no_result_rate": _safe_ratio(
            sum(1 for row in rows if row["persistence_boundary_no_result"]),
            sum(1 for row in rows if row["task_id"] == "durable_persistence_boundary"),
        ),
        "persistence_boundary_wrong_value_rate": _safe_ratio(
            sum(1 for row in rows if row["persistence_boundary_wrong_value"]),
            sum(1 for row in rows if row["task_id"] == "durable_persistence_boundary"),
        ),
        "avg_input_tokens": _safe_mean(row["input_tokens"] for row in completed if row["input_tokens"] > 0),
        "avg_total_tokens": _safe_mean(row["total_tokens"] for row in completed if row["total_tokens"] > 0),
        "avg_latency_ms": _safe_mean(row["latency_ms"] for row in completed),
    }


def _memory_v3_rate(rows, variant, category=None):
    selected = [
        row
        for row in rows
        if row["variant"] == variant and (category is None or row["category"] == category)
    ]
    return _safe_ratio(sum(1 for row in selected if row["task_success"]), len(selected))


def run_memory_mechanism_ablation_v3(
    artifact_path=DEFAULT_MEMORY_MECHANISM_V3_PATH,
    provider="openai",
    repetitions=1,
    request_timeout=180,
    seed=20260729,
    progress=True,
):
    """Run the real-client three-tier memory experiment and write its artifact."""
    repetitions = max(1, int(repetitions))
    request_timeout = max(30, int(request_timeout))
    rng = random.Random(int(seed))
    groups = []
    scenario_runs = 0
    for repetition in range(1, repetitions + 1):
        tasks = list(MEMORY_MECHANISM_TASKS_V3)
        rng.shuffle(tasks)
        for task in tasks:
            variants = list(MEMORY_V3_VARIANTS_BY_CATEGORY[task["category"]])
            rng.shuffle(variants)
            groups.append((task, variants, repetition))
            scenario_runs += len(variants)

    rows = []
    seed_rows = []
    scenario_index = 0
    for task, variants_for_task, repetition in groups:
        with tempfile.TemporaryDirectory(prefix="codini-memory-v3-seed-") as temp_dir:
            workspace_root = Path(temp_dir)
            seed_agent, seed_row = _memory_v3_prepare_shared_seed(
                task=task,
                provider=provider,
                repetition=repetition,
                request_timeout=request_timeout,
                workspace_root=workspace_root,
            )
            seed_rows.append(seed_row)
            for variant in variants_for_task:
                scenario_index += 1
                if progress:
                    print(
                        f"[memory-v3] {scenario_index}/{scenario_runs} "
                        f"task={task['id']} variant={variant} repetition={repetition}",
                        flush=True,
                    )
                if seed_agent is None or seed_row["provider_error"]:
                    row = _memory_v3_empty_row(task, variant, repetition)
                    row["shared_seed_id"] = seed_row["seed_id"]
                    row["provider_error"] = (
                        f"seed_failed: {seed_row['provider_error'] or 'seed agent unavailable'}"
                    )
                else:
                    row = _run_memory_v3_variant_from_seed(
                        seed_agent=seed_agent,
                        seed_row=seed_row,
                        task=task,
                        variant=variant,
                        provider=provider,
                        repetition=repetition,
                        request_timeout=request_timeout,
                        workspace_root=workspace_root,
                    )
                row["expected_note_marker"] = task.get("expected_note_marker", "")
                rows.append(row)

    variants = {
        variant: _memory_v3_variant_summary(
            row for row in rows if row["variant"] == variant
        )
        for variant in sorted({row["variant"] for row in rows})
    }
    full_rate = _memory_v3_rate(rows, "full_memory")
    off_rate = _memory_v3_rate(rows, "memory_off")
    typed_rate = _memory_v3_rate(rows, "full_memory", "episodic")
    untyped_rate = _memory_v3_rate(rows, "untyped_episodic", "episodic")
    working_rate = _memory_v3_rate(rows, "full_memory", "working")
    working_off_rate = _memory_v3_rate(rows, "working_off", "working")
    durable_rate = _memory_v3_rate(rows, "full_memory", "durable")
    durable_off_rate = _memory_v3_rate(rows, "durable_off", "durable")
    completed_seed_rows = [row for row in seed_rows if not row["provider_error"]]
    seed_summary = {
        "runs": len(seed_rows),
        "completed_runs": len(completed_seed_rows),
        "setup_valid_rate": _safe_ratio(
            sum(1 for row in seed_rows if row["setup_valid"]),
            len(seed_rows),
        ),
        "provider_error_rate": _safe_ratio(
            sum(1 for row in seed_rows if row["provider_error"]),
            len(seed_rows),
        ),
        "model_calls": sum(row["model_calls"] for row in seed_rows),
        "input_tokens": sum(row["input_tokens"] for row in seed_rows),
        "total_tokens": sum(row["total_tokens"] for row in seed_rows),
        "cached_tokens": sum(row["cached_tokens"] for row in seed_rows),
        "latency_ms": sum(row["latency_ms"] for row in seed_rows),
    }

    payload = {
        "schema_version": METRICS_SCHEMA_VERSION,
        "artifact_type": "memory-mechanism-ablation-v3",
        "captured_at": datetime.utcnow().isoformat() + "Z",
        "provider": str(provider),
        "model": _provider_profile(provider).get("model", ""),
        "prompt_budget": DEFAULT_TOTAL_BUDGET,
        "repetitions": repetitions,
        "seed": int(seed),
        "task_count": len(MEMORY_MECHANISM_TASKS_V3),
        "scenario_runs": len(rows),
        "methodology": {
            "real_client": True,
            "shared_seed_per_task_repetition": True,
            "scope_resolution": "explicit scope_refs with deterministic conflict resolution",
            "durable_off_isolation": "quarantined outside the agent workspace",
            "false_persistence_definition": "explicit forbidden-value recall only",
            "seed_history_rollover": True,
            "history_pressure_entries": 12,
            "history_pressure_chars_per_entry": 1800,
            "result_verifier": "exact temporary result-file match",
            "primary_comparison": ["full_memory", "memory_off"],
            "targeted_ablations": ["working_off", "episodic_off", "untyped_episodic", "durable_off"],
        },
        "task_catalog": [
            {
                "id": task["id"],
                "category": task["category"],
                "description": task["description"],
            }
            for task in MEMORY_MECHANISM_TASKS_V3
        ],
        "seed_summary": seed_summary,
        "seed_rows": seed_rows,
        "variants": variants,
        "summary": {
            "full_memory_task_success_rate": full_rate,
            "memory_off_task_success_rate": off_rate,
            "overall_task_success_lift_pp": (full_rate - off_rate) * 100.0,
            "typed_episodic_task_success_rate": typed_rate,
            "untyped_episodic_task_success_rate": untyped_rate,
            "typed_note_success_lift_pp": (typed_rate - untyped_rate) * 100.0,
            "working_task_success_rate": working_rate,
            "working_off_task_success_rate": working_off_rate,
            "working_memory_success_lift_pp": (working_rate - working_off_rate) * 100.0,
            "durable_task_success_rate": durable_rate,
            "durable_off_task_success_rate": durable_off_rate,
            "durable_memory_success_lift_pp": (durable_rate - durable_off_rate) * 100.0,
            "full_memory_avg_redundant_tool_calls": variants.get("full_memory", {}).get(
                "avg_redundant_tool_calls", 0.0
            ),
            "memory_off_avg_redundant_tool_calls": variants.get("memory_off", {}).get(
                "avg_redundant_tool_calls", 0.0
            ),
        },
        "rows": rows,
    }
    return _write_json_artifact(artifact_path, payload)


# 对照实验二: 同一预算下的上下文动态分配
CONTEXT_ALLOCATION_SOURCES = ("prefix", "memory", "relevant_memory", "history")
CONTEXT_ALLOCATION_PRESSURES = (("moderate", 1.25), ("high", 1.50), ("extreme", 1.80))
CONTEXT_ALLOCATION_VARIANTS = ("fixed", "dynamic", "full_context")
CONTEXT_EVIDENCE_OFFSETS = (-180, 60, 140)


def _context_noise(label, length):
    length = max(0, int(length))
    unit = f"{label}_noise "
    return (unit * ((length // len(unit)) + 1))[:length]


def _context_payload(label, target_chars, evidence="", evidence_ratio=0.5):
    target_chars = max(len(evidence), int(target_chars))
    if not evidence:
        return _context_noise(label, target_chars)
    evidence_at = min(
        max(0, int(target_chars * float(evidence_ratio))),
        target_chars - len(evidence),
    )
    prefix = _context_noise(f"{label}_before", evidence_at)
    suffix = _context_noise(
        f"{label}_after",
        target_chars - len(prefix) - len(evidence),
    )
    return prefix + evidence + suffix


def _context_history(target_chars, evidence="", evidence_offset=0):
    target_chars = max(0, int(target_chars))
    if evidence:
        newest_total = min(11800, max(0, target_chars - len(evidence) - 900))
        newest_first = newest_total // 2
        newest_second = newest_total - newest_first
        evidence_chars = max(len(evidence) + 900, target_chars - newest_total)
        evidence_ratio = min(
            0.95,
            max(0, 600 + int(evidence_offset)) / max(1, evidence_chars),
        )
        return [
            {
                "role": "user",
                "content": _context_payload(
                    "history_evidence",
                    evidence_chars,
                    evidence,
                    evidence_ratio,
                ),
            },
            {"role": "assistant", "content": _context_noise("history_recent_a", newest_first)},
            {"role": "user", "content": _context_noise("history_recent_b", newest_second)},
        ]

    entries = []
    remaining = target_chars
    index = 0
    while remaining > 0:
        chunk_chars = min(6000, remaining)
        entries.append(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": _context_noise(f"history_{index}", chunk_chars),
            }
        )
        remaining -= chunk_chars
        index += 1
    return entries


class _ContextAllocationMemory:
    def __init__(self, notes):
        self.notes = list(notes)

    def retrieval_candidates(self, query, limit=3):
        del query
        return self.notes[: int(limit)]


class _ContextAllocationAgent:
    def __init__(self, *, prefix, memory_text, notes, history):
        self.prefix = str(prefix)
        self._memory_text = str(memory_text)
        self.memory = _ContextAllocationMemory(notes)
        self.session = {"history": list(history)}
        self.feature_flags = {
            "memory": True,
            "relevant_memory": True,
            "context_reduction": True,
        }

    def memory_text(self):
        return self._memory_text

    def feature_enabled(self, name):
        return bool(self.feature_flags.get(name, True))

    @staticmethod
    def render_checkpoint_text():
        return ""


def _context_allocation_case(
    source,
    pressure_name,
    pressure_ratio,
    total_budget,
    evidence_offset=0,
):
    section_budgets = section_budgets_for_total(total_budget)
    key = f"ALLOC-{source.upper()}-{pressure_name.upper()}"
    value = f"VALUE-{source.upper()}-{pressure_name.upper()}-7319"
    evidence = f"The value for allocation key {key} is {value}."
    target_chars = {
        section: max(200, int(section_budgets[section] * float(pressure_ratio)))
        for section in CONTEXT_ALLOCATION_SOURCES
    }
    if source != "relevant_memory":
        target_chars["history"] += target_chars["relevant_memory"]
    evidence_at = min(
        target_chars[source] - len(evidence),
        section_budgets[source] + 20 + int(evidence_offset),
    )
    evidence_ratio = evidence_at / target_chars[source]

    prefix = _context_payload(
        "prefix",
        target_chars["prefix"],
        evidence if source == "prefix" else "",
        evidence_ratio,
    )
    memory_text = _context_payload(
        "memory",
        target_chars["memory"],
        evidence if source == "memory" else "",
        evidence_ratio,
    )
    history = _context_history(
        target_chars["history"],
        evidence if source == "history" else "",
        evidence_offset=evidence_offset,
    )
    notes = []
    if source == "relevant_memory":
        per_note_chars = max(200, target_chars["relevant_memory"] // 3)
        per_note_evidence_at = min(
            per_note_chars - len(evidence),
            (section_budgets["relevant_memory"] // 3) + 20 + int(evidence_offset),
        )
        per_note_evidence_ratio = per_note_evidence_at / per_note_chars
        for index in range(3):
            notes.append(
                {
                    "text": _context_payload(
                        f"relevant_{index}",
                        per_note_chars,
                        evidence if index == 0 else "",
                        per_note_evidence_ratio,
                    ),
                    "note_type": "constraint" if index == 0 else "observation",
                    "scope": "session",
                    "source": f"context-allocation-{index}",
                }
            )

    prompts = {
        "prefix": f"According to the project rule, return the value for allocation key {key}.",
        "memory": f"Use the working memory for this code task and return the value for allocation key {key}.",
        "relevant_memory": f"Use the relevant remembered note and return the value for allocation key {key}.",
        "history": f"Continue the earlier allocation task and return the value for allocation key {key}.",
    }
    request = prompts[source] + " Return the value only, without explanation."
    return {
        "id": f"{source}-{pressure_name}",
        "source": source,
        "pressure": pressure_name,
        "pressure_ratio": float(pressure_ratio),
        "evidence_offset": int(evidence_offset),
        "key": key,
        "value": value,
        "evidence": evidence,
        "request": request,
        "section_budgets": section_budgets,
        "agent": _ContextAllocationAgent(
            prefix=prefix,
            memory_text=memory_text,
            notes=notes,
            history=history,
        ),
    }


def build_context_allocation_prompt(
    source,
    pressure_name,
    pressure_ratio,
    variant,
    total_budget=DEFAULT_TOTAL_BUDGET,
    evidence_offset=0,
):
    """Build one controlled benchmark prompt without calling a model."""
    case = _context_allocation_case(
        str(source),
        str(pressure_name),
        float(pressure_ratio),
        int(total_budget),
        evidence_offset=int(evidence_offset),
    )
    agent = case["agent"]
    if variant == "full_context":
        agent.feature_flags["context_reduction"] = False
        strategy = "dynamic"
    elif variant in {"fixed", "dynamic"}:
        strategy = variant
    else:
        raise ValueError(f"unsupported context allocation variant: {variant}")
    manager = ContextManager(
        agent,
        total_budget=int(total_budget),
        section_budgets=case["section_budgets"],
        budget_strategy=strategy,
    )
    prompt, metadata = manager.build(case["request"])
    return case, prompt, metadata


def _summarize_context_allocation_rows(rows):
    rows = list(rows)
    completed = [row for row in rows if not row.get("provider_error")]
    return {
        "runs": len(rows),
        "completed_runs": len(completed),
        "provider_error_rate": _safe_ratio(
            sum(1 for row in rows if row.get("provider_error")),
            len(rows),
        ),
        "task_success_rate": _safe_ratio(
            sum(1 for row in completed if row["task_succeeded"]),
            len(completed),
        ),
        "evidence_retention_rate": _safe_ratio(
            sum(1 for row in rows if row["evidence_retained"]),
            len(rows),
        ),
        "misallocation_rate": _safe_ratio(
            sum(1 for row in rows if row["misallocated"]),
            len(rows),
        ),
        "over_budget_rate": _safe_ratio(
            sum(1 for row in rows if row["prompt_over_budget"]),
            len(rows),
        ),
        "avg_prompt_chars": _safe_mean(row["prompt_chars"] for row in rows),
        "avg_input_tokens": _safe_mean(
            row["input_tokens"]
            for row in completed
            if row["input_tokens"] > 0
        ),
        "avg_latency_ms": _safe_mean(row["latency_ms"] for row in completed),
    }


def run_real_context_experiment(
    provider="openai",
    repetitions=3,
    total_budget=DEFAULT_TOTAL_BUDGET,
    max_new_tokens=64,
    request_timeout=180,
):
    """Compare fixed and dynamic allocation with a real model at the same hard budget."""
    repetitions = max(1, int(repetitions))
    total_budget = max(1000, int(total_budget))
    provider = str(provider)
    profile = _provider_profile(provider)
    if profile["status"] != "ready":
        raise RuntimeError(profile["reason"])

    rows = []
    config_index = 0
    for source in CONTEXT_ALLOCATION_SOURCES:
        for pressure_name, pressure_ratio in CONTEXT_ALLOCATION_PRESSURES:
            for repetition in range(repetitions):
                evidence_offset = CONTEXT_EVIDENCE_OFFSETS[
                    repetition % len(CONTEXT_EVIDENCE_OFFSETS)
                ]
                rotation = (config_index + repetition) % len(CONTEXT_ALLOCATION_VARIANTS)
                variants = (
                    CONTEXT_ALLOCATION_VARIANTS[rotation:]
                    + CONTEXT_ALLOCATION_VARIANTS[:rotation]
                )
                for variant in variants:
                    case, prompt, metadata = build_context_allocation_prompt(
                        source,
                        pressure_name,
                        pressure_ratio,
                        variant,
                        total_budget=total_budget,
                        evidence_offset=evidence_offset,
                    )
                    allocation = dict(metadata.get("budget_allocation", {}))
                    allocated = dict(allocation.get("allocated_chars", {}))
                    floors = dict(allocation.get("floor_chars", {}))
                    irrelevant_surplus = sum(
                        max(0, int(allocated.get(section, 0)) - int(floors.get(section, 0)))
                        for section in CONTEXT_ALLOCATION_SOURCES
                        if section != source
                    )
                    evidence_retained = case["evidence"] in prompt
                    client = _make_provider_client(provider, timeout=request_timeout)
                    started_at = time.monotonic()
                    provider_error = ""
                    try:
                        answer = client.complete(prompt, int(max_new_tokens))
                    except RuntimeError as exc:
                        answer = ""
                        provider_error = str(exc)
                    latency_ms = (time.monotonic() - started_at) * 1000.0
                    usage = dict(getattr(client, "last_completion_metadata", {}) or {})
                    rows.append(
                        {
                            "config_id": case["id"],
                            "source": source,
                            "pressure": pressure_name,
                            "pressure_ratio": pressure_ratio,
                            "repetition": repetition + 1,
                            "evidence_offset": evidence_offset,
                            "variant": variant,
                            "task_succeeded": _normalize_text(answer) == _normalize_text(case["value"]),
                            "provider_error": provider_error,
                            "evidence_retained": evidence_retained,
                            "misallocated": bool(not evidence_retained and irrelevant_surplus > 0),
                            "answer": answer,
                            "expected": case["value"],
                            "prompt_chars": len(prompt),
                            "prompt_over_budget": len(prompt) > total_budget,
                            "input_tokens": int(usage.get("input_tokens") or 0),
                            "output_tokens": int(usage.get("output_tokens") or 0),
                            "cached_tokens": int(usage.get("cached_tokens") or 0),
                            "latency_ms": latency_ms,
                            "budget_strategy": metadata.get("budget_strategy", ""),
                            "section_budgets": dict(metadata.get("section_budgets", {})),
                            "budget_allocation": allocation,
                        }
                    )
            config_index += 1

    variants = {
        variant: _summarize_context_allocation_rows(
            row for row in rows if row["variant"] == variant
        )
        for variant in CONTEXT_ALLOCATION_VARIANTS
    }
    pressure_summary = {
        pressure_name: {
            variant: _summarize_context_allocation_rows(
                row
                for row in rows
                if row["pressure"] == pressure_name and row["variant"] == variant
            )
            for variant in ("fixed", "dynamic")
        }
        for pressure_name, _ in CONTEXT_ALLOCATION_PRESSURES
    }
    fixed = variants["fixed"]
    dynamic = variants["dynamic"]
    configs = []
    for source in CONTEXT_ALLOCATION_SOURCES:
        for pressure_name, pressure_ratio in CONTEXT_ALLOCATION_PRESSURES:
            config_rows = [
                row
                for row in rows
                if row["source"] == source and row["pressure"] == pressure_name
            ]
            configs.append(
                {
                    "id": f"{source}-{pressure_name}",
                    "source": source,
                    "pressure": pressure_name,
                    "pressure_ratio": pressure_ratio,
                    "variants": {
                        variant: _summarize_context_allocation_rows(
                            row for row in config_rows if row["variant"] == variant
                        )
                        for variant in CONTEXT_ALLOCATION_VARIANTS
                    },
                }
            )
    return {
        "schema_version": 3,
        "experiment": "context_allocation_quality",
        "provider": provider,
        "model": profile["model"],
        "request_timeout": int(request_timeout),
        "total_budget": total_budget,
        "section_budget_ratios": dict(DEFAULT_SECTION_BUDGET_RATIOS),
        "section_budgets": section_budgets_for_total(total_budget),
        "config_count": len(configs),
        "repetitions": repetitions,
        "run_count": len(rows),
        "configs": configs,
        "variants": variants,
        "pressure_summary": pressure_summary,
        "summary": {
            "dynamic_task_success_rate": dynamic["task_success_rate"],
            "fixed_task_success_rate": fixed["task_success_rate"],
            "task_success_lift_pp": (
                dynamic["task_success_rate"] - fixed["task_success_rate"]
            ) * 100.0,
            "dynamic_evidence_retention_rate": dynamic["evidence_retention_rate"],
            "fixed_evidence_retention_rate": fixed["evidence_retention_rate"],
            "evidence_retention_lift_pp": (
                dynamic["evidence_retention_rate"] - fixed["evidence_retention_rate"]
            ) * 100.0,
            "dynamic_misallocation_rate": dynamic["misallocation_rate"],
            "fixed_misallocation_rate": fixed["misallocation_rate"],
            "misallocation_reduction_pp": (
                fixed["misallocation_rate"] - dynamic["misallocation_rate"]
            ) * 100.0,
        },
        "rows": rows,
    }


def run_context_allocation_ablation_v3(
    artifact_path=DEFAULT_CONTEXT_ALLOCATION_V3_PATH,
    provider="openai",
    repetitions=3,
    total_budget=DEFAULT_TOTAL_BUDGET,
    request_timeout=180,
):
    payload = run_real_context_experiment(
        provider=provider,
        repetitions=repetitions,
        total_budget=total_budget,
        request_timeout=request_timeout,
    )
    artifact = {
        **payload,
        "artifact_type": "context-allocation-v3",
        "captured_at": datetime.utcnow().isoformat() + "Z",
    }
    return _write_json_artifact(artifact_path, artifact)
