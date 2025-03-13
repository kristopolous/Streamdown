#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.8"
# dependencies = [
#     "pygments",
# ]
# ///
import sys
import re
import shutil
from io import StringIO
from pygments import highlight
from pygments.lexers import get_lexer_by_name
from pygments.formatters import Terminal256Formatter
from pygments.styles import get_style_by_name


LEFT_INDENT = 2
LEFT_INDENT_SPACES = " " * LEFT_INDENT
SYMBOL = "175;130;230m"
BRIGHT = "222;195;254m"
DARK = "54;36;77m"
FG = "\033[38;2;"
BG = "\033[48;2;"
RESET = "\033[0m"

def get_terminal_width():
    try:
        return shutil.get_terminal_size().columns
    except (AttributeError, OSError):
        # Fallback to 80 columns
        return 80

WIDTH = int(get_terminal_width() * 11 / 12 - LEFT_INDENT)

def visible_length(s):
    """Calculates the length of a string without ANSI escape codes."""
    return len(re.sub(r"\033\[[0-9;]*[mK]", "", s))


def extract_ansi_codes(text):
    """Extracts all ANSI escape codes from a string."""
    return re.findall(r"\033\[[0-9;]*[mK]", text)


class TableState:
    def __init__(self):
        self.rows = []
        self.in_header = False
        self.in_separator = False
        self.in_body = False

    def reset(self):
        self.__init__()

    def intable(self):
        return self.in_header or self.in_separator or self.in_body


class ParseState:
    def __init__(self):
        self.in_code = False
        self.table = TableState()
        self.buffer = []
        # self.in_list_item = False
        # self.list_item_indent = 0
        self.list_item_stack = []  # stack of (indent, type)
        self.first_line = True
        self.last_line_empty = False
        self.code_buffer = []
        self.code_language = None
        self.code_first_line = False
        self.code_indent = 0
        self.ordered_list_numbers = []

    def debug(self):
        """print out every variable in ParseState and TableState"""
        for key, value in self.__dict__.items():
            print(f"{key:20}: {value}")
        for key, value in self.table.__dict__.items():
            print(f"table.{key:20}: {value}")

    def reset_buffer(self):
        self.buffer = []
        # self.code_buffer = []


def format_table(table_rows):
    """Formats markdown tables with unicode borders and alternating row colors"""
    if not table_rows:
        return []

    # Extract headers and rows, skipping separator
    headers = [cell.strip() for cell in table_rows[0]]
    rows = [
        [cell.strip() for cell in row]
        for row in table_rows[1:]
        if not re.match(r"^[\s|:-]+$", "|".join(row))
    ]

    # Calculate column widths
    col_widths = [
        max(len(str(item)) for item in col) + 2  # +2 for padding
        for col in zip(headers, *rows)
    ]

    formatted = []

    # Header row
    header_cells = [
        f"\033[1m {header.ljust(width)} \033[22m"
        for header, width in zip(headers, col_widths)
    ]
    header_line = "│".join(header_cells)
    formatted.append(f" \033[48;2;29;0;49m{header_line}\033[0m")

    # Data rows
    for i, row in enumerate(rows):
        color = 236 if i % 2 == 0 else 238
        row_cells = [f" {cell.ljust(width)} " for cell, width in zip(row, col_widths)]
        row_line = "│".join(row_cells)
        formatted.append(f" \033[48;2;19;0;30m{row_line}\033[0m")
    return formatted


def wrap_text(text, width = WIDTH, indent = 0, first_line_prefix="", subsequent_line_prefix=""):
    """
    Wraps text to the given width, preserving ANSI escape codes across lines.
    """
    words = text.split()
    lines = []
    current_line = ""
    current_style = ""

    for i, word in enumerate(words):
        # Accumulate ANSI codes within the current word
        codes = extract_ansi_codes(word)
        if codes:
            current_style += "".join(codes)

        if (
            visible_length(current_line) + visible_length(word) + 1 <= width
        ):  # +1 for space
            current_line += (" " if current_line else "") + word
        else:
            # Close previous line with reset and then re-apply current style
            if not lines:  # First line
                lines.append(first_line_prefix + current_line + "\033[0m")
            else:
                lines.append(subsequent_line_prefix + current_line + "\033[0m")

            # Reset current line and apply the preserved style
            current_line = (" " * indent) + current_style + word

    if current_line:
        if not lines:  # only one line
            lines.append(first_line_prefix + current_line + "\033[0m")
        else:
            lines.append(subsequent_line_prefix + current_line + "\033[0m")

    # Re-apply current style to the beginning of each line (except the first)
    final_lines = []
    for i, line in enumerate(lines):
        if i == 0:
            final_lines.append(line)
        else:
            final_lines.append(current_style + line)

    return final_lines


def parse(input_source, style_name="monokai"):
    """Parse markdown with robust table state tracking"""

    if isinstance(input_source, str):
        stdin = StringIO(input_source)
    else:
        stdin = input_source

    state = ParseState()
    try:
        while True:
            char = stdin.read(1)
            if not char:
                break

            state.buffer.append(char)

            if char != "\n":
                continue

            # Process complete line
            line = "".join(state.buffer).rstrip("\n")
            state.reset_buffer()

            # --- Collapse Multiple Empty Lines ---
            is_empty = line.strip() == ""

            if is_empty and state.last_line_empty:
                continue  # Skip processing this line
            elif is_empty:
                state.last_line_empty = True
            else:
                state.last_line_empty = False

            # <code>
            if state.in_code:
                background = "\033[48;2;36;0;26m"
                width = int(get_terminal_width() * 10 / 12)

                if state.code_first_line:
                    state.code_first_line = False
                    for i, char in enumerate(line):
                        if char == " ":
                            state.code_indent += 1
                        else:
                            break
                    line = line[state.code_indent :]

                else:
                    # Dedent subsequent lines
                    if line.startswith(" " * state.code_indent):
                        line = line[state.code_indent :]

                if line.strip() == "```":
                    state.in_code = False
                    # Process code buffer with Pygments if language is specified
                    if state.code_language:
                        try:
                            lexer = get_lexer_by_name(state.code_language)
                            # Create a custom style with a dark fuchsia background
                            try:
                                custom_style = get_style_by_name(style_name)
                            except pygments.util.ClassNotFound:
                                print(
                                    f"Warning: Style '{style_name}' not found. Using default style.",
                                    file=sys.stderr,
                                )
                                custom_style = get_style_by_name("default")
                                print(f"Using Pygments style: default", file=sys.stderr)

                            custom_style.background_color = "#1c021d"
                            formatter = Terminal256Formatter(style=custom_style)
                            highlighted_code = highlight(
                                "".join(state.code_buffer), lexer, formatter
                            )
                            # Take the highlighted code and split it into lines.
                            # Yield each individual line
                            yield f"{LEFT_INDENT_SPACES}{background}  {' ' * (width)} {RESET}\n"

                            for code_line in highlighted_code.split("\n"):
                                # Wrap the code line in a very dark fuschia background, padding to terminal width
                                padding = width - visible_length(code_line)
                                yield f"{LEFT_INDENT_SPACES}{background}  {code_line}{' ' * max(0, padding)} \033[0m\n"

                        except pygments.util.ClassNotFound as e:
                            print(f"Error: Lexer for language '{state.code_language}' not found.", file=sys.stderr)
                        except Exception as e:
                            # Improve error handling: print to stderr and include traceback
                            print(f"Error highlighting: {e}", file=sys.stderr)
                            import traceback

                            traceback.print_exc()
                    state.code_language = None
                    state.code_buffer = []
                    state.code_indent = 0
                else:
                    state.code_buffer.append(line + "\n")
                continue

            code_match = re.match(r"```([\w+-]*)", line.strip())
            if code_match:
                state.in_code = True
                state.code_first_line = True
                state.code_language = code_match.group(1)
                continue

            # --- Inline formatting ---
            line = re.sub(r"\*\*(.+?)\*\*", r"\033[1m\1\033[0m", line)  # Bold
            line = re.sub(r"\*(.+?)\*", r"\033[3m\1\033[0m", line)  # Italic
            line = re.sub(r"_(.+?)_", r"\033[4m\1\033[0m", line)  # Underline
            line = re.sub(
                r"`(.+?)`", r"\033[48;2;49;0;85m \1 \033[0m", line
            )  # Inline code

            # Table state machine <table>
            if re.match(r"^\s*\|.+\|\s*$", line) and not state.in_code:
                if not state.table.in_header and not state.table.in_body:
                    state.table.in_header = True

                cells = [c.strip() for c in line.strip().strip("|").split("|")]

                if state.table.in_header:
                    if re.match(r"^[\s|:-]+$", line):
                        state.table.in_header = False
                        state.table.in_separator = True
                    else:
                        state.table.rows.append(cells)
                elif state.table.in_separator:
                    state.table.in_separator = False
                    state.table.in_body = True
                    state.table.rows.append(cells)
                elif state.table.in_body:
                    state.table.rows.append(cells)

                if not state.table.intable():
                    yield f"{line}\n"
                continue
            else:
                if state.table.in_body or state.table.in_header:
                    formatted = format_table(state.table.rows)
                    for l in formatted:
                        yield f" {l}\n"
                    state.table.reset()

                # --- List Items --- <li> <ul> <ol>
                list_item_match = re.match(r"^(\s*)([*\-]|\d+\.)\s+(.*)", line)
                if list_item_match:
                    indent = len(list_item_match.group(1))
                    list_type = (
                        "number" if list_item_match.group(2)[0].isdigit() else "bullet"
                    )
                    content = list_item_match.group(3)

                    # Handle stack
                    # print(f"Before stack handling: indent={indent}, list_type={list_type}, content={content.strip()}, stack={state.list_item_stack}, numbers={state.ordered_list_numbers}", file=sys.stderr)
                    while (
                        state.list_item_stack and state.list_item_stack[-1][0] > indent
                    ):
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
                        # print(json.dumps([indent, state.ordered_list_numbers]))
                        state.ordered_list_numbers[-1] += 1

                    wrap_width = WIDTH - indent - 4

                    if list_type == "number":
                        list_number = state.ordered_list_numbers[-1]
                        bullet = f"{list_number}"
                        first_line_prefix = (
                            " " * (indent - len(bullet))
                            + f"\033[38;2;{SYMBOL}{bullet}\033[0m"
                            + " "
                        )
                        subsequent_line_prefix = " " * indent
                    else:
                        first_line_prefix = (
                            " " * (indent - 1) + f"\033[38;2;{SYMBOL}•\033[0m" + " "
                        )
                        subsequent_line_prefix = " " * indent

                    wrapped_lines = wrap_text(
                        content,
                        wrap_width,
                        2,
                        first_line_prefix,
                        subsequent_line_prefix,
                    )
                    for wrapped_line in wrapped_lines:
                        yield f"{LEFT_INDENT_SPACES}{wrapped_line}\n"
                    continue

                # Header processing
                header_match = re.match(r"^(#{1,6})\s+(.*)", line)
                if header_match:
                    level = len(header_match.group(1))
                    text = header_match.group(2)
                    spaces_to_center = ' ' * ((WIDTH - visible_length(text)) // 2)
                    if level == 1:
                        yield f"{LEFT_INDENT_SPACES}{BG}{DARK}{spaces_to_center}{text}{spaces_to_center}\033[0m\n"  # Midnight Blue background, light text
                    elif level == 2:
                        yield f"{LEFT_INDENT_SPACES}{FG}{SYMBOL}▌ {FG}{BRIGHT}{text} \n"  # Lighter Indigo
                    elif level == 3:
                        yield f"{LEFT_INDENT_SPACES}{FG}{BRIGHT}{text} {RESET}\n"  # More desaturated Indigo
                    elif level == 4:
                        yield f"{LEFT_INDENT_SPACES}{FG}{SYMBOL}{text} {RESET}\n"  # Even more desaturated Indigo
                    elif level == 5:
                        yield f"{LEFT_INDENT_SPACES}{text} {RESET}\n"  
                    else:  # level == 6
                        yield f"{LEFT_INDENT_SPACES}{text} {RESET}\n"  

                else:
                    # Horizontal rule
                    if re.match(r"^[\s]*[-*_]{3,}[\s]*$", line):
                        # print a horizontal rule using a unicode midline with a unicode fleur de lis in the middle
                        yield f"{LEFT_INDENT_SPACES}{FG}{SYMBOL}{'─' * WIDTH}{RESET}\n"
                    else:
                        if len(line) == 0:
                            print("")
                        else:
                            # This is the basic unformatted text. We still want to word wrap it.
                            wrapped_lines = wrap_text(line)
                            for wrapped_line in wrapped_lines:
                                yield f"{LEFT_INDENT_SPACES}{wrapped_line}\n" 
                            #yield f"{LEFT_INDENT_SPACES}{line}\n"

                # Process any remaining table data
                if state.table.rows:
                    formatted = format_table(state.table.rows)
                    for l in formatted:
                        yield f"{l}\n"
                    state.table.reset()

    except Exception as e:
        print(f"Parser error: {str(e)}", file=sys.stderr)
        raise


help_text = """
# mdstream

A markdown renderer for the terminal.

## Usage

```bash
mdstream [filename]
```

Or, pipe markdown to stdin:

```bash
cat README.md | mdstream
```

If no filename is provided and no input is piped, this help message is displayed.

## Features

- Supports basic markdown formatting (headers, bold, italic, lists, tables, code blocks).
- Uses Pygments for syntax highlighting.
- Falls back to a default style if the specified Pygments style is not found.
"""


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1:
            # File argument provided
            try:
                with open(sys.argv[1], "r") as f:
                    for chunk in parse(f, style_name="monokai"):
                        sys.stdout.write(chunk)
                        sys.stdout.flush()
            except FileNotFoundError:
                print(f"Error: File not found: {sys.argv[1]}", file=sys.stderr)
        else:
            # No file argument, check stdin
            if sys.stdin.isatty():
                # No input piped, print help
                for chunk in parse(help_text, style_name="monokai"):
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
            else:
                # Input piped, process stdin
                for chunk in parse(sys.stdin, style_name="monokai"):
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
    except KeyboardInterrupt:
        pass
