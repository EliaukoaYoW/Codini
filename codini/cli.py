"""
命令行入口
"""

import argparse
from json import load
import os
import shutil
import sys
import textwrap

from dotenv import load_dotenv
from .models import OpenAICompatibleModelClient, SiliconflowModelClient
from .runtime import Codini, SessionStore
from .sandbox import create_sandbox
from .workspace import WorkspaceContext, middle

from .branding import (
    WELCOME_STATUS,
    render_mascot_plain_rows,
    render_mascot_rich_text,
)

from .slash import interactive_prompt

try:
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.rule import Rule
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

load_dotenv()

DEFAULT_SECRET_ENV_NAMES = (
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "RIGHT_CODES_API_KEY",
    "SILICONFLOW_API_KEY",
    "GITHUB_PAT",
    "GH_PAT"
)

WELCOME_NAME = "Codini"
WELCOME_SUBTITLE = "local coding agent"


HELP_DETAILS = textwrap.dedent(
    """\
    Commands:
    /help    Show this help message.
    /memory  Show the agent's distilled working memory.
    /session Show the path to the saved session file.
    /reset   Clear the current session history and memory.
    /skill   List all available skills or read a specific skill.
    /exit    Exit the agent.
    """
).strip()

DEFAULT_OPENAI_MODEL = "gpt-5.4"
DEFAULT_OPENAI_BASE_URL = "https://www.right.codes/codex/v1"

DEFAULT_SILICONFLOW_MODEL = "deepseek-ai/DeepSeek-V3.2"
DEFAULT_SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"


LEGACY_SECRET_ENV_NAMES_VAR = "MINI_CODING_AGENT_SECRET_ENV_NAMES"
SECRET_ENV_NAMES_VAR = "Codini_SECRET_ENV_NAMES"

COMMANDS_HELP = {
    "/help": "Show this help message.",
    "/memory": "Show the agent's distilled working memory.",
    "/session": "Show the path to the saved session file.",
    "/reset": "Clear the current session history and memory.",
    "/model": "Switch current model or show model.",
    "/skill": "List all available skills or read a specific skill.",
    "/exit": "Exit the agent."
}

COMMON_MODELS = [
    "deepseek-ai/DeepSeek-R1",
    "deepseek-ai/DeepSeek-V3.2",
    "deepseek-ai/DeepSeek-V4",
    "Qwen/Qwen2.5-Coder-7B-Instruct",
    "Qwen/Qwen2.5-Coder-32B-Instruct",
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-5.4"
]

def _effective_model(args, provider="openai"):
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        return explicit_model
    if provider == "openai":
        model = os.environ.get("OPENAI_MODEL")
        if model:
            return model
        return DEFAULT_OPENAI_MODEL
    if provider == "siliconflow":
        model = os.environ.get("SILICONFLOW_MODEL")
        if model:
            return model
        return DEFAULT_SILICONFLOW_MODEL

def _first_env(*names):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""

def _configured_secret_names(args):
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    extra_names = os.environ.get(SECRET_ENV_NAMES_VAR, "")
    if not extra_names.strip():
        extra_names = os.environ.get(LEGACY_SECRET_ENV_NAMES_VAR, "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper()
            for item in extra_names.split(",")
            if item.strip()
        )
    return sorted(configured_secret_names)

def _build_model_client(args):
    provider = getattr(args, "provider", "openai")
    if provider == "openai":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or os.environ.get("OPENAI_API_BASE") or DEFAULT_OPENAI_BASE_URL
        api_key = os.environ.get("OPENAI_API_KEY", "")
        return OpenAICompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )
    if provider == "siliconflow":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or os.environ.get("SILICONFLOW_API_BASE") or DEFAULT_SILICONFLOW_BASE_URL
        api_key = _first_env("SILICONFLOW_API_KEY", "")
        return SiliconflowModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "siliconflow_timeout", getattr(args, "ollama_timeout", 300)),
        )
    # 待补充 Anthropic Provider 和 Ollama



def build_welcome(agent, model, host):
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        body = middle(text, width-4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    line = divider("=")
    mascot_lines = render_mascot_plain_rows(fill="#", blank=" ")
    rows = [center(text) for text in mascot_lines]
    rows.extend(
        [
            center(WELCOME_NAME),
            center(WELCOME_SUBTITLE),
            center(WELCOME_STATUS),
            divider("-"),
            row(""),
            row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
            pair("MODEL", model, "BRANCH", agent.workspace.branch),
            pair("APPROVAL", agent.approval_policy, "SESSION", agent.session["id"]),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])

def build_welcome_rich(agent, model, host):
    console = Console()

    title_text = Text.assemble(
        (" Codini ", "bold yellow"),
        ("v0.1.0", "dim white"),
        (" │ ", "grey37"),
        ("Magical Local Coding Agent", "bold magenta"),
        (" │ ", "grey37"),
        ("Ready to cast code spells", "italic bright_white")
    )
    
    divider = Rule(style="grey37")
    
    env_table = Table.grid(padding=(0, 1))
    env_table.add_column(style="bold cyan", justify="right", width=12)
    env_table.add_column(style="bright_white")
    env_table.add_row("LLM Model", middle(model, 30))
    env_table.add_row("Provider", middle(host, 30))
    env_table.add_row("Approval", f"[bold green]{agent.approval_policy}[/]" if agent.approval_policy == "auto" else f"[bold yellow]{agent.approval_policy}[/]")
    env_table.add_row("Sandbox", f"[bold red]{agent.sandbox.name}[/]" if agent.sandbox.name != "none" else f"[grey50]none (host)[/]")
    env_table.add_row("Session ID", f"[dim]{agent.session['id']}[/]")
    
    ws_table = Table.grid(padding=(0, 1))
    ws_table.add_column(style="bold blue", justify="right", width=12)
    ws_table.add_column(style="bright_white")
    ws_table.add_row("Repository", middle(agent.workspace.repo_root, 30))
    ws_table.add_row("Cwd", middle(agent.workspace.cwd, 30))
    ws_table.add_row("Branch", f"[bold magenta]{agent.workspace.branch}[/]")

    right_group = Group(
        Text("ENVIRONMENT", style="bold green"),
        env_table,
        Text("WORKSPACE", style="bold green"),
        ws_table
    )
    
    mascot_text = render_mascot_rich_text()

    grid = Table.grid(padding=(0, 2))
    grid.add_column()
    grid.add_column()
    grid.add_row(mascot_text, right_group)

    outer_panel = Panel(
        Group(
            title_text,
            divider,
            grid
        ),
        border_style="grey37",
        padding=(0, 2),
        expand=False
    )
    
    console.print()
    console.print(outer_panel)

def build_agent(args):
    """
    根据 CLI 参数装配出一个可运行的 Codini 实例。
    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `Codini`，或一个从旧 session 恢复出来的 `Codini`

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 runtime 的装配点: 先整理 secret 名单，再采集工作区快照，
    # 随后决定是恢复旧 session 还是创建一个新的 Codini 实例
    configured_secret_names = _configured_secret_names(args)
    workspace = WorkspaceContext.build(args.cwd)
    store = SessionStore(workspace.repo_root + "/.codini/sessions")
    model = _build_model_client(args)
    sandbox = create_sandbox(
        kind=args.sandbox,
        workspace_root=args.cwd,
        allow_network=args.sandbox_network,
    )
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    if session_id:
        return Codini.from_session(
            model_client = model,
            workspace = workspace,
            session_store = store,
            session_id = session_id,
            approval_policy = args.approval,
            max_steps = args.max_steps,
            max_new_tokens = args.max_new_tokens,
            secret_env_names = configured_secret_names,
            sandbox = sandbox
        )
    return Codini(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
        sandbox=sandbox
    )

def _get_skills_list(agent):
    skills_dir = agent.root / ".codini" / "SKILLS"
    if not skills_dir.exists() or not skills_dir.is_dir():
        return []
    skills = []
    try:
        for item in skills_dir.iterdir():
            if item.is_file() and item.name.endswith(".md"):
                skills.append(item.stem)
            elif item.is_dir():
                if (item / "SKILL.md").is_file() or (item / "README.md").is_file():
                    skills.append(f"{item.name} (package)")
    except Exception:
        pass
    return sorted(skills)

def build_arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Minimal coding agent for Ollama, OpenAI-compatible, or Anthropic-compatible models.",
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument("--provider", choices=("ollama", "openai", "anthropic","siliconflow"), default="siliconflow", help="Model backend to use.")
    parser.add_argument("--model", default=None, help="Model name override. Defaults to qwen3.5:4b for Ollama, OPENAI_MODEL for openai, and ANTHROPIC_MODEL for anthropic when set.",)
    parser.add_argument("--host", default="DEFAULT_OLLAMA_HOST", help="Ollama server URL.")
    parser.add_argument("--base-url", default=None, help="Provider API base URL for openai or anthropic.")
    parser.add_argument("--ollama-timeout", type=int, default=300, help="Ollama request timeout in seconds.")
    parser.add_argument("--openai-timeout", type=int, default=300, help="OpenAI-compatible request timeout in seconds.")
    parser.add_argument("--siliconflow-timeout", type=int, default=300, help="SiliconFlow--compatible request timeout in seconds.")
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask", help="Approval policy for risky tools.")
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )
    parser.add_argument("--max-steps", type=int, default=6, help="Maximum tool/model iterations per request.")
    parser.add_argument("--max-new-tokens", type=int, default=512, help="Maximum model output tokens per step.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature sent to Ollama.")
    parser.add_argument("--top-p", type=float, default=0.9, help="Top-p sampling value sent to Ollama.")
    parser.add_argument("--sandbox", choices=("none", "bubblewrap"), default="none", help="Sandbox backend for shell execution (default: none).")
    parser.add_argument("--sandbox-network", action="store_true", default=False, help="Allow network access inside bubblewrap sandbox.")
    return parser

def main(argv = None):
    args = build_arg_parser().parse_args(argv)
    agent = build_agent(args)
    model = getattr(agent.model_client, "model", getattr(args, "model", DEFAULT_OPENAI_MODEL))
    host = getattr(agent.model_client, "host", getattr(agent.model_client, "base_url", getattr(args, "host", "")))
    # print(build_welcome(agent, model, host))
    
    console = Console() if HAS_RICH else None

    if HAS_RICH:
        build_welcome_rich(agent, model, host)
    else:
        build_welcome(agent, model, host)

    if args.prompt:
        # 单次会话模式：只跑一次 ask，不进入 REPL 循环
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                print(agent.ask(prompt))
            except RuntimeError as e:
                print(str(e), file = sys.stderr)
                return 1
        return 0

    # 初始化历史记录
    history = []

    while True:
        # 交互模式
        try:
            if sys.stdin.isatty():
                skills = _get_skills_list(agent)
                user_input = interactive_prompt(
                    prompt_text="\n\033[1;35mCodini\033[0m \033[1;33m>\033[0m ",
                    commands_help=COMMANDS_HELP,
                    common_models=COMMON_MODELS,
                    history=history,
                    skills=skills
                ).strip()
            elif HAS_RICH and console:
                user_input = console.input("\n[bold magenta]Codini[/] [bold yellow]>[/] ").strip()
            else:
                user_input = input("\nCodini -> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        if not user_input:
            continue
        if not history or history[-1] != user_input:
            history.append(user_input)
        if user_input in {"/exit"}:
            return 0
        if user_input == "/help":
            print(HELP_DETAILS)
            continue
        if user_input == "/memory":
            print(agent.memory_text())
            continue
        if user_input == "/session":
            print(agent.session_path)
            continue
        if user_input == "/reset":
            agent.reset()
            print("session reset")
            continue
        if user_input.startswith("/model"):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 2:
                new_model = agent.switch_model(parts[1])
                print(f"switched to {new_model}")
            else:
                current = getattr(agent.model_client, "model", "")
                print(f"current model: {current}")
            continue
        if user_input.startswith("/skill"):
            from .tools import tool_list_skills, tool_read_skill
            parts = user_input.split(maxsplit=1)
            if len(parts) == 2:
                skill_name = parts[1].strip()
                try:
                    result = tool_read_skill(agent, {"name": skill_name})
                    print(result)
                except ValueError as exc:
                    print(str(exc), file=sys.stderr)
            else:
                result = tool_list_skills(agent, {})
                print(result)
            continue
        print()
        try:
            result = agent.ask(user_input)
            for char in result:
                print(char, end="", flush=True)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)