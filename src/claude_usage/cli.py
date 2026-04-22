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
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
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


def parse_ts(ts: str | None):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


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


def iter_usage_calls(jl: Path, seen_ids: set[str], start: int = 0):
    """Yield (when, model, usage, cost) for unique billed API calls in a jsonl.

    Claude Code writes multiple jsonl entries for one API call (streaming
    chunks, tool-use splits, etc.). Each carries the same message.id. We
    dedupe on that id so we only count each call once.
    """
    for obj in iter_lines(jl, start):
        msg = obj.get("message") or {}
        usage = msg.get("usage")
        if not usage:
            continue
        msg_id = msg.get("id")
        if msg_id:
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
        model = msg.get("model", "unknown")
        when = parse_ts(obj.get("timestamp"))
        yield when, model, usage, cost_of(usage, model)


def scan(since: date | None = None, project_filter: str | None = None):
    """Yield (when, project, session, model, usage, cost_usd) across all jsonl files.

    Dedupes by message.id globally within this scan — one API call counted once,
    even if the same message.id appears in multiple jsonl files.
    """
    if not PROJECTS_DIR.exists():
        return
    cutoff_ts = datetime.combine(since, datetime.min.time()).timestamp() if since else 0
    seen_ids: set[str] = set()
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
            for when, model, usage, c in iter_usage_calls(jl, seen_ids):
                yield when, proj, sess, model, usage, c


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


def _render_dashboard(sessions: dict, recent: deque, today_total: float, watch_start_total: float) -> None:
    sys.stdout.write("\x1b[2J\x1b[H")  # clear + home
    host = os.uname().nodename
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    since_watch = today_total - watch_start_total
    col_total = color_for(today_total, 5, 15)
    col_since = color_for(since_watch, 0.5, 2)

    print(f"{BOLD}claude-usage live{RESET}   {DIM}{now_str}   {host}{RESET}")
    print(f"  today: {col_total}{fmt_usd(today_total)}{RESET}    since watch start: {col_since}{fmt_usd(since_watch)}{RESET}")
    print()

    active_cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
    sorted_sess = sorted(sessions.items(), key=lambda kv: -kv[1]["total"])

    print(f"{BOLD}TOP SESSIONS TODAY{RESET}  {DIM}(● = active in last 60s){RESET}")
    print(f"    {DIM}{'SESSION':<9} {'PROJECT':<34} {'MODEL':<7} {'CALLS':>6}  {'LAST':<8}  {'COST':>9}{RESET}")
    for sid, s in sorted_sess[:8]:
        active = s["last_ts"] and s["last_ts"] > active_cutoff
        marker = f"{GREEN}●{RESET}" if active else " "
        last_str = s["last_ts"].astimezone().strftime("%H:%M:%S") if s["last_ts"] else "--:--:--"
        col = color_for(s["total"], 1, 5)
        proj = s["project"][:33]
        print(f"  {marker} {sid[:8]:<9} {proj:<34} {s['model']:<7} {s['calls']:>6,}  {last_str:<8}  {col}{fmt_usd(s['total']):>9}{RESET}")
    if not sorted_sess:
        print(f"  {DIM}no activity today yet{RESET}")
    print()

    print(f"{BOLD}RECENT CALLS{RESET}  {DIM}(last {len(recent)}){RESET}")
    if not recent:
        print(f"  {DIM}waiting for new API calls…{RESET}")
    else:
        for r in list(recent):
            ts = r["ts"].astimezone().strftime("%H:%M:%S") if r["ts"] else "??:??:??"
            col = GREEN if r["cost"] < 0.05 else YELLOW if r["cost"] < 0.25 else RED
            print(f"  {DIM}{ts}{RESET}  {r['session'][:8]}  {r['model']:<6}  "
                  f"cache_r={r['cache_r']:>9,}  out={r['output']:>6,}  {col}{fmt_usd(r['cost']):>9}{RESET}")
    print()

    if sorted_sess:
        top = sorted_sess[0][0][:8]
        print(f"{DIM}tip: claude-usage --session {top}  to drill in · ctrl-c to exit{RESET}")
    else:
        print(f"{DIM}ctrl-c to exit{RESET}")
    sys.stdout.flush()


def cmd_watch(args) -> None:
    today = date.today()
    sessions: dict[str, dict] = {}
    today_total = 0.0

    for when, proj, sess, model, _usage, c in scan(since=today, project_filter=args.project):
        if not (when and when.astimezone().date() == today):
            continue
        today_total += c
        s = sessions.setdefault(sess, {
            "project": proj, "model": model_family(model),
            "calls": 0, "total": 0.0, "first_ts": when, "last_ts": when,
        })
        s["calls"] += 1
        s["total"] += c
        if when > s["last_ts"]:
            s["last_ts"] = when
        if when < s["first_ts"]:
            s["first_ts"] = when

    # Re-scan to populate seen_ids so subsequent polls don't recount seeded calls.
    seen_ids: set[str] = set()
    for _proj_dir in PROJECTS_DIR.iterdir():
        if not _proj_dir.is_dir():
            continue
        for _jl in _proj_dir.glob("*.jsonl"):
            try:
                if _jl.stat().st_mtime < datetime.combine(today, datetime.min.time()).timestamp():
                    continue
            except FileNotFoundError:
                continue
            for _obj in iter_lines(_jl):
                _msg = _obj.get("message") or {}
                _mid = _msg.get("id")
                if _mid and _msg.get("usage"):
                    seen_ids.add(_mid)

    offsets: dict[Path, int] = {}
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for jl in proj_dir.glob("*.jsonl"):
            try:
                offsets[jl] = jl.stat().st_size
            except FileNotFoundError:
                continue

    recent: deque = deque(maxlen=12)
    watch_start_total = today_total

    try:
        _render_dashboard(sessions, recent, today_total, watch_start_total)
        while True:
            time.sleep(args.interval)
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
                    sess_id = jl.stem
                    for when, model, usage, c in iter_usage_calls(jl, seen_ids, prev):
                        if when is None:
                            when = datetime.now(timezone.utc)
                        today_total += c
                        s = sessions.setdefault(sess_id, {
                            "project": proj, "model": model_family(model),
                            "calls": 0, "total": 0.0, "first_ts": when, "last_ts": when,
                        })
                        s["calls"] += 1
                        s["total"] += c
                        s["last_ts"] = when
                        recent.append({
                            "ts": when, "session": sess_id, "project": proj,
                            "model": model_family(model),
                            "input": usage.get("input_tokens", 0),
                            "cache_r": usage.get("cache_read_input_tokens", 0),
                            "output": usage.get("output_tokens", 0),
                            "cost": c,
                        })
                    offsets[jl] = size
            _render_dashboard(sessions, recent, today_total, watch_start_total)
    except KeyboardInterrupt:
        sys.stdout.write("\x1b[0m\n")


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
    seen: set[str] = set()
    for when, model, usage, c in iter_usage_calls(jl, seen):
        t = when.astimezone().strftime("%H:%M:%S") if when else "??:??:??"
        total += c
        calls += 1
        inp = usage.get("input_tokens", 0)
        cr = usage.get("cache_read_input_tokens", 0)
        cw = usage.get("cache_creation_input_tokens", 0)
        out = usage.get("output_tokens", 0)
        call_col = GREEN if c < 0.05 else YELLOW if c < 0.25 else RED
        print(f"  {DIM}{t}{RESET}  {model_family(model):<6}  in={inp:>6,}  cache_r={cr:>9,}  cache_w={cw:>7,}  out={out:>6,}  {call_col}{fmt_usd(c)}{RESET}")
    col = color_for(total, 1, 3)
    print(f"\n{BOLD}total {col}{fmt_usd(total)}{RESET} across {calls} unique API calls")


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
