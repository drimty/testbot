#!/usr/bin/env python3
"""
Bug / Feature Request Tracker Bot для Telegram.

Бот принимает баги и запросы фич из группы, сохраняет их в SQLite
и даёт администратору команды для просмотра и изменения статуса заявок.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from html import escape as h
from pathlib import Path

from aiogram import Bot, BaseMiddleware, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    CallbackQuery,
    ForceReply,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)
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

# Файл с ролями и серверами (секретные данные, НЕ в git). Читается на каждый
# запрос, поэтому правки применяются без перезапуска бота.
ROLES_FILE = BASE_DIR / os.getenv("ROLES_FILE", "roles.json")

# ID тем форума (message_thread_id), в которых принимаются команды. Пусто —
# команда работает в любой теме/чате. Игнорируется вне форум-групп.
BUG_TOPIC_ID = _parse_optional_int("BUG_TOPIC_ID")
FEATURE_TOPIC_ID = _parse_optional_int("FEATURE_TOPIC_ID")

# Через сколько секунд удалять служебные сообщения в группе (/help, /bot,
# редиректы, подсказки). 0 — не удалять. Удаление чужих сообщений требует у
# бота права администратора «Удалять сообщения».
EPHEMERAL_TTL = _parse_optional_int("EPHEMERAL_TTL", 0) or 0

# ID основной группы. Если задан — в личном чате бот отвечает только участникам
# группы; остальным доступна лишь команда /join (заявка на вступление).
# Пусто — ограничение выключено, бот доступен всем (как раньше).
GROUP_ID = _parse_optional_int("GROUP_ID")

# Ссылка-приглашение в группу и «секретное слово». Если в /join передать секрет
# (напр. /join Чубака), бот сразу выдаёт ссылку на вступление вместо заявки.
GROUP_INVITE_LINK = os.getenv("GROUP_INVITE_LINK", "").strip()
JOIN_SECRET = os.getenv("JOIN_SECRET", "").strip()

# Приветственный стикер, отправляется после сообщения-приглашения.
WELCOME_STICKER_ID = os.getenv(
    "WELCOME_STICKER_ID",
    "CAACAgIAAxkBAAERiuNqVnPGZ3JoPQFyR3luX6t6Ru6tYAACowADIy22FkyTj_pwfbW5PQQ",
).strip()

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
        # Реестр пользователей (через /signin и захват sender_tag из группы).
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                tag TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # Миграция для баз, созданных до появления колонки tag.
        ucols = {r["name"] for r in con.execute("PRAGMA table_info(users)")}
        if "tag" not in ucols:
            con.execute("ALTER TABLE users ADD COLUMN tag TEXT")
        # Заявки на вступление в группу (/join).
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS join_requests (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
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


def db_upsert_user(user_id: int, username: str, full_name: str) -> bool:
    """Сохраняет/обновляет пользователя. True — если запись новая."""
    now = _now()
    with closing(_connect()) as con:
        is_new = con.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone() is None
        con.execute(
            """
            INSERT INTO users (user_id, username, full_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                updated_at = excluded.updated_at
            """,
            (user_id, username, full_name, now, now),
        )
        con.commit()
        return is_new


def db_list_users(limit: int = 500):
    with closing(_connect()) as con:
        rows = con.execute(
            "SELECT * FROM users ORDER BY full_name COLLATE NOCASE, user_id LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def db_update_user_tag(user_id: int, username: str, full_name: str, tag: str) -> None:
    """Сохраняет sender_tag пользователя (создаёт запись, если её ещё нет)."""
    now = _now()
    with closing(_connect()) as con:
        con.execute(
            """
            INSERT INTO users (user_id, username, full_name, tag, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                tag = excluded.tag,
                updated_at = excluded.updated_at
            """,
            (user_id, username, full_name, tag, now, now),
        )
        con.commit()


def db_get_user_tag(user_id: int) -> str | None:
    with closing(_connect()) as con:
        row = con.execute("SELECT tag FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["tag"] if row else None


def db_add_join_request(user_id: int, username: str, full_name: str, note: str) -> bool:
    """Сохраняет/обновляет заявку на вступление. True — если заявка новая."""
    now = _now()
    with closing(_connect()) as con:
        is_new = con.execute(
            "SELECT 1 FROM join_requests WHERE user_id = ?", (user_id,)
        ).fetchone() is None
        con.execute(
            """
            INSERT INTO join_requests (user_id, username, full_name, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            (user_id, username, full_name, note, now, now),
        )
        con.commit()
        return is_new


def db_list_join_requests(limit: int = 500):
    with closing(_connect()) as con:
        rows = con.execute(
            "SELECT * FROM join_requests ORDER BY created_at LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def db_delete_join_request(user_id: int) -> None:
    with closing(_connect()) as con:
        con.execute("DELETE FROM join_requests WHERE user_id = ?", (user_id,))
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


def delete_after(bot: Bot, chat_id: int, message_id: int, seconds: int) -> None:
    """Удаляет сообщение через `seconds` секунд (0 — сразу). Ошибки не критичны."""

    async def _worker() -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception as e:  # нет прав/уже удалено/старше 48ч — не критично
            log.debug("Не удалось удалить сообщение %s в чате %s: %s", message_id, chat_id, e)

    asyncio.create_task(_worker())


def schedule_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    """Планирует эфемерное удаление через EPHEMERAL_TTL секунд (если включено)."""
    if EPHEMERAL_TTL > 0:
        delete_after(bot, chat_id, message_id, EPHEMERAL_TTL)


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


# ---------------------------------------------------------------------------
# Роли и серверы (данные из внешнего JSON-файла, вне git)
# ---------------------------------------------------------------------------


def load_roles_config() -> dict | None:
    """Читает файл ролей. None — файл отсутствует/повреждён (детали в логе)."""
    try:
        with open(ROLES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("Файл ролей не найден: %s", ROLES_FILE)
    except (json.JSONDecodeError, OSError):
        log.error("Не удалось прочитать файл ролей %s", ROLES_FILE, exc_info=True)
    return None


def roles_for_user(cfg: dict, user_id: int) -> list[str]:
    """Список ролей пользователя из секции users (строка или список)."""
    raw = (cfg.get("users") or {}).get(str(user_id)) or []
    return [raw] if isinstance(raw, str) else list(raw)


def match_title_to_role(cfg: dict, title: str | None) -> str | None:
    """Сопоставляет Telegram custom_title с ключом роли (по ключу или title)."""
    if not title:
        return None
    tl = title.strip().casefold()
    for key, rd in (cfg.get("roles") or {}).items():
        if key.casefold() == tl or str(rd.get("title", "")).strip().casefold() == tl:
            return key
    return None


async def get_custom_title(bot: Bot, user_id: int) -> str | None:
    """Читает custom_title участника группы (доступен только у администраторов)."""
    if GROUP_ID is None:
        return None
    try:
        member = await bot.get_chat_member(GROUP_ID, user_id)
        return getattr(member, "custom_title", None)
    except Exception:
        log.debug("Не удалось получить custom_title для %s", user_id, exc_info=True)
        return None


async def resolve_roles(bot: Bot, cfg: dict, user_id: int) -> list[str]:
    """Роли пользователя из всех источников:

    1) секция users в roles.json (по user_id);
    2) Telegram custom_title (только у администраторов);
    3) sender_tag обычного участника, сохранённый при сообщении в группе.
    """
    roles = list(roles_for_user(cfg, user_id))
    sources = [
        await get_custom_title(bot, user_id),
        await asyncio.to_thread(db_get_user_tag, user_id),
    ]
    for source in sources:
        key = match_title_to_role(cfg, source)
        if key and key not in roles:
            roles.append(key)
    return roles


def _code(value) -> str:
    """Моноширинный фрагмент — в Telegram тап по нему копирует значение в буфер."""
    return f"<code>{h(str(value))}</code>"


def _server_hosts(block: dict) -> list:
    """Нормализует список хостов сервера (может быть один или несколько)."""
    hosts = block.get("hosts")
    if hosts:
        return hosts
    if block.get("address"):  # одиночный хост, заданный прямо в блоке
        return [{"address": block["address"], "port": block.get("port")}]
    return []


def _format_host(hb) -> str:
    """Хост в виде 'address:port', каждое поле — отдельно копируемое."""
    if isinstance(hb, str):
        addr, _, port = hb.partition(":")
    else:
        addr, port = hb.get("address", ""), hb.get("port")
    if not addr:
        return ""
    return _code(addr) + (f":{_code(port)}" if port not in (None, "") else "")


def _format_server_block(srv) -> str:
    if isinstance(srv, str):
        return f"• {h(srv)}"
    lines = []
    if srv.get("title"):
        lines.append(f"<b>{h(str(srv['title']))}</b>")
    hosts = [s for s in (_format_host(h_) for h_ in _server_hosts(srv)) if s]
    if hosts:
        lines.append(", ".join(hosts))
    if srv.get("db"):
        lines.append(f"БД: {_code(srv['db'])}")
    accounts = srv.get("accounts") or []
    if accounts:
        lines.append("Пользователи:")
        for a in accounts:
            if isinstance(a, str):
                lines.append(h(a))
                continue
            entry = f"{_code(a.get('username', ''))}:{_code(a.get('password', ''))}"
            desc = a.get("desc") or a.get("description")
            if desc:
                entry += f" — {h(str(desc))}"
            lines.append(entry)
    return "\n".join(lines)


def format_servers(cfg: dict, roles: list[str]) -> str:
    """Форматирует серверы (PostgreSQL) для указанных ролей.

    Все значения экранируются; адрес/порт/БД/логин/пароль обёрнуты в <code>,
    чтобы в Telegram их можно было скопировать одним тапом по отдельности.
    """
    roles_def = cfg.get("roles") or {}
    blocks = []
    for role in roles:
        rd = roles_def.get(role)
        if not rd:
            continue
        role_title = f"<b>▣ {h(rd.get('title', role))}</b>"
        servers = rd.get("servers") or []
        if not servers:
            blocks.append(f"{role_title}\n— серверы не заданы")
            continue
        server_blocks = "\n\n".join(_format_server_block(s) for s in servers)
        blocks.append(f"{role_title}\n\n{server_blocks}")
    return "\n\n".join(blocks)


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
# Ограничение доступа по членству в группе
# ---------------------------------------------------------------------------

# Кэш членства: user_id -> (is_member, monotonic_ts). Снижает число вызовов API.
_MEMBER_STATUSES_IN = {"creator", "administrator", "member"}
_member_cache: dict[int, tuple[bool, float]] = {}
MEMBER_CACHE_TTL = 60
# Команды, доступные не-участникам в личном чате (всё остальное скрыто).
NONMEMBER_ALLOWED_CMDS = {"join"}


async def is_group_member(bot: Bot, user_id: int) -> bool:
    """Проверяет членство пользователя в GROUP_ID (с кэшем на MEMBER_CACHE_TTL c)."""
    if GROUP_ID is None:
        return True
    now = time.monotonic()
    cached = _member_cache.get(user_id)
    if cached and now - cached[1] < MEMBER_CACHE_TTL:
        return cached[0]
    try:
        member = await bot.get_chat_member(GROUP_ID, user_id)
        status = member.status
        if status in _MEMBER_STATUSES_IN:
            ok = True
        elif status == "restricted":
            ok = bool(getattr(member, "is_member", False))
        else:  # left / kicked
            ok = False
    except Exception:
        # «user not found», нет прав и т.п. — считаем, что не участник.
        log.debug("get_chat_member(%s, %s) не удался", GROUP_ID, user_id, exc_info=True)
        ok = False
    _member_cache[user_id] = (ok, now)
    return ok


def _command_name(text: str | None) -> str | None:
    if not text or not text.startswith("/"):
        return None
    token = text[1:].split(maxsplit=1)[0] if len(text) > 1 else ""
    return token.split("@")[0].lower() or None


JOIN_PROMPT_TEXT = (
    "📜 Голокрон запечатан.\n"
    "Только достойные смогут открыть его.\n\n"
    "Введи: <b>/join</b>\n"
    "<i>И древние знания станут доступны.</i>"
)


async def send_join_prompt(message: Message) -> None:
    await message.answer(JOIN_PROMPT_TEXT)
    if WELCOME_STICKER_ID:
        try:
            await message.answer_sticker(WELCOME_STICKER_ID)
        except Exception:
            log.debug("Не удалось отправить приветственный стикер", exc_info=True)


class MembershipMiddleware(BaseMiddleware):
    """В личном чате пропускает только участников группы и админов бота.

    Остальным доступна лишь команда /join — на прочее бот отвечает
    приглашением подать заявку. Работает, только если задан GROUP_ID.
    """

    async def __call__(self, handler, event: Message, data):
        if (
            GROUP_ID is not None
            and getattr(event, "chat", None) is not None
            and event.chat.type == "private"
            and event.from_user is not None
            and not is_admin(event.from_user.id)
        ):
            if not await is_group_member(event.bot, event.from_user.id):
                if _command_name(event.text) not in NONMEMBER_ALLOWED_CMDS:
                    await send_join_prompt(event)
                    return None
        return await handler(event, data)


class TagCaptureMiddleware(BaseMiddleware):
    """Запоминает sender_tag автора из сообщений супергруппы.

    В личке /servers и /myrole берут сохранённый тег как источник роли —
    поэтому важно перехватить его, когда участник пишет в группе.
    """

    async def __call__(self, handler, event: Message, data):
        tag = getattr(event, "sender_tag", None)
        if tag and getattr(event, "from_user", None):
            u = event.from_user
            try:
                await asyncio.to_thread(db_update_user_tag, u.id, u.username or "", u.full_name, tag)
            except Exception:
                log.debug("Не удалось сохранить sender_tag для %s", u.id, exc_info=True)
        return await handler(event, data)


router.message.outer_middleware(TagCaptureMiddleware())
router.message.outer_middleware(MembershipMiddleware())


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

🖥 Серверы для тестирования (по вашей роли, только в личке):
<code>/servers</code>

🎭 Узнать свою роль:
<code>/myrole</code>

🔐 Зарегистрироваться (отправить свои данные администратору):
<code>/signin</code>

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

👥 Список зарегистрированных пользователей (id и имена):
<code>/users</code>

🚪 Заявки на вступление в группу:
<code>/requests</code>

🏷 Назначить метку участнику (роль без прав админа):
<code>/settag USER_ID метка</code>

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


@router.message(Command("join"))
async def cmd_join(message: Message, command: CommandObject):
    user = message.from_user
    # Уже участник (или админ бота) — заявка не нужна.
    if is_admin(user.id) or await is_group_member(message.bot, user.id):
        await message.answer("✅ Вы уже участник группы — заявка не нужна.")
        return

    arg = (command.args or "").strip()

    # Секретное слово — сразу выдаём ссылку на вступление, минуя заявку.
    if JOIN_SECRET and arg.casefold() == JOIN_SECRET.casefold():
        if GROUP_INVITE_LINK:
            builder = InlineKeyboardBuilder()
            builder.button(text="🔓 Войти в круг посвящённых", url=GROUP_INVITE_LINK)
            await message.answer(
                "🗝 Голокрон признал тебя достойным. Проход открыт 👇",
                reply_markup=builder.as_markup(),
            )
        else:
            await message.answer("🗝 Слово верное, но проход не настроен. Сообщите хранителю (администратору).")
            log.warning("JOIN_SECRET верный, но GROUP_INVITE_LINK не задан в .env")
        return

    note = arg[:500]
    is_new = await asyncio.to_thread(
        db_add_join_request, user.id, user.username or "", user.full_name, note
    )

    handle = f"@{h(user.username)}" if user.username else "—"
    text = (
        f"🚪 {'Новая заявка' if is_new else 'Повторная заявка'} на вступление:\n"
        f"Имя: {h(user.full_name)}\n"
        f"Username: {handle}\n"
        f"ID: <code>{user.id}</code>"
    )
    if note:
        text += f"\nКомментарий: {h(note)}"
    await notify_admins(message.bot, text)

    await message.answer(
        "✅ Заявка на вступление отправлена администраторам. Ожидайте — вас добавят вручную."
    )


class TicketForm(StatesGroup):
    waiting_text = State()


async def _finalize_ticket(message: Message, ticket_type: str, text: str) -> None:
    """Создаёт заявку из готового текста (message — сообщение с описанием)."""
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
    await message.reply(
        f"✅ {label} принят, заявка <b>#{ticket_id}</b>.\n"
        f"Проверить статус: <code>/status {ticket_id}</code> (в личке /bot)",
        disable_notification=True,
    )


async def _start_ticket(
    message: Message, command: CommandObject, ticket_type: str, state: FSMContext
) -> None:
    """Один шаг (/bug текст) — сразу создаём; без текста — просим описание."""
    text = (command.args or "").strip()
    if text:
        await state.clear()
        await _finalize_ticket(message, ticket_type, text)
        return

    # Двухшаговый ввод. ForceReply нужен, чтобы в группе с включённым Privacy
    # Mode бот получил ответное сообщение (ответы на бота приходят всегда).
    await state.set_state(TicketForm.waiting_text)
    await state.update_data(ticket_type=ticket_type)
    kind = "🐞 баге" if ticket_type == "bug" else "💡 фиче"
    await message.reply(
        f"✍️ Опишите, в чём суть ({kind}), одним сообщением — ответом на это сообщение.\n"
        f"Отмена: /skip",
        reply_markup=ForceReply(selective=True, input_field_placeholder="Опишите заявку одним сообщением…"),
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
async def cmd_bug(message: Message, command: CommandObject, state: FSMContext):
    if not topic_allowed(message, BUG_TOPIC_ID):
        await _reject_wrong_topic(message, "bug")
        return
    await _start_ticket(message, command, "bug", state)


@router.message(Command("feature"))
async def cmd_feature(message: Message, command: CommandObject, state: FSMContext):
    if not topic_allowed(message, FEATURE_TOPIC_ID):
        await _reject_wrong_topic(message, "feature")
        return
    await _start_ticket(message, command, "feature", state)


@router.message(TicketForm.waiting_text, Command("skip"))
async def cmd_ticket_skip(message: Message, state: FSMContext):
    await state.clear()
    await message.reply("Отменено — заявка не создана.")


@router.message(TicketForm.waiting_text, F.text, ~F.text.startswith("/"))
async def cmd_ticket_text(message: Message, state: FSMContext):
    data = await state.get_data()
    ticket_type = data.get("ticket_type", "bug")
    await state.clear()
    await _finalize_ticket(message, ticket_type, message.text.strip())


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


@router.message(Command("servers"))
async def cmd_servers(message: Message):
    # Только в личке — чтобы не светить серверы в группе.
    if not is_private(message):
        await redirect_to_private(message, "🖥 Список серверов — только в личном чате.")
        return

    cfg = await asyncio.to_thread(load_roles_config)
    if not cfg:
        await message.answer("⚠️ Список серверов сейчас недоступен. Обратитесь к администратору.")
        return

    all_roles = await resolve_roles(message.bot, cfg, message.from_user.id)
    roles = [r for r in all_roles if r in (cfg.get("roles") or {})]
    if not roles:
        await message.answer(
            "У вас нет доступа к списку серверов.\n"
            "Обратитесь к администратору, чтобы вам назначили роль."
        )
        return

    await reply_long(message, f"🖥 <b>Доступные серверы</b>\n\n{format_servers(cfg, roles)}")


@router.message(Command("myrole"))
async def cmd_myrole(message: Message):
    if not is_private(message):
        await redirect_to_private(message, "🎭 Ваша роль — в личном чате.")
        return
    cfg = await asyncio.to_thread(load_roles_config)
    if not cfg:
        await message.answer("⚠️ Роли сейчас недоступны. Обратитесь к администратору.")
        return
    roles_def = cfg.get("roles") or {}
    roles = await resolve_roles(message.bot, cfg, message.from_user.id)
    titles = [str(roles_def[r].get("title", r)) for r in roles if r in roles_def]
    if not titles:
        await message.answer("У вас пока нет назначенной роли. Обратитесь к администратору.")
        return
    await message.answer("🎭 Ваши роли: " + ", ".join(f"<b>{h(t)}</b>" for t in titles))


# Сколько секунд показывать подтверждение /signin перед авто-удалением.
SIGNIN_CONFIRM_TTL = 10


@router.message(Command("signin"))
async def cmd_signin(message: Message):
    user = message.from_user
    is_new = await asyncio.to_thread(db_upsert_user, user.id, user.username or "", user.full_name)

    handle = f"@{h(user.username)}" if user.username else "—"
    action = "🔐 Регистрация" if is_new else "🔁 Обновление данных"
    await notify_admins(
        message.bot,
        f"{action} пользователя:\n"
        f"Имя: {h(user.full_name)}\n"
        f"Username: {handle}\n"
        f"ID: <code>{user.id}</code>",
    )

    # Команду /signin удаляем сразу (независимо от EPHEMERAL_TTL), подтверждение
    # показываем ненадолго и тоже убираем — чтобы не оставлять следов в теме.
    delete_after(message.bot, message.chat.id, message.message_id, 0)
    sent = await message.answer(
        "✅ Готово! Ваши данные отправлены администратору.",
        disable_notification=True,
    )
    delete_after(message.bot, sent.chat.id, sent.message_id, SIGNIN_CONFIRM_TTL)


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

    # Публикация панели: логируем любую ошибку, чтобы она была видна в journalctl,
    # а не проваливалась молча.
    try:
        # message.answer сам подставит текущую тему форума — thread id не нужен.
        sent = await message.answer(PANEL_TEXT, reply_markup=open_bot_keyboard())
    except Exception:
        log.exception("Не удалось опубликовать панель в чате %s", message.chat.id)
        await message.reply("⚠️ Не удалось опубликовать панель — детали в логах бота.")
        return

    schedule_delete(message.bot, message.chat.id, message.message_id)  # уберём саму команду /panel

    try:
        await message.bot.pin_chat_message(sent.chat.id, sent.message_id, disable_notification=True)
    except Exception:
        log.warning("Не удалось закрепить панель в чате %s (нет прав?)", message.chat.id, exc_info=True)
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


@router.message(Command("users"))
async def cmd_users(message: Message):
    if await admin_command_blocked(message):
        return
    users = await asyncio.to_thread(db_list_users)
    if not users:
        await message.answer("Пока никто не зарегистрировался через /signin.")
        return
    lines = []
    for u in users:
        handle = f"@{h(u['username'])}" if u["username"] else "—"
        line = f"• {h(u['full_name'] or '—')} ({handle}) — <code>{u['user_id']}</code>"
        if u.get("tag"):
            line += f" 🏷 {h(u['tag'])}"
        lines.append(line)
    await reply_long(message, f"<b>Зарегистрированные пользователи ({len(users)}):</b>\n\n" + "\n".join(lines))


@router.message(Command("settag"))
async def cmd_settag(message: Message, command: CommandObject):
    if await admin_command_blocked(message):
        return
    if GROUP_ID is None:
        await message.answer("⚠️ Для меток нужно задать GROUP_ID в .env.")
        return
    parts = (command.args or "").split(maxsplit=1)
    if not parts or not parts[0].lstrip("-").isdigit():
        await message.answer(
            "⚠️ Формат: <code>/settag USER_ID метка</code>\n"
            "Пустая метка снимает её: <code>/settag USER_ID</code>"
        )
        return
    target_id = int(parts[0])
    tag = parts[1].strip() if len(parts) > 1 else ""
    try:
        await message.bot.set_chat_member_tag(GROUP_ID, target_id, tag)
    except Exception as e:
        log.warning("set_chat_member_tag не удался: %s", e, exc_info=True)
        await message.answer(
            "❌ Не удалось назначить метку. Проверьте, что бот — администратор группы "
            "с правом «Управление метками» (can_manage_tags), а пользователь состоит в группе."
        )
        return
    await message.answer(
        f"🏷 Метка для <code>{target_id}</code> " + (f"установлена: {h(tag)}." if tag else "снята.")
    )


@router.message(Command("requests"))
async def cmd_requests(message: Message):
    if await admin_command_blocked(message):
        return
    reqs = await asyncio.to_thread(db_list_join_requests)

    # Самоочистка: тех, кто уже вступил в группу, убираем из списка ожидающих.
    pending = []
    for r in reqs:
        if GROUP_ID is not None and await is_group_member(message.bot, r["user_id"]):
            await asyncio.to_thread(db_delete_join_request, r["user_id"])
            continue
        pending.append(r)

    if not pending:
        await message.answer("Нет ожидающих заявок на вступление.")
        return

    lines = []
    for r in pending:
        handle = f"@{h(r['username'])}" if r["username"] else "—"
        line = f"• {h(r['full_name'] or '—')} ({handle}) — <code>{r['user_id']}</code>"
        if r.get("note"):
            line += f"\n  «{h(r['note'])}»"
        lines.append(line)
    await reply_long(message, f"<b>Заявки на вступление ({len(pending)}):</b>\n\n" + "\n".join(lines))


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
# Меню команд (всплывает при вводе «/»)
# ---------------------------------------------------------------------------

# ВНИМАНИЕ: Telegram не поддерживает меню команд для отдельной темы форума —
# scope бывает «личка / группа / конкретный чат», но не по message_thread_id.
# Поэтому в группе список одинаков во всех темах.

PRIVATE_COMMANDS = [
    BotCommand(command="servers", description="🖥 Серверы по вашей роли"),
    BotCommand(command="myrole", description="🎭 Ваша роль"),
    BotCommand(command="mytickets", description="📋 Мои заявки"),
    BotCommand(command="status", description="🔎 Статус заявки по номеру"),
    BotCommand(command="signin", description="🔐 Зарегистрироваться"),
    BotCommand(command="help", description="❓ Помощь"),
]

GROUP_COMMANDS = [
    BotCommand(command="bug", description="🐞 Сообщить о баге"),
    BotCommand(command="feature", description="💡 Предложить фичу"),
    BotCommand(command="signin", description="🔐 Зарегистрироваться"),
    BotCommand(command="bot", description="🤖 Открыть бота в личке"),
]

ADMIN_EXTRA_COMMANDS = [
    BotCommand(command="list", description="📋 Список заявок"),
    BotCommand(command="view", description="🔍 Открыть заявку"),
    BotCommand(command="setstatus", description="✏️ Сменить статус"),
    BotCommand(command="comment", description="💬 Комментарий к заявке"),
    BotCommand(command="users", description="👥 Зарегистрированные пользователи"),
    BotCommand(command="requests", description="🚪 Заявки на вступление"),
    BotCommand(command="settag", description="🏷 Назначить метку участнику"),
    BotCommand(command="panel", description="📌 Панель в группу"),
    BotCommand(command="reset", description="🗑 Очистить базу"),
    BotCommand(command="version", description="ℹ️ Версия бота"),
]


async def setup_bot_commands(bot: Bot) -> None:
    """Устанавливает меню команд для лички, группы и (расширенное) для админов."""
    try:
        await bot.set_my_commands(PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())
        for admin_id in ADMIN_IDS:
            try:
                await bot.set_my_commands(
                    PRIVATE_COMMANDS + ADMIN_EXTRA_COMMANDS,
                    scope=BotCommandScopeChat(chat_id=admin_id),
                )
            except Exception:
                # админ ещё не открывал личный чат с ботом и т.п. — не критично
                log.debug("Не удалось задать меню для админа %s", admin_id, exc_info=True)
    except Exception:
        log.warning("Не удалось установить меню команд", exc_info=True)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


async def main() -> None:
    global BOT_USERNAME
    db_init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())  # FSM для двухшагового ввода /bug, /feature
    dp.include_router(router)

    me = await bot.get_me()
    BOT_USERNAME = me.username
    await setup_bot_commands(bot)
    log.info("Бот запущен как @%s.", BOT_USERNAME)
    if EPHEMERAL_TTL > 0:
        log.info("Эфемерные сообщения включены: TTL=%s c.", EPHEMERAL_TTL)
    if BUG_TOPIC_ID or FEATURE_TOPIC_ID:
        log.info("Гейтинг тем: bug=%s, feature=%s", BUG_TOPIC_ID, FEATURE_TOPIC_ID)
    if GROUP_ID is not None:
        log.info("Доступ только участникам группы %s (не-участникам — только /join).", GROUP_ID)

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
