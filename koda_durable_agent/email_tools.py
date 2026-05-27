"""
Email tools — IMAP inbox triage and search.

Works with any IMAP provider:
  - Microsoft Outlook / Office 365: imap.office365.com
  - Gmail: imap.gmail.com  (requires App Password if 2FA is on)
  - Yahoo: imap.mail.yahoo.com
  - Any other IMAP server

Read-only by design. Koda never sends email without a separate confirmed action.
"""
import email
import email.header
import imaplib
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("koda.email")

_PROVIDERS = {
    "outlook": "imap.office365.com",
    "office365": "imap.office365.com",
    "hotmail": "imap.office365.com",
    "gmail": "imap.gmail.com",
    "yahoo": "imap.mail.yahoo.com",
    "icloud": "imap.mail.me.com",
}


def _get_email_config() -> dict:
    cfg = {
        "address": os.environ.get("EMAIL_ADDRESS", ""),
        "password": os.environ.get("EMAIL_PASSWORD", ""),
        "server": os.environ.get("EMAIL_IMAP_SERVER", ""),
    }
    if not cfg["address"] or not cfg["password"]:
        env_path = Path.home() / ".koda" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                for key in ("EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_IMAP_SERVER"):
                    if line.startswith(f"{key}="):
                        cfg[key.lower()] = line.split("=", 1)[1].strip()
    # Auto-detect IMAP server from email domain if not set
    if not cfg["server"] and cfg["address"] and "@" in cfg["address"]:
        domain = cfg["address"].split("@")[1].lower().split(".")[0]
        cfg["server"] = _PROVIDERS.get(domain, "")
    return cfg


def _decode_header(value: str) -> str:
    parts = email.header.decode_header(value)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)


def _connect(cfg: dict):
    if not cfg["address"] or not cfg["password"]:
        return None, "Email not configured. Add EMAIL_ADDRESS and EMAIL_PASSWORD to ~/.koda/.env"
    if not cfg["server"]:
        return None, (
            f"Could not detect IMAP server for {cfg['address']}. "
            "Add EMAIL_IMAP_SERVER to ~/.koda/.env (e.g. imap.office365.com)"
        )
    try:
        mail = imaplib.IMAP4_SSL(cfg["server"], 993)
        mail.login(cfg["address"], cfg["password"])
        return mail, None
    except imaplib.IMAP4.error as e:
        msg = str(e)
        if "authentication" in msg.lower() or "invalid" in msg.lower():
            hint = ""
            if "gmail" in cfg["server"]:
                hint = " For Gmail with 2FA, use an App Password from myaccount.google.com/apppasswords."
            elif "office365" in cfg["server"]:
                hint = " For Outlook, use an App Password or check that IMAP is enabled in account settings."
            return None, f"Login failed: {msg}.{hint}"
        return None, f"Connection failed: {msg}"
    except Exception as e:
        return None, f"Could not connect to {cfg['server']}: {e}"


def triage_inbox(days: int = 1, max_emails: int = 30) -> str:
    """Triage the email inbox — surface what needs attention and suppress noise.

    Scans recent emails, identifies what requires a response or action, and
    filters out newsletters, notifications, and automated messages.

    Args:
        days: How many days back to scan (default 1 = today).
        max_emails: Maximum emails to fetch (default 30).
    """
    cfg = _get_email_config()
    mail, err = _connect(cfg)
    if err:
        return err

    try:
        mail.select("INBOX")
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        _, data = mail.search(None, f'(SINCE "{since}")')
        ids = data[0].split()
        if not ids:
            return f"No emails in the last {days} day(s)."

        ids = ids[-max_emails:]
        emails = []
        for eid in reversed(ids):
            _, msg_data = mail.fetch(eid, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            subject = _decode_header(msg.get("Subject", "(no subject)"))
            sender = _decode_header(msg.get("From", ""))
            date = msg.get("Date", "")[:16]
            body = _extract_body(msg)
            emails.append({"subject": subject, "from": sender, "date": date, "body": body})

        mail.logout()

        if not emails:
            return "Inbox is empty for that period."

        lines = [f"Inbox — last {days} day(s) — {len(emails)} email(s)\n"]
        needs_action = []
        noise = []

        for e in emails:
            if _is_noise(e):
                noise.append(e)
            else:
                needs_action.append(e)

        if needs_action:
            lines.append(f"⚡ NEEDS ATTENTION ({len(needs_action)})\n")
            for e in needs_action:
                lines.append(f"  From: {e['from'][:60]}")
                lines.append(f"  Subject: {e['subject'][:80]}")
                lines.append(f"  Date: {e['date']}")
                preview = e["body"][:200].replace("\n", " ").strip()
                if preview:
                    lines.append(f"  Preview: {preview}")
                lines.append("")
        else:
            lines.append("✓ Nothing urgent — all clear.\n")

        if noise:
            lines.append(f"[{len(noise)} newsletter/notification/automated email(s) suppressed]")

        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"triage_inbox error: {e}")
        return f"Error reading inbox: {e}"


def search_emails(query: str, max_results: int = 10) -> str:
    """Search emails by keyword, sender, or subject.

    Args:
        query: Search term. Can be a name, email address, subject keyword, or phrase.
        max_results: Maximum results to return (default 10).
    """
    cfg = _get_email_config()
    mail, err = _connect(cfg)
    if err:
        return err

    try:
        mail.select("INBOX")
        # Try subject + from search
        _, data = mail.search(None, f'(OR SUBJECT "{query}" FROM "{query}")')
        ids = data[0].split()
        if not ids:
            # Fallback: text search
            _, data = mail.search(None, f'TEXT "{query}"')
            ids = data[0].split()

        if not ids:
            mail.logout()
            return f"No emails found matching '{query}'."

        ids = ids[-max_results:]
        lines = [f"Search results for '{query}' — {len(ids)} found\n"]
        for eid in reversed(ids):
            _, msg_data = mail.fetch(eid, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            subject = _decode_header(msg.get("Subject", "(no subject)"))
            sender = _decode_header(msg.get("From", ""))
            date = msg.get("Date", "")[:16]
            body = _extract_body(msg)
            preview = body[:150].replace("\n", " ").strip()
            lines.append(f"  From: {sender[:60]}")
            lines.append(f"  Subject: {subject[:80]}")
            lines.append(f"  Date: {date}")
            if preview:
                lines.append(f"  Preview: {preview}")
            lines.append("")

        mail.logout()
        return "\n".join(lines)
    except Exception as e:
        return f"Search failed: {e}"


def read_email(subject_or_id: str) -> str:
    """Read the full body of an email by subject keyword.

    Args:
        subject_or_id: Subject keyword to find the most recent matching email.
    """
    cfg = _get_email_config()
    mail, err = _connect(cfg)
    if err:
        return err

    try:
        mail.select("INBOX")
        _, data = mail.search(None, f'SUBJECT "{subject_or_id}"')
        ids = data[0].split()
        if not ids:
            mail.logout()
            return f"No email found with subject matching '{subject_or_id}'."

        _, msg_data = mail.fetch(ids[-1], "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])
        subject = _decode_header(msg.get("Subject", "(no subject)"))
        sender = _decode_header(msg.get("From", ""))
        date = msg.get("Date", "")
        body = _extract_body(msg)
        mail.logout()

        return (
            f"From: {sender}\n"
            f"Subject: {subject}\n"
            f"Date: {date}\n"
            f"{'─' * 40}\n"
            f"{body}"
        )
    except Exception as e:
        return f"Could not read email: {e}"


def _extract_body(msg) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                try:
                    charset = part.get_content_charset() or "utf-8"
                    body = part.get_payload(decode=True).decode(charset, errors="replace")
                    break
                except Exception:
                    pass
    else:
        try:
            charset = msg.get_content_charset() or "utf-8"
            body = msg.get_payload(decode=True).decode(charset, errors="replace")
        except Exception:
            pass
    return body.strip()


def _is_noise(e: dict) -> bool:
    noise_keywords = [
        "unsubscribe", "newsletter", "no-reply", "noreply", "donotreply",
        "notifications@", "mailer@", "automated", "do not reply",
        "your receipt", "order confirmation", "shipping notification",
        "password reset", "verify your", "account alert", "security alert",
    ]
    text = (e["subject"] + " " + e["from"] + " " + e["body"][:200]).lower()
    return any(kw in text for kw in noise_keywords)
