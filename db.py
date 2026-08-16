"""
db.py — общий слой работы с SQLite.
Используется и userbot.py, и controlbot.py.

Таблицы:
  tracked_chats(chat_id, title, topic, last_read_id)
"""

import sqlite3
import threading
from contextlib import contextmanager

DB_PATH = "/data/bot.db"

_lock = threading.Lock()


def init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tracked_chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                topic TEXT NOT NULL DEFAULT '',
                last_read_id INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def add_tracked_chat(chat_id: int, title: str, topic: str = ""):
    """Добавить чат в список отслеживаемых (или обновить title/topic)."""
    with _lock, _connect() as conn:
        conn.execute("""
            INSERT INTO tracked_chats (chat_id, title, topic, last_read_id)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title
        """, (chat_id, title, topic))
        conn.commit()


def remove_tracked_chat(chat_id: int):
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM tracked_chats WHERE chat_id=?", (chat_id,))
        conn.commit()


def set_topic(chat_id: int, topic: str):
    with _lock, _connect() as conn:
        conn.execute("UPDATE tracked_chats SET topic=? WHERE chat_id=?", (topic, chat_id))
        conn.commit()


def get_topic(chat_id: int) -> str:
    with _connect() as conn:
        row = conn.execute("SELECT topic FROM tracked_chats WHERE chat_id=?", (chat_id,)).fetchone()
        return row["topic"] if row else ""


def list_tracked_chats():
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM tracked_chats ORDER BY title").fetchall()
        return [dict(r) for r in rows]


def get_chat(chat_id: int):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tracked_chats WHERE chat_id=?", (chat_id,)).fetchone()
        return dict(row) if row else None


def get_last_read_id(chat_id: int) -> int:
    with _connect() as conn:
        row = conn.execute("SELECT last_read_id FROM tracked_chats WHERE chat_id=?", (chat_id,)).fetchone()
        return row["last_read_id"] if row else 0


def set_last_read_id(chat_id: int, message_id: int):
    with _lock, _connect() as conn:
        conn.execute("UPDATE tracked_chats SET last_read_id=? WHERE chat_id=?", (message_id, chat_id))
        conn.commit()
