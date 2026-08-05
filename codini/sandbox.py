"""
沙箱执行环境。

提供可插拔的沙箱抽象层，让 run_shell 等高风险操作在隔离环境中执行，
而不是直接在宿主机上运行。
"""

import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SandboxResult:
    """ 沙箱执行命令的返回结果。"""
    returncode: int
    stdout: str
    stderr: str

class Sandbox:
    """ 沙箱基类，定义了 run_shell 的接口。"""

    @property
    def name(self):
        raise NotImplementedError

    def run_shell(self, command, cwd, timeout, env):
        """
        功能：在具体沙箱中执行命令；
        输入：命令、工作目录、超时和环境变量；
        输出：SandboxResult。"""
        raise NotImplementedError


class NoSandbox(Sandbox):
    """直接在宿主机上执行（当前默认行为，无隔离）。"""

    @property
    def name(self):
        return "none"

    def run_shell(self, command, cwd, timeout, env):
        """
        功能：直接在宿主机执行命令；
        输入：命令、工作目录、超时和环境变量；
        输出：SandboxResult。
        """
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                returncode=-1,
                stdout="",
                stderr=f"sandbox: command timed out after {timeout}s",
            )
        return SandboxResult(
            returncode=result.returncode,
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
        )


class BubblewrapSandbox(Sandbox):
    """使用 bubblewrap (bwrap) 创建轻量级 Linux 命名空间隔离。

    - 工作区通过 bind mount 实时映射，文件变更直接反映到宿主机
    - /usr、/etc 等系统目录只读挂载，防止篡改
    - /tmp 使用独立的 tmpfs
    - 默认无网络访问
    """

    def __init__(self, workspace_root, allow_network=False, extra_ro_binds=None):
        self.workspace_root = Path(workspace_root).resolve()
        self.allow_network = allow_network
        self.extra_ro_binds = extra_ro_binds or []
        self._check_bwrap()

    @staticmethod
    def _check_bwrap():
        if not shutil.which("bwrap"):
            raise RuntimeError(
                "bubblewrap (bwrap) not found. Install it with:\n"
                "  apt install bubblewrap        # Debian/Ubuntu\n"
                "  dnf install bubblewrap        # Fedora\n"
                "  pacman -S bubblewrap          # Arch"
            )

    @property
    def name(self):
        return "bubblewrap"

    def run_shell(self, command, cwd, timeout, env):
        """
        功能：通过 bubblewrap 隔离执行命令；
        输入：命令、工作目录、超时和环境变量；
        输出：SandboxResult。"""
        cmd = self._build_command(command, cwd, env)
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,  # bwrap 内部用 --clearenv + --setenv 控制
            )
        except subprocess.TimeoutExpired:
            return SandboxResult(
                returncode=-1,
                stdout="",
                stderr=f"sandbox: command timed out after {timeout}s",
            )
        return SandboxResult(
            returncode=result.returncode,
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
        )

    def _build_command(self, command, cwd, env):
        args = []

        # 命名空间隔离
        if self.allow_network:
            args.extend([
                "--unshare-ipc",    # 隔离进程间通信: 沙箱里的进程无法跟宿主机进程通信
                "--unshare-pid",    # 隔离进程ID: 沙箱里只能看到自己的进程 看不到宿主机的
                "--unshare-uts",    # 隔离主机名: 沙箱里改 hostname 不影响宿主机
                "--unshare-cgroup", # 隔离资源控制组: 限制 CPU/内存用量的基础
            ])
        else:
            args.append("--unshare-all") 

        # 设置沙箱身份
        args.extend([
            "--hostname", "sandbox",# 沙箱主机名
            "--chdir", str(cwd),    # 沙箱目录
        ])

        # 工作区：读写挂载 bind A B ==> 将宿主机的目录 A 映射到沙箱里的路径 B 可读可写
        args.extend(["--bind", str(self.workspace_root), str(self.workspace_root)])

        # 系统目 ro-bind 只读不能写
        for path in self._ro_binds():
            args.extend(["--ro-bind", path, path])

        # 路径映射：处理 /bin、/lib 等符号链接指向
        for link_path in ["/bin", "/lib", "/lib64"]:
            if Path(link_path).is_symlink() and not Path(link_path).exists():
                target = os.readlink(link_path)
                args.extend(["--symlink", target, link_path])

        # 独立 tmpfs
        args.extend(["--tmpfs", "/tmp"])

        # 基本设备
        args.extend(["--dev", "/dev"])

        # proc 文件系统
        args.extend(["--proc", "/proc"])

        # 清除环境变量，只传白名单
        args.append("--clearenv")
        if env:
            for key, value in sorted(env.items()):
                # 空值跳过
                if value:
                    args.extend(["--setenv", key, str(value)])

        # 确保 PATH 存在
        if not env or "PATH" not in env:
            default_path = "/usr/bin:/usr/local/bin:/bin"
            args.extend(["--setenv", "PATH", default_path])

        # 执行 shell
        args.extend(["/bin/sh", "-c", command])

        return args

    def _ro_binds(self):
        """ 收集需要只读挂载的系统目录 """
        candidates = ["/usr", "/etc"]
        # 处理 /lib 和 /lib64 不是符号链接的情况（某些发行版）
        for d in ["/lib", "/lib64"]:
            if Path(d).exists() and not Path(d).is_symlink():
                candidates.append(d)
        for extra in self.extra_ro_binds:
            if extra not in candidates:
                candidates.append(extra)
        return [p for p in candidates if Path(p).exists()]


class DockerSandbox(Sandbox):
    """在一次性 Docker 容器中执行 shell 命令。

    - 工作区读写挂载到容器内的 /workspace
    - 每条命令使用独立容器，结束后自动删除
    - 默认关闭网络并限制 CPU、内存和进程数
    - 不挂载 Docker socket，也不向容器透传宿主机 PATH/HOME
    """

    _HOST_ONLY_ENV_NAMES = {
        "HOME",
        "PATH",
        "PWD",
        "SHELL",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
    }

    def __init__(
        self,
        workspace_root,
        allow_network=False,
        image="python:3.12-slim",
        memory="2g",
        cpus=2.0,
        pids_limit=256,
        user=None,
        docker_executable=None,
    ):
        self.workspace_root = Path(workspace_root).resolve()
        if not self.workspace_root.is_dir():
            raise ValueError(f"docker sandbox workspace does not exist: {self.workspace_root}")
        self.allow_network = bool(allow_network)
        self.image = str(image or "").strip()
        self.memory = str(memory or "").strip()
        self.cpus = float(cpus)
        self.pids_limit = int(pids_limit)
        self.user = str(user or "").strip() or self._default_user()
        self.docker_executable = str(
            docker_executable or shutil.which("docker") or ""
        ).strip()

        if not self.docker_executable:
            raise RuntimeError(
                "docker CLI not found. Install Docker Desktop or Docker Engine, "
                "then ensure 'docker' is available on PATH."
            )
        if not self.image:
            raise ValueError("docker sandbox image must not be empty")
        if self.image.startswith("-"):
            raise ValueError("docker sandbox image must not start with '-'")
        if not self.memory:
            raise ValueError("docker sandbox memory limit must not be empty")
        if self.cpus <= 0:
            raise ValueError("docker sandbox CPU limit must be greater than zero")
        if self.pids_limit <= 0:
            raise ValueError("docker sandbox PID limit must be greater than zero")

    @staticmethod
    def _default_user():
        if os.name != "nt" and hasattr(os, "getuid") and hasattr(os, "getgid"):
            return f"{os.getuid()}:{os.getgid()}"
        return ""

    @property
    def name(self):
        return "docker"

    def run_shell(self, command, cwd, timeout, env):
        container_name = f"codini-sandbox-{uuid.uuid4().hex[:12]}"
        cmd, process_env = self._build_command(
            command,
            cwd,
            env,
            container_name=container_name,
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=process_env,
            )
        except subprocess.TimeoutExpired:
            self._remove_container(container_name, process_env)
            return SandboxResult(
                returncode=-1,
                stdout="",
                stderr=f"sandbox: command timed out after {timeout}s",
            )
        except BaseException:
            self._remove_container(container_name, process_env)
            raise
        return SandboxResult(
            returncode=result.returncode,
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
        )

    def _build_command(self, command, cwd, env, container_name):
        cwd = Path(cwd).resolve()
        try:
            relative_cwd = cwd.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError(f"docker sandbox cwd escapes workspace: {cwd}") from exc
        container_cwd = Path("/workspace") / relative_cwd

        args = [
            self.docker_executable,
            "run",
            "--rm",
            "--name",
            container_name,
            "--init",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            self.memory,
            "--cpus",
            str(self.cpus),
            "--pids-limit",
            str(self.pids_limit),
            "--mount",
            f"type=bind,source={self.workspace_root},target=/workspace",
            "--workdir",
            container_cwd.as_posix(),
        ]
        if not self.allow_network:
            args.extend(["--network", "none"])
        if self.user:
            args.extend(["--user", self.user])
        args.extend(["--entrypoint", "/bin/sh"])

        process_env = os.environ.copy()
        for key, value in sorted((env or {}).items()):
            key = str(key)
            if (
                key.upper() in self._HOST_ONLY_ENV_NAMES
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or value is None
            ):
                continue
            process_env[key] = str(value)
            args.extend(["--env", key])

        args.extend([self.image, "-c", str(command)])
        return args, process_env

    def _remove_container(self, container_name, process_env):
        try:
            subprocess.run(
                [self.docker_executable, "rm", "-f", container_name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                env=process_env,
            )
        except (OSError, subprocess.SubprocessError):
            pass


def create_sandbox(kind="none", **kwargs):
    """工厂函数：根据类型名创建沙箱实例。"""
    if kind == "none":
        return NoSandbox()
    if kind == "bubblewrap":
        return BubblewrapSandbox(**kwargs)
    if kind == "docker":
        return DockerSandbox(**kwargs)
    raise ValueError(f"unknown sandbox type: {kind}")
