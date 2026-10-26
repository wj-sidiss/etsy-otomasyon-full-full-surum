// Money / number / date helpers, locale-aware (tr-TR or en-US from the UI language).
//
// Times: every date helper accepts epoch seconds (number), epoch milliseconds (number
// > 1e12), an ISO string or a Date. null/undefined/invalid -> "–".

import { getLang, locale, t } from "./i18n.js";

const DASH = "–";
const cache = new Map();

function nf(loc, opts) {
  const key = loc + JSON.stringify(opts);
  let f = cache.get(key);
  if (!f) {
    f = new Intl.NumberFormat(loc, opts);
    cache.set(key, f);
  }
  return f;
}

function isNum(n) {
  return typeof n === "number" && Number.isFinite(n);
}

function toNumber(v) {
  if (isNum(v)) return v;
  if (typeof v === "string" && v.trim() !== "" && Number.isFinite(Number(v))) return Number(v);
  return null;
}

/** Symbol-first currencies keep the English layout ("$24.90") in both UI languages, as in the video. */
const CURRENCY_LOCALE = {
  USD: "en-US",
  CAD: "en-CA",
  AUD: "en-AU",
  NZD: "en-NZ",
  GBP: "en-GB",
  HKD: "en-HK",
  SGD: "en-SG",
};

/**
 * money(amount, currency="USD", {digits}) -> "$24.90" / "249.469 ₺" / "24,90 €".
 * `amount` may also be an Etsy money object {amount, divisor, currency_code}.
 * TRY is written the Turkish way with the sign after the number.
 */
export function money(amount, currency, opts = {}) {
  let value = amount;
  let cur = currency;
  if (amount && typeof amount === "object" && "amount" in amount) {
    const div = Number(amount.divisor) || 1;
    value = Number(amount.amount) / div;
    cur = cur || amount.currency_code;
  }
  value = toNumber(value);
  if (value === null) return DASH;
  cur = (cur || "USD").toUpperCase();
  const digits = opts.digits === undefined ? 2 : opts.digits;
  if (cur === "TRY") {
    return `${nf("tr-TR", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value)} ₺`;
  }
  const loc = CURRENCY_LOCALE[cur] || locale();
  try {
    return nf(loc, {
      style: "currency",
      currency: cur,
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(value);
  } catch {
    return `${number(value, digits)} ${cur}`;
  }
}

/** number(1234.5) -> "1.234,5" (tr) / "1,234.5" (en). digits = max fraction digits (default 0..2). */
export function number(n, digits) {
  const v = toNumber(n);
  if (v === null) return DASH;
  const opts =
    digits === undefined
      ? { maximumFractionDigits: 2 }
      : { minimumFractionDigits: digits, maximumFractionDigits: digits };
  return nf(locale(), opts).format(v);
}

/** Compact: 12.5K / 1,2 Mn. */
export function compact(n) {
  const v = toNumber(n);
  if (v === null) return DASH;
  return nf(locale(), { notation: "compact", maximumFractionDigits: 1 }).format(v);
}

/**
 * percent(x, digits=0): x is a RATIO (0.236 -> "%23,6" in Turkish with digits=1, "23.6%" in English).
 */
export function percent(x, digits = 0) {
  const v = toNumber(x);
  if (v === null) return DASH;
  return nf(locale(), {
    style: "percent",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(v);
}

/** Parse any supported time value into a Date (or null). */
export function toDate(v) {
  if (v === null || v === undefined || v === "") return null;
  if (v instanceof Date) return Number.isNaN(v.getTime()) ? null : v;
  if (typeof v === "number") {
    if (!Number.isFinite(v)) return null;
    return new Date(v > 1e12 ? v : v * 1000);
  }
  if (typeof v === "string") {
    if (/^\d+(\.\d+)?$/.test(v)) return toDate(Number(v));
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  return null;
}

function df(opts) {
  const key = "d" + locale() + JSON.stringify(opts);
  let f = cache.get(key);
  if (!f) {
    f = new Intl.DateTimeFormat(locale(), opts);
    cache.set(key, f);
  }
  return f;
}

/** date(epochSec) -> "24 Eyl" (this year) / "24 Eyl 2025"; en "Sep 24" / "Sep 24, 2025". */
export function date(v) {
  const d = toDate(v);
  if (!d) return DASH;
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return df(sameYear ? { day: "numeric", month: "short" } : { day: "numeric", month: "short", year: "numeric" }).format(d);
}

/** dateTime(epochSec) -> "24 Eyl 14:05" / "Sep 24, 2:05 PM". */
export function dateTime(v) {
  const d = toDate(v);
  if (!d) return DASH;
  const sameYear = d.getFullYear() === new Date().getFullYear();
  const opts = { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" };
  if (!sameYear) opts.year = "numeric";
  return df(opts).format(d);
}

/** time(epochSec) -> "14:05". */
export function time(v) {
  const d = toDate(v);
  if (!d) return DASH;
  return df({ hour: "2-digit", minute: "2-digit" }).format(d);
}

/** Month name: monthName(8) -> "Eylül" / "September" (0-based month). short -> "Eyl". */
export function monthName(monthIndex, short = false) {
  const d = new Date(2000, monthIndex, 1);
  return df({ month: short ? "short" : "long" }).format(d);
}

/** relative(epochSec) -> "az önce", "5 dk önce", "3 sa önce", "Dün", "2 gün önce", else date(). */
export function relative(v) {
  const d = toDate(v);
  if (!d) return DASH;
  const diff = Math.round((Date.now() - d.getTime()) / 1000);
  if (diff < 45) return t("time.just_now");
  const min = Math.round(diff / 60);
  if (min < 60) return t("time.minutes_ago", { n: Math.max(1, min) });
  const hours = Math.round(diff / 3600);
  if (hours < 24) return t("time.hours_ago", { n: hours });
  const days = Math.round(diff / 86400);
  if (days === 1) return t("time.yesterday");
  if (days < 7) return t("time.days_ago", { n: days });
  return date(d);
}

/** duration(192) -> "3 dk 12 sn" / "3 min 12 s"; hours when needed. */
export function duration(seconds) {
  const v = toNumber(seconds);
  if (v === null || v < 0) return DASH;
  const s = Math.round(v);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return t("time.duration_h", { h, m });
  if (m > 0) return t("time.duration_m", { m, s: sec });
  return t("time.duration_s", { s: sec });
}

/** bytes(1234567) -> "1,2 MB" / "1.2 MB". */
export function bytes(n) {
  const v = toNumber(n);
  if (v === null) return DASH;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let x = Math.abs(v);
  while (x >= 1024 && i < units.length - 1) {
    x /= 1024;
    i += 1;
  }
  const digits = i === 0 || x >= 100 ? 0 : 1;
  return `${nf(locale(), { maximumFractionDigits: digits }).format(v < 0 ? -x : x)} ${units[i]}`;
}

/** Plain-language list: ["a","b","c"] -> "a, b ve c" / "a, b and c". */
export function list(items) {
  const arr = (items || []).map(String);
  try {
    return new Intl.ListFormat(locale(), { style: "long", type: "conjunction" }).format(arr);
  } catch {
    return arr.join(", ");
  }
}

/** Lowercase / uppercase with the right Turkish i/ı/İ rules. */
export function lower(s) {
  return String(s ?? "").toLocaleLowerCase(getLang() === "tr" ? "tr-TR" : "en-US");
}
export function upper(s) {
  return String(s ?? "").toLocaleUpperCase(getLang() === "tr" ? "tr-TR" : "en-US");
}

export default { money, number, compact, percent, toDate, date, dateTime, time, monthName, relative, duration, bytes, list, lower, upper };
