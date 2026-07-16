from .cli import build_agent, build_arg_parser, build_welcome, main
from .models import FakeModelClient, OllamaModelClient, OpenAICompatibleModelClient,SiliconflowModelClient
from .runtime import MiniAgent, Codini, SessionStore
from .run_store import RunStore
from .workspace import WorkspaceContext

def __getattr__(name):
    if name in _CLI_EXPORTS:
        from . import cli
        return getattr(cli, name)
    raise AttributeError(f"module 'codini' has no attribute {name!r}")

__all__ = [
    "FakeModelClient",
    "Codini",
    "RunStore",
    "build_agent",
    "build_arg_parser",
    "build_welcome",
    "main",
    "MiniAgent",
    "OllamaModelClient",
    "OpenAICompatibleModelClient",
    "SiliconflowModelClient",
    "SessionStore",
    "WorkspaceContext",
]
