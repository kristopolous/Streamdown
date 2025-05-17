<p align="center">
<img src=https://github.com/user-attachments/assets/0468eac0-2a00-4e98-82ca-09e6ac679357/>
<br/>
<a href=https://pypi.org/project/streamdown><img src=https://badge.fury.io/py/streamdown.svg/></a>
<br/><strong>Terminal streaming markdown that rocks</strong>

</p>


Streamdown works with any streaming markdown such as [simonw's llm](https://github.com/simonw/llm) or even something basic like curl. 

It is designed for AI and can be used to do parser based sophisticated pipelines and routing, cracking open various monolithic AI solutions to permit them to integrate. Think of it as output level routing at the semantic level.

You can also just use it like a normal person.

It supports standard piping and files as arguments like any normal pager but can also run as a wrapper so you retain full keyboard interactivity. Arrow keys, control, alt, all still work.
```bash
$ pip install streamdown
```
![Streamdown is Amazing](https://github.com/user-attachments/assets/268cb340-78cc-4df0-a773-c5ac95eceeeb)

### Provides clean copyable code for long code lines
Other renderers inject line breaks when copying code that wraps around. Streamdown's better and now you are too!
![Handle That Mandle](https://github.com/user-attachments/assets/a27aa70c-f691-4796-84f0-c2eb18c7de23)
**Tip**: You can make things prettier if you don't mind if this guarantee is broken. See the `PrettyBroken` flag below! (There's still 2 other convenient ways of getting code blocks out.)

### Supports images
Here's kitty and alacritty. 
![doggie](https://github.com/user-attachments/assets/81c43983-68cd-40c1-b1d5-aa3a52004504)

### Supports hyperlinks (OSC 8) and clipboard (OSC 52)
The optional `Clipboard` feature puts the final codeblock into your clipboard. See below for details.

[links.webm](https://github.com/user-attachments/assets/a5f71791-7c58-4183-ad3b-309f470c08a3)

### As well as everything else...
Here's the `Savebrace` feature with `screen-query` and `sq-picker` from [llmehelp](https://github.com/kristopolous/llmehelp). You can have an ongoing conversation in tmux with your terminal session. Then use popups and fzf to insert command or coding blocks all with a keystroke.

This allows you to interactively debug  in a way that the agent doesn't just wander off doing silly things.

It takes about 2 minutes to set up and about 0.2s to use. Fast, fluid and free.
![screenquery](https://github.com/user-attachments/assets/517be4fe-6962-4e4c-b2f2-563471bc48d0)

### ...It even supports CJK 
Compare how streamdown wraps and spaces this tabular Chinese description of programming languages to other leading markdown renderers.

Only one generates the text without truncation. 很美！
![cjk](https://github.com/user-attachments/assets/cae485d7-c478-4836-9732-d9fa49e13bc9)

### Colors are highly (and quickly) configurable for people who care a lot, or just a little.
![configurable](https://github.com/user-attachments/assets/19ca2ec9-8ea1-4a79-87ca-8352789269fe)

### Has a [Plugin](https://github.com/kristopolous/Streamdown/tree/main/streamdown/plugins) system to extend the parser and renderers.
For instance, here is the [latex plugin](https://github.com/kristopolous/Streamdown/blob/main/streamdown/plugins/latex.py) doing math inside a table:
![calc](https://github.com/user-attachments/assets/0b0027ca-8ef0-4b4a-b4ae-e36ff623a683)


## Configuration 

It's located at `~/.config/streamdown/config.toml` (following the XDG Base Directory Specification). If this file does not exist upon first run, it will be created with default values. 

Here are the sections:

**`[style]`**

Defines the base Hue (H), Saturation (S), and Value (V) from which all other palette colors are derived. This can also be specified at runtime via command line arguments. See below! 

The default values are [at the beginning of the source](https://github.com/kristopolous/Streamdown/blob/main/streamdown/sd.py#L33).

*   `HSV`: [ 0.0 - 1.0, 0.0 - 1.0, 0.0 - 1.0 ] 
*   `Dark`: Multipliers for background elements, code blocks. 
*   `Grey`: Multipliers for blockquote and thinkblock. 
*   `Mid`: Multipliers for inline code backgrounds, table headers. 
*   `Symbol`: Multipliers for list bullets, horizontal rules, links. 
*   `Head`: Multipliers for level 3 headers. 
*   `Bright`: Multipliers for level 2 headers. 
*   `Margin` (integer, default: `2`): The left and right indent for the output. 
*   `Width` (integer, default: `0`): Along with the `Margin`, `Width` specifies the base width of the content, which when set to 0, means use the terminal width. See [#6](https://github.com/kristopolous/Streamdown/issues/6) for more details
*   `PrettyPad` (boolean, default: `false`): Uses a unicode vertical pad trick to add a half height background to code blocks. This makes copy/paste have artifacts. See [#2](https://github.com/kristopolous/Streamdown/issues/2). I like it on. But that's just me
*   `PrettyBroken` (boolean, default: `false`): This will break the copy/paste assurance above. The output is much prettier, but it's also broken. So it's pretty broken. Works nicely with PrettyPad.
*   `ListIndent` (integer, default: `2`): This is the recursive indent for the list styles.
*   `CodeIndent` (integer, default: `2`): This is the indent for code blocks.
*   `Syntax` (string, default `monokai`): This is the syntax [highlighting theme which come via pygments](https://pygments.org/styles/).

Example:
```toml
[style]
PrettyPad = true
PrettyBroken = true
HSV = [0.7, 0.5, 0.5]
Dark = { H = 1.0, S = 1.2, V = 0.25 } # Make dark elements less saturated and darker
Symbol = { H = 1.0, S = 1.8, V = 1.8 } # Make symbols more vibrant
```

**`[features]`**

Controls optional features:

*   `CodeSpaces` (boolean, default: `true`): Enables detection of code blocks indented with 4 spaces. Set to `false` to disable this detection method (triple-backtick blocks still work).
*   `Clipboard` (boolean, default: `true`): Enables copying the last code block encountered to the system clipboard using OSC 52 escape sequences upon exit. Set to `false` to disable.
*   `Logging` (boolean, default: `false`): Enables logging to tmpdir (/tmp/sd) of the raw markdown for debugging and bug reporting. The logging uses an emoji as a record separator so the actual streaming delays can be simulated and replayed. If you use the `filename` based invocation, that is to say, `sd <filename>`, this type of logging is always off.
*   `Savebrace` (boolean, default: `true`): Saves the code blocks of a conversation to the append file `/tmp/sd/savebrace` so you can fzf or whatever you want through it. See how it's used in my [llmehelp](https://github.com/kristopolous/llmehelp) scripts, specifically `screen-query` and `sd-picker`.

Example:
```toml
[features]
CodeSpaces = false
Clipboard = false
```

## Command Line
The most exciting feature here is `--exec` with it you can do full readline support like this:

```shell
$ sd --exec "llm chat"
```

And now you have all your readline stuff. It's pretty great. (Also see the Day50 shellwrap project.)

It's also worth noting that things like the `-c` aren't "broken" with regard to file input. You can do something like this:

```shell
$ sd -c <(echo "[style]\nMargin=10") 
```

To override the margin.

```shell
usage: sd [-h] [-l LOGLEVEL] [-b BASE] [-c CONFIG] [-w WIDTH] [-e EXEC]
          [-s SCRAPE] [filenameList ...]

Streamdown - A Streaming markdown renderer for modern terminals

positional arguments:
  filenameList          Input file to process (also takes stdin)

optional arguments:
  -h, --help            show this help message and exit
  -l LOGLEVEL, --loglevel LOGLEVEL
                        Set the logging level
  -b BASE, --base BASE  Set the hsv base: h,s,v
  -c CONFIG, --config CONFIG
                        Use a custom config
  -w WIDTH, --width WIDTH
                        Set the width WIDTH
  -e EXEC, --exec EXEC  Wrap a program EXEC for more 'proper' i/o handling
  -s SCRAPE, --scrape SCRAPE
                        Scrape code snippets to a directory SCRAPE
```

## Demo
Do this

    $ ./streamdown/sd.py tests/*md

## Install from source
After the git clone least one of these should work, hopefully. it's using the modern uv pip tool but is also backwards compatible to the `pip3 install -r requirements.txt` flow.

    $ pipx install -e .
    $ pip install -e .
    $ uv pip install -e . 

