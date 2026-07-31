"""
Prompt 组装与上下文预算控制。

这个模块负责决定：每一轮到底把多少 prefix、memory、相关笔记、历史
以及当前用户请求送进模型。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

DEFAULT_TOTAL_BUDGET = 25000  # 整个 Prompt 允许的最大字符数
DEFAULT_SECTION_BUDGET_RATIOS = {
    "prefix": 0.25,          # 系统指令 + 工作区快照
    "memory": 0.15,          # 工作记忆（任务摘要 + 最近读过的文件）
    "relevant_memory": 0.10, # 根据当前请求召回的相关历史笔记
    "history": 0.50,         # 本次会话的历史记录
}


def section_budgets_for_total(total_budget: int) -> dict[str, int]:
    """Scale the shared section proportions to a concrete total budget."""
    total_budget = max(0, int(total_budget))
    budgets = {
        section: int(total_budget * ratio)
        for section, ratio in DEFAULT_SECTION_BUDGET_RATIOS.items()
    }
    budgets["history"] += total_budget - sum(budgets.values())
    return budgets


DEFAULT_SECTION_BUDGETS = section_budgets_for_total(DEFAULT_TOTAL_BUDGET)

# 每个部分的最小预算，防止模型输出为空
DEFAULT_SECTION_FLOORS = {
    "prefix": 2400,
    "memory": 800,
    "relevant_memory": 800,
    "history": 3000
}

DEFAULT_REDUCTION_ORDER = ("relevant_memory", "history", "memory", "prefix") # 当 Prompt 超预算时的压缩顺序
SECTION_ORDER = ("prefix", "memory", "relevant_memory", "history", "current_request") # 拼接 Prompt 时各 Section 的排列顺序（从上到下）
CURRENT_REQUEST_SECTION = "current_request"  # 当前用户的请求环节
RELEVANT_MEMORY_LIMIT = 3                    # 最多召回 3 条相关历史笔记
DEFAULT_SECTION_WEIGHTS = {
    "prefix": 4.0,
    "memory": 2.0,
    "relevant_memory": 1.0,
    "history": 2.0,
}
CONTINUATION_PATTERN = re.compile(
    r"(?i)(继续|接着|刚才|之前|上次|前面|上述|基于此|"
    r"\bcontinue\b|\bearlier\b|\bprevious\b|\blast time\b|\babove\b)"
)
WORKING_MEMORY_PATTERN = re.compile(
    r"(?i)(修改|修复|实现|重构|文件|代码|函数|类|模块|"
    r"\bedit\b|\bfix\b|\bimplement\b|\brefactor\b|\bfile\b|\bcode\b|\bfunction\b|\bclass\b|"
    r"(?:[A-Za-z0-9_.-]+[/\\])+[A-Za-z0-9_.-]+)"
)


def _typed_note_text(note: dict[str, Any]) -> str:
    """ 
    把一条 note 转换为面向模型的文本表示。

    输入:
      - note: dict，必须包含至少 text、note_type、scope 三个字段。
        - note_type 取值： "observation" | "decision" | "constraint" | "preference" | "error_resolution"
        - scope 取值： "session" | "project" | "file"
      - 输出："[note_type/scope] text" 格式

    在 agent 链路里的位置：
      被 `WorkspaceContext.get_relevant_notes()` 内部调用，用于把历史笔记转换为
      发送给模型的带标记文本。
    """
    text = str(note.get("text", "")).strip()
    note_type = str(note.get("note_type", "observation")).strip() or "observation"
    scope = str(note.get("scope", "session")).strip() or "session"
    return f"[{note_type}/{scope}] {text}"


def _tail_clip(text: Any, limit: int) -> str:
    """ 
    对文本进行尾部裁剪，确保不超过指定长度
    输入: 
    - text: 待裁剪的文本
    - limit: 最大允许的字符数
    输出: 裁剪后的文本
    """
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[:limit-3] + "..."


def _relevance_terms(text: Any) -> set[str]:
    """Return lightweight English/path tokens and Chinese bigrams."""
    value = str(text).lower()
    terms = set(re.findall(r"[a-z0-9_./\\-]+", value))
    for segment in re.findall(r"[\u4e00-\u9fff]+", value):
        if len(segment) == 1:
            terms.add(segment)
            continue
        terms.update(segment[index:index + 2] for index in range(len(segment) - 1))
    return terms


@dataclass
class SectionRender:
    raw: str
    budget: int
    rendered: str
    details: dict | None = None

    @property
    def raw_chars(self) -> int:
        return len(self.raw)
    
    @property
    def rendered_chars(self) -> int:
        return len(self.rendered)

class ContextManager:
    """ 
    上下文管理器 负责根据预算组装 Prompt 
    组装顺序: prefix -> memory -> relevant_memory -> history -> current_request
    """
    def __init__(
        self,
        agent,
        total_budget = DEFAULT_TOTAL_BUDGET,    # 整个 Prompt 允许的最大字符数
        section_budgets = None,                 # 每个部分的预算
        section_floors = None,                  # 每个部分的最小预算
        reduction_order = None,                 # 当 Prompt 超预算时的压缩顺序
        budget_strategy = "dynamic",            # dynamic / fixed
    ):
        self.agent = agent
        self.total_budget = int(total_budget)
        self.section_budgets = section_budgets_for_total(self.total_budget)
        if section_budgets:
            self.section_budgets.update({str(key): int(value) for key, value in section_budgets.items()})
        self._section_floor_overrides = {str(key): int(value) for key, value in (section_floors or {}).items()}
        self.section_floors = self._compute_section_floors()
        self.reduction_order = tuple(reduction_order or DEFAULT_REDUCTION_ORDER)
        self.budget_strategy = str(budget_strategy).strip().lower()
        if self.budget_strategy not in {"dynamic", "fixed"}:
            raise ValueError(f"unsupported context budget strategy: {budget_strategy}")
        
    def build(self, user_message: Any) -> tuple[str, dict[str, Any]]:
        """ 功能: 按预算组装一轮完整 prompt。
        用户请求 -> 收集上下文（各个 section 的内容 section_texts）-> 裁剪上下文（根据预算）-> 组装 prompt

        为什么存在：仅靠用户这一轮输入，模型并不知道当前仓库状态、会话里已经读过什么、哪些旧信息还值得继续参考。
        这个函数负责把“稳定基线 + 工作记忆 + 相关笔记 + 历史 + 当前请求”拼成真正发给模型的 prompt。

        输入 / 输出：
        - 输入：`user_message`，也就是用户当前这一轮的新请求。
        - 输出：`(prompt, metadata)`。
          `prompt` 是最终发送给模型的文本；
          `metadata` 记录了每个 section 的原始长度、裁剪后的长度、是否触发了预算收缩等信息，
          后续会进入 trace/report，便于解释这轮 prompt是怎么被拼出来的。

        在 agent 链路里的位置：
        它位于 `Codini.ask()` 的每轮模型调用之前，是“真正发请求给模型”的最后一道组装工序。
        `WorkspaceContext` 提供稳定前缀，
        `LayeredMemory`提供工作记忆，这个函数则把它们和当前请求合成一份可控大小的 prompt。
        """
        user_message = str(user_message)
        self.section_floors = self._compute_section_floors()
        memory_enabled = True
        relevant_memory_enabled = True
        context_reduction_enabled = True
        if hasattr(self.agent, "feature_enabled"):
            memory_enabled = self.agent.feature_enabled("memory")
            relevant_memory_enabled = self.agent.feature_enabled("relevant_memory")
            context_reduction_enabled = self.agent.feature_enabled("context_reduction")
        section_texts = {
            "prefix": str(getattr(self.agent, "prefix", "")),
            "memory": "Memory:\n- disabled" if not memory_enabled else str(self.agent.memory_text()),
            "history": "",
            CURRENT_REQUEST_SECTION: f"Current user request:\n{user_message}"
        }
        checkpoint_text = ""
        if hasattr(self.agent, "render_checkpoint_text"):
            checkpoint_text = str(self.agent.render_checkpoint_text() or "").strip()
        if checkpoint_text:
            section_texts["prefix"] = section_texts["prefix"] + "\n\n" + checkpoint_text
        selected_notes = []
        if memory_enabled and relevant_memory_enabled and hasattr(self.agent, "memory") and hasattr(self.agent.memory, "retrieval_candidates"):
            selected_notes = self.agent.memory.retrieval_candidates(user_message, limit = RELEVANT_MEMORY_LIMIT)

        # 用于功能测试
        if not context_reduction_enabled:
            rendered = self._render_sections_without_reduction(section_texts, selected_notes = selected_notes)
            prompt = self._assemble_prompt(rendered)
            metadata = self._metadata(
                prompt=prompt,
                rendered=rendered,
                budgets={section: render.budget for section, render in rendered.items() if section != CURRENT_REQUEST_SECTION},
                reduction_log=[],
                selected_notes=selected_notes,
                user_message=user_message,
                section_texts=section_texts,
            )
            return prompt, metadata

        if self.budget_strategy == "fixed":
            budgets, allocation = self._allocate_fixed_budgets(
                section_texts,
                selected_notes,
            )
        else:
            budgets, allocation = self._allocate_dynamic_budgets(
                section_texts,
                selected_notes,
                user_message,
            )
        effective_reduction_order = tuple(allocation["reduction_order"])
        rendered = self._render_sections(section_texts, budgets, selected_notes = selected_notes)
        prompt = self._assemble_prompt(rendered)
        reduction_log = []

        # 超预算时，根据压缩顺序压缩每个 section 直至不超过 total_budget
        while len(prompt) > self.total_budget:
            overflow = len(prompt) - self.total_budget
            reduced = False
            for section in effective_reduction_order:
                floor = int(self.section_floors.get(section, 0))
                current_budget = int(budgets.get(section, 0))
                if current_budget <= floor:
                    continue
                new_budget = max(floor, current_budget - overflow)
                if new_budget >= current_budget:
                    continue
                reduction_log.append(
                    {
                        "section": section,
                        "before_chars": current_budget,
                        "after_chars": new_budget,
                        "overflow_chars": overflow
                    }
                )
                budgets[section] = new_budget
                rendered = self._render_sections(section_texts, budgets, selected_notes = selected_notes)
                prompt = self._assemble_prompt(rendered)
                reduced = True
                break
            if not reduced:
                break
        allocation["allocated_chars"] = dict(budgets)
        allocation["unused_chars"] = max(
            0,
            int(allocation["available_chars"]) - sum(budgets.values()),
        )
        metadata = self._metadata(
            prompt = prompt,
            rendered = rendered,
            budgets = budgets,
            reduction_log = reduction_log,
            selected_notes = selected_notes,
            user_message = user_message,
            section_texts = section_texts,
            allocation = allocation,
            effective_reduction_order = effective_reduction_order,
        )
        return prompt, metadata
        
    def _render_sections_without_reduction(self, section_texts: dict[str, str], selected_notes: list[dict[str, Any]] | None = None) -> dict[str, SectionRender]:
        """
        不做任何压缩，直接暂渲染各 section。
        输入: section_texts（各 section 的原始文本）、selected_notes（已召回的相关笔记）
        输出: 各 section 对应的 SectionRender
        用途: 当 context_reduction 功能被禁用时（测试 / 实验用）
        """
        selected_notes = selected_notes or []
        relevant_lines = ["Relevant Memory:"]
        if selected_notes:
            relevant_lines.extend(f"- {_typed_note_text(note)}" for note in selected_notes)
        else:
            relevant_lines.append("- none")
        relevant_raw = "\n".join(relevant_lines)
        history = list(getattr(self.agent, "session", {}).get("history", []))
        history_raw = self._raw_history_text(history)
        return {
            "prefix": SectionRender(raw = section_texts["prefix"], budget = len(section_texts["prefix"]), rendered = section_texts["prefix"], details = {}),
            "memory": SectionRender(raw = section_texts["memory"], budget = len(section_texts["memory"]), rendered = section_texts["memory"], details = {}),
            "relevant_memory": SectionRender(
                raw = relevant_raw,
                budget = len(relevant_raw),
                rendered = relevant_raw,
                details = {
                    "selected_notes": [note["text"] for note in selected_notes],
                    "rendered_notes": [note["text"] for note in selected_notes],
                    "selected_count": len(selected_notes),
                    "rendered_count": len(selected_notes),
                    "note_budget": 0,
                },
            ),
            "history": SectionRender(raw = history_raw, budget = len(history_raw), rendered = history_raw, details = {"rendered_entries": []}),
            CURRENT_REQUEST_SECTION: SectionRender(
                raw=section_texts[CURRENT_REQUEST_SECTION],
                budget=0,
                rendered=section_texts[CURRENT_REQUEST_SECTION],
                details={},
            ),
        }


    def _compute_section_floors(self) -> dict[str, int]:
        """
        计算每个 section 超出预算后进行压缩时的最低保障线（floor）
        输入: 无
        输出: 各 section 对应的最低保障线（floor）
        用途: 当 Prompt 超预算时，根据压缩顺序压缩每个 section 直至不超过 total_budget
        """
        floors = {}
        for section, budget in self.section_budgets.items():
            budget = max(0, int(budget))
            if budget == int(DEFAULT_SECTION_BUDGETS.get(section, -1)):
                floor = int(DEFAULT_SECTION_FLOORS.get(section, max(20, budget // 2)))
            else:
                floor = max(20, budget // 2)
            floors[section] = min(budget, floor)
        floors.update(self._section_floor_overrides)
        return floors

    def _allocate_fixed_budgets(
        self,
        section_texts: dict[str, str],
        selected_notes: list[dict[str, Any]],
    ) -> tuple[dict[str, int], dict[str, Any]]:
        """Allocate the same hard budget using fixed configured section shares."""
        sections = list(SECTION_ORDER[:-1])
        history = list(getattr(self.agent, "session", {}).get("history", []))
        raw_sections = {
            "prefix": section_texts["prefix"],
            "memory": section_texts["memory"],
            "relevant_memory": self._relevant_memory_raw(selected_notes),
            "history": self._raw_history_text(history),
        }
        demands = {section: len(raw_sections[section]) for section in sections}
        separator_chars = 2 * (len(SECTION_ORDER) - 1)
        current_request_chars = len(section_texts[CURRENT_REQUEST_SECTION])
        available = max(0, self.total_budget - current_request_chars - separator_chars)
        configured_total = max(
            1,
            sum(max(0, int(self.section_budgets.get(section, 0))) for section in sections),
        )
        quotas = {
            section: int(
                available
                * max(0, int(self.section_budgets.get(section, 0)))
                / configured_total
            )
            for section in sections
        }
        quotas["history"] += available - sum(quotas.values())
        budgets = {
            section: min(demands[section], quotas[section])
            for section in sections
        }
        floors = {
            section: min(demands[section], max(0, int(self.section_floors.get(section, 0))))
            for section in sections
        }
        allocation = {
            "strategy": "fixed",
            "available_chars": available,
            "current_request_chars": current_request_chars,
            "separator_chars": separator_chars,
            "configured_budgets": {
                section: int(self.section_budgets.get(section, 0))
                for section in sections
            },
            "demand_chars": demands,
            "floor_chars": floors,
            "weights": {},
            "signals": {section: ["fixed_share"] for section in sections},
            "allocated_chars": dict(budgets),
            "unused_chars": max(0, available - sum(budgets.values())),
            "reduction_order": list(self.reduction_order),
        }
        return budgets, allocation

    def _allocate_dynamic_budgets(
        self,
        section_texts: dict[str, str],
        selected_notes: list[dict[str, Any]],
        user_message: str,
    ) -> tuple[dict[str, int], dict[str, Any]]:
        """ 
        从低到高（从 floor 到 budget）确定每个 section 的最终分配字符数。
        未用完的空间会自动补给 relevant_memory，避免浪费。 
        """
        sections = list(SECTION_ORDER[:-1])
        relevant_raw = self._relevant_memory_raw(selected_notes)
        history = list(getattr(self.agent, "session", {}).get("history", []))
        raw_sections = {
            "prefix": section_texts["prefix"],
            "memory": section_texts["memory"],
            "relevant_memory": relevant_raw,
            "history": self._raw_history_text(history),
        }
        relevance_history = history
        if (
            history
            and history[-1].get("role") == "user"
            and str(history[-1].get("content", "")) == user_message
        ):
            relevance_history = history[:-1]
        relevance_texts = dict(raw_sections)
        relevance_texts["history"] = self._raw_history_text(relevance_history)

        separator_chars = 2 * (len(SECTION_ORDER) - 1)
        current_request_chars = len(section_texts[CURRENT_REQUEST_SECTION])
        available = max(0, self.total_budget - current_request_chars - separator_chars)
        demands = {section: len(raw_sections[section]) for section in sections}
        floors = {
            section: min(demands[section], max(0, int(self.section_floors.get(section, 0))))
            for section in sections
        }

        configured_total = max(1, sum(max(0, int(value)) for value in self.section_budgets.values()))
        query_terms = _relevance_terms(user_message)
        weights = {}
        signals = {section: [] for section in sections}
        for section in sections:
            configured_share = max(0, int(self.section_budgets.get(section, 0))) / configured_total
            weight = float(DEFAULT_SECTION_WEIGHTS[section]) + configured_share
            section_terms = _relevance_terms(relevance_texts[section])
            overlap = len(query_terms & section_terms)
            if overlap:
                overlap_boost = min(3.0, overlap / max(1, min(len(query_terms), 12)) * 4.0)
                weight += overlap_boost
                signals[section].append(f"query_overlap:{overlap}")
            weights[section] = weight

        if selected_notes:
            note_boost = min(3.0, 1.0 + len(selected_notes) * 0.75)
            weights["relevant_memory"] += note_boost
            signals["relevant_memory"].append(f"retrieved_notes:{len(selected_notes)}")
        if CONTINUATION_PATTERN.search(user_message):
            weights["history"] += 3.0
            signals["history"].append("continuation_request")
        if WORKING_MEMORY_PATTERN.search(user_message):
            weights["memory"] += 2.0
            signals["memory"].append("workspace_task")
        signals["prefix"].append("protected_core")

        budgets = {section: 0 for section in sections}
        remaining = available
        # 为每个 section 分配最低保底字符数 floors
        remaining = self._distribute_budget(budgets, floors, weights, remaining)
        # 为每个 section 分配实际需求字符数 demands
        remaining = self._distribute_budget(budgets, demands, weights, remaining)

        tie_break = {section: index for index, section in enumerate(self.reduction_order)}
        reducible = [section for section in sections if section != "prefix"]
        dynamic_reduction_order = sorted(
            reducible,
            key=lambda section: (weights[section], tie_break.get(section, len(tie_break))),
        )
        dynamic_reduction_order.append("prefix")

        allocation = {
            "strategy": "dynamic_relevance",
            "available_chars": available,
            "current_request_chars": current_request_chars,
            "separator_chars": separator_chars,
            "configured_budgets": {
                section: int(self.section_budgets.get(section, 0))
                for section in sections
            },
            "demand_chars": demands,
            "floor_chars": floors,
            "weights": {section: round(weights[section], 3) for section in sections},
            "signals": signals,
            "allocated_chars": dict(budgets),
            "unused_chars": remaining,
            "reduction_order": dynamic_reduction_order,
        }
        return budgets, allocation

    @staticmethod
    def _distribute_budget(
        budgets: dict[str, int],
        targets: dict[str, int],
        weights: dict[str, float],
        remaining: int,
    ) -> int:
        """Proportionally fill section targets without exceeding the shared pool."""
        remaining = max(0, int(remaining))
        section_order = {section: index for index, section in enumerate(SECTION_ORDER)}
        while remaining > 0:
            active = [
                section
                for section, target in targets.items()
                if budgets.get(section, 0) < max(0, int(target))
            ]
            if not active:
                break
            active.sort(key=lambda section: (-weights.get(section, 1.0), section_order.get(section, 99)))
            total_weight = sum(max(0.01, weights.get(section, 1.0)) for section in active)
            round_budget = remaining
            distributed = 0
            for section in active:
                if remaining <= 0:
                    break
                need = max(0, int(targets[section]) - budgets.get(section, 0))
                share = max(
                    1,
                    int(round_budget * max(0.01, weights.get(section, 1.0)) / total_weight),
                )
                grant = min(need, share, remaining)
                budgets[section] = budgets.get(section, 0) + grant
                remaining -= grant
                distributed += grant
            if distributed <= 0:
                break
        return remaining
    
    def _render_sections(self, section_texts: dict[str, str], budgets: dict[str, int], selected_notes: list[dict[str, Any]] | None = None) -> dict[str, SectionRender]:
        """
        渲染各 section，根据预算压缩。
        输入: section_texts（各 section 的原始文本）、budgets（各 section 的预算）、selected_notes（已召回的相关笔记）
        输出: 各 section 对应的 SectionRender
        用途: 当 Prompt 超预算时，根据压缩顺序压缩每个 section 直至不超过 total_budget
        """
        rendered = {}
        for section in SECTION_ORDER:
            budget = budgets.get(section)
            if section == CURRENT_REQUEST_SECTION:
                # 当前请求永不裁剪 直接渲染
                raw = section_texts[section]
                rendered[section] = SectionRender(raw = raw, budget = 0, rendered = raw, details = {})
            elif section == "relevant_memory":
                # 相关记忆 -> 有独立的渲染逻辑
                rendered[section] = self._render_relevant_memory(selected_notes or [], int(budget or 0))
            elif section == "history":
                # 历史记录 -> 有独立的渲染逻辑
                rendered[section] = self._render_history_section(int(budget or 0))
            else:
                # prefix 和 memory 直接裁剪
                raw = section_texts[section]
                rendered_text = _tail_clip(raw, max(0, int(budget or 0)))
                rendered[section] = SectionRender(raw = raw, budget = int(budget or 0),  rendered = rendered_text, details = {})
        return rendered

    @staticmethod
    def _relevant_memory_raw(selected_notes: list[dict[str, Any]]) -> str:
        """ 格式化相关的笔记作为输入 """
        header = "Relevant Memory:"
        note_texts = [
            _typed_note_text(note)
            for note in selected_notes
            if str(note.get("text", "")).strip()
        ]
        return "\n".join([header] + [f"- {text}" for text in note_texts]) if note_texts else "\n".join([header, "- none"])
    
    def _render_relevant_memory(self, selected_notes: list[dict[str, Any]], budget: int) -> SectionRender:
        """
        渲染相关记忆 section 并根据预算压缩。
        输入: selected_notes（已召回的相关笔记）、budget（相关记忆 section 的预算）
        输出: 相关记忆 section 的 SectionRender
        用途: 当 Prompt 超预算时，根据压缩顺序压缩相关记忆 section 直至不超过 total_budget
        """
        header = "Relevant Memory:"
        # 提取 note 文本并附带类型和作用域标记
        note_texts = [
            _typed_note_text(note)
            for note in selected_notes
            if str(note.get("text", "")).strip()
        ]
        raw = self._relevant_memory_raw(selected_notes)
        if budget <= 0:
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered="",
                details={
                    "selected_notes": note_texts,
                    "rendered_notes": [],
                    "selected_count": len(note_texts),
                    "rendered_count": 0,
                    "note_budget": 0,
                },
            )
        if not note_texts:
            rendered = _tail_clip(raw, budget)
            return SectionRender(
                raw=raw, budget=budget, rendered=rendered, 
                details={
                    "selected_notes": [],
                    "rendered_notes": [],
                    "selected_count": 0,
                    "rendered_count": 0,
                    "note_budget": 0
                }
            )
        
        per_note_budget = self._per_note_budget(budget, len(note_texts), header)
        rendered_notes = []
        while True:
            # 让每条 note 平分这一段的预算，避免一条超长笔记把其他笔记都挤掉
            # 如果整体还超出预算，就减少每个笔记的预算，直到符合预算
            rendered_notes = [_tail_clip(text, per_note_budget) for text in note_texts]
            rendered = "\n".join([header] + [f"- {text}" for text in rendered_notes])
            if len(rendered) <= budget or per_note_budget <= 1:
                break
            per_note_budget -= 1
        
        if len(rendered) > budget and budget > 0:
            # 如果整体还超出预算，就对整个段做最后的整体裁剪
            rendered = _tail_clip(raw, budget)
            rendered_notes = [rendered]
            
        return SectionRender(
            raw = raw, budget = budget, rendered = rendered, 
            details = {
                "selected_notes": note_texts,
                "rendered_notes": rendered_notes,
                "selected_count": len(note_texts),
                "rendered_count": len(rendered_notes),
                "note_budget": per_note_budget
            }
        )

    def _per_note_budget(self, budget: int, note_count: int, header: str) -> int:
        """
        计算每条 note 的平分到的字符预算
        输入: budget（总预算）、note_count（笔记条数）、header（标题）
        输出: 每条 note 的最大字符预算（最小为 1 个字符）
        公式: max(1, (budget - len(header) - 3 * note_count) // note_count)
        """
        if note_count <= 0:
            return 0
        overhead = len(header) + 3 * note_count
        usable = max(0, budget - overhead)
        return max(1, usable // note_count)
    
    def _render_history_section(self, budget: int) -> SectionRender:
        """
        将历史记录 section 渲染进 Prompt，优先保留最近 6 条，旧条目被压缩或折叠。
        输入: budget（分配给 历史记录 section 的字符预算）
        输出: 历史记录 section 的 SectionRender
        """
        history = list(getattr(self.agent, "session", {}).get("history", []))
        raw = self._raw_history_text(history)
        if budget <= 0:
            return SectionRender(
                raw=raw,
                budget=budget,
                rendered="",
                details={
                    "recent_window": 6,
                    "recent_start": max(0, len(history) - 6),
                    "rendered_entries": [],
                },
            )
        if not history:
            rendered = _tail_clip(raw, budget)
            return SectionRender(
                raw = raw, budget = budget, rendered = rendered, 
                details = {
                    "selected_notes": [],
                    "rendered_notes": [],
                    "selected_count": 0,
                    "rendered_count": 0,
                    "note_budget": 0
                }
            )
        # 优先保留最近的历史记录
        recent_window = 6
        recent_start = max(0, len(history) - recent_window)
        history_entries, history_details = self._compressed_history_entries(history, recent_start)
        rendered_entries = []
        for entry in reversed(history_entries):
            recent = bool(entry.get("recent", False))
            candidate_lines = list(entry.get("lines", []))
            candidate_entries = candidate_lines + rendered_entries
            candidate_rendered = "\n".join(["Transcript:", *candidate_entries])
            if len(candidate_rendered) <= budget:
                rendered_entries = candidate_entries
                continue
            if recent:
                available = budget - len("Transcript:")
                if rendered_entries:
                    available -= sum(len(line) + 1 for line in rendered_entries)
                available = max(20, available - 1)
                candidate_lines = [_tail_clip(line, available) for line in candidate_lines]
                candidate_entries = candidate_lines + rendered_entries
                candidate_rendered = "\n".join(["Transcript:", *candidate_entries])
                if len(candidate_rendered) <= budget:
                    rendered_entries = candidate_entries
            else:
                smaller_lines = [_tail_clip(line, 20) for line in candidate_lines]
                smaller_entries = smaller_lines + rendered_entries
                smaller_rendered = "\n".join(["Transcript:", *smaller_entries])
                if len(smaller_rendered) <= budget:
                    rendered_entries = smaller_entries
        rendered = "\n".join(["Transcript:", *rendered_entries])

        if len(rendered) > budget and budget > 0:
            rendered = _tail_clip(raw, budget)

        return SectionRender(
            raw = raw, budget = budget, rendered = rendered,
            details = {
                "recent_window": recent_window,
                "recent_start": recent_start,
                "rendered_entries": rendered_entries,
                **history_details,
            },
        )

    def _compressed_history_entries(self, history: list[dict[str, Any]], recent_start: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """
        将历史条目分成「最近」和「较旧」两类，分别处理:
        - 最近条目：保留完整内容（每条最多 10000 字符）
        - 较旧的 read_file：用记忆中的文件摘要替代，重复的直接丢弃
        - 较旧的其他工具：压缩成一行摘要
        - 较旧的用户/助手消息：压缩到 60 字符
        输入: history（历史记录列表）、recent_start（最近记录起始索引）
        输出: 压缩后的历史记录列表 entries、压缩详情 details
        """
        entries = []
        seen_older_reads = set()
        details = {
            "older_entries_count": 0,
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0
        }
        
        for index, item in enumerate(history):
            recent = index >= recent_start
            if recent:
                line_limit = 10000
                entries.append(
                    {
                        "recent": True,
                        "lines": self._render_history_item(item, line_limit)
                    }
                )
                continue
            if item["role"] == "tool" and item["name"] == "read_file":
                path = str(item["args"].get("path", "")).strip()
                if path in seen_older_reads:
                    details["collapsed_duplicate_reads"] += 1
                    continue
                seen_older_reads.add(path)
                summary = self._reusable_file_summary(path)
                if summary:
                    entries.append({"recent":False, "lines": [f"{path} -> {summary}"]})
                    details["older_entries_count"] += 1
                    details["reused_file_summary_count"] += 1
                    continue
            
            if item["role"] == "tool":
                summary_line = self._summarize_old_tool_item(item)
                entries.append({"recent": False, "lines": [summary_line]})
                details["older_entries_count"] += 1
                details["summarized_tool_count"] += 1
                continue
            
            entries.append({"recent": False, "lines": self._render_history_item(item, 60)})
        return entries, details
    

    def _reusable_file_summary(self, path: str) -> str:
        """
        从记忆中获取文件的已有摘要，用于代替历史记录中的 read_file 操作，如果不存在则返回空字符串。
        输入: path（文件路径）
        输出: 文件摘要（如果存在）
        """
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "to_dict"):
            return ""
        snapshot = memory.to_dict()
        summary = snapshot.get("file_summaries", {}).get(str(path), "")
        if not summary:
            return ""
        return str(summary.get("summary", "")).strip()
    
    def _summarize_old_tool_item(self, item: dict[str, Any]) -> str:
        """
        将一条较旧的工具调用记录压缩成单行摘要。
        run_shell：格式为「命令 -> 前三行输出」
        其他工具：直接截断到 60 字符
        输入：item（一条历史记录字典）
        输出：单行摘要字符串
        """
        if item["name"] == "run_shell":
            command = str(item["args"].get("command", "")).strip() or "shell"
            lines = [line.strip() for line in str(item.get("content","")).splitlines() if line.strip()]
            summary = " | ".join(lines[:3]) if lines else "(empty)"
            return f"{command} -> {summary}"
        return self._render_history_item(item, 60)[0]
    
    def _raw_history_text(self, history: list[dict[str, Any]]) -> str:
        """
        将完整历史列表转成未压缩的纯文本（用于统计原始长度）。
        输入：history（历史列表）
        输出：格式化后的 Transcript 字符串
        """
        if not history:
            return "Transcript:\n- empty"
        lines = []
        for item in history:
            if item["role"] == "tool":
                lines.append(f"[assistant]: <tool>{{\"name\":\"{item['name']}\",\"args\":{json.dumps(item['args'], sort_keys=True)}}}</tool>")
                lines.append(f"[system]: <tool_result>:\n{item['content']}\n<tool_result>")
            else:
                lines.append(f"[{item['role']}] {item['content']}")
        return "\n".join(["Transcript:", *lines])
    

    def _render_history_item(self, item: dict[str, Any], line_limit: int) -> list[str]:
        """
        将单条历史记录渲染成行列表，并按 line_limit 裁剪内容。
        输入：item（单条历史记录字典）、line_limit（每行最大字符数）
        输出：格式化后的列表，包含前缀（如 "[tool: 命令]"）和内容（如 "命令输出"）
        """
        if item["role"] == "tool":
            prefix = f"[assistant] <tool>{{\"name\":\"{item['name']}\",\"args\":{json.dumps(item['args'], sort_keys=True)}}}</tool>"
            content = f"[system] Tool result:\n{_tail_clip(item['content'], max(20, line_limit))}"
            return [prefix, content]
        
        content = item.get("content", "")
        if item["role"] == "assistant":
            # 过滤历史遗留的 STATUS/STEPS_USED/FINDINGS，防止大模型在上下文里产生“回复模仿偏见”
            cleaned = []
            for line in content.splitlines():
                stripped = line.strip()
                if any(stripped.startswith(prefix) for prefix in (
                    "STATUS:", "STEPS_USED:", "FINDINGS:",
                    "Sub-agent status:", "Steps used:", "Findings:"
                )):
                    continue
                cleaned.append(line)
            content = "\n".join(cleaned)
            
        return [f"[{item['role']}] {_tail_clip(content, line_limit)}"]
    
    def _assemble_prompt(self, rendered: dict[str, SectionRender]) -> str:
        # 组装Prompt 其顺序是刻意设计的：稳定规则放前面，最新请求放最后。
        return "\n\n".join(
            [
                rendered["prefix"].rendered,
                rendered["memory"].rendered,
                rendered["relevant_memory"].rendered,
                rendered["history"].rendered,
                rendered[CURRENT_REQUEST_SECTION].rendered,
            ]
        ).strip()

    def _assemble_prompt_without_current_request(self, rendered: dict[str, SectionRender]) -> str:
        return "\n\n".join(
            [
                rendered["prefix"].rendered,
                rendered["memory"].rendered,
                rendered["relevant_memory"].rendered,
                rendered["history"].rendered,
            ]
        ).strip()

    
    def _metadata(
        self, 
        prompt: str, 
        rendered: dict[str, SectionRender], 
        budgets: dict[str, int], 
        reduction_log: list[dict[str, Any]], 
        selected_notes: list[dict[str, Any]], 
        user_message: str, 
        section_texts: dict[str, str],
        allocation: dict[str, Any] | None = None,
        effective_reduction_order: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        section_metadata = {}
        for section in SECTION_ORDER[:-1]:
            section_metadata[section] = {
                "raw_chars": rendered[section].raw_chars,
                "budget_chars": int(budgets.get(section, 0)),
                "rendered_chars": rendered[section].rendered_chars
            }
        section_metadata[CURRENT_REQUEST_SECTION] = {
            "raw_chars": len(section_texts[CURRENT_REQUEST_SECTION]),
            "budget_chars": None,
            "rendered_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
        }
        prompt_without_current_request = self._assemble_prompt_without_current_request(rendered)
        return {
            "prompt_chars": len(prompt),
            "prompt_without_current_request": prompt_without_current_request,
            "prompt_without_current_request_chars": len(prompt_without_current_request),
            "prompt_budget_chars": self.total_budget,
            "prompt_over_budget": len(prompt) > self.total_budget,
            "section_order": list(SECTION_ORDER),
            "budget_strategy": (allocation or {}).get("strategy", "disabled"),
            "budget_allocation": allocation or {},
            "section_budgets": {
                section: (None if section == CURRENT_REQUEST_SECTION else int(budgets.get(section, 0)))
                for section in SECTION_ORDER
            },
            "sections": section_metadata,
            "budget_reductions": reduction_log,
            "reduction_order": list(effective_reduction_order or self.reduction_order),
            "relevant_memory": {
                "limit": RELEVANT_MEMORY_LIMIT,
                "selected_count": len(selected_notes),
                "selected_notes": [note["text"] for note in selected_notes],
                "selected_source": [str(note.get("source", "")) for note in selected_notes],
                "selected_kinds": [str(note.get("kind", "episodic")).strip() or "episodic" for note in selected_notes],
                "selected_note_types": [str(note.get("note_type", "observation")) for note in selected_notes],
                "selected_scopes": [str(note.get("scope", "session")) for note in selected_notes],
                "selected_scope_refs": [
                    list(note.get("scope_refs", []))
                    for note in selected_notes
                ],
                "selected_evidence": [list(note.get("evidence", [])) for note in selected_notes],
                "selected_durable_count": sum(
                    1 for note in selected_notes if (str(note.get("kind", "episodic")).strip() or "episodic") == "durable"
                ),
                "raw_chars": rendered["relevant_memory"].raw_chars,
                "rendered_chars": rendered["relevant_memory"].rendered_chars,
                "rendered_notes": list(rendered["relevant_memory"].details.get("rendered_notes", [])),
                "rendered_count": int(rendered["relevant_memory"].details.get("rendered_count", 0)),
            },
            "history": {
                "raw_chars": rendered["history"].raw_chars,
                "rendered_chars": rendered["history"].rendered_chars,
                "older_entries_count": int(rendered["history"].details.get("older_entries_count", 0)),
                "collapsed_duplicate_reads": int(rendered["history"].details.get("collapsed_duplicate_reads", 0)),
                "reused_file_summary_count": int(rendered["history"].details.get("reused_file_summary_count", 0)),
                "summarized_tool_count": int(rendered["history"].details.get("summarized_tool_count", 0)),
            },
            "current_request": {
                "text": user_message,
                "raw_chars": len(user_message),
                "rendered_chars": len(user_message),
                "section_chars": len(rendered[CURRENT_REQUEST_SECTION].rendered),
            }
        }
