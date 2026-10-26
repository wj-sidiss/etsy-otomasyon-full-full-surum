// String loader and formatter.
//
// Files: /i18n/<ns>.json = {"tr": {...}, "en": {...}} (flat keys, dots allowed).
// t(key, params): page namespace first, then "common"; "{name}" interpolation;
// when params.n === 1 and key + "_one" exists, that form is used.
// A key written "ns:key" (e.g. "orders:shipped") looks in that namespace explicitly.
// A missing key renders as the key itself and warns once in the console.

const LANGS = ["tr", "en"];
const dicts = new Map(); // ns -> {tr: {...}, en: {...}}
const pending = new Map(); // ns -> Promise
const warned = new Set();
let lang = guessLang();

function guessLang() {
  try {
    const nav = (navigator.language || "tr").toLowerCase();
    return nav.startsWith("tr") ? "tr" : "en";
  } catch {
    return "tr";
  }
}

/** Current UI language: "tr" | "en". */
export function getLang() {
  return lang;
}

/** Set the UI language (does not reload anything by itself). */
export function setLang(value) {
  lang = value === "en" ? "en" : "tr";
  try {
    document.documentElement.lang = lang;
  } catch {
    /* no document (tests) */
  }
  return lang;
}

/** BCP-47 locale for Intl: "tr-TR" | "en-US". */
export function locale() {
  return lang === "en" ? "en-US" : "tr-TR";
}

export function isLoaded(ns) {
  return dicts.has(ns);
}

/**
 * Load /i18n/<ns>.json once. Never rejects: a missing or broken file just leaves the
 * namespace empty (keys then fall back to common, then show the key).
 */
export function loadNamespace(ns) {
  if (dicts.has(ns)) return Promise.resolve(dicts.get(ns));
  if (pending.has(ns)) return pending.get(ns);
  const p = fetch(`/i18n/${encodeURIComponent(ns)}.json`, {
    credentials: "same-origin",
    cache: "no-cache",
  })
    .then((res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    })
    .then((data) => {
      const dict = {};
      for (const l of LANGS) dict[l] = (data && typeof data[l] === "object" && data[l]) || {};
      dicts.set(ns, dict);
      return dict;
    })
    .catch((err) => {
      console.warn(`[i18n] could not load namespace "${ns}":`, err);
      const dict = { tr: {}, en: {} };
      dicts.set(ns, dict);
      return dict;
    })
    .finally(() => pending.delete(ns));
  pending.set(ns, p);
  return p;
}

/** Load several namespaces in parallel. */
export function loadNamespaces(list) {
  return Promise.all([...new Set(list)].map((ns) => loadNamespace(ns)));
}

function raw(ns, key, l = lang) {
  const d = dicts.get(ns);
  if (!d) return undefined;
  const v = d[l] && d[l][key];
  return typeof v === "string" ? v : undefined;
}

const NS_PREFIX = /^([a-z][a-z0-9-]*):(.+)$/;

function resolve(ns, key, params, l = lang) {
  let namespaces = ns && ns !== "common" ? [ns, "common"] : ["common"];
  let k = key;
  const m = NS_PREFIX.exec(key);
  if (m) {
    namespaces = m[1] === "common" ? ["common"] : [m[1], "common"];
    k = m[2];
  }
  const tryKeys = params && params.n === 1 ? [k + "_one", k] : [k];
  for (const candidate of tryKeys) {
    for (const n of namespaces) {
      const v = raw(n, candidate, l);
      if (v !== undefined) return v;
    }
  }
  return undefined;
}

/** "{name}" -> params.name; unknown placeholders are left as they are. */
export function interpolate(str, params) {
  if (!params || str.indexOf("{") < 0) return str;
  return str.replace(/\{([a-zA-Z0-9_]+)\}/g, (whole, name) =>
    params[name] === undefined || params[name] === null ? whole : String(params[name]),
  );
}

/** Translate in namespace `ns` (falls back to common). */
export function translate(ns, key, params) {
  const v = resolve(ns, key, params);
  if (v === undefined) {
    const id = `${ns}|${key}`;
    if (!warned.has(id)) {
      warned.add(id);
      console.warn(`[i18n] missing key "${key}" (namespace "${ns}", ${lang})`);
    }
    return key;
  }
  return interpolate(v, params);
}

/** True when the key resolves in `ns` (or common). */
export function has(key, ns = "common") {
  return resolve(ns, key, null) !== undefined;
}

/**
 * A t() bound to a namespace. The function also carries:
 *   t.has(key) -> bool, t.ns, t.lang
 */
export function translator(ns = "common") {
  const fn = (key, params) => translate(ns, key, params);
  fn.has = (key) => has(key, ns);
  fn.ns = ns;
  fn.lang = lang;
  return fn;
}

/** The common-namespace translator. */
export function t(key, params) {
  return translate("common", key, params);
}
t.has = (key) => has(key, "common");
t.ns = "common";

/** The string in every language: {tr, en} (used where the language is unknown). */
export function allLanguages(key, params, ns = "common") {
  const out = {};
  for (const l of LANGS) {
    const v = resolve(ns, key, params, l);
    out[l] = v === undefined ? key : interpolate(v, params);
  }
  return out;
}
