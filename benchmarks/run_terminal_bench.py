"""以单一入口运行 Codini 的 Terminal-Bench 2.1 评测。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import TextIO

sys.dont_write_bytecode = True

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
RESULTS_ROOT = REPOSITORY_ROOT / "benchmarks" / "results"
BENCHMARK_ROOT = REPOSITORY_ROOT / "benchmarks" / "terminal_bench"
DATASET_ROOT = BENCHMARK_ROOT / "dataset"
DEFAULT_MAX_STEPS = 20
AGENT_EXECUTION_TIMEOUT_SECONDS = 875
MODEL_REQUEST_TIMEOUT_SECONDS = 120
AGENT_SETUP_TIMEOUT_MULTIPLIER = 2.0
ENVIRONMENT_BUILD_TIMEOUT_MULTIPLIER = 2.0
VERIFIER_TIMEOUT_MULTIPLIER = 2.0
INFRASTRUCTURE_RETRIES = 1
IMAGE_PULL_ATTEMPTS = 3


def _find_executable(name: str, fallbacks: tuple[Path, ...]) -> str:
    """功能：定位外部程序；输入：命令名和候选路径；输出：可执行文件绝对路径。"""
    executable = shutil.which(name)
    if executable:
        return executable
    for candidate in fallbacks:
        if candidate.is_file():
            return str(candidate.resolve())
    raise RuntimeError(f"找不到 `{name}`，请先完成对应环境安装。")


def _prepend_environment_entry(
    environment: dict[str, str],
    variable_name: str,
    value: str,
) -> None:
    """功能：向路径类环境变量前置目录；输入：环境、变量名和目录；输出：无。"""
    actual_key = next(
        (key for key in environment if key.upper() == variable_name.upper()),
        variable_name,
    )
    entries = [
        entry
        for entry in environment.get(actual_key, "").split(os.pathsep)
        if entry
    ]
    normalized_value = str(Path(value).resolve())
    if normalized_value.casefold() not in {entry.casefold() for entry in entries}:
        entries.insert(0, normalized_value)
    environment[actual_key] = os.pathsep.join(entries)


def _prepare_harbor_environment(
    environment: dict[str, str],
    docker: str,
) -> None:
    """功能：配置 Harbor 子进程环境；输入：环境字典和 Docker 路径；输出：无。"""
    _prepend_environment_entry(environment, "PATH", str(Path(docker).parent))
    _prepend_environment_entry(environment, "PYTHONPATH", str(REPOSITORY_ROOT))
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"


def _load_provider_environment() -> tuple[str, str, dict[str, str]]:
    """功能：读取模型配置；输入：仓库 .env 和进程环境；输出：Provider、模型名和子进程环境。"""
    from dotenv import dotenv_values

    configured = {
        key: str(value or "").strip()
        for key, value in dotenv_values(REPOSITORY_ROOT / ".env").items()
    }
    configured.update(
        {
            key: str(value).strip()
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
            environment = os.environ.copy()
            environment.update(
                {name: configured[name] for name in required_names}
            )
            return provider, configured[required_names[-1]], environment
    expected = " 或 ".join("、".join(names) for _, names in providers)
    raise RuntimeError(f"模型配置不完整，请在 .env 中配置：{expected}。")


def _run_logged(
    command: list[str],
    log: TextIO,
    *,
    environment: dict[str, str] | None = None,
) -> int:
    """功能：执行命令并同步写入终端和日志；输入：参数、日志及环境；输出：进程退出码。"""
    display = subprocess.list2cmdline(command)
    print(f"\n$ {display}")
    log.write(f"\n$ {display}\n")
    log.flush()
    process = subprocess.Popen(
        command,
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    try:
        for line in process.stdout:
            print(line, end="")
            log.write(line)
            log.flush()
    except KeyboardInterrupt:
        process.terminate()
        process.wait()
        raise
    return process.wait()


def _wheel_is_current(wheel: Path) -> bool:
    """功能：判断 wheel 是否对应当前源码；输入：wheel 路径；输出：是否可直接复用。"""
    sources = [
        REPOSITORY_ROOT / "pyproject.toml",
        REPOSITORY_ROOT / "LICENSE",
        *list((REPOSITORY_ROOT / "codini").rglob("*.py")),
        *list((REPOSITORY_ROOT / "codini").rglob("*.html")),
    ]
    source_mtime = max(
        path.stat().st_mtime_ns for path in sources if path.is_file()
    )
    return wheel.stat().st_mtime_ns >= source_mtime


def _prepare_wheel(uv: str, log: TextIO) -> Path:
    """功能：复用或构建 Codini wheel；输入：uv 路径和日志；输出：当前 wheel 路径。"""
    distribution = REPOSITORY_ROOT / "dist"
    wheels = sorted(
        distribution.glob("codini-*.whl"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if wheels and _wheel_is_current(wheels[0]):
        print(f"复用 wheel：{wheels[0]}")
        log.write(f"复用 wheel：{wheels[0]}\n")
        log.flush()
        return wheels[0].resolve()

    exit_code = _run_logged(
        [uv, "build", "--wheel", "--out-dir", str(distribution)],
        log,
    )
    if exit_code != 0:
        raise RuntimeError(f"Codini wheel 构建失败，退出码：{exit_code}")
    wheels = sorted(
        distribution.glob("codini-*.whl"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not wheels:
        raise RuntimeError("uv build 成功，但 dist 中没有找到 Codini wheel。")
    return wheels[0].resolve()


def _runtime_bundle_is_current(bundle: Path, wheel: Path) -> bool:
    """功能：判断离线运行包是否匹配当前 wheel；输入：运行包和 wheel；输出：能否复用。"""
    return bundle.is_file() and bundle.stat().st_mtime_ns >= wheel.stat().st_mtime_ns


def _prepare_runtime_bundle(uv: str, wheel: Path, log: TextIO) -> Path:
    """功能：生成 Codini 离线运行包；输入：uv、wheel 和日志；输出：压缩包路径。"""
    distribution = REPOSITORY_ROOT / "dist"
    distribution.mkdir(exist_ok=True)
    bundle = distribution / "codini-terminal-bench-runtime.tar.gz"
    if _runtime_bundle_is_current(bundle, wheel):
        print(f"复用 Codini 离线运行包：{bundle}")
        log.write(f"复用 Codini 离线运行包：{bundle}\n")
        log.flush()
        return bundle.resolve()

    with tempfile.TemporaryDirectory(
        prefix="codini-tbench-",
        dir=distribution,
    ) as temporary:
        runtime_root = Path(temporary) / "runtime"
        exit_code = _run_logged(
            [
                uv,
                "pip",
                "install",
                "--target",
                str(runtime_root),
                "--python-platform",
                "x86_64-unknown-linux-gnu",
                "--python-version",
                "3.10",
                "--no-cache",
                str(wheel),
            ],
            log,
        )
        if exit_code != 0:
            raise RuntimeError(f"Codini 离线运行包构建失败，退出码：{exit_code}")
        temporary_bundle = Path(temporary) / bundle.name
        with tarfile.open(temporary_bundle, "w:gz") as archive:
            for path in sorted(runtime_root.rglob("*")):
                archive.add(path, arcname=path.relative_to(runtime_root))
        temporary_bundle.replace(bundle)
    return bundle.resolve()


def _task_docker_image(task_name: str) -> str:
    """功能：读取任务预构建镜像；输入：任务目录名；输出：镜像名称或空字符串。"""
    task_config = DATASET_ROOT / task_name / "task.toml"
    content = task_config.read_text(encoding="utf-8")
    match = re.search(r'^docker_image\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _image_exists(
    docker: str,
    image: str,
    environment: dict[str, str],
) -> bool:
    """功能：检查 Docker 镜像是否已缓存；输入：Docker、镜像和环境；输出：是否存在。"""
    result = subprocess.run(
        [docker, "image", "inspect", image],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _prepare_task_images(
    docker: str,
    task_names: list[str],
    log: TextIO,
    environment: dict[str, str],
    records: list[dict[str, object]],
) -> list[dict[str, object]]:
    """功能：预拉取缺失镜像；输入：Docker、任务、日志和环境；输出：准备记录。"""
    images = sorted({_task_docker_image(name) for name in task_names} - {""})
    for image in images:
        started = time.monotonic()
        if _image_exists(docker, image, environment):
            print(f"复用任务镜像：{image}")
            log.write(f"复用任务镜像：{image}\n")
            records.append(
                {
                    "image": image,
                    "status": "cached",
                    "pull_attempts": 0,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            )
            continue

        exit_code = 1
        attempts = 0
        for attempts in range(1, IMAGE_PULL_ATTEMPTS + 1):
            exit_code = _run_logged(
                [docker, "pull", image],
                log,
                environment=environment,
            )
            if exit_code == 0:
                break
            log.write(
                f"镜像拉取失败：{image}（第 {attempts}/{IMAGE_PULL_ATTEMPTS} 次）\n"
            )
            log.flush()
        if exit_code != 0:
            records.append(
                {
                    "image": image,
                    "status": "failed",
                    "pull_attempts": attempts,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            )
            raise RuntimeError(
                f"任务镜像拉取失败：{image}；已尝试 {IMAGE_PULL_ATTEMPTS} 次。"
            )
        records.append(
            {
                "image": image,
                "status": "pulled",
                "pull_attempts": attempts,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
    return records


def _write_summary(path: Path, payload: dict[str, object]) -> None:
    """功能：写入本次评测汇总；输入：目标路径和指标；输出：无。"""
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json_object(path: Path) -> dict[str, object]:
    """功能：安全读取 JSON 对象；输入：文件路径；输出：字典或空字典。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _duration_seconds(started_at: object, finished_at: object) -> float | None:
    """功能：计算 trial 用时；输入：起止 ISO 时间；输出：秒数或空值。"""
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        finished = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return round((finished - started).total_seconds(), 3)


def _latest_jsonl_entry(path: Path) -> dict[str, object]:
    """功能：读取 JSONL 最后一条有效记录；输入：文件路径；输出：字典。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _codini_trial_metrics(trial_directory: Path) -> dict[str, object]:
    """功能：提取单题 Codini 状态；输入：trial 目录；输出：运行指标和工件位置。"""
    agent_directory = trial_directory / "agent"
    state_root = agent_directory / "codini-state"
    report_candidates = sorted(
        (state_root / "sessions").glob("*/report.json"),
        key=lambda path: path.stat().st_mtime_ns,
    )
    task_state_candidates = sorted(
        (state_root / "sessions").glob("*/task_state.json"),
        key=lambda path: path.stat().st_mtime_ns,
    )
    payload: dict[str, object] = {}
    artifact_source = ""
    if report_candidates:
        payload = _read_json_object(report_candidates[-1])
        artifact_source = str(report_candidates[-1])
    elif task_state_candidates:
        payload = _read_json_object(task_state_candidates[-1])
        artifact_source = str(task_state_candidates[-1])
    else:
        legacy_index = agent_directory / "codini-runs" / "index.jsonl"
        payload = _latest_jsonl_entry(legacy_index)
        if payload:
            artifact_source = str(legacy_index)

    nested_state = payload.get("task_state") or {}
    if not isinstance(nested_state, dict):
        nested_state = {}
    summary = payload.get("summary") or nested_state.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    corrections = summary.get("response_corrections") or payload.get(
        "response_corrections",
        [],
    )
    correction_types: dict[str, int] = {}
    if isinstance(corrections, list):
        for correction in corrections:
            if not isinstance(correction, dict):
                continue
            error_type = str(correction.get("error_type") or "unknown")
            correction_types[error_type] = correction_types.get(error_type, 0) + 1

    supervisor = _read_json_object(agent_directory / "supervisor.json")
    return {
        "artifact_source": artifact_source,
        "state_saved": bool(payload),
        "run_id": payload.get("run_id") or nested_state.get("run_id", ""),
        "status": payload.get("status") or nested_state.get("status", ""),
        "stop_reason": payload.get("stop_reason")
        or nested_state.get("stop_reason", ""),
        "attempts": int(
            summary.get("attempts", payload.get("attempts", 0)) or 0
        ),
        "tool_steps": int(
            summary.get("tool_steps", payload.get("tool_steps", 0)) or 0
        ),
        "tokens": summary.get("tokens", {}),
        "latency": summary.get("latency", {}),
        "tools": summary.get("tools", []),
        "response_correction_count": int(
            summary.get("response_correction_count", len(corrections or [])) or 0
        ),
        "response_correction_types": correction_types,
        "supervisor": supervisor,
    }


def _verifier_infrastructure_error(trial_directory: Path) -> str:
    """功能：识别 verifier 自身依赖故障；输入：trial 目录；输出：证据或空字符串。"""
    output_path = trial_directory / "verifier" / "test-stdout.txt"
    try:
        output = output_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    markers = (
        "uvx: command not found",
        "SSL_ERROR_SYSCALL in connection to releases.astral.sh",
        "Could not resolve host",
        "Temporary failure in name resolution",
        "No space left on device",
    )
    matched = [marker for marker in markers if marker in output]
    return "; ".join(matched)


def _classify_trial(trial_directory: Path) -> dict[str, object]:
    """功能：分类单题结果；输入：Harbor trial 目录；输出：稳定诊断记录。"""
    result_path = trial_directory / "result.json"
    result = _read_json_object(result_path)
    exception = result.get("exception_info") or {}
    if not isinstance(exception, dict):
        exception = {}
    exception_type = str(exception.get("exception_type") or "")
    exception_message = str(exception.get("exception_message") or "")
    exception_traceback = str(exception.get("exception_traceback") or "")
    verifier_result = result.get("verifier_result") or {}
    if not isinstance(verifier_result, dict):
        verifier_result = {}
    rewards = verifier_result.get("rewards") or {}
    if not isinstance(rewards, dict):
        rewards = {}
    raw_reward = rewards.get("reward")
    reward = float(raw_reward) if isinstance(raw_reward, (int, float)) else None
    verifier_error = _verifier_infrastructure_error(trial_directory)
    codini_metrics = _codini_trial_metrics(trial_directory)
    supervisor = codini_metrics.get("supervisor") or {}
    if not isinstance(supervisor, dict):
        supervisor = {}
    if supervisor.get("primary_timed_out") is True:
        supervisor_timeout_reason = "primary_execution_timeout"
    elif supervisor.get("audit_timed_out") is True:
        supervisor_timeout_reason = "completion_audit_timeout"
    else:
        supervisor_timeout_reason = ""

    if not result:
        classification = "not_run"
        reason = "missing_trial_result"
    elif reward is not None and reward >= 1.0:
        classification = "passed"
        if supervisor_timeout_reason:
            reason = f"verifier_reward_1_after_{supervisor_timeout_reason}"
        elif exception_type == "AgentTimeoutError":
            reason = "verifier_reward_1_after_agent_timeout"
        else:
            reason = "verifier_reward_1"
    elif exception_type.startswith("Environment") or exception_type.startswith(
        "AgentSetup"
    ):
        classification = "environment_error"
        reason = exception_type
    elif exception_type.startswith("Verifier"):
        classification = "verifier_error"
        reason = exception_type
    elif exception_type.startswith("Agent"):
        classification = "agent_failed"
        reason = exception_type
    elif "benchmarks\\terminal_bench\\agent.py" in exception_traceback or (
        "benchmarks/terminal_bench/agent.py" in exception_traceback
    ):
        classification = "adapter_error"
        reason = exception_type or "terminal_bench_adapter_error"
    elif verifier_error:
        classification = "verifier_error"
        reason = verifier_error
    elif supervisor_timeout_reason:
        classification = "agent_failed"
        reason = supervisor_timeout_reason
    else:
        classification = "agent_failed"
        reason = "verifier_reward_below_1"

    return {
        "task": str(result.get("task_name") or trial_directory.name.split("__", 1)[0]),
        "trial": trial_directory.name,
        "classification": classification,
        "reason": reason,
        "reward": reward,
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
        "duration_seconds": _duration_seconds(
            result.get("started_at"),
            result.get("finished_at"),
        ),
        "exception_type": exception_type,
        "exception_message": exception_message,
        "result_file": str(result_path),
        "codini": codini_metrics,
    }


def _collect_trial_results(job_directory: Path) -> list[dict[str, object]]:
    """功能：收集并分类全部 trial；输入：Harbor job 目录；输出：逐题诊断列表。"""
    if not job_directory.is_dir():
        return []
    return [
        _classify_trial(trial_directory)
        for trial_directory in sorted(job_directory.iterdir())
        if trial_directory.is_dir()
    ]


def _classification_summary(
    trials: list[dict[str, object]],
    planned_trials: int,
) -> dict[str, object]:
    """功能：聚合结果分类；输入：逐题记录和计划数；输出：官方与诊断指标。"""
    counts = {
        "passed": 0,
        "agent_failed": 0,
        "environment_error": 0,
        "verifier_error": 0,
        "adapter_error": 0,
        "not_run": 0,
    }
    reward_sum = 0.0
    for trial in trials:
        classification = str(trial.get("classification") or "environment_error")
        counts[classification] = counts.get(classification, 0) + 1
        reward = trial.get("reward")
        if isinstance(reward, (int, float)):
            reward_sum += float(reward)
    counts["not_run"] += max(planned_trials - len(trials), 0)
    completed = planned_trials - counts["not_run"]
    evaluable = counts["passed"] + counts["agent_failed"]
    excluded = (
        counts["environment_error"]
        + counts["verifier_error"]
        + counts["adapter_error"]
    )
    return {
        "counts": counts,
        "official": {
            "passed": counts["passed"],
            "total": completed,
            "planned_trials": planned_trials,
            "complete": counts["not_run"] == 0,
            "reward_sum": reward_sum,
            "pass_rate": round(100 * counts["passed"] / completed, 2)
            if completed
            else 0.0,
        },
        "diagnostic": {
            "passed": counts["passed"],
            "agent_evaluable_trials": evaluable,
            "excluded_infrastructure_trials": excluded,
            "not_run_trials": counts["not_run"],
            "pass_rate": round(100 * counts["passed"] / evaluable, 2)
            if evaluable
            else 0.0,
        },
    }


def _write_trial_results(path: Path, trials: list[dict[str, object]]) -> None:
    """功能：原子写入逐题 JSONL；输入：文件路径和逐题结果；输出：无。"""
    temporary = path.with_suffix(".jsonl.tmp")
    temporary.write_text(
        "".join(
            json.dumps(trial, ensure_ascii=False, sort_keys=True) + "\n"
            for trial in trials
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _discover_local_tasks() -> list[str]:
    """功能：检查并列出本地题库；输入：固定数据集目录；输出：结构完整的任务目录名。"""
    if not DATASET_ROOT.is_dir():
        raise RuntimeError(f"本地题库不存在：{DATASET_ROOT}")
    required_paths = (
        "instruction.md",
        "task.toml",
        "tests/test.sh",
    )
    task_names: list[str] = []
    for task_directory in sorted(DATASET_ROOT.iterdir()):
        if not task_directory.is_dir():
            continue
        if not (task_directory / "task.toml").is_file():
            continue
        missing = [
            relative
            for relative in required_paths
            if not (task_directory / relative).is_file()
        ]
        if missing:
            raise RuntimeError(
                f"任务 {task_directory.name} 缺少文件：{', '.join(missing)}"
            )
        environment_files = (
            task_directory / "environment" / "Dockerfile",
            task_directory / "environment" / "docker-compose.yaml",
            task_directory / "environment" / "docker-compose.yml",
        )
        if not any(path.is_file() for path in environment_files) and not (
            _task_docker_image(task_directory.name)
        ):
            raise RuntimeError(
                f"任务 {task_directory.name} 缺少 Dockerfile、"
                "docker-compose.yaml 或 docker_image 配置。"
            )
        task_names.append(task_directory.name)
    if not task_names:
        raise RuntimeError(f"本地题库中没有可运行任务：{DATASET_ROOT}")
    return task_names


def _build_parser() -> argparse.ArgumentParser:
    """功能：构建评测参数；输入：无；输出：参数解析器。"""
    parser = argparse.ArgumentParser(
        description="运行 Codini 的本地 Terminal-Bench 2.1 评测。"
    )
    parser.add_argument(
        "--task",
        default=None,
        help="只运行指定任务；省略时运行本地题库中的全部任务。",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=1,
        help="该任务的独立尝试次数。",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="并发 trial 数。",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_MAX_STEPS,
        help="Codini 初始工具步数预算。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """功能：完成预检并运行 Harbor；输入：可选命令行参数；输出：评测进程退出码。"""
    args = _build_parser().parse_args(argv)
    if args.attempts < 1 or args.concurrency < 1 or args.max_steps < 1:
        print("attempts、concurrency 和 max-steps 必须大于 0。", file=sys.stderr)
        return 2

    started_at = time.monotonic()
    started_datetime = datetime.now().astimezone()
    run_name = started_datetime.strftime("%Y-%m-%d-%H-%M-%S--terminal-bench")
    result_directory = RESULTS_ROOT / run_name
    jobs_directory = result_directory / "jobs"
    result_directory.mkdir(parents=True)
    jobs_directory.mkdir()
    log_path = result_directory / "run.log"
    summary_path = result_directory / "summary.json"
    cases_path = result_directory / "cases.jsonl"
    exit_code = 2
    error = ""
    model = ""
    provider = ""
    wheel = ""
    runtime_bundle = ""
    local_tasks: list[str] = []
    selected_tasks: list[str] = []
    image_preparation: list[dict[str, object]] = []

    print(f"结果目录：{result_directory}")
    with log_path.open("w", encoding="utf-8") as log:
        try:
            local_tasks = _discover_local_tasks()
            requested_task = (
                args.task.rsplit("/", 1)[-1].strip() if args.task else ""
            )
            if requested_task and requested_task not in local_tasks:
                raise RuntimeError(
                    f"本地题库中不存在任务 `{requested_task}`；"
                    f"可用任务：{', '.join(local_tasks)}"
                )
            selected_tasks = [requested_task] if requested_task else local_tasks
            print(
                f"本地题库：{DATASET_ROOT} "
                f"（发现 {len(local_tasks)} 题，本次运行 {len(selected_tasks)} 题）"
            )
            local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
            program_files = Path(os.environ.get("ProgramFiles", ""))
            user_profile = Path(os.environ.get("USERPROFILE", ""))
            harbor = _find_executable(
                "harbor",
                (user_profile / ".local" / "bin" / "harbor.exe",),
            )
            uv = _find_executable(
                "uv",
                (user_profile / ".local" / "bin" / "uv.exe",),
            )
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
            provider, model, environment = _load_provider_environment()
            _prepare_harbor_environment(environment, docker)
            log.write(
                f"harbor={harbor}\nuv={uv}\ndocker={docker}\n"
                f"provider={provider}\nmodel={model}\n"
                f"dataset={DATASET_ROOT}\n"
                f"tasks={','.join(selected_tasks)}\n"
            )
            log.flush()
            if _run_logged([docker, "version"], log, environment=environment) != 0:
                raise RuntimeError("Docker daemon 不可用。")
            if _run_logged([harbor, "--version"], log) != 0:
                raise RuntimeError("Harbor 不可用。")
            _prepare_task_images(
                docker,
                selected_tasks,
                log,
                environment,
                image_preparation,
            )
            wheel_path = _prepare_wheel(uv, log)
            wheel = str(wheel_path)
            runtime_bundle_path = _prepare_runtime_bundle(uv, wheel_path, log)
            runtime_bundle = str(runtime_bundle_path)
            harbor_model = f"{provider}/{model}"
            command = [
                harbor,
                "run",
                "-p",
                str(DATASET_ROOT),
                "--agent",
                "benchmarks.terminal_bench.agent:CodiniTerminalBenchAgent",
                "-m",
                harbor_model,
                "-e",
                "docker",
                "--ak",
                f"runtime_bundle_path={runtime_bundle_path}",
                "--ak",
                f"max_steps={args.max_steps}",
                "--ak",
                f"execution_timeout_seconds={AGENT_EXECUTION_TIMEOUT_SECONDS}",
                "--ak",
                f"model_timeout_seconds={MODEL_REQUEST_TIMEOUT_SECONDS}",
                "-k",
                str(args.attempts),
                "-n",
                str(args.concurrency),
                "--agent-setup-timeout-multiplier",
                str(AGENT_SETUP_TIMEOUT_MULTIPLIER),
                "--environment-build-timeout-multiplier",
                str(ENVIRONMENT_BUILD_TIMEOUT_MULTIPLIER),
                "--verifier-timeout-multiplier",
                str(VERIFIER_TIMEOUT_MULTIPLIER),
                "--max-retries",
                str(INFRASTRUCTURE_RETRIES),
                "--retry-include",
                "AgentSetupTimeoutError",
                "--retry-include",
                "EnvironmentStartTimeoutError",
                "--retry-include",
                "VerifierTimeoutError",
                "--job-name",
                run_name,
                "--jobs-dir",
                str(jobs_directory),
                "--yes",
            ]
            if requested_task:
                command.extend(["--include-task-name", requested_task])
            exit_code = _run_logged(command, log, environment=environment)
        except KeyboardInterrupt:
            exit_code = 130
            error = "用户中断"
            log.write(f"\n{error}\n")
        except (OSError, RuntimeError) as exc:
            error = str(exc)
            log.write(f"\n评测启动失败：{error}\n")
            print(f"\n评测启动失败：{error}", file=sys.stderr)

    if exit_code != 0 and not error:
        error = f"Harbor 运行失败，退出码：{exit_code}；具体原因见 run.log。"

    expected_job = jobs_directory / run_name
    job_candidates = [
        path for path in jobs_directory.iterdir() if path.is_dir()
    ]
    actual_job = (
        expected_job
        if expected_job.is_dir()
        else max(
            job_candidates,
            key=lambda path: path.stat().st_mtime_ns,
            default=expected_job,
        )
    )
    job_result_path = actual_job / "result.json"
    harbor_result: dict[str, object] = {}
    if job_result_path.is_file():
        try:
            loaded_result = json.loads(job_result_path.read_text(encoding="utf-8"))
            if isinstance(loaded_result, dict):
                harbor_result = loaded_result
        except (OSError, json.JSONDecodeError):
            pass
    trial_results = _collect_trial_results(actual_job)
    planned_trials_value = harbor_result.get("n_total_trials", len(trial_results))
    planned_trials = (
        int(planned_trials_value)
        if isinstance(planned_trials_value, (int, float))
        else len(trial_results)
    )
    result_classification = _classification_summary(
        trial_results,
        planned_trials,
    )
    _write_trial_results(cases_path, trial_results)
    summary: dict[str, object] = {
        "schema_version": 1,
        "benchmark": "Terminal-Bench 2.1",
        "dataset": str(DATASET_ROOT),
        "available_tasks": local_tasks,
        "selected_tasks": selected_tasks,
        "task_count": len(selected_tasks),
        "provider": provider,
        "model": model,
        "attempts": args.attempts,
        "concurrency": args.concurrency,
        "max_steps": args.max_steps,
        "wheel": wheel,
        "runtime_bundle": runtime_bundle,
        "image_preparation": image_preparation,
        "timeout_policy": {
            "agent_execution_multiplier": 1.0,
            "internal_execution_timeout_seconds": AGENT_EXECUTION_TIMEOUT_SECONDS,
            "model_request_timeout_seconds": MODEL_REQUEST_TIMEOUT_SECONDS,
            "agent_setup_multiplier": AGENT_SETUP_TIMEOUT_MULTIPLIER,
            "environment_build_multiplier": ENVIRONMENT_BUILD_TIMEOUT_MULTIPLIER,
            "verifier_multiplier": VERIFIER_TIMEOUT_MULTIPLIER,
            "infrastructure_retries": INFRASTRUCTURE_RETRIES,
            "retry_exception_types": [
                "AgentSetupTimeoutError",
                "EnvironmentStartTimeoutError",
                "VerifierTimeoutError",
            ],
        },
        "started_at": started_datetime.isoformat(timespec="seconds"),
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
        "exit_code": exit_code,
        "status": "completed" if exit_code == 0 else "failed",
        "error": error,
        "run_log": str(log_path),
        "harbor_job": str(actual_job),
        "harbor_result_file": str(job_result_path),
        "harbor_result": harbor_result,
        "cases_file": str(cases_path),
        "result_classification": result_classification,
        "trials": trial_results,
    }
    _write_summary(summary_path, summary)

    print(f"\n状态：{summary['status']}（exit={exit_code}）")
    print(f"过程日志：{log_path}")
    print(f"汇总结果：{summary_path}")
    print(f"逐题结果：{cases_path}")
    print(f"Harbor 原始结果：{actual_job}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
