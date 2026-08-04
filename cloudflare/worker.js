/**
 * Traty Bot — версия для Cloudflare Workers.
 *
 * Serverless: Telegram шлёт апдейты по webhook, воркер обрабатывает и отвечает
 * через Bot API. Хранилище — Cloudflare KV. Данные (суммы, категории, заметки,
 * контрагенты) шифруются через Web Crypto (AES-GCM), ключ — секрет DB_KEY.
 * Напоминания и месячная сводка — через Cron Trigger (scheduled).
 *
 * Bindings/секреты (задаются при деплое):
 *   KV   — namespace binding с данными
 *   BOT_TOKEN      — токен бота
 *   DB_KEY         — base64 32 байта, ключ шифрования (AES-256-GCM)
 *   WEBHOOK_SECRET — секрет для проверки заголовка вебхука
 *   ADMIN_ID       — (необязательно) id главного админа; иначе первый /start
 */

// ==== Кнопки меню ========================================================
const BTN = {
  ADD: "➕ Добавить трату",
  CALC: "⏱ Калькулятор",
  DEBTS: "📌 Долги",
  SUMMARY: "📊 Сводка",
  SETTINGS: "⚙️ Настройки",
};
const MAIN_BUTTONS = new Set(Object.values(BTN));

function mainKb() {
  return {
    keyboard: [
      [{ text: BTN.ADD }],
      [{ text: BTN.CALC }, { text: BTN.DEBTS }],
      [{ text: BTN.SUMMARY }, { text: BTN.SETTINGS }],
    ],
    resize_keyboard: true,
  };
}

// Быстрые категории (кнопками при добавлении траты без описания)
const QUICK_CATS = [
  ["🍔 Еда", "Еда"], ["🚕 Транспорт", "Транспорт"], ["🏠 Дом", "Дом"],
  ["🎉 Досуг", "Досуг"], ["💊 Здоровье", "Здоровье"], ["🛒 Покупки", "Покупки"],
  ["📦 Прочее", "Прочее"],
];
function categoryKb() {
  const rows = [];
  for (let i = 0; i < QUICK_CATS.length; i += 2) {
    const r = [{ text: QUICK_CATS[i][0], callback_data: `cat:${i}` }];
    if (QUICK_CATS[i + 1]) r.push({ text: QUICK_CATS[i + 1][0], callback_data: `cat:${i + 1}` });
    rows.push(r);
  }
  return { inline_keyboard: rows };
}
// Эмодзи-полоса для «графиков» в сводке
export function bar(frac, width = 10) {
  const f = Math.max(0, Math.min(1, isFinite(frac) ? frac : 0));
  const filled = Math.round(f * width);
  return "█".repeat(filled) + "░".repeat(width - filled);
}

// Часовые пояса (кнопками). Значения — IANA-имена (Intl в Workers их применяет).
const TZ_LIST = [
  ["Калининград, UTC+2", "Europe/Kaliningrad"],
  ["Москва, UTC+3", "Europe/Moscow"],
  ["Самара, UTC+4", "Europe/Samara"],
  ["Екатеринбург, UTC+5", "Asia/Yekaterinburg"],
  ["Омск, UTC+6", "Asia/Omsk"],
  ["Красноярск, UTC+7", "Asia/Krasnoyarsk"],
  ["Иркутск, UTC+8", "Asia/Irkutsk"],
  ["Якутск, UTC+9", "Asia/Yakutsk"],
  ["Владивосток, UTC+10", "Asia/Vladivostok"],
  ["Магадан, UTC+11", "Asia/Magadan"],
  ["Камчатка, UTC+12", "Asia/Kamchatka"],
  ["Рио, UTC-3", "America/Sao_Paulo"],
];
function tzLabel(tz) {
  const found = TZ_LIST.find((t) => t[1] === tz);
  if (found) return found[0];
  if (tz === "America/Sao_Paulo") return "Рио, UTC-3";
  return tz || "UTC";
}
function tzKb() {
  const rows = [];
  for (let i = 0; i < TZ_LIST.length; i += 2) {
    const r = [{ text: TZ_LIST[i][0], callback_data: `tz:${i}` }];
    if (TZ_LIST[i + 1]) r.push({ text: TZ_LIST[i + 1][0], callback_data: `tz:${i + 1}` });
    rows.push(r);
  }
  rows.push([{ text: "🌐 Другой (ввести смещение)", callback_data: "tz:custom" }]);
  rows.push([{ text: "◀ Назад", callback_data: "set:back" }]);
  return { inline_keyboard: rows };
}
export function parseOffset(s) {
  const m = /^\s*([+-]?)(\d{1,2})(?::?(\d{2}))?\s*$/.exec(s || "");
  if (!m) return null;
  const sign = m[1] === "-" ? -1 : 1;
  const h = parseInt(m[2], 10);
  const mm = m[3] ? parseInt(m[3], 10) : 0;
  if (h > 14 || mm >= 60) return null;
  const off = sign * (h + mm / 60);
  return "UTC" + (off >= 0 ? "+" : "") + off;
}

const esc = (s) =>
  String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

// ==== Telegram Bot API ===================================================
async function tg(env, method, payload) {
  const r = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  return r.json();
}
const send = (env, chatId, text, kb) =>
  tg(env, "sendMessage", {
    chat_id: chatId,
    text,
    parse_mode: "HTML",
    reply_markup: kb,
    disable_web_page_preview: true,
  });
const answerCb = (env, id, text, alert) =>
  tg(env, "answerCallbackQuery", { callback_query_id: id, text, show_alert: !!alert });
async function safeEdit(env, cq, text, kb) {
  const m = cq.message;
  if (!m) {
    await answerCb(env, cq.id, "Сообщение устарело — открой меню: /menu", true);
    return;
  }
  await tg(env, "editMessageText", {
    chat_id: m.chat.id,
    message_id: m.message_id,
    text,
    parse_mode: "HTML",
    reply_markup: kb,
    disable_web_page_preview: true,
  });
}

// ==== Шифрование (AES-GCM) ===============================================
let _keyPromise = null;
function cryptoKey(env) {
  if (!_keyPromise) {
    const raw = Uint8Array.from(atob(env.DB_KEY), (c) => c.charCodeAt(0));
    _keyPromise = crypto.subtle.importKey("raw", raw, { name: "AES-GCM" }, false, [
      "encrypt",
      "decrypt",
    ]);
  }
  return _keyPromise;
}
export async function enc(env, value) {
  const key = await cryptoKey(env);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const data = new TextEncoder().encode(String(value == null ? "" : value));
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, data));
  const buf = new Uint8Array(iv.length + ct.length);
  buf.set(iv);
  buf.set(ct, iv.length);
  let bin = "";
  for (const b of buf) bin += String.fromCharCode(b);
  return btoa(bin);
}
export async function dec(env, blob) {
  if (blob == null) return blob;
  try {
    const key = await cryptoKey(env);
    const raw = Uint8Array.from(atob(blob), (c) => c.charCodeAt(0));
    const iv = raw.slice(0, 12);
    const ct = raw.slice(12);
    const pt = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, key, ct);
    return new TextDecoder().decode(pt);
  } catch (e) {
    return blob; // незашифрованные/битые данные вернём как есть
  }
}
async function decAmount(env, blob) {
  const v = parseFloat(await dec(env, blob));
  return isFinite(v) ? v : 0;
}

// ==== KV: пользователи ===================================================
function defaultUser(from) {
  return {
    id: from.id,
    username: from.username || null,
    first_name: from.first_name || "",
    monthly_income: 100000,
    work_hours: 160,
    monthly_budget: 0,
    currency: "₽",
    tz: "Europe/Moscow",
    reminder_hour: 21,
    reminder_min: 0,
    daily_reminder: 1,
    active: 1,
    indexed: true,
    last_evening_date: null,
    last_month_summary: null,
    state: null,
    state_data: null,
    created_at: new Date().toISOString(),
  };
}
async function getUser(env, id) {
  const v = await env.KV.get(`user:${id}`);
  return v ? JSON.parse(v) : null;
}
async function saveUser(env, u) {
  await env.KV.put(`user:${u.id}`, JSON.stringify(u));
}
// Индекс id пользователей в одном ключе — чтобы cron и админ-счётчик
// не делали KV.list (у него дневной лимит 1000 на бесплатном плане).
async function addToIndex(env, id) {
  const raw = await env.KV.get("idx:users");
  let arr;
  try {
    arr = raw ? JSON.parse(raw) : [];
  } catch (e) {
    arr = [];
  }
  if (!arr.includes(id)) {
    arr.push(id);
    await env.KV.put("idx:users", JSON.stringify(arr));
  }
}
async function ensureUser(env, from) {
  const existing = await getUser(env, from.id);
  if (existing) {
    let changed = false;
    const un = from.username || null;
    if (existing.username !== un) {
      existing.username = un;
      changed = true;
    }
    if (existing.first_name !== (from.first_name || "")) {
      existing.first_name = from.first_name || "";
      changed = true;
    }
    if (existing.active !== 1) {
      existing.active = 1;
      changed = true;
    }
    if (!existing.indexed) {
      existing.indexed = true;
      changed = true;
      await addToIndex(env, from.id); // самолечение индекса для старых пользователей
    }
    if (changed) await saveUser(env, existing);
    return { user: existing, isNew: false };
  }
  const u = defaultUser(from);
  await saveUser(env, u);
  await addToIndex(env, from.id);
  return { user: u, isNew: true };
}
async function allUsers(env, activeOnly) {
  const raw = await env.KV.get("idx:users");
  let ids;
  try {
    ids = raw ? JSON.parse(raw) : [];
  } catch (e) {
    ids = [];
  }
  const out = [];
  for (const id of ids) {
    const v = await env.KV.get(`user:${id}`);
    if (v) {
      const u = JSON.parse(v);
      if (!activeOnly || u.active === 1) out.push(u);
    }
  }
  return out;
}

// ==== KV: траты ==========================================================
async function addExpense(env, userId, amount, category, note, ymd) {
  const id = crypto.randomUUID();
  const val = {
    id,
    amount: await enc(env, amount),
    category: await enc(env, category),
    note: await enc(env, note),
    ts: new Date().toISOString(),
  };
  await env.KV.put(`exp:${userId}:${ymd}:${id}`, JSON.stringify(val));
  return id;
}
async function deleteExpense(env, userId, ymd, id) {
  const key = `exp:${userId}:${ymd}:${id}`;
  const v = await env.KV.get(key);
  if (!v) return false;
  await env.KV.delete(key);
  return true;
}
async function getExpense(env, userId, ymd, id) {
  const v = await env.KV.get(`exp:${userId}:${ymd}:${id}`);
  return v ? JSON.parse(v) : null;
}
async function updateExpense(env, userId, ymd, id, amount, category, note) {
  const key = `exp:${userId}:${ymd}:${id}`;
  const v = await env.KV.get(key); // ключ содержит userId → чужую трату не тронуть
  if (!v) return false;
  const rec = JSON.parse(v);
  rec.amount = await enc(env, amount);
  rec.category = await enc(env, category);
  rec.note = await enc(env, note);
  await env.KV.put(key, JSON.stringify(rec));
  return true;
}
async function listRawExpenses(env, userId, datePrefix) {
  const out = [];
  let cursor;
  do {
    const res = await env.KV.list({ prefix: `exp:${userId}:${datePrefix}`, cursor });
    for (const k of res.keys) {
      const v = await env.KV.get(k.name);
      if (v) out.push({ key: k.name, ...JSON.parse(v) });
    }
    cursor = res.list_complete ? null : res.cursor;
  } while (cursor);
  return out;
}
async function monthTotal(env, userId, ym) {
  const rows = await listRawExpenses(env, userId, ym);
  let total = 0;
  for (const r of rows) total += await decAmount(env, r.amount);
  return { total, n: rows.length };
}

// Итог за месяц с гарантированным учётом одной свежей траты.
// KV.list отдаёт данные с задержкой, поэтому только что добавленную трату
// (или новую сумму отредактированной) подставляем явно по её id.
async function monthTotalWith(env, userId, ym, overrideId, overrideAmount) {
  const rows = await listRawExpenses(env, userId, ym);
  let total = 0;
  let n = 0;
  let seen = false;
  for (const r of rows) {
    if (overrideId && r.id === overrideId) {
      total += overrideAmount;
      seen = true;
    } else {
      total += await decAmount(env, r.amount);
    }
    n += 1;
  }
  if (overrideId && !seen) {
    total += overrideAmount;
    n += 1;
  }
  return { total, n };
}
async function dayTotal(env, userId, ymd) {
  const rows = await listRawExpenses(env, userId, ymd);
  let total = 0;
  for (const r of rows) total += await decAmount(env, r.amount);
  return { total, n: rows.length };
}
async function monthBreakdown(env, userId, ym) {
  const rows = await listRawExpenses(env, userId, ym);
  const agg = {};
  for (const r of rows) {
    const cat = (await dec(env, r.category)) || "Прочее";
    if (!agg[cat]) agg[cat] = { s: 0, n: 0 };
    agg[cat].s += await decAmount(env, r.amount);
    agg[cat].n += 1;
  }
  return Object.entries(agg)
    .map(([category, v]) => ({ category, s: v.s, n: v.n }))
    .sort((a, b) => b.s - a.s);
}
async function dayExpenses(env, userId, ymd) {
  const rows = await listRawExpenses(env, userId, ymd);
  const out = [];
  for (const r of rows) {
    out.push({
      id: r.id,
      ts: r.ts,
      amount: await decAmount(env, r.amount),
      category: await dec(env, r.category),
      note: await dec(env, r.note),
    });
  }
  out.sort((a, b) => (a.ts < b.ts ? -1 : 1));
  return out;
}

// ==== KV: долги ==========================================================
async function addDebt(env, userId, direction, counterparty, amount, note, due) {
  const id = crypto.randomUUID();
  const val = {
    id,
    direction,
    counterparty: await enc(env, counterparty),
    amount: await enc(env, amount),
    note: await enc(env, note),
    due_date: due || null,
    settled: 0,
    created_at: new Date().toISOString(),
  };
  await env.KV.put(`debt:${userId}:${id}`, JSON.stringify(val));
}
async function listDebts(env, userId) {
  const out = [];
  let cursor;
  do {
    const res = await env.KV.list({ prefix: `debt:${userId}:`, cursor });
    for (const k of res.keys) {
      const v = await env.KV.get(k.name);
      if (!v) continue;
      const d = JSON.parse(v);
      out.push({
        id: d.id,
        direction: d.direction,
        counterparty: await dec(env, d.counterparty),
        amount: await decAmount(env, d.amount),
        note: await dec(env, d.note),
        due_date: d.due_date,
        settled: d.settled,
        created_at: d.created_at,
      });
    }
    cursor = res.list_complete ? null : res.cursor;
  } while (cursor);
  out.sort((a, b) => {
    const ad = a.due_date || "9999";
    const bd = b.due_date || "9999";
    return ad < bd ? -1 : ad > bd ? 1 : 0;
  });
  return out;
}
async function settleDebt(env, userId, id) {
  const key = `debt:${userId}:${id}`;
  const v = await env.KV.get(key);
  if (!v) return false;
  const d = JSON.parse(v);
  if (d.settled) return false;
  d.settled = 1;
  await env.KV.put(key, JSON.stringify(d));
  return true;
}

// ==== KV: meta / админ ===================================================
async function getAdminId(env) {
  if (env.ADMIN_ID && /^\d+$/.test(env.ADMIN_ID)) return parseInt(env.ADMIN_ID, 10);
  const m = await env.KV.get("meta:admin_id");
  return m && /^\d+$/.test(m) ? parseInt(m, 10) : null;
}
async function onNewUser(env, from) {
  const adminId = await getAdminId(env);
  if (adminId == null) {
    await env.KV.put("meta:admin_id", String(from.id)); // первый = главный админ
    return;
  }
  if (from.id === adminId) return;
  const un = from.username ? "@" + from.username : "без username";
  await send(
    env,
    adminId,
    `🔔 <b>Новый пользователь подключился к боту</b>\n${esc(from.first_name)} (${esc(un)})\nid <code>${from.id}</code>`
  );
}

// ==== Чистая логика (порт с Python) ======================================
export function toFloat(raw) {
  let s = String(raw).replace(/\s/g, "");
  if (!s) return null;
  const hasC = s.includes(",");
  const hasD = s.includes(".");
  if (hasC && hasD) {
    if (s.lastIndexOf(",") > s.lastIndexOf("."))
      s = s.replace(/\./g, "").replace(/,/g, ".");
    else s = s.replace(/,/g, "");
  } else if (hasC) {
    const p = s.split(",");
    if (p.length === 2 && p[1].length >= 1 && p[1].length <= 2) s = p[0] + "." + p[1];
    else s = s.replace(/,/g, "");
  } else if (hasD) {
    const p = s.split(".");
    if (!(p.length === 2 && p[1].length >= 1 && p[1].length <= 2)) s = s.replace(/\./g, "");
  }
  const f = parseFloat(s);
  return isNaN(f) ? null : f;
}
export function parseAmount(text) {
  if (text == null) return null;
  const m = String(text).match(/^\s*([0-9][0-9\s.,]*)\s*([\s\S]*)$/);
  if (!m) return null;
  const amount = toFloat(m[1]);
  if (amount == null || !isFinite(amount) || amount <= 0 || amount > 1e15) return null;
  return { amount, note: m[2].trim() };
}
export function categoryOf(note) {
  note = (note || "").trim();
  if (!note) return "Прочее";
  const w = note.split(/[\s,;.]+/)[0];
  return w ? w[0].toUpperCase() + w.slice(1) : "Прочее";
}
export function parseHHMM(s) {
  s = (s || "").trim();
  let m = s.match(/^(\d{1,2})[:.\s](\d{2})$/);
  let h, mi;
  if (m) {
    h = +m[1];
    mi = +m[2];
  } else {
    m = s.match(/^(\d{1,2})$/);
    if (!m) return null;
    h = +m[1];
    mi = 0;
  }
  return h >= 0 && h <= 23 && mi >= 0 && mi <= 59 ? [h, mi] : null;
}
export function parseDueDate(s, todayYear) {
  s = (s || "").trim().toLowerCase();
  if (["нет", "-", "", "no", "0", "без", "не"].includes(s)) return null;
  const m = s.match(/^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?$/);
  if (!m) return "invalid";
  let d = +m[1];
  let mo = +m[2];
  let y = m[3] ? +m[3] : todayYear;
  if (y < 100) y += 2000;
  const dt = new Date(Date.UTC(y, mo - 1, d));
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== mo - 1 || dt.getUTCDate() !== d)
    return "invalid";
  return `${y}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}
export function hoursFor(amount, income, workHours) {
  if (income > 0 && workHours > 0) return amount / (income / workHours);
  return null;
}
function group(n) {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
}
export function fmtMoney(x, cur = "₽") {
  const n = Number(x);
  if (!isFinite(n)) return "—";
  let body;
  if (Math.abs(n - Math.round(n)) < 0.005) body = group(Math.round(n));
  else {
    const f = n.toFixed(2).split(".");
    body = group(parseInt(f[0], 10)) + "," + f[1];
  }
  return (body + " " + (cur || "").trim()).trim();
}
export function fmtHours(h) {
  if (h == null) return "—";
  h = Number(h);
  if (!isFinite(h)) return "—";
  const totalMin = Math.round(h * 60);
  if (totalMin <= 0) return "меньше минуты";
  const hh = Math.floor(totalMin / 60);
  const mm = totalMin % 60;
  let base = hh && mm ? `${hh} ч ${mm} мин` : hh ? `${hh} ч` : `${mm} мин`;
  if (totalMin >= 480) base += ` (≈ ${(totalMin / 480).toFixed(1)} раб. дн.)`;
  return base;
}
export function isoToDisplay(iso) {
  if (!iso) return "";
  const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return m ? `${m[3]}.${m[2]}.${m[1]}` : String(iso);
}
const MONTHS = [
  "", "январь", "февраль", "март", "апрель", "май", "июнь",
  "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
];
export function ymDisplay(ym) {
  const m = String(ym).match(/^(\d{4})-(\d{2})$/);
  return m ? `${MONTHS[+m[2]]} ${m[1]}` : String(ym);
}
export function prevYm(ym) {
  let [y, m] = String(ym).split("-").map(Number);
  m -= 1;
  if (m === 0) {
    m = 12;
    y -= 1;
  }
  return `${y}-${String(m).padStart(2, "0")}`;
}
// Локальные дата/время по таймзоне пользователя
function localParts(tz) {
  tz = tz || "UTC";
  // Ручное смещение вида "UTC+4" / "UTC-3" / "UTC+5.5" — без зависимости от Intl
  const off = /^UTC([+-]\d+(?:\.\d+)?)$/.exec(tz);
  if (off) {
    const d = new Date(Date.now() + parseFloat(off[1]) * 3600000);
    const ymd = d.toISOString().slice(0, 10);
    return {
      ymd,
      ym: ymd.slice(0, 7),
      year: d.getUTCFullYear(),
      hour: d.getUTCHours(),
      minute: d.getUTCMinutes(),
    };
  }
  const now = new Date();
  let p = {};
  try {
    const fmt = new Intl.DateTimeFormat("en-CA", {
      timeZone: tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    for (const part of fmt.formatToParts(now)) p[part.type] = part.value;
  } catch (e) {
    const fmt = new Intl.DateTimeFormat("en-CA", {
      timeZone: "UTC",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false,
    });
    for (const part of fmt.formatToParts(now)) p[part.type] = part.value;
  }
  let hour = parseInt(p.hour, 10);
  if (hour === 24) hour = 0;
  return {
    ymd: `${p.year}-${p.month}-${p.day}`,
    ym: `${p.year}-${p.month}`,
    year: parseInt(p.year, 10),
    hour,
    minute: parseInt(p.minute, 10),
  };
}
function reminderReached(parts, h, m) {
  return parts.hour > h || (parts.hour === h && parts.minute >= m);
}

// ==== Тексты =============================================================
function welcomeText(isAdmin) {
  let t =
    "👋 Привет! Я считаю твои траты и перевожу их в <b>часы твоего времени</b>.\n\n" +
    "<b>Как записать трату:</b> просто пришли сумму и что купил —\n" +
    "напр. <code>1500 продукты</code> или <code>350 такси</code>.\n" +
    "Я запишу и скажу, во сколько часов работы это тебе обошлось.\n\n" +
    "Кнопки снизу:\n" +
    `${BTN.ADD} — записать трату\n` +
    `${BTN.CALC} — посчитать часы, ничего не записывая\n` +
    `${BTN.DEBTS} — долги (я должен / мне должны)\n` +
    `${BTN.SUMMARY} — сколько и на что ушло за месяц\n` +
    `${BTN.SETTINGS} — доход, часы, валюта, напоминания, бюджет, экспорт\n\n` +
    "💡 Ещё: бюджет-лимит на месяц, экспорт трат в CSV, эмодзи-графики в сводке.\n\n" +
    "Каждый вечер напомню записать траты, в конце месяца пришлю сводку 📊";
  if (isAdmin)
    t += "\n\n👑 Ты — <b>главный админ</b>. Буду сообщать тебе о новых пользователях.";
  return t;
}

// Экспорт всех трат пользователя в CSV-файл
async function exportCsv(env, u, chatId) {
  const rows = await listRawExpenses(env, u.id, "");
  if (!rows.length) {
    await send(env, chatId, "Пока нечего экспортировать — трат нет.");
    return;
  }
  const recs = [];
  for (const r of rows) {
    const parts = r.key.split(":"); // exp:userid:ymd:id
    recs.push({
      ymd: parts[2],
      amount: await decAmount(env, r.amount),
      category: (await dec(env, r.category)) || "",
      note: (await dec(env, r.note)) || "",
      ts: r.ts || "",
    });
  }
  recs.sort((a, b) => (a.ymd + a.ts < b.ymd + b.ts ? -1 : 1));
  const q = (s) => `"${String(s).replace(/"/g, '""')}"`;
  let csv = "Дата;Категория;Заметка;Сумма\n";
  let total = 0;
  for (const r of recs) {
    csv += `${r.ymd};${q(r.category)};${q(r.note)};${r.amount}\n`;
    total += r.amount;
  }
  const form = new FormData();
  form.append("chat_id", String(chatId));
  form.append("caption", `📤 Экспорт трат: ${recs.length} шт., всего ${fmtMoney(total, u.currency)}`);
  form.append("document", new Blob(["﻿" + csv], { type: "text/csv" }), "traty.csv");
  await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/sendDocument`, { method: "POST", body: form });
}

async function summaryText(env, u, ym) {
  const cur = u.currency;
  const rows = await listRawExpenses(env, u.id, ym); // один list на всю сводку
  const head = `📊 <b>Сводка — ${ymDisplay(ym)}</b>\n\n`;
  if (!rows.length) return head + "Трат в этом месяце ещё нет.";
  let total = 0;
  const agg = {};
  for (const r of rows) {
    const amt = await decAmount(env, r.amount);
    total += amt;
    const cat = (await dec(env, r.category)) || "Прочее";
    if (!agg[cat]) agg[cat] = { s: 0, n: 0 };
    agg[cat].s += amt;
    agg[cat].n += 1;
  }
  const n = rows.length;
  const hours = hoursFor(total, u.monthly_income, u.work_hours);
  const lines = [
    head,
    `Всего: <b>${esc(fmtMoney(total, cur))}</b> · ${n} трат`,
    `⏱ Это <b>${esc(fmtHours(hours))}</b> твоего времени`,
  ];
  if (u.monthly_income > 0)
    lines.push(
      `📈 ${Math.round((total / u.monthly_income) * 100)}% месячного дохода (${esc(
        fmtMoney(u.monthly_income, cur)
      )})`
    );
  const budget = u.monthly_budget || 0;
  if (budget > 0) {
    const used = total / budget;
    lines.push(
      `\n🎯 Бюджет: <b>${esc(fmtMoney(total, cur))}</b> из ${esc(fmtMoney(budget, cur))} (${Math.round(used * 100)}%)`
    );
    lines.push(`<code>${bar(used, 12)}</code>${total > budget ? " 🚨" : ""}`);
  }
  lines.push("\n<b>По категориям:</b>");
  const br = Object.entries(agg)
    .map(([category, v]) => ({ category, s: v.s, n: v.n }))
    .sort((a, b) => b.s - a.s);
  for (const row of br.slice(0, 8)) {
    const share = total ? row.s / total : 0;
    lines.push(
      `<code>${bar(share, 8)}</code> ${esc(row.category)} — ${esc(fmtMoney(row.s, cur))} (${Math.round(share * 100)}%)`
    );
  }
  if (br.length > 8) lines.push(`…и ещё ${br.length - 8} категорий`);
  return lines.join("\n");
}

async function todayText(env, u, ymd, dm, rows) {
  const cur = u.currency;
  if (!rows) rows = await dayExpenses(env, u.id, ymd);
  const head = `📅 <b>Сегодня, ${dm}</b>\n\n`;
  if (!rows.length) return head + "Сегодня трат пока нет. Пришли сумму — запишу.";
  let total = 0;
  for (const r of rows) total += r.amount;
  const lines = [head];
  for (const r of rows.slice(0, 50)) {
    const note = r.note || r.category || "Прочее";
    lines.push(`• ${esc(fmtMoney(r.amount, cur))} — ${esc(note)}`);
  }
  if (rows.length > 50) lines.push(`…и ещё ${rows.length - 50} трат`);
  const hours = hoursFor(total, u.monthly_income, u.work_hours);
  lines.push(`\nИтого: <b>${esc(fmtMoney(total, cur))}</b> · ${rows.length} трат`);
  lines.push(`⏱ ${esc(fmtHours(hours))} твоего времени`);
  return lines.join("\n");
}

// Раздел «Долги»: один list на текст и клавиатуру сразу.
async function debtsView(env, u) {
  const cur = u.currency;
  const all = (await listDebts(env, u.id)).filter((d) => !d.settled);
  const iOwe = all.filter((d) => d.direction === "i_owe");
  const toMe = all.filter((d) => d.direction === "owed_to_me");
  let text;
  if (!iOwe.length && !toMe.length) {
    text = "📌 <b>Долги</b>\n\nАктивных долгов нет 🎉";
  } else {
    const lines = ["📌 <b>Долги</b>"];
    const block = (title, items) => {
      const s = items.reduce((a, d) => a + d.amount, 0);
      lines.push(`\n<b>${title}</b> — итого ${esc(fmtMoney(s, cur))}:`);
      for (const d of items.slice(0, 20)) {
        const due = isoToDisplay(d.due_date);
        lines.push(
          `• ${esc(d.counterparty || "—")} — ${esc(fmtMoney(d.amount, cur))}${due ? ", до " + due : ""}`
        );
      }
      if (items.length > 20) lines.push(`…и ещё ${items.length - 20}`);
    };
    if (iOwe.length) block("🔴 Я должен", iOwe);
    if (toMe.length) block("🟢 Мне должны", toMe);
    text = lines.join("\n");
  }
  const rows = [
    [
      { text: "➕ Я должен", callback_data: "d:add:i_owe" },
      { text: "➕ Мне должны", callback_data: "d:add:owed_to_me" },
    ],
  ];
  for (const d of all.slice(0, 20)) {
    const mark = d.direction === "i_owe" ? "🔴" : "🟢";
    const label = `✅ Закрыть ${mark} ${d.counterparty || "—"} ${Math.round(d.amount)}`;
    rows.push([{ text: label.slice(0, 64), callback_data: `d:settle:${d.id}` }]);
  }
  return { text, kb: { inline_keyboard: rows } };
}

function summaryKb(ym) {
  const prev = prevYm(ym);
  return {
    inline_keyboard: [
      [
        { text: `◀ ${ymDisplay(prev)}`, callback_data: `s:m:${prev}` },
        { text: "📅 Сегодня", callback_data: "s:today" },
      ],
    ],
  };
}

function settingsText() {
  return (
    "⚙️ <b>Настройки</b>\n\n" +
    "Здесь задаётся, как считать «часы твоего времени», и когда напоминать.\n" +
    "Ставка за час = доход ÷ рабочие часы в месяц."
  );
}
async function settingsKb(env, u) {
  const cur = u.currency;
  const daily = u.daily_reminder ? "🔔 Вкл" : "🔕 Выкл";
  const rem = `${String(u.reminder_hour).padStart(2, "0")}:${String(u.reminder_min).padStart(2, "0")}`;
  const rows = [
    [{ text: `💰 Доход в месяц: ${fmtMoney(u.monthly_income, cur)}`, callback_data: "set:income" }],
    [{ text: `🕐 Рабочих часов/мес: ${Math.round(u.work_hours)}`, callback_data: "set:hours" }],
    [{ text: `💱 Валюта: ${cur}`, callback_data: "set:currency" }],
    [{ text: `⏰ Напоминание: ${rem}`, callback_data: "set:reminder" }],
    [{ text: `🌍 Часовой пояс: ${tzLabel(u.tz)}`, callback_data: "set:tz" }],
    [{ text: `Ежедневное напоминание: ${daily}`, callback_data: "set:daily" }],
    [
      {
        text: `🎯 Бюджет/мес: ${(u.monthly_budget || 0) > 0 ? fmtMoney(u.monthly_budget, cur) : "не задан"}`,
        callback_data: "set:budget",
      },
    ],
    [{ text: "📤 Экспорт трат (CSV)", callback_data: "set:export" }],
  ];
  if ((await getAdminId(env)) === u.id) {
    const cnt = (await allUsers(env)).length;
    rows.push([{ text: `👥 Пользователи: ${cnt}`, callback_data: "set:admin_users" }]);
  }
  return { inline_keyboard: rows };
}

const undoKb = (ymd, id) => ({
  inline_keyboard: [[
    { text: "✏️ Изменить", callback_data: `x:edit:${ymd}:${id}` },
    { text: "↩️ Удалить", callback_data: `x:del:${ymd}:${id}` },
  ]],
});
// Клавиатура списка «Сегодня»: у каждой траты — кнопка редактирования
function todayKb(ymd, rows, cur) {
  const kb = rows.slice(0, 10).map((r) => [
    {
      text: `✏️ ${fmtMoney(r.amount, cur)} · ${r.category || "Прочее"}`.slice(0, 60),
      callback_data: `x:edit:${ymd}:${r.id}`,
    },
  ]);
  kb.push([{ text: "◀ К сводке за месяц", callback_data: `s:m:${ymd.slice(0, 7)}` }]);
  return { inline_keyboard: kb };
}

// ==== Сохранение траты ===================================================
async function doSaveExpense(env, u, amount, note) {
  const parts = localParts(u.tz);
  const cat = categoryOf(note);
  const id = await addExpense(env, u.id, amount, cat, note, parts.ymd);
  const hours = hoursFor(amount, u.monthly_income, u.work_hours);
  let text =
    `✅ Записал: <b>${esc(fmtMoney(amount, u.currency))}</b> — ${esc(cat)}\n` +
    `⏱ Это ≈ <b>${esc(fmtHours(hours))}</b> твоего времени`;
  try {
    const { total, n } = await monthTotalWith(env, u.id, parts.ym, id, amount);
    text += `\n💰 За ${ymDisplay(parts.ym)}: ${esc(fmtMoney(total, u.currency))} (${n} трат)`;
    text += budgetLine(u, total);
  } catch (e) {
    // трата уже записана; месячный итог временно недоступен (напр. лимит KV)
    text += "\n💾 Записано. Месячный итог посчитаю позже.";
  }
  return { text, kb: undoKb(parts.ymd, id) };
}

// Строка про бюджет для подтверждений (пусто, если лимит не задан)
function budgetLine(u, total) {
  const budget = u.monthly_budget || 0;
  if (budget <= 0) return "";
  const left = budget - total;
  return left < 0
    ? `\n🚨 Бюджет ${esc(fmtMoney(budget, u.currency))} превышен на ${esc(fmtMoney(-left, u.currency))}`
    : `\n🎯 До лимита осталось ${esc(fmtMoney(left, u.currency))} из ${esc(fmtMoney(budget, u.currency))}`;
}

// Есть описание → сохраняем сразу; нет → предлагаем категорию кнопками
async function handleAmount(env, u, chatId, p) {
  if (p.note) {
    const r = await doSaveExpense(env, u, p.amount, p.note);
    await send(env, chatId, r.text, r.kb);
  } else {
    u.state = "expense_category";
    u.state_data = { amount: p.amount };
    await saveUser(env, u);
    await send(
      env,
      chatId,
      `Сумма <b>${esc(fmtMoney(p.amount, u.currency))}</b>. Выбери категорию 👇\n(или просто напиши свою)`,
      categoryKb()
    );
  }
}

// ==== Обработка сообщений ================================================
async function onMessage(env, msg) {
  const from = msg.from;
  if (!from || from.is_bot) return;
  const chatId = msg.chat.id;
  const { user, isNew } = await ensureUser(env, from);
  if (isNew) await onNewUser(env, from);
  const text = msg.text || "";

  // Команды
  if (/^\/start\b/.test(text)) {
    user.state = null;
    user.state_data = null;
    await saveUser(env, user);
    await send(env, chatId, welcomeText((await getAdminId(env)) === user.id), mainKb());
    return;
  }
  if (/^\/(menu|help)\b/.test(text)) {
    user.state = null;
    user.state_data = null;
    await saveUser(env, user);
    await send(
      env,
      chatId,
      /help/.test(text) ? welcomeText((await getAdminId(env)) === user.id) : "Меню 👇",
      mainKb()
    );
    return;
  }
  if (/^\/cancel\b/.test(text)) {
    user.state = null;
    user.state_data = null;
    await saveUser(env, user);
    await send(env, chatId, "Ок, отменил.", mainKb());
    return;
  }

  // Кнопки главного меню — работают в любом состоянии, сбрасывают его
  if (MAIN_BUTTONS.has(text)) {
    user.state = null;
    user.state_data = null;
    if (text === BTN.ADD) {
      user.state = "add_expense";
      await saveUser(env, user);
      await send(env, chatId, "Пришли сумму и что купил, напр.: <code>1500 продукты</code>\n(/cancel — отмена)");
    } else if (text === BTN.CALC) {
      user.state = "calc";
      await saveUser(env, user);
      await send(
        env,
        chatId,
        "⏱ Пришли сумму — посчитаю, сколько это <b>часов твоего времени</b>.\nТрату записывать не буду.\n(/cancel — отмена)"
      );
    } else if (text === BTN.DEBTS) {
      await saveUser(env, user);
      { const dv = await debtsView(env, user); await send(env, chatId, dv.text, dv.kb); }
    } else if (text === BTN.SUMMARY) {
      await saveUser(env, user);
      const ym = localParts(user.tz).ym;
      await send(env, chatId, await summaryText(env, user, ym), summaryKb(ym));
    } else if (text === BTN.SETTINGS) {
      await saveUser(env, user);
      await send(env, chatId, settingsText(), await settingsKb(env, user));
    }
    return;
  }

  // Состояния (FSM)
  const st = user.state;
  if (st === "add_expense") {
    const p = parseAmount(text);
    if (!p) {
      await send(env, chatId, "Не понял сумму 🤔 Пришли число, напр. <code>500 такси</code>");
      return;
    }
    user.state = null;
    await saveUser(env, user);
    await handleAmount(env, user, chatId, p);
    return;
  }
  if (st === "calc") {
    const p = parseAmount(text);
    if (!p) {
      await send(env, chatId, "Пришли число, напр. <code>5000</code>");
      return;
    }
    user.state = null;
    await saveUser(env, user);
    const hours = hoursFor(p.amount, user.monthly_income, user.work_hours);
    let extra = "";
    if (user.monthly_income > 0)
      extra = `\n📈 ${((p.amount / user.monthly_income) * 100).toFixed(1)}% месячного дохода`;
    await send(
      env,
      chatId,
      `💸 ${esc(fmtMoney(p.amount, user.currency))}\n⏱ Это <b>${esc(fmtHours(hours))}</b> твоего времени${extra}`,
      mainKb()
    );
    return;
  }
  if (st === "expense_category") {
    const sd = user.state_data || {};
    user.state = null;
    user.state_data = null;
    await saveUser(env, user);
    if (!sd.amount) {
      const p2 = parseAmount(text);
      if (p2) {
        await handleAmount(env, user, chatId, p2);
        return;
      }
      await send(env, chatId, "Пришли сумму, напр. <code>500 такси</code>", mainKb());
      return;
    }
    const r = await doSaveExpense(env, user, sd.amount, text.trim());
    await send(env, chatId, r.text, r.kb);
    return;
  }
  if (st === "edit_expense") {
    const p = parseAmount(text);
    if (!p) {
      await send(env, chatId, "Пришли число, напр. <code>700 такси</code>");
      return;
    }
    const sd = user.state_data || {};
    user.state = null;
    user.state_data = null;
    await saveUser(env, user);
    if (!sd.ymd || !sd.id) {
      await send(env, chatId, "Не понял, какую трату менять. Открой её заново.", mainKb());
      return;
    }
    let category, note;
    if (p.note) {
      category = categoryOf(p.note);
      note = p.note;
    } else {
      const exp = await getExpense(env, user.id, sd.ymd, sd.id);
      category = exp ? (await dec(env, exp.category)) || "Прочее" : "Прочее";
      note = exp ? (await dec(env, exp.note)) || "" : "";
    }
    const ok = await updateExpense(env, user.id, sd.ymd, sd.id, p.amount, category, note);
    if (!ok) {
      await send(env, chatId, "Трата не найдена — возможно, удалена.", mainKb());
      return;
    }
    const ym = sd.ymd.slice(0, 7);
    const { total } = await monthTotalWith(env, user.id, ym, sd.id, p.amount);
    let text2 =
      `✏️ Изменил: <b>${esc(fmtMoney(p.amount, user.currency))}</b> — ${esc(category)}\n` +
      `💰 За ${ymDisplay(ym)}: ${esc(fmtMoney(total, user.currency))}`;
    text2 += budgetLine(user, total);
    await send(env, chatId, text2, undoKb(sd.ymd, sd.id));
    return;
  }
  if (st === "set_tz_offset") {
    const tz = parseOffset(text);
    if (!tz) {
      await send(env, chatId, "Не понял. Пришли смещение от UTC, напр. <code>+4</code> или <code>-3</code>.");
      return;
    }
    user.state = null;
    user.tz = tz;
    await saveUser(env, user);
    await send(env, chatId, `🌍 Часовой пояс: <b>${esc(tzLabel(tz))}</b>.`, mainKb());
    await send(env, chatId, settingsText(), await settingsKb(env, user));
    return;
  }
  if (st === "set_budget") {
    const t = (text || "").trim();
    let val;
    if (/^0([.,]0*)?$/.test(t)) val = 0;
    else {
      const p = parseAmount(t);
      if (!p) {
        await send(env, chatId, "Пришли число, напр. <code>80000</code> (или <code>0</code>, чтобы снять лимит).");
        return;
      }
      val = p.amount;
    }
    user.state = null;
    user.monthly_budget = val;
    await saveUser(env, user);
    await send(
      env,
      chatId,
      val > 0 ? `🎯 Бюджет на месяц: <b>${esc(fmtMoney(val, user.currency))}</b>.` : "🎯 Лимит бюджета снят.",
      mainKb()
    );
    await send(env, chatId, settingsText(), await settingsKb(env, user));
    return;
  }
  if (st === "debt_counterparty") {
    user.state = "debt_amount";
    user.state_data = { ...(user.state_data || {}), counterparty: text.trim().slice(0, 64) };
    await saveUser(env, user);
    await send(env, chatId, "Сколько? Пришли сумму, напр. <code>5000</code>");
    return;
  }
  if (st === "debt_amount") {
    const p = parseAmount(text);
    if (!p) {
      await send(env, chatId, "Не понял сумму. Пришли число, напр. <code>5000</code>");
      return;
    }
    user.state = "debt_due";
    user.state_data = { ...(user.state_data || {}), amount: p.amount };
    await saveUser(env, user);
    await send(
      env,
      chatId,
      "Когда вернуть? Дата в формате <code>ДД.ММ</code> или <code>ДД.ММ.ГГГГ</code>.\nЕсли срока нет — напиши <code>нет</code>."
    );
    return;
  }
  if (st === "debt_due") {
    const year = localParts(user.tz).year;
    const due = parseDueDate(text, year);
    if (due === "invalid") {
      await send(env, chatId, "Не понял дату. Напиши <code>15.08</code> или <code>нет</code>.");
      return;
    }
    const data = user.state_data || {};
    user.state = null;
    user.state_data = null;
    await saveUser(env, user);
    await addDebt(env, user.id, data.direction, data.counterparty || "—", data.amount, "", due);
    const who = data.direction === "i_owe" ? "🔴 Я должен" : "🟢 Мне должны";
    const dueS = due ? ", до " + isoToDisplay(due) : "";
    await send(
      env,
      chatId,
      `Записал долг: ${who} — ${esc(data.counterparty || "—")} ${esc(fmtMoney(data.amount, user.currency))}${dueS} ✅`
    );
    { const dv = await debtsView(env, user); await send(env, chatId, dv.text, dv.kb); }
    return;
  }
  if (st === "set_income") {
    const p = parseAmount(text);
    if (!p) {
      await send(env, chatId, "Пришли число, напр. <code>100000</code>");
      return;
    }
    user.state = null;
    user.monthly_income = p.amount;
    await saveUser(env, user);
    await send(env, chatId, `💰 Доход обновлён: <b>${esc(fmtMoney(p.amount, user.currency))}</b> в месяц.`, mainKb());
    await send(env, chatId, settingsText(), await settingsKb(env, user));
    return;
  }
  if (st === "set_hours") {
    const p = parseAmount(text);
    if (!p || p.amount <= 0) {
      await send(env, chatId, "Пришли число часов, напр. <code>160</code>");
      return;
    }
    user.state = null;
    user.work_hours = p.amount;
    await saveUser(env, user);
    await send(env, chatId, `🕐 Рабочих часов в месяц: <b>${Math.round(p.amount)}</b>.`, mainKb());
    await send(env, chatId, settingsText(), await settingsKb(env, user));
    return;
  }
  if (st === "set_currency") {
    const cur = (text || "").trim().slice(0, 4);
    if (!cur) {
      await send(env, chatId, "Пришли символ валюты, напр. <code>₽</code>, <code>$</code>, <code>R$</code>");
      return;
    }
    user.state = null;
    user.currency = cur;
    await saveUser(env, user);
    await send(env, chatId, `💱 Валюта: <b>${esc(cur)}</b>.`, mainKb());
    await send(env, chatId, settingsText(), await settingsKb(env, user));
    return;
  }
  if (st === "set_reminder") {
    const hm = parseHHMM(text);
    if (!hm) {
      await send(env, chatId, "Формат <code>ЧЧ:ММ</code>, напр. <code>21:00</code>");
      return;
    }
    user.state = null;
    user.reminder_hour = hm[0];
    user.reminder_min = hm[1];
    await saveUser(env, user);
    await send(env, chatId, `⏰ Буду напоминать в <b>${String(hm[0]).padStart(2, "0")}:${String(hm[1]).padStart(2, "0")}</b>.`, mainKb());
    await send(env, chatId, settingsText(), await settingsKb(env, user));
    return;
  }

  // По умолчанию — записать трату
  const p = parseAmount(text);
  if (!p) {
    await send(
      env,
      chatId,
      "Не понял 🤔 Пришли сумму цифрами, напр. <code>500 такси</code>, или выбери кнопку ниже.",
      mainKb()
    );
    return;
  }
  await saveAndReply(env, user, chatId, p.amount, p.note);
}

// ==== Обработка callback-кнопок =========================================
async function onCallback(env, cq) {
  const from = cq.from;
  const { user, isNew } = await ensureUser(env, from);
  if (isNew) await onNewUser(env, from);
  const data = cq.data || "";

  if (data.startsWith("cat:")) {
    const i = parseInt(data.slice(4), 10);
    const sd = user.state_data || {};
    if (!sd.amount || !QUICK_CATS[i]) {
      await answerCb(env, cq.id, "Устарело — пришли сумму заново");
      return;
    }
    user.state = null;
    user.state_data = null;
    await saveUser(env, user);
    const r = await doSaveExpense(env, user, sd.amount, QUICK_CATS[i][1]);
    await safeEdit(env, cq, r.text, r.kb);
    await answerCb(env, cq.id, "Записал ✅");
    return;
  }
  if (data.startsWith("d:add:")) {
    const direction = data.slice("d:add:".length);
    user.state = "debt_counterparty";
    user.state_data = { direction };
    await saveUser(env, user);
    const ask = direction === "i_owe" ? "Кому ты должен? Напиши имя." : "Кто тебе должен? Напиши имя.";
    await send(env, from.id, ask + "\n(/cancel — отмена)");
    await answerCb(env, cq.id);
    return;
  }
  if (data.startsWith("d:settle:")) {
    const id = data.slice("d:settle:".length);
    const done = await settleDebt(env, from.id, id);
    await answerCb(env, cq.id, done ? "Закрыто ✅" : "Не найдено");
    { const dv = await debtsView(env, user); await safeEdit(env, cq, dv.text, dv.kb); }
    return;
  }
  if (data.startsWith("s:m:")) {
    const ym = data.slice("s:m:".length);
    await safeEdit(env, cq, await summaryText(env, user, ym), summaryKb(ym));
    await answerCb(env, cq.id);
    return;
  }
  if (data === "s:today") {
    const parts = localParts(user.tz);
    const dm = parts.ymd.slice(8) + "." + parts.ymd.slice(5, 7);
    const rows = await dayExpenses(env, user.id, parts.ymd);
    await safeEdit(
      env,
      cq,
      await todayText(env, user, parts.ymd, dm, rows),
      todayKb(parts.ymd, rows, user.currency)
    );
    await answerCb(env, cq.id);
    return;
  }
  if (data === "set:income" || data === "set:hours" || data === "set:currency" || data === "set:reminder") {
    const map = {
      "set:income": ["set_income", "Введи новый <b>доход в месяц</b> (число), напр. <code>100000</code>\n(/cancel — отмена)"],
      "set:hours": ["set_hours", "Сколько <b>рабочих часов в месяц</b>? Напр. <code>160</code> (8 ч × 20 дней).\n(/cancel — отмена)"],
      "set:currency": ["set_currency", "Пришли символ валюты: <code>₽</code>, <code>$</code>, <code>R$</code>…\n(/cancel — отмена)"],
      "set:reminder": ["set_reminder", "Во сколько напоминать вечером? Формат <code>ЧЧ:ММ</code>, напр. <code>21:00</code>\n(/cancel — отмена)"],
    };
    user.state = map[data][0];
    user.state_data = null;
    await saveUser(env, user);
    await send(env, from.id, map[data][1]);
    await answerCb(env, cq.id);
    return;
  }
  if (data === "set:tz") {
    await safeEdit(
      env,
      cq,
      "🌍 <b>Выбери часовой пояс</b>\n\nПо нему считаются день/месяц и время напоминаний.",
      tzKb()
    );
    await answerCb(env, cq.id);
    return;
  }
  if (data === "tz:custom") {
    user.state = "set_tz_offset";
    user.state_data = null;
    await saveUser(env, user);
    await send(
      env,
      from.id,
      "Пришли смещение от UTC, напр. <code>+4</code> (Самара), <code>+3</code> (Москва), <code>-3</code> (Рио).\n(/cancel — отмена)"
    );
    await answerCb(env, cq.id);
    return;
  }
  if (data.startsWith("tz:")) {
    const i = parseInt(data.slice(3), 10);
    if (!TZ_LIST[i]) {
      await answerCb(env, cq.id, "Не найдено");
      return;
    }
    user.tz = TZ_LIST[i][1];
    await saveUser(env, user);
    await answerCb(env, cq.id, "Часовой пояс обновлён ✅");
    await safeEdit(env, cq, settingsText(), await settingsKb(env, user));
    return;
  }
  if (data === "set:budget") {
    user.state = "set_budget";
    user.state_data = null;
    await saveUser(env, user);
    await send(env, from.id, "Введи <b>бюджет на месяц</b> (число). <code>0</code> — снять лимит.\n(/cancel — отмена)");
    await answerCb(env, cq.id);
    return;
  }
  if (data === "set:export") {
    await answerCb(env, cq.id, "Готовлю файл…");
    await exportCsv(env, user, from.id);
    return;
  }
  if (data === "set:daily") {
    user.daily_reminder = user.daily_reminder ? 0 : 1;
    await saveUser(env, user);
    await answerCb(env, cq.id, user.daily_reminder ? "🔔 Вкл" : "🔕 Выкл");
    await safeEdit(env, cq, settingsText(), await settingsKb(env, user));
    return;
  }
  if (data === "set:admin_users") {
    if ((await getAdminId(env)) !== from.id) {
      await answerCb(env, cq.id, "Только для админа", true);
      return;
    }
    const users = await allUsers(env);
    const lines = [`👥 <b>Пользователи (${users.length})</b>\n`];
    for (const us of users.slice(0, 50)) {
      const un = us.username ? "@" + us.username : "—";
      lines.push(`• ${esc(us.first_name || "—")} (${esc(un)}) · id <code>${us.id}</code>`);
    }
    if (users.length > 50) lines.push(`…и ещё ${users.length - 50}`);
    lines.push("\n<i>Траты и долги других пользователей никому не видны — только их количество.</i>");
    await safeEdit(env, cq, lines.join("\n"), {
      inline_keyboard: [[{ text: "◀ Назад", callback_data: "set:back" }]],
    });
    await answerCb(env, cq.id);
    return;
  }
  if (data === "set:back") {
    await safeEdit(env, cq, settingsText(), await settingsKb(env, user));
    await answerCb(env, cq.id);
    return;
  }
  if (data.startsWith("x:edit:")) {
    const rest = data.slice("x:edit:".length);
    const idx = rest.indexOf(":");
    const ymd = rest.slice(0, idx);
    const id = rest.slice(idx + 1);
    const exp = await getExpense(env, from.id, ymd, id);
    if (!exp) {
      await answerCb(env, cq.id, "Не найдено (возможно, удалено)");
      return;
    }
    const curAmount = await decAmount(env, exp.amount);
    const curCat = (await dec(env, exp.category)) || "Прочее";
    user.state = "edit_expense";
    user.state_data = { ymd, id };
    await saveUser(env, user);
    await send(
      env,
      from.id,
      `✏️ Редактирую: <b>${esc(fmtMoney(curAmount, user.currency))}</b> — ${esc(curCat)}\n\n` +
        "Пришли новое значение — сумму и описание, напр. <code>700 такси</code>.\n" +
        "Если пришлёшь только сумму — категория останется прежней.\n(/cancel — отмена)"
    );
    await answerCb(env, cq.id);
    return;
  }
  if (data.startsWith("x:del:")) {
    const rest = data.slice("x:del:".length);
    const idx = rest.indexOf(":");
    const ymd = rest.slice(0, idx);
    const id = rest.slice(idx + 1);
    const done = await deleteExpense(env, from.id, ymd, id);
    await answerCb(env, cq.id, done ? "Удалено 🗑" : "Уже удалено");
    if (done) await safeEdit(env, cq, "🗑 Трата удалена.", null);
    return;
  }
  await answerCb(env, cq.id);
}

// ==== Cron: вечернее напоминание, долги, месячная сводка =================
async function sendEvening(env, u, parts) {
  const cur = u.currency;
  const chunks = [];
  if (u.daily_reminder) {
    const { total, n } = await dayTotal(env, u.id, parts.ymd);
    if (n)
      chunks.push(
        `🌙 Вечерняя сверка. Сегодня уже записано: <b>${esc(fmtMoney(total, cur))}</b> (${n} трат).\nНичего не забыл добавить?`
      );
    else chunks.push("🌙 Не забудь записать траты за сегодня — просто пришли сумму.");
  }
  const due = (await listDebts(env, u.id)).filter(
    (d) => !d.settled && d.due_date && d.due_date <= parts.ymd
  );
  if (due.length) {
    const lines = ["⏰ <b>Напоминание о долгах</b> (срок наступил):"];
    for (const d of due) {
      const mark = d.direction === "i_owe" ? "🔴 ты должен" : "🟢 тебе должны";
      lines.push(`• ${mark}: ${esc(d.counterparty || "—")} — ${esc(fmtMoney(d.amount, cur))} (до ${isoToDisplay(d.due_date)})`);
    }
    chunks.push(lines.join("\n"));
  }
  if (chunks.length) {
    const res = await send(env, u.id, chunks.join("\n\n"), mainKb());
    if (res && res.ok === false && res.error_code === 403) {
      u.active = 0;
      await saveUser(env, u);
    }
  }
}

async function tick(env) {
  const users = await allUsers(env, true);
  for (const u of users) {
    try {
      const parts = localParts(u.tz);
      if (!reminderReached(parts, u.reminder_hour, u.reminder_min)) continue;
      const prev = prevYm(parts.ym);
      // Вечернее напоминание — раз в день (guard ставим ДО отправки)
      if (u.last_evening_date !== parts.ymd) {
        u.last_evening_date = parts.ymd;
        await saveUser(env, u);
        await sendEvening(env, u, parts);
      }
      // Сводка за прошлый месяц — при первом вечере нового месяца
      if (u.last_month_summary !== prev) {
        u.last_month_summary = prev;
        await saveUser(env, u);
        const { n } = await monthTotal(env, u.id, prev);
        if (n > 0) {
          const res = await send(
            env,
            u.id,
            `🗓 <b>Сводка за ${ymDisplay(prev)} — месяц закрылся.</b>\n\n` + (await summaryText(env, u, prev)),
            mainKb()
          );
          if (res && res.ok === false && res.error_code === 403) {
            u.active = 0;
            await saveUser(env, u);
          }
        }
      }
    } catch (e) {
      // одного пользователя пропускаем, остальных обрабатываем
    }
  }
}

// ==== Точки входа воркера ================================================
export default {
  async fetch(request, env, ctx) {
    if (request.method === "GET") {
      return new Response("Traty Bot жив 🟢", { status: 200 });
    }
    if (request.method === "POST") {
      // Проверка секретного заголовка вебхука
      const secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
      if (env.WEBHOOK_SECRET && secret !== env.WEBHOOK_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
      let update;
      try {
        update = await request.json();
      } catch (e) {
        return new Response("bad json", { status: 400 });
      }
      ctx.waitUntil(
        (async () => {
          try {
            if (update.message) await onMessage(env, update.message);
            else if (update.callback_query) await onCallback(env, update.callback_query);
          } catch (e) {
            // Не роняем вебхук (иначе Telegram будет ретраить), но даём фидбек.
            const chatId =
              (update.message && update.message.chat && update.message.chat.id) ||
              (update.callback_query && update.callback_query.from && update.callback_query.from.id);
            if (chatId) {
              try {
                await send(env, chatId, "⚠️ Временная ошибка (возможно, дневной лимит хранилища). Попробуй ещё раз чуть позже — обычно восстанавливается само.");
              } catch (e2) {}
            }
          }
        })()
      );
      return new Response("ok", { status: 200 });
    }
    return new Response("method not allowed", { status: 405 });
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(tick(env));
  },
};
