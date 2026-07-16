from .trace import SpanProcessor, Span
from typing import Dict, Any

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


class TraceSpanProcessor(SpanProcessor):
    def __init__(self, trace_hooks: TraceHooks):
        self.trace = trace_hooks

    def on_span_start(self, span: Span):
        self.trace.current_depth = span.depth
        if span.name == "agent.ask":
            self.trace.on_run_start(span.attributes.get("user_request", ""))
        elif span.name == "llm.complete":
            self.trace.on_thinking_start(
                span.attributes.get("attempts", 1),
                span.attributes.get("max_steps", 6)
            )
        elif span.name.startswith("tool."):
            self.trace.on_tool_call(
                span.attributes.get("name", ""),
                span.attributes.get("args", {}),
                span.attributes.get("risky", False)
            )

    def on_span_end(self, span: Span):
        self.trace.current_depth = span.depth
        if span.name == "agent.ask":
            if span.attributes.get("span_status") == "ERROR":
                self.trace.on_run_error(span.attributes.get("error", "Unknown error"))
                setattr(self.trace, "_last_error_trace_id", span.attributes.get("trace_id", ""))
            else:
                self.trace.on_answer(
                    span.attributes.get("final_answer", ""),
                    span.attributes.get("promotions", []),
                    span.attributes.get("rejections", []),
                    span.attributes.get("tools_summary", []),
                    span.duration_ms or 0,
                    span.attributes.get("prompt_metadata", {}),
                    span.attributes.get("completion_metadata", {})
                )
        elif span.name == "llm.complete":
            self.trace.on_thinking_end(span.duration_ms or 0)
        elif span.name.startswith("tool."):
            self.trace.on_tool_result(
                span.attributes.get("name", ""),
                span.attributes.get("result", ""),
                span.duration_ms or 0,
                span.attributes.get("success", True)
            )

    def on_span_event(self, span: Span, name: str, attributes: Dict[str, Any]):
        if name == "response_correction":
            self.trace.on_response_correction(attributes.get("attempt", 1))