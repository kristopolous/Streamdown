#!/bin/bash
# /// script
# requires-python = ">=3.8"
# dependencies = [
#     "pygments",
#     "pylatexenc",
#     "appdirs",
#     "term-image",
#     "toml"
# ]
# ///
'''':
if command -v uv &> /dev/null; then
    exec uv run --script "$0" "$@"
else
    exec python3 "$0" "$@"
fi
'''
import appdirs, toml
import logging, tempfile
import os,      sys
import pty,     select
import termios, tty

import math
import re
import shutil
import subprocess
import traceback
import colorsys
import base64
from io import BytesIO
from term_image.image import from_file, from_url
import pygments.util
from functools import reduce
from argparse import ArgumentParser
from pygments import highlight
from pygments.lexers import get_lexer_by_name
from pygments.formatters import TerminalTrueColorFormatter
from pygments.styles import get_style_by_name

if __package__ is None:
    from plugins import latex
else:
    from .plugins import latex

default_toml = """
[features]
CodeSpaces = false
Clipboard  = true
Logging    = false
Timeout    = 0.1
Savebrace  = true

[style]
Margin          = 2
ListIndent      = 2
CodeIndent      = 2
PrettyPad       = true
PrettyBroken    = true
Width           = 0
HSV     = [0.8, 0.5, 0.5]
Dark    = { H = 1.00, S = 1.50, V = 0.25 }
Mid     = { H = 1.00, S = 1.00, V = 0.50 }
Symbol  = { H = 1.00, S = 1.00, V = 1.50 }
Head    = { H = 1.00, S = 1.00, V = 1.75 }
Grey    = { H = 1.00, S = 0.25, V = 1.37 }
Bright  = { H = 1.00, S = 2.00, V = 2.00 }
Syntax  = "dracula"
"""

def ensure_config_file(config):
    if config:
        config_path = config
    else:
        config_dir = appdirs.user_config_dir("streamdown")
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, "config.toml")
        if not os.path.exists(config_path):
            open(config_path, 'w').write(default_toml)

    return config_path, open(config_path).read()


FG = "\033[38;2;"
BG = "\033[48;2;"
RESET = "\033[0m"
FGRESET = "\033[39m"
BGRESET = "\033[49m"

BOLD      = ["\033[1m", "\033[22m"]
UNDERLINE = ["\033[4m", "\033[24m"]
ITALIC    = ["\033[3m", "\033[23m"]
STRIKEOUT = ["\033[9m", "\033[29m"]
LINK      = ["\033]8;;", "\033]8;;\033\\"]
SUPER     = [ 0x2070, 0x00B9, 0x00B2, 0x00B3, 0x2074, 0x2075, 0x2076, 0x2077, 0x2078, 0x2079 ]

ESCAPE = r"\033\[[0-9;]*[mK]"
ANSIESCAPE = r'\033(?:\[[0-9;?]*[a-zA-Z]|][0-9]*;;.*?\\|\\)'
KEYCODE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

visible = lambda x: re.sub(ANSIESCAPE, "", x)
# cjk characters are double width
visible_length = lambda x: len(visible(x)) + dbl_count(x)
extract_ansi_codes = lambda text: re.findall(ESCAPE, text)
remove_ansi = lambda line, codeList: reduce(lambda line, code: line.replace(code, ''), codeList, line)

def gettmpdir():
    tmp_dir_all = os.path.join(tempfile.gettempdir(), "sd")
    os.makedirs(tmp_dir_all, mode=0o777, exist_ok=True)
    tmp_dir = os.path.join(tmp_dir_all, str(os.getuid()))
    os.makedirs(tmp_dir, exist_ok=True)
    return tmp_dir

def debug_write(text):
    if state.Logging:
        if state.Logging == True:
            state.Logging = tempfile.NamedTemporaryFile(dir=gettmpdir(), prefix="dbg", delete=False, mode="wb")
        state.Logging.write(text)

def savebrace():
    if state.Savebrace and state.code_buffer_raw:
        path = os.path.join(gettmpdir(), 'savebrace')
        with open(path, "a") as f:
            f.write(state.code_buffer_raw + "\x00")
            f.flush()

class Goto(Exception):
    pass

class Style:
    pass

class Code:
    Spaces = 'spaces'
    Backtick = 'backtick'
    Header = 'header'
    Body = 'body'
    Flush = 'flush'

class ParseState:
    def __init__(self):
        self.buffer = b''
        self.current_line = ''
        self.first_line = True
        self.last_line_empty = False
        self.is_pty = False
        self.is_exec = False
        self.maybe_prompt = False
        self.emit_flag = None
        self.scrape = None
        self.scrape_ix = 0
        self.terminal = None

        self.WidthArg = None
        self.WidthFull = None
        self.WidthWrap = False

        # If the entire block is indented this will
        # tell us what that is
        self.first_indent = None
        self.has_newline = False
        self.bg = BGRESET    

        # These are part of a trick to get
        # streaming code blocks while preserving
        # multiline parsing.
        self.code_buffer = ""
        self.code_buffer_raw = ""
        self.code_gen = 0
        self.code_language = None
        self.code_first_line = False
        self.code_indent = 0
        self.code_line = ''

        self.ordered_list_numbers = []
        self.list_item_stack = []  # stack of (indent, type)
        self.list_indent_text = 0

        self.in_list = False
        self.in_code = False # (Code.[Backtick|Spaces] | False)
        self.inline_code = False
        self.in_bold = False
        self.in_italic = False
        self.in_table = False # (Code.[Header|Body] | False)
        self.in_underline = False
        self.in_strikeout = False
        self.block_depth = 0

        self.exec_sub = None
        self.exec_master = None
        self.exec_slave = None
        self.exec_kb = 0

        self.exit = 0
        self.where_from = None

    def current(self):
        state = { 'inline': self.inline_code, 'code': self.in_code, 'bold': self.in_bold, 'italic': self.in_italic, 'underline': self.in_underline, 'strikeout': self.in_strikeout }
        state['none'] = all(item is False for item in state.values())
        return state

    def reset_inline(self):
        self.inline_code = self.in_bold = self.in_italic = self.in_underline = self.in_strikeout = False

    def full_width(self, offset = 0):
        return offset + (state.current_width(listwidth = True) if Style.PrettyBroken else self.WidthFull)

    def current_width(self, listwidth = False):
        # this will double count the left margin
        return self.Width - (len(visible(self.space_left(listwidth)))) + Style.Margin

    def space_left(self, listwidth = False):
        pre = ' ' * (len(state.list_item_stack)) * Style.ListIndent if listwidth else ''
        return pre + Style.MarginSpaces + (Style.Blockquote * self.block_depth) if len(self.current_line) == 0 else "" 

state = ParseState()

def override_background(style_name, background_color):
    base_style = get_style_by_name(style_name)
    base_style.background_color = background_color
    for i in base_style:
        i[1]['bgcolor'] = background_color
    for i,v in base_style.styles.items():
        if v and 'bg' in v:
            base_style.styles[i] = re.sub(r'bg:[^ ]*', '', base_style.styles[i] )
    for k,v in base_style._styles.items():
         if v[4] != '':
             v[4] = ''

    return base_style

def format_table(rowList):
    num_cols = len(rowList)
    row_height = 0
    wrapped_cellList = []

    # Calculate max width per column (integer division)
    # Subtract num_cols + 1 for the vertical borders '│'
    available_width = state.current_width() - (num_cols * 2)

    width_base = available_width // num_cols
    width_mod  = available_width % num_cols

    col_width_list = [width_base + (1 if i < width_mod else 0) for i in range(num_cols)]
    bg_color = Style.Mid if state.in_table == Style.Head else Style.Dark
    state.bg = f"{BG}{bg_color}"

    # First Pass: Wrap text and calculate row heights
    # Note this is where every cell is formatted so if 
    # you are styling, do it before here!
    for ix in range(len(rowList)):
        row = rowList[ix]
        wrapped_cell = text_wrap(row, width=col_width_list[ix], force_truncate=True)

        # Ensure at least one line, even for empty cells
        if not wrapped_cell:
            wrapped_cell = [""]

        wrapped_cellList.append(wrapped_cell)
        row_height = max(row_height, len(wrapped_cell))

    # --- Second Pass: Format and emit rows ---
    for ix in range(row_height):
        # This is the fancy row separator
        extra = f"\033[4;58;2;{Style.Mid}" if not state.in_table == Style.Head and (ix == row_height - 1) else ""
        line_segments = []

        # Now we want to snatch this row index from all our cells
        for iy in range(len(wrapped_cellList)):
            cell = wrapped_cellList[iy]
            segment = ''
            if ix < len(cell):
                segment = cell[ix]

            # Margin logic is correctly indented here
            margin_needed = col_width_list[iy] - visible_length(segment)
            margin_segment = segment + (" " * max(0, margin_needed))
            line_segments.append(f"{BG}{bg_color}{extra} {margin_segment}")

        # Correct indentation: This should be outside the c_idx loop
        joined_line = f"{BG}{bg_color}{extra}{FG}{Style.Symbol}│{RESET}".join(line_segments)
        # Correct indentation and add missing characters
        yield f"{state.space_left()}{FGRESET}{joined_line}{RESET}"

    state.bg = BGRESET

def emit_h(level, text):
    text = line_format(text)
    lineList = text_wrap(text)
    res = []
    for text in lineList:
        spaces_to_center = (state.current_width() -  visible_length(text)) / 2
        if level == 1:      #
            res.append(f"{state.space_left()}\n{state.space_left()}{BOLD[1]}{' ' * math.floor(spaces_to_center)}{text}{BOLD[1]}")
        elif level == 2:    ##
            res.append(f"{state.space_left()}\n{state.space_left()}{BOLD[0]}{FG}{Style.Bright}{' ' * math.floor(spaces_to_center)}{text}{' ' * math.ceil(spaces_to_center)}{BOLD[1]}{FGRESET}")
        elif level == 3:    ###
            res.append(f"{state.space_left()}{FG}{Style.Head}{BOLD[0]}{text}{RESET}")
        elif level == 4:    ####
            res.append(f"{state.space_left()}{FG}{Style.Symbol}{text}{RESET}")
        elif level == 5:    #####
            res.append(f"{state.space_left()}{text}{RESET}")
        else: 
            res.append(f"{state.space_left()}{FG}{Style.Grey}{text}{RESET}")
    return "\n".join(res)

def code_wrap(text_in):
    if not Style.PrettyBroken and state.WidthWrap and len(text_in) > state.full_width():
        return (0, [text_in])

    # get the indentation of the first line
    indent = len(text_in) - len(text_in.lstrip())
    text = text_in.lstrip()
    mywidth = state.full_width(-4 if Style.PrettyBroken else 0) - indent

    # We take special care to preserve empty lines
    if len(text) == 0:
        return (0, [text_in])
    res = [text[:mywidth]]

    for i in range(mywidth, len(text), mywidth):
        res.append(text[i : i + mywidth])

    # sometimes just a newline wraps ... this isn't what we want actually
    if res[-1].strip() == '':
        res.pop()

    return (indent, res)


# This marvelously obscure code "compacts" long lines of repetitive ANSI format strings by
# removing duplicates. Here's how it works
def ansi_collapse(codelist, inp):
    # We break SGR strings into various classes concerning their applicate or removal
    nums = {
        'fg': r'3\d', 'bg': r'4\d',
        'b': r'2?[12]', 'i': r'2?3', 'u': r'3?2',
        'reset': '0'
    }

    # We have a routine that creates large regex matching strings for them based on 
    # lists that can pass to it
    sgr = lambda l: re.compile(r'\x1b\[(' + '|'.join(l) +')[0-9;]*m')

    for stanza in inp:
        # We construct a named-register regex using the dictionary and run it
        # over a stanza of our input
        mg = re.search( sgr([f'(?P<{k}>{v})' for k, v in nums.items()]), stanza )

        if mg:
            # this means we now have a dictionary populated with whether 
            # we have those tags or not
            mg = mg.groupdict()

            # if it's a reset we can disregard everything
            if mg['reset']:
                return inp                 

            # Find the tags we have by doing a dictionary None check. Make new regex SGR ANSI codes from it
            my_filter = sgr( [nums[k] for k, v in mg.items() if v] )

            # Use that code list as a filter to remove extra
            codelist = list(filter(lambda x: not re.search( my_filter, x ), codelist))

    return codelist + inp


def split_text(text):
    return [x for x in re.split(
        r'(?<=['
            r'\u3000-\u303F'
            r'\u4E00-\u9FFF'
            r'\u3400-\u4DBF'
            r'\uF900-\uFAFF'
          r'])|(?=['
            #r'\u4E00-\u9FFF'
            r'\u3400-\u4DBF'
            r'\uF900-\uFAFF'
          r'])|\s+',
        text
    ) if x]

def text_wrap(text, width = -1, indent = 0, first_line_prefix="", subsequent_line_prefix="", force_truncate=False):
    if width == -1:
        width = state.Width

    # The empty word clears the buffer at the end.
    formatted = line_format(text)
    #print(bytes(formatted, 'utf-8'), formatted)
    words = split_text(formatted) + [""]
    #print([bytes(i, 'utf-8') for i in words])

    lines = []
    current_line = ""
    current_style = []
    
    oldword = ''
    for word in words:
        # we apply the style if we see it at the beginning of the word
        codes = extract_ansi_codes(word)
        if len(codes) and word.startswith(codes[0]):
            # this pop(0) is intentional
            current_style.append(codes.pop(0))

        if len(word) and visible_length(current_line) + visible_length(word) + 1 <= width:  # +1 for space
            space = ""
            if len(visible(word)) > 0 and current_line:
                space = " "
            if (":" in visible(word) or cjk_count(word)) and cjk_count(oldword):
                space = ""
            current_line += space + word
        else:
            # Word doesn't fit, finalize the previous line
            prefix = first_line_prefix if not lines else subsequent_line_prefix
            line_content = prefix + current_line
            # This is expensive, fix.
            while force_truncate and visible_length(line_content) >= width:
                line_content = line_content[:len(line_content) - 2] + "…"

            margin = max(0, width - visible_length(line_content))

            if line_content.strip() != "":
                # We make absolutely positively sure beyond any doubt
                # that we have closed our hyperlink OSC
                if LINK[0] in line_content:
                    line_content += LINK[1]
                lines.append(line_content + state.bg + ' ' * margin)

            current_line = (" " * indent) + "".join(current_style) + word

        if len(codes):
            current_style += codes

        if codes:
            current_style = ansi_collapse(current_style, codes)

        oldword = word

    if len(lines) < 1:
        return []

    if len(lines) == 1:
        lines[0] = lines[0].rstrip()

    return lines

def dbl_count(s):
    dbl_re = re.compile(
        r'[\u2e80-\u2eff\u3000-\u303f\u3400-\u4dbf'
        r'\uFF00-\uFFEF'       # CJK Compatibility Punctuation
        r'\U00004e00-\U00009fff\U0001f300-\U0001f6ff'
        r'\U0001f900-\U0001f9ff\U0001fa70-\U0001faff]',
        re.UNICODE
    )
    return len(dbl_re.findall(visible(s)))

def cjk_count(s):
    cjk_re = re.compile(
        r'[\u4E00-\u9FFF'      # CJK Unified Ideographs
        r'\u3400-\u4DBF'       # CJK Unified Ideographs Extension A
        r'\uF900-\uFAFF'       # CJK Compatibility Ideographs
        r'\uFF00-\uFFEF'       # CJK Compatibility Punctuation
        r'\u3000-\u303F'      # CJK Symbols and Punctuation
        r'\U0002F800-\U0002FA1F]' # CJK Compatibility Ideographs Supplement
    )
    
    return len(cjk_re.findall(visible(s)))

def line_format(line):
    not_text = lambda token: not (token.isalnum() or token in ['\\','"']) or cjk_count(token)
    footnotes = lambda match: ''.join([chr(SUPER[int(i)]) for i in match.group(1)])

    def process_images(match):
        url = match.group(2)
        try:
            if re.match(r"https://", url.lower()):
                image = from_url(url)
            else: 
                image = from_file(url)
            image.height = 20
            print(f"{image:|.-1#}")
        except:
            return match.group(2)

    # Apply OSC 8 hyperlink formatting after other formatting
    def process_links(match):
        description = match.group(1)
        url = match.group(2)
        return f'{LINK[0]}{url}\033\\{Style.Link}{description}{UNDERLINE[1]}{LINK[1]}{FGRESET}'

    line = re.sub(r"\!\[([^\]]*)\]\(([^\)]+)\)", process_images, line)
    line = re.sub(r"\[([^\]]+)\]\(([^\)]+)\)", process_links, line)
    line = re.sub(r"\[\^(\d+)\]:?", footnotes, line)

    tokenList = re.finditer(r"((~~|\*\*_|_\*\*|\*{1,3}|_{1,3}|`+)|[^~_*`]+)", line)
    result = ""

    last_pos = 0
    for match in tokenList:
        if match.span()[0] > last_pos:
            result += line[last_pos:match.span()[0]]

        last_pos = match.span()[1]
        token = re.sub(r'\s+',' ', match.group(1))
        next_token = line[match.end()] if match.end() < len(line) else ""
        prev_token = line[match.start()-1] if match.start() > 0 else ""

        # This trick makes sure that things like `` ` `` render right.
        if "`" in token and (not state.inline_code or state.inline_code == token):
            if state.inline_code:
                if ' ' in state.inline_code:
                    savebrace()
                state.inline_code = False
            else:
                state.inline_code = token
                state.code_buffer_raw = ''

            if state.inline_code:
                result += f'{BG}{Style.Mid}'
            else:
                result += state.bg
                state.code_buffer_raw = ''
   
        # This is important here because we ignore formatting
        # inside of our code block.
        elif state.inline_code:
            result += token
            state.code_buffer_raw += token

        elif token == '~~' and (state.in_strikeout or not_text(prev_token)):
            state.in_strikeout = not state.in_strikeout
            result += STRIKEOUT[0] if state.in_strikeout else STRIKEOUT[1]

        elif token in ['**_','_**','___','***'] and (state.in_bold or not_text(prev_token)):
            state.in_bold = not state.in_bold
            result += BOLD[0] if state.in_bold else BOLD[1]
            state.in_italic = not state.in_italic
            result += ITALIC[0] if state.in_italic else ITALIC[1]

        elif (token == '__' or token == "**") and (state.in_bold or not_text(prev_token)):
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

        elif token == "_" and (state.in_underline or (not_text(prev_token) and next_token.isalnum())):
            state.in_underline = not state.in_underline
            result += UNDERLINE[0] if state.in_underline else UNDERLINE[1]
        else:
            result += token

    return result

def parse(stream):
    last_line_empty_cache = None
    byte = None
    TimeoutIx = 0
    lexer = None
    while True:
        if state.is_pty or state.is_exec:
            byte = None
            ready_in, _, _ = select.select(
                    [stream.fileno(), state.exec_master], [], [], state.Timeout)

            if state.is_exec: 
                # This is keyboard input
                if stream.fileno() in ready_in:
                    byte = os.read(stream.fileno(), 1)

                    state.exec_kb += 1
                    os.write(state.exec_master, byte)

                    if byte in [b'\n', b'\r']:
                        state.buffer = b''
                        print("")
                        state.exec_kb = 0
                    else:
                        continue

                if state.exec_master in ready_in:
                    TimeoutIx = 0
                    byte = os.read(state.exec_master, 1)

                    if state.exec_kb:
                        os.write(sys.stdout.fileno(), byte)

                if len(ready_in) == 0:
                    TimeoutIx += 1

            elif stream.fileno() in ready_in: 
                byte = os.read(stream.fileno(), 1)
                TimeoutIx = 0
            elif TimeoutIx == 0:
                # This is our record separator for debugging - hands peaking
                debug_write("🫣".encode('utf-8'))
                TimeoutIx += 1

        else:
            byte = stream.read(1)

        if byte is not None:
            if byte == b'': break
            state.buffer += byte
            debug_write(byte)

        if not (byte == b'\n' or byte is None): continue

        line = state.buffer.decode('utf-8').replace('\t','  ')
        state.has_newline = line.endswith('\n')
        # I hate this. There should be better ways.
        state.maybe_prompt = not state.has_newline and state.current()['none'] and re.match(r'^.*>\s+$', visible(line))

        # let's wait for a newline
        if state.maybe_prompt:
            state.emit_flag = Code.Flush
            yield line
            state.current_line = ''
            state.buffer = b''

        if not state.has_newline:
            continue

        state.buffer = b''
        """
        # Run through the plugins first
        res = latex.Plugin(line, state, Style)
        if res is True:
            # This means everything was consumed by our plugin and 
            # we should continue
            continue
        elif res is not None:
            for row in res:
                yield row
                continue
        """
        
        # running this here avoids stray |
        block_match = re.match(r"^\s*((>\s*)+|<.?think>)", line)
        if not state.in_code and block_match:
            if block_match.group(1) == '</think>':
                state.block_depth = 0
                yield RESET
            elif block_match.group(1) == '<think>':
                state.block_depth = 1
            else:
                state.block_depth = block_match.group(0).count('>')
                # we also need to consume those tokens
                line = line[len(block_match.group(0)):]
        else:
            if state.block_depth > 0:
                yield FGRESET
                state.block_depth = 0

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
        elif (not line.startswith(' ' * state.list_indent_text)) and line.strip() != "":
            state.in_list = False
            state.list_indent_text = 0

        if state.first_indent is None:
            state.first_indent = len(line) - len(line.lstrip())
        if len(line) - len(line.lstrip()) >= state.first_indent:
            line = line[state.first_indent:]
        else:
            logging.debug("Indentation decreased from first line.")


        # Indent guaranteed

        # in order to stream tables and keep track of the headers we need to know whether
        # we are in table or not table otherwise > 1 tables won't have a stylized header
        if state.in_table and not state.in_code and not re.match(r"^\s*\|.+\|\s*$", line):
            state.in_table = False

        # <code><pre>
        if not state.in_code:
            code_match = re.match(r"^\s*(```|<pre>)\s*([^\s]+|$)\s*$", line)
            if code_match:
                state.in_code = Code.Backtick
                state.code_indent = len(line) - len(line.lstrip())
                state.code_language = code_match.group(1) or 'Bash'

            elif state.CodeSpaces and last_line_empty_cache and not state.in_list:
                code_match = re.match(r"^    \s*[^\s\*]", line)
                if code_match:
                    state.in_code = Code.Spaces
                    state.code_language = 'Bash'

            if state.in_code:
                state.code_buffer = state.code_buffer_raw = ""
                state.code_gen = 0
                state.code_first_line = True
                state.bg = f"{BG}{Style.Dark}"
                state.where_from = "code pad"
                if Style.PrettyPad or Style.PrettyBroken:
                    if not Style.PrettyPad:
                        yield ""

                    yield Style.Codepad[0]
                else:
                    yield ""

                logging.debug(f"In code: ({state.in_code})")

                if state.in_code == Code.Backtick:
                    continue

        if state.in_code:
            try:
                # This is turning it OFF
                if ( (                     state.in_code == Code.Backtick and     line.strip() in ["</pre>", "```"]  ) or 
                     (state.CodeSpaces and state.in_code == Code.Spaces   and not line.startswith('    ')) ):
                    if state.scrape:
                        ext = "sh"
                        try:
                            ext = get_lexer_by_name(state.code_language).filenames[0].split('.')[-1]
                        except:
                            logging.warning(f"Can't find canonical extension for {state.code_language}")
                            pass

                        open(os.path.join(state.scrape, f"file_{state.scrape_ix}.{ext}"), 'w').write(state.code_buffer_raw)
                        state.scrape_ix += 1

                    savebrace()
                    state.code_language = None
                    state.code_indent = 0
                    code_type = state.in_code
                    state.in_code = False
                    state.bg = BGRESET

                    state.where_from = "code pad"
                    if Style.PrettyPad or Style.PrettyBroken:
                        yield Style.Codepad[1] 
                        if not Style.PrettyPad:
                            yield ""

                    else:
                        yield RESET

                    logging.debug(f"code: {state.in_code}")
                    state.emit_flush = True
                    # We suppress the newline - it's not an explicit style
                    #state.has_newline = False
                    #yield RESET

                    if code_type == Code.Backtick:
                        continue
                    else:
                        # otherwise we don't want to consume
                        # nor do we want to be here.
                        raise Goto()

                if state.code_first_line or lexer is None:
                    state.code_first_line = False
                    try:
                        lexer = get_lexer_by_name(state.code_language)
                        custom_style = override_background(Style.Syntax, ansi2hex(Style.Dark))
                    except pygments.util.ClassNotFound:
                        lexer = get_lexer_by_name("Bash")
                        custom_style = override_background("default", ansi2hex(Style.Dark))

                    formatter = TerminalTrueColorFormatter(style=custom_style)
                    if line.startswith(' ' * state.code_indent):
                        line = line[state.code_indent :]

                elif line.startswith(" " * state.code_indent):
                    line = line[state.code_indent :]

                # By now we have the properly stripped code line
                # in the line variable. Add it to the buffer.
                state.code_buffer_raw += line
                state.code_line += line
                if state.code_line.endswith('\n'):
                    line = state.code_line
                    state.code_line = ''
                else:
                    continue

                highlighted_code = highlight(line, lexer, formatter)
                indent, line_wrap = code_wrap(line)
                
                state.where_from = "in code"
                pre = [state.space_left(listwidth = True), ' ' * Style.CodeIndent] if Style.PrettyBroken else ['', '']

                for tline in line_wrap:
                    # wrap-around is a bunch of tricks. We essentially format longer and longer portions of code. The problem is
                    # the length can change based on look-ahead context so we need to use our expected place (state.code_gen) and
                    # then naively search back until our visible_lengths() match. This is not fast and there's certainly smarter
                    # ways of doing it but this thing is way trickery than you think
                    highlighted_code = highlight(state.code_buffer + tline, lexer, formatter)
                    #print("(",highlighted_code,")")

                    # Sometimes the highlighter will do things like a full reset or a background reset.
                    # This is mostly not what we want
                    highlighted_code = re.sub(r"\033\[[34]9(;00|)m", "\033[23m", highlighted_code)
    
                    # Since we are streaming we ignore the resets and newlines at the end
                    if highlighted_code.endswith(FGRESET + "\n"):
                        highlighted_code = highlighted_code[: -(1 + len(FGRESET))]

                    # turns out highlight will eat leading newlines on empty lines
                    vislen = visible_length(state.code_buffer.lstrip())

                    delta = -2 
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

                    margin = state.full_width( -len(pre[1]) ) - visible_length(code_line) % state.WidthFull
                    yield f"{pre[0]}{Style.Codebg}{pre[1]}{code_line}{' ' * max(0, margin)}{BGRESET}"  
                continue
            except Goto:
                pass
            
            except Exception as ex:
                logging.warning(f"Code parsing error: {ex}")
                traceback.print_exc()
                pass

        # <table>
        if re.match(r"^\s*\|.+\|\s*$", line) and not state.in_code:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]

            # This guarantees we are at the first line
            # \n buffer
            if not state.in_table:
                state.in_table = Style.Head

            elif state.in_table == Style.Head:
                # we ignore the separator, this is just a check
                if not re.match(r"^[\s|:-]+$", line):
                    logging.warning(f"Table definition row 2 was NOT a separator. Instead it was:\n({line})")

                # Let's assume everything worked out I guess.
                # We set our header to false and basically say we are expecting the body
                state.in_table = Code.Body 
                continue

            yield from format_table(cells)
            continue

        # <li> <ul> <ol>
        # llama-4 maverick uses + and +- for lists ... for some reason
        content = line
        bullet = ' '
        list_item_match = re.match(r"^(\s*)([\+*\-] |\+\-+|\d+\.\s+)(.*)", line)
        if list_item_match:
            # llama 4 maverick does this weird output like this
            # 1. blah blah blah
            #    this should be a list
            #    
            #    ```bash
            #    blah blah
            #    ```
            #
            #    still in the list
            # We do this here so that the first line which is the bullet
            # line gets the proper hang
            state.list_indent_text = len(list_item_match.group(2)) - 1
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

            bullet = '•'
            if list_type == "number":
                list_number = int(max(state.ordered_list_numbers[-1], float(list_item_match.group(2))))
                bullet = str(list_number)

        # This is intentional ... we can get here in llama 4 using
        # a weird thing
        if state.in_list:
            indent = (len(state.list_item_stack) - 1) * Style.ListIndent #+ (len(bullet) - 1)
            wrap_width = state.current_width() - Style.ListIndent
            
            wrapped_lineList = text_wrap(content, wrap_width, Style.ListIndent,
                first_line_prefix = f"{(' ' * indent)}{FG}{Style.Symbol}{bullet}{RESET} ",
                subsequent_line_prefix = " " * (indent)
            )
            for wrapped_line in wrapped_lineList:
                yield f"{state.space_left()}{wrapped_line}\n"

            continue

        # <h1> ... <h6>
        header_match = re.match(r"^\s*(#{1,6})\s+(.*)", line)
        if header_match:
            level = len(header_match.group(1))
            yield emit_h(level, header_match.group(2))
            continue

        # <hr>
        hr_match = re.match(r"^[\s]*([-\*=_]){3,}[\s]*$", line)
        if hr_match:
            if state.last_line_empty or last_line_empty_cache:
                # print a horizontal rule using a unicode midline 
                yield f"{Style.MarginSpaces}{FG}{Style.Symbol}{'─' * state.Width}{RESET}"
            else:
                # We tell the next level up that the beginning of the buffer should be a flag.
                # Underneath this condition it will no longer yield
                state.emit_flag = 1 if '-' in hr_match.groups(1) else 2
                yield ""
            continue

        state.where_from = "emit_normal"

        # if we've gotten to an emit normal then we can assert that our list stack should
        # be empty. This is a hack.
        state.list_item_stack = []

        if len(line) == 0: yield ""
        if len(line) < state.Width:
            # we want to prevent word wrap
            yield f"{state.space_left()}{line_format(line.lstrip())}"
        else:
            wrapped_lines = text_wrap(line)
            for wrapped_line in wrapped_lines:
                yield f"{state.space_left()}{wrapped_line}\n"

def emit(inp):
    buffer = []
    flush = False
    for chunk in parse(inp):
        width_calc()
        if state.emit_flag:
            if state.emit_flag == Code.Flush:
                flush = True
                state.emit_flag = None
            else:
                buffer[0] = emit_h(state.emit_flag, buffer[0])
                state.emit_flag = None
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
        # This *might* be dangerous
        state.reset_inline()

        if flush:
            chunk = "\n".join(buffer)
            buffer = []
            flush = False

        elif len(buffer) == 1:
            continue

        else:
            chunk = buffer.pop(0)

        print(chunk, end="", file=sys.stdout, flush=True)

    if len(buffer):
        print(buffer.pop(0), file=sys.stdout, end="", flush=True)

def ansi2hex(ansi_code):
    parts = ansi_code.strip('m').split(";")
    r, g, b = map(int, parts)
    return f"#{r:02x}{g:02x}{b:02x}"

def apply_multipliers(style, name, H, S, V):
    m = style.get(name)
    r, g, b = colorsys.hsv_to_rgb(min(1.0, H * m["H"]), min(1.0, S * m["S"]), min(1.0, V * m["V"]))
    return ';'.join([str(int(x * 256)) for x in [r, g, b]]) + "m"

def width_calc():
    if state.WidthArg:
       width = state.WidthArg
    else:
       try:
           width = shutil.get_terminal_size().columns
           state.WidthWrap = True
       except (AttributeError, OSError):
           width = 80
           pass


    # This can't be done because our list item stack can change as well so
    # unless we want to track that too, we're SOL
    #if state.WidthFull == width:
    #    return

    state.WidthFull = width

    state.Width = state.WidthFull - 2 * Style.Margin
    pre = state.space_left(listwidth=True) if Style.PrettyBroken else ''
    design  = [FG, '▄','▀'] if Style.PrettyPad else [BG, ' ',' ']
    Style.Codepad = [
        f"{pre}{RESET}{design[0]}{Style.Dark}{design[1] * state.full_width()}{RESET}\n",
        f"{pre}{RESET}{design[0]}{Style.Dark}{design[2] * state.full_width()}{RESET}"
    ]

def main():
    parser = ArgumentParser(description="Streamdown - A Streaming markdown renderer for modern terminals.  Latest version: https://github.com/day50-dev/Streamdown")
    parser.add_argument("filenameList", nargs="*", help="Input file to process (also takes stdin)")
    parser.add_argument("-l", "--loglevel", default="INFO", help="Set the logging level")
    parser.add_argument("-b", "--base", default=None, help="Set the hsv base: h,s,v")
    parser.add_argument("-c", "--config", default=None, help="Use a custom config")
    parser.add_argument("-w", "--width", default="0", help="Set the width WIDTH")
    parser.add_argument("-e", "--exec", help="Wrap a program EXEC for more 'proper' i/o handling")
    parser.add_argument("-s", "--scrape", help="Scrape code snippets to a directory SCRAPE")
    parser.add_argument("-v", "--version", action="store_true", help="Show version information")
    args = parser.parse_args()

    if args.version:
        try:
            import importlib.metadata
            print(importlib.metadata.version("streamdown"))
        except importlib.metadata.PackageNotFoundError:
            import subprocess
            print(subprocess.run(
                ['git', 'describe', '--always', '--dirty', '--tags'],
                cwd=os.path.dirname(os.path.abspath(__file__)),
                stdout=subprocess.PIPE,
                text=True
            ).stdout.strip())

        sys.exit(0)

    config_toml_path, config_toml_content = ensure_config_file(args.config)
    config = toml.loads(config_toml_content)
    style = toml.loads(default_toml).get('style') | config.get("style", {})
    features = toml.loads(default_toml).get('features') | config.get("features", {})
    H, S, V = style.get("HSV")

    if args.base:
        env_colors = args.base.split(",")
        if len(env_colors) > 0: H = float(env_colors[0])
        if len(env_colors) > 1: S = float(env_colors[1])
        if len(env_colors) > 2: V = float(env_colors[2])

    for color in ["Dark", "Mid", "Symbol", "Head", "Grey", "Bright"]:
        setattr(Style, color, apply_multipliers(style, color, H, S, V))
    for attr in ['PrettyPad', 'PrettyBroken', 'Margin', 'ListIndent', 'CodeIndent', 'Syntax']:
        setattr(Style, attr, style.get(attr))
    for attr in ['CodeSpaces', 'Clipboard', 'Logging', 'Timeout', 'Savebrace']:
        setattr(state, attr, features.get(attr))


    if args.scrape:
        os.makedirs(args.scrape, exist_ok=True)
        state.scrape = args.scrape

    Style.MarginSpaces = " " * Style.Margin
    state.WidthArg = int(args.width) or style.get("Width") or 0
    Style.Blockquote = f"{FG}{Style.Grey}│ "
    width_calc()

    Style.Codebg = f"{BG}{Style.Dark}"
    Style.Link = f"{FG}{Style.Symbol}{UNDERLINE[0]}"

    logging.basicConfig(stream=sys.stdout, level=args.loglevel.upper(), format=f'%(message)s')
    state.exec_master, state.exec_slave = pty.openpty()
    try:
        inp = sys.stdin
        if args.exec:
            state.terminal = termios.tcgetattr(sys.stdin)
            state.is_exec = True
            state.exec_sub = subprocess.Popen(args.exec.split(' '), stdin=state.exec_slave, stdout=state.exec_slave, stderr=state.exec_slave, close_fds=True)
            os.close(state.exec_slave)  # We don't need slave in parent
            # Set stdin to raw mode so we don't need to press enter
            tty.setcbreak(sys.stdin.fileno())
            sys.stdout.write("\x1b[?7h")
            emit(sys.stdin)

        elif args.filenameList:
            # Let's say we only care about logging in streams
            state.Logging = False
            for fname in args.filenameList:
                if len(args.filenameList) > 1:
                    emit(BytesIO(f"\n------\n# {fname}\n\n------\n".encode('utf-8')))
                emit(open(fname, "rb"))
                
        elif sys.stdin.isatty():
            parser.print_help()
            sys.exit()
        else:
            # this is a more sophisticated thing that we'll do in the main loop
            state.is_pty = True
            os.set_blocking(inp.fileno(), False) 
            emit(inp)

    except (OSError, KeyboardInterrupt):
        state.exit = 130
        
    except Exception as ex:
        if state.terminal:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, state.terminal)
        logging.warning(f"Exception thrown: {type(ex)} {ex}")
        traceback.print_exc()

    if state.Clipboard and state.code_buffer_raw:
        code = state.code_buffer_raw
        # code needs to be a base64 encoded string before emitting
        code_bytes = code.encode('utf-8')
        base64_bytes = base64.b64encode(code_bytes)
        base64_string = base64_bytes.decode('utf-8')
        print(f"\033]52;c;{base64_string}\a", end="", flush=True)

    if state.terminal:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, state.terminal)
        os.close(state.exec_master)
        if state.exec_sub:
            state.exec_sub.wait()

    print(RESET, end="")
    sys.exit(state.exit)

if __name__ == "__main__":
    main()
