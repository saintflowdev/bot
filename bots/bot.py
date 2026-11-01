"""
Телеграм-бот для рассылки сообщений в чаты и каналы, куда он добавлен.
Всё управление через кнопки: единственная команда — /start.
"""
import asyncio
import html
import logging
import os
import re
import sqlite3

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
TZ = os.getenv("TZ_NAME", "Asia/Yekaterinburg")
SEND_DELAY = 0.1  # пауза между отправками, чтобы не упереться в лимиты

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=TZ)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# ---------- БД ----------
DB_PATH = os.getenv("DB_PATH", "bot.db")
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
db = sqlite3.connect(DB_PATH)
db.execute(
    "CREATE TABLE IF NOT EXISTS chats "
    "(id INTEGER PRIMARY KEY, title TEXT, username TEXT, kind TEXT)"
)
db.execute(
    "CREATE TABLE IF NOT EXISTS jobs "
    "(id INTEGER PRIMARY KEY AUTOINCREMENT, hhmm TEXT, text TEXT, chat_ids TEXT DEFAULT '')"
)
for ddl in (
    "ALTER TABLE chats ADD COLUMN username TEXT",
    "ALTER TABLE chats ADD COLUMN kind TEXT",
    "ALTER TABLE jobs ADD COLUMN chat_ids TEXT DEFAULT ''",
):
    try:
        db.execute(ddl)
    except sqlite3.OperationalError:
        pass  # колонка уже есть
db.commit()


def get_chats():
    return db.execute("SELECT id, title, username, kind FROM chats ORDER BY title").fetchall()


def chat_link(chat_id: int, username: str | None) -> str | None:
    if username:
        return f"https://t.me/{username}"
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}"  # приватный чат: откроется у участников
    return None


def chat_line(chat) -> str:
    cid, title, username, kind = chat
    icon = "📢" if kind == "channel" else "👥"
    name = html.escape(title or str(cid))
    link = chat_link(cid, username)
    return f'{icon} <a href="{link}">{name}</a>' if link else f"{icon} {name}"


# ---------- Рассылка ----------
async def broadcast(text: str, chat_ids: list[int] | None = None) -> tuple[int, int]:
    ok = fail = 0
    for cid, *_ in get_chats():
        if chat_ids is not None and cid not in chat_ids:
            continue
        try:
            try:
                await bot.send_message(cid, text, parse_mode=None)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                await bot.send_message(cid, text, parse_mode=None)
            ok += 1
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            logging.warning("Чат %s недоступен: %s", cid, e)
            db.execute("DELETE FROM chats WHERE id=?", (cid,))
            db.commit()
            fail += 1
        except Exception:
            logging.exception("Ошибка отправки в %s", cid)
            fail += 1
        await asyncio.sleep(SEND_DELAY)
    return ok, fail


def parse_ids(raw: str) -> list[int] | None:
    return [int(x) for x in raw.split(",")] if raw else None


def add_scheduler_job(job_id: int, hhmm: str, text: str, chat_ids: list[int] | None):
    h, m = map(int, hhmm.split(":"))
    scheduler.add_job(
        broadcast,
        CronTrigger(hour=h, minute=m, timezone=TZ),
        args=[text, chat_ids],
        id=f"job_{job_id}",
        replace_existing=True,
    )


# ---------- Клавиатуры ----------
BTN_CHATS = "📋 Чаты"
BTN_SEND = "📨 Разослать"
BTN_JOBS = "⏰ Расписание"
BTN_ADD = "➕ Добавить бота"

main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_CHATS), KeyboardButton(text=BTN_SEND)],
        [KeyboardButton(text=BTN_JOBS), KeyboardButton(text=BTN_ADD)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def cancel_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✖️ Отмена", callback_data="cancel")
    return kb.as_markup()


def pick_kb(chats, selected: list[int], mode: str):
    kb = InlineKeyboardBuilder()
    for cid, title, _, kind in chats:
        mark = "✅" if cid in selected else "⬜"
        icon = "📢" if kind == "channel" else "👥"
        kb.button(text=f"{mark} {icon} {(title or str(cid))[:28]}", callback_data=f"t:{cid}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="Выбрать все / снять все", callback_data="all"))
    go = "📨 Отправить" if mode == "now" else "➡️ Дальше"
    kb.row(
        InlineKeyboardButton(text=f"{go} ({len(selected)})", callback_data="go"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data="cancel"),
    )
    return kb.as_markup()


def picker_text(text: str) -> str:
    return f"<b>Текст:</b>\n{html.escape(text)}\n\n<b>Выбери чаты:</b>"


# ---------- Состояния ----------
class Flow(StatesGroup):
    text = State()
    time = State()


# ---------- Учёт чатов (без ограничений по админам) ----------
members = Router()


@members.my_chat_member()
async def on_membership(event: ChatMemberUpdated):
    if event.chat.type == ChatType.PRIVATE:
        return
    status = event.new_chat_member.status
    if status in ("member", "administrator"):
        db.execute(
            "INSERT OR REPLACE INTO chats (id, title, username, kind) VALUES (?, ?, ?, ?)",
            (event.chat.id, event.chat.title, event.chat.username, event.chat.type.value),
        )
    elif status in ("left", "kicked"):
        db.execute("DELETE FROM chats WHERE id=?", (event.chat.id,))
    db.commit()


# ---------- Админская часть ----------
admin = Router()
admin.message.filter(F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


@admin.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        "Привет! Управляй рассылкой кнопками внизу.\n"
        "Чтобы бот появился в списке, добавь его в чат или канал.",
        reply_markup=main_kb,
    )


@admin.message(F.text == BTN_CHATS)
async def show_chats(m: Message, state: FSMContext):
    await state.clear()
    chats = get_chats()
    if not chats:
        await m.answer("Бот пока не добавлен ни в один чат. Нажми «➕ Добавить бота».")
        return
    lines = "\n".join(f"{i}. {chat_line(c)}" for i, c in enumerate(chats, 1))
    await m.answer(f"<b>Чаты и каналы ({len(chats)}):</b>\n\n{lines}", link_preview_options=NO_PREVIEW)


@admin.message(F.text == BTN_ADD)
async def add_bot(m: Message, state: FSMContext):
    await state.clear()
    me = await bot.get_me()
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 В группу", url=f"https://t.me/{me.username}?startgroup=true")
    kb.button(
        text="📢 В канал",
        url=f"https://t.me/{me.username}?startchannel=true&admin=post_messages",
    )
    kb.adjust(1)
    await m.answer(
        "Выбери, куда добавить бота. В канале он должен быть администратором "
        "с правом публикации — кнопка выдаст его автоматически.",
        reply_markup=kb.as_markup(),
    )


@admin.message(F.text == BTN_SEND)
async def send_now(m: Message, state: FSMContext):
    await state.clear()
    if not get_chats():
        await m.answer("Сначала добавь бота хотя бы в один чат.")
        return
    await state.set_state(Flow.text)
    await state.update_data(mode="now")
    await m.answer("Пришли текст сообщения для рассылки.", reply_markup=cancel_kb())


@admin.message(F.text == BTN_JOBS)
async def show_jobs(m: Message, state: FSMContext):
    await state.clear()
    rows = db.execute("SELECT id, hhmm, text, chat_ids FROM jobs ORDER BY hhmm").fetchall()
    kb = InlineKeyboardBuilder()
    lines = []
    for jid, hhmm, text, ids in rows:
        scope = f"{len(ids.split(','))} чат." if ids else "все чаты"
        lines.append(f"<b>#{jid} · {hhmm}</b> · {scope}\n{html.escape(text[:80])}")
        kb.button(text=f"🗑 Удалить #{jid}", callback_data=f"del:{jid}")
    kb.button(text="➕ Новое расписание", callback_data="new_sched")
    kb.adjust(1)
    body = "\n\n".join(lines) if lines else "Расписаний пока нет."
    await m.answer(
        f"<b>Ежедневные рассылки ({TZ}):</b>\n\n{body}", reply_markup=kb.as_markup()
    )


@admin.callback_query(F.data == "new_sched")
async def new_sched(c: CallbackQuery, state: FSMContext):
    if not get_chats():
        await c.answer("Сначала добавь бота в чат", show_alert=True)
        return
    await state.set_state(Flow.text)
    await state.update_data(mode="sched")
    await c.message.answer("Пришли текст сообщения для ежедневной рассылки.", reply_markup=cancel_kb())
    await c.answer()


@admin.callback_query(F.data.startswith("del:"))
async def delete_job(c: CallbackQuery):
    jid = int(c.data.split(":")[1])
    db.execute("DELETE FROM jobs WHERE id=?", (jid,))
    db.commit()
    if scheduler.get_job(f"job_{jid}"):
        scheduler.remove_job(f"job_{jid}")
    await c.message.edit_text(f"Расписание #{jid} удалено.")
    await c.answer()


@admin.callback_query(F.data == "cancel")
async def cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("Отменено.")
    await c.answer()


@admin.message(Flow.text, F.text)
async def got_text(m: Message, state: FSMContext):
    chats = get_chats()
    selected = [c[0] for c in chats]
    data = await state.get_data()
    await state.update_data(text=m.text, selected=selected)
    await m.answer(picker_text(m.text), reply_markup=pick_kb(chats, selected, data["mode"]))


@admin.callback_query(F.data.startswith("t:"))
async def toggle_chat(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "selected" not in data:
        await c.answer("Начни заново", show_alert=True)
        return
    cid = int(c.data[2:])
    selected = set(data["selected"])
    selected ^= {cid}
    await state.update_data(selected=list(selected))
    await c.message.edit_reply_markup(
        reply_markup=pick_kb(get_chats(), list(selected), data["mode"])
    )
    await c.answer()


@admin.callback_query(F.data == "all")
async def toggle_all(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if "selected" not in data:
        await c.answer("Начни заново", show_alert=True)
        return
    chats = get_chats()
    all_ids = [x[0] for x in chats]
    selected = [] if len(data["selected"]) == len(all_ids) else all_ids
    await state.update_data(selected=selected)
    await c.message.edit_reply_markup(reply_markup=pick_kb(chats, selected, data["mode"]))
    await c.answer()


@admin.callback_query(F.data == "go")
async def go(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("selected"):
        await c.answer("Выбери хотя бы один чат", show_alert=True)
        return
    if data["mode"] == "now":
        await c.message.edit_text("Отправляю…")
        ok, fail = await broadcast(data["text"], data["selected"])
        await state.clear()
        await c.message.edit_text(f"Готово. Отправлено: {ok}, ошибок: {fail}")
    else:
        await state.set_state(Flow.time)
        await c.message.edit_text(
            f"Во сколько отправлять каждый день? Напиши время в формате ЧЧ:ММ, "
            f"например 09:30 ({TZ}).",
            reply_markup=cancel_kb(),
        )
    await c.answer()


@admin.message(Flow.time, F.text)
async def got_time(m: Message, state: FSMContext):
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", m.text.strip())
    if not match:
        await m.answer("Не похоже на время. Пример: 09:30", reply_markup=cancel_kb())
        return
    data = await state.get_data()
    hhmm = f"{int(match[1]):02d}:{match[2]}"
    all_ids = [x[0] for x in get_chats()]
    ids = "" if set(data["selected"]) == set(all_ids) else ",".join(map(str, data["selected"]))
    cur = db.execute(
        "INSERT INTO jobs (hhmm, text, chat_ids) VALUES (?, ?, ?)", (hhmm, data["text"], ids)
    )
    db.commit()
    add_scheduler_job(cur.lastrowid, hhmm, data["text"], parse_ids(ids))
    await state.clear()
    await m.answer(f"Готово! Расписание #{cur.lastrowid}: каждый день в {hhmm}.")


# ---------- Остальные пользователи ----------
public = Router()


@public.message(F.chat.type == ChatType.PRIVATE, ~F.from_user.id.in_(ADMIN_IDS))
async def not_admin(m: Message):
    await m.answer("Бот управляется только администратором.")


dp.include_routers(members, admin, public)


# ---------- Запуск ----------
async def main():
    for jid, hhmm, text, ids in db.execute("SELECT id, hhmm, text, chat_ids FROM jobs"):
        add_scheduler_job(jid, hhmm, text, parse_ids(ids))
    scheduler.start()
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "my_chat_member"])


if __name__ == "__main__":
    asyncio.run(main())
