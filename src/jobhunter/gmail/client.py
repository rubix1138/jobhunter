"""Gmail API wrapper — list, get, send, label, archive."""

import base64
import email as email_lib
import email.mime.text
import email.mime.multipart
from dataclasses import dataclass
from typing import Optional

from ..utils.logging import get_logger

logger = get_logger(__name__)

_USER = "me"


@dataclass
class GmailMessage:
    message_id: str          # Gmail message ID
    thread_id: str
    from_address: str
    to_address: str
    subject: str
    body_text: str           # plain-text body (truncated to 5000 chars)
    body_preview: str        # first 500 chars
    received_at: str         # RFC 2822 date string
    labels: list[str]


class GmailClient:
    """
    Thin wrapper around the Gmail v1 API.

    All methods are synchronous (the Gmail client library is not async).
    Run in a thread pool executor if you need non-blocking behaviour.
    """

    def __init__(self, service) -> None:
        self._svc = service

    # ── Reading ───────────────────────────────────────────────────────────────

    def list_unread_inbox(self, max_results: int = 50) -> list[str]:
        """Return a list of unread message IDs from the inbox."""
        try:
            result = self._svc.users().messages().list(
                userId=_USER,
                q="is:unread in:inbox",
                maxResults=max_results,
            ).execute()
            messages = result.get("messages", [])
            return [m["id"] for m in messages]
        except Exception as e:
            logger.error(f"Failed to list Gmail messages: {e}")
            return []

    def search_messages(
        self, query: str, max_results: int = 10, include_spam_trash: bool = False
    ) -> list[str]:
        """Return message IDs matching the given Gmail search query."""
        try:
            result = self._svc.users().messages().list(
                userId=_USER,
                q=query,
                maxResults=max_results,
                includeSpamTrash=include_spam_trash,
            ).execute()
            messages = result.get("messages", [])
            return [m["id"] for m in messages]
        except Exception as e:
            logger.error(f"Failed to search Gmail messages ({query!r}): {e}")
            return []

    def get_message(self, message_id: str) -> Optional[GmailMessage]:
        """Fetch and parse a full Gmail message."""
        try:
            raw = self._svc.users().messages().get(
                userId=_USER,
                id=message_id,
                format="full",
            ).execute()
            return self._parse_message(raw)
        except Exception as e:
            logger.error(f"Failed to get Gmail message {message_id}: {e}")
            return None

    def _parse_message(self, raw: dict) -> GmailMessage:
        headers = {
            h["name"].lower(): h["value"]
            for h in raw.get("payload", {}).get("headers", [])
        }
        body = _extract_body(raw.get("payload", {}))

        return GmailMessage(
            message_id=raw["id"],
            thread_id=raw.get("threadId", ""),
            from_address=headers.get("from", ""),
            to_address=headers.get("to", ""),
            subject=headers.get("subject", "(no subject)"),
            body_text=body[:5000],
            body_preview=body[:500],
            received_at=headers.get("date", ""),
            labels=raw.get("labelIds", []),
        )

    # ── Sending ───────────────────────────────────────────────────────────────

    def send_message(self, to: str, subject: str, body: str) -> bool:
        """Send a plain-text email. Returns True on success."""
        try:
            msg = email.mime.text.MIMEText(body, "plain", "utf-8")
            msg["To"] = to
            msg["Subject"] = subject
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            self._svc.users().messages().send(
                userId=_USER, body={"raw": raw}
            ).execute()
            logger.info(f"Email sent to {to}: {subject!r}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email to {to}: {e}")
            return False

    def forward_message(
        self,
        original: GmailMessage,
        to: str,
        note: str = "",
    ) -> bool:
        """Forward a message to another address with optional prepended note."""
        subject = f"FWD: {original.subject}"
        body_parts = []
        if note:
            body_parts.append(note + "\n\n")
        body_parts.append("---------- Forwarded message ----------\n")
        body_parts.append(f"From: {original.from_address}\n")
        body_parts.append(f"Date: {original.received_at}\n")
        body_parts.append(f"Subject: {original.subject}\n\n")
        body_parts.append(original.body_text)
        return self.send_message(to, subject, "".join(body_parts))

    # ── Labels ────────────────────────────────────────────────────────────────

    def modify_labels(
        self,
        message_id: str,
        add_labels: Optional[list[str]] = None,
        remove_labels: Optional[list[str]] = None,
    ) -> bool:
        """Add and/or remove labels on a message. Returns True on success."""
        try:
            self._svc.users().messages().modify(
                userId=_USER,
                id=message_id,
                body={
                    "addLabelIds": add_labels or [],
                    "removeLabelIds": remove_labels or [],
                },
            ).execute()
            return True
        except Exception as e:
            logger.error(f"Failed to modify labels on {message_id}: {e}")
            return False

    def mark_read(self, message_id: str) -> bool:
        return self.modify_labels(message_id, remove_labels=["UNREAD"])

    def archive(self, message_id: str) -> bool:
        """Move message out of inbox (archive it)."""
        return self.modify_labels(message_id, remove_labels=["INBOX"])

    def apply_label(self, message_id: str, label_id: str) -> bool:
        return self.modify_labels(message_id, add_labels=[label_id])

    def get_or_create_label(self, name: str) -> Optional[str]:
        """Return the label ID for `name`, creating it if it doesn't exist."""
        try:
            labels = self._svc.users().labels().list(userId=_USER).execute()
            for label in labels.get("labels", []):
                if label["name"].lower() == name.lower():
                    return label["id"]
            # Create it
            created = self._svc.users().labels().create(
                userId=_USER,
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            ).execute()
            logger.info(f"Created Gmail label: {name!r} ({created['id']})")
            return created["id"]
        except Exception as e:
            logger.error(f"Failed to get/create label {name!r}: {e}")
            return None


# ── Body extraction ───────────────────────────────────────────────────────────

def _extract_body(payload: dict) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return _decode_b64(data)

    if mime_type == "text/html":
        # Use HTML only as a last resort — strip tags
        data = payload.get("body", {}).get("data", "")
        return _strip_html(_decode_b64(data))

    parts = payload.get("parts", [])
    # Prefer text/plain parts
    for part in parts:
        if part.get("mimeType") == "text/plain":
            return _extract_body(part)
    # Recurse into multipart
    for part in parts:
        text = _extract_body(part)
        if text:
            return text
    return ""


def _decode_b64(data: str) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    except Exception:
        return ""


def _strip_html(html: str) -> str:
    """Very basic HTML tag stripper (no dependencies)."""
    import re
    # Preserve anchor URLs before stripping tags so callers can extract links.
    text = re.sub(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        r" \2 (\1) ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Preserve other href/src URLs that may appear in non-anchor tags.
    text = re.sub(
        r'(?:href|src)=["\']([^"\']+)["\']',
        r" \1 ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()
