#!/usr/bin/env python3
"""scripts/wiki_ctl.py — tiny control server for the browsable wiki (port 8001).

Gives the web UI its action side: live automation status, refresh buttons, and
in-browser editing of wiki markdown. Stdlib only. Kept alive by serve_wiki.sh
alongside mkdocs (port 8000).

Endpoints:
  GET  /status     JSON — snapshot freshness, git state, refresh-log tail, servers
  POST /rebuild    regenerate board + dashboard pages from local snapshots (~2s)
  POST /snapshot   full chain: snapshot.py && delta.py && dashboard.py (~90s,
                   queries the China standby — locked against concurrent runs)
  GET  /edit?p=    HTML editor for one .md file (whitelisted paths only)
  POST /save       write the file IF changed + git commit it ("ui-edit: ...")

Security model: binds 0.0.0.0 — same LAN/Tailscale trust domain as the wiki
itself (which already renders the financials this can edit). Every save is a
git commit, so any edit is attributable and revertible. Generated pages
(dashboard.md / data_summary.md / board) are not editable — they'd be clobbered
by the next monthly run.
"""
import json
import os
import subprocess
import urllib.parse
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
LOCK = ROOT / "logs" / ".snapshot_running"
PORT = 8001
GENERATED = {"group/wiki/dashboard.md", "group/wiki/data_summary.md"}
NO_EDIT_TOP = {"_archive", "board", "docs", "logs", "site"}


def run(cmd, timeout=600):
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


def latest_snapshot(entity):
    base = ROOT / entity / "snapshots"
    dirs = sorted(d.name for d in base.iterdir()
                  if d.is_dir() and not d.name.startswith(".")) if base.exists() else []
    if not dirs:
        return None, None
    meta = base / dirs[-1] / "00_meta.json"
    cutoff = None
    if meta.exists():
        try:
            cutoff = str(json.loads(meta.read_text()).get("data_cutoff", ""))[:10]
        except Exception:
            pass
    return dirs[-1], cutoff


def status_payload():
    today = date.today()
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    nxt = date(y, m, 3) if today.day >= 3 else date(today.year, today.month, 3)
    snaps = {}
    for e in ("planta", "plantb"):
        d, cut = latest_snapshot(e)
        snaps[e] = {"snapshot": d, "data_through": cut}
    _, git_line = run(["git", "log", "-1", "--format=%h %ad %s", "--date=format:%Y-%m-%d %H:%M"])
    _, porcelain = run(["git", "status", "--porcelain"])
    _, sync = run(["git", "rev-list", "--left-right", "--count", "origin/main...main"])
    log_tail = ""
    rl = ROOT / "logs" / "refresh.log"
    if rl.exists():
        lines = [l for l in rl.read_text(errors="replace").splitlines() if l.strip()]
        log_tail = lines[-1] if lines else ""
    inbox = [str(p.relative_to(ROOT)) for p in (ROOT / "_inbox").rglob("*")
             if p.is_file() and p.name != "README.md"]
    cycle = None
    cc = ROOT / "logs" / "last_cycle_check.json"
    if cc.exists():
        try:
            j = json.loads(cc.read_text())
            fails = [c["name"] for c in j.get("checks", []) if not c["ok"]]
            cycle = {"at": j.get("checked_at"), "ok": j.get("ok"), "failed": fails}
        except Exception:
            pass
    ingest = None
    li = ROOT / "logs" / "last_ingest.txt"
    if li.exists():
        try:
            ingest = li.read_text(encoding="utf-8").strip().splitlines()[0][:240]
        except Exception:
            pass
    return {
        "last_cycle_check": cycle,
        "last_ingest": ingest,
        "snapshots": snaps,
        "next_db_refresh_cron": f"{nxt} 04:00 (Spark)",
        "routines": "verify cycle: Jul 4 (one-time) · wiki ingest: 5th of month 09:00 — both run in the Claude desktop app; reports arrive there",
        "git": {"last_commit": git_line, "dirty_files": len(porcelain.splitlines()),
                "synced_with_origin": sync.strip() == "0\t0"},
        "refresh_log_last_line": log_tail[:300],
        "inbox_pending": inbox,
        "snapshot_running": LOCK.exists(),
        "as_of": today.isoformat(),
    }


def safe_md(rel):
    """Resolve a user-supplied relative path; return (Path|None, error|None)."""
    if not rel or "\x00" in rel:
        return None, "no path"
    p = (ROOT / rel).resolve()
    try:
        rp = p.relative_to(ROOT)
    except ValueError:
        return None, "outside repo"
    parts = rp.parts
    if p.suffix.lower() != ".md":
        return None, "only .md files are editable"
    if any(s.startswith(".") for s in parts):
        return None, "dot-paths not editable"
    if parts and parts[0] in NO_EDIT_TOP:
        return None, f"'{parts[0]}/' is not editable from the UI"
    if rp.as_posix() in GENERATED:
        return None, ("this page is AUTO-GENERATED by scripts/dashboard.py — edits would be "
                      "clobbered by the next monthly run; change the generator instead")
    if not p.exists():
        return None, "file does not exist (UI editing is for existing pages)"
    return p, None


EDIT_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>edit · {rel}</title>
<style>body{{margin:0;background:#f2f4f8;font-family:-apple-system,'Segoe UI',Roboto,sans-serif;color:#1c2733}}
.wrap{{max-width:980px;margin:0 auto;padding:18px}}h1{{font-size:16px;margin:0 0 4px}}
.hint{{font-size:12.5px;color:#67768a;margin-bottom:10px}}
textarea{{width:100%;height:72vh;font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;border:1px solid #d6dde6;
border-radius:10px;padding:14px;background:#fff;box-sizing:border-box}}
.row{{display:flex;gap:10px;align-items:center;margin-top:10px}}
button{{background:#3563d8;color:#fff;border:0;border-radius:8px;padding:9px 18px;font-size:14px;cursor:pointer}}
button:hover{{background:#2b51b4}}a{{color:#3563d8;text-decoration:none}}#msg{{font-size:13px}}</style></head>
<body><div class="wrap"><h1>✏️ {rel}</h1>
<div class="hint">Saving writes the file and creates a git commit ("ui-edit"). If you're changing a
factual claim, keep the wiki convention: add a line like
<code>&gt; **Revised {today}:** prior claim was X; now Y because Z.</code>
The rendered page (port 8000) refreshes itself a few seconds after saving.</div>
<form id="f"><textarea name="content" spellcheck="false">{content}</textarea>
<div class="row"><button type="submit">Save + commit</button>
<a href="{back}">← back to rendered page</a><span id="msg"></span></div>
<input type="hidden" name="p" value="{rel}"></form>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const fd = new FormData(e.target);
  const r = await fetch('/save', {{method:'POST', body: new URLSearchParams(fd)}});
  document.getElementById('msg').textContent = await r.text();
}});
</script></div></body></html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # quiet; serve_wiki.log gets enough
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/status":
            self._send(200, json.dumps(status_payload(), ensure_ascii=False, indent=1),
                       "application/json; charset=utf-8")
        elif u.path == "/edit":
            rel = urllib.parse.parse_qs(u.query).get("p", [""])[0]
            p, err = safe_md(rel)
            if err:
                self._send(403, f"cannot edit: {err}")
                return
            rp = p.relative_to(ROOT).as_posix()
            back = f"http://{self.headers.get('Host', '').split(':')[0]}:8000/" \
                   + rp[:-3] + ".html"
            html = EDIT_HTML.format(rel=rp, content=p.read_text(encoding="utf-8")
                                    .replace("&", "&amp;").replace("<", "&lt;"),
                                    back=back, today=date.today().isoformat())
            self._send(200, html, "text/html; charset=utf-8")
        else:
            self._send(404, "wiki_ctl: /status /edit?p= (GET) · /rebuild /snapshot /save (POST)")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode() if n else ""
        if self.path == "/rebuild":
            code, out = run([PY, "scripts/dashboard.py"], timeout=120)
            self._send(200 if code == 0 else 500,
                       ("rebuilt ✓ — " if code == 0 else "FAILED — ") + out.splitlines()[-1] if out else "")
        elif self.path == "/snapshot":
            if LOCK.exists():
                self._send(409, "a snapshot run is already in progress — try again in ~2 minutes")
                return
            LOCK.parent.mkdir(exist_ok=True)
            LOCK.write_text(str(os.getpid()))
            try:
                code, out = run([PY, "scripts/snapshot.py"], timeout=600)
                if code == 0:
                    run([PY, "scripts/delta.py"], timeout=120)
                    run([PY, "scripts/dashboard.py"], timeout=120)
                tail = "\n".join(out.splitlines()[-6:])
                self._send(200 if code == 0 else 500,
                           ("snapshot chain done ✓\n" if code == 0 else "snapshot FAILED\n") + tail)
            finally:
                LOCK.unlink(missing_ok=True)
        elif self.path == "/save":
            form = urllib.parse.parse_qs(body)
            rel = form.get("p", [""])[0]
            content = form.get("content", [""])[0]
            p, err = safe_md(rel)
            if err:
                self._send(403, f"not saved: {err}")
                return
            if p.read_text(encoding="utf-8") == content:
                self._send(200, "no changes — nothing saved")
                return
            p.write_text(content, encoding="utf-8")
            rp = p.relative_to(ROOT).as_posix()
            run(["git", "add", rp])
            code, out = run(["git", "commit", "-m", f"ui-edit: {rp} (via wiki UI)"])
            self._send(200, f"saved + committed ✓ ({rp})" if code == 0
                       else f"saved, but commit failed: {out[-200:]}")
        else:
            self._send(404, "unknown endpoint")


if __name__ == "__main__":
    print(f"wiki_ctl on :{PORT} (root {ROOT})")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
