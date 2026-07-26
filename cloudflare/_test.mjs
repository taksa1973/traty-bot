// Тест чистой логики воркера (без Cloudflare-окружения).
import {
  parseAmount, toFloat, categoryOf, parseHHMM, parseDueDate,
  hoursFor, fmtMoney, fmtHours, isoToDisplay, ymDisplay, prevYm,
} from "./worker.js";

let ok = 0, fail = 0;
const check = (name, cond) => {
  if (cond) ok++;
  else { fail++; console.log("  FAIL:", name); }
};
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// parseAmount
check("500 такси", eq(parseAmount("500 такси"), { amount: 500, note: "такси" }));
check("1 500 продукты", eq(parseAmount("1 500 продукты"), { amount: 1500, note: "продукты" }));
check("2 000.50 кафе", eq(parseAmount("2 000.50 кафе"), { amount: 2000.5, note: "кафе" }));
check("12,50 кофе", eq(parseAmount("12,50 кофе"), { amount: 12.5, note: "кофе" }));
check("350 только", eq(parseAmount("350"), { amount: 350, note: "" }));
check("1.500 тысячи", eq(parseAmount("1.500 такси"), { amount: 1500, note: "такси" }));
check("не число", parseAmount("привет") === null);
check("ноль", parseAmount("0 такси") === null);
check("минус", parseAmount("-5") === null);
check("огромное → null", parseAmount("9".repeat(400)) === null);
check("сверх лимита", parseAmount("2000000000000000") === null);
check("ровно лимит", parseAmount("1000000000000000").amount === 1e15);

// категория
check("категория", categoryOf("такси домой") === "Такси");
check("категория пусто", categoryOf("") === "Прочее");

// часы
check("часы 500", Math.abs(hoursFor(500, 100000, 160) - 0.8) < 1e-9);
check("часы доход 0", hoursFor(500, 0, 160) === null);

// деньги
check("деньги целое", fmtMoney(12500, "₽") === "12 500 ₽");
check("деньги дробное", fmtMoney(12500.5, "₽") === "12 500,50 ₽");
check("деньги inf", fmtMoney(Infinity) === "—");
check("деньги nan", fmtMoney(NaN) === "—");

// время
check("часы 0.8", fmtHours(0.8) === "48 мин");
check("часы >8", fmtHours(10).includes("раб. дн."));
check("часы порог 7.999", fmtHours(7.999).includes("раб. дн."));
check("часы порог 8.0", fmtHours(8.0).includes("раб. дн."));
check("часы до порога", !fmtHours(7.5).includes("раб. дн."));
check("часы inf", fmtHours(Infinity) === "—");

// hhmm / due / даты
check("hhmm 21:00", eq(parseHHMM("21:00"), [21, 0]));
check("hhmm 9", eq(parseHHMM("9"), [9, 0]));
check("hhmm мусор", parseHHMM("abc") === null);
check("hhmm 25:00", parseHHMM("25:00") === null);
check("due нет", parseDueDate("нет", 2026) === null);
check("due 15.08", parseDueDate("15.08", 2026) === "2026-08-15");
check("due 15.08.2027", parseDueDate("15.08.2027", 2026) === "2027-08-15");
check("due невалид", parseDueDate("99.99", 2026) === "invalid");
check("iso→display", isoToDisplay("2026-08-15") === "15.08.2026");
check("ym display", ymDisplay("2026-07") === "июль 2026");
check("prevYm год", prevYm("2026-01") === "2025-12");
check("toFloat 1.234,56", Math.abs(toFloat("1.234,56") - 1234.56) < 1e-9);

console.log(`\nИТОГО: ${ok} ok, ${fail} fail`);
process.exit(fail ? 1 : 0);
