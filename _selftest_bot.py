"""Интеграционная проверка планировщика и сохранения траты без сети."""
import os, tempfile, asyncio
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["BOT_TOKEN"] = "123456:AAdummyTokenForTests000000000000000"

import bot as B
import db
import logic as L

ok = fail = 0
def check(name, cond):
    global ok, fail
    if cond: ok += 1
    else:
        fail += 1; print("  FAIL:", name)

# Записываем исходящие "сообщения" вместо реальной отправки в Telegram
sent = []
async def fake_send(chat_id, text, **kw):
    sent.append((chat_id, text))
B.bot.send_message = fake_send  # type: ignore

db.init_db()
db.ensure_user(7, "u", "U", "ts")
# reminder в 00:00 и tz UTC → время напоминания всегда "наступило"
db.update_user(7, reminder_hour=0, reminder_min=0, tz="UTC", daily_reminder=1)

now = L.local_now("UTC")
today = L.ymd(now)
db.add_expense(7, 500, "Такси", "такси", L.ym(now), today, "ts")
db.add_debt(7, "i_owe", "Вася", 5000, "", today, "ts")   # срок = сегодня → напомнить

async def run():
    await B.evening_tick()
    check("вечернее ушло", len(sent) == 1)
    check("в тексте есть траты/долги", "такси" in sent[0][1].lower() or "долг" in sent[0][1].lower())
    check("last_evening_date проставлен", db.get_user(7)["last_evening_date"] == today)
    # повторный тик в тот же день — не должен слать снова
    sent.clear()
    await B.evening_tick()
    check("повторно не шлём", len(sent) == 0)

    # сохранение траты через save_and_reply (подменяем message.answer)
    class FakeMsg:
        def __init__(self):
            self.replies = []
            class U: id = 7
            self.from_user = U()
        async def answer(self, text, **kw):
            self.replies.append(text)
    u = db.get_user(7)
    fm = FakeMsg()
    await B.save_and_reply(fm, u, 5000, "ноутбук")
    check("ответ на трату есть", len(fm.replies) == 1)
    check("в ответе часы", "твоего времени" in fm.replies[0])
    check("трата записана в БД", db.month_total(7, L.ym(now))[1] == 2)

asyncio.run(run())
print(f"\nИТОГО: {ok} ok, {fail} fail")
raise SystemExit(1 if fail else 0)
