from .hooks import TraceHooks

class SubAgentTrace(TraceHooks):
    """Wrapper to indent sub-agent visual output."""
    def __init__(self, parent_viz):
        super().__init__()
        self.parent = parent_viz

    @property
    def current_depth(self):
        return getattr(self.parent, "current_depth", 0)

    @current_depth.setter
    def current_depth(self, value):
        if hasattr(self.parent, "current_depth"):
            self.parent.current_depth = value

    def on_run_start(self, user_message: str) -> None:
        if self.parent:
            self.parent.on_run_start(user_message)

    def on_thinking_start(self, attempt: int, max_steps: int) -> None:
        if self.parent:
            self.parent.on_thinking_start(attempt, max_steps)

    def on_thinking_end(self, duration_ms: int) -> None:
        if self.parent:
            self.parent.on_thinking_end(duration_ms)

    def on_tool_call(self, name: str, args: dict, risky: bool) -> None:
        if self.parent:
            self.parent.on_tool_call(name, args, risky)

    def on_tool_result(self, name: str, result_text: str, duration_ms: int, success: bool) -> None:
        if self.parent:
            self.parent.on_tool_result(name, result_text, duration_ms, success)

    def on_response_correction(self, attempt: int) -> None:
        if self.parent:
            self.parent.on_response_correction(attempt)

    def on_answer(self, final_text: str, promotions: list, rejections: list,
                  tools_summary: list, total_duration_ms: int,
                  prompt_metadata: dict, completion_metadata: dict) -> None:
        if self.parent:
            self.parent.on_answer(
                final_text, promotions, rejections, tools_summary,
                total_duration_ms, prompt_metadata, completion_metadata
            )

    def on_run_error(self, error: str) -> None:
        if self.parent:
            self.parent.on_run_error(error)