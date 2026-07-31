"""
命令行入口
"""

import argparse
import os
import shutil
import sys
import textwrap
import threading

from dotenv import load_dotenv
from .models import OpenAICompatibleModelClient, SiliconflowModelClient
from .runtime import Codini, SessionStore
from .sandbox import create_sandbox
from .workspace import WorkspaceContext, middle

from .branding import (
    WELCOME_STATUS,
    cli_panel_width,
    render_mascot_plain_rows,
    render_mascot_rich_text,
)

from .trace import make_trace

from .slash import interactive_prompt

try:
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.rule import Rule
    from rich import box
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
    /clear   Create a new empty session.
    /compact Compact older session history.
    /context Show prompt context usage.
    /memory  Show the agent's distilled working memory.
    /reset   Clear the current session history and memory.
    /skill   List all available skills or read a specific skill.
    /session Show the path to the saved session file.
    /trace   Show the live trace viewer URL for the current session.
    /exit    Exit the agent.
    """
).strip()

LEGACY_SECRET_ENV_NAMES_VAR = "MINI_CODING_AGENT_SECRET_ENV_NAMES"
SECRET_ENV_NAMES_VAR = "Codini_SECRET_ENV_NAMES"

COMMANDS_HELP = {
    "/help": "Show this help message.",
    "/context": "Show prompt context usage.",
    "/model": "Switch current model or show model.",
    "/memory": "Show the agent's distilled working memory.",
    "/session": "Show the path to the saved session file.",
    "/reset": "Clear the current session history and memory.",
    "/skill": "List all available skills or read a specific skill.",
    "/exit": "Exit the agent."
}

COMMON_MODELS = [
    "deepseek-ai/DeepSeek-R1",
    "deepseek-ai/DeepSeek-V3.2",
    "deepseek-ai/DeepSeek-V4-Flash",
    "gpt-5.5",
    "gpt-5.4",
    "Qwen/Qwen3.7-Plus",
    "MiniMaxAI/MiniMax-M2.5",
    "moonshotai/Kimi-K2.7-Code",
    "zai-org/GLM-5.2",
]

def _required_config(value, provider, env_name, cli_option=None):
    """功能：读取必需配置并在缺失时给出明确提示；输入：候选值、Provider、环境变量名和可选 CLI 参数；输出：非空配置字符串。"""
    resolved = str(value or "").strip()
    if resolved:
        return resolved
    alternatives = f"，或传入 {cli_option}" if cli_option else ""
    raise ValueError(
        f"Provider '{provider}' 缺少必需配置：请设置环境变量 {env_name}"
        f"{alternatives}。"
    )


def _effective_model(args, provider="openai"):
    """功能：解析 Provider 使用的模型名；输入：CLI 参数和 Provider 名称；输出：命令行或环境变量中的模型名。"""
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        return str(explicit_model).strip()
    env_name = {
        "openai": "OPENAI_MODEL",
        "siliconflow": "SILICONFLOW_MODEL",
    }.get(provider)
    if env_name is None:
        raise ValueError(f"不支持的 Provider：{provider}")
    return _required_config(
        os.environ.get(env_name),
        provider,
        env_name,
        "--model MODEL",
    )

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
    """功能：根据 CLI 与环境变量创建模型客户端；输入：解析后的 CLI 参数；输出：已完成配置的模型客户端。"""
    provider = getattr(args, "provider", "openai")
    if provider == "openai":
        model = _effective_model(args, provider)
        base_url = _required_config(
            getattr(args, "base_url", None) or os.environ.get("OPENAI_BASE_URL"),
            provider,
            "OPENAI_BASE_URL",
            "--base-url URL",
        )
        api_key = _required_config(
            _first_env("OPENAI_API_KEY"),
            provider,
            "OPENAI_API_KEY",
        )
        return OpenAICompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=args.openai_timeout,
        )
    if provider == "siliconflow":
        model = _effective_model(args, provider)
        base_url = _required_config(
            getattr(args, "base_url", None) or os.environ.get("SILICONFLOW_BASE_URL"),
            provider,
            "SILICONFLOW_BASE_URL",
            "--base-url URL",
        )
        api_key = _required_config(
            _first_env("SILICONFLOW_API_KEY"),
            provider,
            "SILICONFLOW_API_KEY",
        )
        return SiliconflowModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=args.siliconflow_timeout,
        )
    raise ValueError(f"不支持的 Provider：{provider}")

def build_welcome(agent, model, host, trace_url=None):
    width = cli_panel_width(shutil.get_terminal_size((80, 20)).columns)
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
            pair("TRACE LIVE", trace_url if trace_url else "inactive", "SANDBOX", agent.sandbox.name),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])

def build_welcome_rich(agent, model, host, trace_url=None, console=None):
    """功能：渲染统一宽度的 Rich Welcome 面板；输入：Agent、模型、Provider、Trace 地址和 Console；输出：无。"""
    console = console or Console()

    title_text = Text.assemble(
        ("   Codini ", "bold yellow"),
        ("v0.1.0", "dim white"),
        (" │ ", "grey37"),
        ("Magical Local Harness Agent", "bold magenta"),
        (" │ ", "grey37"),
        ("Ready to cast code spells", "italic bright_white")
    )

    divider = Rule(style="grey37")

    env_table = Table.grid(padding=(0, 1))
    env_table.add_column(style="bold cyan", justify="right", width=12)
    env_table.add_column(style="bright_white")
    env_table.add_row("LLM Model", middle(model, 30))
    env_table.add_row("Provider", middle(host, 34))
    env_table.add_row("Approval", f"[bold green]{agent.approval_policy}[/]" if agent.approval_policy == "auto" else f"[bold yellow]{agent.approval_policy}[/]")
    env_table.add_row("Sandbox", f"[bold red]{agent.sandbox.name}[/]" if agent.sandbox.name != "none" else "[grey50]none (host)[/]")
    env_table.add_row("Trace Live", f"[bold pink]{trace_url}[/]" if trace_url else "[grey50]inactive (use --trace-live)[/]")
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
        box=box.ROUNDED,
        padding=(0, 2),
        width=cli_panel_width(console.width),
        expand=True,
    )

    console.print()
    console.print(outer_panel)

def build_context_usage(metadata, console=None):
    """功能：展示 Prompt 各区段的上下文占用；输入：上下文元数据和可选 Console；输出：无。"""
    console = console or Console()
    table = Table(
        title="📊 Prompt Context Usage",
        title_style="bold magenta",
        border_style="grey37",
        box=box.ROUNDED,
        width=cli_panel_width(console.width),
        expand=True,
    )
    table.add_column("Section", style="cyan")
    table.add_column("Raw Size (Chars)", justify="right")
    table.add_column("Budget Allocated", justify="right")
    table.add_column("Final Rendered (Chars)", justify="right")
    table.add_column("Usage %", justify="right")

    sections = metadata.get("sections", {})
    section_order = metadata.get("section_order", [])

    for section in section_order:
        sec_data = sections.get(section, {})
        raw = sec_data.get("raw_chars", 0)
        budget = sec_data.get("budget_chars")
        rendered = sec_data.get("rendered_chars", 0)

        budget_str = str(budget) if budget is not None else "-"

        if budget is not None and budget > 0:
            pct = (rendered / budget) * 100
            pct_str = f"{pct:.1f}%"
            if pct > 100:
                pct_str = f"[bold red]{pct_str}[/]"
            elif pct > 80:
                pct_str = f"[bold yellow]{pct_str}[/]"
            else:
                pct_str = f"[bold green]{pct_str}[/]"
        else:
            pct_str = "-"

        # Highlight if truncated
        rendered_display = str(rendered)
        if raw > rendered:
            rendered_display = f"[bold yellow]{rendered} (truncated)[/]"

        table.add_row(section, str(raw), budget_str, rendered_display, pct_str)

    console.print(table)

    total_used = metadata.get("prompt_chars", 0)
    total_budget = metadata.get("prompt_budget_chars", 0)
    total_pct = (total_used / total_budget) * 100 if total_budget else 0.0

    total_pct_str = f"{total_pct:.1f}%"
    if total_used > total_budget:
        status_str = f"[bold red]OVER BUDGET {total_pct_str}[/]"
    else:
        status_str = f"[bold green]OK {total_pct_str}[/]"

    console.print(f"Total Prompt Size: [bold]{total_used}[/] / {total_budget} chars ({status_str})")

    reductions = metadata.get("budget_reductions", [])
    if reductions:
        console.print("\n[bold yellow]Budget Reductions Applied:[/]")
        for red in reductions:
            console.print(f"  • [cyan]{red['section']}[/]: {red['before_chars']} -> {red['after_chars']} (overflow: {red['overflow_chars']} chars)")
    print()

def build_agent(args, trace=None):
    """
    根据 CLI 参数装配出一个可运行的 Codini 实例。
    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`，以及可选的 trace 可视化后端
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
            sandbox = sandbox,
            trace = trace,
        )
    return Codini(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
        sandbox=sandbox,
        trace=trace,
    )

def _get_skills_list(agent):
    skills_dir = agent.root / ".codini" / "skills"
    if not skills_dir.exists() or not skills_dir.is_dir():
        return []
    skills = []
    try:
        for item in skills_dir.iterdir():
            if item.is_file() and item.name.endswith(".md"):
                skills.append(item.stem)
            elif item.is_dir():
                if (item / "SKILL.md").is_file() or (item / "README.md").is_file():
                    skills.append(f"{item.name}")
    except Exception:
        pass
    return sorted(skills)

def build_arg_parser():
    """功能：构建 Codini 命令行参数解析器；输入：无；输出：配置完成的 ArgumentParser。"""
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Minimal coding agent for OpenAI-compatible or SiliconFlow models.",
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument("--provider", choices=("openai", "siliconflow"), default="openai", help="Model backend to use.")
    parser.add_argument("--model", default=None, help="Model name override. Defaults to OPENAI_MODEL or SILICONFLOW_MODEL.")
    parser.add_argument("--base-url", default=None, help="Provider API base URL override.")
    parser.add_argument("--openai-timeout", type=int, default=300, help="OpenAI-compatible request timeout in seconds.")
    parser.add_argument("--siliconflow-timeout", type=int, default=300, help="SiliconFlow request timeout in seconds.")
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask", help="Approval policy for risky tools.")
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=6,
        help="Initial tool-step budget per request; successful progress can extend it to an internal hard limit.",
    )
    parser.add_argument("--max-new-tokens", type=int, default=2048, help="Maximum model output tokens per step.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature sent to the model provider.")
    parser.add_argument("--sandbox", choices=("none", "bubblewrap"), default="none", help="Sandbox backend for shell execution (default: none).")
    parser.add_argument("--sandbox-network", action="store_true", default=False, help="Allow network access inside bubblewrap sandbox.")
    parser.add_argument("--no-trace-live", action="store_false", dest="trace_live", default=True, help="Disable starting a live trace viewer for this session.")
    parser.add_argument("--trace-host", default="127.0.0.1", help="Host for --trace-live.")
    parser.add_argument("--trace-port", type=int, default=8765, help="Port for --trace-live.")
    parser.add_argument("--trace-poll-ms", type=int, default=1500, help="Browser polling interval for --trace-live.")
    return parser

def _agent_error_already_rendered(agent):
    trace = getattr(agent, "trace", None)
    state = getattr(agent, "current_task_state", None)
    run_id = getattr(state, "run_id", "") if state else ""
    return bool(trace and run_id and getattr(trace, "_last_error_trace_id", "") == run_id)

def main(argv = None):
    """功能：解析参数并启动一次性任务或交互式 REPL；输入：可选命令行参数列表；输出：进程退出码。"""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    console = Console() if HAS_RICH else None
    trace = make_trace(console=console)
    try:
        agent = build_agent(args, trace=trace)
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    trace_server = None
    trace_url = None
    if args.trace_live:
        from .trace.viewer import make_viewer_server
        trace_server, trace_url = make_viewer_server(
            agent.session.get("id", "latest"),
            agent.root,
            args.trace_host,
            args.trace_port,
            args.trace_poll_ms,
        )
        threading.Thread(target=trace_server.serve_forever, daemon=True).start()

    model = getattr(agent.model_client, "model", getattr(args, "model", ""))
    host = getattr(agent.model_client, "host", getattr(agent.model_client, "base_url", getattr(args, "host", "")))
    # print(build_welcome(agent, model, host))

    if HAS_RICH:
        build_welcome_rich(agent, model, host, trace_url, console=console)
    else:
        build_welcome(agent, model, host, trace_url)

    if args.prompt:
        # 单次会话模式：只跑一次 ask，不进入 REPL 循环
        prompt = " ".join(args.prompt).strip()
        if prompt:
            try:
                agent.ask(prompt)
            except RuntimeError as e:
                if _agent_error_already_rendered(agent):
                    return 1
                if trace:
                    trace.on_run_error(str(e))
                else:
                    print(str(e), file = sys.stderr)
                return 1
        return 0

    # 初始化历史记录，如果从已有会话恢复则导入之前的用户输入历史
    history = []
    if agent.session and "history" in agent.session:
        for item in agent.session["history"]:
            if isinstance(item, dict) and item.get("role") == "user":
                content = item.get("content")
                if content and (not history or history[-1] != content):
                    history.append(content)

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
            if trace_server is not None:
                trace_server.shutdown()
            return 0

        if not user_input:
            continue
        if not history or history[-1] != user_input:
            history.append(user_input)
        if user_input in {"/exit"}:
            if trace_server is not None:
                trace_server.shutdown()
            return 0
        if user_input == "/help":
            print(HELP_DETAILS)
            continue
        if user_input == "/context":
            try:
                _, metadata = agent.context_manager.build("")
                build_context_usage(metadata, console=console)
            except Exception as e:
                print(f"Error calculating context: {e}", file=sys.stderr)
            continue
        if user_input == "/memory":
            print(agent.memory_text())
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
        if user_input == "/reset":
            agent.reset()
            print("session reset")
            continue
        if user_input == "/session":
            print(agent.session_path)
            continue
        if user_input == "/trace":
            if trace_url:
                print(trace_url)
            else:
                print("trace live viewer is inactive; restart without --no-trace-live")
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
        try:
            agent.ask(user_input)
        except KeyboardInterrupt:
            print("\n[interrupted]")
            continue
        except RuntimeError as exc:
            if _agent_error_already_rendered(agent):
                continue
            if trace:
                trace.on_run_error(str(exc))
            else:
                print(str(exc), file=sys.stderr)
