"""Convert Codini trace events to Harbor ATIF v1.7 trajectories."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _timestamp(event: dict[str, Any]) -> str:
    """功能：读取事件时间并提供合法兜底值；输入：Codini trace 事件；输出：ISO 8601 时间。"""
    value = str(event.get("created_at", "") or "").strip()
    return value or datetime.now(timezone.utc).isoformat()


def build_atif_trajectory(
    report: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    agent_version: str,
    model_name: str,
) -> dict[str, Any]:
    """功能：把 Codini report/trace 转为 ATIF；输入：报告、事件、版本和模型；输出：ATIF-v1.7 字典。"""
    steps: list[dict[str, Any]] = []
    latest_agent_step: dict[str, Any] | None = None

    for event in events:
        event_name = event.get("event")
        if event_name == "run_started":
            steps.append(
                {
                    "step_id": len(steps) + 1,
                    "timestamp": _timestamp(event),
                    "source": "user",
                    "message": str(event.get("user_request", "") or ""),
                }
            )
            continue

        if event_name == "model_parsed":
            metrics = {
                "prompt_tokens": int(event.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(event.get("completion_tokens", 0) or 0),
                "cached_tokens": int(event.get("cached_tokens", 0) or 0),
            }
            latest_agent_step = {
                "step_id": len(steps) + 1,
                "timestamp": _timestamp(event),
                "source": "agent",
                "model_name": model_name,
                "message": str(event.get("raw", "") or ""),
                "metrics": metrics,
                "extra": {
                    "codini_kind": event.get("kind", ""),
                    "duration_ms": int(event.get("duration_ms", 0) or 0),
                },
            }
            steps.append(latest_agent_step)
            continue

        if event_name == "tool_executed":
            if latest_agent_step is None:
                latest_agent_step = {
                    "step_id": len(steps) + 1,
                    "timestamp": _timestamp(event),
                    "source": "agent",
                    "model_name": model_name,
                    "message": "",
                }
                steps.append(latest_agent_step)
            call_id = f"codini-{event.get('span_id', len(steps))}"
            latest_agent_step.setdefault("tool_calls", []).append(
                {
                    "tool_call_id": call_id,
                    "function_name": str(event.get("name", "") or ""),
                    "arguments": dict(event.get("args") or {}),
                    "extra": {
                        "tool_status": event.get("tool_status", ""),
                        "duration_ms": int(event.get("duration_ms", 0) or 0),
                    },
                }
            )
            observation = latest_agent_step.setdefault("observation", {"results": []})
            observation["results"].append(
                {
                    "source_call_id": call_id,
                    "content": str(
                        event.get("result_full")
                        or event.get("result")
                        or ""
                    ),
                    "extra": {
                        "exit_code": event.get("exit_code"),
                        "workspace_changed": bool(event.get("workspace_changed", False)),
                        "affected_paths": list(event.get("affected_paths") or []),
                    },
                }
            )
            continue

        if event_name == "run_finished":
            final_answer = str(event.get("final_answer", "") or "").strip()
            if final_answer:
                if latest_agent_step is None:
                    latest_agent_step = {
                        "step_id": len(steps) + 1,
                        "timestamp": _timestamp(event),
                        "source": "agent",
                        "model_name": model_name,
                        "message": final_answer,
                    }
                    steps.append(latest_agent_step)
                elif event.get("stop_reason") == "final_answer_returned":
                    latest_agent_step["message"] = final_answer

    summary = dict(report.get("summary") or {})
    tokens = dict(summary.get("tokens") or {})
    agent = {
        "name": "codini",
        "version": agent_version or "unknown",
        "model_name": model_name,
        "extra": {
            "run_id": report.get("run_id", ""),
            "tool_steps": int(summary.get("tool_steps", 0) or 0),
            "attempts": int(summary.get("attempts", 0) or 0),
        },
    }
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": str(report.get("run_id") or report.get("session_id") or ""),
        "agent": agent,
        "steps": steps,
        "final_metrics": {
            "total_prompt_tokens": int(tokens.get("prompt", 0) or 0),
            "total_completion_tokens": int(tokens.get("completion", 0) or 0),
            "total_cached_tokens": int(tokens.get("cached", 0) or 0),
            "total_steps": len(steps),
        },
        "notes": "Converted from Codini report.json and trace.jsonl",
    }
