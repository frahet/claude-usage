# claude-watch

Live dashboard + kill switch for your Claude Code spend. Parses the `.jsonl` session logs Claude Code already writes to `~/.claude/projects/` — no API calls, no cloud.

## Install

Single-file, zero dependencies, Python 3.10+:

```bash
sudo curl -fsSL https://raw.githubusercontent.com/frahet/claude-watch/main/src/claude_watch/cli.py \
  -o /usr/local/bin/claude-watch && sudo chmod +x /usr/local/bin/claude-watch
```

Or clone and symlink (so `git pull` picks up updates):

```bash
git clone https://github.com/frahet/claude-watch.git ~/Documents/projects/claude-watch
sudo ln -sf ~/Documents/projects/claude-watch/src/claude_watch/cli.py /usr/local/bin/claude-watch
```

(On Linux hosts without sudo, use `~/.local/bin` instead of `/usr/local/bin`.)

## Usage

```bash
claude-watch                            # today's spend, by model and project
claude-watch --watch                    # live dashboard
claude-watch --watch --max-today 20     # circuit breaker: auto-kill LIVE sessions if spend > $20
claude-watch --days 7                   # daily bar chart
claude-watch --session abcd             # drill into one session (prefix match)
claude-watch --project tradebot         # filter by project label
claude-watch --kill abcd                # SIGTERM the process writing session abcd
claude-watch --kill abcd --force        # SIGKILL instead
claude-watch --kill-live                # kill every LIVE session (< 60s activity)
claude-watch --json                     # machine-readable
claude-watch --version
```

## Live dashboard

`--watch` clears the terminal and redraws every 3 seconds:

```
claude-watch live   v0.4.0  2026-04-22 15:48:12  Franks-MacBook-Air
  today: $12.30    since watch: $0.23    burn: $2.40/hr    cap: $20.00

TOP SESSIONS TODAY  (LIVE < 60s · IDLE 1–10m · DONE > 10m)
    STATUS SESSION   PROJECT                 MODEL   CALLS  LAST        COST
  ● LIVE   5aeb9d11  tradebot-ops            opus      337  just now   $12.30
  ● IDLE   1a2b3c4d  forge-ferro-hetland     sonnet     12  4m ago      $0.45
    DONE   9f8e7d6c  ~                       opus      203  2h ago      $1.20

RECENT CALLS  (⚠ = cache_r > 200,000)
  ⚠ 15:48:02  5aeb9d11  opus    cache_r=291,200  out=  420   $0.48
    15:47:48  5aeb9d11  opus    cache_r= 87,400  out=  112   $0.15

tip: claude-watch --session 5aeb9d11 · claude-watch --kill 5aeb9d11 · ctrl-c to exit
```

### Meaning

- **STATUS** — LIVE (API call in last 60s), IDLE (1–10 min ago), DONE (>10 min)
- **burn** — extrapolated $/hr from the last 10 minutes of calls
- **⚠** on a row — that call read more than 200K cached tokens, which usually means context is bloating fast
- **cap** — only shown when `--max-today $N` is passed

## Circuit breaker

Leave this in a terminal during any autonomous / overnight run:

```bash
claude-watch --watch --max-today 20
```

When today's deduped spend exceeds $20, every LIVE session gets SIGTERM'd and the watch exits. That's the "never again" safeguard.

## Kill a runaway by hand

```bash
claude-watch --kill 5aeb9d11         # polite SIGTERM (lets Claude Code clean up)
claude-watch --kill 5aeb9d11 --force # SIGKILL when it won't stop
claude-watch --kill-live             # nuke everything currently firing
```

Kill works by finding the PID holding the session's `.jsonl` file open via `lsof`, then sending the signal. If the process has already exited, it's a no-op.

## How cost is calculated

Each assistant message in a jsonl carries a `message.usage` block. We multiply the four token counts (input, output, cache_write, cache_read) by list prices:

```
opus:   $15 / $75 / $18.75 / $1.50    per MTok
sonnet: $3  / $15 / $3.75  / $0.30
haiku:  $1  / $5  / $1.25  / $0.10
```

Calls are deduped by `message.id` — Claude Code writes the same call to the jsonl multiple times (streaming chunks, tool-use splits), but each should only count once.

## Caveats

- **Cost is at API list price.** If you're on Claude Max, most interactive usage is covered by the flat monthly fee — the reported number is still a useful relative signal for finding which sessions burn the most.
- **Per-machine.** `~/.claude/projects/` is local to each host. Mac sees Mac sessions; remote hosts see their own. Run `claude-watch --watch` over SSH in a `tmux` session to monitor a remote machine.
- **Prices are hardcoded** in `PRICES` at the top of `cli.py`. Bump them if Anthropic changes rates.

## License

MIT.
