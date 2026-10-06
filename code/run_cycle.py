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
                            (daily cron; a no-op on a healthy month). Retries stop
                            after the 5th: the cycle is marked "deferred" with ONE
                            push, and next month's scheduled run starts fresh.

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
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy import push  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "bin" / "python"
LOGS = ROOT / "logs"
STATE = LOGS / "cycle_state.json"
LOCK = LOGS / "cycle.lock"
EGRESS = LOGS / "egress_ip.json"   # the Spark's public IPv4 as of the last cycle that landed
CHAIN = ["snapshot.py", "delta.py", "dashboard.py"]
ENTITIES = ("planta", "plantb")
MAX_PUSH_LINES = 10
RETRY_UNTIL_DAY = 5     # daily retries run on the 3rd..5th only; after that, wait for next month


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


def egress_ip() -> str | None:
    """The Spark's public IPv4: the address the ERP's firewall sees, and must whitelist."""
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=10) as r:
            ip = r.read().decode().strip()
        return ip if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip) else None
    except Exception:
        return None


def egress_note(out: str) -> str:
    """When the ERP host times out, say whether the Spark's IP has moved off the one that last
    worked. Sept–Oct 2026: Verizon re-addressed the Spark (.123 -> .164), the host timed out for
    two cycles, and IT, pinging the old address, reported the connection normal."""
    if "Connection timed out" not in out and "20009" not in out:
        return ""
    now = egress_ip()
    try:
        last = json.loads(EGRESS.read_text())
    except Exception:
        last = {}
    if not now:
        return "Could not look up the Spark's public IP."
    if not last.get("ip"):
        return (f"The Spark's public IP is {now}; no record of the IP the last good cycle came "
                f"from, so ask IT whether {now} is the one whitelisted on port 4369.")
    if last["ip"] != now:
        return (f"The Spark's public IP is now {now}; the last cycle that landed came from "
                f"{last['ip']} ({last.get('seen', '?')}). The ERP's firewall whitelists by IP: "
                f"ask IT to whitelist {now} on port 4369.")
    return (f"The Spark's public IP is unchanged ({now}), so the block is on the ERP side: "
            f"host down, port 4369 closed, or the whitelist entry dropped.")


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


def refresh_views() -> None:
    """The app's model and the static wiki, rebuilt from the new snapshot. Neither can fail the
    cycle: the snapshot has landed, and the app and the keepalive rebuild on their own when stale."""
    for cmd in ([str(PY), str(ROOT / "model" / "build.py")],
                [str(ROOT / "scripts" / "build_wiki.sh"), "--force"]):
        try:
            p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=300)
            print(((p.stdout or "") + (p.stderr or "")).strip() or f"{cmd[-1]}: rc {p.returncode}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"view refresh skipped ({cmd[0]}): {e}", flush=True)


# ---------------------------------------------------------------- main

def attempt(cycle: str, prior_attempts: int) -> int:
    attempts = prior_attempts + 1
    stamp = date.today().isoformat()

    for script in CHAIN:
        ok, out = run_step(script)
        if not ok:
            tail = "\n".join(out.strip().splitlines()[-6:]) or "(no output)"
            note = egress_note(out)
            save_state(cycle=cycle, status="pending", attempts=attempts,
                       last_attempt=stamp, last_fail=[f"{script} crashed"] + ([note] if note else []))
            push("alerts",
                 f"ClientCo refresh FAILED - {script}",
                 f"Attempt {attempts} for cycle {cycle} died in {script}.\n\n"
                 + (f"{note}\n\n" if note else "") + f"{tail}\n\n"
                 f"Retrying daily. Log: logs/refresh.log",
                 priority="high", tags="rotating_light")
            if note:
                print(note)
            print(f"CYCLE ABORTED in {script}")
            return 1

    ok, _ = run_step("verify_cycle.py")
    if ok:
        save_state(cycle=cycle, status="ok", attempts=attempts, last_attempt=stamp,
                   last_fail=[])
        ip = egress_ip()
        if ip:
            EGRESS.write_text(json.dumps({"ip": ip, "seen": stamp}))
        refresh_views()
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
        # Retry window is the 3rd..5th only (David 2026-09-08: "if clientco doesn't work by
        # day 5 of the month just stop pinging and try again next month"). September 2026:
        # the ERP host was unreachable and six identical daily pages went to his phone.
        # After the 5th the cycle is DEFERRED — one informational push, then silence until
        # the next month's scheduled run (which always starts a fresh cycle).
        today = date.today()
        if today.day > RETRY_UNTIL_DAY or cycle != today.strftime("%Y-%m"):
            save_state(cycle=cycle, status="deferred", attempts=state.get("attempts", 0),
                       last_attempt=state.get("last_attempt"), last_fail=state.get("last_fail", []),
                       note=f"not retried after the {RETRY_UNTIL_DAY}th; next attempt is next "
                            f"month's scheduled cycle. Deferred = intentional, not an issue.")
            push("clientco", f"ClientCo refresh deferred to next month - {cycle}",
                 f"{state.get('attempts', 0)} attempts through the {RETRY_UNTIL_DAY}th, last failure: "
                 f"{', '.join(state.get('last_fail', [])) or 'unknown'}.\n"
                 f"No more retries or pages this month. Run it by hand once IT restores the host:\n"
                 f"cd ~/clientco-db && ./.venv/bin/python scripts/run_cycle.py",
                 tags="zzz")
            print(f"retry: cycle {cycle} still pending after the {RETRY_UNTIL_DAY}th — "
                  f"DEFERRED to next month, no further pages")
            return 0
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
