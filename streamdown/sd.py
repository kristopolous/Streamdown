#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.8"
# dependencies = [
#     "pygments",
#     "appdirs",
#     "toml"
# ]
# ///
import appdirs
import base64
import logging
import math
import os
import pty
import re
import select
import shutil
import sys
import subprocess
import tempfile
import toml
import traceback
import colorsys
from io import StringIO
import pygments.util
from argparse import ArgumentParser
from pygments import highlight
from pygments.lexers import get_lexer_by_name
from pygments.formatters import Terminal256Formatter
from pygments.styles import get_style_by_name

default_toml = """
[features]
CodeSpaces = true
Clipboard = true
Logging = false
Margin = 2 
PrettyPad = false
Timeout = 0.5
Width = 0

[colors]
HSV =    [0.8, 0.5, 0.5]
DARK =   { H = 1.00, S = 1.50, V = 0.25 }
MID  =   { H = 1.00, S = 1.00, V = 0.50 }
SYMBOL = { H = 1.00, S = 1.00, V = 1.50 }
HEAD =   { H = 1.00, S = 2.00, V = 1.50 }
GREY =   { H = 1.00, S = 0.12, V = 1.25 }
BRIGHT = { H = 1.00, S = 2.00, V = 2.00 }
STYLE =  "monokai"
"""

def ensure_config_file():
    config_dir = appdirs.user_config_dir("streamdown")
    os.makedirs(config_dir, exist_ok=True)
    config_path = os.path.join(config_dir, "config.toml")
    if not os.path.exists(config_path):
        open(config_path, 'w').write(default_toml)
    return config_path, open(config_path).read()

config_toml_path, config_toml_content = ensure_config_file()
config = toml.loads(config_toml_content)
config_default = toml.loads(default_toml)
colors = config.get("colors", {})
features = config.get("features", {})
H, S, V = colors.get("HSV")

try:
    env_sd_colors = os.getenv("SD_BASEHSV")
    if env_sd_colors:
        env_colors = env_sd_colors.split(",")
        if len(env_colors) > 0: H = float(env_colors[0])
        if len(env_colors) > 1: S = float(env_colors[1])
        if len(env_colors) > 2: V = float(env_colors[2])
except Exception as e:
    logging.warning(f"Error parsing SD_BASEHSV: {e}")

def apply_multipliers(name, H, S, V):
    m = colors.get(name, config_default.get('colors').get(name))
    r, g, b = colorsys.hsv_to_rgb(min(1.0, H * m["H"]), min(1.0, S * m["S"]), min(1.0, V * m["V"]))
    return ';'.join([str(int(x * 256)) for x in [r, g, b]]) + "m"

DARK   = apply_multipliers("DARK", H, S, V)
MID    = apply_multipliers("MID", H, S, V)
SYMBOL = apply_multipliers("SYMBOL", H, S, V)
HEAD   = apply_multipliers("HEAD", H, S, V)
GREY   = apply_multipliers("GREY", H, S, V)
BRIGHT = apply_multipliers("BRIGHT", H, S, V)

STYLE  = colors.get("STYLE", config_default.get('colors').get("STYLE"))
MARGIN = features.get("Margin", config_default.get('features').get("Margin"))
MARGIN_SPACES = " " * MARGIN
LIST_INDENT = 2

FG = "\033[38;2;"
BG = "\033[48;2;"
RESET = "\033[0m"
FGRESET = "\033[39m"
BGRESET = "\033[49m"
BQUOTE = f"{FG}{GREY} \u258E "

BOLD      = ["\033[1m", "\033[22m"]
UNDERLINE = ["\033[4m", "\033[24m"]
ITALIC    = ["\033[3m", "\033[23m"]

CODEBG = f"{BG}{DARK}"
LINK = f"{FG}{SYMBOL}{UNDERLINE[0]}"

ANSIESCAPE = r"\033(\[[0-9;]*[mK]|][0-9]*;;.*?\\|\\)"
KEYCODE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

visible = lambda x: re.sub(ANSIESCAPE, "", x)
visible_length = lambda x: len(visible(x))
extract_ansi_codes = lambda text: re.findall(r"\033\[[0-9;]*[mK]", text)

def debug_write(text):
    if state.Logging:
        if state.Logging == True:
            tmp_dir = os.path.join(tempfile.gettempdir(), "sd")
            os.makedirs(tmp_dir, exist_ok=True)
            state.Logging = tempfile.NamedTemporaryFile(dir=tmp_dir, prefix="dbg", delete=False, encoding="utf-8", mode="w")
        print(f"{text}", file=state.Logging, end="")
        state.Logging.flush()

class Goto(Exception):
    pass

class Code:
    Spaces = 'spaces'
    Backtick = 'backtick'
    Header = 'header'
    Body = 'body'
    Flush = 'flush'

class ParseState:
    def __init__(self):
        self.buffer = ''
        self.current_line = ''
        self.first_line = True
        self.last_line_empty = False
        self.is_pty = False
        self.maybe_prompt = False
        self.emit_flag = None

        self.CodeSpaces = features.get("CodeSpaces", True)
        self.Clipboard = features.get("Clipboard", True)
        self.Logging = features.get("Logging", False)
        self.PrettyPad = features.get("PrettyPad", False)
        self.Timeout = features.get("Timeout", 0.5)

        # If the entire block is indented this will
        # tell us what that is
        self.first_indent = None
        self.has_newline = False
        self.bg = BGRESET    

        # These are part of a trick to get
        # streaming code blocks while preserving
        # multiline parsing.
        self.code_buffer = ""
        self.code_gen = 0
        self.code_language = None
        self.code_first_line = False
        self.code_indent = 0
        self.code_line = ''

        self.ordered_list_numbers = []
        self.list_item_stack = []  # stack of (indent, type)

        # So this can either be False, Code.Backtick or Code.Spaces
        self.in_list = False
        self.in_code = False
        self.inline_code = False
        self.in_bold = False
        self.in_italic = False
        self.in_table = False # (Code.[Header|Body] | False)
        self.in_underline = False
        self.in_blockquote = False

        self.exit = 0
        self.where_from = None

    def current(self):
        state = { 'code': self.in_code, 'bold': self.in_bold, 'italic': self.in_italic, 'underline': self.in_underline }
        state['none'] = all(item is False for item in state.values())
        return state

    def space_left(self):
        return (MARGIN_SPACES if len(self.current_line) == 0 else "") + (BQUOTE if self.in_blockquote else "")

state = ParseState()

def format_table(rowList):
    num_cols = len(rowList)
    row_height = 0
    wrapped_cellList = []

    # Calculate max width per column (integer division)
    # Subtract num_cols + 1 for the vertical borders '│'
    available_width = state.Width - (num_cols + 1)
    col_width = max(1, available_width // num_cols)
    state.bg = f"{BG}{DARK}"

    # --- First Pass: Wrap text and calculate row heights ---
    for row in rowList:
        wrapped_cell = wrap_text(row, width=col_width)

        # Ensure at least one line, even for empty cells
        if not wrapped_cell:
            wrapped_cell = [""]

        wrapped_cellList.append(wrapped_cell)
        row_height = max(row_height, len(wrapped_cell))

    # --- Second Pass: Format and emit rows ---
    bg_color = MID if state.in_table == Code.Header else DARK
    for ix in range(row_height):
        # This is the fancy row separator
        extra = f"\033[4;58;2;{MID}" if not state.in_table == Code.Header and (ix == row_height - 1) else ""
        line_segments = []

        # Now we want to snatch this row index from all our cells
        for cell in wrapped_cellList:
            segment = ''
            if ix < len(cell):
                segment = cell[ix]

            # Margin logic is correctly indented here
            margin_needed = col_width - visible_length(segment)
            margin_segment = segment + (" " * max(0, margin_needed))
            line_segments.append(f"{BG}{bg_color}{extra} {margin_segment}")

        # Correct indentation: This should be outside the c_idx loop
        joined_line = f"{BG}{bg_color}{extra}{FG}{SYMBOL}│{RESET}".join(line_segments)
        # Correct indentation and add missing characters
        yield f"{MARGIN_SPACES}{joined_line}{RESET}"

    state.bg = BGRESET

def emit_h(level, text):
    text = line_format(text)
    spaces_to_center = ((state.Width - visible_length(text)) / 2)
    if level == 1:      #
        return f"\n{MARGIN_SPACES}{BOLD[0]}{' ' * math.floor(spaces_to_center)}{text}{' ' * math.ceil(spaces_to_center)}{BOLD[1]}\n"
    elif level == 2:    ##
        return f"\n{MARGIN_SPACES}{BOLD[0]}{FG}{BRIGHT}{' ' * math.floor(spaces_to_center)}{text}{' ' * math.ceil(spaces_to_center)}{RESET}\n\n"
    elif level == 3:    ###
        return f"{MARGIN_SPACES}{FG}{HEAD}{BOLD[0]}{text}{RESET}"
    elif level == 4:    ####
        return f"{MARGIN_SPACES}{FG}{SYMBOL}{text}{RESET}"
    else:  # level 5 or 6
        return f"{MARGIN_SPACES}{text}{RESET}"

def code_wrap(text_in):
    # get the indentation of the first line
    indent = len(text_in) - len(text_in.lstrip())
    text = text_in.lstrip()
    mywidth = state.FullWidth - indent

    # We take special care to preserve empty lines
    if len(text) == 0:
        return (0, [text_in])
    res = [text[:mywidth]]

    for i in range(mywidth, len(text), mywidth):
        res.append(text[i : i + mywidth])

    return (indent, res)

def wrap_text(text, width = -1, indent = 0, first_line_prefix="", subsequent_line_prefix="", buffer=True):
    if width == -1:
        width = state.Width

    words = line_format(text).split()
    lines = []
    current_line = ""
    current_style = ""
    
    for word in words:
        current_style += "".join(extract_ansi_codes(word) or [])

        if visible_length(current_line) + visible_length(word) + 1 <= width:  # +1 for space
            current_line += (" " if current_line else "") + word
        else:
            # Word doesn't fit, finalize the previous line
            prefix = first_line_prefix if not lines else subsequent_line_prefix
            line_content = prefix + current_line
            margin = max(0, width - visible_length(line_content))
            lines.append(line_content + ' ' * margin + RESET)

            # Start new line
            current_line = (" " * indent) + current_style + word

    # Add the last line
    if current_line:
        prefix = first_line_prefix if not lines else subsequent_line_prefix
        line_content = prefix + current_line
        margin = max(0, width - visible_length(line_content))
        lines.append(line_content + ' ' * margin + RESET)

    return [lines[0], *[current_style + x for x in lines[1:]]]

def line_format(line):
    def not_text(token):
        return not token or len(token.rstrip()) != len(token)

    # Apply OSC 8 hyperlink formatting after other formatting
    def process_links(match):
        description = match.group(1)
        url = match.group(2)
        return f'\033]8;;{url}\033\\{LINK}{description}{UNDERLINE[1]}\033]8;;\033\\'

    line = re.sub(r"\[([^\]]+)\]\(([^\)]+)\)", process_links, line)
    tokenList = re.finditer(r"((\*\*|\*|_|`)|[^_*`]+)", line)
    result = ""

    for match in tokenList:
        token = match.group(1)
        next_token = line[match.end()] if match.end() < len(line) else ""
        prev_token = line[match.start()-1] if match.start() > 0 else ""

        if token == "`":
            state.inline_code = not state.inline_code
            if state.inline_code:
                result += f'{BG}{MID}'
            else:
                result += state.bg
   
        # This is important here because we ignore formatting
        # inside of our code block.
        elif state.inline_code:
            result += token

        elif token == "**" and (state.in_bold or not_text(prev_token)):
            state.in_bold = not state.in_bold
            result += BOLD[0] if state.in_bold else BOLD[1]
 
        elif token == "*" and (state.in_italic or not_text(prev_token)):
            # This is the use case of talking about * and then following
            # up on something as opposed to *like this*.
            if state.in_italic or (not state.in_italic and next_token != ' '):
                state.in_italic = not state.in_italic
                result += ITALIC[0] if state.in_italic else ITALIC[1]
            else:
                result += token

        elif token == "_" and (state.in_underline or not_text(prev_token)):
            state.in_underline = not state.in_underline
            result += UNDERLINE[0] if state.in_underline else UNDERLINE[1]
        else:
            result += token

    return result

def parse(stream):
    last_line_empty_cache = None
    char = None
    process_buffer = False
    _master, _slave = pty.openpty()  # Create a new pseudoterminal
    try:
        while True:
            if state.is_pty:
                ready, _, _ = select.select([_slave, stream.fileno(), _master], [], [], state.Timeout)

                if _master in ready:  # Read from PTY
                    data = os.read(_master, 1024)
                    if not data: break
                    clean_data = KEYCODE_RE.sub('', data.decode('utf-8'))
                    os.write(sys.stdout.fileno(), clean_data.encode())  # Write to stdout

                char = b''
                if stream.fileno() in ready:  # Read from stdin
                    while True:
                        char += os.read(stream.fileno(), 1)

                        # If this doesn't work then we are on a multi-byte
                        # and should be read in many of them
                        try:
                            char.decode('utf-8')
                            break
                        except:
                            continue

                    # This means end of input
                    if char == b'': break

                else:
                    # here I want to say there's nothing more we can add
                    # right this second and we should process the line
                    if len(state.buffer):
                        process_buffer = True

            else:
                char = stream.read(1)
                if len(char) == 0: break
                if type(char) is str:
                    char = char.encode('utf-7')

            if char:
                state.buffer += char.decode('utf-8')

            debug_write(char.decode('utf-8'))
            if char != b'\n' and not process_buffer : continue

            state.has_newline = state.buffer.endswith('\n')
            process_buffer = False

            state.maybe_prompt = not state.has_newline and state.current()['none'] and re.match('^.*>', state.buffer)

            # let's wait for a newline
            if state.maybe_prompt:
                state.emit_flag = Code.Flush
                yield state.buffer
                state.buffer = ''
                continue

            if not state.has_newline:
                continue

            # Process complete line
            line = state.buffer
            state.buffer = ''
            # --- Collapse Multiple Empty Lines if not in code blocks ---
            if not state.in_code:
                is_empty = line.strip() == ""

                if is_empty and state.last_line_empty:
                    continue  # Skip processing this line
                elif is_empty:
                    state.last_line_empty = True
                    yield state.space_left()
                    continue
                else:
                    last_line_empty_cache = state.last_line_empty
                    state.last_line_empty = False

            # This is to reset our top-level line-based systems
            # \n buffer
            if not state.in_list and len(state.ordered_list_numbers) > 0:
                state.ordered_list_numbers[0] = 0
            else:
                state.in_list = False

            if state.first_indent == None:
                state.first_indent = len(line) - len(line.lstrip())
            if len(line) - len(line.lstrip()) >= state.first_indent:
                line = line[state.first_indent:]
            else:
                logging.warning("Indentation decreased from first line.")


            # Indent guaranteed

            # in order to stream tables and keep track of the headers we need to know whether
            # we are in table or not table otherwise > 1 tables won't have a stylized header
            if state.in_table and not state.in_code and not re.match(r"^\s*\|.+\|\s*$", line):
                state.in_table = False

            block_match = re.match(r"^<.?think>$", line)
            if block_match:
                state.in_blockquote = not state.in_blockquote
                # consume and don't emit
                if not state.in_blockquote:
                    yield( RESET)
                continue

            #
            # <code><pre>
            #
            # This needs to be first
            if not state.in_code:
                code_match = re.match(r"\s*```\s*([^\s]+|$)", line)
                if code_match:
                    state.in_code = Code.Backtick
                    state.code_language = code_match.group(1) or 'Bash'

                elif state.CodeSpaces and last_line_empty_cache and not state.in_list:
                    code_match = re.match(r"^    \s*[^\s\*]", line)
                    if code_match:
                        state.in_code = Code.Spaces
                        state.code_language = 'Bash'

                if state.in_code:
                    state.code_buffer = ""
                    state.code_gen = 0
                    state.code_first_line = True
                    state.bg = f"{BG}{DARK}"
                    state.where_from = "code pad"
                    if state.PrettyPad:
                        yield state.CODEPAD[0]

                    logging.debug(f"In code: ({state.in_code})")

                    if state.in_code == Code.Backtick:
                        continue

            if state.in_code:
                try:
                    if not state.code_first_line and (
                            (                     state.in_code == Code.Backtick and     line.strip() == "```"  ) or
                            (state.CodeSpaces and state.in_code == Code.Spaces   and not line.startswith('    '))
                        ):
                        state.code_language = None
                        state.code_indent = 0
                        code_type = state.in_code
                        state.in_code = False
                        state.bg = BGRESET

                        state.where_from = "code pad"
                        if state.PrettyPad:
                            yield state.CODEPAD[1]

                        logging.debug(f"code: {state.in_code}")

                        if code_type == Code.Backtick:
                            continue
                        else:
                            # otherwise we don't want to consume
                            # nor do we want to be here.
                            raise Goto()

                    if state.code_first_line:
                        state.code_first_line = False
                        try:
                            lexer = get_lexer_by_name(state.code_language)
                            custom_style = get_style_by_name(STYLE)
                        except pygments.util.ClassNotFound:
                            lexer = get_lexer_by_name("Bash")
                            custom_style = get_style_by_name("default")

                        formatter = Terminal256Formatter(style=custom_style)
                        for i, char in enumerate(line):
                            if char == " ":
                                state.code_indent += 1
                            else:
                                break
                        line = line[state.code_indent :]

                    elif line.startswith(" " * state.code_indent):
                        line = line[state.code_indent :]

                    # By now we have the properly stripped code line
                    # in the line variable. Add it to the buffer.
                    state.code_line += line
                    if state.code_line.endswith('\n'):
                        line = state.code_line
                        state.code_line = ''
                    else:
                        continue

                    indent, line_wrap = code_wrap(line)
                    
                    state.where_from = "in code"
                    for tline in line_wrap:
                        # wrap-around is a bunch of tricks. We essentially format longer and longer portions of code. The problem is
                        # the length can change based on look-ahead context so we need to use our expected place (state.code_gen) and
                        # then naively search back until our visible_lengths() match. This is not fast and there's certainly smarter
                        # ways of doing it but this thing is way trickery than you think
                        highlighted_code = highlight(state.code_buffer + tline, lexer, formatter)
        
                        # Since we are streaming we ignore the resets and newlines at the end
                        if highlighted_code.endswith(FGRESET + "\n"):
                            highlighted_code = highlighted_code[: -(1 + len(FGRESET))]

                        # turns out highlight will eat leading newlines on empty lines
                        vislen = visible_length(state.code_buffer.lstrip())

                        delta = 0
                        while visible_length(highlighted_code[:(state.code_gen-delta)]) > vislen:
                            delta += 1

                        state.code_buffer += tline

                        this_batch = highlighted_code[state.code_gen-delta :]
                        if this_batch.startswith(FGRESET):
                            this_batch = this_batch[len(FGRESET) :]

                        ## this is the crucial counter that will determine
                        # the begninning of the next line
                        state.code_gen = len(highlighted_code)

                        code_line = ' ' * indent + this_batch.strip()

                        margin = state.FullWidth - visible_length(code_line)
                        yield f"{CODEBG}{code_line}{' ' * max(0, margin)}{BGRESET}"  
                    continue
                except Goto as ex:
                    pass
                
                except Exception as ex:
                    logging.warning(f"Code parsing error: {ex}")
                    traceback.print_exc()
                    pass

            #
            # <table>
            #
            if re.match(r"^\s*\|.+\|\s*$", line) and not state.in_code:
                cells = [c.strip() for c in line.strip().strip("|").split("|")]

                # This guarantees we are at the first line
                # \n buffer
                if not state.in_table:
                    state.in_table = Code.Header

                elif state.in_table == Code.Header:
                    # we ignore the separator, this is just a check
                    if not re.match(r"^[\s|:-]+$", line):
                        logging.warning(f"Table definition row 2 was NOT a separator. Instead it was:\n({line})")

                    # Let's assume everything worked out I guess.
                    # We set our header to false and basically say we are expecting the body
                    state.in_table = Code.Body 
                    continue

                yield from format_table(cells)
                continue

            #
            # <li> <ul> <ol>
            # llama-4 maverick uses + for lists ... for some reason
            list_item_match = re.match(r"^(\s*)([\+*\-]|\d+\.)\s+(.*)", line)
            if list_item_match:
                state.in_list = True

                indent = len(list_item_match.group(1))
                list_type = "number" if list_item_match.group(2)[0].isdigit() else "bullet"
                content = list_item_match.group(3)

                # Handle stack
                while state.list_item_stack and state.list_item_stack[-1][0] > indent:
                    state.list_item_stack.pop()  # Remove deeper nested items
                    if state.ordered_list_numbers:
                        state.ordered_list_numbers.pop()
                if state.list_item_stack and state.list_item_stack[-1][0] < indent:
                    # new nested list
                    state.list_item_stack.append((indent, list_type))
                    state.ordered_list_numbers.append(0)
                elif not state.list_item_stack:
                    # first list
                    state.list_item_stack.append((indent, list_type))
                    state.ordered_list_numbers.append(0)
                if list_type == "number":
                    state.ordered_list_numbers[-1] += 1

                indent = len(state.list_item_stack) * 2

                wrap_width = state.Width - indent - (2 * LIST_INDENT) 

                bullet = '•'
                if list_type == "number":
                    list_number = int(max(state.ordered_list_numbers[-1], float(list_item_match.group(2))))
                    bullet = f"{list_number}"
                
                wrapped_lineList = wrap_text(content, wrap_width, LIST_INDENT, buffer = False,
                    first_line_prefix      = f"{(' ' * (indent - len(bullet)))}{FG}{SYMBOL}{bullet}{RESET} ",
                    subsequent_line_prefix = " " * (indent - 1)
                )
                for wrapped_line in wrapped_lineList:
                    yield f"{state.space_left()}{wrapped_line}\n"
                continue
            #
            # <h1> <h2> <h3>
            # <h4> <h5> <h6>
            # 
            header_match = re.match(r"^\s*(#{1,6})\s+(.*)", line)
            if header_match:
                level = len(header_match.group(1))
                yield emit_h(level, header_match.group(2))
                continue

            #
            # <hr>
            #
            hr_match = re.match(r"^[\s]*([-=_]){3,}[\s]*$", line)
            if hr_match:
                if state.last_line_empty or last_line_empty_cache:
                    # print a horizontal rule using a unicode midline 
                    yield f"{MARGIN_SPACES}{FG}{SYMBOL}{'─' * state.Width}{RESET}"
                else:
                    # We tell the next level up that the beginning of the buffer should be a flag.
                    # Underneath this condition it will no longer yield
                    state.emit_flag = 1 if '-' in hr_match.groups(1) else 2
                    yield ""
                continue

            state.where_from = "emit_normal"
            if len(line) == 0: yield ""
            if len(line) < state.Width:
                # we want to prevent word wrap
                yield f"{state.space_left()}{line_format(line)}"
            else:
                wrapped_lines = wrap_text(line)
                for wrapped_line in wrapped_lines:
                    yield f"{state.space_left()}{wrapped_line}\n"

    except Exception as e:
        logging.error(f"Parser error: {str(e)}")
        traceback.print_exc()
        raise

def get_terminal_width():
    try:
        return shutil.get_terminal_size().columns
    except (AttributeError, OSError):
        return 80

def main():
    parser = ArgumentParser(description="Streamdown - A markdown renderer for modern terminals")
    parser.add_argument("filename", nargs="?", help="Input file to process (also takes stdin)")
    parser.add_argument("-l", "--loglevel", default="INFO", help="Set the logging level")
    parser.add_argument("-w", "--width", default="0", help="Set the width")
    parser.add_argument("-e", "--exec", help="Wrap a program for more 'proper' i/o handling")
    args = parser.parse_args()
    
    state.FullWidth = int(args.width)
    if not state.FullWidth:
        state.FullWidth = features.get("Width") or int(get_terminal_width())
    
    state.Width = state.FullWidth - 2 * MARGIN
    state.CODEPAD = [
        f"{RESET}{FG}{DARK}{'▄' * state.FullWidth}{RESET}\n",
        f"{RESET}{FG}{DARK}{'▀' * state.FullWidth}{RESET}"
    ]

    logging.basicConfig(stream=sys.stdout,
        level=os.getenv('LOGLEVEL') or getattr(logging, args.loglevel.upper()), format=f'%(message)s')
    try:
        inp = sys.stdin
        if args.exec:
            state.sub = subprocess.Popen(args.exec.split(' '), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            inp = state.sub.stdout

        elif args.filename:
            inp = open(args.filename, "r")
        elif sys.stdin.isatty():
            parser.print_help()
            sys.exit()
        else:
            # this is a more sophisticated thing that we'll do in the main loop
            state.is_pty = True
            os.set_blocking(inp.fileno(), False) 

        buffer = []
        flush = False
        for chunk in parse(inp):
            #logging.debug(state.where_from)
            if state.emit_flag:
                if state.emit_flag == Code.Flush:
                    flush = True
                    state.emit_flag = None
                else:
                    buffer[0] = emit_h(state.emit_flag, buffer[0])
                    state.emit_flag = None
                    # and we abandon the yield
                    continue

            if not state.has_newline:
                chunk = chunk.rstrip("\n")
            elif not chunk.endswith("\n"):
                chunk += "\n"

            if chunk.endswith("\n"):
                state.current_line = ''
            else:
                state.current_line += chunk
                
            buffer.append(chunk)
            if flush:
                chunk = "\n".join(buffer)
                buffer = []
                flush = False

            elif len(buffer) == 1:
                continue

            else:
                chunk = buffer.pop(0)

            if state.is_pty:
                print(chunk, end="", flush=True)
            else:
                sys.stdout.write(chunk)

        if len(buffer):
            chunk = buffer.pop(0)
            if state.is_pty:
                print(chunk, end="", flush=True)
            else:
                sys.stdout.write(chunk)

    except KeyboardInterrupt:
        state.exit = 130
        
    except Exception as ex:
        logging.warning(f"Exception thrown: {ex}")

    if state.Clipboard and state.code_buffer:
        code = state.code_buffer
        # code needs to be a base64 encoded string before emitting
        code_bytes = code.encode('utf-8')
        base64_bytes = base64.b64encode(code_bytes)
        base64_string = base64_bytes.decode('utf-8')
        print(f"\033]52;c;{base64_string}\a", end="", flush=True)

    sys.exit(state.exit)

if __name__ == "__main__":
    main()
