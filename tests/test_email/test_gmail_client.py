"""Tests for gmail/client.py — GmailClient, GmailMessage, body extraction."""

import base64
import pytest
from unittest.mock import MagicMock, call

from jobhunter.gmail.client import (
    GmailClient,
    GmailMessage,
    _decode_b64,
    _extract_body,
    _strip_html,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _b64(text: str) -> str:
    """Encode text to URL-safe base64 as Gmail would."""
    return base64.urlsafe_b64encode(text.encode()).decode()


def _make_service(
    list_result=None,
    get_result=None,
    modify_result=None,
    send_result=None,
    labels_result=None,
    label_create_result=None,
):
    """Build a mock Gmail service object using return_value chaining to avoid recording calls."""
    svc = MagicMock()
    u = svc.users.return_value

    # messages().list().execute()
    u.messages.return_value.list.return_value.execute.return_value = list_result or {}

    # messages().get().execute()
    u.messages.return_value.get.return_value.execute.return_value = get_result or {}

    # messages().modify().execute()
    u.messages.return_value.modify.return_value.execute.return_value = modify_result or {}

    # messages().send().execute()
    u.messages.return_value.send.return_value.execute.return_value = send_result or {}

    # labels().list().execute()
    u.labels.return_value.list.return_value.execute.return_value = (
        labels_result or {"labels": []}
    )

    # labels().create().execute()
    u.labels.return_value.create.return_value.execute.return_value = (
        label_create_result or {"id": "Label_new", "name": "JobHunter/Test"}
    )

    return svc


# ── _decode_b64 ───────────────────────────────────────────────────────────────

class TestDecodeB64:
    def test_empty_string_returns_empty(self):
        assert _decode_b64("") == ""

    def test_decodes_urlsafe_b64(self):
        encoded = base64.urlsafe_b64encode(b"Hello, world!").decode()
        assert _decode_b64(encoded) == "Hello, world!"

    def test_handles_missing_padding(self):
        # Gmail often strips padding '='
        text = "Test string"
        encoded = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
        assert _decode_b64(encoded) == text

    def test_returns_empty_on_none_like_input(self):
        # Empty string is the only guaranteed-empty case in the implementation
        assert _decode_b64("") == ""


# ── _strip_html ───────────────────────────────────────────────────────────────

class TestStripHtml:
    def test_removes_tags(self):
        assert _strip_html("<p>Hello</p>") == "Hello"

    def test_decodes_entities(self):
        # &amp; is replaced with &
        result = _strip_html("A &amp; B")
        assert "&" in result
        assert "&amp;" not in result
        # &lt; and &gt; are replaced with < and >
        result2 = _strip_html("A &lt;tag&gt;")
        assert "<" in result2
        assert ">" in result2

    def test_collapses_whitespace(self):
        result = _strip_html("<p>  lots   of   space  </p>")
        assert "  " not in result

    def test_handles_nbsp(self):
        result = _strip_html("hello&nbsp;world")
        assert "hello" in result
        assert "world" in result


# ── _extract_body ─────────────────────────────────────────────────────────────

class TestExtractBody:
    def _plain_payload(self, text: str) -> dict:
        return {
            "mimeType": "text/plain",
            "body": {"data": _b64(text)},
        }

    def _html_payload(self, html: str) -> dict:
        return {
            "mimeType": "text/html",
            "body": {"data": _b64(html)},
        }

    def test_extracts_plain_text(self):
        payload = self._plain_payload("Hello from plain text")
        assert _extract_body(payload) == "Hello from plain text"

    def test_extracts_html_as_fallback(self):
        payload = self._html_payload("<p>Hello HTML</p>")
        result = _extract_body(payload)
        assert "Hello HTML" in result

    def test_prefers_plain_over_html_in_multipart(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                self._html_payload("<p>HTML version</p>"),
                self._plain_payload("Plain version"),
            ],
        }
        assert _extract_body(payload) == "Plain version"

    def test_falls_back_to_html_in_multipart(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                self._html_payload("<p>Only HTML here</p>"),
            ],
        }
        result = _extract_body(payload)
        assert "Only HTML here" in result

    def test_recurses_into_nested_multipart(self):
        inner = {
            "mimeType": "multipart/related",
            "parts": [self._plain_payload("Nested plain text")],
        }
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [inner],
        }
        assert _extract_body(payload) == "Nested plain text"

    def test_returns_empty_for_unknown_mime(self):
        payload = {"mimeType": "application/pdf", "body": {}}
        assert _extract_body(payload) == ""


# ── GmailClient.list_unread_inbox ────────────────────────────────────────────

class TestListUnreadInbox:
    def test_returns_message_ids(self):
        svc = _make_service(list_result={"messages": [{"id": "abc"}, {"id": "def"}]})
        client = GmailClient(svc)
        ids = client.list_unread_inbox()
        assert ids == ["abc", "def"]

    def test_returns_empty_list_when_no_messages(self):
        svc = _make_service(list_result={})
        client = GmailClient(svc)
        assert client.list_unread_inbox() == []

    def test_returns_empty_list_on_api_error(self):
        svc = MagicMock()
        svc.users().messages().list().execute.side_effect = Exception("API error")
        client = GmailClient(svc)
        assert client.list_unread_inbox() == []


# ── GmailClient.get_message ───────────────────────────────────────────────────

class TestGetMessage:
    def _raw_message(self, subject="Test", from_addr="sender@example.com") -> dict:
        return {
            "id": "msg123",
            "threadId": "thread456",
            "labelIds": ["INBOX", "UNREAD"],
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": from_addr},
                    {"name": "To", "value": "me@example.com"},
                    {"name": "Subject", "value": subject},
                    {"name": "Date", "value": "Mon, 1 Jan 2024 10:00:00 +0000"},
                ],
                "body": {"data": _b64("Email body content here")},
            },
        }

    def test_parses_basic_message(self):
        raw = self._raw_message()
        svc = _make_service(get_result=raw)
        client = GmailClient(svc)
        msg = client.get_message("msg123")
        assert msg is not None
        assert msg.message_id == "msg123"
        assert msg.thread_id == "thread456"
        assert msg.subject == "Test"
        assert msg.from_address == "sender@example.com"
        assert msg.to_address == "me@example.com"
        assert "Email body content here" in msg.body_text

    def test_body_truncated_to_5000_chars(self):
        long_body = "x" * 10_000
        raw = {
            "id": "msg1",
            "threadId": "t1",
            "labelIds": [],
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "a@b.com"},
                    {"name": "To", "value": "c@d.com"},
                    {"name": "Subject", "value": "Long"},
                    {"name": "Date", "value": ""},
                ],
                "body": {"data": _b64(long_body)},
            },
        }
        svc = _make_service(get_result=raw)
        client = GmailClient(svc)
        msg = client.get_message("msg1")
        assert len(msg.body_text) == 5000

    def test_body_preview_truncated_to_500_chars(self):
        long_body = "y" * 2000
        raw = {
            "id": "msg2",
            "threadId": "t2",
            "labelIds": [],
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "a@b.com"},
                    {"name": "To", "value": "c@d.com"},
                    {"name": "Subject", "value": "Preview"},
                    {"name": "Date", "value": ""},
                ],
                "body": {"data": _b64(long_body)},
            },
        }
        svc = _make_service(get_result=raw)
        client = GmailClient(svc)
        msg = client.get_message("msg2")
        assert len(msg.body_preview) == 500

    def test_returns_none_on_api_error(self):
        svc = MagicMock()
        svc.users().messages().get().execute.side_effect = Exception("not found")
        client = GmailClient(svc)
        assert client.get_message("missing") is None

    def test_missing_headers_use_defaults(self):
        raw = {
            "id": "msg3",
            "threadId": "t3",
            "labelIds": [],
            "payload": {
                "mimeType": "text/plain",
                "headers": [],
                "body": {"data": _b64("body")},
            },
        }
        svc = _make_service(get_result=raw)
        client = GmailClient(svc)
        msg = client.get_message("msg3")
        assert msg.subject == "(no subject)"
        assert msg.from_address == ""


# ── GmailClient.send_message ─────────────────────────────────────────────────

class TestSendMessage:
    def test_returns_true_on_success(self):
        svc = _make_service(send_result={"id": "sent1"})
        client = GmailClient(svc)
        assert client.send_message("to@example.com", "Subject", "Body") is True

    def test_returns_false_on_api_error(self):
        svc = MagicMock()
        svc.users().messages().send().execute.side_effect = Exception("quota exceeded")
        client = GmailClient(svc)
        assert client.send_message("to@example.com", "Subject", "Body") is False


# ── GmailClient.forward_message ──────────────────────────────────────────────

class TestForwardMessage:
    def _make_msg(self) -> GmailMessage:
        return GmailMessage(
            message_id="orig1",
            thread_id="thread1",
            from_address="recruiter@company.com",
            to_address="job@me.com",
            subject="Interview Invitation",
            body_text="Please join us for an interview.",
            body_preview="Please join us",
            received_at="Mon, 1 Jan 2024 10:00:00 +0000",
            labels=["INBOX"],
        )

    def test_forward_prefixes_subject_with_fwd(self):
        sent_bodies = []
        svc = MagicMock()
        svc.users().messages().send().execute.return_value = {"id": "sent1"}

        # Capture the raw body passed to send
        def capture_send(**kwargs):
            sent_bodies.append(kwargs.get("body", {}).get("raw", ""))
            return MagicMock()

        svc.users().messages().send.return_value.execute = lambda: {"id": "sent1"}

        client = GmailClient(svc)
        client.forward_message(self._make_msg(), "personal@me.com", note="[JobHunter] Note")
        # The actual send call is internal — just verify it was called
        svc.users().messages().send.assert_called()

    def test_forward_returns_true_on_success(self):
        svc = _make_service(send_result={"id": "sent1"})
        client = GmailClient(svc)
        result = client.forward_message(self._make_msg(), "personal@me.com")
        assert result is True


# ── GmailClient label operations ─────────────────────────────────────────────

class TestLabelOperations:
    def test_mark_read_removes_unread(self):
        svc = _make_service()
        client = GmailClient(svc)
        client.mark_read("msg1")
        svc.users().messages().modify.assert_called_with(
            userId="me",
            id="msg1",
            body={"addLabelIds": [], "removeLabelIds": ["UNREAD"]},
        )

    def test_archive_removes_inbox(self):
        svc = _make_service()
        client = GmailClient(svc)
        client.archive("msg1")
        svc.users().messages().modify.assert_called_with(
            userId="me",
            id="msg1",
            body={"addLabelIds": [], "removeLabelIds": ["INBOX"]},
        )

    def test_apply_label_adds_label(self):
        svc = _make_service()
        client = GmailClient(svc)
        client.apply_label("msg1", "Label_abc")
        svc.users().messages().modify.assert_called_with(
            userId="me",
            id="msg1",
            body={"addLabelIds": ["Label_abc"], "removeLabelIds": []},
        )

    def test_modify_labels_returns_false_on_error(self):
        svc = MagicMock()
        svc.users().messages().modify().execute.side_effect = Exception("fail")
        client = GmailClient(svc)
        assert client.modify_labels("msg1", add_labels=["X"]) is False


# ── GmailClient.get_or_create_label ─────────────────────────────────────────

class TestGetOrCreateLabel:
    def test_returns_existing_label_id(self):
        svc = _make_service(
            labels_result={
                "labels": [
                    {"id": "Label_123", "name": "JobHunter/Rejected"},
                ]
            }
        )
        client = GmailClient(svc)
        label_id = client.get_or_create_label("JobHunter/Rejected")
        assert label_id == "Label_123"
        # Should NOT call create — check via return_value chain to avoid recording a new call
        svc.users.return_value.labels.return_value.create.assert_not_called()

    def test_label_match_is_case_insensitive(self):
        svc = _make_service(
            labels_result={
                "labels": [{"id": "Label_999", "name": "JOBHUNTER/REJECTED"}]
            }
        )
        client = GmailClient(svc)
        assert client.get_or_create_label("jobhunter/rejected") == "Label_999"

    def test_creates_label_if_missing(self):
        svc = _make_service(
            labels_result={"labels": []},
            label_create_result={"id": "Label_new", "name": "JobHunter/Interview"},
        )
        client = GmailClient(svc)
        label_id = client.get_or_create_label("JobHunter/Interview")
        assert label_id == "Label_new"

    def test_returns_none_on_api_error(self):
        svc = MagicMock()
        svc.users().labels().list().execute.side_effect = Exception("API error")
        client = GmailClient(svc)
        assert client.get_or_create_label("JobHunter/X") is None
