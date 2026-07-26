"""
SQLite-хранилище бота учёта трат.

Изоляция пользователей — на уровне данных: каждая трата и каждый долг
привязаны к user_id, и все выборки обязательно фильтруются по user_id.
Один пользователь физически не может получить строки другого.

Только стандартная библиотека (sqlite3) — никаких платных зависимостей.
Соединение открывается на каждую операцию: это безопасно при работе из
разных потоков (планировщик) и достаточно быстро для личного бота.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_PATH = os.getenv("DB_PATH", str(Path(__file__).parent / "data.db"))

# --- Шифрование данных (Fernet = AES-128-CBC + HMAC) ---------------------
# Ключ берётся из .env (DB_KEY) и в git не попадает. Суммы, категории,
# заметки и контрагенты хранятся на диске зашифрованными: даже если кто-то
# заполучит файл data.db без ключа — прочитать траты и долги не сможет.
# Если DB_KEY не задан (напр. в юнит-тестах) — данные пишутся как есть.
_DB_KEY = os.getenv("DB_KEY", "").strip()
if _DB_KEY:
    from cryptography.fernet import Fernet
    _fernet = Fernet(_DB_KEY.encode())
else:
    _fernet = None


def enc(value) -> str:
    """Зашифровать значение в строку (или вернуть как есть без ключа)."""
    s = "" if value is None else str(value)
    if _fernet is None:
        return s
    return _fernet.encrypt(s.encode()).decode()


def dec(blob):
    """Расшифровать строку. Битые/незашифрованные данные вернём как есть."""
    if blob is None or _fernet is None:
        return blob
    try:
        raw = blob.encode() if isinstance(blob, str) else blob
        return _fernet.decrypt(raw).decode()
    except Exception:      # noqa: BLE001
        return blob


def _dec_amount(blob) -> float:
    try:
        return float(dec(blob))
    except (TypeError, ValueError):
        return 0.0


def _row_expense(r) -> dict:
    d = dict(r)
    d["amount"] = _dec_amount(d.get("amount"))
    d["category"] = dec(d.get("category"))
    d["note"] = dec(d.get("note"))
    return d


def _row_debt(r) -> dict:
    d = dict(r)
    d["counterparty"] = dec(d.get("counterparty"))
    d["amount"] = _dec_amount(d.get("amount"))
    d["note"] = dec(d.get("note"))
    return d


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users(
    user_id            INTEGER PRIMARY KEY,
    username           TEXT,
    first_name         TEXT,
    monthly_income     REAL    NOT NULL DEFAULT 100000,
    work_hours         REAL    NOT NULL DEFAULT 160,
    currency           TEXT    NOT NULL DEFAULT '₽',
    tz                 TEXT    NOT NULL DEFAULT 'America/Sao_Paulo',
    reminder_hour      INTEGER NOT NULL DEFAULT 21,
    reminder_min       INTEGER NOT NULL DEFAULT 0,
    daily_reminder     INTEGER NOT NULL DEFAULT 1,
    active             INTEGER NOT NULL DEFAULT 1,
    last_evening_date  TEXT,
    last_month_summary TEXT,
    created_at         TEXT
);

CREATE TABLE IF NOT EXISTS expenses(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    amount   REAL    NOT NULL,
    category TEXT,
    note     TEXT,
    ym       TEXT    NOT NULL,   -- '2026-07' по таймзоне пользователя
    ymd      TEXT    NOT NULL,   -- '2026-07-26'
    ts_utc   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exp_user_ym  ON expenses(user_id, ym);
CREATE INDEX IF NOT EXISTS idx_exp_user_ymd ON expenses(user_id, ymd);

CREATE TABLE IF NOT EXISTS debts(
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    direction    TEXT    NOT NULL,   -- 'i_owe' (я должен) | 'owed_to_me' (мне должны)
    counterparty TEXT,
    amount       REAL    NOT NULL,
    note         TEXT,
    due_date     TEXT,               -- 'YYYY-MM-DD' или NULL
    settled      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_debt_user ON debts(user_id, settled);
"""

# Только эти поля можно менять через update_user — защита от инъекции имён колонок.
_ALLOWED_FIELDS = {
    "username", "first_name", "monthly_income", "work_hours", "currency", "tz",
    "reminder_hour", "reminder_min", "daily_reminder", "active",
    "last_evening_date", "last_month_summary",
}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def _ensure_column(c: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)
        # Миграции для баз, созданных ранней версией схемы.
        _ensure_column(c, "users", "active", "INTEGER NOT NULL DEFAULT 1")


# --- meta (глобальные настройки, напр. id админа) ------------------------

def get_meta(key: str):
    with _conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


# --- пользователи --------------------------------------------------------

def ensure_user(user_id: int, username, first_name, created_at: str) -> bool:
    """Создаёт пользователя, если его нет. Возвращает True, если он новый."""
    with _conn() as c:
        exists = c.execute(
            "SELECT 1 FROM users WHERE user_id=?", (user_id,)
        ).fetchone() is not None
        if exists:
            # active=1 — если пользователь вернулся после блокировки бота.
            c.execute(
                "UPDATE users SET username=?, first_name=?, active=1 WHERE user_id=?",
                (username, first_name, user_id),
            )
        else:
            c.execute(
                "INSERT INTO users(user_id, username, first_name, created_at) "
                "VALUES(?, ?, ?, ?)",
                (user_id, username, first_name, created_at),
            )
    return not exists


def get_user(user_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def update_user(user_id: int, **fields) -> None:
    keys = [k for k in fields if k in _ALLOWED_FIELDS]
    if not keys:
        return
    assignments = ", ".join(f"{k}=?" for k in keys)   # имена колонок — из белого списка
    values = [fields[k] for k in keys] + [user_id]
    with _conn() as c:
        c.execute(f"UPDATE users SET {assignments} WHERE user_id=?", values)


def all_users(active_only: bool = False) -> list[dict]:
    q = "SELECT * FROM users"
    if active_only:
        q += " WHERE active=1"
    with _conn() as c:
        rows = c.execute(q).fetchall()
    return [dict(r) for r in rows]


def users_count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


# --- траты ---------------------------------------------------------------

def add_expense(user_id: int, amount: float, category: str, note: str,
                ym: str, ymd: str, ts_utc: str) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO expenses(user_id, amount, category, note, ym, ymd, ts_utc) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (user_id, enc(amount), enc(category), enc(note), ym, ymd, ts_utc),
        )
        return cur.lastrowid


def delete_expense(user_id: int, expense_id: int) -> bool:
    """Удаляет трату — только если она принадлежит этому пользователю."""
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM expenses WHERE id=? AND user_id=?", (expense_id, user_id)
        )
        return cur.rowcount > 0


# Суммы зашифрованы, поэтому агрегация (SUM/GROUP BY) делается в Python,
# а не в SQL — по строкам конкретного пользователя (данные изолированы).

def month_total(user_id: int, ym: str) -> tuple[float, int]:
    with _conn() as c:
        rows = c.execute(
            "SELECT amount FROM expenses WHERE user_id=? AND ym=?", (user_id, ym)
        ).fetchall()
    return sum(_dec_amount(r["amount"]) for r in rows), len(rows)


def day_total(user_id: int, ymd: str) -> tuple[float, int]:
    with _conn() as c:
        rows = c.execute(
            "SELECT amount FROM expenses WHERE user_id=? AND ymd=?", (user_id, ymd)
        ).fetchall()
    return sum(_dec_amount(r["amount"]) for r in rows), len(rows)


def day_expenses(user_id: int, ymd: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM expenses WHERE user_id=? AND ymd=? ORDER BY id",
            (user_id, ymd),
        ).fetchall()
    return [_row_expense(r) for r in rows]


def month_breakdown(user_id: int, ym: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT category, amount FROM expenses WHERE user_id=? AND ym=?",
            (user_id, ym),
        ).fetchall()
    agg: dict[str, list] = {}
    for r in rows:
        cat = dec(r["category"]) or "Прочее"
        e = agg.setdefault(cat, [0.0, 0])
        e[0] += _dec_amount(r["amount"])
        e[1] += 1
    out = [{"category": k, "s": v[0], "n": v[1]} for k, v in agg.items()]
    out.sort(key=lambda x: x["s"], reverse=True)
    return out


# --- долги ---------------------------------------------------------------

def add_debt(user_id: int, direction: str, counterparty: str, amount: float,
             note: str, due_date, created_at: str) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO debts(user_id, direction, counterparty, amount, note, "
            "due_date, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (user_id, direction, enc(counterparty), enc(amount), enc(note),
             due_date, created_at),
        )
        return cur.lastrowid


def list_debts(user_id: int, settled: int = 0, direction: str | None = None) -> list[dict]:
    q = "SELECT * FROM debts WHERE user_id=? AND settled=?"
    params: list = [user_id, settled]
    if direction:
        q += " AND direction=?"
        params.append(direction)
    q += " ORDER BY (due_date IS NULL), due_date, id"
    with _conn() as c:
        rows = c.execute(q, params).fetchall()
    return [_row_debt(r) for r in rows]


def settle_debt(user_id: int, debt_id: int) -> bool:
    """Закрывает долг — только свой."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE debts SET settled=1 WHERE id=? AND user_id=? AND settled=0",
            (debt_id, user_id),
        )
        return cur.rowcount > 0


def due_debts(user_id: int, today_ymd: str) -> list[dict]:
    """Незакрытые долги со сроком, наступившим или просроченным."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM debts WHERE user_id=? AND settled=0 "
            "AND due_date IS NOT NULL AND due_date<=? ORDER BY due_date",
            (user_id, today_ymd),
        ).fetchall()
    return [_row_debt(r) for r in rows]
