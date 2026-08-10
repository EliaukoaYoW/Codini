"""Terminal-Bench 容器内的 Codini 进程监督与完成质量门禁。"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


MUTATION_TOOLS = {"patch_file", "write_file"}
VALIDATION_TOOLS = {"run_shell"}
AUDIT_MAX_STEPS = 6
AUDIT_TIMEOUT_SECONDS = 180
MINIMUM_AUDIT_SECONDS = 90
GRACEFUL_STOP_SECONDS = 5
_active_process: subprocess.Popen[bytes] | None = None


def _read_json_object(path: Path) -> dict[str, Any]:
    """功能：安全读取JSON对象；输入：文件路径；输出：字典或空字典。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    """功能：容错读取JSONL事件；输入：轨迹路径；输出：有效事件列表。"""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _latest_session(state_root: Path) -> Path | None:
    """功能：定位最近Codini会话；输入：状态根目录；输出：会话目录或空值。"""
    sessions_root = state_root / "sessions"
    candidates = [path for path in sessions_root.glob("*") if path.is_dir()]
    return max(candidates, key=lambda path: path.stat().st_mtime_ns, default=None)


def completion_audit_reason(state_root: Path) -> str:
    """功能：判断是否需要完成审计；输入：Codini状态目录；输出：原因或空字符串。"""
    session = _latest_session(state_root)
    if session is None:
        return "missing_session"
    report = _read_json_object(session / "report.json")
    if not report:
        return "missing_report"
    summary = report.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    stop_reason = str(summary.get("stop_reason") or report.get("stop_reason") or "")
    if stop_reason != "final_answer_returned":
        return stop_reason or "unfinished_run"
    tool_steps = int(summary.get("tool_steps", 0) or 0)
    if tool_steps == 0:
        return "final_without_tools"

    events = _read_jsonl_objects(session / "trace.jsonl")
    last_mutation = -1
    last_validation = -1
    for index, event in enumerate(events):
        if event.get("event") != "tool_executed":
            continue
        span_name = str(event.get("span_name") or "")
        tool_name = span_name.removeprefix("tool.")
        if tool_name in MUTATION_TOOLS:
            last_mutation = index
        if tool_name in VALIDATION_TOOLS:
            last_validation = index
    if last_mutation >= 0 and last_validation < last_mutation:
        return "mutation_without_followup_validation"

    tools = summary.get("tools") or []
    tool_counts = {
        str(item[0]): int(item[1])
        for item in tools
        if isinstance(item, list) and len(item) == 2
    }
    if any(tool_counts.get(name, 0) for name in MUTATION_TOOLS) and not any(
        tool_counts.get(name, 0) for name in VALIDATION_TOOLS
    ):
        return "mutation_without_validation"
    return "independent_completion_audit"


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """功能：分阶段终止进程组；输入：子进程；输出：无。"""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=GRACEFUL_STOP_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=GRACEFUL_STOP_SECONDS)


def _forward_output(stream: Any, log_path: Path) -> None:
    """功能：同步转发进程输出；输入：输出流和日志路径；输出：无。"""
    with log_path.open("ab") as log:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            log.write(chunk)
            log.flush()


def run_with_timeout(
    command: list[str],
    environment: dict[str, str],
    log_path: Path,
    timeout_seconds: float,
) -> tuple[int, bool]:
    """功能：限时执行并清理进程组；输入：命令、环境、日志和秒数；输出：退出码和超时标记。"""
    global _active_process
    process = subprocess.Popen(
        command,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _active_process = process
    assert process.stdout is not None
    pump = threading.Thread(
        target=_forward_output,
        args=(process.stdout, log_path),
        daemon=True,
    )
    pump.start()
    timed_out = False
    try:
        return_code = process.wait(timeout=max(timeout_seconds, 1.0))
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(process)
        return_code = 124
    finally:
        pump.join(timeout=GRACEFUL_STOP_SECONDS)
        _active_process = None
    return return_code, timed_out


def _handle_signal(signum: int, _frame: Any) -> None:
    """功能：转发外部终止信号；输入：信号与帧；输出：进程退出。"""
    if _active_process is not None:
        _terminate_process_group(_active_process)
    raise SystemExit(128 + signum)


def _codini_command(
    args: argparse.Namespace,
    prompt: str,
    *,
    resume: bool,
    audit: bool = False,
) -> list[str]:
    """功能：构造Codini命令；输入：参数、提示词和恢复标记；输出：参数列表。"""
    max_steps = min(args.max_steps, AUDIT_MAX_STEPS) if audit else args.max_steps
    command = [
        sys.executable,
        "-c",
        "from codini.cli import main; raise SystemExit(main())",
        "--headless",
        "--approval",
        "auto",
        "--sandbox",
        "none",
        "--provider",
        args.provider,
        "--model",
        args.model,
        "--max-steps",
        str(max_steps),
        "--max-new-tokens",
        str(args.max_new_tokens),
        "--temperature",
        str(args.temperature),
        "--openai-timeout",
        str(args.model_timeout_seconds),
        "--siliconflow-timeout",
        str(args.model_timeout_seconds),
    ]
    if resume:
        command.extend(["--resume", "latest"])
    command.append(prompt)
    return command


def _audit_prompt(original_prompt: str, reason: str) -> str:
    """功能：生成二次完成审计提示；输入：原任务和原因；输出：续跑提示词。"""
    return (
        f"{original_prompt}\n\n"
        "Completion audit: the previous pass did not provide sufficient execution "
        f"or validation evidence ({reason}). Continue from the current workspace. "
        "Inspect the actual state, complete every requested requirement, and run "
        "concrete validation commands against the produced files, interfaces, "
        "services, or outputs. Fix every observed failure before returning a final "
        "answer. Do not merely restate a plan or claim success without evidence."
    )


def _write_result(path: Path, payload: dict[str, Any]) -> None:
    """功能：原子保存监督结果；输入：路径和结果；输出：无。"""
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _build_parser() -> argparse.ArgumentParser:
    """功能：构造监督器参数；输入：无；输出：参数解析器。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-seconds", type=int, required=True)
    parser.add_argument("--model-timeout-seconds", type=int, required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-steps", type=int, required=True)
    parser.add_argument("--max-new-tokens", type=int, required=True)
    parser.add_argument("--temperature", required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--log-path", type=Path, required=True)
    parser.add_argument("--result-path", type=Path, required=True)
    parser.add_argument("--prompt", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """功能：监督主运行和一次审计续跑；输入：命令行参数；输出：监督器退出码。"""
    args = _build_parser().parse_args(argv)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    started = time.monotonic()
    deadline = started + args.timeout_seconds
    environment = os.environ.copy()
    result: dict[str, Any] = {
        "schema_version": 1,
        "timeout_seconds": args.timeout_seconds,
        "model_timeout_seconds": args.model_timeout_seconds,
        "primary_exit_code": None,
        "primary_timed_out": False,
        "audit_required": False,
        "audit_reason": "",
        "audit_exit_code": None,
        "audit_timed_out": False,
        "audit_timeout_seconds": None,
    }

    primary_code, primary_timed_out = run_with_timeout(
        _codini_command(args, args.prompt, resume=False),
        environment,
        args.log_path,
        deadline - time.monotonic(),
    )
    result["primary_exit_code"] = primary_code
    result["primary_timed_out"] = primary_timed_out

    remaining = deadline - time.monotonic()
    reason = "" if primary_timed_out else completion_audit_reason(args.state_root)
    if reason and remaining >= MINIMUM_AUDIT_SECONDS:
        result["audit_required"] = True
        result["audit_reason"] = reason
        audit_timeout = min(remaining, AUDIT_TIMEOUT_SECONDS)
        result["audit_timeout_seconds"] = round(audit_timeout, 3)
        print(f"\n[completion audit: {reason}]", flush=True)
        audit_code, audit_timed_out = run_with_timeout(
            _codini_command(
                args,
                _audit_prompt(args.prompt, reason),
                resume=True,
                audit=True,
            ),
            environment,
            args.log_path,
            audit_timeout,
        )
        result["audit_exit_code"] = audit_code
        result["audit_timed_out"] = audit_timed_out
    elif reason:
        result["audit_reason"] = f"{reason}:insufficient_time"

    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    result["timed_out"] = bool(
        result["primary_timed_out"] or result["audit_timed_out"]
    )
    _write_result(args.result_path, result)
    if result["timed_out"]:
        print("\n[Codini supervisor stopped the process group before Harbor timeout]", flush=True)
    if primary_code not in (0, 124):
        return int(primary_code)
    audit_code = result["audit_exit_code"]
    return int(audit_code) if audit_code not in (None, 0, 124) else 0


if __name__ == "__main__":
    raise SystemExit(main())
