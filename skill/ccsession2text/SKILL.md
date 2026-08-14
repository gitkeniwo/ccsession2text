---
name: ccsession2text
description: Automatically ingest a Claude Code session export that the user pastes into a message (a .zip / .jsonl / export-directory path, e.g. "session-export-*.zip") into this chat as prior conversation history - no explicit request needed. Use whenever the user's message contains a session-export path, or says "ccsession2text" / "continue this session" / "接着这个对话". Pasted path counts as the trigger even without a "continue" request.
---

# Continue a Claude Code session in this chat

1. Extract to markdown:
   `ccsession2text.py "<path>" -o /tmp/ccsession2text.md`
   - Append `--level minimal|full` or `--thinking` only if the user asks (default balanced)
   - If `command not found`: ccsession2text.py is missing from PATH - tell the user to install it
2. Read the whole file into context - if the read is truncated, keep reading with increasing `offset` until the end.
3. The rest of the user's message is the first follow-up of that session: treat the extracted markdown as its history and answer directly - do not summarize the session first. If the user pasted only a path with no instruction, ingest it and ask what they want to do.