from dataclasses import dataclass, field
from enum import Enum
from http.client import RemoteDisconnected
import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Mapping


@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: Any

    def to_dict(self):
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class ModelToolCall:
    """Provider 原始工具调用被转换后的统一表示。"""

    name: str
    arguments: Mapping[str, Any] | str = field(default_factory=dict)
    call_id: str = ""

    def arguments_dict(self):
        if isinstance(self.arguments, Mapping):
            return dict(self.arguments)
        try:
            parsed = json.loads(str(self.arguments))
        except (TypeError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}

    def to_dict(self):
        return {
            "id": self.call_id,
            "name": self.name,
            "arguments": self.arguments_dict(),
}


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0

    @classmethod
    def from_mapping(cls, payload=None):
        payload = dict(payload or {})
        details = (
            payload.get("input_tokens_details")
            or payload.get("prompt_tokens_details")
            or {}
        )
        input_tokens = payload.get("input_tokens", payload.get("prompt_tokens", 0))
        output_tokens = payload.get("output_tokens", payload.get("completion_tokens", 0))
        total_tokens = payload.get("total_tokens")
        cached_tokens = details.get("cached_tokens", payload.get("cached_tokens", 0))
        input_tokens = int(input_tokens or 0)
        output_tokens = int(output_tokens or 0)
        total_tokens = int(total_tokens or input_tokens + output_tokens)
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_tokens=int(cached_tokens or 0),
        )

    def to_metadata(self):
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "prompt_tokens": self.input_tokens,
            "completion_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_hit": self.cached_tokens > 0,
        }


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[ModelMessage, ...]
    system_prompt: str | None = None
    tools: tuple[Mapping[str, Any], ...] = ()
    temperature: float | None = None
    max_tokens: int = 0
    stream: bool = False
    prompt_cache_key: str | None = None
    prompt_cache_retention: str | None = None

    def __post_init__(self):
        messages = tuple(
            item if isinstance(item, ModelMessage) else ModelMessage(**item)
            for item in self.messages
        )
        object.__setattr__(self, "messages", messages)
        object.__setattr__(self, "tools", tuple(dict(item) for item in self.tools))

    @classmethod
    def from_prompt(cls, prompt, max_tokens, **kwargs):
        return cls(
            messages=(ModelMessage(role="user", content=str(prompt)),),
            max_tokens=int(max_tokens),
            **kwargs,
        )

    def message_payload(self):
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.extend(message.to_dict() for message in self.messages)
        return messages


@dataclass(frozen=True)
class ModelResponse:
    assistant_text: str = ""
    tool_calls: tuple[ModelToolCall, ...] = ()
    usage: ModelUsage = field(default_factory=ModelUsage)
    finish_reason: str | None = None
    raw_response: Any = None
    provider: str = ""

    def __post_init__(self):
        object.__setattr__(
            self,
            "tool_calls",
            tuple(
                item if isinstance(item, ModelToolCall) else ModelToolCall(**item)
                for item in self.tool_calls
            ),
        )

    @property
    def text(self):
        return self.assistant_text

    def trace_text(self):
        """Return a provider-neutral representation for Trace/Viewer display."""
        if self.assistant_text:
            return self.assistant_text
        if not self.tool_calls:
            return ""
        return "\n".join(
            "<tool_call>"
            + json.dumps(
                {"name": call.name, "args": call.arguments_dict()},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "</tool_call>"
            for call in self.tool_calls
        )

    @classmethod
    def from_legacy(cls, value, metadata=None, provider=""):
        metadata = dict(metadata or {})
        usage = ModelUsage.from_mapping(metadata)
        assistant_text = str(value or "")
        tool_calls = parse_provider_text_tool_calls(assistant_text) if assistant_text else ()
        if tool_calls:
            assistant_text = ""
        return cls(
            assistant_text=assistant_text,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=metadata.get("finish_reason"),
            raw_response=metadata.get("raw_response"),
            provider=provider,
        )

    def to_metadata(self):
        metadata = self.usage.to_metadata()
        if self.finish_reason:
            metadata["finish_reason"] = self.finish_reason
        if self.provider:
            metadata["provider"] = self.provider
        if self.tool_calls:
            metadata["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        return metadata


class ModelErrorKind(str, Enum):
    TRANSIENT = "transient"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    CONTEXT_LENGTH = "context_length"
    REFUSAL = "refusal"
    TOOL_FORMAT = "tool_format"
    INVALID_RESPONSE = "invalid_response"
    UNKNOWN = "unknown"


class ModelProviderError(RuntimeError):
    def __init__(
        self,
        kind,
        message,
        *,
        provider="",
        status_code=None,
        code="",
        retryable=False,
        raw=None,
        attempts=1,
    ):
        self.kind = ModelErrorKind(kind)
        self.provider = str(provider or "")
        self.status_code = status_code
        self.code = str(code or "")
        self.retryable = bool(retryable)
        self.raw = raw
        self.attempts = int(attempts or 1)
        super().__init__(str(message))

    def with_attempts(self, attempts):
        self.attempts = int(attempts or 1)
        return self

    def to_dict(self):
        return {
            "error_type": self.kind.value,
            "error_code": self.code,
            "provider": self.provider,
            "http_status": self.status_code,
            "retryable": self.retryable,
            "attempts": self.attempts,
            "message": str(self),
        }


def _body_payload(body):
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    text = str(body or "")
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {}, text
    if not isinstance(payload, dict):
        return {}, text
    nested = payload.get("error")
    if isinstance(nested, dict):
        return nested, text
    return payload, text


def normalize_provider_error(error, provider=""):
    if isinstance(error, ModelProviderError):
        return error

    status_code = getattr(error, "code", None)
    body = ""
    if isinstance(error, urllib.error.HTTPError):
        try:
            body = error.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
    payload, body_text = _body_payload(body)
    message = str(payload.get("message") or payload.get("detail") or body_text or error)
    code = payload.get("code") or payload.get("type") or ""
    lower = f"{message} {body_text}".lower()

    if status_code in {401, 403} or any(value in lower for value in ("unauthorized", "invalid api key", "authentication")):
        kind = ModelErrorKind.AUTHENTICATION
    elif status_code == 429 or any(value in lower for value in ("rate limit", "rate_limit", "too many requests")):
        kind = ModelErrorKind.RATE_LIMIT
    elif any(value in lower for value in ("context length", "context_length", "maximum context", "too many tokens")):
        kind = ModelErrorKind.CONTEXT_LENGTH
    elif any(value in lower for value in ("refusal", "refused", "safety policy")):
        kind = ModelErrorKind.REFUSAL
    elif any(value in lower for value in ("tool call", "tool_call", "function call", "malformed tool")):
        kind = ModelErrorKind.TOOL_FORMAT
    elif status_code in {408, 409, 425, 500, 502, 503, 504} or isinstance(
        error,
        (urllib.error.URLError, TimeoutError, ConnectionRefusedError, RemoteDisconnected),
    ) or "timeout" in lower:
        kind = ModelErrorKind.TRANSIENT
    elif isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        kind = ModelErrorKind.INVALID_RESPONSE
    else:
        kind = ModelErrorKind.UNKNOWN

    retryable = kind in {ModelErrorKind.TRANSIENT, ModelErrorKind.RATE_LIMIT}
    return ModelProviderError(
        kind,
        message,
        provider=provider,
        status_code=status_code,
        code=code,
        retryable=retryable,
        raw=payload or body_text or error,
    )


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    backoff_factor: float = 2.0
    max_delay: float = 4.0

    def delay(self, retry_index):
        return min(
            self.max_delay,
            self.base_delay * (self.backoff_factor ** max(0, int(retry_index))),
        )


DEFAULT_RETRY_POLICY = RetryPolicy()


def execute_with_retry(operation, *, provider, timeout, policy=DEFAULT_RETRY_POLICY):
    """执行一次 Provider 请求；timeout 是整组尝试共享的总时限。"""
    deadline = time.monotonic() + float(timeout) if timeout else None
    last_error = None
    for attempt in range(max(1, policy.max_attempts)):
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            error = normalize_provider_error(TimeoutError("provider request deadline exceeded"), provider)
            raise error.with_attempts(attempt or 1) from last_error
        try:
            return operation(remaining)
        except Exception as exc:
            error = normalize_provider_error(exc, provider).with_attempts(attempt + 1)
            last_error = error
            if not error.retryable or attempt + 1 >= policy.max_attempts:
                raise error from exc
            delay = policy.delay(attempt)
            if remaining is not None:
                delay = min(delay, max(0.0, remaining))
            time.sleep(delay)
    raise last_error


def _parse_call_args(text):
    args = {}
    pattern = re.compile(
        r"""(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^,\s]+)"""
    )
    for match in pattern.finditer(str(text)):
        value = match.group("value").strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            try:
                value = json.loads(value) if value.startswith('"') else value[1:-1].replace("\\'", "'")
            except Exception:
                value = value[1:-1]
        elif re.fullmatch(r"-?\d+", value):
            value = int(value)
        elif re.fullmatch(r"-?\d+\.\d+", value):
            value = float(value)
        elif value.lower() in {"true", "false"}:
            value = value.lower() == "true"
        elif value.lower() in {"none", "null"}:
            value = None
        args[match.group("key")] = value
    return args


def _parse_longcat(body):
    body = re.sub(r"/\s*$", "", body.strip()).strip()
    native_match = re.match(
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?=<longcat_arg_key>|$)",
        body,
        re.S,
    )
    native_pairs = re.findall(
        r"<longcat_arg_key>(?P<key>.*?)</longcat_arg_key>\s*"
        r"<longcat_arg_value>(?P<value>.*?)</longcat_arg_value>",
        body,
        re.S,
    )
    if native_match and native_pairs:
        args = {}
        for raw_key, raw_value in native_pairs:
            key = raw_key.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                return None
            value_text = raw_value.strip()
            try:
                value = json.loads(value_text)
            except json.JSONDecodeError:
                value = value_text
            args[key] = value
        return ModelToolCall(native_match.group("name"), args)

    call_match = re.match(
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<args>.*)\)\s*$",
        body,
        re.S,
    )
    if call_match:
        name, args_text = call_match.group("name"), call_match.group("args")
    else:
        attr_match = re.match(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+(?P<args>.*)$", body, re.S)
        if not attr_match:
            return None
        name, args_text = attr_match.group("name"), attr_match.group("args")
    try:
        decoded_args = json.loads(args_text)
    except (json.JSONDecodeError, TypeError):
        decoded_args = None
    if isinstance(decoded_args, dict):
        args = decoded_args.get("args")
        args = args if isinstance(args, dict) else decoded_args
    else:
        args = _parse_call_args(args_text)
    return ModelToolCall(name, args)


def _parse_json_tool_body(body):
    try:
        payload = json.loads(body.strip())
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    function = payload.get("function") if isinstance(payload.get("function"), dict) else payload
    name = str(function.get("name") or payload.get("name") or "").strip()
    arguments = function.get("arguments", function.get("args", payload.get("arguments", {})))
    if not name:
        return None
    return ModelToolCall(name=name, arguments=arguments)


def parse_provider_text_tool_calls(text, formats=("longcat_tool_call", "tool_call", "tool")):
    """按 Provider 适配器声明的格式解析文本工具调用。"""
    text = str(text or "")
    for tag in formats:
        match = re.search(rf"<{re.escape(tag)}>(?P<body>.*?)(?:</{re.escape(tag)}>|$)", text, re.S)
        if not match:
            continue
        body = match.group("body")
        if tag == "longcat_tool_call":
            call = _parse_longcat(body)
        else:
            call = _parse_json_tool_body(body)
        if call:
            return (call,)
    return ()

def _urlopen_interruptible(request, timeout):
    """urlopen wrapped in a thread so KeyboardInterrupt can escape the blocking socket call on Windows."""
    result = [None]
    error = [None]
    done = threading.Event()

    def _worker():
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                result[0] = (resp.read().decode("utf-8"), dict(resp.headers))
        except Exception as exc:
            error[0] = exc
        finally:
            done.set()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    while not done.wait(timeout=0.1):
        pass  # allows KeyboardInterrupt to propagate from main thread
    if error[0] is not None:
        raise error[0]
    return result[0]  # (body_text, headers_dict)


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base

def _extract_openai_text(data):
    if data.get("output_text"):
        return data["output_text"]

    for item in data.get("output", []):
        for content in item.get("content", []):
            if isinstance(content, dict):
                text = content.get("text")
                if text:
                    return text

    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        return text
    return ""


def _extract_openai_tool_calls(data):
    """把 Chat Completions 和 Responses 风格的调用统一成 ModelToolCall。"""
    calls = []
    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {}) or {}
        for item in message.get("tool_calls", []) or []:
            function = item.get("function", {}) or {}
            name = str(function.get("name") or item.get("name") or "").strip()
            if name:
                calls.append(
                    ModelToolCall(
                        name=name,
                        arguments=function.get("arguments", item.get("arguments", {})),
                        call_id=str(item.get("id") or ""),
                    )
                )

    for item in data.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") not in {"function_call", "tool_call"}:
            continue
        name = str(item.get("name") or "").strip()
        if name:
            calls.append(
                ModelToolCall(
                    name=name,
                    arguments=item.get("arguments", {}),
                    call_id=str(item.get("call_id") or item.get("id") or ""),
                )
            )
    return tuple(calls)


def _extract_finish_reason(data):
    choices = data.get("choices", [])
    if choices:
        reason = choices[0].get("finish_reason")
        if reason:
            return str(reason)
    return data.get("finish_reason") or data.get("status")


def _model_response_from_openai_payload(data, provider, fallback_text=""):
    usage = ModelUsage.from_mapping(data.get("usage") or {})
    return ModelResponse(
        assistant_text=_extract_openai_text(data) or fallback_text,
        tool_calls=_extract_openai_tool_calls(data),
        usage=usage,
        finish_reason=_extract_finish_reason(data),
        raw_response=data,
        provider=provider,
    )


def _normalize_text_tool_response(response):
    """把 Provider 文本协议在适配层转换成统一 ToolCall。"""
    if response.tool_calls or not response.assistant_text:
        return response
    calls = parse_provider_text_tool_calls(response.assistant_text)
    if not calls:
        return response
    return ModelResponse(
        assistant_text="",
        tool_calls=calls,
        usage=response.usage,
        finish_reason=response.finish_reason or "tool_calls",
        raw_response=response.raw_response or response.assistant_text,
        provider=response.provider,
    )


def _extract_openai_text_from_sse(body_text):
    last_response = None
    deltas = []
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
            continue
        if event_type == "response.output_text.done":
            text = event.get("text")
            if text and isinstance(text, str):
                return text
        part = event.get("part")
        if isinstance(part, dict):
            text = part.get("text")
            if text and isinstance(text, str):
                return text
        item = event.get("item")
        if isinstance(item, dict):
            text = _extract_openai_text({"output": [item]})
            if text:
                return text
        response = event.get("response")
        if isinstance(response, dict):
            last_response = response
            text = _extract_openai_text(response)
            if text:
                return text
        text = _extract_openai_text(event)
        if text:
            return text
    if deltas:
        return "".join(deltas)
    if isinstance(last_response, dict):
        return _extract_openai_text(last_response)
    return ""


def _extract_openai_response_from_sse(body_text):
    last_response = None
    deltas = []
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        response = event.get("response")
        if isinstance(response, dict):
            last_response = response
            if event.get("type") == "response.completed":
                text = _extract_openai_text(response)
                if text:
                    return text, response
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
        elif event_type == "response.output_text.done":
            text = event.get("text")
            if text and isinstance(text, str):
                return text, last_response or {}
        else:
            text = _extract_openai_text(event)
            if text:
                return text, event
    if deltas:
        return "".join(deltas), last_response or {}
    if isinstance(last_response, dict):
        return _extract_openai_text(last_response), last_response
    return "", {}


def _extract_usage_cache_details(data):
    # 把不同 OpenAI-compatible 返回里的 usage 字段整理成统一结构，让 runtime/trace/report 不需要关心 provider 细节。
    usage = data.get("usage") or {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tokens = int(input_details.get("cached_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": cached_tokens,
        "cache_hit": cached_tokens > 0,
    }


class _ModelClientBase:
    """保留 complete() 兼容入口，并在内部提供统一响应。"""

    def complete_response(self, prompt, max_new_tokens, **kwargs):
        # 旧的 FakeClient/实验客户端会覆盖 complete；优先走它们，避免破坏既有测试契约。
        if type(self).complete is not _ModelClientBase.complete:
            text = self.complete(prompt, max_new_tokens, **kwargs)
            response = ModelResponse.from_legacy(
                text,
                getattr(self, "last_completion_metadata", {}),
                provider=getattr(self, "provider", ""),
            )
        else:
            request = ModelRequest.from_prompt(
                prompt,
                max_new_tokens,
                system_prompt=kwargs.get("system_prompt"),
                tools=tuple(kwargs.get("tools") or ()),
                temperature=kwargs.get("temperature"),
                stream=bool(kwargs.get("stream", False)),
                prompt_cache_key=kwargs.get("prompt_cache_key"),
                prompt_cache_retention=kwargs.get("prompt_cache_retention"),
            )
            response = self._complete_request(request)
        self.last_completion_metadata = {
            **response.to_metadata(),
            "prompt_cache_supported": bool(getattr(self, "supports_prompt_cache", False)),
            "prompt_cache_key": getattr(response, "prompt_cache_key", None)
            or kwargs.get("prompt_cache_key"),
            "prompt_cache_retention": kwargs.get("prompt_cache_retention"),
        }
        return response

    def complete(self, prompt, max_new_tokens, **kwargs):
        """现有上层代码继续使用的兼容入口。"""
        return self.complete_response(prompt, max_new_tokens, **kwargs).assistant_text

    def _complete_request(self, request):
        raise NotImplementedError


class FakeModelClient(_ModelClientBase):
    def __init__(self, outputs):
        self.outputs = outputs
        self.prompts = []
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        del max_new_tokens, kwargs
        self.prompts.append(prompt)
        self.last_completion_metadata = {}
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)


class OllamaModelClient(_ModelClientBase):
    def __init__(self, model, host=None, temperature=0.2, top_p=0.9, timeout=300, *, base_url=None, api_key=None):
        del api_key
        self.model = model
        self.host = str(base_url or host or "http://127.0.0.1:11434").rstrip("/")
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def _complete_request(self, request):
        payload = {
            "model": self.model,
            "prompt": "\n\n".join(str(item.content) for item in request.messages),
            "stream": request.stream,
            "raw": False,
            "think": False,
            "options": {
                "num_predict": request.max_tokens,
                "temperature": self.temperature if request.temperature is None else request.temperature,
                "top_p": self.top_p,
            },
        }
        http_request = urllib.request.Request(
            f"{self.host}/api/v1/completions",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload).encode("utf-8"),
        )
        body_text, _ = execute_with_retry(
            lambda timeout: _urlopen_interruptible(http_request, timeout),
            provider="ollama",
            timeout=self.timeout,
        )
        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise ModelProviderError("invalid_response", f"Ollama response is not valid JSON: {exc}", provider="ollama") from exc
        if data.get("error"):
            raise normalize_provider_error(RuntimeError(str(data["error"])), "ollama")
        return _normalize_text_tool_response(ModelResponse(
            assistant_text=str(data.get("response", "")),
            tool_calls=_extract_openai_tool_calls(data),
            usage=ModelUsage.from_mapping(data.get("usage") or data),
            finish_reason=data.get("finish_reason"),
            raw_response=data,
            provider="ollama",
        ))


class _OpenAICompatibleModelClient(_ModelClientBase):
    output_token_field = "max_output_tokens"
    provider_name = "openai-compatible"

    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.last_completion_metadata = {}

    def _build_payload(self, request):
        payload = {
            "model": self.model,
            "messages": request.message_payload(),
            self.output_token_field: request.max_tokens,
            "stream": request.stream,
        }
        temperature = self.temperature if request.temperature is None else request.temperature
        if temperature is not None:
            payload["temperature"] = temperature
        if request.tools:
            payload["tools"] = list(request.tools)
        if self.supports_prompt_cache and request.prompt_cache_key:
            payload["prompt_cache_key"] = request.prompt_cache_key
        if self.supports_prompt_cache and request.prompt_cache_retention:
            payload["prompt_cache_retention"] = request.prompt_cache_retention
        return payload

    def _complete_request(self, request):
        payload = self._build_payload(request)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        http_request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            method="POST",
            headers=headers,
            data=json.dumps(payload).encode("utf-8"),
        )
        body_text, response_headers = execute_with_retry(
            lambda timeout: _urlopen_interruptible(http_request, timeout),
            provider=self.provider_name,
            timeout=self.timeout,
        )
        content_type = response_headers.get("Content-Type", "")
        if content_type.startswith("text/event-stream") or body_text.lstrip().startswith("data:"):
            text, response_data = _extract_openai_response_from_sse(body_text)
            if response_data and isinstance(response_data, dict):
                return _normalize_text_tool_response(_model_response_from_openai_payload(
                    response_data,
                    self.provider_name,
                    fallback_text=text,
                ))
            if text:
                return _normalize_text_tool_response(ModelResponse(assistant_text=text, provider=self.provider_name))
            raise ModelProviderError(
                "invalid_response",
                f"{self.provider_name} response did not contain text or tool calls",
                provider=self.provider_name,
            )
        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise ModelProviderError(
                "invalid_response",
                f"{self.provider_name} response is not valid JSON: {exc}",
                provider=self.provider_name,
            ) from exc
        if data.get("error"):
            raise normalize_provider_error(RuntimeError(str(data["error"])), self.provider_name)
        return _normalize_text_tool_response(_model_response_from_openai_payload(data, self.provider_name))


class OpenAICompatibleModelClient(_OpenAICompatibleModelClient):
    def __init__(self, model, base_url, api_key, temperature, timeout):
        super().__init__(model, base_url, api_key, temperature, timeout)
        self.supports_prompt_cache = any(
            host in self.base_url for host in ("openai.com", "right.codes", "longcat", "deepseek")
        )


class SiliconflowModelClient(_OpenAICompatibleModelClient):
    output_token_field = "max_tokens"
    provider_name = "siliconflow"

    def __init__(self, model, base_url, api_key, temperature, timeout):
        super().__init__(model, base_url, api_key, temperature, timeout)
        self.supports_prompt_cache = any(
            host in self.base_url for host in ("openai.com", "right.codes", "siliconflow.cn")
        )


@dataclass(frozen=True)
class ModelTarget:
    """一个可切换模型及其实际 Provider。"""

    provider: str
    model: str

    def to_dict(self):
        return {"provider": self.provider, "model": self.model}


def configured_model_names(default_model="", model_list=""):
    """合并默认模型和逗号分隔模型列表，保持顺序并去重。"""
    default_model = str(default_model or "").strip()
    if "," in default_model:
        raise ValueError("*_MODEL 只能配置一个默认模型；多个模型请使用 *_MODELS。")

    names = []
    if default_model:
        names.append(default_model)
    names.extend(
        item.strip()
        for item in str(model_list or "").split(",")
        if item.strip()
    )
    return tuple(dict.fromkeys(names))


@dataclass(frozen=True)
class ProviderSpec:
    """描述 Provider 的配置来源和模型客户端。"""

    client_cls: type
    model_env: str
    base_url_envs: str | tuple[str, ...]
    api_key_envs: str | tuple[str, ...]
    models_env: str | None = None
    api_key_required: bool = True

    def __post_init__(self):
        if not self.models_env:
            object.__setattr__(self, "models_env", f"{self.model_env}S")
        for field_name in ("base_url_envs", "api_key_envs"):
            value = getattr(self, field_name)
            if isinstance(value, str):
                object.__setattr__(self, field_name, (value,))
            else:
                object.__setattr__(self, field_name, tuple(value))


def provider_spec(provider):
    try:
        return PROVIDER_SPECS[provider]
    except KeyError as exc:
        supported = ", ".join(PROVIDER_SPECS)
        raise ValueError(
            f"不支持的 Provider：{provider}；可选值：{supported}"
        ) from exc


def provider_names():
    return tuple(PROVIDER_SPECS)


def create_model_client(provider, model, base_url, api_key, temperature, timeout):
    """根据 Provider 配置创建统一模型客户端。"""
    spec = provider_spec(provider)
    client = spec.client_cls(
        model=model,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        timeout=timeout,
    )
    client.provider = provider
    return client


PROVIDER_SPECS = {
    "openai": ProviderSpec(
        client_cls=OpenAICompatibleModelClient,
        model_env="OPENAI_MODEL",
        base_url_envs="OPENAI_BASE_URL",
        api_key_envs="OPENAI_API_KEY",
    ),
    "deepseek": ProviderSpec(
        client_cls=OpenAICompatibleModelClient,
        model_env="DEEPSEEK_MODEL",
        base_url_envs="DEEPSEEK_BASE_URL",
        api_key_envs="DEEPSEEK_API_KEY",
    ),
    "longcat": ProviderSpec(
        client_cls=OpenAICompatibleModelClient,
        model_env="LONGCAT_MODEL",
        base_url_envs="LONGCAT_BASE_URL",
        api_key_envs="LONGCAT_API_KEY",
    ),
    "stepfun": ProviderSpec(
        client_cls=OpenAICompatibleModelClient,
        model_env="STEPFUN_MODEL",
        base_url_envs="STEPFUN_BASE_URL",
        api_key_envs="STEPFUN_API_KEY",
    ),
    "siliconflow": ProviderSpec(
        client_cls=SiliconflowModelClient,
        model_env="SILICONFLOW_MODEL",
        base_url_envs="SILICONFLOW_BASE_URL",
        api_key_envs="SILICONFLOW_API_KEY",
    ),
    "ollama": ProviderSpec(
        client_cls=OllamaModelClient,
        model_env="OLLAMA_MODEL",
        base_url_envs="OLLAMA_BASE_URL",
        api_key_envs="OLLAMA_API_KEY",
        api_key_required=False,
    ),
}
