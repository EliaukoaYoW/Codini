"""运行 Codini 的 Python/JavaScript Polyglot smoke 评测。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_ROOT = Path(__file__).resolve().parent / "polyglot"
DATASET_ROOT = BENCHMARK_ROOT / "dataset" / "python"
RESULTS_ROOT = REPOSITORY_ROOT / "benchmarks" / "results"
IMAGE = "codini-polyglot-pyjs:0.1"
AGENT_INITIAL_STEPS = 12
AGENT_TIMEOUT_SECONDS = 600


def _find_executable(name: str, fallbacks: tuple[Path, ...]) -> str:
    """功能：定位外部程序；输入：命令名和候选路径；输出：可执行文件绝对路径。"""
    executable = shutil.which(name)
    if executable:
        return executable
    for candidate in fallbacks:
        if candidate.is_file():
            return str(candidate.resolve())
    raise RuntimeError(f"找不到 `{name}`，请先完成项目依赖和 Docker 环境准备。")


def _completed(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    """功能：执行无 shell 的子进程；输入：参数列表和运行选项；输出：文本模式执行结果。"""
    return subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **kwargs,
    )


def _provider_from_environment() -> tuple[str, dict[str, str]]:
    """功能：检查模型配置并选择 Provider；输入：进程环境和仓库 .env；输出：Provider 及其环境变量。"""
    from dotenv import dotenv_values

    configured = {
        key: str(value or "").strip()
        for key, value in dotenv_values(REPOSITORY_ROOT / ".env").items()
    }
    configured.update(
        {
            key: value
            for key, value in os.environ.items()
            if str(value).strip()
        }
    )
    providers = (
        ("openai", ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")),
        (
            "siliconflow",
            ("SILICONFLOW_API_KEY", "SILICONFLOW_BASE_URL", "SILICONFLOW_MODEL"),
        ),
    )
    for provider, required_names in providers:
        if all(configured.get(name) for name in required_names):
            return provider, {name: configured[name] for name in required_names}
    expected = "、".join(name for _, names in providers for name in names)
    raise RuntimeError(f"模型配置不完整，请在 .env 中配置一组 Provider：{expected}。")


def _preflight(docker: str) -> dict[str, object]:
    """功能：确认 Docker 与 Agent 镜像可用；输入：Docker 路径；输出：镜像元数据。"""
    daemon = _completed([docker, "version", "--format", "{{.Server.Version}}"], capture_output=True)
    if daemon.returncode != 0:
        message = daemon.stderr.strip() or daemon.stdout.strip()
        raise RuntimeError(f"Docker daemon 不可用：{message}")
    image = _completed([docker, "image", "inspect", IMAGE], capture_output=True)
    if image.returncode != 0:
        raise RuntimeError(
            f"缺少包含 Codini 的评测镜像 `{IMAGE}`；"
            "请先按 benchmarks/polyglot/README.md 构建一次。"
        )
    image_payload = json.loads(image.stdout)[0]
    labels = image_payload.get("Config", {}).get("Labels", {}) or {}
    if labels.get("io.codini.polyglot.agent") != "true":
        raise RuntimeError(f"镜像 `{IMAGE}` 不包含 Docker Agent，请重新构建。")
    return {
        "tag": IMAGE,
        "id": image_payload.get("Id", ""),
        "created": image_payload.get("Created", ""),
        "polyglot_revision": labels.get("io.codini.polyglot.revision", ""),
    }


def _git_revision() -> str:
    """功能：读取当前代码版本；输入：仓库；输出：带 dirty 标记的 Git 提交。"""
    revision = _completed(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
    )
    if revision.returncode != 0:
        return "unknown"
    status = _completed(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
    )
    suffix = "-dirty" if status.returncode == 0 and status.stdout.strip() else ""
    return f"{revision.stdout.strip()}{suffix}"


def _dataset_digest(tasks: list[object]) -> str:
    """功能：汇总题库版本；输入：有序任务列表；输出：稳定 SHA-256 摘要。"""
    digest = hashlib.sha256()
    for task in tasks:
        digest.update(task.id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(task.dataset_tree_sha256.encode("ascii"))
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _load_agent_report(workspace: Path) -> dict[str, object]:
    """功能：读取当前题目的根 Agent 报告；输入：题目工作区；输出：报告字典或空字典。"""
    reports: list[tuple[Path, dict[str, object]]] = []
    for path in (workspace / ".codini" / "sessions").glob("*/report.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        task_state = payload.get("task_state") or {}
        if isinstance(task_state, dict) and int(task_state.get("depth", 0) or 0) == 0:
            reports.append((path, payload))
    if not reports:
        return {}
    return max(reports, key=lambda item: item[0].stat().st_mtime_ns)[1]


def _agent_metrics(report: dict[str, object]) -> dict[str, object]:
    """功能：提取 Codini 指标；输入：根 Agent report；输出：稳定、可聚合的指标字典。"""
    summary = report.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    tokens = summary.get("tokens") or {}
    latency = summary.get("latency") or {}
    task_state = report.get("task_state") or {}
    return {
        "run_id": report.get("run_id", ""),
        "status": report.get("status", ""),
        "stop_reason": report.get("stop_reason", ""),
        "attempts": int(report.get("attempts", 0) or 0),
        "tool_steps": int(report.get("tool_steps", 0) or 0),
        "model": task_state.get("model", "") if isinstance(task_state, dict) else "",
        "provider": task_state.get("provider", "") if isinstance(task_state, dict) else "",
        "tokens": {
            "prompt": int(tokens.get("prompt", 0) or 0),
            "completion": int(tokens.get("completion", 0) or 0),
            "total": int(tokens.get("total", 0) or 0),
            "cached": int(tokens.get("cached", 0) or 0),
        },
        "latency": {
            "model_ms": int(latency.get("model_ms", 0) or 0),
            "tool_ms": int(latency.get("tool_ms", 0) or 0),
            "model_calls": int(latency.get("count_model", 0) or 0),
            "tool_calls": int(latency.get("count_tool", 0) or 0),
        },
        "tools": summary.get("tools", []),
        "step_budget": summary.get("step_budget", {}),
        "response_correction_count": int(
            summary.get("response_correction_count", 0) or 0
        ),
        "response_corrections": report.get(
            "response_corrections",
            summary.get("response_corrections", []),
        ),
        "error": report.get("error", {}),
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    """功能：原子写入 JSON；输入：目标路径和字典；输出：无。"""
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _write_case(path: Path, row: dict[str, object]) -> None:
    """功能：追加单题结果；输入：JSONL 路径和结果；输出：无。"""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def _summary(
    run_metadata: dict[str, object],
    rows: list[dict[str, object]],
    total_cases: int,
    started_at: float,
    finished: bool,
) -> dict[str, object]:
    """功能：聚合官方兼容与 Codini 指标；输入：运行信息、单题结果和计时；输出：汇总报告。"""
    passed = sum(bool(row["passed"]) for row in rows)
    completed = len(rows)
    total_duration_ms = sum(int(row["duration_ms"]) for row in rows)
    tokens = Counter()
    latency = Counter()
    tools = Counter()
    stop_reasons = Counter()
    statuses = Counter()
    correction_types = Counter()
    agent_attempts = 0
    tool_steps = 0
    response_correction_count = 0
    for row in rows:
        agent = row["agent"]
        tokens.update(agent["tokens"])
        latency.update(agent["latency"])
        agent_attempts += int(agent.get("attempts", 0) or 0)
        tool_steps += int(agent.get("tool_steps", 0) or 0)
        response_correction_count += int(
            agent.get("response_correction_count", 0) or 0
        )
        stop_reasons[str(agent.get("stop_reason") or "missing_report")] += 1
        statuses[str(row["status"])] += 1
        for correction in agent.get("response_corrections", []):
            if isinstance(correction, dict):
                correction_types[str(correction.get("error_type") or "unknown")] += 1
        for item in agent.get("tools", []):
            if isinstance(item, (list, tuple)) and len(item) == 2:
                tools[str(item[0])] += int(item[1])
    return {
        **run_metadata,
        "finished": finished,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds") if finished else None,
        "test_cases": total_cases,
        "completed_cases": completed,
        "remaining_cases": total_cases - completed,
        "pass_num_1": passed,
        "fail_num_1": completed - passed,
        "pass_rate_1": round(100 * passed / completed, 2) if completed else 0.0,
        "attempts_per_case": 1,
        "agent_attempts": agent_attempts,
        "tool_steps": tool_steps,
        "response_correction_count": response_correction_count,
        "response_correction_types": dict(correction_types),
        "test_timeouts": statuses["agent_timeout"] + statuses["verifier_timeout"],
        "error_outputs": statuses["agent_error"] + statuses["verifier_error"],
        "seconds_per_case": round(total_duration_ms / completed / 1000, 3) if completed else 0.0,
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
        "status_counts": dict(statuses),
        "stop_reason_counts": dict(stop_reasons),
        "tokens": dict(tokens),
        "latency": dict(latency),
        "tool_usage": dict(tools),
    }


def _run_agent(
    docker: str,
    task: object,
    workspace: Path,
    provider: str,
    provider_environment: dict[str, str],
) -> int:
    """功能：在受限容器内运行 Codini；输入：Docker、任务、工作区和模型配置；输出：Agent 退出码。"""
    container_name = f"codini-polyglot-agent-{uuid.uuid4().hex[:12]}"
    mount = f"type=bind,source={workspace.resolve()},target=/workspace"
    environment = os.environ.copy()
    environment.update(provider_environment)
    command = [
        docker,
        "run",
        "--name",
        container_name,
        "--rm",
        "--network",
        "bridge",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "256",
        "--memory",
        "2g",
        "--cpus",
        "2.0",
        "--user",
        "1000:1000",
        "--tmpfs",
        "/tmp:rw,nosuid,size=512m",
        "--mount",
        mount,
        "--workdir",
        "/workspace",
        "--env",
        "HOME=/tmp/home",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "PYTHONUNBUFFERED=1",
    ]
    for name in provider_environment:
        command.extend(["--env", name])
    command.extend(
        [
            IMAGE,
            "codini",
            "--cwd",
            "/workspace",
            "--approval",
            "auto",
            "--provider",
            provider,
            "--max-steps",
            str(AGENT_INITIAL_STEPS),
            "--no-trace-live",
            task.prompt,
        ]
    )
    try:
        completed = _completed(command, env=environment, timeout=AGENT_TIMEOUT_SECONDS)
        return completed.returncode
    except subprocess.TimeoutExpired:
        print(
            f"{task.id}: agent timeout after {AGENT_TIMEOUT_SECONDS}s",
            file=sys.stderr,
        )
        return 124
    finally:
        _completed([docker, "rm", "-f", container_name], capture_output=True)


def main() -> int:
    """功能：运行全部 smoke 题并清理现场；输入：无；输出：0 全部通过，否则返回 1。"""
    from polyglot.isolation import create_isolated_workspace
    from polyglot.task_adapter import TaskAdapterError, load_tasks
    from polyglot.verifier import DockerVerifier

    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    program_files = Path(os.environ.get("ProgramFiles", ""))
    try:
        docker = _find_executable(
            "docker",
            (
                local_app_data
                / "Programs"
                / "DockerDesktop"
                / "resources"
                / "bin"
                / "docker.exe",
                program_files
                / "Docker"
                / "Docker"
                / "resources"
                / "bin"
                / "docker.exe",
            ),
        )
        provider, provider_environment = _provider_from_environment()
        image_metadata = _preflight(docker)
        tasks = load_tasks(DATASET_ROOT)
    except (OSError, RuntimeError, TaskAdapterError) as exc:
        print(f"Preflight failed: {exc}", file=sys.stderr)
        return 2

    verifier = DockerVerifier(image=IMAGE, docker_executable=docker)
    passed = 0
    started_at = time.monotonic()
    started_datetime = datetime.now().astimezone()
    run_name = started_datetime.strftime("%Y-%m-%d-%H-%M-%S--codini-python")
    result_directory = RESULTS_ROOT / run_name
    result_directory.mkdir(parents=True)
    cases_path = result_directory / "cases.jsonl"
    summary_path = result_directory / "summary.json"
    rows: list[dict[str, object]] = []
    model_env_name = "OPENAI_MODEL" if provider == "openai" else "SILICONFLOW_MODEL"
    run_metadata: dict[str, object] = {
        "schema_version": 1,
        "benchmark": "Codini on Aider Polyglot - Python subset",
        "run_name": run_name,
        "date": started_datetime.date().isoformat(),
        "started_at": started_datetime.isoformat(timespec="seconds"),
        "language": "python",
        "model": provider_environment[model_env_name],
        "provider": provider,
        "commit_hash": _git_revision(),
        "dataset_digest": _dataset_digest(tasks),
        "image": image_metadata,
        "command": "python benchmarks/run_polyglot.py",
        "agent_budget": {
            "initial_tool_steps": AGENT_INITIAL_STEPS,
            "dynamic_hard_limit": max(
                AGENT_INITIAL_STEPS * 3,
                AGENT_INITIAL_STEPS + 6,
            ),
            "timeout_seconds": AGENT_TIMEOUT_SECONDS,
            "no_progress_stop_count": 5,
        },
    }
    _write_json(summary_path, _summary(run_metadata, rows, len(tasks), started_at, False))

    with tempfile.TemporaryDirectory(prefix="codini-polyglot-") as temporary_root:
        isolation_root = Path(temporary_root)
        for index, task in enumerate(tasks, start=1):
            print(f"\n[{index}/{len(tasks)}] {task.id}")
            workspace = create_isolated_workspace(
                task,
                BENCHMARK_ROOT,
                isolation_root=isolation_root,
                run_id="attempt-1",
            )
            try:
                case_started_at = time.monotonic()
                agent_exit_code = _run_agent(
                    docker,
                    task,
                    workspace.agent_workspace,
                    provider,
                    provider_environment,
                )
                agent_report = _load_agent_report(workspace.agent_workspace)
                agent_metrics = _agent_metrics(agent_report)
                result = verifier.verify(
                    task,
                    workspace.agent_workspace,
                    BENCHMARK_ROOT,
                )
                case_passed = agent_exit_code == 0 and result.passed
                if case_passed:
                    passed += 1
                if agent_exit_code == 124:
                    status = "agent_timeout"
                elif agent_exit_code != 0:
                    status = "agent_error"
                elif result.status == "timeout":
                    status = "verifier_timeout"
                elif result.status == "error":
                    status = "verifier_error"
                else:
                    status = result.status
                row: dict[str, object] = {
                    "task_id": task.id,
                    "passed": case_passed,
                    "status": status,
                    "duration_ms": int((time.monotonic() - case_started_at) * 1000),
                    "agent_exit_code": agent_exit_code,
                    "agent": agent_metrics,
                    "verifier": result.to_dict(),
                }
                rows.append(row)
                _write_case(cases_path, row)
                _write_json(
                    summary_path,
                    _summary(run_metadata, rows, len(tasks), started_at, False),
                )
                print(
                    f"{task.id}: {status} "
                    f"(agent_exit={agent_exit_code}, verifier_exit={result.exit_code})"
                )
                if result.violations:
                    print("integrity violations:", ", ".join(result.violations))
                if result.stdout.strip():
                    print(result.stdout.rstrip())
                if result.stderr.strip():
                    print(result.stderr.rstrip(), file=sys.stderr)
            finally:
                workspace.remove()

    _write_json(summary_path, _summary(run_metadata, rows, len(tasks), started_at, True))
    print(f"\nSummary: {passed}/{len(tasks)} passed")
    print(f"Results: {result_directory}")
    return 0 if passed == len(tasks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
