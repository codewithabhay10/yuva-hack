"""Run the UnitWatt bot.

    python -m unitwatt.bot chat [--as owner|supervisor|accountant]   # chat in the terminal, no accounts needed
    python -m unitwatt.bot telegram                                  # a real Telegram bot (TELEGRAM_BOT_TOKEN)

For WhatsApp, run the API server instead: ``uvicorn unitwatt.server:app`` (see the README).

In the terminal chat, type messages as the person would, or:
    /photo <path>   send a bill photo or PDF        /voice <path>   send a voice note
    /as <role>      switch person                   /quit           leave
"""

from __future__ import annotations

import argparse
import logging
import mimetypes
import os
import sys
from pathlib import Path

from unitwatt.bot.core import Bot, Incoming
from unitwatt.bot.store import BotStore


def _build(db: str) -> Bot:
    from unitwatt.pipeline import run_demo

    if db != ":memory:":
        Path(db).parent.mkdir(parents=True, exist_ok=True)
    return Bot(run_demo(), BotStore(db))


def chat(bot: Bot, role: str, out_dir: Path) -> None:
    people = {c.role: c for c in bot.F.contacts}
    if role not in people:
        sys.exit(f"No contact with role {role!r} in the factory profile; roles present: {', '.join(people)}.")
    person = people[role]
    print(f"Chatting as {person.name} ({person.role}, {person.phone}). Type 'hi' to start; /quit to leave.\n")
    while True:
        try:
            line = input(f"{person.name}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in ("/quit", "/exit"):
            return
        msg = Incoming(sender=person.phone, text=line, channel="terminal")
        if line.startswith("/as "):
            wanted = line.split(maxsplit=1)[1]
            if wanted in people:
                person = people[wanted]
                print(f"Now chatting as {person.name} ({person.role}).\n")
            else:
                print(f"Roles: {', '.join(people)}\n")
            continue
        if line.startswith(("/photo ", "/voice ")):
            path = Path(line.split(maxsplit=1)[1]).expanduser()
            if not path.exists():
                print(f"No such file: {path}\n")
                continue
            mime = mimetypes.guess_type(path.name)[0] or ("audio/ogg" if line.startswith("/voice") else "image/jpeg")
            kind = "audio" if line.startswith("/voice") else ("document" if mime == "application/pdf" else "image")
            msg = Incoming(sender=person.phone, kind=kind, media=path.read_bytes(), mime=mime, filename=path.name, channel="terminal")
        for reply in bot.handle(msg):
            print("\n" + "\n".join("  " + line for line in reply.text.splitlines()))
            if reply.buttons:
                print("  " + "   ".join(f"[{bid}] {title}" for bid, title in reply.buttons))
            if reply.document:
                out_dir.mkdir(parents=True, exist_ok=True)
                target = out_dir / reply.document[0]
                target.write_bytes(reply.document[1])
                print(f"  📎 saved {target}")
            print()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m unitwatt.bot", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("chat", help="chat with the bot in the terminal")
    c.add_argument("--as", dest="role", default="owner", help="owner, supervisor or accountant")
    c.add_argument("--db", default=":memory:", help="SQLite file for the bot's memory (default: in memory)")
    t = sub.add_parser("telegram", help="run a Telegram bot (needs TELEGRAM_BOT_TOKEN)")
    t.add_argument("--db", default=os.environ.get("UNITWATT_BOT_DB", "data/unitwatt_bot.sqlite"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("rapidocr_onnxruntime").setLevel(logging.ERROR)
    if args.command == "chat":
        chat(_build(args.db), args.role, Path("data/generated/bot"))
    else:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            sys.exit("Set TELEGRAM_BOT_TOKEN (create a bot with @BotFather in Telegram).")
        from unitwatt.bot.telegram import TelegramBot

        TelegramBot(token, _build(args.db)).run_forever()


if __name__ == "__main__":
    main()
