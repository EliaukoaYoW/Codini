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
            # 管道模式下，逐字节读取并按 UTF-8 多字节规则重组字符（中文占 3 字节）
            buf = ctypes.create_string_buffer(1)
            read = wintypes.DWORD()
            res = kernel32.ReadFile(h_stdin, buf, 1, ctypes.byref(read), None)
            if not (res and read.value > 0):
                return ''
            first_byte = buf.raw[0]
            # 根据 UTF-8 首字节确定总字节数
            if first_byte < 0x80:
                total = 1
            elif first_byte < 0xE0:
                total = 2
            elif first_byte < 0xF0:
                total = 3
            else:
                total = 4
            raw = bytes([first_byte])
            for _ in range(total - 1):
                res2 = kernel32.ReadFile(h_stdin, buf, 1, ctypes.byref(read), None)
                if res2 and read.value > 0:
                    raw += buf.raw[0:1]
            return raw.decode('utf-8', errors='replace')
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
        first_byte = b[0]
        # 根据 UTF-8 首字节确定总字节数（中文占 3 字节，emoji 占 4 字节）
        if first_byte < 0x80:
            total = 1
        elif first_byte < 0xE0:
            total = 2
        elif first_byte < 0xF0:
            total = 3
        else:
            total = 4
        raw = b
        for _ in range(total - 1):
            extra = os.read(fd, 1)
            if extra:
                raw += extra
        ch = raw.decode('utf-8', errors='replace')
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
    # 如果包含空格（表明已经输入了命令参数，例如已选定 "/model " 或 "/skill "），不再显示下拉的 Box UI
    if ' ' in current_text:
        return []
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


class RawModeUnix:
    def __enter__(self):
        import tty
        import termios
        self.fd = sys.stdin.fileno()
        self.old_settings = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, type, value, traceback):
        import termios
        if self.old_settings:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_settings)


class RawModeWin:
    def __enter__(self):
        return self
    def __exit__(self, type, value, traceback):
        pass


def read_key_raw_unix(fd):
    import select
    b = os.read(fd, 1)
    if not b:
        return None
    first_byte = b[0]
    if first_byte < 0x80:
        total = 1
    elif first_byte < 0xE0:
        total = 2
    elif first_byte < 0xF0:
        total = 3
    else:
        total = 4
    raw = b
    for _ in range(total - 1):
        extra = os.read(fd, 1)
        if extra:
            raw += extra
    ch = raw.decode('utf-8', errors='replace')
    if ch == '\x1b':
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


def interactive_prompt(prompt_text, commands_help, common_models, history=None, skills=None):
    if history is None:
        history = []

    if prompt_text.startswith("\n"):
        sys.stdout.write("\n")
        sys.stdout.flush()
        prompt_text = prompt_text[1:]

    if sys.platform == "win32":
        os.system("")

    import unicodedata

    def _char_width(ch):
        """计算字符的终端显示宽度（中文/全角 = 2，其他 = 1）"""
        if unicodedata.east_asian_width(ch) in ('F', 'W'):
            return 2
        return 1

    def _str_width(s):
        """计算字符串的终端显示宽度"""
        return sum(_char_width(c) for c in s)

    chars = []
    cursor_pos = 0
    draft_text = ""
    selected_idx = -1
    prev_lines_count = 0
    history_idx = len(history)
    slash_mode = False

    def draw_current_state():
        nonlocal prev_lines_count
        current_text = "".join(chars)
        if slash_mode:
            matches = get_matches(current_text, commands_help, common_models, skills)
            prev_lines_count = draw_interface(prompt_text, current_text, matches, selected_idx, prev_lines_count)
            move_left = _str_width(current_text[cursor_pos:])
            if move_left > 0:
                write_stdout(f"\033[{move_left}D")
                sys.stdout.flush()
        else:
            # 清理下拉菜单并重绘正常行
            if prev_lines_count > 0:
                write_stdout("\r\033[J")
                prev_lines_count = 0
            
            write_stdout(f"\r\033[K{prompt_text}{current_text}")
            move_left = _str_width(current_text[cursor_pos:])
            if move_left > 0:
                write_stdout(f"\033[{move_left}D")
            sys.stdout.flush()

    # 画初始空行
    draw_current_state()

    # 确定原始 raw 模式上下文管理器
    if sys.platform == "win32":
        raw_mode_ctx = RawModeWin()
        fd = None
    else:
        raw_mode_ctx = RawModeUnix()
        fd = sys.stdin.fileno()

    def has_input():
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                kernel32 = ctypes.windll.kernel32
                h_stdin = kernel32.GetStdHandle(-10)
                avail = wintypes.DWORD()
                res = kernel32.PeekNamedPipe(h_stdin, None, 0, None, ctypes.byref(avail), None)
                return res and avail.value > 0
            except Exception:
                return False
        else:
            import select
            try:
                r, _, _ = select.select([fd], [], [], 0)
                return bool(r)
            except Exception:
                return False

    pending_keys = []

    try:
        with raw_mode_ctx:
            while True:
                if pending_keys:
                    key = pending_keys.pop(0)
                else:
                    if sys.platform == "win32":
                        key = get_key_win()
                    else:
                        key = read_key_raw_unix(fd)

                if key is None:
                    continue

                # 连续输入与粘贴优化
                if key not in ('up', 'down', 'left', 'right', 'enter', 'tab', 'backspace', 'escape'):
                    while has_input():
                        if sys.platform == "win32":
                            next_key = get_key_win()
                        else:
                            next_key = read_key_raw_unix(fd)
                        if next_key is None:
                            continue
                        if next_key in ('up', 'down', 'left', 'right', 'enter', 'tab', 'backspace', 'escape'):
                            pending_keys.append(next_key)
                            break
                        key += next_key

                if key == 'enter':
                    break

                elif key == 'backspace':
                    if cursor_pos > 0:
                        chars.pop(cursor_pos - 1)
                        cursor_pos -= 1
                        
                        current_text = "".join(chars)
                        was_slash = slash_mode
                        slash_mode = current_text.startswith('/')
                        
                        if was_slash and not slash_mode:
                            selected_idx = -1
                            history_idx = len(history)
                            draft_text = ""
                        
                        draw_current_state()
                    continue

                elif key == 'left':
                    if cursor_pos > 0:
                        cursor_pos -= 1
                        draw_current_state()
                    continue

                elif key == 'right':
                    if cursor_pos < len(chars):
                        cursor_pos += 1
                        draw_current_state()
                    continue

                elif key == 'up':
                    current_text = "".join(chars)
                    matches = get_matches(current_text, commands_help, common_models, skills)
                    if slash_mode and matches:
                        if selected_idx == -1:
                            selected_idx = len(matches) - 1
                        else:
                            selected_idx = (selected_idx - 1) % len(matches)
                    else:
                        if history and history_idx > 0:
                            if history_idx == len(history):
                                draft_text = current_text
                            history_idx -= 1
                            chars = list(history[history_idx])
                            cursor_pos = len(chars)
                            slash_mode = history[history_idx].startswith('/')
                            selected_idx = -1
                    draw_current_state()
                    continue

                elif key == 'down':
                    current_text = "".join(chars)
                    matches = get_matches(current_text, commands_help, common_models, skills)
                    if slash_mode and matches:
                        if selected_idx == -1:
                            selected_idx = 0
                        else:
                            selected_idx = (selected_idx + 1) % len(matches)
                    else:
                        if history and history_idx < len(history) - 1:
                            history_idx += 1
                            chars = list(history[history_idx])
                            cursor_pos = len(chars)
                            slash_mode = history[history_idx].startswith('/')
                            selected_idx = -1
                        elif history and history_idx == len(history) - 1:
                            history_idx = len(history)
                            chars = list(draft_text)
                            cursor_pos = len(chars)
                            slash_mode = draft_text.startswith('/')
                            selected_idx = -1
                    draw_current_state()
                    continue

                elif key == 'tab':
                    if slash_mode:
                        current_text = "".join(chars)
                        matches = get_matches(current_text, commands_help, common_models, skills)
                        if matches:
                            idx = selected_idx if selected_idx != -1 else 0
                            completed = matches[idx][0]
                            if completed in {"/model", "/skill"}:
                                chars = list(completed + " ")
                            else:
                                chars = list(completed)
                            cursor_pos = len(chars)
                            selected_idx = -1
                            draw_current_state()
                    continue

                elif key == 'escape':
                    draw_current_state()
                    continue

                elif len(key) >= 1:
                    # 在光标处插入字符
                    for c in key:
                        chars.insert(cursor_pos, c)
                        cursor_pos += 1
                    
                    current_text = "".join(chars)
                    was_slash = slash_mode
                    slash_mode = current_text.startswith('/')
                    
                    if slash_mode and not was_slash:
                        selected_idx = -1
                        history_idx = len(history)
                        draft_text = current_text
                    
                    draw_current_state()
                    continue

    except (KeyboardInterrupt, EOFError) as e:
        current_text = "".join(chars)
        write_stdout("\r\033[J")
        write_stdout(f"{prompt_text}{current_text}\n")
        sys.stdout.flush()
        raise e

    current_text = "".join(chars)
    write_stdout("\r\033[J")
    write_stdout(f"{prompt_text}{current_text}\n")
    sys.stdout.flush()

    return current_text