// Тест шифрования воркера (AES-GCM через Web Crypto) в node.
import { webcrypto } from "node:crypto";
globalThis.crypto = webcrypto;
globalThis.btoa = (s) => Buffer.from(s, "binary").toString("base64");
globalThis.atob = (s) => Buffer.from(s, "base64").toString("binary");

const { enc, dec } = await import("./worker.js");

let ok = 0, fail = 0;
const check = (n, c) => (c ? ok++ : (fail++, console.log("  FAIL:", n)));

const key = Buffer.from(webcrypto.getRandomValues(new Uint8Array(32))).toString("base64");
const env = { DB_KEY: key };

const plain = "такси в аэропорт 12345";
const tok = await enc(env, plain);
check("шифртекст != открытый текст", tok !== plain);
check("в байтах нет '12345'", !atob(tok).includes("12345"));
check("round-trip", (await dec(env, tok)) === plain);
check("сумма round-trip", (await dec(env, await enc(env, 12345))) === "12345");

// Порча токена → dec не падает, возвращает как есть (аутентификация сработала)
const tampered = tok.slice(0, -6) + "AAAAAA";
const r = await dec(env, tampered);
check("порча не роняет dec", r === tampered);

console.log(`\nИТОГО: ${ok} ok, ${fail} fail`);
process.exit(fail ? 1 : 0);
