import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini").strip()
ALLOWED_CHAT_ID = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
HISTORY_LIMIT = int(os.getenv("TELEGRAM_HISTORY_LIMIT", "8"))
SYSTEM_PROMPT = os.getenv(
    "OPENAI_SYSTEM_PROMPT",
    (
        "Ты мой личный инженерный ассистент. "
        "Принимай задачи как от владельца проекта, предлагай конкретные шаги, "
        "пиши кратко, точно и по делу."
    ),
).strip()
STATE_FILE = Path(os.getenv("TELEGRAM_STATE_FILE", "reports/telegram_bridge_state.json"))
LOG_FILE = Path(os.getenv("TELEGRAM_LOG_FILE", "reports/telegram_bridge.log"))


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_FILE),
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def require_env() -> None:
    missing = []
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")


def tg_api(method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_message(chat_id: int, text: str) -> None:
    tg_api("sendMessage", {"chat_id": chat_id, "text": text[:4000]})


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"chats": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("Failed to parse state file, starting fresh")
        return {"chats": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_chat_history(state: dict, chat_id: int) -> list[dict]:
    chats = state.setdefault("chats", {})
    chat = chats.setdefault(str(chat_id), {"history": []})
    return chat.setdefault("history", [])


def reset_chat_history(state: dict, chat_id: int) -> None:
    chats = state.setdefault("chats", {})
    chats[str(chat_id)] = {"history": []}
    save_state(state)


def openai_answer(user_text: str, history: list[dict]) -> str:
    input_items = [
        {
            "role": "system",
            "content": [{"type": "input_text", "text": SYSTEM_PROMPT}],
        }
    ]
    for item in history[-HISTORY_LIMIT:]:
        input_items.append(
            {
                "role": item["role"],
                "content": [{"type": "input_text", "text": item["text"]}],
            }
        )
    input_items.append(
        {"role": "user", "content": [{"type": "input_text", "text": user_text}]}
    )

    payload = {
        "model": OPENAI_MODEL,
        "input": input_items,
    }
    req = urllib.request.Request(
        url="https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    # Prefer `output_text` when present, fallback to parsed output chunks.
    output_text = data.get("output_text")
    if output_text:
        return output_text.strip()

    parts: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                parts.append(content.get("text", ""))
    text = "\n".join(p.strip() for p in parts if p.strip()).strip()
    return text or "Не удалось получить текстовый ответ от модели."


def is_allowed(chat_id: int) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    return str(chat_id) == ALLOWED_CHAT_ID


def main() -> None:
    setup_logging()
    require_env()
    state = load_state()
    print("Telegram <-> OpenAI bridge started")
    logging.info("Bridge started")
    offset = 0
    while True:
        try:
            updates = tg_api(
                "getUpdates",
                {"timeout": 25, "offset": offset, "allowed_updates": ["message"]},
            )
            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                if chat_id is None:
                    continue
                if not is_allowed(chat_id):
                    send_message(chat_id, "Доступ запрещён для этого чата.")
                    logging.warning("Access denied for chat_id=%s", chat_id)
                    continue

                text = (msg.get("text") or "").strip()
                if not text:
                    send_message(chat_id, "Отправь текстовое сообщение.")
                    continue

                if text.lower() in {"/start", "/help"}:
                    send_message(
                        chat_id,
                        (
                            "Напиши задачу, и я отправлю её в OpenAI.\n"
                            "Команды:\n"
                            "/status - текущее состояние\n"
                            "/reset - очистить память диалога"
                        ),
                    )
                    continue

                if text.lower() == "/status":
                    history = get_chat_history(state, chat_id)
                    send_message(
                        chat_id,
                        (
                            f"Модель: {OPENAI_MODEL}\n"
                            f"Память сообщений: {len(history)}\n"
                            f"Лимит контекста: {HISTORY_LIMIT}"
                        ),
                    )
                    continue

                if text.lower() == "/reset":
                    reset_chat_history(state, chat_id)
                    send_message(chat_id, "Память диалога очищена.")
                    continue

                history = get_chat_history(state, chat_id)
                send_message(chat_id, "Принял, думаю...")
                answer = openai_answer(text, history)
                history.append({"role": "user", "text": text})
                history.append({"role": "assistant", "text": answer})
                if len(history) > HISTORY_LIMIT * 4:
                    del history[: len(history) - HISTORY_LIMIT * 4]
                save_state(state)
                send_message(chat_id, answer)
                logging.info("Answered chat_id=%s text_len=%s", chat_id, len(text))
        except urllib.error.HTTPError as e:
            logging.exception("HTTP error: %s %s", e.code, e.reason)
            time.sleep(2)
        except Exception as e:
            logging.exception("Runtime error: %s", e)
            time.sleep(2)


if __name__ == "__main__":
    main()
