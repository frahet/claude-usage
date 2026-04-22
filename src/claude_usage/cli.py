#!/usr/bin/env python3
"""claude-usage — parse local Claude Code logs, show token spend.

Reads ~/.claude/projects/**/*.jsonl on this machine and computes USD cost
per session/project/model/day. Stdlib only, no external deps.

Usage:
  claude-usage                 # today
  claude-usage --watch         # live tail
  claude-usage --days 7        # last N days
  claude-usage --session XXX   # drill into one session
  claude-usage --project NAME  # filter by project dir
  claude-usage --json          # machine-readable
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Per-million-token prices (USD). Update if Anthropic changes them.
PRICES = {
    "opus":   {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
    "sonnet": {"input":  3.00, "output": 15.00, "cache_write":  3.75, "cache_read": 0.30},
    "haiku":  {"input":  1.00, "output":  5.00, "cache_write":  1.25, "cache_read": 0.10},
}

BOLD = "\x1b[1m"
DIM = "\x1b[2m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
CYAN = "\x1b[36m"
RESET = "\x1b[0m"


def model_family(model: str) -> str:
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    return "sonnet"


def cost_of(usage: dict, model: str) -> float:
    p = PRICES[model_family(model)]
    return (
        usage.get("input_tokens", 0) * p["input"] / 1_000_000
        + usage.get("output_tokens", 0) * p["output"] / 1_000_000
        + usage.get("cache_creation_input_tokens", 0) * p["cache_write"] / 1_000_000
        + usage.get("cache_read_input_tokens", 0) * p["cache_read"] / 1_000_000
    )


def project_slug(proj_dir: Path) -> str:
    # e.g. "-Users-frank-Documents-projects-tradebot-ops" -> "tradebot-ops"
    name = proj_dir.name
    if name.startswith("-"):
        parts = name.lstrip("-").split("-")
        if "projects" in parts:
            idx = parts.index("projects")
            return "-".join(parts[idx + 1:]) or name
    return name


def iter_lines(path: Path, start: int = 0):
    try:
        with open(path, "rb") as f:
            f.seek(start)
            for raw in f:
                try:
                    yield json.loads(raw.decode("utf-8", errors="ignore"))
                except Exception:
                    continue
    except FileNotFoundError:
        return


def scan(since: date | None = None, project_filter: str | None = None):
    """Yield (when, project, session, model, usage, cost_usd) across all jsonl files."""
    if not PROJECTS_DIR.exists():
        return
    cutoff_ts = datetime.combine(since, datetime.min.time()).timestamp() if since else 0
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        proj = project_slug(proj_dir)
        if project_filter and project_filter not in proj:
            continue
        for jl in proj_dir.glob("*.jsonl"):
            try:
                if since and jl.stat().st_mtime < cutoff_ts:
                    continue
            except FileNotFoundError:
                continue
            sess = jl.stem
            for obj in iter_lines(jl):
                msg = obj.get("message") or {}
                usage = msg.get("usage")
                if not usage:
                    continue
                model = msg.get("model", "unknown")
                ts = obj.get("timestamp") or ""
                try:
                    when = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
                except Exception:
                    when = None
                yield when, proj, sess, model, usage, cost_of(usage, model)


def fmt_usd(x: float) -> str:
    return f"${x:,.2f}"


def color_for(cost: float, low: float = 1.0, mid: float = 5.0) -> str:
    return GREEN if cost < low else YELLOW if cost < mid else RED


def cmd_today(args) -> None:
    today = date.today()
    rows = [r for r in scan(since=today, project_filter=args.project) if r[0] and r[0].date() == today]
    if not rows:
        print(f"{DIM}no Claude activity today.{RESET}")
        return
    total = sum(r[5] for r in rows)
    by_sess: dict[str, float] = defaultdict(float)
    by_proj: dict[str, float] = defaultdict(float)
    by_model: dict[str, float] = defaultdict(float)
    sess_proj: dict[str, str] = {}
    for when, proj, sess, model, _usage, c in rows:
        by_sess[sess] += c
        by_proj[proj] += c
        by_model[model_family(model)] += c
        sess_proj[sess] = proj

    print(f"{BOLD}claude-usage{RESET}  {today.isoformat()}  ({os.uname().nodename})")
    col = color_for(total, 5, 15)
    print(f"  total today: {col}{fmt_usd(total)}{RESET}  across {len(rows):,} API calls\n")

    print(f"{BOLD}by model{RESET}")
    for m, c in sorted(by_model.items(), key=lambda kv: -kv[1]):
        print(f"  {m:<8} {fmt_usd(c)}")

    print(f"\n{BOLD}by project{RESET}")
    for p, c in sorted(by_proj.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {p[:50]:<50} {fmt_usd(c)}")

    print(f"\n{BOLD}top sessions{RESET}")
    for sess, c in sorted(by_sess.items(), key=lambda kv: -kv[1])[:5]:
        print(f"  {sess[:8]}  {sess_proj[sess][:40]:<40}  {fmt_usd(c)}")


def cmd_watch(args) -> None:
    today = date.today()
    # Seed: total today so far, offsets = current file sizes.
    offsets: dict[Path, int] = {}
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for jl in proj_dir.glob("*.jsonl"):
            try:
                offsets[jl] = jl.stat().st_size
            except FileNotFoundError:
                continue
    today_total = sum(
        c for when, _p, _s, _m, _u, c in scan(since=today, project_filter=args.project)
        if when and when.date() == today
    )

    print(f"{DIM}watching {PROJECTS_DIR} — ctrl-c to exit{RESET}")
    col = color_for(today_total, 5, 15)
    sys.stdout.write(f"\r{BOLD}today: {col}{fmt_usd(today_total)}{RESET}")
    sys.stdout.flush()

    try:
        while True:
            new_calls = []
            for proj_dir in PROJECTS_DIR.iterdir():
                if not proj_dir.is_dir():
                    continue
                proj = project_slug(proj_dir)
                if args.project and args.project not in proj:
                    continue
                for jl in proj_dir.glob("*.jsonl"):
                    try:
                        size = jl.stat().st_size
                    except FileNotFoundError:
                        continue
                    prev = offsets.get(jl, size)
                    if size <= prev:
                        offsets[jl] = size
                        continue
                    for obj in iter_lines(jl, prev):
                        msg = obj.get("message") or {}
                        usage = msg.get("usage")
                        if not usage:
                            continue
                        model = msg.get("model", "unknown")
                        c = cost_of(usage, model)
                        today_total += c
                        new_calls.append((proj, jl.stem[:8], model_family(model), c))
                    offsets[jl] = size

            if new_calls:
                sys.stdout.write("\r" + " " * 80 + "\r")
                ts = datetime.now().strftime("%H:%M:%S")
                for proj, sess, m, c in new_calls:
                    call_col = GREEN if c < 0.05 else YELLOW if c < 0.25 else RED
                    print(f"{DIM}{ts}{RESET}  {proj[:25]:<25} {sess}  {m:<6}  {call_col}{fmt_usd(c)}{RESET}")

            col = color_for(today_total, 5, 15)
            sys.stdout.write(f"\r{BOLD}today: {col}{fmt_usd(today_total)}{RESET}  ")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()


def cmd_days(args) -> None:
    since = date.today() - timedelta(days=args.days - 1)
    by_day: dict[date, float] = defaultdict(float)
    total = 0.0
    for when, _proj, _sess, _model, _usage, c in scan(since=since, project_filter=args.project):
        if when is None:
            continue
        d = when.date()
        if d < since:
            continue
        by_day[d] += c
        total += c

    if not by_day:
        print(f"{DIM}no activity in the last {args.days} days.{RESET}")
        return

    print(f"{BOLD}last {args.days} days{RESET}  total {fmt_usd(total)}\n")
    peak = max(by_day.values())
    d = since
    while d <= date.today():
        c = by_day.get(d, 0.0)
        bar_len = int(50 * c / peak) if peak > 0 else 0
        col = color_for(c, 5, 15)
        print(f"  {d.isoformat()}  {col}{fmt_usd(c):>9}{RESET}  {'█' * bar_len}")
        d += timedelta(days=1)


def cmd_session(args) -> None:
    match = None
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for jl in proj_dir.glob(f"{args.session}*.jsonl"):
            match = (proj_dir, jl)
            break
        if match:
            break
    if not match:
        print(f"no session found matching '{args.session}'", file=sys.stderr)
        sys.exit(1)
    proj_dir, jl = match
    proj = project_slug(proj_dir)
    print(f"{BOLD}session {jl.stem}{RESET}  project: {proj}\n")
    total = 0.0
    calls = 0
    for obj in iter_lines(jl):
        msg = obj.get("message") or {}
        usage = msg.get("usage")
        if not usage:
            continue
        model = msg.get("model", "unknown")
        ts = obj.get("timestamp") or ""
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%H:%M:%S")
        except Exception:
            t = "??:??:??"
        c = cost_of(usage, model)
        total += c
        calls += 1
        inp = usage.get("input_tokens", 0)
        cr = usage.get("cache_read_input_tokens", 0)
        cw = usage.get("cache_creation_input_tokens", 0)
        out = usage.get("output_tokens", 0)
        call_col = GREEN if c < 0.05 else YELLOW if c < 0.25 else RED
        print(f"  {DIM}{t}{RESET}  {model_family(model):<6}  in={inp:>6,}  cache_r={cr:>9,}  cache_w={cw:>7,}  out={out:>6,}  {call_col}{fmt_usd(c)}{RESET}")
    col = color_for(total, 1, 3)
    print(f"\n{BOLD}total {col}{fmt_usd(total)}{RESET} across {calls} API calls")


def cmd_json(args) -> None:
    since = date.today() - timedelta(days=args.days - 1) if args.days else None
    out = []
    for when, proj, sess, model, usage, c in scan(since=since, project_filter=args.project):
        out.append({
            "timestamp": when.isoformat() if when else None,
            "project": proj,
            "session": sess,
            "model": model,
            "model_family": model_family(model),
            "usage": usage,
            "cost_usd": round(c, 6),
        })
    print(json.dumps(out, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(prog="claude-usage", description=__doc__.split("\n")[0])
    p.add_argument("--watch", action="store_true", help="live tail, refresh every --interval seconds")
    p.add_argument("--days", type=int, help="summarise last N days")
    p.add_argument("--session", help="drill into one session (prefix match)")
    p.add_argument("--project", help="filter by project slug substring")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--interval", type=float, default=3.0, help="watch poll interval (default 3s)")
    args = p.parse_args()

    if args.json:
        cmd_json(args)
    elif args.watch:
        cmd_watch(args)
    elif args.session:
        cmd_session(args)
    elif args.days:
        cmd_days(args)
    else:
        cmd_today(args)


if __name__ == "__main__":
    main()
