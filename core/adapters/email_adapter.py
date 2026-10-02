"""Email adapter — SMTP (send) + IMAP (fetch).

Capabilities exposed as tools:

- ``email_send``       send a plaintext or HTML email.
- ``email_list``       list recent messages (subject + from + date).
- ``email_fetch``      fetch one message body by UID.
- ``email_status``     SMTP/IMAP reachability probe.

Credentials (env or keystore):

    SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS, SMTP_FROM
    IMAP_HOST, IMAP_PORT (default 993), IMAP_USER, IMAP_PASS

A single ``EMAIL_USER`` / ``EMAIL_PASS`` pair is used as fallback for
both transports when the per-transport vars aren't set.
"""

from __future__ import annotations

import email
import imaplib
import smtplib
from email.message import EmailMessage
from typing import Any, Callable, Dict, List, Optional

from .base import AdapterError, BaseAdapter


class EmailAdapter(BaseAdapter):
    NAME = "email"
    KEYSTORE_NAMES: List[str] = []
    ENV_KEYS = [
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM",
        "IMAP_HOST", "IMAP_PORT", "IMAP_USER", "IMAP_PASS",
        "EMAIL_USER", "EMAIL_PASS",
    ]
    REQUIRED: List[str] = []  # ready when at least one transport has user+pass+host
    CLI_FALLBACK = False
    RATE_PER_SEC = 1.0
    RATE_BURST = 3

    # ------------------------------------------------------------------
    def _smtp_user(self) -> str:
        return self.cred("SMTP_USER") or self.cred("EMAIL_USER") or ""

    def _smtp_pass(self) -> str:
        return self.cred("SMTP_PASS") or self.cred("EMAIL_PASS") or ""

    def _imap_user(self) -> str:
        return self.cred("IMAP_USER") or self.cred("EMAIL_USER") or ""

    def _imap_pass(self) -> str:
        return self.cred("IMAP_PASS") or self.cred("EMAIL_PASS") or ""

    def _smtp_ready(self) -> bool:
        return bool(self.cred("SMTP_HOST")
                    and self._smtp_user() and self._smtp_pass())

    def _imap_ready(self) -> bool:
        return bool(self.cred("IMAP_HOST")
                    and self._imap_user() and self._imap_pass())

    def is_ready(self) -> bool:
        return self._smtp_ready() or self._imap_ready()

    # ------------------------------------------------------------------
    def send(self, to: str, subject: str, body: str, *,
             html: str = "", from_addr: str = "",
             cc: str = "", bcc: str = "") -> Dict[str, Any]:
        if not self._smtp_ready():
            raise AdapterError("email: SMTP غير مُعدّ (HOST/USER/PASS)")
        host = self.cred("SMTP_HOST") or ""
        port = int(self.cred("SMTP_PORT") or "587")
        user = self._smtp_user()
        pwd = self._smtp_pass()
        sender = from_addr or self.cred("SMTP_FROM") or user

        def _do() -> Dict[str, Any]:
            msg = EmailMessage()
            msg["From"] = sender
            msg["To"] = to
            if cc:
                msg["Cc"] = cc
            if bcc:
                msg["Bcc"] = bcc
            msg["Subject"] = subject
            if html:
                msg.set_content(body or "")
                msg.add_alternative(html, subtype="html")
            else:
                msg.set_content(body or "")
            recipients = [a.strip() for a in (to + "," + cc + "," + bcc).split(",") if a.strip()]
            try:
                if port == 465:
                    with smtplib.SMTP_SSL(host, port, timeout=self.DEFAULT_TIMEOUT) as cli:
                        cli.login(user, pwd)
                        cli.send_message(msg, to_addrs=recipients)
                else:
                    with smtplib.SMTP(host, port, timeout=self.DEFAULT_TIMEOUT) as cli:
                        cli.starttls()
                        cli.login(user, pwd)
                        cli.send_message(msg, to_addrs=recipients)
            except Exception as exc:  # noqa: BLE001
                raise AdapterError(f"smtp send failed: {exc}") from exc
            return {"sent": True, "to": to, "subject": subject}

        return self._call("send", _do)

    # ------------------------------------------------------------------
    def _imap(self) -> imaplib.IMAP4:
        host = self.cred("IMAP_HOST") or ""
        port = int(self.cred("IMAP_PORT") or "993")
        try:
            cli = imaplib.IMAP4_SSL(host, port)
            cli.login(self._imap_user(), self._imap_pass())
            return cli
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"imap login failed: {exc}") from exc

    def list_messages(self, *, mailbox: str = "INBOX",
                      query: str = "ALL",
                      limit: int = 20) -> List[Dict[str, Any]]:
        if not self._imap_ready():
            raise AdapterError("email: IMAP غير مُعدّ")

        def _do() -> List[Dict[str, Any]]:
            cli = self._imap()
            try:
                cli.select(mailbox)
                typ, data = cli.search(None, query)
                if typ != "OK" or not data or not data[0]:
                    return []
                ids = data[0].split()[-limit:]
                out: List[Dict[str, Any]] = []
                for uid in reversed(ids):
                    typ, msg_data = cli.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
                    if typ != "OK" or not msg_data:
                        continue
                    raw = b""
                    for part in msg_data:
                        if isinstance(part, tuple):
                            raw = part[1]
                            break
                    msg = email.message_from_bytes(raw)
                    out.append({
                        "uid": uid.decode(),
                        "subject": msg.get("Subject", ""),
                        "from": msg.get("From", ""),
                        "date": msg.get("Date", ""),
                    })
                return out
            finally:
                try:
                    cli.logout()
                except Exception:
                    pass

        return self._call("list_messages", _do)

    def fetch_message(self, uid: str, *, mailbox: str = "INBOX") -> Dict[str, Any]:
        if not self._imap_ready():
            raise AdapterError("email: IMAP غير مُعدّ")

        def _do() -> Dict[str, Any]:
            cli = self._imap()
            try:
                cli.select(mailbox)
                typ, data = cli.fetch(uid.encode(), "(RFC822)")
                if typ != "OK" or not data:
                    raise AdapterError(f"imap fetch failed for uid={uid}")
                raw = data[0][1] if isinstance(data[0], tuple) else b""
                msg = email.message_from_bytes(raw)
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode(
                                part.get_content_charset() or "utf-8",
                                errors="replace",
                            )
                            break
                else:
                    payload = msg.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        body = payload.decode(
                            msg.get_content_charset() or "utf-8",
                            errors="replace",
                        )
                return {
                    "uid": uid,
                    "subject": msg.get("Subject", ""),
                    "from": msg.get("From", ""),
                    "to": msg.get("To", ""),
                    "date": msg.get("Date", ""),
                    "body": body[:200_000],
                }
            finally:
                try:
                    cli.logout()
                except Exception:
                    pass

        return self._call("fetch_message", _do)

    # ------------------------------------------------------------------
    def status_call(self) -> Dict[str, Any]:
        return {
            "smtp_ready": self._smtp_ready(),
            "imap_ready": self._imap_ready(),
            "smtp_host": self.cred("SMTP_HOST"),
            "imap_host": self.cred("IMAP_HOST"),
        }

    # ------------------------------------------------------------------
    def tools_for(self) -> Dict[str, Callable[..., Any]]:
        return {
            "email_send": self.send,
            "email_list": self.list_messages,
            "email_fetch": self.fetch_message,
            "email_status": self.status_call,
        }


__all__ = ["EmailAdapter"]
