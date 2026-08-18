#!/usr/bin/env python3
"""scripts/verify_cycle.py — deterministic post-refresh verification (no LLM).

Runs on cron a couple of hours after the monthly snapshot chain (3rd) and
answers "did the cycle land?" — result goes to logs/last_cycle_check.json,
which wiki_ctl /status exposes and the landing page renders. Also prints a
human-readable line per check (cron appends to logs/refresh.log).

Checks: fresh snapshot per entity (≤5 days old, zero section errors), no
leftover .tmp- staging dirs, the matching delta report exists (inbox or
archive) and its §0 plumbing carries no 🚨, generated views were rebuilt
(dashboard.md frontmatter ≥ snapshot date; board carries the new cutoff),
and both web servers answer.
"""
import json
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "logs" / "last_cycle_check.json"
ENTITIES = ("planta", "plantb")


def latest(entity):
    base = ROOT / entity / "snapshots"
    dirs = sorted(d for d in base.iterdir()
                  if d.is_dir() and not d.name.startswith(".")) if base.exists() else []
    return dirs[-1] if dirs else None


def http_ok(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    today = date.today()
    snap_dates, cutoffs = {}, {}
    for e in ENTITIES:
        d = latest(e)
        if d is None:
            add(f"{e}: snapshot exists", False, "no snapshot dirs at all")
            continue
        snap_dates[e] = d.name
        try:
            age = (today - date.fromisoformat(d.name)).days
        except ValueError:
            age = 99
        meta = {}
        try:
            meta = json.loads((d / "00_meta.json").read_text())
        except Exception:
            pass
        cutoffs[e] = str(meta.get("data_cutoff", ""))[:10]
        errs = meta.get("errors", ["meta unreadable"])
        add(f"{e}: snapshot fresh", age <= 5, f"{d.name} ({age}d old)")
        add(f"{e}: snapshot clean", errs == [], f"errors={len(errs)}" if errs else "0 section errors")
        tmp = [t.name for t in (ROOT / e / "snapshots").iterdir() if t.name.startswith(".tmp-")]
        add(f"{e}: no leftover .tmp dirs", not tmp, ", ".join(tmp))

    sd = max(snap_dates.values()) if snap_dates else None
    if sd:
        delta = ROOT / "_inbox" / "db_deltas" / f"{sd}.md"
        if not delta.exists():
            delta = ROOT / "_archive" / "db_deltas" / sd[:4] / f"{sd}.md"
        add("delta report exists", delta.exists(), str(delta.relative_to(ROOT)) if delta.exists() else f"none for {sd}")
        if delta.exists():
            txt = delta.read_text(encoding="utf-8")
            plumb = txt.split("## 1.")[0]
            add("delta §0 plumbing clean", "🚨" not in plumb,
                "sentinels advanced" if "🚨" not in plumb else "🚨 in plumbing — IT refresh may not have landed")
        dash = ROOT / "group" / "wiki" / "dashboard.md"
        upd = ""
        for line in dash.read_text(encoding="utf-8").splitlines()[:4]:
            if line.startswith("updated:"):
                upd = line.split(":", 1)[1].strip()
        add("views regenerated", upd >= sd, f"dashboard.md updated {upd} vs snapshot {sd}")
        board = (ROOT / "board" / "index.html").read_text(encoding="utf-8", errors="replace")
        cq = cutoffs.get("planta", "")
        add("board carries new cutoff", bool(cq) and f"data through {cq}" in board, f"expect 'data through {cq}'")

    add("wiki server :8000", http_ok("http://127.0.0.1:8000/board/index.html"))
    add("control server :8001", http_ok("http://127.0.0.1:8001/status"))

    ok = all(c["ok"] for c in checks)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"checked_at": today.isoformat(), "ok": ok, "checks": checks},
                              ensure_ascii=False, indent=1))
    print(f"CYCLE CHECK {'PASS ✅' if ok else 'FAIL 🚨'} ({today})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
