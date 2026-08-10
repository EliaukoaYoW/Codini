"""为每一道 Polyglot 题创建互不共享状态的 Agent 工作区。"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .task_adapter import PolyglotTask, TaskAdapterError, materialize_task


_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class IsolatedTaskWorkspace:
    """功能：描述单题隔离目录；输入：任务与运行标识；输出：Agent 和审计目录的稳定路径。"""

    task: PolyglotTask
    run_id: str
    root: Path
    agent_workspace: Path
    isolation_root: Path
    benchmark_root: Path

    def remove(self) -> None:
        """功能：安全删除当前单题隔离目录；输入：当前工作区；输出：无。"""
        resolved_root = self.root.resolve()
        resolved_parent = self.isolation_root.resolve()
        try:
            resolved_root.relative_to(resolved_parent)
        except ValueError as exc:
            raise TaskAdapterError(f"拒绝删除隔离根目录之外的路径：{resolved_root}") from exc
        if resolved_root == resolved_parent:
            raise TaskAdapterError("拒绝删除整个隔离根目录")
        if resolved_root.exists():
            shutil.rmtree(resolved_root)


def create_isolated_workspace(
    task: PolyglotTask,
    benchmark_root: Path | str,
    isolation_root: Path | str,
    run_id: str | None = None,
) -> IsolatedTaskWorkspace:
    """功能：创建全新单题 Agent 工作区；输入：任务、benchmark 根目录和可选运行标识；输出：隔离工作区。"""
    resolved_run_id = run_id or uuid.uuid4().hex
    if not _SAFE_RUN_ID.fullmatch(resolved_run_id):
        raise TaskAdapterError(f"run_id 包含非法字符：{resolved_run_id!r}")

    resolved_benchmark_root = Path(benchmark_root).resolve()
    resolved_isolation_root = Path(isolation_root).resolve()
    resolved_isolation_root.mkdir(parents=True, exist_ok=True)

    task_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", task.id).strip("-")
    task_root = resolved_isolation_root / f"{task_slug}-{resolved_run_id}"
    if task_root.exists():
        raise TaskAdapterError(f"隔离任务目录已经存在：{task_root}")

    agent_workspace = task_root / "agent"
    try:
        task_root.mkdir()
        materialize_task(task, agent_workspace, resolved_benchmark_root)
        (agent_workspace / ".codini").mkdir()
        manifest = {
            "schema_version": 1,
            "run_id": resolved_run_id,
            "task": task.to_dict(),
            "agent_workspace": "agent",
        }
        (task_root / "workspace.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        if task_root.exists():
            shutil.rmtree(task_root)
        raise

    return IsolatedTaskWorkspace(
        task=task,
        run_id=resolved_run_id,
        root=task_root,
        agent_workspace=agent_workspace,
        isolation_root=resolved_isolation_root,
        benchmark_root=resolved_benchmark_root,
    )
