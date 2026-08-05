"""Run-level dynamic tool-step budget."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from dataclasses import dataclass
from typing import Any

ANSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ISO_TIME_PATTERN = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ][0-2]\d:[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\b"
)
RUN_ID_PATTERN = re.compile(r"\brun_\d{8}-\d{6}-[0-9a-f]+\b", re.I)
EMPTY_RESULTS = {"", "(empty)", "(no matches)", "no matches", "[]", "{}"}


@dataclass(frozen=True)
class BudgetExtension:
    previous_limit: int
    new_limit: int
    hard_limit: int
    extension_count: int
    recent_progress_score: int


@dataclass(frozen=True)
class ProgressObservation:
    score: int
    reasons: tuple[str, ...]
    no_progress_count: int
    should_warn: bool
    should_stop: bool


@dataclass(frozen=True)
class SemanticRepeatDecision:
    blocked: bool
    warning: bool
    reason: str
    signature: str
    intent: str
    repeat_count: int
    cycle_length: int = 0


class DynamicStepBudget:
    """Grow a soft tool-step limit only while recent calls make progress."""

    def __init__(
        self,
        initial_limit: int,
        *,
        hard_limit: int | None = None,
        extension_size: int | None = None,
        progress_window: int = 3,
        extension_threshold: int = 3,
        warning_count: int = 3,
        stop_count: int = 5,
    ):
        initial = max(0, int(initial_limit))
        self.initial_limit = initial
        self.soft_limit = initial
        self.hard_limit = max(
            initial,
            int(hard_limit) if hard_limit is not None else max(initial * 3, initial + 6),
        )
        self.extension_size = max(
            1,
            int(extension_size) if extension_size is not None else max(2, (initial + 1) // 2),
        )
        self.extension_count = 0
        self.extension_threshold = max(1, int(extension_threshold))
        self.warning_count = max(1, int(warning_count))
        self.stop_count = max(self.warning_count + 1, int(stop_count))
        self.no_progress_count = 0
        self._progress_scores = deque(maxlen=max(1, int(progress_window)))
        self._result_fingerprints = set()
        self._error_fingerprints = set()
        self._read_ranges: dict[str, list[tuple[int, int]]] = {}
        self._semantic_actions = deque(maxlen=12)
        self._covered_read_attempts: dict[str, int] = {}
        self.semantic_repeat_count = 0
        self.last_observation = ProgressObservation(0, (), 0, False, False)

    @property
    def recent_progress_score(self) -> int:
        return sum(self._progress_scores)

    @property
    def should_stop(self) -> bool:
        return self.no_progress_count >= self.stop_count

    def observe(
        self,
        tool_name: str,
        args: dict[str, Any] | None,
        result: Any,
        metadata: dict[str, Any] | None = None,
    ) -> ProgressObservation:
        """Classify progress from tool state changes, new information, and diagnostics."""
        name = str(tool_name or "").strip()
        call_args = dict(args or {})
        meta = dict(metadata or {})
        status = str(meta.get("tool_status", "ok") or "ok")
        normalized_result = self._normalize_result(result)
        result_fingerprint = self._fingerprint(normalized_result) if normalized_result else ""
        result_is_empty = normalized_result.lower() in EMPTY_RESULTS
        result_is_new = bool(
            result_fingerprint
            and result_fingerprint not in self._result_fingerprints
            and not result_is_empty
        )
        if result_fingerprint and not result_is_empty:
            self._result_fingerprints.add(result_fingerprint)

        reasons = []
        score = 0

        if bool(meta.get("workspace_changed")) or meta.get("diffs"):
            self._semantic_actions.clear()
            self._covered_read_attempts.clear()
            self._read_ranges.clear()
            current_action, _ = self._semantic_action(name, call_args)
            self._semantic_actions.append(current_action)
            score = 3
            reasons.append("workspace_changed")
        elif meta.get("child_run_id") or meta.get("child_trace_id"):
            if result_is_new:
                score = 2
                reasons.append("new_subagent_result")
            else:
                reasons.append("repeated_subagent_result")
        elif status in {"error", "rejected", "partial_success"}:
            error_key = self._fingerprint(
                "|".join(
                    [
                        name,
                        str(meta.get("tool_error_code", "") or ""),
                        normalized_result,
                    ]
                )
            )
            if normalized_result and error_key not in self._error_fingerprints:
                self._error_fingerprints.add(error_key)
                score = 1
                reasons.append("new_error_information")
            else:
                reasons.append("repeated_error")
        elif result_is_empty:
            reasons.append("empty_result")
        elif not result_is_new:
            reasons.append("repeated_result")
        elif name == "read_file" and self._record_new_read_coverage(call_args):
            score = 2
            reasons.append("new_read_coverage")
        elif name in {"search", "search_files", "list_files"}:
            score = 2
            reasons.append("new_discovery_result")
        elif name == "run_shell":
            score = 2
            reasons.append("new_command_result")
        else:
            score = 1
            reasons.append("new_tool_result")

        if score > 0:
            self.no_progress_count = 0
        else:
            self.no_progress_count += 1

        self._progress_scores.append(score)
        observation = ProgressObservation(
            score=score,
            reasons=tuple(reasons),
            no_progress_count=self.no_progress_count,
            should_warn=self.no_progress_count >= self.warning_count,
            should_stop=self.should_stop,
        )
        self.last_observation = observation
        return observation

    def check_semantic_repeat(
        self,
        tool_name: str,
        args: dict[str, Any] | None,
    ) -> SemanticRepeatDecision:
        """Detect equivalent calls and short action cycles before tool execution."""
        name = str(tool_name or "").strip()
        call_args = dict(args or {})
        action_key, intent = self._semantic_action(name, call_args)
        signature = self._fingerprint(action_key)[:16]
        recent = list(self._semantic_actions)
        repeat_count = sum(1 for item in recent if item == action_key)
        warning = repeat_count >= 1
        blocked = repeat_count >= 2
        reason = "semantic_duplicate" if warning else ""
        cycle_length = 0

        candidate_sequence = [*recent, action_key]
        for period in (2, 3):
            # 未形成周期跳过
            if len(candidate_sequence) < period * 2:
                continue
            # [A ➔ B ➔ A ➔ B] 上一个周期 [A, B] == [A, B] 最新周期 判定重复调用
            if candidate_sequence[-period * 2 : -period] == candidate_sequence[-period:]:
                blocked = True
                warning = True
                reason = "semantic_cycle"
                cycle_length = period
                break

        if name == "read_file":
            path, start, end = self._read_identity(call_args)
            if path and self._read_range_is_covered(path, start, end):
                read_key = f"{path}:{start}:{end}"
                covered_count = self._covered_read_attempts.get(read_key, 0) + 1
                self._covered_read_attempts[read_key] = covered_count
                warning = True
                reason = "covered_read"
                repeat_count = max(repeat_count, covered_count)
                if covered_count >= 2:
                    blocked = True

        self._semantic_actions.append(action_key)
        if warning:
            self.semantic_repeat_count += 1
        return SemanticRepeatDecision(
            blocked=blocked,
            warning=warning,
            reason=reason,
            signature=signature,
            intent=intent,
            repeat_count=repeat_count,
            cycle_length=cycle_length,
        )

    @staticmethod
    def semantic_feedback(decision: SemanticRepeatDecision) -> str:
        cycle = f", cycle length: {decision.cycle_length}" if decision.cycle_length else ""
        action = "blocked" if decision.blocked else "allowed once with warning"
        return (
            "Execution guard: semantically repeated action "
            f"({decision.reason}{cycle}; intent: {decision.intent}) was {action}. "
            "Use a materially different tool, scope, query, or line range, "
            "or return a final answer."
        )

    def warning_feedback(self) -> str:
        observation = self.last_observation
        reason_text = ", ".join(observation.reasons) or "no_new_information"
        return (
            "Execution guard: recent tool calls produced no measurable progress "
            f"({observation.no_progress_count} consecutive; reason: {reason_text}). "
            "Change the investigation strategy, make a concrete workspace change, "
            "run a meaningful verification, or return a final answer."
        )

    @staticmethod
    def _normalize_result(result: Any) -> str:
        text = ANSI_PATTERN.sub("", str(result or ""))
        text = ISO_TIME_PATTERN.sub("<timestamp>", text)
        text = RUN_ID_PATTERN.sub("<run_id>", text)
        return " ".join(text.split()).strip()

    @staticmethod
    def _fingerprint(value: str) -> str:
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

    def _record_new_read_coverage(self, args: dict[str, Any]) -> bool:
        path, start, end = self._read_identity(args)
        if not path:
            return False

        ranges = self._read_ranges.setdefault(path, [])
        if any(existing_start <= start and existing_end >= end for existing_start, existing_end in ranges):
            return False
        ranges.append((start, end))
        ranges.sort()
        merged = []
        for range_start, range_end in ranges:
            if merged and range_start <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], range_end))
            else:
                merged.append((range_start, range_end))
        self._read_ranges[path] = merged
        return True

    def _read_range_is_covered(self, path: str, start: int, end: int) -> bool:
        return any(
            existing_start <= start and existing_end >= end
            for existing_start, existing_end in self._read_ranges.get(path, [])
        )

    def _read_identity(self, args: dict[str, Any]) -> tuple[str, int, int]:
        path = self._normalize_path(args.get("path", ""))
        start = self._as_int(args.get("start"), 1)
        end = self._as_int(args.get("end"), 200)
        if end < start:
            start, end = end, start
        return path, start, end

    def _semantic_action(self, tool_name: str, args: dict[str, Any]) -> tuple[str, str]:
        """
        生成工具调用的语义指纹，用于检测重复调用
        输入：
            - tool_name: 工具名称
            - args: 工具参数
        return：
            - action_key: 工具调用的语义指纹
            - intent: 意图可读描述
        """
        name = str(tool_name or "").strip().lower()
        if name == "read_file":
            path, start, end = self._read_identity(args)
            intent = f"read_file:{path}:{start}-{end}"
            return intent, intent
        if name in {"search", "search_files"}:
            path = self._normalize_path(args.get("path", "."))
            pattern = self._normalize_text(
                args.get("pattern", args.get("query", "")),
                casefold=False,
            )
            intent = f"search:{path}:{pattern}"
            return intent, intent
        if name == "list_files":
            path = self._normalize_path(args.get("path", "."))
            intent = f"list_files:{path}"
            return intent, intent
        if name == "delegate":
            task = self._normalize_text(args.get("task", ""), strip_punctuation=True)
            intent = f"delegate:{task}"
            return intent, intent
        if name == "run_shell":
            command = str(args.get("command", "") or "").strip()
            intent = f"run_shell:{command}"
            return intent, intent
        if name == "write_file":
            path = self._normalize_path(args.get("path", ""))
            content_hash = self._fingerprint(str(args.get("content", "")))[:12]
            intent = f"write_file:{path}:{content_hash}"
            return intent, intent
        if name == "patch_file":
            path = self._normalize_path(args.get("path", ""))
            patch_hash = self._fingerprint(
                f"{args.get('old_text', '')}\0{args.get('new_text', '')}"
            )[:12]
            intent = f"patch_file:{path}:{patch_hash}"
            return intent, intent

        normalized_args = self._normalize_structure(args)
        encoded = json.dumps(normalized_args, sort_keys=True, ensure_ascii=False, default=str)
        intent = f"{name}:{encoded}"
        return intent, intent[:180]

    @staticmethod
    def _normalize_path(value: Any) -> str:
        text = str(value or ".").replace("\\", "/").strip()
        while text.startswith("./"):
            text = text[2:]
        text = re.sub(r"/+", "/", text)
        normalized = text or "."
        return normalized.lower() if os.name == "nt" else normalized

    @staticmethod
    def _normalize_text(
        value: Any,
        *,
        strip_punctuation: bool = False,
        casefold: bool = True,
    ) -> str:
        text = str(value or "").strip()
        if casefold:
            text = text.casefold()
        if strip_punctuation:
            text = re.sub(r"[\s,，。.!！?？;；:：]+", " ", text)
        return " ".join(text.split())

    @classmethod
    def _normalize_structure(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): cls._normalize_structure(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, list):
            return [cls._normalize_structure(item) for item in value]
        if isinstance(value, str):
            stripped = value.strip()
            if re.fullmatch(r"-?\d+", stripped):
                return int(stripped)
            return " ".join(stripped.split())
        return value

    @staticmethod
    def _as_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def try_extend(self, tool_steps: int) -> BudgetExtension | None:
        """Extend at the soft boundary when the recent window contains progress."""
        steps = max(0, int(tool_steps))
        if steps < self.soft_limit or self.soft_limit >= self.hard_limit:
            return None
        if (
            not self._progress_scores
            or self.recent_progress_score < self.extension_threshold
        ):
            return None

        previous = self.soft_limit
        self.soft_limit = min(self.hard_limit, self.soft_limit + self.extension_size)
        self.extension_count += 1
        return BudgetExtension(
            previous_limit=previous,
            new_limit=self.soft_limit,
            hard_limit=self.hard_limit,
            extension_count=self.extension_count,
            recent_progress_score=self.recent_progress_score,
        )

    def has_capacity(self, tool_steps: int) -> bool:
        return int(tool_steps) < self.soft_limit
