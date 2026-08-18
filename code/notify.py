"""Email notify — Phase 3 (NOT YET DEPLOYED).

Sends notification emails from the dedicated ClientCo-Claude Gmail back to
David's primary inbox (`user@clientco.com`). Used by other scripts
(`snapshot.py`, `delta.py`, snippet-flow scripts) to ping David when
something happens.

## Usage

Programmatic:

    from scripts.notify import send_notification
    send_notification(
        subject="Delta ready — 3 watchlist hits",
        body="See _inbox/db_deltas/2026-05-11.md ...",
        attachments=[Path("/path/to/file.md")],  # optional
    )

CLI (for one-off / cron jobs):

    ./.venv/bin/python scripts/notify.py --subject "..." --body "..."

## Setup (NOT YET DEPLOYED)

Same Gmail account + app password as `pull_email.py`. Add to `.env`:

    NOTIFY_FROM=your-dedicated-address@gmail.com    # same as INBOX_EMAIL is fine
    NOTIFY_TO=user@clientco.com
    NOTIFY_SMTP_HOST=smtp.gmail.com
    NOTIFY_SMTP_PORT=465
    NOTIFY_APP_PASSWORD=...  # can reuse INBOX_APP_PASSWORD if same account

## Notification policy

Per the session conversation 2026-05-11, three triggers:
1. Monthly delta landed — once on the 5th: "delta report ready, N pages flagged"
2. Snippet sweep processed — confirmation w/ affected-pages list
3. Watchlist hit — e.g., "FDFM2JA* first appeared", "emBOI non-empty", etc.

Everything else stays silent.
"""
from __future__ import annotations

import argparse
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def send_notification(
    subject: str,
    body: str,
    attachments: list[Path] | None = None,
    to: str | None = None,
) -> bool:
    """Send a notification email. Returns True on success, False otherwise.

    Reads SMTP config from environment (see module docstring). If config is
    missing, logs to stderr and returns False (so calling scripts can fail
    gracefully without raising).
    """
    sender = os.environ.get("NOTIFY_FROM") or os.environ.get("INBOX_EMAIL")
    pw = os.environ.get("NOTIFY_APP_PASSWORD") or os.environ.get("INBOX_APP_PASSWORD")
    recipient = to or os.environ.get("NOTIFY_TO")
    host = os.environ.get("NOTIFY_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("NOTIFY_SMTP_PORT", "465"))

    if not (sender and pw and recipient):
        print(
            "WARN: notification skipped — NOTIFY_FROM/TO/APP_PASSWORD not configured. "
            "See scripts/notify.py docstring.",
            file=sys.stderr,
        )
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)

    for path in attachments or []:
        if not path.exists():
            continue
        data = path.read_bytes()
        # Best-effort MIME guess; fall back to octet-stream
        maintype, subtype = "application", "octet-stream"
        if path.suffix.lower() in (".md", ".txt"):
            maintype, subtype = "text", "plain"
        elif path.suffix.lower() == ".csv":
            maintype, subtype = "text", "csv"
        elif path.suffix.lower() == ".pdf":
            maintype, subtype = "application", "pdf"
        elif path.suffix.lower() in (".png", ".jpg", ".jpeg"):
            maintype, subtype = "image", path.suffix.lstrip(".").lower()
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(host, port, context=context) as smtp:
            smtp.login(sender, pw)
            smtp.send_message(msg)
    except Exception as e:
        print(f"ERROR: notification failed: {e}", file=sys.stderr)
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description="Send a notification email.")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--body", required=True)
    ap.add_argument("--to", default=None)
    ap.add_argument("--attach", action="append", default=[], help="File to attach (repeatable)")
    args = ap.parse_args()

    attachments = [Path(p) for p in args.attach]
    ok = send_notification(args.subject, args.body, attachments=attachments, to=args.to)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
