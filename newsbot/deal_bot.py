"""Telegram bot for ECM deal launches: IPO, QIP, OFS, rights, preferential, blocks.

Run:  python -m newsbot.deal_bot
The token is read from DEAL_BOT_TOKEN in the project .env file.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

from newsbot.feeds import Deal, nse_deals, press_deals

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "newsbot", "state", "deals.json")
POLL_SECONDS = 180
BACKFILL_HOURS = 36
BACKFILL_CAP = 20

WELCOME = (
    "Deal bot is on.\n"
    "I post new IPO, QIP, OFS, rights, preferential, buyback, and block-deal "
    "launches from NSE filings, plus headlines from Economic Times, Mint, "
    "Business Standard, Moneycontrol, and Reuters.\n"
    "Commands: /latest  /status"
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="poll feeds once and exit")
    parser.add_argument("--dry-run", action="store_true", help="print matches, send nothing")
    args = parser.parse_args()
    token = _load_token()
    if args.dry_run:
        _print_deals()
        return
    state = _load_state()
    _ensure_env_chat(state)
    if args.once:
        _poll(token, state)
        _save_state(state)
        return
    _configure_bot(token)
    print("Deal bot running. Open t.me/Dealnewsroomecm_bot and tap Start.", flush=True)
    socket.setdefaulttimeout(20)
    next_poll = 0.0
    while True:
        try:
            _within(lambda: _read_updates(token, state), 20)
        except Exception as exc:
            print("updates error: " + _redact(exc, token), flush=True)
        try:
            _deliver_pending(token, state)
            if time.time() >= next_poll:
                print("checking feeds", flush=True)
                _poll(token, state)
                next_poll = time.time() + POLL_SECONDS
        except Exception as exc:
            print("poll error: " + _redact(exc, token), flush=True)
        _save_state(state)
        time.sleep(2)


def _print_deals() -> None:
    deals = _fetch()
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=BACKFILL_HOURS)
    recent = [d for d in deals if d.published and d.published >= cutoff]
    print(f"{len(deals)} classified, {len(recent)} in the last {BACKFILL_HOURS}h")
    for deal in recent[:BACKFILL_CAP]:
        print("---")
        print(deal.format())


def _poll(token: str, state: dict) -> None:
    try:
        deals = _fetch()
    except Exception as exc:
        print("feed error: " + _redact(exc, token), flush=True)
        return
    seen = set(state["seen"])
    fresh = [d for d in deals if d.id not in seen]
    if not state["primed"]:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=BACKFILL_HOURS)
        to_send = [d for d in fresh if d.published is None or d.published >= cutoff]
        to_send.sort(key=lambda d: d.published or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc))
        overflow = max(0, len(to_send) - BACKFILL_CAP)
        to_send = to_send[-BACKFILL_CAP:]
        state["primed"] = True
        state["seen"] = _trim([d.id for d in deals])
        if overflow and state["chats"]:
            _broadcast(token, state, f"{overflow} older deals from the last day were skipped.")
    else:
        to_send = fresh
        state["seen"] = _trim(state["seen"] + [d.id for d in fresh])
    for deal in to_send:
        _broadcast(token, state, deal.format())
        state["recent"].append({"text": deal.format(), "id": deal.id})
    state["recent"] = state["recent"][-30:]
    print(f"{len(to_send)} new deals, {len(state['chats'])} chats", flush=True)


def _fetch() -> list[Deal]:
    deals = []
    errors = []
    for loader in (nse_deals, press_deals):
        try:
            deals.extend(_within(loader, 25))
        except Exception as exc:
            errors.append(f"{loader.__name__}: {exc}")
    if errors and not deals:
        raise RuntimeError("; ".join(errors))
    for err in errors:
        print("feed skipped: " + err, flush=True)
    return deals


def _within(loader, seconds: int):
    box: dict = {}

    def run():
        try:
            box["value"] = loader()
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        raise TimeoutError(f"no reply in {seconds}s")
    if "error" in box:
        raise box["error"]
    return box.get("value", [])


def _read_updates(token: str, state: dict) -> None:
    payload = {"timeout": 0, "offset": state["offset"], "allowed_updates": ["message", "my_chat_member"]}
    updates = _api(token, "getUpdates", payload, timeout=20)
    for update in updates:
        state["offset"] = update["update_id"] + 1
        if "my_chat_member" in update:
            member = update["my_chat_member"]
            chat_id = member["chat"]["id"]
            status = member["new_chat_member"]["status"]
            if status in ("member", "administrator"):
                _add_chat(token, state, chat_id)
            elif status in ("left", "kicked"):
                state["chats"] = [c for c in state["chats"] if c != chat_id]
            continue
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        text = (message.get("text") or "").strip()
        command = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""
        if command in ("/start", "/help"):
            _add_chat(token, state, chat_id)
        elif command == "/latest":
            _add_chat(token, state, chat_id, welcome=False)
            recent = state["recent"][-8:]
            body = "\n\n".join(item["text"] for item in recent) or "Nothing posted yet."
            _send(token, chat_id, body)
        elif command == "/status":
            _add_chat(token, state, chat_id, welcome=False)
            _send(
                token,
                chat_id,
                "Watching NSE filings and offer documents, and Economic Times, "
                "Mint, Business Standard, Moneycontrol, and Reuters.",
            )
        elif chat.get("type") == "private":
            _add_chat(token, state, chat_id)


def _deliver_pending(token: str, state: dict) -> None:
    for chat_id in list(state["chats"]):
        if chat_id not in state["welcomed"]:
            _add_chat(token, state, chat_id)


def _add_chat(token: str, state: dict, chat_id: int, welcome: bool = True) -> None:
    if chat_id not in state["chats"]:
        state["chats"].append(chat_id)
        print(f"chat {chat_id}", flush=True)
    if welcome and chat_id not in state["welcomed"]:
        _send(token, chat_id, WELCOME)
        for item in state["recent"][-20:]:
            _send(token, chat_id, item["text"])
        state["welcomed"].append(chat_id)
        print(f"delivered {len(state['recent'][-20:])} to {chat_id}", flush=True)


def _broadcast(token: str, state: dict, text: str) -> None:
    for chat_id in list(state["chats"]):
        try:
            _send(token, chat_id, text)
        except RuntimeError as exc:
            print(f"send to {chat_id} failed: {_redact(exc, token)}", flush=True)


def _configure_bot(token: str) -> None:
    me = _api(token, "getMe")
    print(f"connected as @{me.get('username')}", flush=True)
    optional = (
        ("deleteWebhook", None),
        (
            "setMyShortDescription",
            {"short_description": "IPO, QIP, OFS, rights, preferential, and block-deal launches."},
        ),
        (
            "setMyDescription",
            {
                "description": (
                    "Posts equity capital-markets deal launches from NSE filings "
                    "and from Economic Times, Mint, Business Standard, Moneycontrol, and Reuters."
                )
            },
        ),
        (
            "setMyCommands",
            {
                "commands": [
                    {"command": "latest", "description": "Show the latest deals"},
                    {"command": "status", "description": "Which sources are watched"},
                ]
            },
        ),
    )
    for method, payload in optional:
        try:
            _api(token, method, payload)
        except RuntimeError as exc:
            print(f"{method} skipped: {_redact(exc, token)}", flush=True)


def _send(token: str, chat_id: int, text: str) -> None:
    payload = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": False}
    try:
        _api(token, "sendMessage", payload)
    except RuntimeError as exc:
        if "429" not in str(exc):
            raise
        time.sleep(3)
        _api(token, "sendMessage", payload)
    time.sleep(0.4)


def _api(token: str, method: str, payload: dict | None = None, timeout: int = 30):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"} if data else {}
    last = "no response"
    for attempt in range(5):
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode())
            if not body.get("ok"):
                raise RuntimeError(f"{method} refused")
            return body.get("result")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            last = f"{method} {exc.code} {_redact(detail, token)}"
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 4:
                break
        except Exception as exc:
            last = f"{method} {_redact(exc, token)}"
            if attempt == 4:
                break
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(last)


def _load_token() -> str:
    path = os.path.join(ROOT, ".env")
    token = os.environ.get("DEAL_BOT_TOKEN", "")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("DEAL_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"')
    if not token:
        sys.exit("DEAL_BOT_TOKEN is missing from .env")
    return token


def _ensure_env_chat(state: dict) -> None:
    raw = os.environ.get("DEAL_CHAT_ID", "").strip()
    if not raw:
        return
    chat_id = int(raw)
    if chat_id not in state["chats"]:
        state["chats"].append(chat_id)
    if chat_id not in state["welcomed"]:
        state["welcomed"].append(chat_id)


def _load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"seen": [], "chats": [], "welcomed": [], "recent": [], "primed": False, "offset": 0}
    with open(STATE_PATH, encoding="utf-8") as handle:
        state = json.load(handle)
    state.setdefault("seen", [])
    state.setdefault("chats", [])
    state.setdefault("welcomed", [])
    state.setdefault("recent", [])
    state.setdefault("primed", False)
    state.setdefault("offset", 0)
    return state


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, sort_keys=True, separators=(",", ":"))
    os.replace(tmp, STATE_PATH)


def _trim(ids: list[str]) -> list[str]:
    kept = []
    seen = set()
    for item_id in ids:
        if item_id in seen:
            continue
        seen.add(item_id)
        kept.append(item_id)
    return kept[-5000:]


def _redact(value, token: str) -> str:
    return str(value).replace(token, "<token>")


if __name__ == "__main__":
    main()
