#!/usr/bin/env python3
"""claude-watch — parse local Claude Code logs, show token spend.

Reads ~/.claude/projects/**/*.jsonl on this machine and computes USD cost
per session/project/model/day. Stdlib only, no external deps.

Usage:
  claude-watch                       # today
  claude-watch --watch               # live dashboard
  claude-watch --watch --max-today N # circuit breaker: kill LIVE sessions if today > $N
  claude-watch --days 7              # last N days bar chart
  claude-watch --session XXX         # drill into one session (prefix match)
  claude-watch --project NAME        # filter by project slug substring
  claude-watch --json                # machine-readable
  claude-watch --kill SESSION        # SIGTERM the process writing a session's jsonl
  claude-watch --kill SESSION --force  # SIGKILL instead
  claude-watch --kill-live           # kill every session with status=LIVE
  claude-watch --version
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

__version__ = "0.4.0"

PROJECTS_DIR = Path.home() / ".claude" / "projects"
HOME = Path.home()

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

# cache_read threshold above which a call's context is considered "bloated"
CTX_WARN_THRESHOLD = 200_000


# ----- basic helpers ----------------------------------------------------------

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


def parse_ts(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def local_date(dt):
    """Local calendar date of an aware datetime, or None."""
    return dt.astimezone().date() if dt else None


def fmt_usd(x: float) -> str:
    return f"${x:,.2f}"


def color_for(cost: float, low: float = 1.0, mid: float = 5.0) -> str:
    return GREEN if cost < low else YELLOW if cost < mid else RED


def fmt_age(last_ts, now_utc) -> str:
    if not last_ts:
        return "--"
    age = int((now_utc - last_ts).total_seconds())
    if age < 5:
        return "just now"
    if age < 60:
        return f"{age}s ago"
    if age < 3600:
        return f"{age // 60}m ago"
    if age < 86400:
        return f"{age // 3600}h ago"
    return f"{age // 86400}d ago"


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    h, rem = divmod(s, 3600)
    return f"{h}h {rem // 60}m"


def session_state(last_ts, now_utc):
    """Return (label, color, marker)."""
    if not last_ts:
        return "DONE", DIM, " "
    age = (now_utc - last_ts).total_seconds()
    if age < 60:
        return "LIVE", GREEN, f"{GREEN}●{RESET}"
    if age < 600:
        return "IDLE", YELLOW, f"{YELLOW}●{RESET}"
    return "DONE", DIM, " "


# ----- project label (read cwd from jsonl) ------------------------------------

_cwd_cache: dict = {}


def cwd_label(cwd) -> str:
    """Convert an absolute cwd into a short human label."""
    if not cwd:
        return "?"
    p = Path(cwd)
    try:
        if p == HOME:
            return "~"
        rel = p.relative_to(HOME)
        parts = rel.parts
        if len(parts) >= 3 and parts[0] == "Documents" and parts[1] == "projects":
            return parts[2]
        if len(parts) >= 2 and parts[0] == "projects":
            return parts[1]
        if len(parts) >= 2 and parts[0] == "src":
            return parts[1]
        return "~/" + "/".join(parts[:2])
    except ValueError:
        return str(p)


def session_cwd(jl: Path):
    """Read the first message in a jsonl file; return its cwd."""
    if jl in _cwd_cache:
        return _cwd_cache[jl]
    for obj in iter_lines(jl):
        cwd = obj.get("cwd")
        if cwd:
            _cwd_cache[jl] = cwd
            return cwd
    _cwd_cache[jl] = None
    return None


def project_label(jl: Path) -> str:
    return cwd_label(session_cwd(jl))


# ----- jsonl scan + dedup -----------------------------------------------------

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


def iter_usage_calls(jl: Path, seen_ids: set, start: int = 0):
    """Yield (when, model, usage, cost) per unique billed API call.

    Dedupes by message.id — Claude Code writes multiple jsonl entries per
    billed call (streaming chunks, tool-use splits, retries).
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


def scan(since=None, project_filter=None):
    """Yield (when, project, session, model, usage, cost_usd) across all jsonl files."""
    if not PROJECTS_DIR.exists():
        return
    cutoff_ts = datetime.combine(since, datetime.min.time()).timestamp() if since else 0
    seen_ids: set = set()
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for jl in proj_dir.glob("*.jsonl"):
            try:
                if since and jl.stat().st_mtime < cutoff_ts:
                    continue
            except FileNotFoundError:
                continue
            proj = project_label(jl)
            if project_filter and project_filter.lower() not in proj.lower():
                continue
            sess = jl.stem
            for when, model, usage, c in iter_usage_calls(jl, seen_ids):
                yield when, proj, sess, model, usage, c


# ----- no-data guard ----------------------------------------------------------

def guard_projects_dir():
    if not PROJECTS_DIR.exists():
        print(f"{DIM}no Claude Code logs found at {PROJECTS_DIR}.{RESET}")
        print(f"{DIM}This machine either hasn't run Claude Code yet, or uses a different config path.{RESET}")
        sys.exit(0)


# ----- kill infrastructure ----------------------------------------------------

def lsof_pids(path: Path) -> list:
    """Return PIDs holding a file open. Empty list if lsof missing / no holders."""
    try:
        r = subprocess.run(
            ["lsof", "-t", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        return [int(p) for p in r.stdout.split() if p.isdigit()]
    except FileNotFoundError:
        return []
    except subprocess.TimeoutExpired:
        return []


def find_session_file(prefix: str):
    if not PROJECTS_DIR.exists():
        return None
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for jl in proj_dir.glob(f"{prefix}*.jsonl"):
            return jl
    return None


def kill_pids(pids, force=False) -> int:
    sig = signal.SIGKILL if force else signal.SIGTERM
    name = "SIGKILL" if force else "SIGTERM"
    killed = 0
    for pid in pids:
        try:
            os.kill(pid, sig)
            print(f"  {name} → PID {pid}")
            killed += 1
        except ProcessLookupError:
            print(f"  PID {pid} already gone")
        except PermissionError:
            print(f"  permission denied killing PID {pid}", file=sys.stderr)
    return killed


# ----- today view -------------------------------------------------------------

def cmd_today(args) -> None:
    guard_projects_dir()
    today = date.today()
    rows = [r for r in scan(since=today, project_filter=args.project)
            if local_date(r[0]) == today]
    if not rows:
        print(f"{DIM}no Claude activity today.{RESET}")
        return
    total = sum(r[5] for r in rows)
    by_sess = defaultdict(float)
    by_proj = defaultdict(float)
    by_model = defaultdict(float)
    sess_proj = {}
    for _when, proj, sess, model, _usage, c in rows:
        by_sess[sess] += c
        by_proj[proj] += c
        by_model[model_family(model)] += c
        sess_proj[sess] = proj

    print(f"{BOLD}claude-watch{RESET}  {today.isoformat()}  ({os.uname().nodename})")
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


# ----- watch dashboard --------------------------------------------------------

def _burn_rate(burn_window: deque, now_utc) -> float:
    cutoff = now_utc - timedelta(minutes=10)
    while burn_window and burn_window[0][0] < cutoff:
        burn_window.popleft()
    if not burn_window:
        return 0.0
    total = sum(c for _, c in burn_window)
    elapsed = max((now_utc - burn_window[0][0]).total_seconds(), 1.0)
    return total / elapsed * 3600


def _render_dashboard(sessions: dict, recent: deque, today_total: float,
                      watch_start_total: float, burn_window: deque,
                      max_today: float | None) -> None:
    sys.stdout.write("\x1b[2J\x1b[H")
    host = os.uname().nodename
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_utc = datetime.now(timezone.utc)
    since_watch = today_total - watch_start_total
    rate = _burn_rate(burn_window, now_utc)

    col_total = color_for(today_total, 5, 15)
    col_since = color_for(since_watch, 0.5, 2)
    col_rate = color_for(rate, 5, 15)

    print(f"{BOLD}claude-watch live{RESET}   {DIM}v{__version__}  {now_str}  {host}{RESET}")
    header = (f"  today: {col_total}{fmt_usd(today_total)}{RESET}"
              f"    since watch: {col_since}{fmt_usd(since_watch)}{RESET}"
              f"    burn: {col_rate}{fmt_usd(rate)}/hr{RESET}")
    if max_today is not None:
        cap_col = RED if today_total > 0.8 * max_today else YELLOW if today_total > 0.5 * max_today else GREEN
        header += f"    cap: {cap_col}{fmt_usd(max_today)}{RESET}"
    print(header)
    print()

    def sort_key(kv):
        s = kv[1]
        age = (now_utc - s["last_ts"]).total_seconds() if s["last_ts"] else 1e12
        bucket = 0 if age < 60 else 1 if age < 600 else 2
        return (bucket, -s["total"])
    sorted_sess = sorted(sessions.items(), key=sort_key)

    print(f"{BOLD}TOP SESSIONS TODAY{RESET}  {DIM}(LIVE < 60s · IDLE 1–10m · DONE > 10m){RESET}")
    print(f"    {DIM}{'STATUS':<6} {'SESSION':<9} {'PROJECT':<24} {'MODEL':<7} {'CALLS':>6}  {'LAST':<10}  {'COST':>9}{RESET}")
    if not sorted_sess:
        print(f"  {DIM}no activity today yet{RESET}")
    for sid, s in sorted_sess[:8]:
        label, label_col, marker = session_state(s["last_ts"], now_utc)
        age_str = fmt_age(s["last_ts"], now_utc)
        col = color_for(s["total"], 1, 5)
        proj = (s["project"] or "?")[:23]
        print(f"  {marker} {label_col}{label:<4}{RESET} {sid[:8]:<9} {proj:<24} {s['model']:<7} {s['calls']:>6,}  {age_str:<10}  {col}{fmt_usd(s['total']):>9}{RESET}")
    print()

    print(f"{BOLD}RECENT CALLS{RESET}  {DIM}(⚠ = cache_r > {CTX_WARN_THRESHOLD:,}){RESET}")
    if not recent:
        print(f"  {DIM}waiting for new API calls…{RESET}")
    else:
        for r in list(recent):
            ts = r["ts"].astimezone().strftime("%H:%M:%S") if r["ts"] else "??:??:??"
            warn = f"{YELLOW}⚠{RESET}" if r["cache_r"] > CTX_WARN_THRESHOLD else " "
            col = GREEN if r["cost"] < 0.05 else YELLOW if r["cost"] < 0.25 else RED
            print(f"  {warn} {DIM}{ts}{RESET}  {r['session'][:8]}  {r['model']:<6}  "
                  f"cache_r={r['cache_r']:>9,}  out={r['output']:>6,}  {col}{fmt_usd(r['cost']):>9}{RESET}")
    print()

    # Highlight the top LIVE session in the tip line, else the top spender
    live_sessions = [sid for sid, s in sorted_sess
                     if s["last_ts"] and (now_utc - s["last_ts"]).total_seconds() < 60]
    if live_sessions:
        sid = live_sessions[0][:8]
        print(f"{DIM}tip: claude-watch --session {sid} · claude-watch --kill {sid} · ctrl-c to exit{RESET}")
    elif sorted_sess:
        sid = sorted_sess[0][0][:8]
        print(f"{DIM}tip: claude-watch --session {sid}  to drill in · ctrl-c to exit{RESET}")
    else:
        print(f"{DIM}ctrl-c to exit{RESET}")
    sys.stdout.flush()


def _kill_all_live(sessions: dict, now_utc) -> int:
    killed = 0
    for sid, s in sessions.items():
        if s["last_ts"] and (now_utc - s["last_ts"]).total_seconds() < 60:
            jl = find_session_file(sid)
            if jl:
                killed += kill_pids(lsof_pids(jl))
    return killed


def cmd_watch(args) -> None:
    guard_projects_dir()
    today = date.today()
    sessions: dict = {}
    today_total = 0.0

    for when, proj, sess, model, _usage, c in scan(since=today, project_filter=args.project):
        if local_date(when) != today:
            continue
        today_total += c
        s = sessions.setdefault(sess, {
            "project": proj, "model": model_family(model),
            "calls": 0, "total": 0.0, "first_ts": when, "last_ts": when,
        })
        s["calls"] += 1
        s["total"] += c
        if when and (not s["last_ts"] or when > s["last_ts"]):
            s["last_ts"] = when
        if when and (not s["first_ts"] or when < s["first_ts"]):
            s["first_ts"] = when

    # Pre-populate seen_ids so poll-loop reads don't recount seeded calls.
    seen_ids: set = set()
    cutoff_ts = datetime.combine(today, datetime.min.time()).timestamp()
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for jl in proj_dir.glob("*.jsonl"):
            try:
                if jl.stat().st_mtime < cutoff_ts:
                    continue
            except FileNotFoundError:
                continue
            for obj in iter_lines(jl):
                msg = obj.get("message") or {}
                mid = msg.get("id")
                if mid and msg.get("usage"):
                    seen_ids.add(mid)

    offsets: dict = {}
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for jl in proj_dir.glob("*.jsonl"):
            try:
                offsets[jl] = jl.stat().st_size
            except FileNotFoundError:
                continue

    recent: deque = deque(maxlen=12)
    burn_window: deque = deque()
    watch_start_total = today_total

    try:
        _render_dashboard(sessions, recent, today_total, watch_start_total,
                          burn_window, args.max_today)
        while True:
            time.sleep(args.interval)
            for proj_dir in PROJECTS_DIR.iterdir():
                if not proj_dir.is_dir():
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
                    proj = project_label(jl)
                    if args.project and args.project.lower() not in proj.lower():
                        offsets[jl] = size
                        continue
                    sess_id = jl.stem
                    for when, model, usage, c in iter_usage_calls(jl, seen_ids, prev):
                        if when is None:
                            when = datetime.now(timezone.utc)
                        today_total += c
                        burn_window.append((when, c))
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

            # Circuit breaker
            if args.max_today is not None and today_total > args.max_today:
                sys.stdout.write("\x1b[2J\x1b[H")
                print(f"{BOLD}{RED}CIRCUIT BREAKER TRIPPED{RESET}")
                print(f"  today's total: {fmt_usd(today_total)} > cap {fmt_usd(args.max_today)}")
                print(f"  killing all LIVE sessions with SIGTERM...\n")
                killed = _kill_all_live(sessions, datetime.now(timezone.utc))
                print(f"\n  killed {killed} process(es). Exiting watch.")
                return

            _render_dashboard(sessions, recent, today_total, watch_start_total,
                              burn_window, args.max_today)
    except KeyboardInterrupt:
        sys.stdout.write("\x1b[0m\n")


# ----- days view --------------------------------------------------------------

def cmd_days(args) -> None:
    guard_projects_dir()
    since = date.today() - timedelta(days=args.days - 1)
    by_day = defaultdict(float)
    total = 0.0
    for when, _proj, _sess, _model, _usage, c in scan(since=since, project_filter=args.project):
        d = local_date(when)
        if d is None or d < since:
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


# ----- session drill-down -----------------------------------------------------

def cmd_session(args) -> None:
    guard_projects_dir()
    jl = find_session_file(args.session)
    if not jl:
        print(f"no session found matching '{args.session}'", file=sys.stderr)
        sys.exit(1)
    proj_dir = jl.parent
    proj = project_label(jl)

    total = 0.0
    calls = 0
    first_ts = None
    last_ts = None
    seen: set = set()
    rows = []
    for when, model, usage, c in iter_usage_calls(jl, seen):
        rows.append((when, model, usage, c))
        total += c
        calls += 1
        if when:
            if first_ts is None or when < first_ts:
                first_ts = when
            if last_ts is None or when > last_ts:
                last_ts = when

    now_utc = datetime.now(timezone.utc)
    label, label_col, marker = session_state(last_ts, now_utc)
    duration_str = fmt_duration((last_ts - first_ts).total_seconds()) if (first_ts and last_ts and last_ts > first_ts) else "--"
    first_str = first_ts.astimezone().strftime("%Y-%m-%d %H:%M:%S") if first_ts else "--"
    last_str = last_ts.astimezone().strftime("%Y-%m-%d %H:%M:%S") if last_ts else "--"
    age_str = fmt_age(last_ts, now_utc)

    pids = lsof_pids(jl)
    proc_hint = f"  pid(s): {DIM}{', '.join(str(p) for p in pids)}{RESET}" if pids else f"  pid(s): {DIM}none — file not currently open{RESET}"

    print(f"{BOLD}session {jl.stem}{RESET}  project: {proj}")
    print(f"  status: {marker} {label_col}{label}{RESET}  {DIM}(last call {age_str}){RESET}")
    print(f"  started: {DIM}{first_str}{RESET}    last: {DIM}{last_str}{RESET}    duration: {duration_str}")
    col = color_for(total, 1, 3)
    print(f"  calls: {calls:,}    cost: {col}{fmt_usd(total)}{RESET}")
    print(proc_hint)
    print()

    print(f"{BOLD}per-call detail{RESET}")
    for when, model, usage, c in rows:
        t = when.astimezone().strftime("%H:%M:%S") if when else "??:??:??"
        inp = usage.get("input_tokens", 0)
        cr = usage.get("cache_read_input_tokens", 0)
        cw = usage.get("cache_creation_input_tokens", 0)
        out = usage.get("output_tokens", 0)
        warn = f"{YELLOW}⚠{RESET}" if cr > CTX_WARN_THRESHOLD else " "
        call_col = GREEN if c < 0.05 else YELLOW if c < 0.25 else RED
        print(f"  {warn} {DIM}{t}{RESET}  {model_family(model):<6}  in={inp:>6,}  cache_r={cr:>9,}  cache_w={cw:>7,}  out={out:>6,}  {call_col}{fmt_usd(c)}{RESET}")


# ----- json output ------------------------------------------------------------

def cmd_json(args) -> None:
    guard_projects_dir()
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


# ----- kill commands ----------------------------------------------------------

def cmd_kill(args) -> None:
    guard_projects_dir()
    jl = find_session_file(args.kill)
    if not jl:
        print(f"no session found matching '{args.kill}'", file=sys.stderr)
        sys.exit(1)
    pids = lsof_pids(jl)
    if not pids:
        print(f"session {jl.stem[:8]}: no live process (jsonl not currently open).")
        return
    print(f"session {jl.stem[:8]} held by {len(pids)} PID(s):")
    killed = kill_pids(pids, force=args.force)
    print(f"\nsent signal to {killed} process(es).")


def cmd_kill_live(args) -> None:
    guard_projects_dir()
    today = date.today()
    now_utc = datetime.now(timezone.utc)
    sessions: dict = {}
    for when, _proj, sess, _model, _usage, c in scan(since=today):
        if local_date(when) != today:
            continue
        s = sessions.setdefault(sess, {"last_ts": when, "total": 0.0})
        if when and (not s["last_ts"] or when > s["last_ts"]):
            s["last_ts"] = when
        s["total"] += c

    live = [(sid, s) for sid, s in sessions.items()
            if s["last_ts"] and (now_utc - s["last_ts"]).total_seconds() < 60]
    if not live:
        print("no LIVE sessions to kill.")
        return

    print(f"{len(live)} LIVE session(s):")
    for sid, s in live:
        age_str = fmt_age(s["last_ts"], now_utc)
        print(f"  {sid[:8]}  cost today: {fmt_usd(s['total']):>9}  last: {age_str}")

    if not args.yes:
        try:
            ans = input(f"\nkill all {len(live)}? [y/N] ")
        except EOFError:
            ans = ""
        if ans.strip().lower() != "y":
            print("aborted.")
            return

    print()
    killed = 0
    for sid, _ in live:
        jl = find_session_file(sid)
        if not jl:
            continue
        pids = lsof_pids(jl)
        if not pids:
            print(f"  {sid[:8]}: no PID holding jsonl (maybe already exited)")
            continue
        killed += kill_pids(pids, force=args.force)
    print(f"\nkilled {killed} process(es).")


# ----- main -------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(prog="claude-watch",
                                description="Live dashboard + kill switch for Claude Code spend.")
    p.add_argument("--version", action="version", version=f"claude-watch {__version__}")
    p.add_argument("--watch", action="store_true", help="live dashboard")
    p.add_argument("--days", type=int, help="summarise last N days")
    p.add_argument("--session", help="drill into one session (prefix match)")
    p.add_argument("--project", help="filter by project label substring")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--interval", type=float, default=3.0, help="watch poll interval (seconds)")
    p.add_argument("--max-today", type=float, help="kill LIVE sessions when --watch if today's spend exceeds $N")
    p.add_argument("--kill", metavar="SESSION", help="SIGTERM the process writing a session's jsonl")
    p.add_argument("--kill-live", action="store_true", help="SIGTERM every session with status=LIVE")
    p.add_argument("--force", action="store_true", help="with --kill/--kill-live: use SIGKILL instead")
    p.add_argument("--yes", action="store_true", help="with --kill-live: skip confirmation prompt")
    args = p.parse_args()

    if args.kill:
        cmd_kill(args)
    elif args.kill_live:
        cmd_kill_live(args)
    elif args.json:
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
