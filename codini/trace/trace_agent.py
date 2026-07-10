import shutil
import re
from typing import Optional

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    from rich.table import Table
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

from .hooks import VizHooks

TOOL_ICONS = {
    "read_file": "📖",
    "write_file": "✏️",
    "patch_file": "🔧",
    "run_shell": "⚡",
    "search": "🔍",
    "list_files": "📁",
    "delegate": "🤖",
}

TOOL_COLORS = {
    "read_file": "#38bdf8",
    "write_file": "#f59e0b",
    "patch_file": "#f59e0b",
    "run_shell": "#ef4444",
    "search": "#a78bfa",
    "list_files": "#22d3ee",
    "delegate": "#10b981",
}

COLOR_THINKING = "#64748b"
COLOR_SUCCESS = "#10b981"
COLOR_ERROR = "#ef4444"
COLOR_WARNING = "#eab308"
COLOR_ANSWER = "#f8fafc"
COLOR_USER = "#f472b6"
COLOR_MEM = "#a78bfa"
COLOR_SLATE = "#94a3b8"
COLOR_DIM = "#475569"

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _args_summary(name: str, args: dict) -> str:
    if name == "read_file":
        path = args.get("path", "?")
        start = args.get("start")
        end = args.get("end")
        if start and end:
            return f"{path}  L{start}-{end}"
        return path
    if name == "write_file":
        return args.get("path", "?")
    if name == "patch_file":
        return args.get("path", "?")
    if name == "run_shell":
        cmd = args.get("command", "")
        return cmd[:60] + ("…" if len(cmd) > 60 else "")
    if name == "search":
        pat = args.get("pattern", "")
        path = args.get("path", ".")
        return f'"{pat}" in {path}'
    if name == "list_files":
        return args.get("path", ".")
    if name == "delegate":
        task = args.get("task", "")
        return task[:50] + ("…" if len(task) > 50 else "")
    parts = [f"{k}={v}" for k, v in args.items() if v]
    return ", ".join(parts)[:60] if parts else ""


def _result_summary(name: str, result_text: str) -> tuple[str, bool]:
    text = result_text.strip()
    is_error = text.startswith("error:") or text.startswith("Error:")
    if is_error:
        first_line = text.split("\n")[0]
        return first_line[:80], False
    if name == "read_file":
        lines = text.count("\n") + 1
        chars = len(text)
        return f"{lines} lines • {chars} chars", True
    if name in ("write_file", "patch_file"):
        return "written", True
    if name == "run_shell":
        lines = text.count("\n") + 1
        return f"{lines} lines output", True
    if name == "search":
        lines = text.count("\n") + 1
        return f"{lines} result lines", True
    if name == "list_files":
        lines = text.count("\n") + 1
        return f"{lines} entries", True
    if name == "delegate":
        return "delegated", True
    return f"{len(text)} chars", True


def _extract_code_blocks(text: str) -> list[tuple[str, str]]:
    blocks = []
    pattern = r"```(\w*)\n(.*?)```"
    for m in re.finditer(pattern, text, re.DOTALL):
        lang = m.group(1) or ""
        code = m.group(2).strip()
        blocks.append((lang, code))
    return blocks


class RichTrace(TraceHooks):
    def __init__(self, console: Optional[Console] = None, max_result_chars: int = 600):
        self.console = console or Console()
        self.max_result_chars = max_result_chars
        self._tools: list[dict] = []
        self._attempt = 0
        self._max_steps = 6
        self._spinner_idx = 0
        self._corrections = 0
        self._start_ms: int = 0

    def _next_spinner(self) -> str:
        ch = SPINNER_FRAMES[self._spinner_idx % len(SPINNER_FRAMES)]
        self._spinner_idx += 1
        return ch

    def on_run_start(self, user_message: str) -> None:
        self._tools = []
        self._attempt = 0
        self._corrections = 0
        self.console.print()

    def on_thinking_start(self, attempt: int, max_steps: int) -> None:
        self._attempt = attempt
        self._max_steps = max_steps
        spinner = self._next_spinner()
        self.console.print(
            f"  {spinner}  [dim italic]Codini thinking …[/]  "
            f"[dim](step {attempt}/{max_steps})[/]"
        )

    def on_thinking_end(self, duration_ms: int) -> None:
        ms = duration_ms
        self.console.print(
            f"  {' ' * 2}[dim]└─ model responded in {ms}ms[/]"
        )

    def on_tool_call(self, name: str, args: dict, risky: bool) -> None:
        icon = TOOL_ICONS.get(name, "🔧")
        color = TOOL_COLORS.get(name, "#38bdf8")
        summary = _args_summary(name, args)
        risk_tag = f" [bold red][risky][/]" if risky else ""
        self.console.print(
            f"  [dim]├─[/] [{color}]{icon} {name}[/]"
            f"  [dim]{summary}[/]{risk_tag}"
        )

    def on_tool_result(self, name: str, result_text: str, duration_ms: int, success: bool) -> None:
        summary, ok = _result_summary(name, result_text)
        status_icon = "✅" if ok else "❌"
        status_color = COLOR_SUCCESS if ok else COLOR_ERROR
        ms = duration_ms
        self.console.print(
            f"  [dim]│  └─[/] [{status_color}]{status_icon}[/] "
            f"[dim]{summary} • {ms}ms[/]"
        )
        self._tools.append({
            "name": name,
            "success": ok,
            "summary": summary,
            "duration_ms": ms,
        })

        if not ok:
            err_lines = result_text.strip().split("\n")[:3]
            for line in err_lines:
                clipped = line[:80]
                self.console.print(f"  [dim]│     [/][red dim]{clipped}[/]")

    def on_response_correction(self, attempt: int) -> None:
        self._corrections += 1
        self.console.print(
            f"  [dim]│  [/][yellow]⚠ response format error — retrying[/]"
        )

    def on_answer(self, final_text: str, promotions: list, rejections: list,
                  tools_summary: list[dict], total_duration_ms: int,
                  prompt_metadata: dict, completion_metadata: dict) -> None:
        self.console.print()

        answer_text = final_text.strip()
        if answer_text:
            has_code = "```" in answer_text
            if has_code:
                self.console.print(
                    Panel(
                        answer_text,
                        title="[bold green] Answer[/]",
                        title_align="left",
                        border_style="#10b981",
                        padding=(0, 1),
                        expand=False,
                    )
                )
            else:
                self.console.print( f"  [bold green][/] {answer_text}")

        if promotions:
            self.console.print()
            self.console.print(f"  [dim]💾 Durable memory promoted:[/]")
            for topic, note_text in promotions:
                self.console.print(
                    f"  [dim]   •[/] [italic]{topic}[/]: "
                    f"[dim]{note_text}[/]"
                )

        total_s = total_duration_ms / 1000
        total_tools = len(tools_summary)
        ok_tools = sum(1 for t in tools_summary if t["success"])
        total_tool_ms = sum(t["duration_ms"] for t in tools_summary)

        tokens = completion_metadata.get("usage", {})
        prompt_tokens = (
            completion_metadata.get("prompt_tokens")
            or tokens.get("prompt_tokens", 0)
        )
        completion_tokens = (
            completion_metadata.get("completion_tokens")
            or tokens.get("completion_tokens", 0)
        )
        total_tokens = (
            completion_metadata.get("total_tokens")
            or tokens.get("total_tokens", 0)
        )

        parts = [
            f"[dim]⏱[/] [bold]{total_s:.1f}[/]s",
            f"[dim]🔧[/] {ok_tools}/{total_tools} tools",
            f"[dim]⚡[/] {total_tool_ms}ms tool time",
        ]
        if total_tokens:
            parts.append(f"[dim]🧠[/] {total_tokens} tokens")
        if self._corrections:
            parts.append(f"[dim]⚠[/] {self._corrections} corrections")

        self.console.print()
        self.console.print("  " + "  │  ".join(parts))

    def on_run_error(self, error: str) -> None:
        self.console.print()
        self.console.print(
            Panel(
                error,
                title="[bold red]❌ Run Error[/]",
                title_align="left",
                border_style="#ef4444",
                padding=(0, 1),
                expand=False,
            )
        )
        self.console.print()


class PlainTrace(TraceHooks):
    def __init__(self, max_result_chars: int = 600):
        self.max_result_chars = max_result_chars
        self._tools = []
        self._attempt = 0
        self._corrections = 0

    def on_run_start(self, user_message: str) -> None:
        self._tools = []
        self._attempt = 0
        self._corrections = 0
        print()

    def on_thinking_start(self, attempt: int, max_steps: int) -> None:
        self._attempt = attempt
        print(f"  ~ Codini thinking ...  (step {attempt}/{max_steps})")

    def on_thinking_end(self, duration_ms: int) -> None:
        print(f"    └─ model responded in {duration_ms}ms")

    def on_tool_call(self, name: str, args: dict, risky: bool) -> None:
        summary = _args_summary(name, args)
        risk_tag = " [risky]" if risky else ""
        print(f"  |─ {name}  {summary}{risk_tag}")

    def on_tool_result(self, name: str, result_text: str, duration_ms: int, success: bool) -> None:
        summary, ok = _result_summary(name, result_text)
        status = "OK " if ok else "ERR"
        print(f"    |─ [{status}] {summary} • {duration_ms}ms")
        self._tools.append({
            "name": name, "success": ok, "summary": summary, "duration_ms": duration_ms,
        })
        if not ok:
            for line in result_text.strip().split("\n")[:3]:
                print(f"        {line[:80]}")

    def on_response_correction(self, attempt: int) -> None:
        self._corrections += 1
        print(f"    |  ⚠ response format error — retrying")

    def on_answer(self, final_text: str, promotions: list, rejections: list,
                  tools_summary: list[dict], total_duration_ms: int,
                  prompt_metadata: dict, completion_metadata: dict) -> None:
        print()
        answer_text = final_text.strip()
        if answer_text:
            print(f"  ✨ Answer:")
            for line in answer_text.split("\n"):
                print(f"    {line}")

        if promotions:
            print(f"\n  Durable memory promoted:")
            for topic, note_text in promotions:
                print(f"    • {topic}: {note_text}")

        total_s = total_duration_ms / 1000
        total_tools = len(tools_summary)
        ok_tools = sum(1 for t in tools_summary if t["success"])
        total_tool_ms = sum(t["duration_ms"] for t in tools_summary)

        tokens = completion_metadata.get("usage", {})
        prompt_tokens = (
            completion_metadata.get("prompt_tokens")
            or tokens.get("prompt_tokens", 0)
        )
        completion_tokens = (
            completion_metadata.get("completion_tokens")
            or tokens.get("completion_tokens", 0)
        )
        total_tokens = (
            completion_metadata.get("total_tokens")
            or tokens.get("total_tokens", 0)
        )

        parts = [
            f"⏱ {total_s:.1f}s",
            f"🔧 {ok_tools}/{total_tools} tools",
            f"⚡ {total_tool_ms}ms tool time",
        ]
        if total_tokens:
            parts.append(f"🧠 {total_tokens} tok")
        if self._corrections:
            parts.append(f"⚠ {self._corrections} corrections")

        print()
        print("  " + "  |  ".join(parts))
        print()

    def on_run_error(self, error: str) -> None:
        print()
        print(f"  ❌ Run Error: {error}")
        print()


def make_trace(console: Optional[Console] = None, force_plain: bool = False) -> TraceHooks:
    if not force_plain and HAS_RICH and console is not None:
        return RichTrace(console=console)
    return PlainTrace()
