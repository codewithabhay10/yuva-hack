"""WhatsApp Cloud API (Meta) for the bot: webhook parsing, signature checks, replies and media.

Free to start: a Meta developer app with the WhatsApp product comes with a test number and up to
five test recipients; messages a user starts are free to answer within 24 hours. Set
``WHATSAPP_TOKEN``, ``WHATSAPP_PHONE_NUMBER_ID``, ``WHATSAPP_VERIFY_TOKEN`` (any string you choose)
and ``WHATSAPP_APP_SECRET``, run ``uvicorn unitwatt.server:app``, expose it over HTTPS, and register
``https://<host>/webhook/whatsapp`` as the app's webhook with the same verify token. The README has
the step-by-step setup.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass

from unitwatt.bot.core import Incoming, Reply

log = logging.getLogger(__name__)

TEXT_LIMIT, BUTTON_BODY_LIMIT, BUTTON_TITLE_LIMIT = 4096, 1024, 20


class WhatsAppError(RuntimeError):
    pass


@dataclass(frozen=True)
class WhatsAppConfig:
    token: str = ""
    phone_number_id: str = ""
    verify_token: str = ""
    app_secret: str = ""
    api_version: str = "v23.0"
    base_url: str = "https://graph.facebook.com"

    @classmethod
    def from_env(cls) -> WhatsAppConfig:
        return cls(
            token=os.environ.get("WHATSAPP_TOKEN", ""),
            phone_number_id=os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""),
            verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN", ""),
            app_secret=os.environ.get("WHATSAPP_APP_SECRET", ""),
            api_version=os.environ.get("WHATSAPP_API_VERSION", "v23.0"),
            base_url=os.environ.get("WHATSAPP_API_BASE", "https://graph.facebook.com").rstrip("/"),
        )

    @property
    def ready(self) -> bool:
        return bool(self.token and self.phone_number_id)


def verify_subscription(params: dict, verify_token: str) -> str | None:
    """Meta's GET handshake: the challenge to echo back, or None to refuse."""
    if params.get("hub.mode") != "subscribe" or not verify_token:
        return None
    if not hmac.compare_digest(str(params.get("hub.verify_token", "")), verify_token):
        return None
    return str(params.get("hub.challenge", ""))


def signature_ok(body: bytes, header: str | None, app_secret: str) -> bool:
    """X-Hub-Signature-256 check, so only Meta can post to the webhook. Skipped if no secret is set."""
    if not app_secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def parse_webhook(payload: dict) -> list[tuple[Incoming, str | None]]:
    """The people's messages in a webhook delivery, each with its media id if it carries a file.

    Delivery and read receipts (``statuses``) are ignored.
    """
    out = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            for m in (change.get("value") or {}).get("messages", []) or []:
                kind = m.get("type", "")
                msg = Incoming(sender=str(m.get("from", "")), message_id=str(m.get("id", "")), channel="whatsapp")
                media_id = None
                if kind == "text":
                    msg.text = m.get("text", {}).get("body", "")
                elif kind == "interactive":
                    picked = m["interactive"].get("button_reply") or m["interactive"].get("list_reply") or {}
                    msg.text = picked.get("id") or picked.get("title", "")
                elif kind == "button":  # a quick-reply button on a template message
                    msg.text = m["button"].get("payload") or m["button"].get("text", "")
                elif kind in ("image", "document", "audio"):
                    media = m.get(kind, {})
                    media_id = media.get("id")
                    msg.kind, msg.mime = kind, media.get("mime_type", "")
                    msg.filename, msg.text = media.get("filename", ""), media.get("caption", "")
                else:
                    msg.kind = "unsupported"
                out.append((msg, media_id))
    return out


class WhatsAppClient:
    def __init__(self, config: WhatsAppConfig, session=None, timeout: float = 30.0):
        import requests

        self.config, self.http, self.timeout = config, session or requests.Session(), timeout

    def _url(self, path: str) -> str:
        return f"{self.config.base_url}/{self.config.api_version}/{path}"

    @property
    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.config.token}"}

    def _check(self, response) -> dict:
        if response.status_code // 100 != 2:
            try:
                detail = response.json().get("error", {}).get("message", response.text[:300])
            except ValueError:
                detail = response.text[:300]
            raise WhatsAppError(f"WhatsApp API returned HTTP {response.status_code}: {detail}")
        return response.json() if response.content else {}

    def _send(self, payload: dict) -> dict:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", **payload}
        return self._check(self.http.post(self._url(f"{self.config.phone_number_id}/messages"), json=body,
                                          headers=self._auth, timeout=self.timeout))

    def send_text(self, to: str, text: str) -> None:
        for start in range(0, len(text), TEXT_LIMIT):
            self._send({"to": to, "type": "text", "text": {"preview_url": False, "body": text[start:start + TEXT_LIMIT]}})

    def send_buttons(self, to: str, text: str, buttons: list[tuple[str, str]]) -> None:
        if len(text) > BUTTON_BODY_LIMIT:  # interactive bodies are capped at 1,024 characters
            self.send_text(to, text)
            text = "👇"
        self._send({"to": to, "type": "interactive", "interactive": {
            "type": "button", "body": {"text": text},
            "action": {"buttons": [{"type": "reply", "reply": {"id": bid[:256], "title": title[:BUTTON_TITLE_LIMIT]}}
                                   for bid, title in buttons[:3]]},
        }})

    def upload_media(self, filename: str, content: bytes, mime: str) -> str:
        response = self.http.post(self._url(f"{self.config.phone_number_id}/media"), headers=self._auth, timeout=self.timeout,
                                  data={"messaging_product": "whatsapp", "type": mime}, files={"file": (filename, content, mime)})
        return self._check(response)["id"]

    def send_document(self, to: str, filename: str, content: bytes, mime: str, caption: str = "") -> None:
        media_id = self.upload_media(filename, content, mime)
        self._send({"to": to, "type": "document", "document": {"id": media_id, "filename": filename, "caption": caption[:1024]}})

    def send(self, to: str, reply: Reply) -> None:
        if reply.document:
            name, content, mime = reply.document
            self.send_document(to, name, content, mime, caption=reply.text)
        elif reply.buttons:
            self.send_buttons(to, reply.text, reply.buttons)
        else:
            self.send_text(to, reply.text)

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        """A file a user sent: look up its short-lived URL, then fetch it with the same token."""
        meta = self._check(self.http.get(self._url(media_id), headers=self._auth, timeout=self.timeout))
        response = self.http.get(meta["url"], headers=self._auth, timeout=self.timeout)
        if response.status_code != 200:
            raise WhatsAppError(f"Could not download media {media_id}: HTTP {response.status_code}")
        return response.content, meta.get("mime_type", "")

    def mark_read(self, message_id: str) -> None:
        try:
            self._check(self.http.post(self._url(f"{self.config.phone_number_id}/messages"), headers=self._auth, timeout=self.timeout,
                                       json={"messaging_product": "whatsapp", "status": "read", "message_id": message_id}))
        except WhatsAppError as exc:  # a missed read receipt is not worth failing a reply over
            log.warning("mark_read failed: %s", exc)


def process_webhook(payload: dict, bot, client: WhatsAppClient) -> int:
    """Answer every new message in a delivery; returns how many were handled."""
    handled = 0
    for msg, media_id in parse_webhook(payload):
        if not bot.store.first_time(msg.message_id):
            continue  # Meta re-delivers when a reply is slow; answer once
        try:
            client.mark_read(msg.message_id)
            if media_id:
                msg.media, mime = client.download_media(media_id)
                msg.mime = msg.mime or mime
            for reply in bot.handle(msg):
                client.send(msg.sender, reply)
            handled += 1
        except WhatsAppError:
            log.exception("WhatsApp delivery failed for message %s", msg.message_id)
    return handled
