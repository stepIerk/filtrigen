"""
controlbot.py — обычный Telegram-бот (через Bot API), это твой интерфейс
управления. Кнопки: "Дать инфу", "Настройки" (добавить чат / задать топик).

Запускается в том же процессе, что и userbot (общий event loop asyncio),
поэтому может напрямую вызывать функции из userbot.py.
"""

import asyncio
import logging
import os

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import db
import userbot

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("controlbot")
# у python-telegram-bot тоже свой логгер, приглушим лишний шум на DEBUG-уровне
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(os.environ.get("LOG_LEVEL", "INFO"))

BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
OWNER_ID = int(os.environ["TG_OWNER_ID"])  # твой личный Telegram user_id — бот отвечает только тебе

# in-memory состояние ожидания текстового ввода (топик / номер чата для добавления)
PENDING_ACTION = {}  # {user_id: ("set_topic", chat_id) | ("add_chat_pick",) }


def _owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id != OWNER_ID:
            if update.message:
                await update.message.reply_text("Доступ запрещён.")
            return
        return await func(update, context)
    return wrapper


def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Дать инфу", callback_data="give_info")],
        [InlineKeyboardButton("⚙️ Настройки чатов", callback_data="settings")],
    ])


@_owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧬 Filtrigen — твой личный фильтр по Telegram-чатам.\n\n"
        "Что умею:\n"
        "• 📥 Читаю твои чаты (личные и группы) и делаю сводки новых сообщений;\n"
        "• 🎯 Фильтрую по интересующей тебя теме (дедлайны, оплаты, решения и т.п.);\n"
        "• 🔒 Всё работает локально через Ollama — переписка не уходит на сторонние серверы;\n"
        "• 🧠 Запоминаю, что уже показано, и не таскаю старые сообщения повторно.\n\n"
        "Как пользоваться:\n"
        "1️⃣ Нажми «⚙️ Настройки чатов» → «➕ Добавить чат» и выбери нужный диалог;\n"
        "2️⃣ Задай интересующую тему (или оставь без неё — будет общая сводка);\n"
        "3️⃣ Когда захочешь — нажми «📥 Дать инфу» и выбери чат. Я пришлю сводку.\n\n"
        "Ничего не читается фоном и без твоей команды — только по кнопке.\n\n"
        "Начинай: выбери действие",
        reply_markup=main_menu_kb(),
    )


# ---------- Дать инфу ----------

async def show_tracked_chats_for_info(query):
    chats = db.list_tracked_chats()
    if not chats:
        await query.edit_message_text(
            "Нет ни одного отслеживаемого чата.\nДобавь чат через ⚙️ Настройки чатов.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]]
            ),
        )
        return

    buttons = [
        [InlineKeyboardButton(f"{c['title']}", callback_data=f"info_chat:{c['chat_id']}")]
        for c in chats
    ]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")])
    await query.edit_message_text(
        "Выбери чат, по которому нужна сводка:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def do_give_info(query, chat_id: int):
    chat = db.get_chat(chat_id)
    if not chat:
        await query.edit_message_text("Чат не найден в списке отслеживаемых.")
        return

    log.info("do_give_info: старт для chat_id=%s (%s)", chat_id, chat["title"])
    await query.edit_message_text(f"⏳ Читаю новые сообщения в «{chat['title']}»…")

    try:
        messages_text, newest_id, count = await userbot.fetch_new_messages(chat_id)
    except Exception as e:
        log.exception("Ошибка чтения сообщений для chat_id=%s", chat_id)
        await query.edit_message_text(f"Ошибка при чтении чата: {e}\n\nПодробности в логах: docker compose logs -f")
        return

    if count == 0:
        db.set_last_read_id(chat_id, newest_id)
        await query.edit_message_text(
            f"В «{chat['title']}» новых сообщений нет.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]
            ),
        )
        return

    await query.edit_message_text(
        f"⏳ Прочитал {count} новых сообщений в «{chat['title']}», делаю сводку через Ollama…"
    )

    topic = chat["topic"]
    loop = asyncio.get_running_loop()
    try:
        # requests-вызов синхронный и может занять время — уводим в отдельный поток,
        # чтобы не блокировать бота
        summary = await loop.run_in_executor(None, userbot.summarize, topic, messages_text)
    except Exception as e:
        log.exception("Ошибка обращения к Ollama для chat_id=%s", chat_id)
        import requests as _requests
        if isinstance(e, _requests.exceptions.Timeout):
            hint = (
                "Ollama не успела ответить за отведённое время.\n"
                "Увеличь OLLAMA_TIMEOUT в .env, попробуй модель полегче, "
                "или проверь, не грузит ли что-то ещё CPU/GPU."
            )
        elif isinstance(e, _requests.exceptions.ConnectionError):
            hint = "Не могу достучаться до Ollama. Проверь, что она запущена, и правильный ли OLLAMA_URL в .env."
        else:
            hint = "Подробности в логах: docker compose logs -f"
        await query.edit_message_text(f"Ошибка при обращении к Ollama: {e}\n\n{hint}")
        return

    # Обновляем чекпоинт ТОЛЬКО после успешной сводки, чтобы при сбое
    # сообщения не потерялись и попали в следующий запрос
    db.set_last_read_id(chat_id, newest_id)

    topic_line = f"(тема: {topic})" if topic else "(тема не задана — общая сводка)"
    text = (
        # f"📋 Сводка по «{chat['title']}» {topic_line}\n"
        f"Прочитано сообщений: {count}\n\n"
        f"{summary}"
    )
    # Telegram ограничивает сообщение 4096 символами
    if len(text) > 4000:
        text = text[:4000] + "\n\n…(обрезано)"

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]
        ),
    )


# ---------- Настройки ----------

async def show_settings_menu(query):
    chats = db.list_tracked_chats()
    buttons = []
    for c in chats:
        label = f"{c['title']} — тема: {c['topic'] or '—'}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"chat_settings:{c['chat_id']}")])
    buttons.append([InlineKeyboardButton("➕ Добавить чат", callback_data="add_chat")])
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")])
    await query.edit_message_text("Настройки отслеживаемых чатов:", reply_markup=InlineKeyboardMarkup(buttons))


async def show_chat_settings(query, chat_id: int):
    chat = db.get_chat(chat_id)
    if not chat:
        await query.edit_message_text("Чат не найден.")
        return
    buttons = [
        [InlineKeyboardButton("✏️ Изменить тему", callback_data=f"set_topic:{chat_id}")],
        [InlineKeyboardButton("🗑 Удалить из отслеживания", callback_data=f"remove_chat:{chat_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="settings")],
    ]
    await query.edit_message_text(
        f"Чат: {chat['title']}\n"
        f"Текущая тема: {chat['topic'] or '(не задана)'}\n"
        f"Последний прочитанный id: {chat['last_read_id']}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def show_add_chat_list(query):
    await query.edit_message_text("⏳ Загружаю список твоих диалогов…")
    try:
        dialogs = await userbot.list_dialogs(limit=100)
    except Exception as e:
        log.exception("Ошибка получения диалогов")
        await query.edit_message_text(f"Ошибка: {e}")
        return

    tracked_ids = {c["chat_id"] for c in db.list_tracked_chats()}
    available = [d for d in dialogs if d["chat_id"] not in tracked_ids]

    if not available:
        await query.edit_message_text(
            "Все доступные диалоги уже отслеживаются (или список пуст).",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="settings")]]
            ),
        )
        return

    buttons = [
        [InlineKeyboardButton(f"{d['title']} [{d['kind']}]", callback_data=f"pick_chat:{d['chat_id']}")]
        for d in available[:50]  # ограничим список кнопок разумным числом
    ]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings")])
    await query.edit_message_text("Выбери чат для добавления в отслеживание:", reply_markup=InlineKeyboardMarkup(buttons))


# ---------- Callback router ----------

@_owner_only
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "main_menu":
        await query.edit_message_text("Главное меню:", reply_markup=main_menu_kb())

    elif data == "give_info":
        await show_tracked_chats_for_info(query)

    elif data.startswith("info_chat:"):
        chat_id = int(data.split(":", 1)[1])
        await do_give_info(query, chat_id)

    elif data == "settings":
        await show_settings_menu(query)

    elif data.startswith("chat_settings:"):
        chat_id = int(data.split(":", 1)[1])
        await show_chat_settings(query, chat_id)

    elif data == "add_chat":
        await show_add_chat_list(query)

    elif data.startswith("pick_chat:"):
        chat_id = int(data.split(":", 1)[1])
        dialogs = await userbot.list_dialogs(limit=100)
        title = next((d["title"] for d in dialogs if d["chat_id"] == chat_id), str(chat_id))
        db.add_tracked_chat(chat_id, title)
        await query.edit_message_text(
            f"Добавлено: {title}\nТеперь задай тему для этого чата.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("✏️ Задать тему", callback_data=f"set_topic:{chat_id}")],
                 [InlineKeyboardButton("⬅️ В меню", callback_data="main_menu")]]
            ),
        )

    elif data.startswith("set_topic:"):
        chat_id = int(data.split(":", 1)[1])
        PENDING_ACTION[query.from_user.id] = ("set_topic", chat_id)
        await query.edit_message_text("Напиши текстом тему, которая тебя интересует в этом чате (например: «дедлайны и оплаты»).")

    elif data.startswith("remove_chat:"):
        chat_id = int(data.split(":", 1)[1])
        db.remove_tracked_chat(chat_id)
        await show_settings_menu(query)


@_owner_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pending = PENDING_ACTION.get(user_id)
    if not pending:
        await update.message.reply_text("Используй /start для меню.")
        return

    action = pending[0]
    if action == "set_topic":
        chat_id = pending[1]
        topic = update.message.text.strip()
        db.set_topic(chat_id, topic)
        del PENDING_ACTION[user_id]
        await update.message.reply_text(
            f"Тема сохранена: «{topic}»",
            reply_markup=main_menu_kb(),
        )


def build_app():
    db.init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


async def run():
    await userbot.ensure_started()
    app = build_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    log.info("controlbot запущен и опрашивает Telegram")
    # держим процесс живым
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(run())