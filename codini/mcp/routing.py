"""Provider-neutral MCP tool catalog, exposure budgeting, and routing.

The model should not receive an unbounded MCP catalog.  This module keeps the
complete catalog in the host process and exposes only a deterministic, bounded
working set for each user request.  It deliberately does not call an LLM for
routing: discovery must remain available when the model provider has no native
tool-search feature and must not add another network dependency.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter


DEFAULT_ROUTING_CONFIG = {
    "mode": "auto",
    "eager_tool_limit": 8,
    "route_top_k": 5,
    "max_active_tools": 8,
    "max_total_schema_chars": 16000,
    "max_single_tool_chars": 8000,
    "min_score": 0.05,
    "default_search_limit": 5,
    "max_search_limit": 20,
}

_IDENTIFIER_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_LATIN_TOKEN = re.compile(r"[a-z0-9]+")
_CJK_SEGMENT = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_SPACE = re.compile(r"\s+")


def _positive_int(config, name, default):
    raw = config.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"routing.{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"routing.{name} must be greater than zero")
    return value


def normalize_routing_config(config=None):
    raw = dict(config or {})
    normalized = dict(DEFAULT_ROUTING_CONFIG)
    mode = str(raw.get("mode", normalized["mode"])).strip().lower()
    if mode not in {"auto", "dynamic", "eager"}:
        raise ValueError("routing.mode must be auto, dynamic, or eager")
    normalized["mode"] = mode
    for name in (
        "eager_tool_limit",
        "route_top_k",
        "max_active_tools",
        "max_total_schema_chars",
        "max_single_tool_chars",
        "default_search_limit",
        "max_search_limit",
    ):
        normalized[name] = _positive_int(raw, name, normalized[name])
    if normalized["route_top_k"] > normalized["max_active_tools"]:
        raise ValueError("routing.route_top_k cannot exceed routing.max_active_tools")
    if normalized["default_search_limit"] > normalized["max_search_limit"]:
        raise ValueError(
            "routing.default_search_limit cannot exceed routing.max_search_limit"
        )
    try:
        normalized["min_score"] = float(raw.get("min_score", normalized["min_score"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("routing.min_score must be a number") from exc
    if normalized["min_score"] < 0:
        raise ValueError("routing.min_score cannot be negative")
    return normalized


def _string_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(item).strip() for item in value if str(item).strip()]


def search_terms(value):
    """Create stable English identifier tokens and Chinese bigrams."""
    text = _IDENTIFIER_BOUNDARY.sub(r"\1 \2", str(value or ""))
    text = text.replace("_", " ").replace("-", " ").replace("/", " ").lower()
    terms = list(_LATIN_TOKEN.findall(text))
    for segment in _CJK_SEGMENT.findall(text):
        terms.append(segment)
        if len(segment) == 1:
            continue
        terms.extend(segment[index:index + 2] for index in range(len(segment) - 1))
    return terms


def _definition_chars(definition):
    return len(json.dumps(definition, ensure_ascii=False, sort_keys=True))


class MCPToolRouter:
    """Own the private catalog and the bounded model-visible working set."""

    def __init__(self, tool_index=None, model_tools=None, config=None):
        self.tool_index = dict(tool_index or {})
        self.config = normalize_routing_config(config)
        self.definition_index = {}
        for definition in model_tools or ():
            name = str((definition.get("function") or {}).get("name") or "")
            if name:
                self.definition_index[name] = definition
        self._documents = {
            name: self._build_document(tool)
            for name, tool in self.tool_index.items()
        }
        self._active = set()
        self._pinned = set()
        self._used = set()
        self._discovered = set()
        self._last_query = ""
        self._last_reason = "disabled"
        self._last_ranked = []
        self._dropped = []
        self._active_schema_chars = 0

    @property
    def enabled(self):
        return bool(self.tool_index)

    def _build_document(self, tool):
        weighted_terms = []
        fields = (
            (tool.get("name"), 5),
            (tool.get("remote_name"), 5),
            (tool.get("title"), 4),
            (" ".join(_string_list(tool.get("routing_tags"))), 4),
            (" ".join(_string_list(tool.get("use_when"))), 3),
            (tool.get("server_summary"), 2),
            (tool.get("description"), 1),
        )
        for value, weight in fields:
            for term in search_terms(value):
                weighted_terms.extend([term] * weight)
        return Counter(weighted_terms)

    def _rank(self, query):
        """基于 BM25 全文检索算法 + 领域启发式加权的工具打分与排序逻辑"""
        query_terms = list(dict.fromkeys(search_terms(query)))
        if not query_terms or not self._documents:
            return []
        document_count = len(self._documents)
        lengths = [sum(document.values()) for document in self._documents.values()]
        average_length = sum(lengths) / max(1, len(lengths))
        document_frequency = {
            term: sum(1 for document in self._documents.values() if term in document)
            for term in query_terms
        }
        ranked = []
        for name, document in self._documents.items():
            length = max(1, sum(document.values()))
            score = 0.0
            matched = []
            for term in query_terms:
                frequency = document.get(term, 0)
                if not frequency:
                    continue
                matched.append(term)
                frequency_docs = document_frequency[term]
                # IDF: 关键词越罕见，权重越高
                inverse_frequency = math.log(
                    1 + (document_count - frequency_docs + 0.5) / (frequency_docs + 0.5)
                )
                # BM25 核心公式
                denominator = frequency + 1.5 * (
                    1 - 0.75 + 0.75 * length / max(1.0, average_length)
                )
                score += inverse_frequency * frequency * 2.5 / denominator

            tool = self.tool_index[name]
            lowered_query = str(query or "").lower()
            remote_name = str(tool.get("remote_name") or "").lower()
            server_name = str(tool.get("server") or "").lower()
            if remote_name and remote_name in lowered_query:
                score += 8.0
            if server_name and server_name in lowered_query:
                score += 2.0
            if score >= self.config["min_score"]:
                ranked.append(
                    {
                        "name": name,
                        "score": round(score, 6),
                        "matched_terms": matched[:12],
                    }
                )
        return sorted(ranked, key=lambda item: (-item["score"], item["name"]))

    def search(self, query, limit=None):
        if not self.enabled:
            return []
        if limit is None:
            limit = self.config["default_search_limit"]
        limit = max(1, min(int(limit), self.config["max_search_limit"]))
        return self._rank(query)[:limit]

    def _select_with_budget(self, requested_names):
        selected = []
        dropped = []
        total_chars = 0
        max_tools = self.config["max_active_tools"]
        max_total = self.config["max_total_schema_chars"]
        max_single = self.config["max_single_tool_chars"]
        for name in requested_names:
            if name in selected:
                continue
            definition = self.definition_index.get(name)
            if definition is None:
                dropped.append({"name": name, "reason": "missing_definition"})
                continue
            size = _definition_chars(definition)
            if size > max_single:
                dropped.append({"name": name, "reason": "single_schema_budget", "chars": size})
                continue
            if len(selected) >= max_tools:
                dropped.append({"name": name, "reason": "active_tool_limit", "chars": size})
                continue
            if total_chars + size > max_total:
                dropped.append({"name": name, "reason": "total_schema_budget", "chars": size})
                continue
            selected.append(name)
            total_chars += size
        self._active = set(selected)
        self._active_schema_chars = total_chars
        self._dropped = dropped
        return selected

    def begin_request(self, query):
        self._last_query = str(query or "")
        self._pinned = set()
        self._used = set()
        self._discovered = set()
        always = sorted(
            name for name, tool in self.tool_index.items()
            if tool.get("always_expose")
        )
        all_names = sorted(self.tool_index)
        all_chars = sum(
            _definition_chars(self.definition_index[name])
            for name in all_names
            if name in self.definition_index
        )
        mode = self.config["mode"]
        eager = mode == "eager" or (
            mode == "auto"
            and len(all_names) <= self.config["eager_tool_limit"]
            and all_chars <= self.config["max_total_schema_chars"]
        )
        if eager:
            requested = always + all_names
            self._last_reason = "eager_small_catalog" if mode == "auto" else "eager_configured"
            self._last_ranked = [
                {"name": name, "score": None, "matched_terms": []}
                for name in all_names
            ]
        else:
            self._last_ranked = self.search(query, self.config["route_top_k"])
            requested = always + [item["name"] for item in self._last_ranked]
            self._last_reason = "dynamic_route"
        return self._select_with_budget(requested)

    def activate(self, names, source="discovery"):
        valid = [name for name in names if name in self.tool_index]
        always = {
            name for name, tool in self.tool_index.items()
            if tool.get("always_expose")
        }
        # Keep previously admitted persistent tools before considering new ones.
        # Failed admission must not reserve capacity on a later activation.
        protected = self._active & (always | self._pinned | self._used)
        remaining = self._active - protected - set(valid)
        current = sorted(protected) + valid + sorted(remaining)
        selected = self._select_with_budget(current)
        self._pinned.update(set(valid) & self._active)
        self._last_reason = source
        return selected

    def search_and_activate(self, query, limit=None):
        ranked = self.search(query, limit)
        self._last_query = str(query or "")
        self._last_ranked = ranked
        names = [item["name"] for item in ranked]
        self._discovered.update(names)
        self.activate(names, source="model_tool_search")
        return [self.public_tool_summary(item["name"], item) for item in ranked]

    def resolve_name(self, name):
        requested = str(name or "").strip()
        if requested in self.tool_index:
            return requested
        matches = [
            canonical
            for canonical, tool in self.tool_index.items()
            if tool.get("remote_name") == requested
        ]
        return matches[0] if len(matches) == 1 else None

    def describe_and_activate(self, name):
        canonical = self.resolve_name(name)
        if canonical is None:
            raise ValueError(
                "unknown or ambiguous MCP tool; call mcp_search_tools first and use its canonical name"
            )
        if canonical not in self._active and canonical not in self._discovered:
            raise ValueError(
                "MCP tool was not returned by the current request's routing or tool search; "
                "call mcp_search_tools first"
            )
        self.activate([canonical], source="model_tool_describe")
        if canonical not in self._active:
            raise ValueError(
                "MCP tool cannot fit the configured exposure budget while retaining "
                "persistent tools for this request"
            )
        tool = self.tool_index[canonical]
        return {
            **self.public_tool_summary(canonical),
            "input_schema": tool.get("input_schema") or {
                "type": "object",
                "properties": {},
            },
            "output_schema": tool.get("output_schema"),
            "active": canonical in self._active,
        }

    def public_tool_summary(self, name, ranked=None):
        tool = self.tool_index[name]
        summary = {
            "name": name,
            "server": tool.get("server"),
            "title": tool.get("title"),
            "description": tool.get("description"),
            "risk": "approval_required" if tool.get("risky", True) else "read_only",
            "active": name in self._active,
        }
        if ranked:
            summary["score"] = ranked.get("score")
            summary["matched_terms"] = list(ranked.get("matched_terms") or [])
        return summary

    def mark_used(self, name):
        if name in self.tool_index:
            self._used.add(name)
            self.activate([name], source="tool_used")

    def is_active(self, name):
        return name in self._active

    def active_names(self):
        return tuple(sorted(self._active))

    def model_definitions(self):
        return tuple(
            self.definition_index[name]
            for name in sorted(self._active)
            if name in self.definition_index
        )

    def metadata(self):
        return {
            "mcp_routing_mode": self.config["mode"],
            "mcp_route_reason": self._last_reason,
            "mcp_catalog_tool_count": len(self.tool_index),
            "mcp_exposed_tool_count": len(self._active),
            "mcp_exposed_tools": list(self.active_names()),
            "mcp_schema_chars": self._active_schema_chars,
            "mcp_route_candidates": list(self._last_ranked),
            "mcp_route_dropped": list(self._dropped),
        }

    def capability_prompt(self):
        if not self.enabled:
            return ""
        servers = {}
        for tool in self.tool_index.values():
            server = str(tool.get("server") or "unknown")
            card = servers.setdefault(
                server,
                {
                    "summary": str(tool.get("server_summary") or "External MCP service."),
                    "tags": [],
                    "use_when": [],
                },
            )
            for key in ("routing_tags", "use_when"):
                target = "tags" if key == "routing_tags" else key
                for value in _string_list(tool.get(key)):
                    if value not in card[target]:
                        card[target].append(value)
        lines = [
            "External MCP capability catalog (full schemas are loaded on demand):"
        ]
        for server in sorted(servers):
            card = servers[server]
            detail = card["summary"]
            if card["tags"]:
                detail += " Tags: " + ", ".join(card["tags"])
            if card["use_when"]:
                detail += " Use when: " + " | ".join(card["use_when"])
            lines.append(f"- {server}: {detail}")
        lines.extend(
            [
                "- MCP tools are external capabilities, not evidence that must always be used.",
                "- Use mcp_search_tools when remote/current/account-specific information or an external action may be required and no visible tool clearly fits.",
                "- Use mcp_describe_tool with a canonical search result when its arguments are unclear.",
                "- Do not claim that an external capability is unavailable before searching this catalog when one of the categories above may apply.",
            ]
        )
        return "\n".join(lines)


def compact_query(value, limit=500):
    text = _SPACE.sub(" ", str(value or "")).strip()
    return text[:limit]
