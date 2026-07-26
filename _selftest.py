"""Быстрая самопроверка чистой логики и хранилища (без Telegram)."""
import os, tempfile
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")

from datetime import date
import logic as L
import db

ok = 0
fail = 0

def check(name, cond):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print(f"  FAIL: {name}")

# --- parse_amount ---
check("500 такси", L.parse_amount("500 такси") == (500.0, "такси"))
check("1 500 продукты", L.parse_amount("1 500 продукты") == (1500.0, "продукты"))
check("2 000.50 кафе", L.parse_amount("2 000.50 кафе") == (2000.5, "кафе"))
check("12,50 кофе", L.parse_amount("12,50 кофе") == (12.5, "кофе"))
check("350 только число", L.parse_amount("350") == (350.0, ""))
check("1.500 такси(тысячи)", L.parse_amount("1.500 такси") == (1500.0, "такси"))
check("1.234,56 евро", L.parse_amount("1.234,56 x")[0] == 1234.56)
check("не число", L.parse_amount("привет") is None)
check("ноль отклонён", L.parse_amount("0 такси") is None)
check("минус отклонён", L.parse_amount("-5") is None)

# --- category ---
check("категория первое слово", L.category_of("такси домой") == "Такси")
check("категория пусто", L.category_of("") == "Прочее")

# --- hours_for ---
h = L.hours_for(500, 100000, 160)      # ставка 625/ч → 0.8 ч
check("часы 500@100k/160", abs(h - 0.8) < 1e-6)
check("часы деление на 0 дохода", L.hours_for(500, 0, 160) is None)
check("часы 0 часов", L.hours_for(500, 100000, 0) is None)

# --- форматирование ---
check("деньги целое", L.fmt_money(12500, "₽") == "12 500 ₽")
check("деньги дробное", L.fmt_money(12500.5, "₽") == "12 500,50 ₽")
check("часы формат", L.fmt_hours(0.8) == "48 мин")
check("часы >8", "раб. дн." in L.fmt_hours(10))
check("iso→display", L.iso_to_display("2026-08-15") == "15.08.2026")
check("ym display", L.ym_display("2026-07") == "июль 2026")
check("prev_ym через год", L.prev_ym("2026-01") == "2025-12")

# --- parse_hhmm / parse_due_date ---
check("hhmm 21:00", L.parse_hhmm("21:00") == (21, 0))
check("hhmm 9", L.parse_hhmm("9") == (9, 0))
check("hhmm мусор", L.parse_hhmm("abc") is None)
check("hhmm 25:00 невалид", L.parse_hhmm("25:00") is None)
check("due нет", L.parse_due_date("нет", date(2026, 7, 26)) is None)
check("due 15.08", L.parse_due_date("15.08", date(2026, 7, 26)) == "2026-08-15")
check("due 15.08.2027", L.parse_due_date("15.08.2027", date(2026, 7, 26)) == "2027-08-15")
check("due невалид", L.parse_due_date("99.99", date(2026, 7, 26)) == "invalid")

# --- db: изоляция пользователей ---
db.init_db()
db.ensure_user(1, "alice", "Alice", "2026-07-26T00:00:00Z")
new2 = db.ensure_user(2, "bob", "Bob", "2026-07-26T00:00:00Z")
check("bob новый", new2 is True)
check("bob повторно не новый", db.ensure_user(2, "bob", "Bob", "x") is False)

db.add_expense(1, 500, "Такси", "такси", "2026-07", "2026-07-26", "ts")
db.add_expense(1, 1500, "Еда", "еда", "2026-07", "2026-07-26", "ts")
db.add_expense(2, 9999, "Секрет", "секрет", "2026-07", "2026-07-26", "ts")

s1, n1 = db.month_total(1, "2026-07")
s2, n2 = db.month_total(2, "2026-07")
check("alice видит только своё", (s1, n1) == (2000.0, 2))
check("bob видит только своё", (s2, n2) == (9999.0, 1))
br = db.month_breakdown(1, "2026-07")
check("разбивка alice 2 категории", len(br) == 2 and br[0]["s"] == 1500)

# alice не может удалить трату bob
bob_exp = db.month_breakdown(2, "2026-07")
check("delete чужого не проходит", db.delete_expense(1, 3) is False)  # id=3 принадлежит bob
check("bob трата на месте", db.month_total(2, "2026-07")[1] == 1)

# --- db: долги ---
db.add_debt(1, "i_owe", "Вася", 5000, "", "2026-07-01", "ts")   # просрочен
db.add_debt(1, "owed_to_me", "Петя", 3000, "", None, "ts")
due = db.due_debts(1, "2026-07-26")
check("просроченный долг найден", len(due) == 1 and due[0]["counterparty"] == "Вася")
check("долг без срока не в due", all(d["due_date"] for d in due))
check("bob чужих долгов не видит", db.list_debts(2) == [])
did = db.list_debts(1)[0]["id"]
check("bob не закроет чужой долг", db.settle_debt(2, did) is False)
check("alice закрывает свой долг", db.settle_debt(1, did) is True)

# --- meta / admin ---
db.set_meta("admin_id", "1")
check("meta admin", db.get_meta("admin_id") == "1")
db.set_meta("admin_id", "42")
check("meta upsert", db.get_meta("admin_id") == "42")

# --- update_user белый список ---
db.update_user(1, monthly_income=200000)
check("доход обновлён", db.get_user(1)["monthly_income"] == 200000)
db.update_user(1, created_at="ХАК", monthly_income=150000)  # created_at не в белом списке
u = db.get_user(1)
check("не-белое поле игнорируется", u["created_at"] != "ХАК")
check("белое поле применилось", u["monthly_income"] == 150000)

# --- защита от inf/nan (правки ревью) ---
check("огромное число отклонено", L.parse_amount("9" * 400) is None)
check("сумма сверх лимита отклонена", L.parse_amount("2000000000000000") is None)  # 2e15 > 1e15
check("нормальный максимум ок", L.parse_amount("1000000000000000")[0] == 1e15)      # ровно 1e15
check("fmt_money(inf) не падает", L.fmt_money(float("inf")) == "—")
check("fmt_money(nan) не падает", L.fmt_money(float("nan")) == "—")
check("fmt_hours(inf) не падает", L.fmt_hours(float("inf")) == "—")
check("порог раб.дн. согласован (7.999)", "раб. дн." in L.fmt_hours(7.999))
check("порог раб.дн. согласован (8.0)", "раб. дн." in L.fmt_hours(8.0))
check("нет раб.дн. до порога", "раб. дн." not in L.fmt_hours(7.5))

# --- активность пользователей (деактивация заблокировавших) ---
db.update_user(2, active=0)
active_ids = {x["user_id"] for x in db.all_users(active_only=True)}
check("деактивированный вне рассылки", 2 not in active_ids)
check("активный в рассылке", 1 in active_ids)
db.ensure_user(2, "bob", "Bob", "x")   # вернулся — реактивация
check("реактивация при возврате", 2 in {x["user_id"] for x in db.all_users(active_only=True)})

print(f"\nИТОГО: {ok} ok, {fail} fail")
raise SystemExit(1 if fail else 0)
