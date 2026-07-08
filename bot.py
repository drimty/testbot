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


def _parse_optional_int(name: str, default: int | None = None) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"Ошибка: {name} должен быть числом, получено '{raw}'")


ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
DB_PATH = BASE_DIR / os.getenv("DB_PATH", "tickets.db")
VERSION_FILE = BASE_DIR / "VERSION"

# ID тем форума (message_thread_id), в которых принимаются команды. Пусто —
# команда работает в любой теме/чате. Игнорируется вне форум-групп.
BUG_TOPIC_ID = _parse_optional_int("BUG_TOPIC_ID")
FEATURE_TOPIC_ID = _parse_optional_int("FEATURE_TOPIC_ID")

# Через сколько секунд удалять служебные сообщения в группе (/help, /bot,
# редиректы, подсказки). 0 — не удалять. Удаление чужих сообщений требует у
# бота права администратора «Удалять сообщения».
EPHEMERAL_TTL = _parse_optional_int("EPHEMERAL_TTL", 0) or 0

# Заполняется в main() из bot.get_me() — нужно для deep-link в личный чат.
BOT_USERNAME = ""

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
STATUS_CANCELLED = "cancelled"

STATUS_LABELS = {
    STATUS_NEW: "🆕 Новая",
    STATUS_IN_PROGRESS: "🔧 В работе",
    STATUS_DONE: "✅ Готово",
    STATUS_REJECTED: "🚫 Отклонено",
    STATUS_CANCELLED: "🗑 Отменена",
}

# короткие коды статусов для инлайн-клавиатуры администратора (callback_data
# ограничен 64 байтами). Отмена — действие пользователя, кнопки для неё нет.
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
                message_thread_id INTEGER,
                admin_comment TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # Миграция для баз, созданных до появления колонки темы форума.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(tickets)")}
        if "message_thread_id" not in cols:
            con.execute("ALTER TABLE tickets ADD COLUMN message_thread_id INTEGER")
        con.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_tickets_user_id ON tickets(user_id)")
        con.commit()


def db_add_ticket(
    ticket_type, text, user_id, username, full_name, chat_id, message_id, message_thread_id=None
) -> int:
    now = _now()
    with closing(_connect()) as con:
        cur = con.execute(
            """
            INSERT INTO tickets (type, status, text, user_id, username, full_name,
                                  chat_id, message_id, message_thread_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ticket_type, STATUS_NEW, text, user_id, username, full_name, chat_id,
             message_id, message_thread_id, now, now),
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


def db_reset() -> int:
    """Удаляет все заявки и сбрасывает нумерацию. Возвращает число удалённых."""
    with closing(_connect()) as con:
        n = con.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        con.execute("DELETE FROM tickets")
        try:
            con.execute("DELETE FROM sqlite_sequence WHERE name='tickets'")
        except sqlite3.OperationalError:
            pass  # таблица счётчиков ещё не создана — нечего сбрасывать
        con.commit()
        return n


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
    """Отправляет ответ в исходный чат как reply на сообщение с заявкой.

    Держит ответ в той же теме форума (message_thread_id), поэтому даже если
    исходное сообщение удалено, уведомление не «падает» в General.
    """
    thread_id = ticket.get("message_thread_id")
    extra = {"message_thread_id": thread_id} if thread_id else {}
    try:
        await bot.send_message(
            ticket["chat_id"],
            text,
            reply_parameters=ReplyParameters(message_id=ticket["message_id"]),
            **extra,
        )
    except Exception as e:  # сообщение могло быть удалено и т.п.
        log.warning("Не удалось ответить в исходный чат %s: %s", ticket["chat_id"], e)
        try:
            await bot.send_message(ticket["chat_id"], text, **extra)
        except Exception as e2:
            log.error("Не удалось отправить сообщение в чат %s: %s", ticket["chat_id"], e2)


# ---------------------------------------------------------------------------
# Hub-режим, темы форума и эфемерные сообщения
# ---------------------------------------------------------------------------


def is_private(message: Message) -> bool:
    return message.chat.type == "private"


def bot_deeplink(payload: str = "help") -> str:
    return f"https://t.me/{BOT_USERNAME}?start={payload}"


def open_bot_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🤖 Открыть бота в личке", url=bot_deeplink())
    return builder.as_markup()


def topic_allowed(message: Message, expected_topic_id: int | None) -> bool:
    """True, если команду можно принять здесь.

    Гейтинг применяется только в форум-группах и только когда тема настроена;
    в личке и при пустой настройке — всегда разрешено.
    """
    if expected_topic_id is None or is_private(message):
        return True
    return message.message_thread_id == expected_topic_id


def schedule_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    """Планирует удаление сообщения через EPHEMERAL_TTL секунд (если включено)."""
    if EPHEMERAL_TTL <= 0:
        return

    async def _worker() -> None:
        await asyncio.sleep(EPHEMERAL_TTL)
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception as e:  # нет прав/уже удалено/старше 48ч — не критично
            log.debug("Не удалось удалить сообщение %s в чате %s: %s", message_id, chat_id, e)

    asyncio.create_task(_worker())


async def ephemeral_reply(message: Message, text: str, **kwargs) -> Message:
    """Silent-ответ, который сам удалится вместе с исходной командой.

    Удаление сообщения пользователя требует у бота права «Удалять сообщения»;
    без него удалится только ответ бота.
    """
    sent = await message.reply(text, disable_notification=True, **kwargs)
    schedule_delete(message.bot, message.chat.id, message.message_id)
    schedule_delete(message.bot, sent.chat.id, sent.message_id)
    return sent


async def redirect_to_private(message: Message, reason: str) -> None:
    """В группе отвечает короткой silent-подсказкой с кнопкой перехода в личку."""
    await ephemeral_reply(
        message,
        f"{reason}\nНапишите мне в личный чат 👇",
        reply_markup=open_bot_keyboard(),
    )


async def notify_admins(bot: Bot, text: str) -> None:
    """Рассылает уведомление всем администраторам в личные сообщения.

    Если админ не начинал диалог с ботом, отправка не удастся — это ожидаемо,
    просто логируем и продолжаем.
    """
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:
            log.warning("Не удалось уведомить администратора %s: %s", admin_id, e)


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

🗑 Отменить свою заявку (пока её не взяли в работу):
<code>/cancel НОМЕР</code>

🤖 Открыть личный чат с ботом:
<code>/bot</code>

Бот подтвердит получение заявки и напишет её номер. Когда статус
изменится, бот ответит в этом же чате на ваше исходное сообщение.

Чтобы не засорять группу, команды <code>/help</code>, <code>/status</code>
и <code>/mytickets</code> работают в личном чате со мной."""

HELP_TEXT_ADMIN = """

<b>Команды администратора</b> (используйте в личном чате с ботом):

📋 Список новых заявок:
<code>/list</code>
Список по статусу: <code>/list new|in_progress|done|rejected|cancelled|all</code>

🔍 Открыть заявку и изменить статус кнопками:
<code>/view НОМЕР</code>

✏️ Изменить статус вручную:
<code>/setstatus НОМЕР new|in_progress|done|rejected</code>

💬 Добавить комментарий (публикуется в группе):
<code>/comment НОМЕР текст комментария</code>

📌 Опубликовать в группе панель с кнопкой «Открыть бота» (вызывать в группе):
<code>/panel</code>

🗑 Очистить ВСЕ заявки и сбросить нумерацию:
<code>/reset CONFIRM</code>

ℹ️ Версия бота: <code>/version</code>"""


@router.message(Command("start"))
async def cmd_start(message: Message):
    text = "👋 Привет! Я бот для сбора багов и запросов фич.\n" + HELP_TEXT_USER
    if is_admin(message.from_user.id):
        text += HELP_TEXT_ADMIN
    await message.answer(text)


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not is_private(message):
        await redirect_to_private(message, "📖 Справка — в личном чате.")
        return
    text = HELP_TEXT_USER
    if is_admin(message.from_user.id):
        text += HELP_TEXT_ADMIN
    await message.answer(text)


@router.message(Command("bot"))
async def cmd_bot(message: Message):
    if is_private(message):
        await message.answer("Вы уже в личном чате со мной 🙂\nНапишите /help, чтобы увидеть команды.")
        return
    await ephemeral_reply(
        message,
        "🤖 Давайте продолжим в личном чате, чтобы не засорять группу 👇",
        reply_markup=open_bot_keyboard(),
    )


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
        message.message_thread_id,
    )
    label = "Баг" if ticket_type == "bug" else "Запрос фичи"
    # Подтверждение silent — чтобы не шуметь уведомлениями в группе.
    await message.reply(
        f"✅ {label} принят, заявка <b>#{ticket_id}</b>.\n"
        f"Проверить статус: <code>/status {ticket_id}</code> (в личке /bot)",
        disable_notification=True,
    )


async def _reject_wrong_topic(message: Message, ticket_type: str) -> None:
    topic_hint = "🐞 для багов" if ticket_type == "bug" else "💡 для фич"
    cmd = "/bug" if ticket_type == "bug" else "/feature"
    await ephemeral_reply(
        message,
        f"⚠️ Команда <code>{cmd}</code> принимается в другой теме ({topic_hint}). "
        f"Напишите её в предназначенной для этого теме.",
    )


@router.message(Command("bug"))
async def cmd_bug(message: Message, command: CommandObject):
    if not topic_allowed(message, BUG_TOPIC_ID):
        await _reject_wrong_topic(message, "bug")
        return
    await _create_ticket(message, command, "bug")


@router.message(Command("feature"))
async def cmd_feature(message: Message, command: CommandObject):
    if not topic_allowed(message, FEATURE_TOPIC_ID):
        await _reject_wrong_topic(message, "feature")
        return
    await _create_ticket(message, command, "feature")


@router.message(Command("status"))
async def cmd_status(message: Message, command: CommandObject):
    if not is_private(message):
        await redirect_to_private(message, "🔎 Статус заявок смотрите в личном чате.")
        return
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
    if not is_private(message):
        await redirect_to_private(message, "📋 Ваши заявки — в личном чате.")
        return
    tickets = await asyncio.to_thread(db_list_user_tickets, message.from_user.id)
    if not tickets:
        await message.reply("У вас пока нет заявок.")
        return
    text = "\n\n".join(format_ticket_short(t) for t in tickets)
    await reply_long(message, f"<b>Ваши последние заявки:</b>\n\n{text}")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, command: CommandObject):
    if not command.args or not command.args.strip().isdigit():
        await message.reply("⚠️ Укажите номер заявки: <code>/cancel 5</code>")
        return

    ticket_id = int(command.args.strip())
    ticket = await asyncio.to_thread(db_get_ticket, ticket_id)
    # Чужие и несуществующие заявки неотличимы — не раскрываем чужие номера.
    if not ticket or ticket["user_id"] != message.from_user.id:
        await message.reply("❌ Заявка с таким номером не найдена.")
        return
    if ticket["status"] != STATUS_NEW:
        await message.reply(
            "⚠️ Отменить можно только новую заявку. "
            "Эту уже обрабатывают — обратитесь к администратору."
        )
        return

    await asyncio.to_thread(db_update_status, ticket_id, STATUS_CANCELLED)
    await message.reply(f"🗑 Заявка #{ticket_id} отменена.")

    author = f"@{h(ticket['username'])}" if ticket.get("username") else h(
        ticket.get("full_name") or str(ticket["user_id"])
    )
    await notify_admins(
        message.bot,
        f"🗑 {author} отменил(а) заявку #{ticket_id} "
        f"({TYPE_LABELS.get(ticket['type'], ticket['type'])}).",
    )


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


PANEL_TEXT = (
    "🤖 <b>Бот для багов и запросов фич</b>\n\n"
    "• 🐞 Сообщить о баге: <code>/bug описание</code>\n"
    "• 💡 Предложить фичу: <code>/feature описание</code>\n\n"
    "Статус заявок и историю смотрите в личном чате — по кнопке ниже."
)


@router.message(Command("panel"))
async def cmd_panel(message: Message):
    # Панель нужно публиковать в группе (чтобы закрепить), поэтому обычный
    # админ-гард (который требует лички) здесь не подходит.
    if is_private(message):
        await message.answer("Команду /panel вызывайте в группе — бот опубликует там панель для закрепления.")
        return
    if not is_admin(message.from_user.id):
        return  # не-админам в группе молча не отвечаем

    sent = await message.answer(
        PANEL_TEXT,
        reply_markup=open_bot_keyboard(),
        message_thread_id=message.message_thread_id,
    )
    schedule_delete(message.bot, message.chat.id, message.message_id)  # уберём саму команду /panel
    try:
        await message.bot.pin_chat_message(sent.chat.id, sent.message_id, disable_notification=True)
    except Exception as e:
        log.warning("Не удалось закрепить панель: %s", e)
        await message.answer(
            "Панель опубликована — закрепите её вручную (у бота нет права закреплять сообщения).",
            disable_notification=True,
        )


@router.message(Command("reset"))
async def cmd_reset(message: Message, command: CommandObject):
    if await admin_command_blocked(message):
        return
    if (command.args or "").strip() != "CONFIRM":
        await message.answer(
            "⚠️ Это <b>безвозвратно</b> удалит ВСЕ заявки и сбросит нумерацию с #1.\n"
            "Сообщения в группе при этом не трогаются.\n\n"
            "Для подтверждения отправьте:\n<code>/reset CONFIRM</code>"
        )
        return
    n = await asyncio.to_thread(db_reset)
    await message.answer(f"🗑 База очищена. Удалено заявок: <b>{n}</b>. Новые заявки начнутся с #1.")


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
    global BOT_USERNAME
    db_init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    me = await bot.get_me()
    BOT_USERNAME = me.username
    log.info("Бот запущен как @%s.", BOT_USERNAME)
    if EPHEMERAL_TTL > 0:
        log.info("Эфемерные сообщения включены: TTL=%s c.", EPHEMERAL_TTL)
    if BUG_TOPIC_ID or FEATURE_TOPIC_ID:
        log.info("Гейтинг тем: bug=%s, feature=%s", BUG_TOPIC_ID, FEATURE_TOPIC_ID)

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
