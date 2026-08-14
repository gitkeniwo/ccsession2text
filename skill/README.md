# ccsession2text skill

An [Agent Skills](https://opencode.ai/docs/skills/)-compatible skill that
ingests a Claude Code session export (`.zip` / `.jsonl` / export directory)
into the current chat as prior conversation history, so you can pick up
where that session left off:

- `skill/ccsession2text/SKILL.md` - the skill definition

The skill works with any agent that supports `SKILL.md` skills, including
[opencode](https://opencode.ai/docs/skills/), Claude Code, and other
`~/.agents/skills/` consumers.

## Prerequisite

[ccsession2text.py](../README.md) must be installed and on your `PATH` (see
the [Install](../README.md#install) section of the main README). The skill
tells the agent how to detect and report a missing installation.

## Install

Pick one method. After installing, restart your agent so it picks up the new
skill.

### Method 1: Tell your agent to install it

Paste this into your agent (replacing the target directory with the one for
your agent):

```
Install the ccsession2text skill for me:

1. Download https://raw.githubusercontent.com/gitkeniwo/ccsession2text/main/skill/ccsession2text/SKILL.md
2. Save it to ~/.config/opencode/skills/ccsession2text/SKILL.md
   (Claude Code: ~/.claude/skills/ccsession2text/SKILL.md;
   other agents: ~/.agents/skills/ccsession2text/SKILL.md)
3. Verify the file was written correctly
```

The agent downloads the file and places it in your agent's skill directory.

### Method 2: One-liner (macOS / Linux)

```bash
mkdir -p ~/.config/opencode/skills/ccsession2text
curl -fsSL https://raw.githubusercontent.com/gitkeniwo/ccsession2text/main/skill/ccsession2text/SKILL.md \
  -o ~/.config/opencode/skills/ccsession2text/SKILL.md
```

For Claude Code, use `~/.claude/skills/ccsession2text/SKILL.md` instead.

### Method 3: Point opencode at a checkout

Clone the repo once and register the skill directory in `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "skills": { "paths": ["~/ws/ccsession2text/skill"] }
}
```

This stays in sync with the repo automatically.

## Usage

Once installed, you don't invoke the skill by name - the agent loads it
automatically when it sees a session-export path in your message.

Just paste the path and start talking. No "continue this chat" preamble is
needed - the pasted path is the trigger.

1. Paste the path to the export (`.zip`, `.jsonl`, or export directory)
2. Go straight into your instruction

The agent then: converts the export with `ccsession2text.py`, reads the
resulting markdown into context, and answers as if that session was the
conversation history.

### Example (path made up - use your own)

> ~/Downloads/session-export-1234567890123.zip 现在请你先替我讲解下最基本的入门 process mining 的概念

The agent picks up the past session's context (its cwd, what was discussed,
what files were changed) and starts answering your question right away,
without re-explaining the history. After that, you can simply continue asking
follow-up questions as usual. Pasting only the path with no instruction also
works - the agent ingests it and asks what you want to do.

### Tips

- Keep the export path and your instruction in the same message so the agent
  has everything it needs in one go.
- Huge sessions can be condensed harder for fewer tokens: ask for
  `--level minimal` (or `--thinking` if you want reasoning blocks kept).
- If the agent reports "ccsession2text.py is missing from PATH", install it
  first (see the [main README](../README.md#install)).
- The export is a static snapshot - later changes to the original project are
  not included.