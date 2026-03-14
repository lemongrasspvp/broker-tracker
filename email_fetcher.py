"""
email_fetcher.py
Connects to iCloud via IMAP, fetches broker emails, extracts text + images,
and groups related emails into threads.
"""

import imaplib
import email
import hashlib
import re
import base64
import os
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime

IMAP_SERVER = "imap.mail.me.com"


def _detect_image_type(data: bytes) -> str:
    """Detect actual image format from magic bytes, ignoring MIME header claims."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"  # safe fallback


def decode_mime_words(s: str) -> str:
    decoded = decode_header(s or "")
    result = []
    for fragment, enc in decoded:
        if isinstance(fragment, bytes):
            result.append(fragment.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(fragment)
    return "".join(result)


def normalize_subject(subject: str) -> str:
    """Strip reply/forward prefixes and normalize for thread matching."""
    s = re.sub(r"^(Re|Fwd|SV|VS|FW|AW)[\s]*[:\-]\s*", "", subject, flags=re.IGNORECASE).strip()
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def parse_email_date(date_str: str) -> datetime:
    try:
        dt = parsedate_to_datetime(date_str)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def extract_text_and_images(msg) -> tuple[str, list[dict]]:
    """
    Returns (plain_text, images) where images is a list of
    {"data": base64_string, "media_type": "image/jpeg"} dicts.
    """
    text_parts = []
    images = []

    def process_part(part):
        ct = part.get_content_type()
        cd = str(part.get("Content-Disposition", ""))

        if ct == "text/plain" and "attachment" not in cd:
            payload = part.get_payload(decode=True)
            if payload:
                text_parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))

        elif ct == "text/html" and "attachment" not in cd and not text_parts:
            payload = part.get_payload(decode=True)
            if payload:
                html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                text = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL)
                text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                text_parts.append(text)

        elif ct.startswith("image/") and len(images) < 8:
            payload = part.get_payload(decode=True)
            if payload and len(payload) > 500:  # skip tiny spacer images
                images.append({
                    "data":       base64.standard_b64encode(payload).decode("utf-8"),
                    "media_type": _detect_image_type(payload),
                })

    if msg.is_multipart():
        for part in msg.walk():
            process_part(part)
    else:
        process_part(msg)

    return "\n".join(text_parts).strip(), images


def fetch_all_broker_emails(icloud_email: str, app_password: str, broker_sender: str) -> list[dict]:
    """
    Fetch all recent broker emails from iCloud.
    Returns list of email dicts sorted oldest→newest.
    """
    all_emails = []

    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER)
        mail.login(icloud_email, app_password)
        mail.select("INBOX")

        search_criteria = f'(FROM "{broker_sender}")' if broker_sender else "ALL"
        _, message_ids = mail.search(None, search_criteria)
        ids = message_ids[0].split()

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Found {len(ids)} broker emails total")

        for eid in ids[-300:]:
            try:
                _, msg_data = mail.fetch(eid, "BODY[]")
                if not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                raw_email = msg_data[0][1]
                if not isinstance(raw_email, bytes):
                    continue

                msg_hash = hashlib.md5(raw_email[:300]).hexdigest()
                msg      = email.message_from_bytes(raw_email)
                subject  = decode_mime_words(msg.get("Subject", "(no subject)"))
                sender   = msg.get("From", "")
                date     = msg.get("Date", "")
                body, images = extract_text_and_images(msg)

                if not body.strip():
                    continue

                all_emails.append({
                    "hash":       msg_hash,
                    "subject":    subject,
                    "sender":     sender,
                    "date":       date,
                    "datetime":   parse_email_date(date),
                    "body":       body[:7000],
                    "images":     images,
                    "thread_key": normalize_subject(subject),
                })

            except Exception as e:
                continue  # skip malformed individual emails

        mail.logout()

    except Exception as e:
        print(f"[ERROR] IMAP fetch failed: {e}")

    all_emails.sort(key=lambda e: e["datetime"])
    return all_emails


def build_threads(all_emails: list[dict]) -> dict[str, list[dict]]:
    """Group emails by normalized subject into threads."""
    threads: dict[str, list[dict]] = {}
    for em in all_emails:
        threads.setdefault(em["thread_key"], []).append(em)
    return threads
