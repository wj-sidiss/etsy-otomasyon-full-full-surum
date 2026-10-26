// fetch wrapper for the local etsyotomasyon server.
//
//   api.get("/api/status")                       -> parsed JSON
//   api.get("/api/jobs", {kind: "drop", active: 1})
//   api.post("/api/shops/switch", {id: "2"})
//   api.upload("/api/designs/upload", file, {query: {name: file.name}, onProgress})
//   <img src={api.url("/api/files/thumb", {path, w: 160})}>
//   await api.download("/api/seo/export.csv", {params: {state}, filename: "seo.csv"})
//
// Paths may be written "/api/x" or just "x" (-> "/api/x"). Every request sends the
// session cookie (same-origin) and the header "X-Etsyotomasyon: 1". Non-2xx answers throw
// ApiError {status, code, message, params}; a network failure throws code "network".

import { has as hasKey, t as commonT } from "./i18n.js";

export class ApiError extends Error {
  constructor(status, code, message, params) {
    super(message || code || `HTTP ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code || "internal";
    this.params = params || {};
  }
}

const HTTP_CODES = {
  400: "invalid",
  401: "reconnect",
  403: "forbidden",
  404: "not_found",
  409: "busy",
  413: "too_large",
  422: "invalid",
  429: "rate_limited",
  502: "etsy_error",
  503: "offline",
};

const noSessionHandlers = new Set();
let noSessionFired = false;

function fireNoSession() {
  if (noSessionFired) return;
  noSessionFired = true;
  for (const cb of noSessionHandlers) {
    try {
      cb();
    } catch (err) {
      console.error(err);
    }
  }
}

function apiPath(path) {
  if (/^https?:/i.test(path)) throw new Error("api: absolute URLs are not allowed");
  return path.startsWith("/") ? path : `/api/${path}`;
}

function encodeParams(params) {
  const qs = new URLSearchParams();
  if (!params) return "";
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    const values = Array.isArray(v) ? v : [v];
    for (const item of values) {
      if (item === undefined || item === null) continue;
      qs.append(k, item === true ? "1" : item === false ? "0" : String(item));
    }
  }
  return qs.toString();
}

/** URL for <img src> / links: api.url("/api/files/thumb", {path, w}). */
export function url(path, params) {
  const p = apiPath(path);
  const qs = encodeParams(params);
  if (!qs) return p;
  return p + (p.includes("?") ? "&" : "?") + qs;
}

function toApiError(status, data, statusText) {
  const e = (data && typeof data === "object" && data.error) || {};
  const code = e.code || HTTP_CODES[status] || (status >= 500 ? "internal" : "invalid");
  const message =
    e.message || (typeof data === "string" && data.length < 300 ? data : "") || statusText || "";
  const err = new ApiError(status, code, message, e.params);
  if (err.code === "no_session") fireNoSession();
  return err;
}

function parseBody(text, contentType) {
  if (!text) return null;
  if ((contentType || "").includes("json")) {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }
  return text;
}

async function request(method, path, { params, body, signal, headers } = {}) {
  const init = {
    method,
    credentials: "same-origin",
    cache: "no-store",
    headers: { "X-Etsyotomasyon": "1", Accept: "application/json", ...(headers || {}) },
    signal,
  };
  if (body !== undefined && body !== null) {
    if (body instanceof FormData || body instanceof Blob || body instanceof ArrayBuffer) {
      init.body = body;
    } else {
      init.body = JSON.stringify(body);
      init.headers["Content-Type"] = "application/json";
    }
  } else if (method !== "GET" && method !== "HEAD") {
    init.body = "{}";
    init.headers["Content-Type"] = "application/json";
  }
  let res;
  try {
    res = await fetch(url(path, params), init);
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    throw new ApiError(0, "network", (err && err.message) || "network error");
  }
  let text = "";
  try {
    text = res.status === 204 ? "" : await res.text();
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    throw new ApiError(0, "network", (err && err.message) || "network error");
  }
  const data = parseBody(text, res.headers.get("content-type"));
  if (!res.ok) throw toApiError(res.status, data, res.statusText);
  return data;
}

/**
 * Upload one file as the raw request body (XMLHttpRequest, for progress).
 * opts: {onProgress({loaded, total, fraction}), query, method="PUT", headers, signal}
 * Resolves with the parsed JSON answer.
 */
export function upload(path, file, opts = {}) {
  const { onProgress, query, method = "PUT", headers, signal } = opts;
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(method, url(path, query));
    xhr.withCredentials = true;
    xhr.setRequestHeader("X-Etsyotomasyon", "1");
    xhr.setRequestHeader("Accept", "application/json");
    xhr.setRequestHeader("Content-Type", (file && file.type) || "application/octet-stream");
    for (const [k, v] of Object.entries(headers || {})) xhr.setRequestHeader(k, v);
    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        const total = e.lengthComputable ? e.total : file.size || 0;
        onProgress({ loaded: e.loaded, total, fraction: total ? e.loaded / total : 0 });
      };
    }
    xhr.onload = () => {
      const data = parseBody(xhr.responseText, xhr.getResponseHeader("content-type"));
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(toApiError(xhr.status, data, xhr.statusText));
    };
    xhr.onerror = () => reject(new ApiError(0, "network", "network error"));
    xhr.onabort = () => reject(new DOMException("Aborted", "AbortError"));
    if (signal) {
      if (signal.aborted) {
        reject(new DOMException("Aborted", "AbortError"));
        return;
      }
      signal.addEventListener("abort", () => xhr.abort(), { once: true });
    }
    xhr.send(file);
  });
}

/** The file name in a Content-Disposition header ("" when there is none). */
export function dispositionName(header) {
  if (!header) return "";
  const star = /filename\*\s*=\s*utf-8''([^;]+)/i.exec(header);
  if (star) {
    try {
      return cleanName(decodeURIComponent(star[1].trim().replace(/^"|"$/g, "")));
    } catch {
      /* fall back to the plain form */
    }
  }
  const plain = /filename\s*=\s*"([^"]*)"/i.exec(header) || /filename\s*=\s*([^;]+)/i.exec(header);
  return plain ? cleanName(plain[1].trim()) : "";
}

function cleanName(name) {
  return String(name || "").replace(/[\\/:*?"<>|\u0000-\u001f]+/g, "_").trim();
}

/** Hand a Blob to the browser as a download named `name`. */
export function saveBlob(blob, name) {
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = cleanName(name) || "download";
  a.hidden = true;
  a.dataset.external = ""; // not an app route: the router leaves it alone
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 10000);
}

/**
 * Download a file the API makes (a CSV export): fetched with the session and the
 * X-Etsyotomasyon header, then saved under the server's file name (Content-Disposition),
 * else `filename`. An error answer throws ApiError like any other call, so the page
 * can show a toast instead of the browser saving an error as a file.
 * opts: {params, method="GET", body (JSON), filename, signal}. Resolves with the name.
 */
export async function download(path, { params, method = "GET", body, filename = "download", signal } = {}) {
  const init = { method, credentials: "same-origin", cache: "no-store", headers: { "X-Etsyotomasyon": "1" }, signal };
  if (body !== undefined && body !== null) {
    init.body = JSON.stringify(body);
    init.headers["Content-Type"] = "application/json";
  } else if (method !== "GET" && method !== "HEAD") {
    init.body = "{}";
    init.headers["Content-Type"] = "application/json";
  }
  let res;
  let blob;
  try {
    res = await fetch(url(path, params), init);
    if (!res.ok) {
      const text = await res.text();
      throw toApiError(res.status, parseBody(text, res.headers.get("content-type")), res.statusText);
    }
    blob = await res.blob();
  } catch (err) {
    if (err instanceof ApiError || (err && err.name === "AbortError")) throw err;
    throw new ApiError(0, "network", (err && err.message) || "network error");
  }
  const name = dispositionName(res.headers.get("content-disposition")) || filename;
  saveBlob(blob, name);
  return name;
}

/**
 * The text to show for an error: common.json "errors.<code>" (params + {message, status}
 * are interpolated), else the error's own message. AbortError -> "".
 */
export function errorText(err, t) {
  if (!err) return "";
  if (err.name === "AbortError") return "";
  const tr = typeof t === "function" ? t : commonT;
  const code = err.code;
  if (code) {
    const key = `errors.${code}`;
    const known = (typeof tr.has === "function" && tr.has(key)) || hasKey(key);
    if (known) {
      // The server's params win: etsy_error sends Etsy's own status as params.status,
      // which must not be replaced by the HTTP status of our answer (502).
      return tr(key, { message: err.message || "", status: err.status, ...(err.params || {}) });
    }
  }
  return err.message || String(err);
}

/** True for AbortError (a request cancelled on purpose, e.g. page left). */
export function isAbort(err) {
  return !!err && err.name === "AbortError";
}

/** Register a callback for a 401 no_session answer (fires once). Returns unsubscribe. */
export function onNoSession(cb) {
  noSessionHandlers.add(cb);
  return () => noSessionHandlers.delete(cb);
}

export const api = {
  get: (path, params, opts) => request("GET", path, { ...(opts || {}), params }),
  post: (path, body, opts) => request("POST", path, { ...(opts || {}), body }),
  put: (path, body, opts) => request("PUT", path, { ...(opts || {}), body }),
  patch: (path, body, opts) => request("PATCH", path, { ...(opts || {}), body }),
  del: (path, body, opts) => request("DELETE", path, { ...(opts || {}), body }),
  upload,
  download,
  saveBlob,
  url,
  errorText,
  isAbort,
  onNoSession,
  ApiError,
};

export default api;
