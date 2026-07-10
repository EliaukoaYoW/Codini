from .hooks import TraceHooks
from .trace_agent import _args_summary, _result_summary, TOOL_ICONS, TOOL_COLORS, COLOR_SUCCESS, COLOR_ERROR

class SubAgentTrace(TraceHooks):
    """Wrapper to indent sub-agent visual output."""
    def __init__(self, parent_trace):
        self.parent = parent_trace

    def on_run_start(self, user_message: str) -> None:
        pass

    def on_thinking_start(self, attempt: int, max_steps: int) -> None:
        if hasattr(self.parent, "console"):
            spinner = self.parent._next_spinner()
            self.parent.console.print(
                f"  [dim]│[/]   {spinner}  [dim italic]Sub-agent thinking …[/]  "
                f"[dim](step {attempt}/{max_steps})[/]"
            )
        else:
            print(f"  |   ~ Sub-agent thinking ... (step {attempt}/{max_steps})")

    def on_thinking_end(self, duration_ms: int) -> None:
        if hasattr(self.parent, "console"):
            self.parent.console.print(f"  [dim]│[/]     [dim]└─ model responded in {duration_ms}ms[/]")
        else:
            print(f"    |     └─ model responded in {duration_ms}ms")

    def on_tool_call(self, name: str, args: dict, risky: bool) -> None:
        if hasattr(self.parent, "console"):
            icon = TOOL_ICONS.get(name, "🔧")
            color = TOOL_COLORS.get(name, "#38bdf8")
            summary = _args_summary(name, args)
            risk_tag = f" [bold red][risky][/]" if risky else ""
            self.parent.console.print(
                f"  [dim]│[/]   [dim]├─[/] [{color}]{icon} {name}[/]"
                f"  [dim]{summary}[/]{risk_tag}"
            )
        else:
            summary = _args_summary(name, args)
            risk_tag = " [risky]" if risky else ""
            print(f"  |   |─ {name}  {summary}{risk_tag}")

    def on_tool_result(self, name: str, result_text: str, duration_ms: int, success: bool) -> None:
        if hasattr(self.parent, "console"):
            summary, ok = _result_summary(name, result_text)
            status_icon = "✅" if ok else "❌"
            status_color = COLOR_SUCCESS if ok else COLOR_ERROR
            self.parent.console.print(
                f"  [dim]│[/]   [dim]│  └─[/] [{status_color}]{status_icon}[/] "
                f"[dim]{summary} • {duration_ms}ms[/]"
            )
            if not ok:
                err_lines = result_text.strip().split("\n")[:3]
                for line in err_lines:
                    clipped = line[:80]
                    self.parent.console.print(f"  [dim]│[/]   [dim]│     [/][red dim]{clipped}[/]")
        else:
            summary, ok = _result_summary(name, result_text)
            status = "OK " if ok else "ERR"
            print(f"    |   |─ [{status}] {summary} • {duration_ms}ms")

    def on_response_correction(self, attempt: int) -> None:
        if hasattr(self.parent, "console"):
            self.parent.console.print(f"  [dim]│[/]   [dim]│  [/][yellow]⚠ response format error — retrying[/]")
        else:
            print(f"    |   |  ⚠ response format error — retrying")

    def on_answer(self, final_text: str, promotions: list, rejections: list,
                  tools_summary: list, total_duration_ms: int,
                  prompt_metadata: dict, completion_metadata: dict) -> None:
        if hasattr(self.parent, "console"):
            self.parent.console.print(f"  [dim]│[/]   [dim]└─ Sub-agent finished in {total_duration_ms/1000:.1f}s[/]")
        else:
            print(f"  |   └─ Sub-agent finished in {total_duration_ms/1000:.1f}s")

    def on_run_error(self, error: str) -> None:
        if hasattr(self.parent, "console"):
            self.parent.console.print(f"  [dim]│[/]   [bold red]❌ Sub-agent Error:[/] {error}")
        else:
            print(f"  |   ❌ Sub-agent Error: {error}")
