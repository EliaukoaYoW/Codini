"""在不可篡改的全新副本中运行 Polyglot 官方测试。"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .task_adapter import (
    PolyglotTask,
    TaskAdapterError,
    materialize_task,
    tree_sha256,
)


_IGNORED_AGENT_ROOTS = {
    ".codini",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}
_IGNORED_AGENT_FILES = {".coverage"}
_MAX_SOLUTION_BYTES = 1_000_000
_MAX_LOG_CHARS = 100_000


@dataclass(frozen=True)
class WorkspaceAudit:
    """功能：保存 Agent 工作区审计结果；输入：路径检查结果；输出：完整性、违规项和 solution 摘要。"""

    integrity_ok: bool
    violations: tuple[str, ...]
    solution_sha256: dict[str, str]


@dataclass(frozen=True)
class VerifierResult:
    """功能：保存一次可信验证结果；输入：审计和容器执行数据；输出：可序列化评测记录。"""

    task_id: str
    status: str
    passed: bool
    integrity_ok: bool
    violations: tuple[str, ...]
    image: str
    verifier_argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    solution_sha256: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        """功能：转换验证结果为 JSON 字典；输入：当前结果；输出：列表化后的字段。"""
        payload = asdict(self)
        payload["violations"] = list(self.violations)
        payload["verifier_argv"] = list(self.verifier_argv)
        return payload


def _sha256_file(path: Path) -> str:
    """功能：计算文件摘要；输入：文件路径；输出：带 sha256 前缀的摘要。"""
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _ignored_agent_path(relative_path: str) -> bool:
    """功能：识别 Codini 和测试工具运行产物；输入：POSIX 相对路径；输出：是否忽略。"""
    parts = Path(relative_path).parts
    return bool(
        (parts and parts[0] in _IGNORED_AGENT_ROOTS)
        or relative_path in _IGNORED_AGENT_FILES
    )


def _hidden_task_path(relative_path: str, hidden_paths: set[str]) -> bool:
    """功能：识别被隔离的参考文件或目录；输入：相对路径和隐藏路径；输出：是否隐藏。"""
    return any(
        relative_path == hidden_path or relative_path.startswith(f"{hidden_path}/")
        for hidden_path in hidden_paths
    )


def _case_source(task: PolyglotTask, benchmark_root: Path) -> Path:
    """功能：安全定位可信题目源；输入：任务和 benchmark 根目录；输出：题目绝对路径。"""
    root = benchmark_root.resolve()
    source = (root / task.dataset_path).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise TaskAdapterError(f"题目源越过 benchmark 根目录：{task.dataset_path}") from exc
    if not source.is_dir():
        raise TaskAdapterError(f"题目源不存在：{source}")
    return source


def audit_workspace(
    task: PolyglotTask,
    agent_workspace: Path | str,
    benchmark_root: Path | str,
) -> WorkspaceAudit:
    """功能：阻止测试篡改和越界文件进入 verifier；输入：任务、Agent 工作区和可信题目根；输出：完整性审计。"""
    workspace_input = Path(agent_workspace)
    if workspace_input.is_symlink():
        raise TaskAdapterError(f"Agent 工作区不能是符号链接：{workspace_input}")
    workspace = workspace_input.resolve()
    if not workspace.is_dir():
        raise TaskAdapterError(f"Agent 工作区无效：{workspace}")

    source = _case_source(task, Path(benchmark_root))
    violations: list[str] = []
    if tree_sha256(source) != task.dataset_tree_sha256:
        violations.append("trusted_dataset_digest_mismatch")

    hidden = set(task.hidden_files)
    expected_files = {
        path.relative_to(source).as_posix(): path
        for path in source.rglob("*")
        if path.is_file()
        and not _hidden_task_path(path.relative_to(source).as_posix(), hidden)
    }
    actual_files: dict[str, Path] = {}
    for path in workspace.rglob("*"):
        relative_path = path.relative_to(workspace).as_posix()
        if _ignored_agent_path(relative_path):
            continue
        if path.is_symlink():
            violations.append(f"symlink_not_allowed:{relative_path}")
            continue
        if path.is_file():
            actual_files[relative_path] = path

    expected_names = set(expected_files)
    actual_names = set(actual_files)
    violations.extend(f"missing_file:{path}" for path in sorted(expected_names - actual_names))
    violations.extend(f"unexpected_file:{path}" for path in sorted(actual_names - expected_names))

    solution_names = set(task.solution_files)
    for relative_path in sorted(expected_names - solution_names):
        actual_path = actual_files.get(relative_path)
        if actual_path is not None and _sha256_file(actual_path) != _sha256_file(
            expected_files[relative_path]
        ):
            violations.append(f"protected_file_modified:{relative_path}")

    solution_sha256: dict[str, str] = {}
    for relative_path in task.solution_files:
        solution_path = actual_files.get(relative_path)
        if solution_path is None:
            continue
        if solution_path.stat().st_size > _MAX_SOLUTION_BYTES:
            violations.append(f"solution_too_large:{relative_path}")
            continue
        solution_sha256[relative_path] = _sha256_file(solution_path)

    unique_violations = tuple(dict.fromkeys(violations))
    return WorkspaceAudit(
        integrity_ok=not unique_violations,
        violations=unique_violations,
        solution_sha256=solution_sha256,
    )


def _clip_log(value: str | bytes | None) -> str:
    """功能：限制容器日志体积；输入：文本或字节日志；输出：UTF-8 文本。"""
    if value is None:
        return ""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    if len(text) <= _MAX_LOG_CHARS:
        return text
    return text[:_MAX_LOG_CHARS] + "\n...[truncated by Codini verifier]\n"


class DockerVerifier:
    """在断网、只读、无额外 capability 的容器中执行可信测试。"""

    def __init__(
        self,
        image: str = "codini-polyglot-pyjs:0.1",
        docker_executable: str = "docker",
        timeout_seconds: int = 600,
    ):
        """功能：配置 Docker verifier；输入：镜像、Docker 程序和超时；输出：可复用 verifier。"""
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        self.image = image
        self.docker_executable = docker_executable
        self.timeout_seconds = timeout_seconds

    def _docker_command(
        self,
        task: PolyglotTask,
        verifier_workspace: Path,
        container_name: str,
    ) -> list[str]:
        """功能：构造加固后的 docker run；输入：任务、可信副本和容器名；输出：参数列表。"""
        mount = (
            f"type=bind,source={verifier_workspace.resolve()},"
            "target=/workspace,readonly"
        )
        return [
            self.docker_executable,
            "run",
            "--name",
            container_name,
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "1g",
            "--cpus",
            "1.0",
            "--user",
            "1000:1000",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=128m",
            "--mount",
            mount,
            "--workdir",
            "/workspace",
            "--env",
            "CI=1",
            "--env",
            "HOME=/tmp/home",
            "--env",
            "npm_config_cache=/tmp/npm-cache",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTEST_ADDOPTS=-p no:cacheprovider",
            self.image,
            *task.verifier_argv,
        ]

    def _remove_container(self, container_name: str) -> None:
        """功能：兜底删除超时或异常容器；输入：容器名；输出：无。"""
        try:
            subprocess.run(
                [self.docker_executable, "rm", "-f", container_name],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def verify(
        self,
        task: PolyglotTask,
        agent_workspace: Path | str,
        benchmark_root: Path | str,
    ) -> VerifierResult:
        """功能：审计并在可信副本中验证 solution；输入：任务、Agent 工作区和题目根；输出：结构化结果。"""
        started = time.monotonic()
        audit = audit_workspace(task, agent_workspace, benchmark_root)
        if not audit.integrity_ok:
            return VerifierResult(
                task_id=task.id,
                status="invalid_workspace",
                passed=False,
                integrity_ok=False,
                violations=audit.violations,
                image=self.image,
                verifier_argv=task.verifier_argv,
                exit_code=None,
                stdout="",
                stderr="",
                duration_ms=int((time.monotonic() - started) * 1000),
                solution_sha256=audit.solution_sha256,
            )

        agent_path = Path(agent_workspace).resolve()
        verifier_parent = Path(
            tempfile.mkdtemp(prefix="verifier-", dir=str(agent_path.parent))
        )
        verifier_workspace = verifier_parent / "workspace"
        container_name = f"codini-polyglot-verify-{uuid.uuid4().hex[:12]}"
        try:
            materialize_task(task, verifier_workspace, benchmark_root)
            for relative_path in task.solution_files:
                source_path = agent_path / relative_path
                target_path = verifier_workspace / relative_path
                shutil.copyfile(source_path, target_path)

            command = self._docker_command(task, verifier_workspace, container_name)
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
            passed = completed.returncode == 0
            return VerifierResult(
                task_id=task.id,
                status="pass" if passed else "fail",
                passed=passed,
                integrity_ok=True,
                violations=(),
                image=self.image,
                verifier_argv=task.verifier_argv,
                exit_code=completed.returncode,
                stdout=_clip_log(completed.stdout),
                stderr=_clip_log(completed.stderr),
                duration_ms=int((time.monotonic() - started) * 1000),
                solution_sha256=audit.solution_sha256,
            )
        except subprocess.TimeoutExpired as exc:
            return VerifierResult(
                task_id=task.id,
                status="timeout",
                passed=False,
                integrity_ok=True,
                violations=(),
                image=self.image,
                verifier_argv=task.verifier_argv,
                exit_code=None,
                stdout=_clip_log(exc.stdout),
                stderr=_clip_log(exc.stderr),
                duration_ms=int((time.monotonic() - started) * 1000),
                solution_sha256=audit.solution_sha256,
            )
        except OSError as exc:
            return VerifierResult(
                task_id=task.id,
                status="error",
                passed=False,
                integrity_ok=True,
                violations=(),
                image=self.image,
                verifier_argv=task.verifier_argv,
                exit_code=None,
                stdout="",
                stderr=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
                solution_sha256=audit.solution_sha256,
            )
        finally:
            self._remove_container(container_name)
            if verifier_parent.exists():
                shutil.rmtree(verifier_parent)
