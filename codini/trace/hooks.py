class TraceHooks:
    def __init__(self, *args, **kwargs):
        pass

    def on_run_start(self, user_message: str) -> None:
        pass

    def on_thinking_start(self, attempt: int, max_steps: int) -> None:
        pass

    def on_thinking_end(self, duration_ms: int) -> None:
        pass

    def on_tool_call(self, name: str, args: dict, risky: bool) -> None:
        pass

    def on_tool_result(self, name: str, result_text: str, duration_ms: int, success: bool) -> None:
        pass

    def on_response_correction(self, attempt: int) -> None:
        pass

    def on_answer(self, final_text: str, promotions: list, rejections: list,
                  tools_summary: list[dict], total_duration_ms: int,
                  prompt_metadata: dict, completion_metadata: dict) -> None:
        pass

    def on_run_error(self, error: str) -> None:
        pass
