"""
Чистая логика бота учёта трат — без Telegram и без БД.

Здесь только функции, которые можно протестировать в изоляции:
разбор сумм и дат, перевод денег в «часы твоего времени»,
форматирование денег/времени и работа с датами по таймзоне пользователя.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


# --- Разбор пользовательского ввода --------------------------------------

def _to_float(raw: str) -> float | None:
    """Превращает строку вида «1 500», «1.500», «12,50», «2 000.50» в число.

    Правила: пробелы — разделители разрядов; если есть и точка, и запятая —
    десятичным считается последний из них; одиночный разделитель с 1–2
    цифрами после — десятичный, иначе — разделитель тысяч.
    """
    s = re.sub(r"\s", "", raw or "")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):      # 1.234,56 → запятая десятичная
            s = s.replace(".", "").replace(",", ".")
        else:                                # 1,234.56 → точка десятичная
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            s = parts[0] + "." + parts[1]
        else:
            s = s.replace(",", "")
    elif "." in s:
        parts = s.split(".")
        if not (len(parts) == 2 and 1 <= len(parts[1]) <= 2):
            s = s.replace(".", "")           # 1.500 / 1.234.567 → тысячи
    try:
        return float(s)
    except ValueError:
        return None


def parse_amount(text: str):
    """Из строки «1500 такси домой» возвращает (1500.0, 'такси домой').

    Возвращает None, если в начале строки нет положительного числа.
    """
    if text is None:
        return None
    m = re.match(r"^\s*([0-9][\d\s.,]*)\s*(.*)$", text, re.S)
    if not m:
        return None
    amount = _to_float(m.group(1))
    # Отбраковываем не-конечные (inf/nan из ~300+ цифр) и абсурдно большие
    # значения — иначе inf попадёт в БД и навсегда уронит форматирование сумм.
    if amount is None or not math.isfinite(amount) or amount <= 0 or amount > 1e15:
        return None
    note = m.group(2).strip()
    return amount, note


def category_of(note: str) -> str:
    """Категория трат = первое слово заметки (с заглавной), иначе «Прочее»."""
    note = (note or "").strip()
    if not note:
        return "Прочее"
    word = re.split(r"[\s,;.]+", note, maxsplit=1)[0]
    return (word[:1].upper() + word[1:]) if word else "Прочее"


def parse_hhmm(s: str):
    """«21:00» / «21.00» / «9 30» / «9» → (h, m). None, если не распознано."""
    s = (s or "").strip()
    m = re.match(r"^(\d{1,2})[:.\s](\d{2})$", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
    else:
        m = re.match(r"^(\d{1,2})$", s)
        if not m:
            return None
        h, mi = int(m.group(1)), 0
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return h, mi
    return None


def parse_due_date(s: str, today: date):
    """Разбор срока долга.

    Возвращает:
      None         — «нет срока» (пользователь написал нет/-/пусто);
      'YYYY-MM-DD' — корректная дата;
      'invalid'    — не удалось распознать.
    """
    s = (s or "").strip().lower()
    if s in ("нет", "-", "", "no", "0", "без", "не"):
        return None
    m = re.match(r"^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?$", s)
    if not m:
        return "invalid"
    d, mo = int(m.group(1)), int(m.group(2))
    if m.group(3):
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
    else:
        y = today.year
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return "invalid"


# --- Деньги → часы твоего времени ----------------------------------------

def hours_for(amount: float, monthly_income: float, work_hours: float):
    """Сколько рабочих часов «стоит» сумма. None, если ставка не определена."""
    try:
        if monthly_income and monthly_income > 0 and work_hours and work_hours > 0:
            rate = monthly_income / work_hours          # доход за час
            return amount / rate
    except (TypeError, ZeroDivisionError):
        pass
    return None


# --- Форматирование ------------------------------------------------------

def _group(intpart: str) -> str:
    return f"{int(intpart):,}".replace(",", " ")


def fmt_money(x, cur: str = "₽") -> str:
    """12500 → «12 500 ₽»; 12500.5 → «12 500,50 ₽»."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        x = 0.0
    if not math.isfinite(x):
        return "—"
    if abs(x - round(x)) < 0.005:
        body = _group(str(int(round(x))))
    else:
        s = f"{x:,.2f}"                                  # 12,500.50
        # запятую-разделитель тысяч → пробел, точку-десятичную → запятую
        body = s.replace(",", "\x00").replace(".", ",").replace("\x00", " ")
    cur = (cur or "").strip()
    return f"{body} {cur}".strip()


def fmt_hours(h) -> str:
    """Часы (float) → «12 ч 30 мин (≈ 1.6 раб. дн.)»."""
    if h is None:
        return "—"
    try:
        h = float(h)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(h):
        return "—"
    total_min = int(round(h * 60))
    if total_min <= 0:
        return "меньше минуты"
    hh, mm = divmod(total_min, 60)
    if hh and mm:
        base = f"{hh} ч {mm} мин"
    elif hh:
        base = f"{hh} ч"
    else:
        base = f"{mm} мин"
    if total_min >= 480:      # порог «раб. дн.» считаем по тем же округлённым минутам
        base += f" (≈ {total_min / 480:.1f} раб. дн.)"
    return base


def iso_to_display(iso) -> str:
    """'2026-08-15' → '15.08.2026'. Пусто → ''."""
    if not iso:
        return ""
    try:
        return date.fromisoformat(iso).strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return str(iso)


def ym_display(ym_str: str) -> str:
    """'2026-07' → 'июль 2026'."""
    months = ["", "январь", "февраль", "март", "апрель", "май", "июнь",
              "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
    try:
        y, m = ym_str.split("-")
        return f"{months[int(m)]} {y}"
    except (ValueError, IndexError):
        return ym_str


# --- Дата/время по таймзоне пользователя ---------------------------------

def local_now(tz: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz))
    except Exception:
        return datetime.now(ZoneInfo("UTC"))


def ymd(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def ym(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def prev_ym(ym_str: str) -> str:
    y, m = map(int, ym_str.split("-"))
    m -= 1
    if m == 0:
        m, y = 12, y - 1
    return f"{y:04d}-{m:02d}"


def is_last_day_of_month(dt: datetime) -> bool:
    return (dt + timedelta(days=1)).month != dt.month


def reminder_reached(dt: datetime, hour: int, minute: int) -> bool:
    """Наступило ли (по локальному времени) время напоминания сегодня."""
    return (dt.hour, dt.minute) >= (int(hour), int(minute))
