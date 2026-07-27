# ccsession2text

Turn a Claude Code session export into compact Markdown for continuing work in a
new chat. It keeps the conversation, tool calls, and code-change summary while
dropping images, hook logs, skill listings, and other low-signal export data.

The input can be an export directory, a `.jsonl` transcript, or a `.zip`
archive.

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)

The script declares its `typer` dependency with PEP 723 metadata. `uv` creates
the required environment automatically on first run.

## Install

Copy the executable script into a directory on your `PATH`:

```bash
mkdir -p ~/.local/bin
cp ccsession2text.py ~/.local/bin/
chmod +x ~/.local/bin/ccsession2text.py
export PATH="$HOME/.local/bin:$PATH"
```

Add the `export PATH=...` line to your shell startup file if `~/.local/bin` is
not already on your `PATH`.

## Usage

After installation, invoke the script directly—no `uv run` prefix is needed:

```bash
ccsession2text.py ./exports/session-export.zip
```

By default, the Markdown file is written beside the input:

```text
./exports/session-export.md
```

Choose another destination, write to standard output, or control the amount of
detail:

```bash
ccsession2text.py ./exports/session-export.zip -o context.md
ccsession2text.py ./exports/session-export.zip -o -
ccsession2text.py ./exports/session-export.zip --level minimal
```

## Options

```text
--level minimal|balanced|full  Condensation level (default: balanced)
--thinking                     Include thinking blocks
--stats                        Print parsing and compression statistics
-o, --out PATH                 Output path; use - for stdout
```

Run `ccsession2text.py --help` for the complete CLI reference.
