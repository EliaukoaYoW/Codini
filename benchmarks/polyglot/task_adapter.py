"""把 vendored Aider Polyglot 练习转换为统一的 Codini 任务描述。"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


_HIDDEN_SOURCE_ROOTS = (".approaches", ".articles")


class TaskAdapterError(ValueError):
    """表示题目清单或上游练习结构不符合适配约定。"""


@dataclass(frozen=True)
class PolyglotTask:
    """功能：保存一项标准化评测任务；输入：题目元数据；输出：runner 可直接消费的不可变任务描述。"""

    id: str
    language: str
    exercise: str
    dataset_path: str
    source_path: str
    dataset_tree_sha256: str
    prompt: str
    solution_files: tuple[str, ...]
    test_files: tuple[str, ...]
    example_files: tuple[str, ...]
    hidden_files: tuple[str, ...]
    protected_files: tuple[str, ...]
    verifier_argv: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """功能：把任务转换为 JSON 兼容字典；输入：当前任务；输出：列表化后的任务字段。"""
        payload = asdict(self)
        for name in (
            "solution_files",
            "test_files",
            "example_files",
            "hidden_files",
            "protected_files",
            "verifier_argv",
        ):
            payload[name] = list(payload[name])
        return payload


def _read_json(path: Path) -> dict[str, Any]:
    """功能：读取并校验 JSON 对象；输入：JSON 路径；输出：解析后的字典。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskAdapterError(f"无法读取 JSON：{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TaskAdapterError(f"JSON 顶层必须是对象：{path}")
    return payload


def tree_sha256(root: Path) -> str:
    """功能：计算带相对路径的目录摘要；输入：题目根目录；输出：稳定的 SHA-256 标识。"""
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative_path = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative_path)
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return f"sha256:{digest.hexdigest()}"


def _safe_relative_path(raw_path: Any, field_name: str) -> str:
    """功能：校验题目内相对路径；输入：原始路径和字段名；输出：规范化 POSIX 路径。"""
    value = str(raw_path or "").strip().replace("\\", "/")
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise TaskAdapterError(f"{field_name} 包含非法相对路径：{raw_path!r}")
    return path.as_posix()


def _file_list(config: dict[str, Any], kind: str, case_dir: Path) -> tuple[str, ...]:
    """功能：读取并确认 solution/test/example 文件；输入：元数据、类型和题目目录；输出：有序相对路径。"""
    files = config.get("files")
    if not isinstance(files, dict):
        raise TaskAdapterError(f"缺少 files 元数据：{case_dir / '.meta/config.json'}")
    raw_items = files.get(kind, [])
    if not isinstance(raw_items, list):
        raise TaskAdapterError(f"files.{kind} 必须是列表：{case_dir}")

    items = tuple(_safe_relative_path(item, f"files.{kind}") for item in raw_items)
    if len(set(items)) != len(items):
        raise TaskAdapterError(f"files.{kind} 存在重复路径：{case_dir}")
    for relative_path in items:
        if not (case_dir / relative_path).is_file():
            raise TaskAdapterError(f"files.{kind} 指向不存在的文件：{relative_path}")
    return items


def _instructions(case_dir: Path) -> str:
    """功能：合并上游题目说明；输入：题目目录；输出：不含参考答案的 Markdown 说明。"""
    docs_dir = case_dir / ".docs"
    instruction_path = docs_dir / "instructions.md"
    if not instruction_path.is_file():
        raise TaskAdapterError(f"缺少题目说明：{instruction_path}")

    sections = [instruction_path.read_text(encoding="utf-8").strip()]
    append_path = docs_dir / "instructions.append.md"
    if append_path.is_file():
        sections.append(append_path.read_text(encoding="utf-8").strip())
    return "\n\n".join(section for section in sections if section)


def _build_prompt(
    task_id: str,
    solution_files: tuple[str, ...],
    test_files: tuple[str, ...],
    instructions: str,
) -> str:
    """功能：生成统一编码任务提示；输入：任务 ID、文件边界和上游说明；输出：可交给 Codini 的 Prompt。"""
    solution_text = ", ".join(f"`{path}`" for path in solution_files)
    test_text = ", ".join(f"`{path}`" for path in test_files)
    return (
        f"Implement the `{task_id}` exercise in this repository.\n\n"
        f"You may modify only the solution files: {solution_text}.\n"
        f"Do not modify tests, metadata, documentation, or configuration files. "
        f"The protected test files are: {test_text}.\n\n"
        f"{instructions}\n"
    )


def _verifier_argv(language: str, test_files: tuple[str, ...], case_dir: Path) -> tuple[str, ...]:
    """功能：选择官方测试入口；输入：语言、测试文件和题目目录；输出：无 shell 拼接的命令参数。"""
    if language == "javascript":
        package = _read_json(case_dir / "package.json")
        scripts = package.get("scripts")
        if not isinstance(scripts, dict) or not str(scripts.get("test", "")).strip():
            raise TaskAdapterError(f"JavaScript 题目缺少 npm test：{case_dir}")
        return ("npm", "test", "--", "--runInBand")
    if language == "python":
        return ("python3", "-m", "pytest", "-q", *test_files)
    raise TaskAdapterError(f"当前 Polyglot 镜像不支持语言：{language}")


def _hidden_path(relative_path: str, hidden_paths: set[str]) -> bool:
    """功能：识别隐藏的参考内容；输入：相对路径和隐藏路径；输出：是否应隔离。"""
    return any(
        relative_path == hidden_path or relative_path.startswith(f"{hidden_path}/")
        for hidden_path in hidden_paths
    )


def _protected_files(
    case_dir: Path,
    solution_files: tuple[str, ...],
    hidden_files: tuple[str, ...],
) -> tuple[str, ...]:
    """功能：列出禁止 Agent 修改的题目文件；输入：题目、solution 和隐藏项；输出：受保护路径。"""
    solutions = set(solution_files)
    hidden = set(hidden_files)
    return tuple(
        path.relative_to(case_dir).as_posix()
        for path in sorted(case_dir.rglob("*"))
        if path.is_file()
        and path.relative_to(case_dir).as_posix() not in solutions
        and not _hidden_path(path.relative_to(case_dir).as_posix(), hidden)
    )


def adapt_case(
    case: dict[str, Any],
    benchmark_root: Path,
    dataset_tree_sha256: str,
) -> PolyglotTask:
    """功能：适配单个题目清单项；输入：case 字典和 benchmark 根目录；输出：标准化 PolyglotTask。"""
    task_id = str(case.get("id", "")).strip()
    language = str(case.get("language", "")).strip().lower()
    exercise = str(case.get("exercise", "")).strip()
    dataset_path = _safe_relative_path(case.get("dataset_path"), "dataset_path")
    source_path = _safe_relative_path(case.get("source_path"), "source_path")
    if not task_id or task_id != f"{language}/{exercise}":
        raise TaskAdapterError(f"任务 ID 与 language/exercise 不一致：{task_id!r}")

    root = benchmark_root.resolve()
    case_dir = (root / dataset_path).resolve()
    try:
        case_dir.relative_to(root)
    except ValueError as exc:
        raise TaskAdapterError(f"题目目录越过 benchmark 根目录：{dataset_path}") from exc
    if not case_dir.is_dir():
        raise TaskAdapterError(f"题目目录不存在：{case_dir}")
    actual_tree_sha256 = tree_sha256(case_dir)
    if actual_tree_sha256 != dataset_tree_sha256:
        raise TaskAdapterError(
            f"本地题目摘要不匹配：{task_id}: "
            f"expected={dataset_tree_sha256}, actual={actual_tree_sha256}"
        )

    metadata = _read_json(case_dir / ".meta" / "config.json")
    solution_files = _file_list(metadata, "solution", case_dir)
    test_files = _file_list(metadata, "test", case_dir)
    example_files = _file_list(metadata, "example", case_dir)
    if not solution_files or not test_files:
        raise TaskAdapterError(f"题目必须同时声明 solution 和 test 文件：{task_id}")
    if set(solution_files) & set(test_files):
        raise TaskAdapterError(f"solution 与 test 文件不能重叠：{task_id}")

    hidden_files = tuple(
        dict.fromkeys(
            (
                *example_files,
                *(
                    root_name
                    for root_name in _HIDDEN_SOURCE_ROOTS
                    if (case_dir / root_name).exists()
                ),
            )
        )
    )
    instructions = _instructions(case_dir)
    return PolyglotTask(
        id=task_id,
        language=language,
        exercise=exercise,
        dataset_path=dataset_path,
        source_path=source_path,
        dataset_tree_sha256=dataset_tree_sha256,
        prompt=_build_prompt(task_id, solution_files, test_files, instructions),
        solution_files=solution_files,
        test_files=test_files,
        example_files=example_files,
        hidden_files=hidden_files,
        protected_files=_protected_files(case_dir, solution_files, hidden_files),
        verifier_argv=_verifier_argv(language, test_files, case_dir),
    )


def load_tasks(dataset_root: Path | str) -> list[PolyglotTask]:
    """功能：发现并适配某语言全部题目；输入：语言数据集目录；输出：按练习名排序的任务列表。"""
    root = Path(dataset_root).resolve()
    benchmark_root = Path(__file__).resolve().parent
    try:
        relative_root = root.relative_to(benchmark_root)
    except ValueError as exc:
        raise TaskAdapterError(f"数据集目录越过 benchmark 根目录：{root}") from exc
    if not root.is_dir():
        raise TaskAdapterError(f"数据集目录不存在：{root}")

    language = root.name.lower()
    case_dirs = sorted(
        (
            path
            for path in root.iterdir()
            if path.is_dir() and (path / ".meta" / "config.json").is_file()
        ),
        key=lambda path: path.name,
    )
    if not case_dirs:
        raise TaskAdapterError(f"数据集目录中没有有效题目：{root}")

    tasks = [
        adapt_case(
            {
                "id": f"{language}/{case_dir.name}",
                "language": language,
                "exercise": case_dir.name,
                "dataset_path": (relative_root / case_dir.name).as_posix(),
                "source_path": f"{language}/exercises/practice/{case_dir.name}",
            },
            benchmark_root,
            tree_sha256(case_dir),
        )
        for case_dir in case_dirs
    ]
    if len({task.id for task in tasks}) != len(tasks):
        raise TaskAdapterError("自动发现的题目存在重复任务 ID")
    return tasks


def materialize_task(
    task: PolyglotTask,
    destination: Path | str,
    benchmark_root: Path | str,
) -> Path:
    """功能：创建不含参考答案的 Agent 工作区；输入：任务、目标目录和 benchmark 根目录；输出：新工作区路径。"""
    root = Path(benchmark_root).resolve()
    source = (root / task.dataset_path).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise TaskAdapterError(f"题目目录越过 benchmark 根目录：{task.dataset_path}") from exc
    if not source.is_dir():
        raise TaskAdapterError(f"题目目录不存在：{source}")

    target = Path(destination).resolve()
    if target.exists():
        raise TaskAdapterError(f"目标工作区已经存在：{target}")
    hidden = set(task.hidden_files)

    def ignore_hidden(current_dir: str, names: list[str]) -> list[str]:
        """功能：过滤参考实现文件；输入：当前复制目录和名称；输出：应忽略的名称列表。"""
        relative_dir = Path(current_dir).resolve().relative_to(source)
        return [
            name
            for name in names
            if _hidden_path((relative_dir / name).as_posix(), hidden)
        ]

    shutil.copytree(source, target, ignore=ignore_hidden)
    for relative_path in task.hidden_files:
        if (target / relative_path).exists():
            raise TaskAdapterError(f"参考内容未被隔离：{relative_path}")
    return target
