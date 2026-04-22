# claude-usage

Parse local Claude Code logs and show how much your sessions actually cost.

No API calls. No cloud. Just reads the `.jsonl` files Claude Code already writes to `~/.claude/projects/`.

## Install

```bash
pipx install claude-usage
```

Or grab the single file:

```bash
curl -fsSL https://raw.githubusercontent.com/frahet/claude-usage/main/src/claude_usage/cli.py \
  -o /usr/local/bin/claude-usage && chmod +x /usr/local/bin/claude-usage
```

Zero dependencies — stdlib only. Python 3.10+.

## Usage

```bash
claude-usage                 # today's spend, by model and project
claude-usage --watch         # live tail: shows new API calls as they happen
claude-usage --days 7        # daily bar chart of the last N days
claude-usage --session abcd  # drill into one session (prefix match)
claude-usage --project tradebot   # filter by project slug substring
claude-usage --json          # raw output for piping
```

## What it reports

For each API call in the local jsonl logs:
- `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`
- Detects model family (opus / sonnet / haiku) from `message.model`
- Multiplies by list prices to give a USD estimate

## Caveats

- Cost is at **API list price**. If you're on Claude Max, most of your interactive usage is covered by the flat monthly fee — the reported number is still useful as a relative signal to find which sessions burn the most.
- Prices are hardcoded in `PRICES` at the top of `cli.py`. Bump them if Anthropic changes rates.
- Works wherever Claude Code writes to `~/.claude/projects/` — Mac, Linux, WSL.

## Why

Catching a runaway orchestrator in real time is worth more than any post-hoc dashboard. `--watch` in a second terminal shows you the bleed as it happens.

## License

MIT.
