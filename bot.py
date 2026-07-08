#!/usr/bin/env python3
"""
Bug / Feature Request Tracker Bot для Telegram.

Бот принимает баги и запросы фич из группы, сохраняет их в SQLite
и даёт администратору команды для просмотра и изменения статуса заявок.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from html import escape as h
from pathlib import Path

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, ReplyParameters
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()


def _parse_admin_ids(raw: str) -> set[int]:
    ids = set()
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            raise SystemExit(f"Ошибка: некорректный ADMIN_IDS — '{part}' не является числом")
    return ids


ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
DB_PATH = BASE_DIR / os.getenv("DB_PATH", "tickets.db")
VERSION_FILE = BASE_DIR / "VERSION"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bugtracker-bot")

if not BOT_TOKEN:
    raise SystemExit("Ошибка: не задан BOT_TOKEN в файле .env")
if not ADMIN_IDS:
    log.warning("ADMIN_IDS не задан — административные команды будут недоступны никому!")

STATUS_NEW = "new"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE = "done"
STATUS_REJECTED = "rejected"

STATUS_LABELS = {
    STATUS_NEW: "🆕 Новая",
    STATUS_IN_PROGRESS: "🔧 В работе",
    STATUS_DONE: "✅ Готово",
    STATUS_REJECTED: "🚫 Отклонено",
}

# короткие коды статусов для callback_data (ограничение Telegram — 64 байта)
STATUS_CODES = {"n": STATUS_NEW, "p": STATUS_IN_PROGRESS, "d": STATUS_DONE, "r": STATUS_REJECTED}

TYPE_LABELS = {"bug": "🐞 Баг", "feature": "💡 Фича"}

# Лимит длины одного сообщения в Telegram.
TELEGRAM_MSG_LIMIT = 4096

router = Router()
# Игнорируем апдейты без отправителя (посты от имени канала, анонимные админы),
# иначе обращение к message.from_user.id уронит хендлеры.
router.message.filter(F.from_user)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def db_init() -> None:
    with closing(_connect()) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                text TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                admin_comment TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_tickets_user_id ON tickets(user_id)")
        con.commit()


def db_add_ticket(ticket_type, text, user_id, username, full_name, chat_id, message_id) -> int:
    now = _now()
    with closing(_connect()) as con:
        cur = con.execute(
            """
            INSERT INTO tickets (type, status, text, user_id, username, full_name,
                                  chat_id, message_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ticket_type, STATUS_NEW, text, user_id, username, full_name, chat_id, message_id, now, now),
        )
        con.commit()
        return cur.lastrowid


def db_get_ticket(ticket_id: int):
    with closing(_connect()) as con:
        row = con.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return dict(row) if row else None


def db_list_tickets(status: str | None = None, limit: int = 15):
    query = "SELECT * FROM tickets WHERE 1=1"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with closing(_connect()) as con:
        rows = con.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def db_list_user_tickets(user_id: int, limit: int = 10):
    with closing(_connect()) as con:
        rows = con.execute(
            "SELECT * FROM tickets WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def db_update_status(ticket_id: int, status: str) -> None:
    now = _now()
    with closing(_connect()) as con:
        con.execute(
            "UPDATE tickets SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, ticket_id),
        )
        con.commit()


def db_update_comment(ticket_id: int, comment: str) -> None:
    now = _now()
    with closing(_connect()) as con:
        con.execute(
            "UPDATE tickets SET admin_comment = ?, updated_at = ? WHERE id = ?",
            (comment, now, ticket_id),
        )
        con.commit()


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def admin_command_blocked(message: Message) -> bool:
    """Гард для админ-команд. Возвращает True (и уведомляет), если выполнять нельзя.

    Проверка приватного чата идёт первой, чтобы не раскрывать список
    администраторов в группах.
    """
    if message.chat.type != "private":
        await message.reply("⛔ Административные команды работают только в личном чате с ботом.")
        return True
    if not is_admin(message.from_user.id):
        await message.reply("⛔ Эта команда доступна только администратору.")
        return True
    return False


def format_ticket_short(t: dict) -> str:
    preview = t["text"][:80]
    if len(t["text"]) > 80:
        preview += "…"
    return (
        f"#{t['id']} {TYPE_LABELS.get(t['type'], t['type'])} — "
        f"{STATUS_LABELS.get(t['status'], t['status'])}\n"
        f"{h(preview)}"
    )


def format_ticket_full(t: dict) -> str:
    author = f"@{h(t['username'])}" if t.get("username") else h(t.get("full_name") or str(t["user_id"]))
    lines = [
        f"<b>Заявка #{t['id']}</b>",
        f"Тип: {TYPE_LABELS.get(t['type'], t['type'])}",
        f"Статус: {STATUS_LABELS.get(t['status'], t['status'])}",
        f"Автор: {author}",
        f"Создано: {t['created_at']} UTC",
        "",
        h(t["text"]),
    ]
    if t.get("admin_comment"):
        lines += ["", f"💬 Комментарий администратора: {h(t['admin_comment'])}"]
    return "\n".join(lines)


def status_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for code, status in STATUS_CODES.items():
        builder.button(text=STATUS_LABELS[status], callback_data=f"st:{ticket_id}:{code}")
    builder.adjust(2)
    return builder.as_markup()


def split_message(text: str, limit: int = TELEGRAM_MSG_LIMIT) -> list[str]:
    """Режет текст на части по границам абзацев, укладываясь в лимит Telegram."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(block) > limit:  # одиночный блок длиннее лимита — режем жёстко
            chunks.append(block[:limit])
            block = block[limit:]
        current = block
    if current:
        chunks.append(current)
    return chunks


async def reply_long(message: Message, text: str) -> None:
    for chunk in split_message(text):
        await message.reply(chunk)


async def notify_group(bot: Bot, ticket: dict, text: str) -> None:
    """Отправляет ответ в исходный чат как reply на сообщение с заявкой."""
    try:
        await bot.send_message(
            ticket["chat_id"],
            text,
            reply_parameters=ReplyParameters(message_id=ticket["message_id"]),
        )
    except Exception as e:  # сообщение могло быть удалено и т.п.
        log.warning("Не удалось ответить в исходный чат %s: %s", ticket["chat_id"], e)
        try:
            await bot.send_message(ticket["chat_id"], text)
        except Exception as e2:
            log.error("Не удалось отправить сообщение в чат %s: %s", ticket["chat_id"], e2)


# ---------------------------------------------------------------------------
# Хендлеры: обычные пользователи
# ---------------------------------------------------------------------------

HELP_TEXT_USER = """
<b>Как пользоваться ботом</b>

🐞 Сообщить о баге:
<code>/bug описание проблемы</code>

💡 Предложить фичу:
<code>/feature описание идеи</code>

🔎 Проверить статус своей заявки:
<code>/status НОМЕР</code>

📋 Мои заявки:
<code>/mytickets</code>

Бот подтвердит получение заявки и напишет её номер. Когда статус
изменится, бот ответит в этом же чате на ваше исходное сообщение."""

HELP_TEXT_ADMIN = """

<b>Команды администратора</b> (используйте в личном чате с ботом):

📋 Список новых заявок:
<code>/list</code>
Список по статусу: <code>/list new|in_progress|done|rejected|all</code>

🔍 Открыть заявку и изменить статус кнопками:
<code>/view НОМЕР</code>

✏️ Изменить статус вручную:
<code>/setstatus НОМЕР new|in_progress|done|rejected</code>

💬 Добавить комментарий (публикуется в группе):
<code>/comment НОМЕР текст комментария</code>

ℹ️ Версия бота: <code>/version</code>"""


@router.message(Command("start"))
async def cmd_start(message: Message):
    text = "👋 Привет! Я бот для сбора багов и запросов фич.\n" + HELP_TEXT_USER
    if is_admin(message.from_user.id):
        text += HELP_TEXT_ADMIN
    await message.answer(text)


@router.message(Command("help"))
async def cmd_help(message: Message):
    text = HELP_TEXT_USER
    if is_admin(message.from_user.id):
        text += HELP_TEXT_ADMIN
    await message.answer(text)


@router.message(Command("version"))
async def cmd_version(message: Message):
    version = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else "неизвестно"
    await message.answer(f"Версия бота: <code>{h(version)}</code>")


async def _create_ticket(message: Message, command: CommandObject, ticket_type: str):
    if not command.args or not command.args.strip():
        label = "/bug описание проблемы" if ticket_type == "bug" else "/feature описание идеи"
        await message.reply(f"⚠️ Укажите текст после команды, например:\n<code>{label}</code>")
        return

    text = command.args.strip()
    user = message.from_user
    ticket_id = await asyncio.to_thread(
        db_add_ticket,
        ticket_type,
        text,
        user.id,
        user.username or "",
        user.full_name,
        message.chat.id,
        message.message_id,
    )
    label = "Баг" if ticket_type == "bug" else "Запрос фичи"
    await message.reply(
        f"✅ {label} принят, заявка <b>#{ticket_id}</b>.\n"
        f"Проверить статус: <code>/status {ticket_id}</code>"
    )


@router.message(Command("bug"))
async def cmd_bug(message: Message, command: CommandObject):
    await _create_ticket(message, command, "bug")


@router.message(Command("feature"))
async def cmd_feature(message: Message, command: CommandObject):
    await _create_ticket(message, command, "feature")


@router.message(Command("status"))
async def cmd_status(message: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        await message.reply("⚠️ Укажите номер заявки: <code>/status 5</code>")
        return
    ticket = await asyncio.to_thread(db_get_ticket, int(command.args.strip()))
    if not ticket:
        await message.reply("❌ Заявка с таким номером не найдена.")
        return
    # Неадмин может просматривать только собственные заявки.
    if not is_admin(message.from_user.id) and ticket["user_id"] != message.from_user.id:
        await message.reply("❌ Заявка с таким номером не найдена.")
        return
    await message.reply(format_ticket_full(ticket))


@router.message(Command("mytickets"))
async def cmd_mytickets(message: Message):
    tickets = await asyncio.to_thread(db_list_user_tickets, message.from_user.id)
    if not tickets:
        await message.reply("У вас пока нет заявок.")
        return
    text = "\n\n".join(format_ticket_short(t) for t in tickets)
    await reply_long(message, f"<b>Ваши последние заявки:</b>\n\n{text}")


# ---------------------------------------------------------------------------
# Хендлеры: администратор
# ---------------------------------------------------------------------------


@router.message(Command("list"))
async def cmd_list(message: Message, command: CommandObject):
    if await admin_command_blocked(message):
        return

    arg = (command.args or "new").strip().lower()
    status = None if arg == "all" else arg
    if status and status not in STATUS_LABELS:
        await message.reply("⚠️ Неизвестный статус. Используйте: new, in_progress, done, rejected, all")
        return

    tickets = await asyncio.to_thread(db_list_tickets, status)
    if not tickets:
        await message.reply("Заявок не найдено.")
        return

    text = "\n\n".join(format_ticket_short(t) for t in tickets)
    await reply_long(message, f"<b>Заявки ({arg}):</b>\n\n{text}\n\nОткройте заявку командой <code>/view НОМЕР</code>")


@router.message(Command("view"))
async def cmd_view(message: Message, command: CommandObject):
    if await admin_command_blocked(message):
        return
    if not command.args or not command.args.strip().isdigit():
        await message.reply("⚠️ Укажите номер заявки: <code>/view 5</code>")
        return

    ticket = await asyncio.to_thread(db_get_ticket, int(command.args.strip()))
    if not ticket:
        await message.reply("❌ Заявка с таким номером не найдена.")
        return

    await message.reply(format_ticket_full(ticket), reply_markup=status_keyboard(ticket["id"]))


@router.message(Command("setstatus"))
async def cmd_setstatus(message: Message, command: CommandObject):
    if await admin_command_blocked(message):
        return

    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit() or parts[1] not in STATUS_LABELS:
        await message.reply("⚠️ Формат: <code>/setstatus НОМЕР new|in_progress|done|rejected</code>")
        return

    ticket_id, status = int(parts[0]), parts[1]
    ticket = await asyncio.to_thread(db_get_ticket, ticket_id)
    if not ticket:
        await message.reply("❌ Заявка с таким номером не найдена.")
        return

    await asyncio.to_thread(db_update_status, ticket_id, status)
    await message.reply(f"✅ Статус заявки #{ticket_id} изменён на: {STATUS_LABELS[status]}")
    await notify_group(message.bot, ticket, f"🔔 Статус вашей заявки #{ticket_id} изменён: {STATUS_LABELS[status]}")


@router.message(Command("comment"))
async def cmd_comment(message: Message, command: CommandObject):
    if await admin_command_blocked(message):
        return

    parts = (command.args or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        await message.reply("⚠️ Формат: <code>/comment НОМЕР текст комментария</code>")
        return

    ticket_id, comment = int(parts[0]), parts[1].strip()
    ticket = await asyncio.to_thread(db_get_ticket, ticket_id)
    if not ticket:
        await message.reply("❌ Заявка с таким номером не найдена.")
        return

    await asyncio.to_thread(db_update_comment, ticket_id, comment)
    await message.reply(f"✅ Комментарий добавлен к заявке #{ticket_id}.")
    await notify_group(
        message.bot,
        ticket,
        f"💬 Комментарий администратора к заявке #{ticket_id}:\n{h(comment)}",
    )


@router.callback_query(F.data.startswith("st:"))
async def cb_set_status(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Только для администратора.", show_alert=True)
        return

    _, ticket_id_str, code = callback.data.split(":")
    ticket_id = int(ticket_id_str)
    status = STATUS_CODES.get(code)
    ticket = await asyncio.to_thread(db_get_ticket, ticket_id)
    if not ticket or not status:
        await callback.answer("❌ Заявка не найдена.", show_alert=True)
        return

    if ticket["status"] == status:
        await callback.answer("Статус уже установлен.")
        return

    await asyncio.to_thread(db_update_status, ticket_id, status)
    ticket["status"] = status
    await callback.message.edit_text(format_ticket_full(ticket), reply_markup=status_keyboard(ticket_id))
    await callback.answer(f"Статус изменён: {STATUS_LABELS[status]}")

    await notify_group(callback.bot, ticket, f"🔔 Статус вашей заявки #{ticket_id} изменён: {STATUS_LABELS[status]}")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


async def main() -> None:
    db_init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    log.info("Бот запущен.")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        log.info("Бот остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
