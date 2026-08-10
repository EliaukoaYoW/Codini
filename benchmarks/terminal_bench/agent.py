"""Harbor Installed Agent adapter for running Codini inside task containers."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

from harbor.agents.installed.base import BaseInstalledAgent, CliFlag, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from .trajectory import build_atif_trajectory


DEFAULT_EXECUTION_TIMEOUT_SECONDS = 875
DEFAULT_MODEL_TIMEOUT_SECONDS = 120


def _evaluation_prompt(instruction: str) -> str:
    """功能：追加完成质量要求；输入：原始任务；输出：评测专用提示词。"""
    return (
        f"{instruction.rstrip()}\n\n"
        "Completion requirements:\n"
        "- Use tools to perform the requested work; a prose plan is not completion.\n"
        "- Verify the actual result after making changes. Run concrete commands "
        "that exercise the requested files, interfaces, services, or outputs.\n"
        "- Check every explicit path, field name, version, port, and observable "
        "behavior from the request rather than relying on assumptions.\n"
        "- If a command or check fails, inspect the failure and continue fixing it.\n"
        "- Return a final answer only after the observed validation succeeds. "
        "Never claim success based only on intended changes."
    )


def _read_jsonl_objects(path: Path) -> tuple[list[dict[str, Any]], int]:
    """功能：容错读取 JSONL；输入：文件路径；输出：有效对象与无效行数。"""
    objects: list[dict[str, Any]] = []
    malformed_lines = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return objects, malformed_lines
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            malformed_lines += 1
            continue
        if isinstance(payload, dict):
            objects.append(payload)
        else:
            malformed_lines += 1
    return objects, malformed_lines


class CodiniTerminalBenchAgent(BaseInstalledAgent):
    """功能：把 Codini 离线运行包接入 Harbor 任务容器；输入：任务指令与模型配置；输出：工作区修改和运行指标。"""

    CLI_FLAGS = [
        CliFlag("max_steps", cli="--max-steps", type="int", default=20),
        CliFlag("max_new_tokens", cli="--max-new-tokens", type="int", default=4096),
        CliFlag("temperature", cli="--temperature", type="str", default="0.2"),
    ]
    SUPPORTS_ATIF = True

    def __init__(
        self,
        *args: Any,
        runtime_bundle_path: str | None = None,
        execution_timeout_seconds: int = DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        model_timeout_seconds: int = DEFAULT_MODEL_TIMEOUT_SECONDS,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        configured_path = runtime_bundle_path or os.environ.get(
            "CODINI_TBENCH_RUNTIME_BUNDLE"
        )
        if not configured_path:
            raise ValueError(
                "Codini runtime bundle is required; pass "
                "--ak runtime_bundle_path=<path> or set "
                "CODINI_TBENCH_RUNTIME_BUNDLE"
            )
        self._runtime_bundle_path = Path(configured_path).resolve()
        if not self._runtime_bundle_path.is_file() or not str(
            self._runtime_bundle_path
        ).endswith(".tar.gz"):
            raise FileNotFoundError(
                f"Codini runtime bundle not found: {self._runtime_bundle_path}"
            )
        self._supervisor_path = Path(__file__).with_name("supervisor.py").resolve()
        if not self._supervisor_path.is_file():
            raise FileNotFoundError(
                f"Codini supervisor not found: {self._supervisor_path}"
            )
        self._execution_timeout_seconds = int(execution_timeout_seconds)
        self._model_timeout_seconds = int(model_timeout_seconds)
        if not 60 <= self._execution_timeout_seconds <= 890:
            raise ValueError("execution_timeout_seconds must be between 60 and 890")
        if not 15 <= self._model_timeout_seconds <= 300:
            raise ValueError("model_timeout_seconds must be between 15 and 300")

    @staticmethod
    def name() -> str:
        """功能：返回 Harbor 中的 Agent 名称；输入：无；输出：固定名称 codini。"""
        return "codini"

    def get_version_command(self) -> str | None:
        """功能：生成容器内版本检查命令；输入：无；输出：Codini 包版本命令。"""
        return (
            "PYTHONPATH=/opt/codini-runtime python3 -c \"import importlib.metadata; "
            "print(importlib.metadata.version('codini'))\""
        )

    async def install(self, environment: BaseEnvironment) -> None:
        """功能：上传并解压离线运行包；输入：Harbor 环境；输出：可执行的 Codini。"""
        await self.exec_as_root(
            environment,
            command=(
                "if python3 -c 'import sys; assert sys.version_info >= (3, 10)' "
                ">/dev/null 2>&1; then :; "
                "elif command -v apt-get >/dev/null 2>&1; then "
                "apt-get update && "
                "DEBIAN_FRONTEND=noninteractive apt-get install -y "
                "python3; "
                "elif command -v apk >/dev/null 2>&1; then "
                "apk add --no-cache python3; "
                "elif command -v dnf >/dev/null 2>&1; then "
                "dnf install -y python3; "
                "elif command -v yum >/dev/null 2>&1; then "
                "yum install -y python3; "
                "elif ! command -v python3 >/dev/null 2>&1; then "
                "echo 'Codini requires Python 3.10 or newer' >&2; exit 1; "
                "fi; "
                "python3 -c 'import sys; assert sys.version_info >= (3, 10), "
                'f"Python 3.10+ required, got {sys.version}"\''
            ),
        )
        remote_bundle = "/tmp/codini-terminal-bench-runtime.tar.gz"
        await environment.upload_file(self._runtime_bundle_path, remote_bundle)
        await environment.upload_file(
            self._supervisor_path,
            "/opt/codini-supervisor.py",
        )
        await self.exec_as_root(
            environment,
            command=(
                "rm -rf /opt/codini-runtime && mkdir -p /opt/codini-runtime && "
                "python3 -c \"import tarfile; "
                f"tarfile.open({remote_bundle!r}, 'r:gz').extractall("
                "'/opt/codini-runtime')\" && "
                "PYTHONPATH=/opt/codini-runtime python3 -c "
                "'from codini.cli import main; raise SystemExit(main())' "
                "--help >/dev/null"
            ),
        )

    def _provider_config(self) -> tuple[str, str, dict[str, str]]:
        """功能：把 Harbor 的 provider/model 转为 Codini 参数；输入：Agent 模型与环境变量；输出：provider、模型名和容器环境变量。"""
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("Model name must use provider/model format")
        provider, model = self.model_name.split("/", 1)
        if provider == "openai":
            key_name = "OPENAI_API_KEY"
            base_name = "OPENAI_BASE_URL"
        elif provider == "siliconflow":
            key_name = "SILICONFLOW_API_KEY"
            base_name = "SILICONFLOW_BASE_URL"
        else:
            raise ValueError(
                "Codini Terminal-Bench adapter currently supports "
                "openai/<model> and siliconflow/<model>"
            )

        api_key = self._get_env(key_name)
        if not api_key:
            raise ValueError(f"Missing required agent environment variable: {key_name}")
        env = {key_name: api_key}
        base_url = self._get_env(base_name)
        if not base_url:
            raise ValueError(
                f"Missing required agent environment variable: {base_name}"
            )
        env[base_name] = base_url
        return provider, model, env

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """功能：在题目工作区运行一次 Codini；输入：任务指令、任务容器和指标上下文；输出：工作区修改及持久化日志。"""
        provider, model, env = self._provider_config()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = "/opt/codini-runtime"
        max_steps = int(self._resolved_flags.get("max_steps", 20))
        max_new_tokens = int(self._resolved_flags.get("max_new_tokens", 4096))
        temperature = str(self._resolved_flags.get("temperature", "0.2"))
        prompt = _evaluation_prompt(instruction)
        command = (
            "mkdir -p /logs/agent/codini-state/sessions "
            "/logs/agent/codini-state/runs .codini; "
            "if [ ! -e .codini/sessions ]; then "
            "ln -s /logs/agent/codini-state/sessions .codini/sessions; "
            "fi; "
            "if [ ! -e .codini/runs ]; then "
            "ln -s /logs/agent/codini-state/runs .codini/runs; "
            "fi; "
            "set +e; "
            "python3 /opt/codini-supervisor.py "
            f"--timeout-seconds {self._execution_timeout_seconds} "
            f"--model-timeout-seconds {self._model_timeout_seconds} "
            f"--provider {shlex.quote(provider)} "
            f"--model {shlex.quote(model)} "
            f"--max-steps {max_steps} "
            f"--max-new-tokens {max_new_tokens} "
            f"--temperature {shlex.quote(temperature)} "
            "--state-root /logs/agent/codini-state "
            "--log-path /logs/agent/codini.txt "
            "--result-path /logs/agent/supervisor.json "
            f"--prompt {shlex.quote(prompt)}; "
            "status=$?; "
            "if [ -d .codini/sessions ] && [ ! -L .codini/sessions ]; then "
            "cp -R .codini/sessions/. /logs/agent/codini-state/sessions/; "
            "fi; "
            "if [ -d .codini/runs ] && [ ! -L .codini/runs ]; then "
            "cp -R .codini/runs/. /logs/agent/codini-state/runs/; "
            "fi; "
            "exit \"$status\""
        )
        await self.exec_as_agent(
            environment,
            command=command,
            env=env,
            timeout_sec=self._execution_timeout_seconds + 15,
        )

    def populate_context_post_run(self, context: AgentContext) -> None:
        """功能：从 Codini report 回填 Harbor 指标；输入：Harbor AgentContext；输出：token、步数和运行标识。"""
        supervisor = {}
        supervisor_path = self.logs_dir / "supervisor.json"
        if supervisor_path.is_file():
            try:
                loaded_supervisor = json.loads(
                    supervisor_path.read_text(encoding="utf-8")
                )
                if isinstance(loaded_supervisor, dict):
                    supervisor = loaded_supervisor
            except (OSError, json.JSONDecodeError):
                supervisor = {"artifact_error": "invalid_supervisor_json"}
        reports = sorted(
            (
                *(
                    self.logs_dir
                    / "codini-state"
                    / "sessions"
                ).glob("*/report.json"),
                *(self.logs_dir / "codini-runs").glob("*/report.json"),
            ),
            key=lambda path: path.stat().st_mtime,
        )
        if not reports:
            context.metadata = {"supervisor": supervisor}
            return
        try:
            report = json.loads(reports[-1].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            context.metadata = {"codini_artifact_error": "invalid_report_json"}
            return
        if not isinstance(report, dict):
            context.metadata = {"codini_artifact_error": "invalid_report_type"}
            return
        trace_path = reports[-1].with_name("trace.jsonl")
        events = []
        malformed_trace_lines = 0
        if trace_path.is_file():
            events, malformed_trace_lines = _read_jsonl_objects(trace_path)
        trajectory = build_atif_trajectory(
            report,
            events,
            agent_version=self.version() or "",
            model_name=self.model_name or "",
        )
        (self.logs_dir / "trajectory.json").write_text(
            json.dumps(trajectory, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        summary = report.get("summary") or {}
        tokens = summary.get("tokens") or {}
        context.n_input_tokens = int(tokens.get("prompt", 0) or 0)
        context.n_output_tokens = int(tokens.get("completion", 0) or 0)
        context.n_cache_tokens = int(tokens.get("cached", 0) or 0)
        context.metadata = {
            "codini_run_id": report.get("run_id", ""),
            "attempts": int(summary.get("attempts", 0) or 0),
            "tool_steps": int(summary.get("tool_steps", 0) or 0),
            "stop_reason": summary.get("stop_reason", ""),
            "malformed_trace_lines": malformed_trace_lines,
            "supervisor": supervisor,
        }
