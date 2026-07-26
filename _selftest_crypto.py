"""Проверка шифрования базы: на диске — шифртекст, через API — расшифровка."""
import os, tempfile, sqlite3
from cryptography.fernet import Fernet

tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(tmp, "enc.db")
os.environ["DB_KEY"] = Fernet.generate_key().decode()   # ключ задан ДО импорта db

import db   # noqa: E402

ok = fail = 0
def check(name, cond):
    global ok, fail
    if cond: ok += 1
    else:
        fail += 1; print("  FAIL:", name)

db.init_db()
db.ensure_user(1, "u", "U", "ts")
db.add_expense(1, 12345, "Такси", "такси в аэропорт", "2026-07", "2026-07-26", "ts")
db.add_debt(1, "i_owe", "Вася Пупкин", 5000, "за обед", "2026-07-30", "ts")

# 1) Через API всё читается в открытом виде
total, n = db.month_total(1, "2026-07")
check("сумма расшифрована", total == 12345.0 and n == 1)
br = db.month_breakdown(1, "2026-07")
check("категория расшифрована", br and br[0]["category"] == "Такси")
exps = db.day_expenses(1, "2026-07-26")
check("заметка расшифрована", exps and exps[0]["note"] == "такси в аэропорт")
debts = db.list_debts(1)
check("контрагент расшифрован", debts and debts[0]["counterparty"] == "Вася Пупкин")
check("сумма долга расшифрована", debts[0]["amount"] == 5000.0)

# 2) На диске (читаем сырую базу мимо db.py) — данных в открытом виде НЕТ
raw = sqlite3.connect(os.environ["DB_PATH"])
amount_cell, cat_cell, note_cell = raw.execute(
    "SELECT amount, category, note FROM expenses LIMIT 1").fetchone()
cp_cell = raw.execute("SELECT counterparty FROM debts LIMIT 1").fetchone()[0]
raw.close()
check("сумма на диске зашифрована", "12345" not in str(amount_cell))
check("категория на диске зашифрована", "Такси" not in str(cat_cell))
check("заметка на диске зашифрована", "аэропорт" not in str(note_cell))
check("контрагент на диске зашифрован", "Вася" not in str(cp_cell))

# 3) Весь файл базы не содержит осмысленных строк
with open(os.environ["DB_PATH"], "rb") as f:
    blob = f.read()
check("файл базы не содержит 'Такси'", "Такси".encode() not in blob)
check("файл базы не содержит 'Вася'", "Вася".encode() not in blob)

print(f"\nИТОГО: {ok} ok, {fail} fail")
raise SystemExit(1 if fail else 0)
