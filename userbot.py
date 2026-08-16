"""
userbot.py — клиент, залогиненный ПОД ТВОИМ АККАУНТОМ через Telethon (MTProto).
Он не является отдельным ботом в понимании Telegram — это твой же аккаунт,
управляемый скриптом. Именно поэтому он видит твои личные/непубличные чаты.

Функции, которые дергает controlbot.py:
  - list_dialogs()            -> список твоих диалогов (для настройки отслеживания)
  - fetch_new_messages(chat_id) -> новые сообщения с последнего чекпоинта
  - summarize(chat_id, topic, messages) -> сводка через локальную Ollama

Первый запуск потребует интерактивного логина (номер телефона + код из Telegram,
возможно пароль 2FA). После этого сессия сохраняется в /data/userbot.session
и повторный логин не нужен — см. инструкцию в README ниже по чату.
"""

import asyncio
import logging
import os
import time
import requests
from telethon import TelegramClient
from telethon.tl.types import Channel, Chat, User

import db

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("userbot")

# Telethon сам логирует много всего — по умолчанию приглушим до WARNING,
# чтобы не заливало логи низкоуровневыми деталями MTProto.
# Если нужно отладить именно сетевой уровень Telethon — раскомментируй:
# logging.getLogger("telethon").setLevel(logging.DEBUG)
logging.getLogger("telethon").setLevel(os.environ.get("TELETHON_LOG_LEVEL", "WARNING"))

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION_PATH = "/data/userbot.session"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "600"))  # секунды

MAX_MESSAGES_PER_FETCH = 500  # защитный предел на случай очень долгого простоя

client = TelegramClient(SESSION_PATH, API_ID, API_HASH)


def _chat_title(entity) -> str:
    if isinstance(entity, User):
        name = " ".join(filter(None, [entity.first_name, entity.last_name]))
        return name or (entity.username or str(entity.id))
    return getattr(entity, "title", str(entity.id))


async def ensure_started():
    """Подключиться и убедиться, что сессия авторизована.
    Если не авторизована — интерактивный логин (нужен запуск с открытым stdin,
    см. инструкцию: docker compose run --rm userbot python userbot.py login)."""
    if not client.is_connected():
        await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError(
            "Сессия не авторизована. Запусти интерактивный логин:\n"
            "  docker compose run --rm filtrigen python -c \"import asyncio, userbot; asyncio.run(userbot.interactive_login())\""
        )


async def interactive_login():
    """Разовая интерактивная авторизация. Запускать вручную (см. README)."""
    await client.start()  # спросит номер телефона, код, при необходимости пароль 2FA
    me = await client.get_me()
    print(f"Успешно авторизован как: {me.first_name} (id={me.id})")


async def list_dialogs(limit: int = 100):
    """Вернуть список последних диалогов: [{chat_id, title, kind}, ...]"""
    await ensure_started()
    result = []
    async for dialog in client.iter_dialogs(limit=limit):
        entity = dialog.entity
        kind = "user"
        if isinstance(entity, Channel):
            kind = "channel" if entity.broadcast else "supergroup"
        elif isinstance(entity, Chat):
            kind = "group"
        result.append({
            "chat_id": dialog.id,
            "title": _chat_title(entity),
            "kind": kind,
        })
    return result


async def fetch_new_messages(chat_id: int):
    """Забрать все сообщения после last_read_id (или последние N, если чат новый).
    Возвращает (messages_text, newest_message_id, count)."""
    await ensure_started()
    t0 = time.monotonic()

    last_read_id = db.get_last_read_id(chat_id)
    log.info("fetch_new_messages: chat_id=%s last_read_id=%s", chat_id, last_read_id)
    entity = await client.get_entity(chat_id)

    collected = []
    newest_id = last_read_id

    if last_read_id == 0:
        # Чат отслеживается впервые — берём только последние сообщения,
        # чтобы не тащить всю историю разом.
        async for msg in client.iter_messages(entity, limit=50):
            if msg.id > newest_id:
                newest_id = msg.id
            if msg.text:
                collected.append(msg)
    else:
        # min_id — забираем всё, что новее чекпоинта. reverse=True — от старых к новым.
        async for msg in client.iter_messages(
            entity, min_id=last_read_id, limit=MAX_MESSAGES_PER_FETCH, reverse=True
        ):
            if msg.id > newest_id:
                newest_id = msg.id
            if msg.text:
                collected.append(msg)

    if not collected:
        log.info("fetch_new_messages: chat_id=%s новых сообщений нет (%.1fс)", chat_id, time.monotonic() - t0)
        return "", newest_id, 0

    # Формируем читаемый текст для промпта: [время] Автор: текст
    lines = []
    for msg in collected:
        sender = "Неизвестно"
        try:
            sender_entity = await msg.get_sender()
            sender = _chat_title(sender_entity) if sender_entity else "Неизвестно"
        except Exception:
            log.warning("Не удалось получить отправителя для msg_id=%s", msg.id, exc_info=True)
        time_str = msg.date.strftime("%d.%m %H:%M")
        lines.append(f"[{time_str}] {sender}: {msg.text}")

    text = "\n".join(lines)
    log.info(
        "fetch_new_messages: chat_id=%s собрано %d сообщений, %d символов, newest_id=%s (%.1fс)",
        chat_id, len(collected), len(text), newest_id, time.monotonic() - t0,
    )
    return text, newest_id, len(collected)


def summarize(topic: str, messages_text: str) -> str:
    """Синхронный запрос к локальной Ollama. Вызывается из отдельного потока
    (см. controlbot.py: run_in_executor), чтобы не блокировать event loop."""
    if not topic:
        topic_instruction = "Выдели самое важное и требующее внимания в этой переписке."
    else:
        topic_instruction = (
            f'Меня интересует тема: "{topic}". '
            f"Проверь, есть ли в переписке что-то важное, связанное с этой темой."
        )

    prompt = f"""Вот новые сообщения из чата:

{messages_text}

{topic_instruction}

Если по теме есть важная информация — кратко перечисли её пунктами (что произошло, кто написал, что требует моего внимания).
Если ничего важного по теме нет — так и напиши одной фразой: "Ничего важного по теме не найдено."
Отвечай кратко, по-русски, без лишней воды."""

    log.info(
        "summarize: старт, model=%s, длина промпта=%d символов, таймаут=%dс, ollama_url=%s",
        OLLAMA_MODEL, len(prompt), OLLAMA_TIMEOUT, OLLAMA_URL,
    )
    t0 = time.monotonic()
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
            },
            timeout=OLLAMA_TIMEOUT,
        )
        elapsed = time.monotonic() - t0
        log.info("summarize: Ollama ответила за %.1fс, status=%d", elapsed, resp.status_code)
        resp.raise_for_status()
        data = resp.json()
        result = data["response"].strip()
        log.info(
            "summarize: готово, длина ответа=%d символов, eval_count=%s, total_duration=%sнс",
            len(result), data.get("eval_count"), data.get("total_duration"),
        )
        return result
    except requests.exceptions.Timeout:
        elapsed = time.monotonic() - t0
        log.error(
            "summarize: ТАЙМАУТ после %.1fс (лимит %dс). "
            "Модель %s не успела ответить — либо она слишком большая/медленная для CPU, "
            "либо промпт слишком длинный (%d символов). "
            "Увеличь OLLAMA_TIMEOUT в .env или используй модель полегче.",
            elapsed, OLLAMA_TIMEOUT, OLLAMA_MODEL, len(prompt),
        )
        raise
    except requests.exceptions.ConnectionError:
        log.error(
            "summarize: не удалось подключиться к Ollama по адресу %s. "
            "Проверь: 1) Ollama запущена на хосте (curl %s/api/tags), "
            "2) OLLAMA_URL корректен для твоей ОС (Linux иногда требует IP docker0 вместо host.docker.internal).",
            OLLAMA_URL, OLLAMA_URL,
        )
        raise
    except Exception:
        log.exception("summarize: непредвиденная ошибка")
        raise


async def main():
    """Standalone-режим (для отладки): просто держит клиент подключённым."""
    await ensure_started()
    me = await client.get_me()
    print(f"userbot запущен, авторизован как {me.first_name}")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())