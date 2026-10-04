"""Telegram for the bot: free, no business verification, good for the team and the prototype.

Create a bot with @BotFather, set ``TELEGRAM_BOT_TOKEN`` and run ``python -m unitwatt.bot telegram``.
It long-polls, so it needs no public URL. Telegram does not reveal phone numbers, so a new chat is
asked to share its number once; the bot then treats it like the same person on WhatsApp.
"""

from __future__ import annotations

import logging
import time

from unitwatt.bot.core import Incoming, Reply
from unitwatt.profiles import normalise_phone

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


class TelegramBot:
    def __init__(self, token: str, bot, session=None, base_url: str = "https://api.telegram.org", timeout: float = 40.0):
        import requests

        self.token, self.bot, self.base, self.timeout = token, bot, base_url.rstrip("/"), timeout
        self.http = session or requests.Session()
        self.offset = 0

    def _call(self, method: str, files=None, **params) -> dict:
        url = f"{self.base}/bot{self.token}/{method}"
        response = self.http.post(url, data=params, files=files, timeout=self.timeout) if files else \
            self.http.post(url, json=params, timeout=self.timeout)
        data = response.json() if response.content else {}
        if response.status_code != 200 or not data.get("ok"):
            raise TelegramError(f"Telegram {method} failed: {data.get('description', response.status_code)}")
        return data.get("result")

    def _download(self, file_id: str) -> bytes:
        path = self._call("getFile", file_id=file_id)["file_path"]
        response = self.http.get(f"{self.base}/file/bot{self.token}/{path}", timeout=self.timeout)
        if response.status_code != 200:
            raise TelegramError(f"Could not download file {file_id}: HTTP {response.status_code}")
        return response.content

    def send(self, chat_id: int, reply: Reply) -> None:
        markup = None
        if reply.buttons:
            markup = {"inline_keyboard": [[{"text": title, "callback_data": bid[:64]} for bid, title in reply.buttons]]}
        if reply.document:
            name, content, mime = reply.document
            params = {"chat_id": str(chat_id), "caption": reply.text[:1024]}
            self._call("sendDocument", files={"document": (name, content, mime)}, **params)
            return
        params = {"chat_id": chat_id, "text": reply.text[:4096]}
        if markup:
            params["reply_markup"] = markup
        self._call("sendMessage", **params)

    def _ask_for_number(self, chat_id: int) -> None:
        self._call("sendMessage", chat_id=chat_id, text=(
            "🙏 To use UnitWatt, share your phone number once (tap the button below).\n"
            "UnitWatt इस्तेमाल करने के लिए एक बार अपना फ़ोन नंबर साझा करें (नीचे बटन दबाएं)।"),
            reply_markup={"keyboard": [[{"text": "📱 Share my number / नंबर साझा करें", "request_contact": True}]],
                          "one_time_keyboard": True, "resize_keyboard": True})

    def handle_update(self, update: dict) -> None:
        if "callback_query" in update:  # a tapped inline button
            q = update["callback_query"]
            self._call("answerCallbackQuery", callback_query_id=q["id"])
            msg, text = q.get("message", {}), q.get("data", "")
            chat_id = msg.get("chat", {}).get("id")
            self._deliver(chat_id, Incoming(sender="", text=text, message_id=f"tg-cb-{q['id']}", channel="telegram"))
            return
        msg = update.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        if chat_id is None:
            return
        if "contact" in msg:
            contact = msg["contact"]
            if contact.get("user_id") != msg.get("from", {}).get("id"):
                self._call("sendMessage", chat_id=chat_id, text="Please share your own number. / कृपया अपना ही नंबर साझा करें।")
                return
            self.bot.store.link(f"tg:{chat_id}", normalise_phone(contact.get("phone_number", "")))
            self._call("sendMessage", chat_id=chat_id, text="✅", reply_markup={"remove_keyboard": True})
            self._deliver(chat_id, Incoming(sender="", text="menu", channel="telegram"))
            return
        incoming = Incoming(sender="", text=msg.get("text") or msg.get("caption", ""), message_id=f"tg-{chat_id}-{msg.get('message_id')}",
                            channel="telegram")
        if msg.get("photo"):
            incoming.kind, incoming.mime = "image", "image/jpeg"
            incoming.media = self._download(msg["photo"][-1]["file_id"])  # the largest size
        elif msg.get("document"):
            doc = msg["document"]
            incoming.kind, incoming.mime, incoming.filename = "document", doc.get("mime_type", ""), doc.get("file_name", "")
            incoming.media = self._download(doc["file_id"])
        elif msg.get("voice") or msg.get("audio"):
            audio = msg.get("voice") or msg.get("audio")
            incoming.kind, incoming.mime = "audio", audio.get("mime_type", "audio/ogg")
            incoming.media = self._download(audio["file_id"])
        self._deliver(chat_id, incoming)

    def _deliver(self, chat_id, incoming: Incoming) -> None:
        phone = self.bot.store.linked_phone(f"tg:{chat_id}")
        if not phone:
            self._ask_for_number(chat_id)
            return
        if not self.bot.store.first_time(incoming.message_id):
            return
        incoming.sender = phone
        for reply in self.bot.handle(incoming):
            self.send(chat_id, reply)

    def poll_once(self) -> int:
        updates = self._call("getUpdates", offset=self.offset, timeout=30, allowed_updates=["message", "callback_query"])
        for update in updates or []:
            self.offset = max(self.offset, update["update_id"] + 1)
            try:
                self.handle_update(update)
            except Exception:  # noqa: BLE001 - one bad update must not stop the bot
                log.exception("Telegram update %s failed", update.get("update_id"))
        return len(updates or [])

    def run_forever(self) -> None:
        log.info("Telegram bot polling; press Ctrl+C to stop.")
        while True:
            try:
                self.poll_once()
            except TelegramError as exc:
                log.warning("%s; retrying in 5 s", exc)
                time.sleep(5)
