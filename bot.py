"""
Бот учёта личных трат.

Что умеет:
  • записывает траты (просто пришли «1500 продукты») и сразу говорит,
    сколько это «часов твоего времени» исходя из месячного дохода;
  • калькулятор времени — посчитать часы, не записывая трату;
  • долги (я должен / мне должны) с напоминанием по сроку;
  • сводка за месяц — сколько и на что ушло (+ эквивалент в часах);
  • каждый вечер напоминает прислать траты за день;
  • в конце месяца присылает сводку;
  • многопользовательский: у каждого свои траты и долги, чужого не видно;
  • о каждом новом подключившемся сообщает главному админу.

Стек: aiogram 3 + SQLite + APScheduler. Только бесплатные сервисы.
UX — кнопками. Управление: reply-меню снизу + inline-кнопки.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime, timezone

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

import db
import logic as L

load_dotenv()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("traty-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("Не задан BOT_TOKEN (см. .env.example)")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()

esc = html.escape


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Кнопки главного меню -------------------------------------------------

BTN_ADD = "➕ Добавить трату"
BTN_CALC = "⏱ Калькулятор"
BTN_DEBTS = "📌 Долги"
BTN_SUMMARY = "📊 Сводка"
BTN_SETTINGS = "⚙️ Настройки"
MAIN_BUTTONS = {BTN_ADD, BTN_CALC, BTN_DEBTS, BTN_SUMMARY, BTN_SETTINGS}


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD)],
            [KeyboardButton(text=BTN_CALC), KeyboardButton(text=BTN_DEBTS)],
            [KeyboardButton(text=BTN_SUMMARY), KeyboardButton(text=BTN_SETTINGS)],
        ],
        resize_keyboard=True,
    )


# --- FSM-состояния --------------------------------------------------------

class Flow(StatesGroup):
    add_expense = State()
    calc = State()
    debt_counterparty = State()
    debt_amount = State()
    debt_due = State()
    set_income = State()
    set_hours = State()
    set_currency = State()
    set_reminder = State()


# --- Админ и уведомление о новых пользователях ----------------------------

def get_admin_id() -> int | None:
    env = os.getenv("ADMIN_ID", "").strip()
    if env.isdigit():
        return int(env)
    m = db.get_meta("admin_id")
    if m and str(m).isdigit():
        return int(m)
    return None


async def on_new_user(tg_user) -> None:
    admin_id = get_admin_id()
    if admin_id is None:
        # Первый подключившийся автоматически становится главным админом.
        db.set_meta("admin_id", str(tg_user.id))
        log.info("Назначен главный админ: %s", tg_user.id)
        return
    if tg_user.id == admin_id:
        return
    un = f"@{tg_user.username}" if tg_user.username else "без username"
    text = (
        "🔔 <b>Новый пользователь подключился к боту</b>\n"
        f"{esc(tg_user.full_name)} ({esc(un)})\n"
        f"id <code>{tg_user.id}</code>"
    )
    try:
        await bot.send_message(admin_id, text)
    except Exception as e:  # noqa: BLE001
        log.warning("Не смог уведомить админа: %s", e)


class EnsureUserMiddleware(BaseMiddleware):
    """Гарантирует наличие пользователя в БД и ловит новых для админа."""

    async def __call__(self, handler, event, data):
        tg_user = data.get("event_from_user")
        if tg_user is not None and not tg_user.is_bot:
            is_new = db.ensure_user(
                tg_user.id, tg_user.username, tg_user.full_name, now_utc_iso()
            )
            if is_new:
                await on_new_user(tg_user)
        return await handler(event, data)


# --- Текстовые заготовки --------------------------------------------------

def welcome_text(is_admin: bool) -> str:
    t = (
        "👋 Привет! Я считаю твои траты и перевожу их в <b>часы твоего времени</b>.\n\n"
        "<b>Как записать трату:</b> просто пришли сумму и что купил —\n"
        "напр. <code>1500 продукты</code> или <code>350 такси</code>.\n"
        "Я запишу и скажу, во сколько часов работы это тебе обошлось.\n\n"
        "Кнопки снизу:\n"
        f"{BTN_ADD} — записать трату\n"
        f"{BTN_CALC} — посчитать часы, ничего не записывая\n"
        f"{BTN_DEBTS} — долги (я должен / мне должны)\n"
        f"{BTN_SUMMARY} — сколько и на что ушло за месяц\n"
        f"{BTN_SETTINGS} — доход, часы, валюта, напоминания\n\n"
        "Каждый вечер напомню записать траты, в конце месяца пришлю сводку 📊"
    )
    if is_admin:
        t += "\n\n👑 Ты — <b>главный админ</b>. Буду сообщать тебе о новых пользователях."
    return t


def expense_saved_text(u: dict, amount: float, cat: str, hours, now) -> str:
    cur = u["currency"]
    msum, mn = db.month_total(u["user_id"], L.ym(now))
    return (
        f"✅ Записал: <b>{esc(L.fmt_money(amount, cur))}</b> — {esc(cat)}\n"
        f"⏱ Это ≈ <b>{esc(L.fmt_hours(hours))}</b> твоего времени\n"
        f"💰 За {L.ym_display(L.ym(now))}: {esc(L.fmt_money(msum, cur))} ({mn} трат)"
    )


def summary_text(u: dict, ym: str) -> str:
    cur = u["currency"]
    total, n = db.month_total(u["user_id"], ym)
    head = f"📊 <b>Сводка — {L.ym_display(ym)}</b>\n\n"
    if n == 0:
        return head + "Трат в этом месяце ещё нет."
    hours = L.hours_for(total, u["monthly_income"], u["work_hours"])
    lines = [
        head,
        f"Всего: <b>{esc(L.fmt_money(total, cur))}</b> · {n} трат",
        f"⏱ Это <b>{esc(L.fmt_hours(hours))}</b> твоего времени",
    ]
    inc = u["monthly_income"]
    if inc and inc > 0:
        lines.append(f"📈 {total / inc * 100:.0f}% месячного дохода ({esc(L.fmt_money(inc, cur))})")
    lines.append("\n<b>По категориям:</b>")
    for row in db.month_breakdown(u["user_id"], ym)[:12]:
        share = row["s"] / total * 100 if total else 0
        lines.append(
            f"• {esc(row['category'] or 'Прочее')} — "
            f"{esc(L.fmt_money(row['s'], cur))} ({share:.0f}%, {row['n']})"
        )
    return "\n".join(lines)


def today_text(u: dict, now) -> str:
    cur = u["currency"]
    ymd = L.ymd(now)
    rows = db.day_expenses(u["user_id"], ymd)
    head = f"📅 <b>Сегодня, {now.strftime('%d.%m')}</b>\n\n"
    if not rows:
        return head + "Сегодня трат пока нет. Пришли сумму — запишу."
    total, n = db.day_total(u["user_id"], ymd)
    hours = L.hours_for(total, u["monthly_income"], u["work_hours"])
    lines = [head]
    for r in rows[:50]:
        note = r["note"] or r["category"] or "Прочее"
        lines.append(f"• {esc(L.fmt_money(r['amount'], cur))} — {esc(note)}")
    if len(rows) > 50:
        lines.append(f"…и ещё {len(rows) - 50} трат")
    lines.append(f"\nИтого: <b>{esc(L.fmt_money(total, cur))}</b> · {n} трат")
    lines.append(f"⏱ {esc(L.fmt_hours(hours))} твоего времени")
    return "\n".join(lines)


def debts_text(u: dict) -> str:
    cur = u["currency"]
    i_owe = db.list_debts(u["user_id"], settled=0, direction="i_owe")
    to_me = db.list_debts(u["user_id"], settled=0, direction="owed_to_me")
    if not i_owe and not to_me:
        return "📌 <b>Долги</b>\n\nАктивных долгов нет 🎉"
    lines = ["📌 <b>Долги</b>"]

    def block(title, items):
        s = sum(d["amount"] for d in items)          # итог считаем по всем
        out = [f"\n<b>{title}</b> — итого {esc(L.fmt_money(s, cur))}:"]
        for d in items[:20]:                          # показываем не больше 20 (лимит Telegram)
            due = L.iso_to_display(d["due_date"])
            due_s = f", до {due}" if due else ""
            out.append(f"• {esc(d['counterparty'] or '—')} — {esc(L.fmt_money(d['amount'], cur))}{due_s}")
        if len(items) > 20:
            out.append(f"…и ещё {len(items) - 20}")
        return out

    if i_owe:
        lines += block("🔴 Я должен", i_owe)
    if to_me:
        lines += block("🟢 Мне должны", to_me)
    return "\n".join(lines)


def debts_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="➕ Я должен", callback_data="d:add:i_owe"),
        InlineKeyboardButton(text="➕ Мне должны", callback_data="d:add:owed_to_me"),
    ]]
    for d in db.list_debts(user_id, settled=0)[:20]:
        mark = "🔴" if d["direction"] == "i_owe" else "🟢"
        label = f"✅ Закрыть {mark} {d['counterparty'] or '—'} {int(d['amount'])}"
        rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"d:settle:{d['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def summary_kb(ym: str) -> InlineKeyboardMarkup:
    prev = L.prev_ym(ym)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"◀ {L.ym_display(prev)}", callback_data=f"s:m:{prev}"),
        InlineKeyboardButton(text="📅 Сегодня", callback_data="s:today"),
    ]])


def settings_text(u: dict) -> str:
    return (
        "⚙️ <b>Настройки</b>\n\n"
        "Здесь задаётся, как считать «часы твоего времени», и когда напоминать.\n"
        "Ставка за час = доход ÷ рабочие часы в месяц."
    )


def settings_kb(u: dict, is_admin: bool) -> InlineKeyboardMarkup:
    cur = u["currency"]
    daily = "🔔 Вкл" if u["daily_reminder"] else "🔕 Выкл"
    rem = f"{int(u['reminder_hour']):02d}:{int(u['reminder_min']):02d}"
    rows = [
        [InlineKeyboardButton(text=f"💰 Доход в месяц: {L.fmt_money(u['monthly_income'], cur)}", callback_data="set:income")],
        [InlineKeyboardButton(text=f"🕐 Рабочих часов/мес: {int(u['work_hours'])}", callback_data="set:hours")],
        [InlineKeyboardButton(text=f"💱 Валюта: {cur}", callback_data="set:currency")],
        [InlineKeyboardButton(text=f"⏰ Напоминание: {rem}", callback_data="set:reminder")],
        [InlineKeyboardButton(text=f"Ежедневное напоминание: {daily}", callback_data="set:daily")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(
            text=f"👥 Пользователи: {db.users_count()}", callback_data="set:admin_users")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def undo_kb(expense_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Удалить эту трату", callback_data=f"x:del:{expense_id}")
    ]])


def _msg_usable(cq: CallbackQuery) -> bool:
    # cq.message может быть InaccessibleMessage (сообщение старше 48 ч) — без методов.
    return cq.message is not None and hasattr(cq.message, "edit_text")


async def safe_edit(cq: CallbackQuery, text: str, kb=None) -> None:
    if not _msg_usable(cq):
        try:
            await cq.answer("Сообщение устарело — открой меню заново: /menu", show_alert=True)
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        await cq.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass  # «message is not modified» и т.п. — не критично


async def cq_send(cq: CallbackQuery, text: str) -> None:
    """Ответ на callback обычным сообщением, с фолбэком на send_message
    (когда исходное сообщение недоступно — старше 48 ч)."""
    if cq.message is not None and hasattr(cq.message, "answer"):
        await cq.message.answer(text)
    else:
        await bot.send_message(cq.from_user.id, text)


# --- Сохранение траты -----------------------------------------------------

async def save_and_reply(message: Message, u: dict, amount: float, note: str) -> None:
    now = L.local_now(u["tz"])
    cat = L.category_of(note)
    eid = db.add_expense(
        u["user_id"], amount, cat, note, L.ym(now), L.ymd(now), now_utc_iso()
    )
    hours = L.hours_for(amount, u["monthly_income"], u["work_hours"])
    await message.answer(expense_saved_text(u, amount, cat, hours, now),
                         reply_markup=undo_kb(eid))


# --- Команды --------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    is_admin = get_admin_id() == message.from_user.id
    await message.answer(welcome_text(is_admin), reply_markup=main_kb())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    is_admin = get_admin_id() == message.from_user.id
    await message.answer(welcome_text(is_admin), reply_markup=main_kb())


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Меню 👇", reply_markup=main_kb())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок, отменил.", reply_markup=main_kb())


# --- Кнопки главного меню (работают в любом состоянии, сбрасывают его) -----

@router.message(F.text == BTN_ADD)
async def btn_add(message: Message, state: FSMContext) -> None:
    await state.set_state(Flow.add_expense)
    await message.answer(
        "Пришли сумму и что купил, напр.: <code>1500 продукты</code>\n"
        "(/cancel — отмена)"
    )


@router.message(F.text == BTN_CALC)
async def btn_calc(message: Message, state: FSMContext) -> None:
    await state.set_state(Flow.calc)
    await message.answer(
        "⏱ Пришли сумму — посчитаю, сколько это <b>часов твоего времени</b>.\n"
        "Трату записывать не буду.\n(/cancel — отмена)"
    )


@router.message(F.text == BTN_DEBTS)
async def btn_debts(message: Message, state: FSMContext) -> None:
    await state.clear()
    u = db.get_user(message.from_user.id)
    await message.answer(debts_text(u), reply_markup=debts_kb(u["user_id"]))


@router.message(F.text == BTN_SUMMARY)
async def btn_summary(message: Message, state: FSMContext) -> None:
    await state.clear()
    u = db.get_user(message.from_user.id)
    ym = L.ym(L.local_now(u["tz"]))
    await message.answer(summary_text(u, ym), reply_markup=summary_kb(ym))


@router.message(F.text == BTN_SETTINGS)
async def btn_settings(message: Message, state: FSMContext) -> None:
    await state.clear()
    u = db.get_user(message.from_user.id)
    is_admin = get_admin_id() == u["user_id"]
    await message.answer(settings_text(u), reply_markup=settings_kb(u, is_admin))


# --- FSM: ввод трат / калькулятор -----------------------------------------

@router.message(Flow.add_expense, F.text)
async def st_add_expense(message: Message, state: FSMContext) -> None:
    parsed = L.parse_amount(message.text)
    if not parsed:
        await message.answer("Не понял сумму 🤔 Пришли число, напр. <code>500 такси</code>")
        return
    await state.clear()
    u = db.get_user(message.from_user.id)
    await save_and_reply(message, u, parsed[0], parsed[1])


@router.message(Flow.calc, F.text)
async def st_calc(message: Message, state: FSMContext) -> None:
    parsed = L.parse_amount(message.text)
    if not parsed:
        await message.answer("Пришли число, напр. <code>5000</code>")
        return
    await state.clear()
    u = db.get_user(message.from_user.id)
    amount = parsed[0]
    hours = L.hours_for(amount, u["monthly_income"], u["work_hours"])
    cur = u["currency"]
    extra = ""
    if u["monthly_income"] and u["monthly_income"] > 0:
        extra = f"\n📈 {amount / u['monthly_income'] * 100:.1f}% месячного дохода"
    await message.answer(
        f"💸 {esc(L.fmt_money(amount, cur))}\n"
        f"⏱ Это <b>{esc(L.fmt_hours(hours))}</b> твоего времени{extra}",
        reply_markup=main_kb(),
    )


# --- FSM: добавление долга -------------------------------------------------

@router.message(Flow.debt_counterparty, F.text)
async def st_debt_counterparty(message: Message, state: FSMContext) -> None:
    await state.update_data(counterparty=message.text.strip()[:64])
    await state.set_state(Flow.debt_amount)
    await message.answer("Сколько? Пришли сумму, напр. <code>5000</code>")


@router.message(Flow.debt_amount, F.text)
async def st_debt_amount(message: Message, state: FSMContext) -> None:
    parsed = L.parse_amount(message.text)
    if not parsed:
        await message.answer("Не понял сумму. Пришли число, напр. <code>5000</code>")
        return
    await state.update_data(amount=parsed[0])
    await state.set_state(Flow.debt_due)
    await message.answer(
        "Когда вернуть? Дата в формате <code>ДД.ММ</code> или <code>ДД.ММ.ГГГГ</code>.\n"
        "Если срока нет — напиши <code>нет</code>."
    )


@router.message(Flow.debt_due, F.text)
async def st_debt_due(message: Message, state: FSMContext) -> None:
    u = db.get_user(message.from_user.id)
    today = L.local_now(u["tz"]).date()
    due = L.parse_due_date(message.text, today)
    if due == "invalid":
        await message.answer("Не понял дату. Напиши <code>15.08</code> или <code>нет</code>.")
        return
    data = await state.get_data()
    await state.clear()
    db.add_debt(
        u["user_id"], data["direction"], data.get("counterparty", "—"),
        data["amount"], "", due, now_utc_iso(),
    )
    who = "🔴 Я должен" if data["direction"] == "i_owe" else "🟢 Мне должны"
    due_s = f", до {L.iso_to_display(due)}" if due else ""
    await message.answer(
        f"Записал долг: {who} — {esc(data.get('counterparty', '—'))} "
        f"{esc(L.fmt_money(data['amount'], u['currency']))}{due_s} ✅"
    )
    await message.answer(debts_text(u), reply_markup=debts_kb(u["user_id"]))


# --- FSM: настройки -------------------------------------------------------

@router.message(Flow.set_income, F.text)
async def st_set_income(message: Message, state: FSMContext) -> None:
    parsed = L.parse_amount(message.text)
    if not parsed:
        await message.answer("Пришли число, напр. <code>100000</code>")
        return
    await state.clear()
    db.update_user(message.from_user.id, monthly_income=parsed[0])
    u = db.get_user(message.from_user.id)
    await message.answer(
        f"💰 Доход обновлён: <b>{esc(L.fmt_money(parsed[0], u['currency']))}</b> в месяц.",
        reply_markup=main_kb(),
    )
    await message.answer(settings_text(u), reply_markup=settings_kb(u, get_admin_id() == u["user_id"]))


@router.message(Flow.set_hours, F.text)
async def st_set_hours(message: Message, state: FSMContext) -> None:
    parsed = L.parse_amount(message.text)
    if not parsed or parsed[0] <= 0:
        await message.answer("Пришли число часов, напр. <code>160</code>")
        return
    await state.clear()
    db.update_user(message.from_user.id, work_hours=parsed[0])
    u = db.get_user(message.from_user.id)
    await message.answer(f"🕐 Рабочих часов в месяц: <b>{int(parsed[0])}</b>.", reply_markup=main_kb())
    await message.answer(settings_text(u), reply_markup=settings_kb(u, get_admin_id() == u["user_id"]))


@router.message(Flow.set_currency, F.text)
async def st_set_currency(message: Message, state: FSMContext) -> None:
    cur = (message.text or "").strip()[:4]
    if not cur:
        await message.answer("Пришли символ валюты, напр. <code>₽</code>, <code>$</code>, <code>R$</code>")
        return
    await state.clear()
    db.update_user(message.from_user.id, currency=cur)
    u = db.get_user(message.from_user.id)
    await message.answer(f"💱 Валюта: <b>{esc(cur)}</b>.", reply_markup=main_kb())
    await message.answer(settings_text(u), reply_markup=settings_kb(u, get_admin_id() == u["user_id"]))


@router.message(Flow.set_reminder, F.text)
async def st_set_reminder(message: Message, state: FSMContext) -> None:
    hm = L.parse_hhmm(message.text)
    if not hm:
        await message.answer("Формат <code>ЧЧ:ММ</code>, напр. <code>21:00</code>")
        return
    await state.clear()
    db.update_user(message.from_user.id, reminder_hour=hm[0], reminder_min=hm[1])
    u = db.get_user(message.from_user.id)
    await message.answer(f"⏰ Буду напоминать в <b>{hm[0]:02d}:{hm[1]:02d}</b>.", reply_markup=main_kb())
    await message.answer(settings_text(u), reply_markup=settings_kb(u, get_admin_id() == u["user_id"]))


# --- Callback-и: долги -----------------------------------------------------

@router.callback_query(F.data.startswith("d:add:"))
async def cb_debt_add(cq: CallbackQuery, state: FSMContext) -> None:
    direction = cq.data.split(":", 2)[2]
    await state.set_state(Flow.debt_counterparty)
    await state.update_data(direction=direction)
    ask = "Кому ты должен? Напиши имя." if direction == "i_owe" else "Кто тебе должен? Напиши имя."
    await cq_send(cq, ask + "\n(/cancel — отмена)")
    await cq.answer()


@router.callback_query(F.data.startswith("d:settle:"))
async def cb_debt_settle(cq: CallbackQuery) -> None:
    debt_id = int(cq.data.rsplit(":", 1)[1])
    done = db.settle_debt(cq.from_user.id, debt_id)
    await cq.answer("Закрыто ✅" if done else "Не найдено")
    u = db.get_user(cq.from_user.id)
    await safe_edit(cq, debts_text(u), debts_kb(u["user_id"]))


# --- Callback-и: сводка ----------------------------------------------------

@router.callback_query(F.data.startswith("s:m:"))
async def cb_summary_month(cq: CallbackQuery) -> None:
    ym = cq.data.split(":", 2)[2]
    u = db.get_user(cq.from_user.id)
    await safe_edit(cq, summary_text(u, ym), summary_kb(ym))
    await cq.answer()


@router.callback_query(F.data == "s:today")
async def cb_summary_today(cq: CallbackQuery) -> None:
    u = db.get_user(cq.from_user.id)
    now = L.local_now(u["tz"])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀ К сводке за месяц", callback_data=f"s:m:{L.ym(now)}")
    ]])
    await safe_edit(cq, today_text(u, now), kb)
    await cq.answer()


# --- Callback-и: настройки -------------------------------------------------

@router.callback_query(F.data == "set:income")
async def cb_set_income(cq: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Flow.set_income)
    await cq_send(cq, "Введи новый <b>доход в месяц</b> (число), напр. <code>100000</code>\n(/cancel — отмена)")
    await cq.answer()


@router.callback_query(F.data == "set:hours")
async def cb_set_hours(cq: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Flow.set_hours)
    await cq_send(
        cq, "Сколько <b>рабочих часов в месяц</b>? Напр. <code>160</code> (8 ч × 20 дней).\n(/cancel — отмена)"
    )
    await cq.answer()


@router.callback_query(F.data == "set:currency")
async def cb_set_currency(cq: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Flow.set_currency)
    await cq_send(cq, "Пришли символ валюты: <code>₽</code>, <code>$</code>, <code>R$</code>…\n(/cancel — отмена)")
    await cq.answer()


@router.callback_query(F.data == "set:reminder")
async def cb_set_reminder(cq: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Flow.set_reminder)
    await cq_send(cq, "Во сколько напоминать вечером? Формат <code>ЧЧ:ММ</code>, напр. <code>21:00</code>\n(/cancel — отмена)")
    await cq.answer()


@router.callback_query(F.data == "set:daily")
async def cb_set_daily(cq: CallbackQuery) -> None:
    u = db.get_user(cq.from_user.id)
    new_val = 0 if u["daily_reminder"] else 1
    db.update_user(u["user_id"], daily_reminder=new_val)
    u = db.get_user(cq.from_user.id)
    await cq.answer("🔔 Вкл" if new_val else "🔕 Выкл")
    await safe_edit(cq, settings_text(u), settings_kb(u, get_admin_id() == u["user_id"]))


@router.callback_query(F.data == "set:admin_users")
async def cb_admin_users(cq: CallbackQuery) -> None:
    if get_admin_id() != cq.from_user.id:
        await cq.answer("Только для админа", show_alert=True)
        return
    users = db.all_users()
    lines = [f"👥 <b>Пользователи ({len(users)})</b>\n"]
    for us in users[:50]:
        un = f"@{us['username']}" if us["username"] else "—"
        lines.append(f"• {esc(us['first_name'] or '—')} ({esc(un)}) · id <code>{us['user_id']}</code>")
    if len(users) > 50:
        lines.append(f"…и ещё {len(users) - 50}")
    lines.append("\n<i>Траты и долги других пользователей никому не видны — только их количество.</i>")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀ Назад", callback_data="set:back")
    ]])
    await safe_edit(cq, "\n".join(lines), kb)
    await cq.answer()


@router.callback_query(F.data == "set:back")
async def cb_settings_back(cq: CallbackQuery) -> None:
    u = db.get_user(cq.from_user.id)
    await safe_edit(cq, settings_text(u), settings_kb(u, get_admin_id() == u["user_id"]))
    await cq.answer()


# --- Callback: удалить трату ----------------------------------------------

@router.callback_query(F.data.startswith("x:del:"))
async def cb_del_expense(cq: CallbackQuery) -> None:
    eid = int(cq.data.rsplit(":", 1)[1])
    done = db.delete_expense(cq.from_user.id, eid)
    await cq.answer("Удалено 🗑" if done else "Уже удалено")
    if done:
        await safe_edit(cq, "🗑 Трата удалена.", None)


# --- Свободный ввод (по умолчанию = записать трату) -----------------------

@router.message(StateFilter(None), F.text)
async def free_text(message: Message) -> None:
    parsed = L.parse_amount(message.text)
    if not parsed:
        await message.answer(
            "Не понял 🤔 Пришли сумму цифрами, напр. <code>500 такси</code>, "
            "или выбери кнопку ниже.",
            reply_markup=main_kb(),
        )
        return
    u = db.get_user(message.from_user.id)
    await save_and_reply(message, u, parsed[0], parsed[1])


# --- Планировщик: вечернее напоминание, долги, месячная сводка ------------

async def send_evening(u: dict, now) -> None:
    uid = u["user_id"]
    cur = u["currency"]
    ymd = L.ymd(now)
    parts: list[str] = []

    if u["daily_reminder"]:
        d_total, d_n = db.day_total(uid, ymd)
        if d_n:
            parts.append(
                "🌙 Вечерняя сверка. Сегодня уже записано: "
                f"<b>{esc(L.fmt_money(d_total, cur))}</b> ({d_n} трат).\n"
                "Ничего не забыл добавить?"
            )
        else:
            parts.append("🌙 Не забудь записать траты за сегодня — просто пришли сумму.")

    due = db.due_debts(uid, ymd)
    if due:
        lines = ["⏰ <b>Напоминание о долгах</b> (срок наступил):"]
        for d in due:
            mark = "🔴 ты должен" if d["direction"] == "i_owe" else "🟢 тебе должны"
            due_s = L.iso_to_display(d["due_date"])
            lines.append(f"• {mark}: {esc(d['counterparty'] or '—')} — "
                         f"{esc(L.fmt_money(d['amount'], cur))} (до {due_s})")
        parts.append("\n".join(lines))

    if parts:
        try:
            await bot.send_message(uid, "\n\n".join(parts), reply_markup=main_kb())
        except TelegramForbiddenError:
            db.update_user(uid, active=0)   # юзер заблокировал бота — исключаем из рассылки
        except Exception as e:  # noqa: BLE001
            log.warning("evening → %s: %s", uid, e)


async def send_month_summary(u: dict, ym: str) -> None:
    try:
        await bot.send_message(
            u["user_id"],
            f"🗓 <b>Сводка за {L.ym_display(ym)} — месяц закрылся.</b>\n\n" + summary_text(u, ym),
            reply_markup=main_kb(),
        )
    except TelegramForbiddenError:
        db.update_user(u["user_id"], active=0)
    except Exception as e:  # noqa: BLE001
        log.warning("month summary → %s: %s", u["user_id"], e)


async def evening_tick() -> None:
    """Раз в минуту: у кого наступило вечернее время — шлём напоминание/долги,
    а в начале нового месяца — сводку за прошлый.

    Guard (last_evening_date / last_month_summary) выставляется ДО отправки —
    так при сбое максимум пропустим одно уведомление, а не зашлём его повторно.
    """
    for u in db.all_users(active_only=True):
        try:
            uid = u["user_id"]
            now = L.local_now(u["tz"])
            if not L.reminder_reached(now, u["reminder_hour"], u["reminder_min"]):
                continue
            today = L.ymd(now)
            prev = L.prev_ym(L.ym(now))

            # Вечернее напоминание — один раз в день.
            if u["last_evening_date"] != today:
                db.update_user(uid, last_evening_date=today)
                await send_evening(u, now)

            # Сводка за прошлый месяц — при первом вечере нового месяца
            # (не зависит от того, работал ли бот в последний день месяца).
            if u["last_month_summary"] != prev:
                db.update_user(uid, last_month_summary=prev)
                _, n = db.month_total(uid, prev)
                if n > 0:
                    await send_month_summary(u, prev)
        except Exception as e:  # noqa: BLE001
            log.warning("tick user %s: %s", u.get("user_id"), e)


# --- Запуск ---------------------------------------------------------------

async def main() -> None:
    db.init_db()
    m = EnsureUserMiddleware()
    dp.message.middleware(m)
    dp.callback_query.middleware(m)
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(evening_tick, "cron", minute="*", id="evening", max_instances=1)
    scheduler.start()

    await bot.set_my_commands([
        BotCommand(command="start", description="Меню и справка"),
        BotCommand(command="menu", description="Показать меню"),
        BotCommand(command="cancel", description="Отменить текущее действие"),
        BotCommand(command="help", description="Справка"),
    ])
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
