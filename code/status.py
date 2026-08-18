#!/usr/bin/env python3
"""scripts/status.py — one-glance project status for session start.

Answers "what's pending?" without spelunking: unprocessed _inbox/ items, latest
snapshot per entity + next cron date, git working-tree state, and the most
recent log.md entry per entity. Pure stdlib, never touches the DB.

    ./.venv/bin/python scripts/status.py
"""
import subprocess
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTITIES = ["planta", "plantb", "usa", "group"]
SNAPSHOT_ENTITIES = ["planta", "plantb"]


def hr(title):
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


def inbox():
    hr("_inbox (unprocessed work)")
    items = [p for p in sorted((ROOT / "_inbox").rglob("*"))
             if p.is_file() and p.name != "README.md"]
    if not items:
        print("  empty — nothing to triage ✅")
        return
    for p in items:
        kb = p.stat().st_size / 1024
        print(f"  • {p.relative_to(ROOT)}  ({kb:,.0f} KB)")
    print(f"  → {len(items)} item(s). Workflow: INGESTION.md")


def next_cron(today):
    # cron fires the 3rd of each month at 04:00
    if today.day < 3:
        return date(today.year, today.month, 3)
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return date(y, m, 3)


def snapshots():
    hr("DB snapshots (monthly cron, 3rd 04:00)")
    today = date.today()
    for e in SNAPSHOT_ENTITIES:
        base = ROOT / e / "snapshots"
        dirs = sorted(d.name for d in base.iterdir()
                      if d.is_dir() and not d.name.startswith(".")) if base.exists() else []
        if not dirs:
            print(f"  {e:10s} no snapshots yet")
            continue
        latest = dirs[-1]
        try:
            age = (today - date.fromisoformat(latest)).days
            print(f"  {e:10s} latest {latest}  ({age}d ago, {len(dirs)} total)")
        except ValueError:
            print(f"  {e:10s} latest {latest}  ({len(dirs)} total)")
        tmp = [d.name for d in base.iterdir() if d.is_dir() and d.name.startswith(".tmp-")] \
            if base.exists() else []
        if tmp:
            print(f"  {'':10s} ⚠ leftover errored run(s): {', '.join(tmp)} — check logs/refresh.log")
    print(f"  next cron run: {next_cron(today)}")
    deltas = sorted((ROOT / "_inbox" / "db_deltas").glob("*.md")) \
        if (ROOT / "_inbox" / "db_deltas").exists() else []
    if deltas:
        print(f"  ⚠ unprocessed delta report(s): {', '.join(d.name for d in deltas)}")


def git_state():
    hr("git")
    try:
        sb = subprocess.run(["git", "status", "-sb"], cwd=ROOT, capture_output=True,
                            text=True, timeout=10).stdout.strip().splitlines()
    except Exception as ex:
        print(f"  (git unavailable: {ex})")
        return
    print(f"  {sb[0] if sb else '?'}")
    dirty = sb[1:]
    mod = sum(1 for l in dirty if not l.startswith("??"))
    new = sum(1 for l in dirty if l.startswith("??"))
    if dirty:
        print(f"  ⚠ working tree dirty: {mod} modified, {new} untracked — consider a commit")
    else:
        print("  working tree clean ✅")


def recent_logs():
    hr("last log.md entry per entity")
    for e in ENTITIES:
        log = ROOT / e / "wiki" / "log.md"
        if not log.exists():
            continue
        last = None
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.startswith("## ["):
                last = line
        print(f"  {e:10s} {(last or '(no entries)').lstrip('# ')}")


def main():
    print("clientco-db status — read CLAUDE.md scope rules before querying anything")
    inbox()
    snapshots()
    git_state()
    recent_logs()
    print()


if __name__ == "__main__":
    main()
