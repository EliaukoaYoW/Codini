"""
交互式命令行提示符工具，支持自适应单线边框下拉列表与多级联想补全
"""
import os
import sys
import shutil
import time

def get_key_win():
    import ctypes
    from ctypes import wintypes
    import msvcrt
    import time

    kernel32 = ctypes.windll.kernel32
    h_stdin = kernel32.GetStdHandle(-10)

    # 检查是否为管道输入（Git Bash / mintty 等模拟终端使用管道作为 stdin）
    avail = wintypes.DWORD()
    is_pipe = bool(kernel32.PeekNamedPipe(h_stdin, None, 0, None, ctypes.byref(avail), None))

    def read_char():
        if is_pipe:
            # 管道模式下，使用 ReadFile 读取单个字节以规避 line buffer 阻碍
            buf = ctypes.create_string_buffer(1)
            read = wintypes.DWORD()
            res = kernel32.ReadFile(h_stdin, buf, 1, ctypes.byref(read), None)
            if res and read.value > 0:
                return buf.raw[0:1].decode('utf-8', errors='ignore')
            return ''
        else:
            # 标准 CMD/PowerShell 模式下使用 getwch
            return msvcrt.getwch()

    def chars_available():
        if is_pipe:
            # 检查管道内是否有数据
            avail_bytes = wintypes.DWORD()
            res = kernel32.PeekNamedPipe(h_stdin, None, 0, None, ctypes.byref(avail_bytes), None)
            return res and avail_bytes.value > 0
        else:
            # 检查标准控制台的键盘缓冲区
            return msvcrt.kbhit()

    ch = read_char()
    if not ch:
        return None

    if not is_pipe and ch in ('\x00', '\xe0'):
        # 仅在非管道模式下解析 getwch 方向键前导码
        ch2 = read_char()
        if ch2 == 'H': return 'up'
        if ch2 == 'P': return 'down'
        if ch2 == 'K': return 'left'
        if ch2 == 'M': return 'right'
        return None

    if ch == '\x1b':
        # 检测后续 ANSI 序列 (在 Git Bash 等环境下，方向键表现为 \x1b[A / \x1b[B)
        # 稍微等待 10ms 以防数据包分包到达
        if not chars_available():
            time.sleep(0.01)
        if chars_available():
            ch2 = read_char()
            if ch2 == '[':
                if not chars_available():
                    time.sleep(0.01)
                if chars_available():
                    ch3 = read_char()
                    if ch3 == 'A': return 'up'
                    if ch3 == 'B': return 'down'
                    if ch3 == 'C': return 'right'
                    if ch3 == 'D': return 'left'
        return 'escape'

    if ch in ('\r', '\n'): return 'enter'
    if ch == '\t': return 'tab'
    if ch in ('\b', '\x7f'): return 'backspace'
    if ch == '\x03': raise KeyboardInterrupt
    if ch in ('\x04', '\x1a'): raise EOFError
    return ch


def get_key_unix():
    import tty
    import termios
    import select
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        # 使用 os.read 进行完全系统级的无缓冲读取，防止 Python text wrapper 预读数据包
        b = os.read(fd, 1)
        if not b:
            return None
        ch = b.decode('utf-8', errors='ignore')
        if ch == '\x1b':
            # 检测缓冲区中是否存在后续转义序列字节
            r, _, _ = select.select([fd], [], [], 0.05)
            if r:
                b2 = os.read(fd, 1)
                ch2 = b2.decode('utf-8', errors='ignore')
                if ch2 == '[':
                    r, _, _ = select.select([fd], [], [], 0.05)
                    if r:
                        b3 = os.read(fd, 1)
                        ch3 = b3.decode('utf-8', errors='ignore')
                        if ch3 == 'A': return 'up'
                        if ch3 == 'B': return 'down'
                        if ch3 == 'C': return 'right'
                        if ch3 == 'D': return 'left'
            return 'escape'
        if ch in ('\r', '\n'): return 'enter'
        if ch == '\t': return 'tab'
        if ch in ('\x7f', '\x08'): return 'backspace'
        if ch == '\x03': raise KeyboardInterrupt
        if ch == '\x04': raise EOFError
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def get_matches(current_text, commands_help, common_models, skills=None):
    if not current_text.startswith('/'):
        return []
    if current_text.startswith('/model '):
        prefix = current_text[len('/model '):]
        return [(f"/model {m}", f"Switch to {m}") for m in common_models if m.startswith(prefix)]
    elif current_text == '/model':
        return [('/model', commands_help['/model'])]
    elif current_text.startswith('/skill '):
        if not skills:
            return []
        prefix = current_text[len('/skill '):]
        prefix_lower = prefix.lower()
        return [(f"/skill {s}", f"Read skill {s}") for s in skills if prefix_lower in s.lower()]
    elif current_text == '/skill':
        return [('/skill', commands_help['/skill'])]
    return [(cmd, desc) for cmd, desc in commands_help.items() if cmd.startswith(current_text)]


def write_stdout(text):
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        # 如果终端不支持 Unicode 边框字符，降级为 ASCII 字符，防止崩溃
        ascii_text = text.replace("┌", "+").replace("┐", "+").replace("└", "+").replace("┘", "+").replace("─", "-").replace("│", "|")
        sys.stdout.write(ascii_text)


def draw_interface(prompt, current_text, matches, selected_idx, prev_lines_count):
    # 1. 移回行首并清除该行以及下方所有残留的菜单（使用 \r\033[J 极其高效且不滚动屏幕）
    write_stdout(f"\r\033[J{prompt}{current_text}")
    sys.stdout.flush()

    # 2. 如果有匹配的候选，渲染单线边框下拉框
    lines_printed = 0
    if matches:
        term_width = shutil.get_terminal_size((80, 20)).columns
        max_content_width = term_width - 6

        formatted_lines = []
        max_len = 0
        for idx, (cmd, desc) in enumerate(matches):
            prefix = "> " if idx == selected_idx else "  "
            line_content = f"{prefix}{cmd:<10}  {desc}"
            if len(line_content) > max_content_width:
                line_content = line_content[:max_content_width-3] + "..."
            formatted_lines.append(line_content)
            max_len = max(max_len, len(line_content))

        box_width = max_len + 2

        # 顶边框
        write_stdout(f"\n\033[K┌" + "─" * box_width + "┐")
        lines_printed += 1
        for line_content in formatted_lines:
            write_stdout(f"\n\033[K│ " + line_content.ljust(box_width - 2) + " │")
            lines_printed += 1
        # 底边框
        write_stdout(f"\n\033[K└" + "─" * box_width + "┘")
        lines_printed += 1

        # 将光标移动回输入行末端
        write_stdout(f"\033[{lines_printed}A\r{prompt}{current_text}")
        sys.stdout.flush()

    return lines_printed


def interactive_prompt(prompt_text, commands_help, common_models, history=None, skills=None):
    if history is None:
        history = []

    # 提取并分离提示符开头的换行符，防止每次重绘时屏幕都下移一行
    if prompt_text.startswith("\n"):
        sys.stdout.write("\n")
        sys.stdout.flush()
        prompt_text = prompt_text[1:]

    if sys.platform == "win32":
        os.system("")

    current_text = ""
    draft_text = ""       # 保存用户进入历史浏览前正在输入的草稿
    selected_idx = -1
    prev_lines_count = 0
    history_idx = len(history)

    matches = []
    prev_lines_count = draw_interface(prompt_text, current_text, matches, selected_idx, prev_lines_count)

    while True:
        try:
            if sys.platform == "win32":
                key = get_key_win()
            else:
                key = get_key_unix()
        except (KeyboardInterrupt, EOFError) as e:
            # 清理下拉菜单并定位
            write_stdout("\r\033[J")
            write_stdout(f"{prompt_text}{current_text}\n")
            sys.stdout.flush()
            raise e

        if key is None:
            continue

        if key == 'up':
            matches = get_matches(current_text, commands_help, common_models, skills)
            if matches:
                if selected_idx == -1:
                    selected_idx = len(matches) - 1
                else:
                    selected_idx = (selected_idx - 1) % len(matches)
            else:
                if history and history_idx > 0:
                    # 首次离开末尾位置时，保存当前输入为草稿
                    if history_idx == len(history):
                        draft_text = current_text
                    history_idx -= 1
                    current_text = history[history_idx]
                    selected_idx = -1
        elif key == 'down':
            matches = get_matches(current_text, commands_help, common_models, skills)
            if matches:
                if selected_idx == -1:
                    selected_idx = 0
                else:
                    selected_idx = (selected_idx + 1) % len(matches)
            else:
                if history and history_idx < len(history) - 1:
                    history_idx += 1
                    current_text = history[history_idx]
                    selected_idx = -1
                elif history and history_idx == len(history) - 1:
                    # 回到末尾，恢复用户之前的草稿输入
                    history_idx = len(history)
                    current_text = draft_text
                    selected_idx = -1
        elif key == 'backspace':
            if current_text:
                current_text = current_text[:-1]
            selected_idx = -1
            history_idx = len(history)
            draft_text = current_text  # 实时同步草稿
        elif key == 'tab':
            matches = get_matches(current_text, commands_help, common_models, skills)
            if matches:
                idx = selected_idx if selected_idx != -1 else 0
                completed = matches[idx][0]
                if completed in {"/model", "/skill"}:
                    current_text = completed + " "
                else:
                    current_text = completed
                selected_idx = -1
        elif key == 'enter':
            matches = get_matches(current_text, commands_help, common_models, skills)
            if matches and selected_idx != -1:
                completed = matches[selected_idx][0]
                if completed in {"/help", "/memory", "/session", "/reset", "/exit", "/quit"}:
                    current_text = completed
                    break
                else:
                    if completed in {"/model", "/skill"}:
                        current_text = completed + " "
                    else:
                        current_text = completed
                    selected_idx = -1
            else:
                break
        elif len(key) == 1:
            current_text += key
            selected_idx = -1
            history_idx = len(history)
            draft_text = current_text  # 实时同步草稿

        matches = get_matches(current_text, commands_help, common_models, skills)
        prev_lines_count = draw_interface(prompt_text, current_text, matches, selected_idx, prev_lines_count)

    # 退出前清理下拉菜单，只保留输入行
    write_stdout("\r\033[J")
    write_stdout(f"{prompt_text}{current_text}\n")
    sys.stdout.flush()

    return current_text