import os
import smtplib
from contextlib import suppress
from dataclasses import dataclass
from email.header import Header, decode_header
from email.message import Message
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid, parseaddr
from pathlib import Path

import email
import imaplib
from dotenv import load_dotenv


load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD")

SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT") or 587)
IMAP_SERVER = os.getenv("IMAP_SERVER")

TARGET_EMAIL = (
    os.getenv("TARGET_EMAIL")
    or os.getenv("TARGET_FROM_EMAIL")
    or os.getenv("RECEIVER_EMAIL")
)
# Prefer All Mail to avoid missing conversations outside INBOX.
GMAIL_FALLBACK_MAILBOXES = ('"[Gmail]/All Mail"', "inbox", '"[Gmail]/Spam"')


@dataclass(frozen=True)
class ReceivedEmail:
    uid: str
    mailbox: str
    from_addr: str
    subject: str
    message_id: str
    date: str
    body: str
    raw: Message


def require_env() -> None:
    missing = [
        name
        for name, value in {
            "EMAIL_ADDRESS": EMAIL_ADDRESS,
            "EMAIL_APP_PASSWORD": EMAIL_APP_PASSWORD,
            "SMTP_SERVER": SMTP_SERVER,
            "SMTP_PORT": str(SMTP_PORT) if SMTP_PORT else None,
            "IMAP_SERVER": IMAP_SERVER,
            "TARGET_EMAIL": TARGET_EMAIL,
        }.items()
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)}")


def decode_mime_header(value: str) -> str:
    if not value:
        return ""
    decoded_parts = []
    for part, enc in decode_header(value):
        if isinstance(part, bytes):
            decoded_parts.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded_parts.append(part)
    return "".join(decoded_parts)


def extract_text_plain(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disp = str(part.get("Content-Disposition", "")).lower()
            if content_type == "text/plain" and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                if isinstance(payload, bytes):
                    return payload.decode(charset, errors="replace")
                return str(payload or "")

    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    return str(payload or "")


def prompt_multiline(prompt: str, end_token: str = ".") -> str:
    print(prompt)
    print(f"(finish with a single '{end_token}' line)")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == end_token:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def smtp_send(
    to_email: str,
    subject: str,
    body: str,
    *,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> None:
    msg = MIMEText(body, _subtype="plain", _charset="utf-8")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = formataddr((str(Header("Me", "utf-8")), EMAIL_ADDRESS))
    msg["To"] = to_email
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        server.send_message(msg)


def imap_connect() -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL(IMAP_SERVER)
    mail.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
    return mail


def imap_select(mail: imaplib.IMAP4_SSL, mailbox: str) -> None:
    status, _ = mail.select(mailbox)
    if status != "OK":
        raise RuntimeError(f"Failed to select mailbox: {mailbox}")


def imap_search_unseen_from(
    mail: imaplib.IMAP4_SSL, from_email: str, limit: int
) -> list[str]:
    # Use separate search keys (more compatible than a single parenthesized string).
    status, messages = mail.uid("search", None, "UNSEEN", "FROM", f'"{from_email}"')
    if status != "OK":
        raise RuntimeError(f"IMAP search failed: {status}")
    uids = messages[0].split() if messages and messages[0] else []
    return [u.decode() if isinstance(u, bytes) else str(u) for u in uids[-limit:]]


def imap_search_from(mail: imaplib.IMAP4_SSL, from_email: str, limit: int) -> list[str]:
    status, messages = mail.uid("search", None, "FROM", f'"{from_email}"')
    if status != "OK":
        raise RuntimeError(f"IMAP search failed: {status}")
    uids = messages[0].split() if messages and messages[0] else []
    return [u.decode() if isinstance(u, bytes) else str(u) for u in uids[-limit:]]


def imap_fetch(mail: imaplib.IMAP4_SSL, uid: str) -> Message:
    # PEEK avoids setting \Seen on some servers just by fetching.
    _, msg_data = mail.uid("fetch", uid, "(BODY.PEEK[])")
    return email.message_from_bytes(msg_data[0][1])


def imap_mark_seen(mail: imaplib.IMAP4_SSL, uid: str) -> None:
    mail.uid("store", uid, "+FLAGS", "\\Seen")


def summarize(r: ReceivedEmail, *, index: int | None = None) -> None:
    idx = f"[{index}] " if index is not None else ""
    subject = (r.subject or "").strip()
    date = (r.date or "").strip()
    preview = " ".join(((r.body or "").strip()).split())
    preview = preview[:200] + ("..." if len(preview) > 200 else "")
    print(f"{idx}{date} | {subject}")
    print(f"{idx}From: {r.from_addr}")
    print(f"{idx}{preview or '(no text/plain body)'}")


def receive_unread(limit: int) -> list[ReceivedEmail]:
    mail = imap_connect()
    try:

        def _find_unseen_uids() -> tuple[str | None, list[str]]:
            for mailbox in GMAIL_FALLBACK_MAILBOXES:
                with suppress(Exception):
                    imap_select(mail, mailbox)
                    uids = imap_search_unseen_from(mail, TARGET_EMAIL, limit)
                    if uids:
                        return mailbox, uids
            return None, []

        selected, uids = _find_unseen_uids()

        received: list[ReceivedEmail] = []
        for uid in uids:
            msg = imap_fetch(mail, uid)
            from_header = msg.get("From", "")
            from_addr = parseaddr(from_header)[1] or from_header
            subject = decode_mime_header(msg.get("Subject", ""))
            message_id = (msg.get("Message-ID") or "").strip()
            date = (msg.get("Date") or "").strip()
            body = extract_text_plain(msg)
            r = ReceivedEmail(
                uid=uid,
                mailbox=selected or "",
                from_addr=from_addr,
                subject=subject,
                message_id=message_id,
                date=date,
                body=body,
                raw=msg,
            )
            received.append(r)
            imap_mark_seen(mail, uid)
        return received
    finally:
        with suppress(Exception):
            mail.logout()


def inbox_latest(limit: int) -> list[ReceivedEmail]:
    mail = imap_connect()
    try:
        for mailbox in GMAIL_FALLBACK_MAILBOXES:
            with suppress(Exception):
                imap_select(mail, mailbox)
                uids = imap_search_from(mail, TARGET_EMAIL, limit)
                if not uids:
                    continue
                received: list[ReceivedEmail] = []
                for uid in uids:
                    msg = imap_fetch(mail, uid)
                    from_header = msg.get("From", "")
                    from_addr = parseaddr(from_header)[1] or from_header
                    subject = decode_mime_header(msg.get("Subject", ""))
                    message_id = (msg.get("Message-ID") or "").strip()
                    date = (msg.get("Date") or "").strip()
                    body = extract_text_plain(msg)
                    received.append(
                        ReceivedEmail(
                            uid=uid,
                            mailbox=mailbox,
                            from_addr=from_addr,
                            subject=subject,
                            message_id=message_id,
                            date=date,
                            body=body,
                            raw=msg,
                        )
                    )
                return received
        return []
    finally:
        with suppress(Exception):
            mail.logout()


def reply_to_email(
    original: ReceivedEmail, reply_body: str, subject_override: str | None = None
) -> None:
    subj = subject_override or original.subject or ""
    if not subj.lower().startswith("re:"):
        subj = f"Re: {subj}".strip()

    in_reply_to = original.message_id or None
    references = None
    if in_reply_to:
        existing_refs = (original.raw.get("References") or "").strip()
        references = (
            f"{existing_refs} {in_reply_to}".strip() if existing_refs else in_reply_to
        )

    smtp_send(
        TARGET_EMAIL, subj, reply_body, in_reply_to=in_reply_to, references=references
    )


def main() -> None:
    require_env()
    last_inbox: list[ReceivedEmail] = []

    print("Email Terminal (simple)")
    print("")
    print("Commands:")
    print("  send              Send email to target")
    print("  inbox [N]         Show latest N emails from target (default 10)")
    print("  reply <n>         Reply to the nth email from last inbox list")
    print("  quit              Exit")

    while True:
        cmd = (input("\n> ").strip() or "").split()
        if not cmd:
            continue
        c = cmd[0].lower()

        if c in {"quit", "q", "exit"}:
            return

        if c in {"send", "s"}:
            subject = input("Subject: ").strip() or "Hello"
            body = prompt_multiline("Body:") or "(empty)"
            smtp_send(TARGET_EMAIL, subject, body)
            print("Sent.")
            continue

        if c in {"inbox", "i"}:
            limit = int(cmd[1]) if len(cmd) > 1 else 10
            last_inbox = inbox_latest(limit=limit)
            if not last_inbox:
                print("No emails found from target.")
                continue
            for i, rcv in enumerate(last_inbox, start=1):
                summarize(rcv, index=i)
            continue

        if c in {"reply", "r"}:
            if not last_inbox:
                print("Run 'inbox' first.")
                continue
            if len(cmd) < 2:
                print("Usage: reply <n>")
                continue
            try:
                idx = int(cmd[1])
                original = last_inbox[idx - 1]
            except Exception:
                print("Invalid selection.")
                continue
            reply_body = prompt_multiline("Reply body:") or ""
            if not reply_body.strip():
                print("Reply cancelled (empty).")
                continue
            reply_to_email(original, reply_body)
            print("Replied.")
            continue

        print("Unknown command. Use: send, inbox, reply, quit")


if __name__ == "__main__":
    main()
