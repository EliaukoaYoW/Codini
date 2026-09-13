"""
远程 MCP 客户端与同步桥接层。

RemoteMCPClient 负责异步连接远程 MCP Server；
MCPBridge 负责让同步的 Codini Runtime 调用异步 MCP Client。
"""

import asyncio
import hashlib
import json
import os
import re
import threading
import warnings
from collections import Counter
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
from mcp.client.streamable_http import streamable_http_client
from mcp import ClientSession

from .routing import normalize_routing_config

config_path = (
    Path(__file__).resolve().parents[2]
    / ".codini"
    / "mcp"
    / "mcp.json"
)

DEFAULT_MAX_RESULT_CHARS = 12000
DEFAULT_MAX_DESCRIPTION_CHARS = 800
DEFAULT_CONNECT_TIMEOUT_SECONDS = 30
EMPTY_MCP_BUNDLE = (None, {}, ())


class MCPStartupWarning(RuntimeWarning):
    """MCP 启动失败但被按可选能力降级时发出的警告。"""


def load_config(config_path):
    with config_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def get_enabled_servers(config):
    """Select enabled entries; validate each inside its startup failure boundary."""
    servers = config.get("servers",{})
    enabled = []
    for name, server_config in servers.items():
        if not server_config.get("enabled"):
            continue
        enabled.append((str(name), dict(server_config)))
    return enabled


def get_enabled_server(config):
    """Backward-compatible helper returning the first enabled server."""
    servers = get_enabled_servers(config)
    return servers[0] if servers else None


def auth_headers(server_config):
    """Resolve per-server credentials without storing secrets in the tool catalog."""
    auth = server_config.get("auth") or {}
    kind = auth.get("type", "none")
    if kind == "none":
        return {}
    if kind not in {"header-env", "optional-header-env"}:
        raise ValueError(f"Unsupported MCP auth type: {kind}")
    header = auth.get("header")
    env = auth.get("env")
    if not isinstance(header, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header):
        raise ValueError("MCP auth.header must be a valid HTTP header name")
    if not isinstance(env, str) or not env.strip():
        raise ValueError("MCP auth.env must name an environment variable")
    value = os.environ.get(env)
    if not value:
        if kind == "optional-header-env":
            return {}
        raise ValueError(f"MCP authentication environment variable is missing: {env}")
    if any(ord(char) < 32 or ord(char) >= 127 for char in value):
        raise ValueError("MCP authentication header contains invalid characters")
    return {header: value}


def _annotation_value(tool, name):
    annotations = getattr(tool, "annotations", None)
    if isinstance(annotations, dict):
        return annotations.get(name)
    return getattr(annotations, name, None)


def infer_tool_risky(tool, server_config):
    """只有服务端明确声明只读且非破坏性时，才允许免审批。"""
    configured_risk = str(server_config.get("risk", "")).strip().lower()
    if configured_risk in {"high", "write", "risky", "destructive"}:
        return True
    if _annotation_value(tool, "destructiveHint") is True:
        return True
    return _annotation_value(tool, "readOnlyHint") is not True


def compact_description(value, limit=DEFAULT_MAX_DESCRIPTION_CHARS):
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 3)].rstrip() + "..."


def safe_tool_segment(value):
    """Keep provider function names portable while preserving stable routing."""
    segment = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "").strip())
    segment = segment.strip("_")
    if not segment:
        raise ValueError("MCP server and tool names must contain a portable character")
    return segment


def portable_tool_name(server_name, remote_name, max_length=64):
    raw = f"mcp__{safe_tool_segment(server_name)}__{safe_tool_segment(remote_name)}"
    if len(raw) <= max_length:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    prefix = raw[: max_length - len(digest) - 2].rstrip("_")
    return f"{prefix}__{digest}"


def _string_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_tool(
        server_name,
        tool,
        server_config,
        max_result_chars=DEFAULT_MAX_RESULT_CHARS,
        max_description_chars=DEFAULT_MAX_DESCRIPTION_CHARS,
):
    title = compact_description(
        getattr(tool, "title", None) or tool.name,
        max_description_chars,
    )
    server_routing = dict(server_config.get("routing") or {})
    always_expose = {
        str(name)
        for name in server_routing.get(
            "always_expose_tools",
            server_config.get("always_expose_tools", []),
        )
    }
    server_summary = compact_description(
        server_routing.get("summary")
        or server_config.get("summary")
        or server_config.get("notes")
        or f"External tools provided by {server_name}.",
        max_description_chars,
    )
    return {
        "name": portable_tool_name(server_name, tool.name),
        "server": server_name,
        "remote_name": tool.name,
        "title": title,
        "description": compact_description(
            getattr(tool, "description", ""),
            max_description_chars,
        ),
        "prompt_description": f"Remote MCP tool from {server_name}: {title}.",
        "server_summary": server_summary,
        "routing_tags": _string_list(
            server_routing.get("tags", server_config.get("tags"))
        ),
        "use_when": _string_list(
            server_routing.get("use_when", server_config.get("use_when"))
        ),
        "always_expose": tool.name in always_expose,
        "input_schema": getattr(tool, "inputSchema", None),
        "output_schema": getattr(tool, "outputSchema", None),
        "max_result_chars": max_result_chars,
        "risky": infer_tool_risky(tool, server_config),
    }


def to_model_tool_definition(tool):
    input_schema = tool["input_schema"]

    if not input_schema:
        input_schema = {
            "type": "object",
            "properties": {}
        }

    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": input_schema,
        },
    }

def extract_text_content(call_result):
    """Render MCP results for the text runtime without silently losing blocks."""
    text_parts = []
    for content in getattr(call_result, "content", []) or []:
        kind = getattr(content, "type", None)
        if kind == "text":
            text_parts.append(getattr(content, "text", "") or "")
        elif kind in {"image", "audio"}:
            text_parts.append(
                f"[MCP {kind}: mimeType={getattr(content, 'mimeType', '')}; "
                "binary content cannot be displayed by this text runtime]"
            )
        elif kind == "resource_link":
            text_parts.append(json.dumps({
                "type": kind, "uri": str(content.uri),
                "name": content.name,
                "description": getattr(content, "description", None),
            }, ensure_ascii=False))
        elif kind == "resource":
            resource = content.resource
            text_parts.append(json.dumps({
                "type": kind, "uri": str(resource.uri),
                "mimeType": getattr(resource, "mimeType", None),
                "text": getattr(resource, "text", None),
                "binary_content_omitted": getattr(resource, "blob", None) is not None,
            }, ensure_ascii=False))
        else:
            text_parts.append(f"[Unsupported MCP content type: {kind}]")
    structured = getattr(call_result, "structuredContent", None)
    if structured is not None:
        text_parts.append("structuredContent: " + json.dumps(structured, ensure_ascii=False))
    text = "\n".join(text_parts)
    if getattr(call_result, "isError", False):
        # Error results bypass the runtime's normal successful-output clipping.
        raise RuntimeError("MCP 工具调用失败: " + (text[:DEFAULT_MAX_RESULT_CHARS] or "服务端未提供错误详情"))
    return text or "[MCP tool completed with empty content]"


def build_tool_bundle(
        server_name,
        tools_result,
        server_config=None,
        max_result_chars=DEFAULT_MAX_RESULT_CHARS,
        max_description_chars=DEFAULT_MAX_DESCRIPTION_CHARS,
):
    server_config = dict(server_config or {})
    enabled_tools = server_config.get("enabled_tools")
    allowed_tools = None if enabled_tools is None else {
        str(name) for name in enabled_tools
    }
    normalized_tools = [
        normalize_tool(
            server_name,
            tool,
            server_config,
            max_result_chars,
            max_description_chars,
        )
        for tool in tools_result.tools
        if allowed_tools is None or tool.name in allowed_tools
    ]

    normalized_names = [tool["name"] for tool in normalized_tools]
    if len(normalized_names) != len(set(normalized_names)):
        duplicates = sorted(
            name for name, count in Counter(normalized_names).items() if count > 1
        )
        raise ValueError(
            "MCP tool name collision after normalization: "
            + ", ".join(duplicates)
        )

    tool_index = {
        tool["name"]: tool
        for tool in normalized_tools
    }

    model_tools = [
        to_model_tool_definition(tool)
        for tool in normalized_tools
    ]

    return normalized_tools, tool_index, model_tools

def _positive_int(config, name, default):
    raw_value = config.get(name, default)
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"transport_defaults.{name} 必须是整数") from exc
    if value <= 0:
        raise ValueError(f"transport_defaults.{name} 必须大于 0")
    return value


def connect_mcp_from_config(path=None, *, enabled=True):
    """Connect all enabled MCP servers and build one namespaced catalog."""
    if not enabled:
        return EMPTY_MCP_BUNDLE

    selected_path = Path(path) if path is not None else config_path
    if not selected_path.is_file():
        return EMPTY_MCP_BUNDLE

    pool = None
    try:
        config = load_config(selected_path)
        top_level_required = bool(config.get("required", False))
        enabled_servers = get_enabled_servers(config)
        if not enabled_servers:
            return EMPTY_MCP_BUNDLE
        routing_config = normalize_routing_config(config.get("routing"))
        transport_defaults = config.get("transport_defaults") or {}
        max_result_chars = _positive_int(
            transport_defaults,
            "max_result_chars",
            DEFAULT_MAX_RESULT_CHARS,
        )
        max_description_chars = _positive_int(
            transport_defaults,
            "max_description_chars",
            DEFAULT_MAX_DESCRIPTION_CHARS,
        )
        request_timeout_seconds = _positive_int(
            transport_defaults,
            "request_timeout_seconds",
            60,
        )
        connect_timeout_seconds = _positive_int(
            transport_defaults,
            "connect_timeout_seconds",
            DEFAULT_CONNECT_TIMEOUT_SECONDS,
        )

        pool = MCPBridgePool(routing_config=routing_config)
        tool_index = {}
        model_tools = []
        for server_name, server_config in enabled_servers:
            server_required = top_level_required or bool(server_config.get("required", False))
            bridge = None
            try:
                if server_config.get("transport") != "streamable-http":
                    raise ValueError(
                        f"Server {server_name} 不支持 transport: "
                        f"{server_config.get('transport')!r}"
                    )
                if not server_config.get("url"):
                    raise ValueError(f"Server {server_name} 缺少 url")
                bridge = MCPBridge(
                    server_config["url"],
                    connect_timeout_seconds=connect_timeout_seconds,
                    request_timeout_seconds=request_timeout_seconds,
                    headers=auth_headers(server_config),
                )
                bridge.start()
                tools_result = bridge.list_tools()
                _, server_index, server_model_tools = build_tool_bundle(
                    server_name,
                    tools_result,
                    server_config,
                    max_result_chars,
                    max_description_chars,
                )
                if not server_index:
                    bridge.close()
                    continue
                duplicates = set(tool_index).intersection(server_index)
                if duplicates:
                    raise ValueError(
                        "MCP tool name collision after normalization: "
                        + ", ".join(sorted(duplicates))
                    )
                pool.add(server_name, bridge)
                tool_index.update(server_index)
                model_tools.extend(server_model_tools)
            except Exception as exc:
                try:
                    if bridge is not None:
                        bridge.close()
                except Exception:
                    pass
                if server_required:
                    raise RuntimeError(
                        f"required MCP server {server_name} failed: {exc}"
                    ) from exc
                warnings.warn(
                    f"MCP server {server_name} failed and was disabled: {exc}",
                    MCPStartupWarning,
                    stacklevel=2,
                )
        if not tool_index:
            pool.close()
            return EMPTY_MCP_BUNDLE
        return pool, tool_index, tuple(model_tools)
    except Exception as exc:
        if pool is not None:
            try:
                pool.close()
            except Exception:
                pass
        if bool(locals().get("top_level_required", False)):
            raise RuntimeError(f"required MCP startup failed: {exc}") from exc
        if isinstance(exc, RuntimeError) and "required MCP server" in str(exc):
            raise
        warnings.warn(
            f"MCP 启动失败，已禁用远程工具: {exc}",
            MCPStartupWarning,
            stacklevel=2,
        )
        return EMPTY_MCP_BUNDLE

class RemoteMCPClient:
    def __init__(
            self,
            url,
            connect_timeout_seconds=DEFAULT_CONNECT_TIMEOUT_SECONDS,
            request_timeout_seconds=60,
            headers=None,
    ):
        self.url = url
        self.headers = dict(headers or {})
        self.connect_timeout_seconds = connect_timeout_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.exit_stack = AsyncExitStack()
        self.session = None
        self.initialize_result = None

    async def connect(self):
        timeout = httpx.Timeout(
            self.request_timeout_seconds,
            connect=self.connect_timeout_seconds,
        )
        http_client = await self.exit_stack.enter_async_context(
            httpx.AsyncClient(timeout=timeout, headers=self.headers)
        )
        read, write, _ = (
            await self.exit_stack.enter_async_context(
                streamable_http_client(self.url, http_client=http_client)
            )
        )
        self.session = await self.exit_stack.enter_async_context(
            ClientSession(
                read,
                write,
                read_timeout_seconds=timedelta(
                    seconds=self.request_timeout_seconds
                ),
            )
        )
        self.initialize_result = await self.session.initialize()

        return self.initialize_result

    async def list_tools(self):
        if self.session is None:
            raise RuntimeError("MCP client is not connected")
        page = await self.session.list_tools()
        tools = list(getattr(page, "tools", None) or [])
        cursor = getattr(page, "nextCursor", None)
        seen_cursors = set()
        while cursor:
            if cursor in seen_cursors:
                raise RuntimeError("MCP tools/list returned a repeated pagination cursor")
            seen_cursors.add(cursor)
            page = await self.session.list_tools(cursor=cursor)
            tools.extend(getattr(page, "tools", None) or [])
            cursor = getattr(page, "nextCursor", None)
        return SimpleNamespace(tools=tools)

    async def call_tool(self, remote_name, arguments):
        if self.session is None:
            raise RuntimeError("MCP client is not connected")

        return await self.session.call_tool(
            remote_name,
            arguments=arguments,
        )

    async def call_tool_text(self, remote_name, arguments):
        call_result = await self.call_tool(
            remote_name,
            arguments,
        )

        return extract_text_content(call_result)

    async def close(self):
        await self.exit_stack.aclose()


class MCPBridge:
    def __init__(
            self,
            url,
            connect_timeout_seconds=DEFAULT_CONNECT_TIMEOUT_SECONDS,
            request_timeout_seconds=60,
            headers=None,
    ):
        self.connect_timeout_seconds = connect_timeout_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.client = RemoteMCPClient(
            url,
            connect_timeout_seconds,
            request_timeout_seconds,
            headers=headers,
        )
        self.loop = None
        self.thread = None
        self.loop_ready = threading.Event()
        self.lifecycle_future = None
        self.shutdown_event = None
        self.connection_ready = threading.Event()
        self.lifecycle_closed = threading.Event()
        self.start_error = None
        self.close_error = None

    def _run_loop(self):
        # 1. 为当前子线程创建一个全新的、独立的事件循环
        self.loop = asyncio.new_event_loop()
        # 2. 将此循环设置为当前子线程的上下文默认事件循环
        asyncio.set_event_loop(self.loop)
        # 3. 线程间信号同步：通知主线程“事件循环已经创建完毕并准备就绪”
        self.loop_ready.set()
        # 4. 阻塞运行事件循环，持续监听并处理所有的异步任务和网络 I/O
        try:
            self.loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.loop_ready.clear()

        self.thread = threading.Thread(
            target=self._run_loop,
            name="mcp-event-loop",
            daemon=True,
        )
        self.thread.start()

        if not self.loop_ready.wait(timeout=10):
            raise TimeoutError("MCP 事件循环启动超时")

        if self.loop is None:
            raise RuntimeError("MCP 事件循环没有成功创建")

        self.connection_ready.clear()
        self.lifecycle_closed.clear()
        self.start_error = None
        self.close_error = None

        self.lifecycle_future = asyncio.run_coroutine_threadsafe(
            self._session_lifecycle(),
            self.loop,
        )

        if not self.connection_ready.wait(timeout=self.connect_timeout_seconds):
            self.lifecycle_future.cancel()
            raise TimeoutError("MCP 连接建立超时")

        if self.start_error is not None:
            raise RuntimeError("MCP 连接建立失败") from self.start_error

    def _submit(self, coroutine):
        if self.loop is None:
            coroutine.close()
            raise RuntimeError("MCP Bridge 尚未启动")

        if self.thread is None or not self.thread.is_alive():
            coroutine.close()
            raise RuntimeError("MCP Bridge 线程没有运行")
            
        # 主线程/同步函数中调用异步协程并等待结果：
        future = asyncio.run_coroutine_threadsafe(
            coroutine,
            self.loop,
        )

        try:
            return future.result(timeout=self.request_timeout_seconds)
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError("MCP 工具调用超时") from exc

    def list_tools(self):
        return self._submit(
            self.client.list_tools()
        )

    def call_tool(self, remote_name, arguments):
        return self._submit(
            self.client.call_tool(
                remote_name,
                arguments,
            )
        )

    def call_tool_text(self, remote_name, arguments):
        return self._submit(
            self.client.call_tool_text(
                remote_name,
                arguments,
            )
        )

    async def _session_lifecycle(self):
        self.shutdown_event = asyncio.Event()
        lifecycle_error = None
        try:
            await self.client.connect()
            self.connection_ready.set()
            await self.shutdown_event.wait()
        except BaseException as error:
            lifecycle_error = error
            self.start_error = error
            self.connection_ready.set()
        finally:
            try:
                await self.client.close()
            except BaseException as close_error:
                self.close_error = close_error
                if lifecycle_error is None:
                    lifecycle_error = close_error
                    self.start_error = close_error
                    self.connection_ready.set()
            finally:
                self.lifecycle_closed.set()
        if lifecycle_error is not None:
            raise lifecycle_error

    def close(self):
        if self.loop is None:
            return

        loop = self.loop
        thread = self.thread
        lifecycle_future = self.lifecycle_future
        close_timeout = min(5, self.connect_timeout_seconds)

        if lifecycle_future is not None:
            if not self.lifecycle_closed.is_set():
                if self.shutdown_event is not None:
                    loop.call_soon_threadsafe(self.shutdown_event.set)
                if not self.lifecycle_closed.wait(timeout=close_timeout):
                    lifecycle_future.cancel()
                    if not self.lifecycle_closed.wait(timeout=close_timeout):
                        # Keep the loop running and retain handles so cleanup can
                        # finish and the caller can retry close().
                        raise TimeoutError("MCP session cleanup is still running")
        if not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=close_timeout)
            if thread.is_alive():
                raise TimeoutError("MCP event-loop thread is still shutting down")
        self.loop = None
        self.thread = None
        self.lifecycle_future = None
        if self.close_error is not None:
            raise RuntimeError("MCP session cleanup failed") from self.close_error


class MCPBridgePool:
    """Dispatch namespaced tools to independent per-server MCP sessions."""

    def __init__(self, routing_config=None):
        self.routing_config = normalize_routing_config(routing_config)
        self.bridges = {}

    def add(self, server_name, bridge):
        name = str(server_name)
        if name in self.bridges:
            raise ValueError(f"duplicate MCP server: {name}")
        self.bridges[name] = bridge

    def call_tool_text(self, server_name, remote_name, arguments):
        bridge = self.bridges.get(str(server_name))
        if bridge is None:
            raise RuntimeError(f"MCP server is not connected: {server_name}")
        return bridge.call_tool_text(remote_name, arguments)

    def close(self):
        errors = []
        for server_name, bridge in reversed(tuple(self.bridges.items())):
            try:
                bridge.close()
            except Exception as exc:
                errors.append((server_name, exc))
            else:
                del self.bridges[server_name]
        if errors:
            names = ", ".join(name for name, _ in errors)
            raise RuntimeError(f"failed to close MCP servers: {names}") from errors[0][1]
