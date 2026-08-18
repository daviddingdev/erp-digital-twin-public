#!/usr/bin/env python3
"""scripts/run_cycle.py — run the monthly refresh cycle, notify, and retry until it lands.

The cycle is snapshot.py -> delta.py -> dashboard.py -> verify_cycle.py. Any of
them can fail, but the failure that actually bites is *silent and data-shaped*:
in August 2026 every script exited 0 while PlantA's standby copy hadn't been
refreshed, so the run produced a byte-identical snapshot and nobody noticed for
five days. verify_cycle.py catches that (delta §0 plumbing); this wrapper makes
sure a human hears about it and that the cycle re-attempts on its own once IT
catches up.

Modes:
    run_cycle.py            full attempt — the monthly cron entry (3rd, 04:00)
    run_cycle.py --retry    attempt ONLY if the last cycle is still pending
                            (daily cron; a no-op on a healthy month)

State lives in logs/cycle_state.json:
    {"cycle": "2026-08", "status": "pending"|"ok", "attempts": N, ...}

Notifications (scripts/ntfy.py, topics in ~/maintenance/config/ntfy.json):
    success -> `clientco`  "refresh landed" + cutoffs + what changed this month
    failure -> `alerts`   which checks failed + attempt count (urgent priority)

Exit code 0 = cycle green (or retry skipped), 1 = still broken.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy import push  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "bin" / "python"
LOGS = ROOT / "logs"
STATE = LOGS / "cycle_state.json"
LOCK = LOGS / "cycle.lock"
CHAIN = ["snapshot.py", "delta.py", "dashboard.py"]
ENTITIES = ("planta", "plantb")
MAX_PUSH_LINES = 10


# ---------------------------------------------------------------- state

def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(**kw) -> None:
    LOGS.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(kw, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- running

def run_step(script: str) -> tuple[bool, str]:
    """Run one pipeline script; return (ok, combined output)."""
    p = subprocess.run([str(PY), str(ROOT / "scripts" / script)],
                       cwd=ROOT, capture_output=True, text=True)
    out = (p.stdout or "") + (p.stderr or "")
    print(out, end="", flush=True)
    return p.returncode == 0, out


def failing_checks() -> list[str]:
    """Names of checks verify_cycle.py just marked failed."""
    try:
        data = json.loads((LOGS / "last_cycle_check.json").read_text())
    except Exception:
        return ["cycle-check result unreadable"]
    return [f"{c['name']}" + (f" — {c['detail']}" if c.get("detail") else "")
            for c in data.get("checks", []) if not c.get("ok")]


# ---------------------------------------------------------------- summary

def latest_snapshot(entity: str):
    base = ROOT / entity / "snapshots"
    dirs = sorted((d for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")),
                  key=lambda d: d.name) if base.exists() else []
    return dirs[-1] if dirs else None


def cutoffs() -> dict[str, str]:
    out = {}
    for e in ENTITIES:
        d = latest_snapshot(e)
        if not d:
            continue
        try:
            out[e] = str(json.loads((d / "00_meta.json").read_text())
                         .get("data_cutoff", "?"))[:10]
        except Exception:
            out[e] = "?"
    return out


def delta_highlights() -> tuple[str | None, list[str]]:
    """(delta filename, the lines worth pushing) from the newest inbox delta report."""
    inbox = ROOT / "_inbox" / "db_deltas"
    files = sorted(inbox.glob("*.md")) if inbox.exists() else []
    if not files:
        return None, []
    txt = files[-1].read_text(encoding="utf-8")
    hits = []
    for line in txt.splitlines():
        s = line.strip()
        # the delta marks real signal with these; everything else is prose/scaffolding
        if s.startswith("-") and any(m in s for m in ("⚠", "🆕", "🚨")):
            s = re.sub(r"\s+", " ", s.lstrip("- ").replace("**", ""))
            # ERP-A temp-report tables churn every month and mean nothing
            if "UFTmpTable_" in s:
                s = re.sub(r":.*", ": ERP-A temp-table churn (ignored)", s)
            hits.append(s[:180])
    return files[-1].name, hits


def revenue_lines() -> list[str]:
    """The year-to-date headline dashboard.py just wrote."""
    try:
        txt = (ROOT / "group" / "wiki" / "dashboard.md").read_text(encoding="utf-8")
    except Exception:
        return []
    return [re.sub(r"[*`]", "", ln).strip("- ").strip()
            for ln in txt.splitlines() if re.match(r"^- \*\*(PlantA|PlantB) Jan", ln)]


def success_message() -> str:
    cuts = cutoffs()
    lines = ["DB data through: " + ", ".join(
        f"{e[:2].upper()} {cuts.get(e, '?')}" for e in ENTITIES)]
    lines += revenue_lines()
    name, hits = delta_highlights()
    if hits:
        lines.append("")
        lines.append(f"Changes ({name}):")
        lines += [f"• {h}" for h in hits[:MAX_PUSH_LINES]]
        if len(hits) > MAX_PUSH_LINES:
            lines.append(f"• …+{len(hits) - MAX_PUSH_LINES} more")
    elif name:
        lines.append(f"\nNo material changes flagged ({name}).")
    lines.append("\nWiki: http://127.0.0.1:8000/board/brief.html")
    return "\n".join(lines)


# ---------------------------------------------------------------- main

def attempt(cycle: str, prior_attempts: int) -> int:
    attempts = prior_attempts + 1
    stamp = date.today().isoformat()

    for script in CHAIN:
        ok, out = run_step(script)
        if not ok:
            tail = "\n".join(out.strip().splitlines()[-6:]) or "(no output)"
            save_state(cycle=cycle, status="pending", attempts=attempts,
                       last_attempt=stamp, last_fail=[f"{script} crashed"])
            push("alerts",
                 f"ClientCo refresh FAILED - {script}",
                 f"Attempt {attempts} for cycle {cycle} died in {script}.\n\n{tail}\n\n"
                 f"Retrying daily. Log: logs/refresh.log",
                 priority="high", tags="rotating_light")
            print(f"CYCLE ABORTED in {script}")
            return 1

    ok, _ = run_step("verify_cycle.py")
    if ok:
        save_state(cycle=cycle, status="ok", attempts=attempts, last_attempt=stamp,
                   last_fail=[])
        push("clientco", f"ClientCo refresh landed - {cycle}", success_message(),
             tags="factory")
        print("CYCLE OK")
        return 0

    fails = failing_checks()
    save_state(cycle=cycle, status="pending", attempts=attempts, last_attempt=stamp,
               last_fail=fails)
    body = ("\n".join(f"• {f}" for f in fails)
            + f"\n\nAttempt {attempts} for cycle {cycle}. Retrying every 24h until it passes."
            + ("\n\nA stuck §0 plumbing check means IT's standby refresh hasn't landed — "
               "the retry clears itself once it does."
               if any("plumbing" in f for f in fails) else ""))
    push("alerts", f"ClientCo refresh needs attention - {cycle}", body,
         priority="high", tags="warning")
    print("CYCLE FAILED")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--retry", action="store_true",
                    help="only run if the last cycle is still pending")
    args = ap.parse_args()

    LOGS.mkdir(exist_ok=True)
    state = load_state()
    cycle = date.today().strftime("%Y-%m")

    if args.retry:
        if state.get("status") != "pending":
            print(f"retry: nothing pending (last cycle {state.get('cycle', '—')} "
                  f"= {state.get('status', 'unknown')}) — skipping")
            return 0
        cycle = state.get("cycle", cycle)
        print(f"retry: cycle {cycle} still pending after "
              f"{state.get('attempts', 0)} attempt(s) — re-running")

    # A slow run must not be lapped by the next cron tick.
    with open(LOCK, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another cycle run holds the lock — skipping")
            return 0
        print(f"=== run_cycle {cycle} "
              f"({'retry' if args.retry else 'scheduled'}) {date.today()} ===")
        return attempt(cycle, state.get("attempts", 0) if args.retry else 0)


if __name__ == "__main__":
    raise SystemExit(main())
