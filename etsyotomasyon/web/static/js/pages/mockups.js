// Mockuplar (frames t170, t180) and the print-area editor (frame t190).
//
//   /kurulum/mockuplar         grid of mockup cards, type filter chips, upload by picker or drop,
//                              and under it the Filigran card (#filigran): the seller's watermark
//   /kurulum/mockuplar/:name   editor: library list, canvas with the print-area rectangle (or
//                              four corners for perspective), X / Y / width / height fields,
//                              realism and curve sliders, same-size apply, preview design
//
// Everything is local (1-MOCKUPS in the products folder); no Etsy call is made, so the
// page works before any key is saved. Endpoints: etsyotomasyon/web/api/mockups.py and
// etsyotomasyon/web/api/watermark.py.

import {
  badge,
  button,
  checkbox,
  chips,
  cx,
  debounce,
  dropzone,
  emptyState,
  field,
  filesFromDrop,
  h,
  iconButton,
  infoNote,
  menu,
  mount,
  progressBar,
  sectionTitle,
  select,
  skeleton,
  spinner,
  svg,
  tabs,
  textInput,
  toggle,
  uid,
} from "../ui.js";
import { icon } from "../icons.js";
import { percent } from "../format.js";

const IMAGE_RE = /\.(png|jpe?g|webp|gif|bmp|tiff?)$/i;
const ACCEPT = ".png,.jpg,.jpeg,.webp,.gif,.bmp,.tif,.tiff,image/png,image/jpeg,image/webp,image/gif,image/bmp,image/tiff";
const TYPES = ["tshirt", "sweatshirt", "hoodie", "mug", "poster", "canvas", "phone_case", "tote", "pillow", "sticker", "other"];
const APPLIED_KEY = "etsyotomasyon.mockups.applied";
const DESIGN_KEY = "etsyotomasyon.mockups.design";
const SHOW_KEY = "etsyotomasyon.mockups.show-design";
const ZOOMS = [1, 1.5, 2, 3, 4];
const IMAGE_MAX = 1400;
const HANDLES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];
// The four-corner area's corners, in the order the server stores them.
const CORNERS = ["tl", "tr", "br", "bl"];
const SAMPLE = { id: "sample", version: "1" };
const GRID_PATH = "/kurulum/mockuplar";
// The "Baskı alanı" pill shows once the rectangle is this big on screen (CSS px): the
// video's PrintGuide shows it from 104 x 44 px on a 540 px photo, about 94 x 40 here.
const LABEL_MIN_W = 94;
const LABEL_MIN_H = 40;

const enc = encodeURIComponent;
const editorPath = (name) => `${GRID_PATH}/${enc(name)}`;
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export default {
  async mount(el, ctx) {
    return ctx.params && ctx.params.name ? mountEditor(el, ctx, ctx.params.name) : mountGrid(el, ctx);
  },
};

// ------------------------------------------------------------------ small helpers

function store(kind) {
  try {
    return kind === "session" ? window.sessionStorage : window.localStorage;
  } catch {
    return null;
  }
}

function readStored(kind, key, fallback) {
  const s = store(kind);
  if (!s) return fallback;
  try {
    const raw = s.getItem(key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

function writeStored(kind, key, value) {
  const s = store(kind);
  if (!s) return;
  try {
    s.setItem(key, JSON.stringify(value));
  } catch {
    /* private mode or full: a convenience only */
  }
}

function stem(name) {
  return String(name || "").replace(/\.[^.]+$/, "");
}

function typeLabel(t, type) {
  const key = `type.${type}`;
  return t.has(key) ? t(key) : String(type || "");
}

function colorLabel(t, color) {
  if (!color) return "";
  const key = `color.${color}`;
  return t.has(key) ? t(key) : color;
}

/** "Tişört · Beyaz"; a mockup of type "other" is called by its file name. */
function itemLabel(t, item) {
  const base = item.type === "other" ? stem(item.name) : typeLabel(t, item.type);
  const color = colorLabel(t, item.color);
  return color ? `${base} · ${color}` : base;
}

function sizeText(item) {
  return item.width && item.height ? `${item.width}×${item.height}` : "–";
}

function thumbUrl(ctx, item, w) {
  return ctx.api.url("/api/files/thumb", { path: item.path, w, v: item.version });
}

function designUrl(ctx, design, max) {
  return ctx.api.url("/api/mockups/design-image", { design: design.id, max, v: design.version || "1" });
}

function designLabel(t, design) {
  return !design || design.id === SAMPLE.id ? t("editor.sample") : design.label || design.name || design.id;
}

function sameArea(a, b) {
  if (!a || !b) return false;
  return ["x", "y", "w", "h"].every((k) => Math.abs(a[k] - b[k]) < 1e-5);
}

function roundArea(a) {
  const r = (v) => Math.round(v * 100000) / 100000;
  return { x: r(a.x), y: r(a.y), w: r(a.w), h: r(a.h) };
}

/** Four [x, y] fractions -> the same, rounded like roundArea (null stays null). */
function roundQuad(q) {
  const r = (v) => Math.round(v * 100000) / 100000;
  return q ? q.map(([x, y]) => [r(x), r(y)]) : null;
}

function sameQuad(a, b) {
  if (!a || !b) return !a && !b;
  return a.every((p, i) => Math.abs(p[0] - b[i][0]) < 1e-5 && Math.abs(p[1] - b[i][1]) < 1e-5);
}

/** The rectangle {x, y, w, h} -> its corners, top-left first, clockwise. */
function rectQuad(a) {
  return [
    [a.x, a.y],
    [a.x + a.w, a.y],
    [a.x + a.w, a.y + a.h],
    [a.x, a.y + a.h],
  ];
}

/** Four corners -> their bounding box {x, y, w, h}. */
function quadBox(q) {
  const xs = q.map((p) => p[0]);
  const ys = q.map((p) => p[1]);
  const x = Math.min(...xs);
  const y = Math.min(...ys);
  return { x, y, w: Math.max(...xs) - x, h: Math.max(...ys) - y };
}

/** Whether the corners (in pixels) turn clockwise at every corner: no dent, no twist. */
function isConvex(q) {
  for (let i = 0; i < 4; i++) {
    const [x0, y0] = q[i];
    const [x1, y1] = q[(i + 1) % 4];
    const [x2, y2] = q[(i + 2) % 4];
    if ((x1 - x0) * (y2 - y1) - (y1 - y0) * (x2 - x1) <= 1e-9) return false;
  }
  return true;
}

function triangle(p, q, r) {
  return Math.abs((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])) / 2;
}

/** Convex, in order, and no corner flattened into a side (its triangle with its two
 *  neighbours at least 8% of the shape): the server's surface.is_well_shaped. */
function wellShaped(q) {
  if (!isConvex(q)) return false;
  const whole = triangle(q[0], q[1], q[2]) + triangle(q[0], q[2], q[3]);
  if (whole <= 0) return false;
  for (let i = 0; i < 4; i++) if (triangle(q[(i + 3) % 4], q[i], q[(i + 1) % 4]) < 0.08 * whole) return false;
  return true;
}

function insideQuad(q, x, y) {
  for (let i = 0; i < 4; i++) {
    const [x0, y0] = q[i];
    const [x1, y1] = q[(i + 1) % 4];
    if ((x1 - x0) * (y - y0) - (y1 - y0) * (x - x0) < 0) return false;
  }
  return true;
}

/** The 3x3 homography (row-major, last 1) taking four src points to four dst points. */
function homography(src, dst) {
  const A = [];
  for (let i = 0; i < 4; i++) {
    const [x, y] = src[i];
    const [u, v] = dst[i];
    A.push([x, y, 1, 0, 0, 0, -u * x, -u * y, u]);
    A.push([0, 0, 0, x, y, 1, -v * x, -v * y, v]);
  }
  for (let c = 0; c < 8; c++) {
    let pivot = c;
    for (let r = c + 1; r < 8; r++) if (Math.abs(A[r][c]) > Math.abs(A[pivot][c])) pivot = r;
    if (Math.abs(A[pivot][c]) < 1e-12) return null;
    [A[c], A[pivot]] = [A[pivot], A[c]];
    const lead = A[c][c];
    for (let k = c; k < 9; k++) A[c][k] /= lead;
    for (let r = 0; r < 8; r++) {
      if (r === c || !A[r][c]) continue;
      const f = A[r][c];
      for (let k = c; k < 9; k++) A[r][k] -= f * A[c][k];
    }
  }
  return [...A.map((row) => row[8]), 1];
}

function applyH(M, x, y) {
  const w = M[6] * x + M[7] * y + M[8];
  return [(M[0] * x + M[1] * y + M[2]) / w, (M[3] * x + M[4] * y + M[5]) / w];
}

/** "%34,0" (tr) / "34.0%" (en) for a fraction. */
function pct(fraction) {
  return percent(fraction, 1);
}

/** Read what someone typed into a % field ("34,5", "%34.5", "34") -> 0..100 or null. */
function parsePercent(raw) {
  let s = String(raw ?? "").replace(/[%\s ]/g, "");
  if (!s) return null;
  if (s.includes(",") && !s.includes(".")) s = s.replace(",", ".");
  else s = s.replace(/,/g, "");
  const n = Number(s);
  return Number.isFinite(n) ? clamp(n, 0, 100) : null;
}

function pickFiles({ multiple = true } = {}) {
  return new Promise((resolve) => {
    const input = h("input", { type: "file", accept: ACCEPT, multiple, class: "sr-only", tabindex: "-1", "aria-hidden": "true" });
    const done = (files) => {
      input.remove();
      resolve(files);
    };
    input.addEventListener("change", () => done([...(input.files || [])]), { once: true });
    input.addEventListener("cancel", () => done([]), { once: true });
    document.body.appendChild(input);
    input.click();
  });
}

async function openFolder(ctx) {
  try {
    await ctx.api.post("/api/open-folder", { which: "mockups" });
  } catch (err) {
    if (!ctx.api.isAbort(err)) ctx.toast({ tone: "danger", title: ctx.t("open_folder_failed"), message: ctx.api.errorText(err, ctx.t) });
  }
}

/**
 * Upload files one by one (PUT /api/mockups/files). A progress toast for the batch, an
 * error toast per refused file, a success toast at the end. hooks: onStart, onProgress,
 * onDone(file, item), onFail(file, err). Resolves {added: [item], failed: [{file, err}]}.
 */
async function uploadBatch(ctx, list, hooks = {}) {
  const t = ctx.t;
  const images = list.filter((f) => IMAGE_RE.test((f && f.name) || ""));
  const skipped = list.length - images.length;
  if (skipped) ctx.toast({ tone: "warning", title: t("upload.skipped", { n: skipped }) });
  const result = { added: [], failed: [] };
  if (!images.length) return result;

  const msg = h("span", { class: "num" }, t("upload.progress", { done: 0, total: images.length }));
  const bar = progressBar({ value: 0, max: images.length, size: "sm", label: t("upload.title") });
  const note = ctx.toast({ tone: "info", title: t("upload.title"), message: h("span", { class: "mk-toast-body" }, msg, bar.el), timeout: 0 });
  const small = [];
  try {
    for (let i = 0; i < images.length; i += 1) {
      const file = images[i];
      if (hooks.onStart) hooks.onStart(file);
      try {
        const res = await ctx.api.upload("/api/mockups/files", file, {
          query: { name: file.name },
          signal: ctx.signal,
          onProgress: (p) => {
            bar.update(i + p.fraction);
            if (hooks.onProgress) hooks.onProgress(file, p.fraction);
          },
        });
        result.added.push(res.item);
        if (res.item && res.item.small) small.push(res.item.name);
        if (hooks.onDone) hooks.onDone(file, res.item);
      } catch (err) {
        if (ctx.api.isAbort(err)) throw err;
        result.failed.push({ file, err });
        if (hooks.onFail) hooks.onFail(file, err);
        ctx.toast({ tone: "danger", title: t("upload.failed", { name: file.name }), message: ctx.api.errorText(err, t), timeout: 9000 });
      }
      bar.update(i + 1);
      msg.textContent = t("upload.progress", { done: i + 1, total: images.length });
    }
  } finally {
    note.close();
  }
  if (result.added.length) {
    ctx.toast({ tone: "success", title: t("upload.done", { n: result.added.length }), message: t("upload.done_msg") });
  }
  if (small.length) {
    ctx.toast({ tone: "warning", title: t("upload.small_title"), message: t("upload.small", { names: small.join(", ") }), timeout: 9000 });
  }
  return result;
}

/**
 * Accept files dropped anywhere on the page (the element is a drop target for app.js).
 * Shows a full-area overlay while files are dragged over; ui.dropzone children handle
 * their own drops.
 */
function installPageDrop(el, ctx, onFiles) {
  el.dataset.dropTarget = "";
  const overlay = h(
    "div",
    { class: "mk-drop", "aria-hidden": "true" },
    h(
      "div",
      { class: "mk-drop-box" },
      h("span", { class: "mk-drop-icon" }, icon("upload", { size: 26 })),
      h("p", { class: "mk-drop-title" }, ctx.t("drop.title")),
      h("p", { class: "mk-drop-sub" }, ctx.t("drop.sub")),
    ),
  );
  overlay.hidden = true;
  el.appendChild(overlay);
  let timer = null;
  const hasFiles = (e) => !!e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files");
  // A dropzone takes its own files; so does the Filigran card (a watermark, not a mockup).
  const inZone = (e) => !!(e.target && e.target.closest && e.target.closest(".dropzone, .mk-wm"));
  const onOver = (e) => {
    if (!hasFiles(e) || inZone(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    overlay.hidden = false;
    clearTimeout(timer);
    timer = setTimeout(() => {
      overlay.hidden = true;
    }, 200);
  };
  const onDrop = async (e) => {
    if (!hasFiles(e) || inZone(e) || e.defaultPrevented) return;
    e.preventDefault();
    clearTimeout(timer);
    overlay.hidden = true;
    const items = await filesFromDrop(e.dataTransfer);
    onFiles(items.map((x) => x.file));
  };
  el.addEventListener("dragenter", onOver);
  el.addEventListener("dragover", onOver);
  el.addEventListener("drop", onDrop);
  return () => clearTimeout(timer);
}

/** The type / colour modal. Resolves with the updated item, or null. */
function editMeta(ctx, item) {
  const t = ctx.t;
  return new Promise((resolve) => {
    let result = null;
    const typeSel = select({ options: TYPES.map((ty) => ({ value: ty, label: typeLabel(t, ty) })), value: item.type, ariaLabel: t("meta.type") });
    const colorIn = textInput({ value: item.color || "", placeholder: t("meta.color_placeholder"), maxLength: 40 });
    const colorField = field({ label: t("meta.color"), input: colorIn, hint: t("meta.hint") });
    const m = ctx.modal({
      title: t("meta.title"),
      subtitle: item.name,
      width: 440,
      body: h("div", { class: "stack mk-meta" }, field({ label: t("meta.type"), input: typeSel }), colorField),
      actions: [
        { label: t("common.cancel"), variant: "secondary" },
        {
          label: t("common.save"),
          variant: "primary",
          icon: "check",
          autoLoading: true,
          onClick: async ({ close }) => {
            try {
              const res = await ctx.api.patch(`/api/mockups/${enc(item.name)}`, { type: typeSel.value, color: colorIn.value.trim() });
              result = res.item;
              close();
            } catch (err) {
              colorField.setError(ctx.api.errorText(err, t));
            }
          },
        },
      ],
      onClose: () => resolve(result),
    });
    colorIn.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        const save = m.el.querySelector(".modal-actions .btn-primary");
        if (save) save.click();
      }
    });
  });
}

async function deleteMockup(ctx, item) {
  const t = ctx.t;
  const ok = await ctx.confirm({
    title: t("delete.title"),
    message: t("delete.message", { label: itemLabel(t, item), name: item.name }),
    confirmLabel: t("menu.delete"),
    danger: true,
  });
  if (!ok) return false;
  try {
    await ctx.api.del(`/api/mockups/${enc(item.name)}`);
    ctx.toast({ tone: "success", title: t("delete.done"), message: item.name });
    return true;
  } catch (err) {
    if (!ctx.api.isAbort(err)) ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
    return false;
  }
}

async function setEnabled(ctx, item, enabled) {
  const t = ctx.t;
  try {
    const res = await ctx.api.patch(`/api/mockups/${enc(item.name)}`, { enabled });
    ctx.toast({ tone: enabled ? "success" : "info", title: t(enabled ? "enabled.on" : "enabled.off"), message: itemLabel(t, res.item), timeout: 3000 });
    return res.item;
  } catch (err) {
    if (!ctx.api.isAbort(err)) ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
    return null;
  }
}

function areaState(t, item) {
  if (item.area_source === "own") {
    return h("span", { class: "mk-state is-own", title: t("area.own_hint") }, t("area.own"), icon("check", { size: 12, strokeWidth: 2.4 }));
  }
  if (item.area_source === "same_size") return h("span", { class: "mk-state is-shared", title: t("area.same_size_hint") }, t("area.same_size"));
  return h("span", { class: "mk-state is-default", title: t("area.default_hint") }, icon("crop", { size: 12 }), t("area.default"));
}

// ------------------------------------------------------------------ which mockups drafts use

/**
 * The one rule (catalog.usage on the server, the same for Tasarım Yükle and the run):
 * switched-on mockups in the seller's order; the first `max` are used, position 1 is the
 * draft's main image, the rest are over the limit. -> {pos: Map(name -> n), over: Set,
 * used, on, main}
 */
function plan(order, isOn, max) {
  const pos = new Map();
  const over = new Set();
  let on = 0;
  let main = null;
  for (const name of order) {
    if (!isOn(name)) continue;
    on += 1;
    if (on === 1) main = name;
    if (on <= max) pos.set(name, on);
    else over.add(name);
  }
  return { pos, over, used: Math.min(on, max), on, main };
}

/** `order` with `name` taken out and put before (or after) `target`. */
function placed(order, name, target, after = false) {
  const rest = order.filter((n) => n !== name);
  if (target === null) return [name, ...rest];
  const i = rest.indexOf(target);
  if (i < 0) return order.slice();
  rest.splice(after ? i + 1 : i, 0, name);
  return rest;
}

function sameList(a, b) {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

// ------------------------------------------------------------------ grid

async function mountGrid(el, ctx) {
  const t = ctx.t;
  el.classList.add("mk-page-grid");
  let data = null;
  let byName = new Map();
  let filter = ctx.query.type || "all";
  const uploads = new Map(); // File -> {el, bar}
  const images = new Map(); // name|version -> <img>, reused so re-renders do not flicker
  let chain = Promise.resolve();
  // Selection mode: a draft of the order and of which mockups are on, saved in one go.
  let sel = null; // {order: [names], on: Set, saving: bool}
  let drag = null;
  let suppressClick = false;
  const cleanups = [];

  // 19 (Etsy's 20 pictures less the flat design), less one per info image (Şablon İlan).
  const max = () => (data && typeof data.max_enabled === "number" ? data.max_enabled : 19);
  const infoCount = () => (data && data.info_images) || 0;
  const usageRule = () => (infoCount() ? t("usage.rule_info", { info: t("usage.info_part", { n: infoCount() }) }) : t("usage.rule"));
  const savedOrder = () => (data ? data.items.map((it) => it.name) : []);
  const order = () => (sel ? sel.order : savedOrder());
  const isOn = (name) => (sel ? sel.on.has(name) : !!(byName.get(name) || {}).enabled);
  const current = () => plan(order(), isOn, max());
  const selDirty = () => {
    if (!sel || !data) return false;
    if (!sameList(sel.order, savedOrder())) return true;
    return data.items.some((it) => it.enabled !== sel.on.has(it.name));
  };

  // The header holds only "Mockup ekle", as in the video; "Klasörü aç" is in each card's menu.
  const addBtn = button({ label: t("add"), icon: "plus", variant: "primary", onClick: () => pickAndUpload() });
  ctx.setHeader({ actions: [addBtn] });

  const chipsCtl = chips({
    items: [{ id: "all", label: t("all") }],
    value: filter,
    ariaLabel: t("filter_label"),
    onChange: (id) => {
      filter = id;
      ctx.setQuery({ type: id === "all" ? null : id });
      renderGrid();
    },
  });
  // "Seç ve sırala" sits in the filter bar; the counter panel below shows only when it has
  // something to say (over the limit, nothing switched on) or while choosing.
  const selectStartBtn = button({ label: t("select.start"), icon: "check-circle", variant: "ghost", size: "sm", class: "mk-select-start", onClick: () => enterSelect() });
  selectStartBtn.hidden = true;
  const toolbar = h(
    "div",
    { class: "mk-toolbar" },
    chipsCtl.el,
    selectStartBtn,
    // The video's burst ("sparkle", MockuplarEkrani.tsx FilterBar), not the twin stars.
    h("p", { class: "mk-autonote" }, icon("sparkle", { size: 15 }), h("span", null, t("auto_note"))),
  );
  const usageHost = h("section", { class: "mk-usage", "aria-label": t("usage.label") });
  // Selection tools sit under the counter and scroll away; the counter and Save stay.
  const toolsHost = h("div", { class: "mk-select-panel" });
  toolsHost.hidden = true;
  const notes = h("div", { class: "mk-notes" });
  const grid = h("div", { class: "mk-grid", role: "list" });
  // Filigran: under the grid, so the page opens as the video's; "#filigran" scrolls to it.
  const wmCtl = watermarkSection(ctx, { labelFor: (name) => (byName.get(name) ? itemLabel(t, byName.get(name)) : stem(name)) });
  wmCtl.el.hidden = true;
  el.append(toolbar, usageHost, toolsHost, notes, grid, wmCtl.el);
  cleanups.push(() => wmCtl.flush());
  // A reload or a closed tab never runs the cleanup: what waits goes with the page.
  window.addEventListener("pagehide", wmCtl.flushOnExit);
  cleanups.push(() => window.removeEventListener("pagehide", wmCtl.flushOnExit));
  const offDrop = installPageDrop(el, ctx, (files) => queueUpload(files));
  cleanups.push(offDrop);

  ctx.onBeforeLeave(async () => {
    if (!selDirty()) return true;
    const ok = await ctx.confirm({ title: t("select.unsaved_title"), message: t("select.unsaved_msg"), confirmLabel: t("editor.leave"), danger: true });
    if (ok) {
      sel = null;
      ctx.setDirty(false);
    }
    return ok;
  });

  function renderSkeleton() {
    toolbar.hidden = false;
    selectStartBtn.hidden = true;
    notes.hidden = true;
    usageHost.hidden = true;
    toolsHost.hidden = true;
    chipsCtl.update([{ id: "all", label: t("all") }], "all");
    mount(
      grid,
      Array.from({ length: 8 }, () =>
        h(
          "div",
          { class: "mk-card is-skeleton", "aria-hidden": "true" },
          h("div", { class: "mk-card-media" }, h("span", { class: "skeleton mk-sk-img" })),
          h("div", { class: "mk-card-foot" }, skeleton({ lines: 2, height: 11, widths: ["62%", "44%"] })),
        ),
      ),
    );
  }

  function renderError(err) {
    toolbar.hidden = true;
    notes.hidden = true;
    usageHost.hidden = true;
    toolsHost.hidden = true;
    wmCtl.el.hidden = true;
    mount(
      grid,
      h(
        "div",
        { class: "mk-grid-full" },
        emptyState({
          icon: "alert",
          title: t("load_error"),
          message: ctx.api.errorText(err, t),
          action: button({ label: t("common.retry"), icon: "refresh", onClick: () => load() }),
        }),
      ),
    );
  }

  function renderEmpty() {
    toolbar.hidden = true;
    notes.hidden = true;
    usageHost.hidden = true;
    toolsHost.hidden = true;
    const examples = [
      ["shirt", "tshirt"],
      ["mug", "mug"],
      ["frame", "poster"],
      ["phone", "phone_case"],
      ["bag", "tote"],
    ];
    const zone = dropzone({
      accept: ACCEPT,
      multiple: true,
      class: "mk-empty",
      onFiles: (list) => queueUpload(list.map((x) => x.file)),
      onReject: (bad) => ctx.toast({ tone: "warning", title: t("upload.skipped", { n: bad.length }) }),
      content: [
        h("span", { class: "mk-empty-icon" }, icon("image", { size: 26 })),
        h("p", { class: "mk-empty-title" }, t("empty.title")),
        h("p", { class: "mk-empty-sub" }, t("empty.sub")),
        h(
          "div",
          { class: "mk-empty-types", "aria-hidden": "true" },
          examples.map(([ic, ty]) => h("span", { class: "mk-empty-type" }, icon(ic, { size: 18 }), h("span", null, typeLabel(t, ty)))),
        ),
        h(
          "div",
          { class: "mk-empty-actions" },
          button({ label: t("empty.pick"), icon: "upload", variant: "primary", onClick: () => pickAndUpload() }),
          button({ label: t("open_folder"), icon: "folder-open", variant: "ghost", onClick: () => openFolder(ctx) }),
        ),
        h("p", { class: "mk-empty-what" }, t("empty.what")),
      ],
    });
    mount(grid, h("div", { class: "mk-grid-full" }, zone));
  }

  // ---- the counter, and the selection tools while choosing

  function renderUsage() {
    if (!data || !data.items.length) {
      usageHost.hidden = true;
      toolsHost.hidden = true;
      selectStartBtn.hidden = true;
      return;
    }
    const p = current();
    const m = max();
    selectStartBtn.hidden = !!sel;
    // The video goes straight from the filter bar to the cards. The counter stays for what
    // must never go unnoticed (FIXLIST 9): mockups over the limit, none switched on, and
    // while choosing.
    usageHost.hidden = !(sel || p.on > m || p.on === 0);
    if (usageHost.hidden) {
      toolsHost.hidden = true;
      mount(usageHost);
      mount(toolsHost);
      return;
    }
    usageHost.classList.toggle("is-selecting", !!sel);
    usageHost.classList.toggle("is-over", p.on > m);
    const fill = h("span", { class: "mk-meter-fill", style: { width: `${Math.round((p.used / m) * 100)}%` } });
    const meter = h("span", { class: "mk-meter", role: "meter", "aria-valuemin": "0", "aria-valuemax": String(m), "aria-valuenow": String(p.used), "aria-label": t("usage.count", { used: p.used, max: m }) }, fill);
    let sub;
    if (p.on > m) sub = h("p", { class: "mk-usage-sub is-over" }, icon("alert", { size: 13 }), h("span", null, t("usage.over", { n: p.on - m, max: m })));
    else if (!p.on) sub = h("p", { class: "mk-usage-sub is-none" }, icon("info", { size: 13 }), h("span", null, t("usage.none")));
    else {
      const main = byName.get(p.main);
      sub = h("p", { class: "mk-usage-sub" }, icon("star", { size: 13 }), h("span", null, t("usage.main", { label: main ? itemLabel(t, main) : p.main })));
    }
    const text = h(
      "div",
      { class: "mk-usage-text" },
      h("p", { class: "mk-usage-count" }, h("strong", { class: "num" }, t("usage.count", { used: p.used, max: m })), h("span", { class: "mk-usage-rule" }, ` — ${usageRule()}`)),
      sub,
    );
    if (!sel) {
      mount(usageHost, h("div", { class: "mk-usage-row" }, meter, text));
      toolsHost.hidden = true;
      mount(toolsHost);
      return;
    }
    const dirty = selDirty();
    const cancel = button({ label: t("common.cancel"), variant: "secondary", onClick: () => leaveSelect() });
    const save = button({ label: t("common.save"), icon: "check", variant: "primary", disabled: !dirty, loading: sel.saving, onClick: () => saveSelect() });
    const shown = visibleItems().map((it) => it.name);
    const allOn = shown.length > 0 && shown.every((n) => sel.on.has(n));
    const noneOn = shown.every((n) => !sel.on.has(n));
    const tools = h(
      "div",
      { class: "mk-select-tools" },
      button({ label: t("select.all"), icon: "check", size: "sm", variant: "ghost", disabled: allOn, onClick: () => setMany(shown, true) }),
      button({ label: t("select.none"), icon: "x", size: "sm", variant: "ghost", disabled: noneOn, onClick: () => setMany(shown, false) }),
      quickGroup("type"),
      quickGroup("color"),
    );
    mount(usageHost, h("div", { class: "mk-usage-row" }, meter, text, h("div", { class: "spacer" }), cancel, save));
    toolsHost.hidden = false;
    mount(toolsHost, tools, h("p", { class: "mk-select-hint" }, icon("move", { size: 13 }), h("span", null, t("select.hint"))));
    ctx.setDirty(dirty);
  }

  /** "Türe göre: Tişört 3/20 · Kupa 0/5": one click selects the whole group, again clears it. */
  function quickGroup(kind) {
    const groups = new Map();
    for (const name of order()) {
      const it = byName.get(name);
      if (!it) continue;
      const key = kind === "type" ? it.type : it.color;
      if (!key) continue;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(name);
    }
    if (groups.size < 2) return null;
    const label = (key) => (kind === "type" ? typeLabel(t, key) : colorLabel(t, key));
    const keys = [...groups.keys()];
    if (kind === "type") keys.sort((a, b) => typeOrder().indexOf(a) - typeOrder().indexOf(b));
    return h(
      "div",
      { class: "mk-qgroup", role: "group", "aria-label": t(kind === "type" ? "select.by_type" : "select.by_color") },
      h("span", { class: "mk-qgroup-label" }, t(kind === "type" ? "select.by_type" : "select.by_color")),
      keys.map((key) => {
        const names = groups.get(key);
        const on = names.filter((n) => sel.on.has(n)).length;
        const all = on === names.length;
        return h(
          "button",
          {
            type: "button",
            class: cx("mk-qchip", all && "is-all", on > 0 && !all && "is-some"),
            "aria-pressed": all ? "true" : on ? "mixed" : "false",
            title: t(all ? "select.group_clear" : "select.group_pick", { label: label(key), n: names.length }),
            onClick: () => setMany(names, !all),
          },
          all ? icon("check", { size: 12, strokeWidth: 2.6 }) : null,
          h("span", null, label(key)),
          h("span", { class: "mk-qchip-count num" }, `${on}/${names.length}`),
        );
      }),
    );
  }

  function setMany(names, on) {
    if (!sel) return;
    for (const n of names) {
      if (on) sel.on.add(n);
      else sel.on.delete(n);
    }
    renderGrid();
  }

  function enterSelect() {
    if (!data) return;
    sel = { order: savedOrder(), on: new Set(data.items.filter((it) => it.enabled).map((it) => it.name)), saving: false };
    renderGrid();
    usageHost.scrollIntoView({ block: "nearest" });
  }

  function leaveSelect() {
    sel = null;
    ctx.setDirty(false);
    renderGrid();
  }

  async function saveSelect() {
    if (!sel || sel.saving) return;
    sel.saving = true;
    renderUsage();
    try {
      const res = await ctx.api.post("/api/mockups/arrange", { order: sel.order, enabled: [...sel.on] }, { signal: ctx.signal });
      setData(res);
      sel = null;
      ctx.setDirty(false);
      renderGrid();
      const n = res.usage ? res.usage.used.length : 0;
      ctx.toast({ tone: "success", title: t("select.saved"), message: t("select.saved_msg", { n }), timeout: 3500 });
    } catch (err) {
      if (ctx.api.isAbort(err)) return;
      if (sel) sel.saving = false;
      renderUsage();
      ctx.toast({ tone: "danger", title: t("select.save_failed"), message: ctx.api.errorText(err, t) });
    }
  }

  // ---- notes above the grid

  // A mockup still on the default area says so on its own card (an amber "Baskı alanını
  // ayarla"), so the grid needs no banner for it (FIXLIST 8's cue lives on the card).
  function renderNotes() {
    const list = [];
    if (data && data.positions_error) {
      list.push(infoNote({ tone: "warning", icon: "alert", text: [t("positions_error"), " ", h("span", { class: "mono muted" }, data.positions_error)] }));
    }
    mount(notes, list);
    notes.hidden = !list.length;
  }

  function typeOrder() {
    return (data && data.type_order) || TYPES;
  }

  function renderChips() {
    const counts = new Map();
    for (const it of data.items) counts.set(it.type, (counts.get(it.type) || 0) + 1);
    const items = [{ id: "all", label: t("all"), count: data.items.length }];
    for (const ty of typeOrder()) if (counts.get(ty)) items.push({ id: ty, label: typeLabel(t, ty), count: counts.get(ty) });
    if (filter !== "all" && !counts.get(filter)) filter = "all";
    chipsCtl.update(items, filter);
  }

  function visibleItems() {
    if (!data) return [];
    return order()
      .map((n) => byName.get(n))
      .filter((it) => it && (filter === "all" || it.type === filter));
  }

  // ---- cards

  function thumbImg(item) {
    const key = `${item.name}|${item.version}`;
    let img = images.get(key);
    if (!img) {
      img = h("img", { src: thumbUrl(ctx, item, 600), alt: "", loading: "lazy", decoding: "async", draggable: "false" });
      img.addEventListener("error", () => img.replaceWith(h("span", { class: "mk-img-fallback" }, icon("image", { size: 26 }))), { once: true });
      images.set(key, img);
    }
    return img;
  }

  /**
   * The pill over a card's photo. Outside selection mode the photo stays clean, as in the
   * video (the grid order is the listing order); only a mockup that is off or over the
   * limit says so, so nothing is ever dropped silently (FIXLIST 9).
   */
  function positionMark(item, p) {
    if (!sel && isOn(item.name) && !p.over.has(item.name)) return null;
    const n = p.pos.get(item.name);
    if (n === 1) return h("span", { class: "mk-pos is-main", title: t("badge.main_hint") }, icon("star", { size: 11, strokeWidth: 2.2 }), h("span", null, t("badge.main")));
    if (n) return h("span", { class: "mk-pos num", title: t("badge.position", { n }), "aria-label": t("badge.position", { n }) }, String(n));
    if (p.over.has(item.name)) return h("span", { class: "mk-pos is-over", title: t("badge.over_hint", { max: max() }) }, icon("alert", { size: 11, strokeWidth: 2.2 }), h("span", null, t("badge.over")));
    return h("span", { class: "mk-pos is-off" }, icon("eye-off", { size: 11 }), h("span", null, t("unused")));
  }

  function cardFor(item, p) {
    const label = itemLabel(t, item);
    const on = isOn(item.name);
    const over = p.over.has(item.name);
    const more = iconButton({
      icon: "more",
      title: t("menu.more", { name: label }),
      variant: "ghost",
      size: "sm",
      class: "mk-more",
      onClick: (e) => {
        e.preventDefault();
        e.stopPropagation();
        openMenu(more, item);
      },
    });
    // FIXLIST 8's visible action, only where it is needed: a mockup still on the default
    // area. A card whose area is set ends at its meta line, like the video's; the whole
    // card and "⋯ > Baskı alanını düzenle" still open the editor.
    const setArea =
      item.area_source === "default"
        ? h("a", { class: "mk-set-area is-default", href: editorPath(item.name), title: t("area.default_hint") }, icon("crop", { size: 14 }), h("span", null, t("card.set_area")))
        : null;
    const mark = positionMark(item, p);
    let check = null;
    if (sel) {
      check = checkbox({ checked: on, ariaLabel: t("select.use", { label }), onChange: (v) => setMany([item.name], v) });
      check.classList.add("mk-card-check");
    }
    return h(
      "div",
      {
        class: cx("mk-card", !on && "is-disabled", over && "is-over", sel && "is-selecting", sel && on && "is-selected"),
        role: "listitem",
        title: item.name,
        dataset: { name: item.name },
      },
      h("div", { class: "mk-card-media" }, thumbImg(item), mark ? h("span", { class: "mk-card-pos" }, mark) : null, check),
      h(
        "div",
        { class: "mk-card-foot" },
        h("div", { class: "mk-card-row" }, sel ? h("span", { class: "mk-card-title ellipsis" }, label) : h("a", { class: "mk-card-link ellipsis", href: editorPath(item.name) }, label), more),
        h(
          "p",
          { class: "mk-card-meta" },
          h("span", { class: "mono" }, sizeText(item)),
          h("span", { class: "mk-sep", "aria-hidden": "true" }, "·"),
          areaState(t, item),
        ),
        setArea,
      ),
    );
  }

  function uploadingCard(file) {
    const bar = progressBar({ value: 0, max: 1, size: "sm", label: file.name });
    const node = h(
      "div",
      { class: "mk-card is-uploading", role: "listitem" },
      h("div", { class: "mk-card-media" }, spinner({ size: 22, tone: "accent" })),
      h(
        "div",
        { class: "mk-card-foot" },
        h("div", { class: "mk-card-row" }, h("span", { class: "mk-card-title ellipsis" }, file.name)),
        h("p", { class: "mk-card-meta" }, h("span", null, t("uploading"))),
        bar.el,
      ),
    );
    return { el: node, bar };
  }

  function renderGrid() {
    if (!data) return;
    if (!data.items.length && !uploads.size) {
      if (sel) leaveSelect();
      renderEmpty();
      return;
    }
    toolbar.hidden = false;
    renderChips();
    renderUsage();
    renderNotes();
    el.classList.toggle("is-selecting", !!sel);
    const p = current();
    const m = max();
    const cards = [];
    for (const it of visibleItems()) {
      cards.push(cardFor(it, p));
      // Everything switched on after this card is over the limit: say so in the grid.
      if (p.over.size && p.pos.get(it.name) === m && filter === "all") {
        cards.push(h("div", { class: "mk-grid-full mk-limit-line", role: "separator" }, h("span", null, icon("alert", { size: 13 }), t("limit.line", { max: m, n: p.over.size }))));
      }
    }
    for (const u of uploads.values()) cards.push(u.el);
    if (!cards.length) cards.push(h("div", { class: "mk-grid-full" }, emptyState({ icon: "filter", title: t("filter_empty"), compact: true })));
    newTile = data.items.length > 0 && filter === "all" && !sel ? newMockupTile() : null;
    if (newTile) cards.push(newTile);
    mount(grid, cards);
    spanNewTile();
  }

  // ---- the "Yeni mockup" tile that ends the grid (two columns wide, like the video's)

  let newTile = null;
  let gridCols = 0;

  function newMockupTile() {
    const [before, after = ""] = t("grid_new.hint").split("{size}");
    const zone = dropzone({
      class: "mk-grid-new",
      accept: ACCEPT,
      multiple: true,
      title: t("library.new"),
      subtitle: t("grid_new.sub"),
      onFiles: (list) => queueUpload(list.map((x) => x.file)),
      onReject: (bad) => ctx.toast({ tone: "warning", title: t("upload.skipped", { n: bad.length }) }),
      content: [
        h("span", { class: "mk-grid-new-icon", "aria-hidden": "true" }, icon("plus", { size: 26, strokeWidth: 2.2 })),
        h("p", { class: "mk-grid-new-title" }, t("library.new")),
        h("p", { class: "mk-grid-new-sub" }, t("grid_new.sub")),
        h("p", { class: "mk-grid-new-hint" }, icon("info", { size: 13 }), h("span", null, before, h("span", { class: "mono" }, "2000×2000"), after)),
      ],
    });
    return h("div", { class: "mk-grid-new-cell", role: "listitem" }, zone);
  }

  /** Two columns wide, or one when only one is left in the last row. */
  function spanNewTile() {
    if (!newTile || !newTile.isConnected) return;
    const cols = String(getComputedStyle(grid).gridTemplateColumns || "")
      .split(" ")
      .filter(Boolean).length;
    gridCols = cols;
    if (!cols) return;
    let pos = 0;
    for (const cell of grid.children) {
      if (cell === newTile) break;
      pos = cell.classList.contains("mk-grid-full") ? 0 : (pos + 1) % cols;
    }
    const span = pos === 0 ? Math.min(2, cols) : Math.min(2, cols - pos);
    const want = `span ${span}`;
    if (newTile.style.gridColumn !== want) newTile.style.gridColumn = want;
  }

  // The column count changes with the window (4, 5 from 1900 px, 3, 2).
  const gridRo = new ResizeObserver(() => {
    requestAnimationFrame(() => {
      if (!newTile) return;
      const cols = String(getComputedStyle(grid).gridTemplateColumns || "").split(" ").filter(Boolean).length;
      if (cols !== gridCols) spanNewTile();
    });
  });
  gridRo.observe(grid);
  cleanups.push(() => gridRo.disconnect());

  // ---- order: menu actions, drag in selection mode

  function neighbours(name) {
    const shown = visibleItems().map((it) => it.name);
    const i = shown.indexOf(name);
    return { prev: i > 0 ? shown[i - 1] : null, next: i >= 0 && i < shown.length - 1 ? shown[i + 1] : null };
  }

  async function reorder(next, toastTitle) {
    if (sameList(next, order())) return;
    if (sel) {
      sel.order = next;
      renderGrid();
      return;
    }
    try {
      setData(await ctx.api.post("/api/mockups/arrange", { order: next }, { signal: ctx.signal }));
      renderGrid();
      ctx.toast({ tone: "success", title: toastTitle || t("order.saved"), timeout: 2500 });
    } catch (err) {
      if (!ctx.api.isAbort(err)) ctx.toast({ tone: "danger", title: t("order.failed"), message: ctx.api.errorText(err, t) });
    }
  }

  function openMenu(anchor, item) {
    const label = itemLabel(t, item);
    const { prev, next } = neighbours(item.name);
    const p = current();
    const items = [{ label: t("menu.edit_area"), icon: "crop", onClick: () => ctx.navigate(editorPath(item.name)) }];
    if (!sel) {
      items.push({
        label: t("menu.use_in_drafts"),
        icon: "layers",
        checked: !!item.enabled,
        onClick: async () => {
          if (await setEnabled(ctx, item, !item.enabled)) await load({ quiet: true, warnOver: item.enabled ? [] : [item.name] });
        },
      });
    }
    items.push(
      { divider: true },
      { label: t("menu.make_main"), icon: "star", disabled: !isOn(item.name) || p.pos.get(item.name) === 1, onClick: () => reorder(placed(order(), item.name, null), t("order.main_done", { label })) },
      { label: t("menu.move_up"), icon: "arrow-left", disabled: !prev, onClick: () => reorder(placed(order(), item.name, prev)) },
      { label: t("menu.move_down"), icon: "arrow-right", disabled: !next, onClick: () => reorder(placed(order(), item.name, next, true)) },
    );
    if (!sel) {
      items.push(
        { divider: true },
        {
          label: t("menu.edit_meta"),
          icon: "tag",
          onClick: async () => {
            if (await editMeta(ctx, item)) load({ quiet: true });
          },
        },
        { label: t("open_folder"), icon: "folder-open", onClick: () => openFolder(ctx) },
        {
          label: t("menu.delete"),
          icon: "trash",
          danger: true,
          onClick: async () => {
            if (await deleteMockup(ctx, item)) load({ quiet: true });
          },
        },
      );
    }
    const pop = menu(anchor, items, { placement: "bottom-end", width: 230 });
    // The cards carry no "Ana görsel" pill outside selection mode; the menu item says
    // what the first position means.
    const mainItem = pop && [...pop.el.querySelectorAll(".menu-item")].find((b) => b.textContent === t("menu.make_main"));
    if (mainItem) mainItem.title = t("badge.main_hint");
  }

  function scroller() {
    return el.closest(".content") || document.scrollingElement || document.documentElement;
  }

  function clearMarks() {
    for (const c of grid.querySelectorAll(".drop-before, .drop-after")) c.classList.remove("drop-before", "drop-after");
  }

  function endDrag(commit) {
    const d = drag;
    drag = null;
    window.removeEventListener("pointermove", onDragMove);
    window.removeEventListener("pointerup", onDragUp);
    window.removeEventListener("pointercancel", onDragCancel);
    if (!d) return;
    cancelAnimationFrame(d.raf || 0);
    clearMarks();
    if (d.ghost) d.ghost.remove();
    if (d.card) d.card.classList.remove("is-dragging");
    el.classList.remove("is-dragging");
    if (d.started) {
      suppressClick = true;
      setTimeout(() => {
        suppressClick = false;
      }, 0);
    }
    if (commit && d.started && d.target && d.target !== d.name) reorder(placed(order(), d.name, d.target, d.after));
  }

  function onDragMove(e) {
    if (!drag || e.pointerId !== drag.id) return;
    drag.cx = e.clientX;
    drag.cy = e.clientY;
    if (!drag.started) {
      if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) < 6) return;
      drag.started = true;
      drag.card.classList.add("is-dragging");
      el.classList.add("is-dragging");
      const it = byName.get(drag.name);
      drag.ghost = h("div", { class: "mk-ghost", "aria-hidden": "true" }, it ? h("img", { src: thumbUrl(ctx, it, 160), alt: "" }) : null, h("span", null, it ? itemLabel(t, it) : drag.name));
      document.body.appendChild(drag.ghost);
      const tick = () => {
        if (!drag) return;
        const box = scroller().getBoundingClientRect();
        const edge = 70;
        const top = Math.max(box.top, 0);
        const bottom = Math.min(box.bottom, window.innerHeight);
        let dy = 0;
        if (drag.cy < top + edge) dy = -Math.ceil((top + edge - drag.cy) / 5);
        else if (drag.cy > bottom - edge) dy = Math.ceil((drag.cy - (bottom - edge)) / 5);
        if (dy) {
          scroller().scrollTop += dy;
          track();
        }
        drag.raf = requestAnimationFrame(tick);
      };
      drag.raf = requestAnimationFrame(tick);
    }
    e.preventDefault();
    track();
  }

  function track() {
    if (!drag || !drag.started) return;
    drag.ghost.style.transform = `translate(${drag.cx + 14}px, ${drag.cy + 12}px)`;
    const under = document.elementFromPoint(drag.cx, drag.cy);
    const card = under && under.closest ? under.closest(".mk-card[data-name]") : null;
    clearMarks();
    drag.target = null;
    if (!card || !grid.contains(card) || card.dataset.name === drag.name) return;
    const r = card.getBoundingClientRect();
    drag.after = drag.cx > r.left + r.width / 2;
    drag.target = card.dataset.name;
    card.classList.add(drag.after ? "drop-after" : "drop-before");
  }

  function onDragUp(e) {
    if (drag && e.pointerId === drag.id) endDrag(true);
  }

  function onDragCancel(e) {
    if (drag && e.pointerId === drag.id) endDrag(false);
  }

  grid.addEventListener("pointerdown", (e) => {
    if (!sel || e.button !== 0 || drag) return;
    const card = e.target.closest(".mk-card[data-name]");
    if (!card || e.target.closest("a, button, input, label")) return;
    drag = { name: card.dataset.name, card, id: e.pointerId, x: e.clientX, y: e.clientY, cx: e.clientX, cy: e.clientY, started: false, target: null, after: false };
    window.addEventListener("pointermove", onDragMove);
    window.addEventListener("pointerup", onDragUp);
    window.addEventListener("pointercancel", onDragCancel);
  });
  grid.addEventListener("click", (e) => {
    if (!sel) return;
    if (suppressClick) {
      e.preventDefault();
      return;
    }
    const card = e.target.closest(".mk-card[data-name]");
    if (!card || e.target.closest("a, button, input, label")) return;
    const name = card.dataset.name;
    setMany([name], !sel.on.has(name));
  });
  grid.addEventListener("dragstart", (e) => {
    if (sel) e.preventDefault();
  });
  cleanups.push(() => endDrag(false));

  // ---- data

  function setData(next) {
    data = next;
    byName = new Map(data.items.map((it) => [it.name, it]));
    for (const key of [...images.keys()]) {
      const [name, version] = key.split("|");
      const it = byName.get(name);
      if (!it || it.version !== version) images.delete(key);
    }
    if (sel) {
      // New uploads join the draft at the end; deleted mockups leave it.
      const known = new Set(data.items.map((it) => it.name));
      sel.order = sel.order.filter((n) => known.has(n));
      for (const it of data.items) {
        if (!sel.order.includes(it.name)) {
          sel.order.push(it.name);
          if (it.enabled) sel.on.add(it.name);
        }
      }
      for (const n of [...sel.on]) if (!known.has(n)) sel.on.delete(n);
    }
  }

  function addItem(item) {
    if (!data || !item) return;
    const items = data.items.filter((it) => it.name !== item.name);
    items.push(item);
    setData({ ...data, items });
  }

  function queueUpload(files) {
    const list = (files || []).filter((f) => f && IMAGE_RE.test(f.name || ""));
    for (const file of list) uploads.set(file, uploadingCard(file));
    if (list.length) renderGrid();
    chain = chain
      .then(() =>
        uploadBatch(ctx, files, {
          onProgress: (file, fraction) => {
            const u = uploads.get(file);
            if (u) u.bar.update(fraction);
          },
          onDone: (file, item) => {
            uploads.delete(file);
            addItem(item);
            renderGrid();
          },
          onFail: (file) => {
            uploads.delete(file);
            renderGrid();
          },
        }),
      )
      .then((res) => {
        if (ctx.isActive()) return load({ quiet: true, warnOver: res.added.filter(Boolean).map((it) => it.name) });
        return null;
      })
      .catch((err) => {
        if (!ctx.api.isAbort(err)) console.error("[mockups] upload failed", err);
      });
    return chain;
  }

  async function pickAndUpload() {
    const files = await pickFiles({ multiple: true });
    if (files.length && ctx.isActive()) queueUpload(files);
  }

  async function load({ quiet = false, warnOver = [] } = {}) {
    if (!quiet || !data) renderSkeleton();
    try {
      setData(await ctx.api.get("/api/mockups", null, { signal: ctx.signal }));
    } catch (err) {
      if (ctx.api.isAbort(err)) return;
      if (quiet && data) {
        ctx.toast({ tone: "danger", title: t("load_error"), message: ctx.api.errorText(err, t) });
        return;
      }
      renderError(err);
      return;
    }
    renderGrid();
    // The mockups a preview can use follow the grid's (switched on, in order).
    wmCtl.el.hidden = false;
    wmCtl.load({ quiet: true });
    // Never silently: a mockup that was just switched on or added but does not fit says so.
    const over = warnOver.map((n) => byName.get(n)).filter((it) => it && it.over_limit);
    if (over.length === 1) ctx.toast({ tone: "warning", title: t("enabled.over_title"), message: t("enabled.over", { max: max(), label: itemLabel(t, over[0]) }), timeout: 8000 });
    else if (over.length) ctx.toast({ tone: "warning", title: t("enabled.over_title_many", { n: over.length }), message: t("enabled.over_many", { max: max() }), timeout: 8000 });
  }

  await load();
  return () => {
    for (const fn of cleanups) {
      try {
        fn();
      } catch {
        /* ignore */
      }
    }
  };
}

// ------------------------------------------------------------------ Filigran (watermark)
//
// The seller's own mark (a logo, the shop name). Tasarım Yükle and `drop run/auto` stamp
// it on a copy of every listing PHOTO (the composited mockups, the flat image, a product
// folder's own photos), never on the files buyers download. The picture and its
// settings live at the workspace root (drop/watermark.py): watermark.png, watermark.json.

const WM_ACCEPT = ".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp";
const WM_RE = /\.(png|jpe?g|webp)$/i;
const WM_FLAT = "flat";
const WM_PREVIEW_MAX = 1000;
const WM_POSITIONS = ["center", "corner", "tiled"];
const WM_SCOPES = ["digital", "all"];

/** A position tile's picture: a photo frame with the mark where it goes. */
function wmPositionArt(pos) {
  const marks = [];
  if (pos === "center") marks.push(svg("rect", { x: 13, y: 13, width: 18, height: 6, rx: 1.6 }));
  else if (pos === "corner") marks.push(svg("rect", { x: 27, y: 22.5, width: 12, height: 5, rx: 1.4 }));
  else {
    const spots = [[3, 8], [19, 4], [35, 0], [11, 17], [27, 13], [3, 26], [19, 22], [35, 18]];
    for (const [x, y] of spots) {
      marks.push(svg("rect", { x, y, width: 9, height: 3.4, rx: 1, transform: `rotate(-30 ${x + 4.5} ${y + 1.7})` }));
    }
  }
  return svg(
    "svg",
    { class: "mk-wm-pos-art", viewBox: "0 0 44 32", "aria-hidden": "true" },
    svg("rect", { class: "mk-wm-pos-frame", x: 0.75, y: 0.75, width: 42.5, height: 30.5, rx: 4 }),
    svg("g", { class: "mk-wm-pos-marks" }, marks),
  );
}

/**
 * The Filigran card. -> {el, load({quiet}), flush()}. Changes save on their own (PATCH
 * /api/watermark, a moment after the last one); the preview is drawn by the server on a
 * real mockup with the unsaved values, the same way the drafts get it.
 */
function watermarkSection(ctx, { labelFor }) {
  const t = ctx.t;
  const titleId = uid("wm");
  const el = h("section", { class: "card mk-wm", id: "filigran", "aria-labelledby": titleId, dataset: { drop: t("wm.drop") } });
  let wm = null; // GET /api/watermark
  let draft = null; // the settings on screen (saved or about to be)
  let pending = {}; // fields changed on screen and not saved yet
  let saveTimer = null;
  let saving = null;
  let busy = false; // an upload, a removal or a background removal is running
  let target = null; // the mockup the preview shows (or WM_FLAT)
  let loadSeq = 0;
  let previewSeq = 0;
  let depth = 0;
  let scrolled = false;
  const refs = {};

  const limits = () => (wm && wm.limits) || { opacity: [10, 90], size: [5, 60], tile_size: [5, 40], max_mb: 10 };
  const sizeKey = () => (draft && draft.position === "tiled" ? "tile_size" : "size");
  const pct = (v) => percent(v / 100);

  // ---- data

  async function load({ quiet = false } = {}) {
    if (quiet && busy) return; // the upload or removal redraws the card itself
    const seq = ++loadSeq;
    if (!wm) renderSkeleton();
    let next;
    try {
      next = await ctx.api.get("/api/watermark", null, { signal: ctx.signal });
    } catch (err) {
      if (ctx.api.isAbort(err) || seq !== loadSeq) return;
      if (!wm) {
        mount(el, infoNote({ tone: "danger", icon: "alert", text: [h("strong", null, t("wm.load_error")), " ", ctx.api.errorText(err, t)] }));
      }
      return;
    }
    if (seq !== loadSeq) return;
    const sameFile = wm && wm.file && next.file && wm.file.version === next.file.version;
    const settle = quiet && sameFile && refs.side && refs.side.isConnected;
    wm = next;
    if (settle) {
      // Only what the grid can change: the mockups the preview can use, and whether the
      // next run stamps. The sliders keep their place (and focus).
      if (!Object.keys(pending).length) draft = { ...wm.settings };
      renderTargets();
      renderApplies();
      return;
    }
    draft = { ...wm.settings, ...pending };
    render();
    if (!scrolled && location.hash === "#filigran") {
      scrolled = true;
      requestAnimationFrame(() => el.scrollIntoView({ block: "start", behavior: "smooth" }));
    }
  }

  /** A new state from the server, drawn at once. `where` ("drop", "replace", "title"):
   *  where the keyboard goes when it was in the card, whose nodes are all rebuilt. */
  function take(res, where = null) {
    const inCard = el.contains(document.activeElement);
    wm = res;
    pending = {};
    draft = { ...wm.settings };
    render();
    if (where && inCard && !el.contains(document.activeElement)) refocus(where);
  }

  function refocus(where) {
    const node = where === "drop" ? refs.drop : where === "replace" ? refs.replace : refs.title;
    const target = node && node.isConnected ? node : refs.title;
    if (target) target.focus({ preventScroll: true });
  }

  function queueSave(fields, { now = false } = {}) {
    Object.assign(draft, fields);
    Object.assign(pending, fields);
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => save(), now ? 0 : 550);
    renderApplies();
    renderState();
  }

  async function save() {
    clearTimeout(saveTimer);
    if (!Object.keys(pending).length) return;
    if (saving) {
      // One PATCH at a time; the next one goes when this one is answered.
      saveTimer = setTimeout(() => save(), 120);
      return;
    }
    const body = pending;
    pending = {};
    saving = ctx.api.patch("/api/watermark", body);
    try {
      const res = await saving;
      wm = res;
      // What changed on screen meanwhile stays on screen (it is queued).
      draft = { ...wm.settings, ...pending };
      renderApplies();
      renderState();
    } catch (err) {
      if (ctx.api.isAbort(err)) return;
      ctx.toast({ tone: "danger", title: t("wm.save_failed"), message: ctx.api.errorText(err, t) });
      pending = {};
      if (ctx.isActive()) await load();
    } finally {
      saving = null;
    }
  }

  /** The tab is closing (a reload, the window shut): what still waits goes as a keepalive
   *  request, which the browser completes after the page is gone. */
  function flushOnExit() {
    clearTimeout(saveTimer);
    if (!Object.keys(pending).length) return;
    const body = pending;
    pending = {};
    try {
      fetch("/api/watermark", {
        method: "PATCH",
        keepalive: true,
        credentials: "same-origin",
        headers: { "X-Etsyotomasyon": "1", "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).catch(() => {});
    } catch {
      /* the page is closing: nothing more to do */
    }
  }

  /** What is still waiting goes now (not tied to the page's signal: it also runs when the
   *  page is left). Resolves when it is saved. */
  function flush() {
    clearTimeout(saveTimer);
    const before = (saving || Promise.resolve()).catch(() => null);
    if (!Object.keys(pending).length) return before;
    const body = pending;
    pending = {};
    // After the PATCH already on its way, so an older value never lands last.
    return before.then(() => ctx.api.patch("/api/watermark", body)).catch(() => null);
  }

  async function uploadMark(file) {
    if (!file || busy) return;
    if (!WM_RE.test(file.name || "")) {
      ctx.toast({ tone: "danger", title: t("wm.upload_failed"), message: t("errors.watermark_type", { name: file.name || "" }) });
      return;
    }
    const maxMb = limits().max_mb || 10;
    if (file.size > maxMb * 1024 * 1024) {
      ctx.toast({ tone: "danger", title: t("wm.upload_failed"), message: t("errors.watermark_too_large", { name: file.name, max_mb: maxMb }) });
      return;
    }
    const had = !!(wm && wm.file);
    busy = true;
    el.classList.add("is-busy");
    renderBusy(t("wm.uploading"));
    try {
      await flush();
      const res = await ctx.api.upload("/api/watermark/file", file, { query: { name: file.name }, signal: ctx.signal });
      take(res, "replace");
      ctx.toast({ tone: "success", title: t(had ? "wm.replaced" : "wm.uploaded"), message: res.file ? res.file.name : file.name, timeout: 3500 });
    } catch (err) {
      if (ctx.api.isAbort(err)) return;
      ctx.toast({ tone: "danger", title: t("wm.upload_failed"), message: ctx.api.errorText(err, t), timeout: 9000 });
      render();
    } finally {
      busy = false;
      el.classList.remove("is-busy");
    }
  }

  async function pickMark() {
    const input = h("input", { type: "file", accept: WM_ACCEPT, class: "sr-only", tabindex: "-1", "aria-hidden": "true" });
    const file = await new Promise((resolve) => {
      const done = (f) => {
        input.remove();
        resolve(f);
      };
      input.addEventListener("change", () => done((input.files || [])[0] || null), { once: true });
      input.addEventListener("cancel", () => done(null), { once: true });
      document.body.appendChild(input);
      input.click();
    });
    if (file && ctx.isActive()) await uploadMark(file);
  }

  async function removeMark() {
    if (busy || !wm || !wm.file) return;
    const ok = await ctx.confirm({
      title: t("wm.remove_title"),
      message: t("wm.remove_msg", { name: wm.file.name }),
      confirmLabel: t("wm.remove"),
      danger: true,
    });
    if (!ok) return;
    busy = true;
    try {
      await flush();
      take(await ctx.api.del("/api/watermark/file", null, { signal: ctx.signal }), "drop");
      ctx.toast({ tone: "success", title: t("wm.removed"), timeout: 3000 });
    } catch (err) {
      if (!ctx.api.isAbort(err)) ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
    } finally {
      busy = false;
    }
  }

  async function clearGround(btn) {
    if (busy) return;
    busy = true;
    if (btn) btn.setLoading(true);
    try {
      take(await ctx.api.post("/api/watermark/remove-ground", null, { signal: ctx.signal }), "title");
      ctx.toast({ tone: "success", title: t("wm.ground_done"), timeout: 3000 });
    } catch (err) {
      if (btn) btn.setLoading(false);
      if (!ctx.api.isAbort(err)) ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
    } finally {
      busy = false;
    }
  }

  // ---- drag and drop onto the card: the dropped picture becomes the watermark

  const hasFiles = (e) => !!e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files");
  // The empty card's drop area takes its own files (ui.dropzone).
  const inDropzone = (e) => !!(e.target && e.target.closest && e.target.closest(".dropzone"));
  // The page's own drop overlay leaves this card alone (installPageDrop), so a file
  // dropped here is always taken here, never opened by the browser.
  el.addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth += 1;
    if (wm && wm.file) el.classList.add("is-over");
  });
  el.addEventListener("dragover", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = wm ? "copy" : "none";
  });
  el.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (!depth) el.classList.remove("is-over");
  });
  el.addEventListener("drop", (e) => {
    depth = 0;
    el.classList.remove("is-over");
    if (!hasFiles(e) || inDropzone(e)) return;
    e.preventDefault();
    const file = [...(e.dataTransfer.files || [])][0];
    if (file && wm) uploadMark(file);
  });

  // ---- drawing

  function renderSkeleton() {
    mount(
      el,
      h("div", { class: "mk-wm-head" }, h("span", { class: "icon-tile" }, icon("droplet", { size: 16 })), h("div", { class: "mk-wm-titles" }, h("h2", { class: "mk-wm-title", id: titleId }, t("wm.title")), skeleton({ lines: 1, height: 10, widths: ["280px"] }))),
    );
  }

  function renderBusy(text) {
    if (refs.stageWait) {
      refs.stageWait.hidden = false;
      refs.stageWait.lastChild.textContent = text;
    } else if (refs.drop) {
      refs.drop.classList.add("is-reading");
    }
  }

  function head() {
    const file = wm && wm.file;
    refs.state = h("span", { class: "mk-wm-state" });
    refs.toggle = file
      ? toggle({
          checked: !!draft.enabled,
          ariaLabel: t("wm.toggle"),
          onChange: (on) => {
            queueSave({ enabled: on }, { now: true });
            if (refs.stage) refs.stage.classList.toggle("is-off", !on);
          },
        })
      : null;
    return h(
      "header",
      { class: "mk-wm-head" },
      h("span", { class: "icon-tile" }, icon("droplet", { size: 16 })),
      h("div", { class: "mk-wm-titles" }, (refs.title = h("h2", { class: "mk-wm-title", id: titleId, tabindex: "-1" }, t("wm.title"))), h("p", { class: "mk-wm-sub" }, t("wm.sub"))),
      file ? h("div", { class: "mk-wm-switch" }, refs.state, refs.toggle) : null,
    );
  }

  function renderState() {
    if (!refs.state || !wm || !wm.file) return;
    const on = !!draft.enabled;
    mount(refs.state, h("span", { class: cx("mk-wm-dot", on && "is-on"), "aria-hidden": "true" }), t(on ? "wm.state_on" : "wm.state_off"));
    refs.state.classList.toggle("is-on", on);
    if (refs.toggle && refs.toggle.checked !== on) refs.toggle.update(on);
  }

  function render() {
    for (const k of Object.keys(refs)) delete refs[k];
    if (!wm) return;
    const body = wm.file ? fullBody() : emptyBody();
    mount(el, head(), body);
    el.classList.toggle("is-empty", !wm.file);
    renderState();
    renderApplies();
    if (wm.file) drawPreview();
  }

  function emptyBody() {
    const lim = limits();
    refs.drop = dropzone({
      accept: WM_ACCEPT,
      multiple: false,
      class: "mk-wm-drop",
      title: t("wm.empty_title"),
      onFiles: (list) => uploadMark(list[0] && list[0].file),
      onReject: (bad) => ctx.toast({ tone: "danger", title: t("wm.upload_failed"), message: t("errors.watermark_type", { name: (bad[0] && bad[0].file.name) || "" }) }),
      content: [
        h("span", { class: "mk-wm-drop-icon" }, icon("upload", { size: 22 })),
        h("p", { class: "mk-wm-drop-title" }, t("wm.empty_title")),
        h("p", { class: "mk-wm-drop-sub" }, t("wm.empty_sub")),
        h("p", { class: "mk-wm-drop-types" }, t("wm.empty_types", { mb: lim.max_mb || 10 })),
      ],
    });
    const fact = (ic, text) => h("li", { class: "mk-wm-fact" }, h("span", { class: "mk-wm-fact-icon" }, icon(ic, { size: 15 })), h("span", null, text));
    return h(
      "div",
      { class: "mk-wm-body is-empty" },
      refs.drop,
      h(
        "div",
        { class: "mk-wm-about" },
        h("p", { class: "mk-wm-about-title" }, t("wm.about_title")),
        h("ul", { class: "mk-wm-facts" }, fact("image", t("wm.fact_photos")), fact("download", t("wm.fact_downloads")), fact("layers", t("wm.fact_settings"))),
      ),
    );
  }

  function fullBody() {
    const file = wm.file;
    // The preview: a real mockup (the first one the drafts use), or the flat image.
    refs.img = h("img", { class: "mk-wm-img", alt: t("wm.preview_alt"), decoding: "async", draggable: "false" });
    refs.stageWait = h("span", { class: "mk-wm-wait" }, spinner({ size: 18, tone: "accent" }), h("span", null, t("wm.preview_drawing")));
    refs.stageWait.hidden = true;
    refs.stageErr = h("span", { class: "mk-wm-stage-err" }, icon("alert", { size: 14 }), h("span", null, t("wm.preview_failed")));
    refs.stageErr.hidden = true;
    refs.stage = h("div", { class: cx("mk-wm-stage", !draft.enabled && "is-off") }, refs.img, refs.stageWait, refs.stageErr);
    refs.targets = h("div", { class: "mk-wm-target" });
    renderTargets();
    const previewCol = h("div", { class: "mk-wm-preview" }, refs.stage, h("div", { class: "mk-wm-under" }, refs.targets, h("span", { class: "mk-wm-under-note" }, icon("check", { size: 13 }), t("wm.preview_note"))));

    // The picture itself.
    const size = file.width && file.height ? `${file.width}×${file.height}` : "";
    const ground = file.readable === false ? "unreadable" : file.ground || "";
    const meta = [
      size ? h("span", { class: "mono" }, size) : null,
      ground && ground !== "unreadable" ? h("span", { class: cx("mk-wm-ground", `is-${ground}`) }, t(`wm.ground.${ground}`)) : null,
    ].filter(Boolean);
    const fileRow = h(
      "div",
      { class: "mk-wm-file" },
      h("span", { class: "mk-wm-file-thumb checker" }, file.readable === false ? icon("alert", { size: 18 }) : h("img", { src: ctx.api.url("/api/watermark/image", { v: file.version }), alt: "" })),
      h(
        "div",
        { class: "mk-wm-file-text" },
        h("span", { class: "mk-wm-file-name ellipsis", title: file.name }, file.name),
        h("span", { class: "mk-wm-file-meta" }, meta),
      ),
      (refs.replace = button({ label: t("wm.replace"), size: "sm", variant: "secondary", onClick: () => pickMark() })),
      iconButton({ icon: "trash", title: t("wm.remove"), variant: "ghost", size: "sm", class: "mk-wm-remove", onClick: () => removeMark() }),
    );
    let groundNote = null;
    if (ground === "unreadable") {
      groundNote = infoNote({ tone: "danger", icon: "alert", text: t("wm.unreadable") });
    } else if (ground === "flat") {
      const fix = button({ label: t("wm.ground_action"), size: "sm", variant: "secondary", onClick: () => clearGround(fix) });
      groundNote = infoNote({ tone: "info", icon: "image", text: t("wm.ground_flat_note"), action: fix });
    } else if (ground === "photo") {
      groundNote = infoNote({ tone: "warning", icon: "alert", text: t("wm.ground_photo_note") });
    }

    // Where it goes (scope), where on the photo (position), how strong and how big. Each
    // group is one Tab stop (the chosen option); the arrow keys move and choose.
    const stop = (group, ids) => (ids.includes(draft[group]) ? draft[group] : ids[0]);
    const option = (group, id, title, sub) =>
      h(
        "button",
        {
          type: "button",
          role: "radio",
          class: cx("mk-wm-opt", draft[group] === id && "is-selected"),
          "aria-checked": draft[group] === id ? "true" : "false",
          tabindex: stop(group, group === "scope" ? WM_SCOPES : WM_POSITIONS) === id ? "0" : "-1",
          dataset: { group, id },
          onClick: () => choose(group, id),
        },
        group === "position" ? wmPositionArt(id) : h("span", { class: "mk-wm-radio", "aria-hidden": "true" }),
        h("span", { class: "mk-wm-opt-text" }, h("strong", null, title), sub ? h("span", null, sub) : null),
      );
    refs.scope = h(
      "div",
      { class: "mk-wm-opts mk-wm-scope", role: "radiogroup", "aria-label": t("wm.scope") },
      WM_SCOPES.map((id) => option("scope", id, t(`wm.scope.${id}`), t(`wm.scope.${id}_sub`))),
    );
    refs.positions = h(
      "div",
      { class: "mk-wm-opts mk-wm-positions", role: "radiogroup", "aria-label": t("wm.position") },
      WM_POSITIONS.map((id) => option("position", id, t(`wm.position.${id}`), null)),
    );
    radioKeys(refs.scope, "scope");
    radioKeys(refs.positions, "position");
    refs.opacity = slider("opacity", t("wm.opacity"));
    refs.size = slider(sizeKey(), t("wm.size"));
    refs.applies = h("div", { class: "mk-wm-applies" });
    const side = h(
      "div",
      { class: "mk-wm-side" },
      fileRow,
      groundNote,
      h("div", { class: "mk-wm-group" }, h("p", { class: "mk-wm-label" }, t("wm.scope")), refs.scope),
      h("div", { class: "mk-wm-group" }, h("p", { class: "mk-wm-label" }, t("wm.position")), refs.positions),
      h("div", { class: "mk-wm-sliders" }, refs.opacity.el, refs.size.el),
      refs.applies,
    );
    refs.side = side;
    return h("div", { class: "mk-wm-body" }, previewCol, side);
  }

  /** A labelled range: -> {el, key, set(value)}. */
  function slider(key, label) {
    const [lo, hi] = limits()[key] || [0, 100];
    const value = h("span", { class: "mk-wm-slider-value num" });
    const hint = h("span", { class: "mk-wm-slider-hint" });
    const input = h("input", { type: "range", class: "mk-wm-range", min: String(lo), max: String(hi), step: "1", "aria-label": label });
    const ctl = { key, el: null, input };
    const paint = () => {
      const v = Number(input.value);
      value.textContent = pct(v);
      input.style.setProperty("--fill", `${((v - lo) / (hi - lo || 1)) * 100}%`);
      input.setAttribute("aria-valuetext", pct(v));
    };
    ctl.set = (v, k = ctl.key) => {
      ctl.key = k;
      const [l, u] = limits()[k] || [lo, hi];
      input.min = String(l);
      input.max = String(u);
      input.value = String(v);
      if (k !== "opacity") hint.textContent = t(k === "tile_size" ? "wm.size_hint_tiled" : "wm.size_hint");
      paint();
    };
    input.addEventListener("input", () => {
      paint();
      draft[ctl.key] = Number(input.value);
      schedulePreview();
    });
    // "change" comes once, at the end of a drag (or a key press): saved at once, so a
    // reload right after it loses nothing. The preview follows "input" meanwhile.
    input.addEventListener("change", () => queueSave({ [ctl.key]: Number(input.value) }, { now: true }));
    ctl.el = h("label", { class: "mk-wm-slider" }, h("span", { class: "mk-wm-slider-row" }, h("span", { class: "mk-wm-label" }, label), hint, h("span", { class: "spacer" }), value), input);
    ctl.set(draft[key], key);
    return ctl;
  }

  /** The ARIA radio group's keys: arrows move to the next or previous option (round),
   *  Home and End to the ends; each move chooses it. */
  function radioKeys(box, group) {
    box.addEventListener("keydown", (e) => {
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      const opts = [...box.querySelectorAll(".mk-wm-opt")];
      const i = opts.indexOf(document.activeElement);
      if (i < 0) return;
      let j = null;
      if (e.key === "ArrowRight" || e.key === "ArrowDown") j = (i + 1) % opts.length;
      else if (e.key === "ArrowLeft" || e.key === "ArrowUp") j = (i - 1 + opts.length) % opts.length;
      else if (e.key === "Home") j = 0;
      else if (e.key === "End") j = opts.length - 1;
      if (j === null) return;
      e.preventDefault();
      choose(group, opts[j].dataset.id);
      opts[j].focus();
    });
  }

  function choose(group, id) {
    if (draft[group] === id) return;
    const box = group === "scope" ? refs.scope : refs.positions;
    for (const b of box.querySelectorAll(".mk-wm-opt")) {
      const on = b.dataset.id === id;
      b.classList.toggle("is-selected", on);
      b.setAttribute("aria-checked", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
    }
    queueSave({ [group]: id });
    if (group === "position") {
      refs.size.set(draft[sizeKey()], sizeKey());
      drawPreview();
    }
  }

  function renderTargets() {
    if (!refs.targets || !wm) return;
    const names = wm.preview_mockups || [];
    if (target !== WM_FLAT && !names.includes(target)) target = wm.preview_default || WM_FLAT;
    const options = names.map((n) => ({ value: n, label: labelFor(n) }));
    options.push({ value: WM_FLAT, label: t("wm.preview_flat") });
    mount(
      refs.targets,
      select({
        options,
        value: target,
        size: "sm",
        prefix: t("wm.preview_on"),
        onChange: (v) => {
          target = v;
          drawPreview();
        },
      }),
    );
  }

  const schedulePreview = debounce(() => drawPreview(), 160);

  function drawPreview() {
    if (!refs.img || !wm || !wm.file || wm.file.readable === false) return;
    schedulePreview.cancel();
    const seq = ++previewSeq;
    const src = ctx.api.url("/api/watermark/preview", {
      mockup: target || WM_FLAT,
      design: wm.preview_design || undefined,
      position: draft.position,
      opacity: draft.opacity,
      size: draft.size,
      tile_size: draft.tile_size,
      max: WM_PREVIEW_MAX,
      v: wm.file.version,
    });
    const slow = setTimeout(() => {
      if (seq === previewSeq && refs.stageWait) {
        refs.stageWait.lastChild.textContent = t("wm.preview_drawing");
        refs.stageWait.hidden = false;
      }
    }, 180);
    const probe = new Image();
    probe.decoding = "async";
    probe.onload = () => {
      clearTimeout(slow);
      if (seq !== previewSeq || !refs.img) return;
      refs.img.src = src;
      refs.stageWait.hidden = true;
      refs.stageErr.hidden = true;
      refs.stage.classList.add("has-image");
    };
    probe.onerror = () => {
      clearTimeout(slow);
      if (seq !== previewSeq || !refs.stageErr) return;
      refs.stageWait.hidden = true;
      refs.stageErr.hidden = false;
    };
    probe.src = src;
  }

  /** What the next Tasarım Yükle run does with it, and where it never goes. */
  function renderApplies() {
    if (!refs.applies || !wm) return;
    const type = wm.listing_type;
    const digital = type === "download" || type === "both";
    let tone = "muted";
    let text;
    if (!draft.enabled) text = t("wm.applies.off");
    else if (draft.scope === "all") {
      // Every listing's photos, whatever the template (drop/watermark.applies_to).
      tone = "on";
      text = t("wm.applies.all");
    } else if (!type) text = t("wm.applies.no_template");
    else if (digital) {
      tone = "on";
      text = t("wm.applies.digital");
    } else text = t("wm.applies.physical");
    mount(
      refs.applies,
      h("p", { class: cx("mk-wm-applies-line", `is-${tone}`) }, icon(tone === "on" ? "check-circle" : "info", { size: 14 }), h("span", null, text)),
      h("p", { class: "mk-wm-applies-line is-never" }, icon("download", { size: 14 }), h("span", null, t("wm.never_downloads"))),
    );
  }

  return { el, load, flush, flushOnExit };
}

// ------------------------------------------------------------------ editor

async function mountEditor(el, ctx, name) {
  const t = ctx.t;
  el.classList.add("mk-page-editor");

  const shopKey = () => {
    const s = ctx.session && ctx.session();
    return `${APPLIED_KEY}:${(s && s.shop_id) || ""}`;
  };
  const applied = new Set(readStored("session", shopKey(), []));
  const storeApplied = () => writeStored("session", shopKey(), [...applied]);

  let list = [];
  let item = null;
  let saved = null;
  let area = null;
  let source = "default";
  let siblings = [];
  let sameSize = false;
  let lastApplied = null;
  let zoom = 1;
  let showDesign = readStored("local", SHOW_KEY, true) !== false;
  let design = readStored("local", DESIGN_KEY, null);
  let designs = null;
  let drag = null;
  // Four corners (fractions, TL TR BR BL) when the area is a "4 köşe" one, else null;
  // `area` is then their bounding box. `look` is the realism and curve on screen;
  // `explicit` says which of them the seller set (the others follow the mockup type).
  let quad = null;
  let savedQuad = null;
  let look = { realism: 0, curve: 0 };
  let savedLook = { realism: 0, curve: 0 };
  let explicit = { realism: false, curve: false };
  let savedExplicit = { realism: false, curve: false };
  let style = { realism_default: 0, curve_default: 0, curve_offered: false };
  let previewSeq = 0;
  let saving = false;
  let chain = Promise.resolve();
  let maxEnabled = 19;
  let noteKey = "";
  const cleanups = [];
  // Declared before anything can call markStale(), which cancels it.
  const schedulePreview = debounce(() => loadPreview(), 350);

  const isDirty = () =>
    !!(saved && area && (!sameArea(saved, area) || !sameQuad(savedQuad, quad) || look.realism !== savedLook.realism || look.curve !== savedLook.curve));
  const W = () => (item && item.width) || 1;
  const H = () => (item && item.height) || 1;

  // As in the video, the header holds only "Mockup ekle"; the way back is the sidebar, the
  // library list, Vazgeç (with nothing unsaved) or the browser's Back.
  const addBtn = button({ label: t("add"), icon: "plus", variant: "primary", onClick: () => pickAndUpload() });
  ctx.setHeader({ actions: [addBtn] });

  // ---- library (left)
  const libCount = h("span", { class: "mk-lib-count num" });
  const libList = h("div", { class: "mk-lib-list", role: "list" });
  const lib = h(
    "section",
    { class: "card mk-lib", "aria-label": t("library.title") },
    h("header", { class: "mk-lib-head" }, h("h2", { class: "mk-lib-title" }, t("library.title")), libCount),
    libList,
    h(
      "button",
      { type: "button", class: "mk-lib-new", onClick: () => pickAndUpload() },
      icon("plus", { size: 15 }),
      h("span", { class: "mk-lib-new-label" }, t("library.new")),
      h("span", { class: "mk-lib-new-sub" }, `· ${t("library.new_types")}`),
    ),
  );

  // ---- canvas (centre)
  const baseImg = h("img", { class: "mk-base", alt: "", draggable: "false" });
  let previewImg = h("img", { class: "mk-preview", alt: "", draggable: "false" });
  previewImg.hidden = true;
  const overlayImg = h("img", { class: "mk-rect-design", alt: "", draggable: "false" });
  const sizeChip = h("span", { class: "mk-rect-size num" });
  // How to use the rectangle with a mouse and keys: its description (aria-label is the area).
  const rectHint = h("span", { class: "sr-only", id: uid("mk-rect-hint") }, t("editor.hint"));
  const rect = h(
    "div",
    { class: "mk-rect", tabindex: "0", role: "group", title: t("editor.hint"), "aria-describedby": rectHint.id },
    overlayImg,
    h("span", { class: "mk-rect-label" }, t("editor.area_label")),
    sizeChip,
    HANDLES.map((hd) => h("span", { class: `mk-handle h-${hd}`, dataset: { handle: hd }, "aria-hidden": "true" })),
  );
  // The four-corner area: a shape, the design mapped into it in perspective while it is
  // dragged (the server's preview replaces it after), and one handle per corner.
  const quadHint = h("span", { class: "sr-only", id: uid("mk-quad-hint") }, t("editor.quad_hint"));
  const quadPoly = svg("polygon", { class: "mk-quad-shape" });
  const quadSvg = svg("svg", { class: "mk-quad-svg", viewBox: "0 0 100 100", preserveAspectRatio: "none", "aria-hidden": "true" }, quadPoly);
  const quadDesign = h("img", { class: "mk-quad-design", alt: "", draggable: "false" });
  quadDesign.addEventListener("load", () => placeQuadDesign());
  const quadHandles = CORNERS.map((c, i) =>
    h("span", { class: `mk-qhandle q-${c}`, tabindex: "0", role: "button", dataset: { corner: String(i) }, title: t(`editor.corner_${c}`), "aria-describedby": quadHint.id }),
  );
  const quadLayer = h("div", { class: "mk-quad" }, quadDesign, quadSvg, quadHandles);
  const art = h("div", { class: "mk-art" }, baseImg, previewImg, rect, quadLayer);
  const stageSpinner = h("div", { class: "mk-stage-wait" }, spinner({ size: 22, tone: "accent" }));
  const stage = h("div", { class: "mk-stage" }, art);
  const zoomVal = h("span", { class: "num" }, percent(1, 0));
  const zoomBtn = h(
    "button",
    { type: "button", class: "mk-zoom", title: t("editor.zoom"), "aria-label": t("editor.zoom"), onClick: () => openZoomMenu() },
    icon("search", { size: 13 }),
    zoomVal,
  );
  // The pill at the bottom of the stage: "draw the print area", then "print area set".
  const underHost = h("div", { class: "mk-under", role: "status" });
  const stageWrap = h("div", { class: "mk-stage-wrap" }, stage, stageSpinner, zoomBtn, underHost, quadHint);

  // ---- side panel (right)
  const titleEl = h("h2", { class: "mk-side-title" });
  // One ⋯ menu instead of extra buttons around the video's layout: show the design, type
  // and colour, back to the default area.
  const moreBtn = iconButton({ icon: "more", title: t("editor.more"), variant: "ghost", size: "sm", class: "mk-side-more", onClick: () => openSideMenu() });
  const sizeEl = h("span", { class: "mk-size-chip num" });
  const stateHost = h("span", { class: "mk-state-host" });
  const disabledHost = h("div", { class: "mk-disabled-host" });
  const fields = {};
  const fieldEls = [];
  for (const key of ["x", "y", "w", "h"]) {
    const input = textInput({ mono: true, suffix: "0 px", ariaLabel: t(`editor.field_${key}`), inputmode: "decimal", class: "mk-field-input" });
    input.input.addEventListener("input", () => onFieldInput(key, input.input.value, false));
    input.input.addEventListener("change", () => onFieldInput(key, input.input.value, true));
    input.input.addEventListener("blur", () => renderFields(true));
    input.input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") onFieldInput(key, input.input.value, true);
      if (e.key === "ArrowUp" || e.key === "ArrowDown") {
        e.preventDefault();
        const cur = area ? area[key] * 100 : 0;
        const step = (e.shiftKey ? 1 : 0.1) * (e.key === "ArrowUp" ? 1 : -1);
        onFieldInput(key, String(Math.round((cur + step) * 10) / 10), true);
      }
    });
    fields[key] = input;
    fieldEls.push(field({ label: t(`editor.field_${key}`), input, class: "mk-field" }));
  }
  const modeTabs = tabs({
    size: "sm",
    ariaLabel: t("editor.mode_label"),
    value: "rect",
    items: [
      { id: "rect", label: t("editor.mode_rect") },
      { id: "quad", label: t("editor.mode_quad") },
    ],
    onChange: (id) => setMode(id),
  });
  const fieldsBox = h("div", { class: "mk-fields" }, fieldEls);
  const quadNote = h("p", { class: "mk-quad-note" }, icon("info", { size: 14 }), h("span", null, t("editor.quad_note")));
  quadNote.hidden = true;
  const realismCtl = lookSlider("realism", t("editor.realism"), t("editor.realism_hint"));
  const curveCtl = lookSlider("curve", t("editor.curve"), t("editor.curve_hint"));
  const lookBox = h("div", { class: "mk-look-box" }, realismCtl.el, curveCtl.el);
  const designThumb = h("span", { class: "mk-design-thumb checker" });
  const designName = h("span", { class: "mk-design-name ellipsis" });
  const designRow = h(
    "button",
    { type: "button", class: "mk-design-row", onClick: () => pickDesign() },
    designThumb,
    h("span", { class: "mk-design-text" }, designName, h("span", { class: "mk-design-sub" }, t("editor.preview_only"))),
    icon("chevron-right", { size: 16 }),
  );
  const appliedTitle = h("p", { class: "mk-applied-title" });
  const appliedCard = h(
    "div",
    { class: "mk-applied", role: "status" },
    h("span", { class: "mk-applied-icon" }, icon("check", { size: 15, strokeWidth: 2.8 })),
    h("div", { class: "mk-applied-text" }, appliedTitle, h("p", { class: "mk-applied-sub" }, t("editor.applied_sub"))),
  );
  appliedCard.hidden = true;
  const sideSkeleton = h(
    "div",
    { class: "mk-side-skeleton", "aria-hidden": "true" },
    skeleton({ lines: 1, height: 22, widths: ["60%"] }),
    skeleton({ lines: 2, height: 11, widths: ["92%", "70%"] }),
    skeleton({ lines: 4, height: 38, gap: 12, widths: ["100%"] }),
  );
  const side = h(
    "aside",
    { class: "mk-side" },
    sideSkeleton,
    h("div", { class: "mk-side-head" }, titleEl, moreBtn),
    h("div", { class: "mk-side-chips" }, sizeEl, stateHost),
    h("p", { class: "mk-helper" }, t("editor.helper")),
    disabledHost,
    h("div", { class: "mk-divider" }),
    sectionTitle(t("editor.section_area"), { actions: modeTabs.el }),
    fieldsBox,
    quadNote,
    sectionTitle(t("editor.section_look")),
    lookBox,
    sectionTitle(t("editor.section_preview")),
    designRow,
    h("div", { class: "mk-side-fill" }),
    appliedCard,
  );

  // ---- footer
  const sameHost = h("div", { class: "mk-same" });
  // After a save: the next mockup still on the default area, so 37 are done one after another.
  const nextHost = h("div", { class: "mk-next-host" });
  nextHost.hidden = true;
  const cancelBtn = button({ label: t("common.cancel"), variant: "secondary", onClick: () => cancel() });
  const saveBtn = button({ label: t("common.save"), icon: "check", variant: "primary", onClick: () => save() });
  const foot = h("footer", { class: "mk-main-foot" }, sameHost, h("div", { class: "spacer" }), nextHost, cancelBtn, saveBtn);

  const main = h(
    "section",
    { class: "card mk-main is-loading" },
    h("div", { class: "mk-main-top" }, h("div", { class: "mk-canvas-col" }, stageWrap, rectHint), side),
    foot,
  );
  const shell = h("div", { class: "mk-editor" }, lib, main);
  el.append(shell);
  const offDrop = installPageDrop(el, ctx, (files) => uploadHere(files));
  cleanups.push(offDrop);

  // ---- load
  libCount.textContent = "";
  mount(libList, Array.from({ length: 5 }, () => h("div", { class: "mk-lib-item is-skeleton" }, h("span", { class: "skeleton mk-lib-thumb" }), skeleton({ lines: 2, height: 10, widths: ["70%", "40%"] }))));
  art.classList.add("is-loading");

  let listRes;
  let areaRes = null;
  try {
    listRes = await ctx.api.get("/api/mockups", null, { signal: ctx.signal });
    // Asked only for a mockup that exists, so a stale link does not log a failed request.
    if (listRes.items.some((it) => it.name === name)) {
      areaRes = await ctx.api.get(`/api/mockups/${enc(name)}/area`, null, { signal: ctx.signal }).catch((err) => {
        if (err && err.code === "not_found") return null;
        throw err;
      });
    }
  } catch (err) {
    if (ctx.api.isAbort(err)) return () => {};
    mount(
      el,
      emptyState({
        icon: "alert",
        title: t("load_error"),
        message: ctx.api.errorText(err, t),
        action: button({ label: t("common.retry"), icon: "refresh", onClick: () => ctx.remount() }),
      }),
    );
    return () => offDrop();
  }
  list = listRes.items;
  maxEnabled = listRes.max_enabled || maxEnabled;
  item = list.find((it) => it.name === name) || null;
  if (!item || !areaRes) {
    mount(
      el,
      emptyState({
        icon: "image",
        title: t("editor.not_found"),
        message: t("editor.not_found_msg"),
        action: button({ label: t("back"), icon: "arrow-left", onClick: () => ctx.navigate(GRID_PATH) }),
      }),
    );
    return () => offDrop();
  }
  main.classList.remove("is-loading");
  sideSkeleton.remove();
  takeArea(areaRes);
  siblings = areaRes.same_size || [];
  // On unless a same-size mockup has an area of its own that differs from this one: after
  // one same-size save they all share it, and the switch stays on (the video's normal).
  sameSize = siblings.length > 0 && !ownDiffering().length;

  renderLibrary();
  renderSide();
  renderSame();
  renderRect();
  renderQuad();
  renderLook();
  updateState();
  setShowDesign(showDesign, { quiet: true });

  // The base picture: exactly what the compositor draws on (upright, flattened).
  baseImg.addEventListener(
    "load",
    () => {
      art.classList.remove("is-loading");
      stageSpinner.hidden = true;
      layout();
      loadPreview();
    },
    { once: true },
  );
  baseImg.addEventListener(
    "error",
    () => {
      stageSpinner.hidden = true;
      mount(stage, emptyState({ icon: "alert", title: t("editor.unreadable"), message: item.name, compact: true }));
    },
    { once: true },
  );
  baseImg.src = ctx.api.url(`/api/mockups/${enc(name)}/image`, { max: IMAGE_MAX, v: item.version });

  const ro = new ResizeObserver(() => layout());
  ro.observe(stage);
  cleanups.push(() => ro.disconnect());

  // Designs for the preview (the default is the first transparent one, else the sample).
  ctx.api
    .get("/api/mockups/designs", null, { signal: ctx.signal })
    .then((res) => {
      designs = res;
      const known = design && (design.id === SAMPLE.id || res.items.some((d) => d.id === design.id));
      if (!known) {
        const fallback = res.items.find((d) => d.id === res.default);
        setDesign(fallback || SAMPLE, { remember: false });
      } else {
        const fresh = res.items.find((d) => d.id === design.id);
        if (fresh) setDesign(fresh, { remember: false });
      }
    })
    .catch((err) => {
      if (ctx.api.isAbort(err)) return;
      if (!design) setDesign(SAMPLE, { remember: false });
    });
  if (design) setDesign(design, { remember: false });
  else renderDesignRow();

  // ---- guards against losing unsaved work: the app asks before any in-app navigation
  // (library, sidebar, header, Back), a shop switch or a language change; setDirty makes
  // a reload or closing the tab ask as well. A "leave" answer keeps the edits marked as
  // unsaved: the leave can still be called off after it (the quit question, a refused
  // shop switch, a failed language save), and the page is unmounted when it happens.
  ctx.onBeforeLeave(async () => {
    if (!isDirty()) return true;
    return ctx.confirm({ title: t("editor.unsaved_title"), message: t("editor.unsaved_msg"), confirmLabel: t("editor.leave"), danger: true });
  });

  // ---- pointer editing
  art.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !area || art.classList.contains("is-loading")) return;
    const handle = e.target.closest(".mk-handle");
    const inRect = e.target.closest(".mk-rect");
    const r = art.getBoundingClientRect();
    if (quad) {
      // Four corners: a corner handle moves that corner, a press inside the shape moves
      // all four, a press outside draws a new rectangle that becomes four corners.
      const corner = e.target.closest(".mk-qhandle");
      const fx = (e.clientX - r.left) / r.width;
      const fy = (e.clientY - r.top) / r.height;
      drag = {
        mode: corner ? "corner" : insideQuad(quad, fx, fy) ? "qmove" : "draw",
        corner: corner ? Number(corner.dataset.corner) : -1,
        startQuad: quad.map((pt) => [...pt]),
        start: { ...area },
        px: e.clientX,
        py: e.clientY,
        fx,
        fy,
        width: r.width,
        height: r.height,
        moved: false,
        id: e.pointerId,
      };
      try {
        art.setPointerCapture(e.pointerId);
      } catch {
        /* synthetic events */
      }
      e.preventDefault();
      if (corner) corner.focus({ preventScroll: true });
      return;
    }
    drag = {
      mode: handle ? "resize" : inRect ? "move" : "draw",
      handle: handle ? handle.dataset.handle : null,
      start: { ...area },
      px: e.clientX,
      py: e.clientY,
      fx: (e.clientX - r.left) / r.width,
      fy: (e.clientY - r.top) / r.height,
      width: r.width,
      height: r.height,
      moved: false,
      id: e.pointerId,
    };
    try {
      art.setPointerCapture(e.pointerId);
    } catch {
      /* synthetic events */
    }
    e.preventDefault();
    rect.focus({ preventScroll: true });
  });
  art.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const ddx = e.clientX - drag.px;
    const ddy = e.clientY - drag.py;
    if (!drag.moved && Math.abs(ddx) + Math.abs(ddy) < 3) return;
    if (!drag.moved) {
      drag.moved = true;
      art.classList.add("is-dragging", `drag-${drag.mode}`);
      // What the drag changes lights up at the side (the video's focused Genişlik and
      // Yükseklik while drawing), and Kaydet waits.
      if (!quad) for (const key of drag.mode === "move" ? ["x", "y"] : ["w", "h"]) fields[key].classList.add("is-focus");
      saveBtn.classList.add("is-waiting");
    }
    if (quad) {
      dragQuad(drag, ddx / drag.width, ddy / drag.height);
      return;
    }
    setArea(computeDrag(drag, ddx / drag.width, ddy / drag.height, e.shiftKey));
  });
  const endDrag = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const was = drag;
    drag = null;
    art.classList.remove("is-dragging", "drag-move", "drag-resize", "drag-draw", "drag-corner", "drag-qmove");
    for (const key of ["x", "y", "w", "h"]) fields[key].classList.remove("is-focus");
    saveBtn.classList.remove("is-waiting");
    try {
      art.releasePointerCapture(e.pointerId);
    } catch {
      /* already released */
    }
    if (was.moved) schedulePreview();
  };
  art.addEventListener("pointerup", endDrag);
  art.addEventListener("pointercancel", endDrag);
  art.addEventListener("dragstart", (e) => e.preventDefault());

  // A focused corner moves with the arrow keys: 1 px, 10 px with Shift (mockup pixels).
  quadHandles.forEach((el, i) =>
    el.addEventListener("keydown", (e) => {
      const moves = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
      const mv = moves[e.key];
      if (!mv || !quad) return;
      e.preventDefault();
      const step = e.shiftKey ? 10 : 1;
      const next = quad.map((pt) => [...pt]);
      next[i] = [clamp(next[i][0] + (mv[0] * step) / W(), 0, 1), clamp(next[i][1] + (mv[1] * step) / H(), 0, 1)];
      if (setQuad(next)) schedulePreview();
    }),
  );

  rect.addEventListener("keydown", (e) => {
    const moves = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    const mv = moves[e.key];
    if (!mv || !area) return;
    e.preventDefault();
    const step = e.shiftKey ? 10 : 1;
    setArea({ ...area, x: clamp(area.x + (mv[0] * step) / W(), 0, 1 - area.w), y: clamp(area.y + (mv[1] * step) / H(), 0, 1 - area.h) });
    schedulePreview();
  });

  stage.addEventListener(
    "wheel",
    (e) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      const i = ZOOMS.indexOf(zoom);
      const next = e.deltaY < 0 ? ZOOMS[Math.min(ZOOMS.length - 1, i + 1)] : ZOOMS[Math.max(0, i - 1)];
      if (next !== zoom) setZoom(next);
    },
    { passive: false },
  );

  // ---- functions

  function minW() {
    return Math.min(1, Math.max(12 / W(), 0.01));
  }
  function minH() {
    return Math.min(1, Math.max(12 / H(), 0.01));
  }

  function computeDrag(d, dx, dy, keepRatio) {
    const s = d.start;
    if (d.mode === "move") {
      return { x: clamp(s.x + dx, 0, 1 - s.w), y: clamp(s.y + dy, 0, 1 - s.h), w: s.w, h: s.h };
    }
    if (d.mode === "draw") {
      const ax = clamp(d.fx, 0, 1);
      const ay = clamp(d.fy, 0, 1);
      const bx = clamp(d.fx + dx, 0, 1);
      const by = clamp(d.fy + dy, 0, 1);
      const w = Math.max(Math.abs(bx - ax), minW());
      const hh = Math.max(Math.abs(by - ay), minH());
      return { x: clamp(Math.min(ax, bx), 0, 1 - w), y: clamp(Math.min(ay, by), 0, 1 - hh), w, h: hh };
    }
    const hd = d.handle;
    let left = s.x;
    let right = s.x + s.w;
    let top = s.y;
    let bottom = s.y + s.h;
    if (hd.includes("w")) left = clamp(s.x + dx, 0, right - minW());
    if (hd.includes("e")) right = clamp(s.x + s.w + dx, left + minW(), 1);
    if (hd.includes("n")) top = clamp(s.y + dy, 0, bottom - minH());
    if (hd.includes("s")) bottom = clamp(s.y + s.h + dy, top + minH(), 1);
    if (keepRatio && hd.length === 2) {
      // Keep the rectangle's pixel ratio, anchored at the opposite corner.
      const ratio = (s.w * W()) / (s.h * H());
      let wpx = (right - left) * W();
      let hpx = (bottom - top) * H();
      if (wpx / hpx > ratio) hpx = wpx / ratio;
      else wpx = hpx * ratio;
      let nw = wpx / W();
      let nh = hpx / H();
      const maxW = hd.includes("w") ? s.x + s.w : 1 - s.x;
      const maxH = hd.includes("n") ? s.y + s.h : 1 - s.y;
      const k = Math.min(1, maxW / nw, maxH / nh);
      nw *= k;
      nh *= k;
      left = hd.includes("w") ? s.x + s.w - nw : s.x;
      top = hd.includes("n") ? s.y + s.h - nh : s.y;
      right = left + nw;
      bottom = top + nh;
    }
    return { x: left, y: top, w: right - left, h: bottom - top };
  }

  /** The area, corners and look from an area payload (GET/POST/DELETE .../area). */
  function takeArea(res) {
    const a = res.area || {};
    saved = { x: a.x, y: a.y, w: a.w, h: a.h };
    area = { ...saved };
    savedQuad = Array.isArray(a.quad) ? a.quad.map((pt) => [pt[0], pt[1]]) : null;
    quad = savedQuad ? savedQuad.map((pt) => [...pt]) : null;
    source = res.source;
    takeStyle(res, { stored: a });
  }

  /** The type's defaults, and the realism and curve in use (`stored`: the area's own). */
  function takeStyle(res, { stored } = {}) {
    const st = res.style || {};
    style = {
      realism_default: st.realism_default || 0,
      curve_default: st.curve_default || 0,
      curve_offered: !!st.curve_offered,
    };
    const a = stored || {};
    savedLook = { realism: st.realism ?? 0, curve: st.curve ?? 0 };
    savedExplicit = { realism: a.realism !== undefined && a.realism !== null, curve: a.curve !== undefined && a.curve !== null };
    look = { ...savedLook };
    explicit = { ...savedExplicit };
  }

  /** Corners in pixels of the mockup, for the shape rules (no dent, no twist). */
  function quadPx(q) {
    return q.map(([x, y]) => [x * W(), y * H()]);
  }

  /** Take four corners if they make a valid shape (false: refused, nothing changes). */
  function setQuad(next) {
    const clean = next.map(([x, y]) => [clamp(x, 0, 1), clamp(y, 0, 1)]);
    const box = quadBox(clean);
    if (!wellShaped(quadPx(clean)) || box.w * W() < 12 || box.h * H() < 12) return false;
    quad = clean;
    area = box;
    markStale();
    renderQuad();
    renderRect();
    updateState();
    return true;
  }

  function dragQuad(d, dx, dy) {
    const s = d.startQuad;
    if (d.mode === "corner") {
      const next = s.map((pt) => [...pt]);
      next[d.corner] = [clamp(s[d.corner][0] + dx, 0, 1), clamp(s[d.corner][1] + dy, 0, 1)];
      setQuad(next);
      return;
    }
    if (d.mode === "qmove") {
      const xs = s.map((pt) => pt[0]);
      const ys = s.map((pt) => pt[1]);
      const mx = clamp(dx, -Math.min(...xs), 1 - Math.max(...xs));
      const my = clamp(dy, -Math.min(...ys), 1 - Math.max(...ys));
      setQuad(s.map(([x, y]) => [x + mx, y + my]));
      return;
    }
    setQuad(rectQuad(computeDrag(d, dx, dy, false)));
  }

  function setMode(mode) {
    if (mode === "quad" && !quad) setQuad(rectQuad(area));
    else if (mode === "rect" && quad) {
      quad = null;
      markStale();
      renderQuad();
      renderRect();
      updateState();
    }
    renderQuad();
    schedulePreview();
  }

  function renderQuad() {
    const on = !!quad;
    art.classList.toggle("is-quad", on);
    if (modeTabs.value !== (on ? "quad" : "rect")) modeTabs.update(null, on ? "quad" : "rect");
    fieldsBox.hidden = on;
    quadNote.hidden = !on;
    if (!on) return;
    quadPoly.setAttribute("points", quad.map(([x, y]) => `${x * 100},${y * 100}`).join(" "));
    quad.forEach(([x, y], i) => {
      const el = quadHandles[i];
      el.style.left = `${x * 100}%`;
      el.style.top = `${y * 100}%`;
      el.setAttribute("aria-label", t("editor.corner_aria", { corner: t(`editor.corner_${CORNERS[i]}`), x: pct(x), y: pct(y) }));
    });
    placeQuadDesign();
  }

  /** The design drawn into the corners with a CSS perspective (matrix3d), fitted and
   *  centred as the compositor fits it; the server's preview replaces it after a drag. */
  function placeQuadDesign() {
    const iw = quadDesign.naturalWidth;
    const ih = quadDesign.naturalHeight;
    const aw = art.clientWidth;
    const ah = art.clientHeight;
    const M = quad && iw && ih && aw && ah ? designMatrix(iw, ih, aw, ah) : null;
    quadDesign.classList.toggle("is-placed", !!M);
    if (!M) return;
    quadDesign.style.width = `${iw}px`;
    quadDesign.style.height = `${ih}px`;
    quadDesign.style.transform = `matrix3d(${[M[0], M[3], 0, M[6], M[1], M[4], 0, M[7], 0, 0, 1, 0, M[2], M[5], 0, M[8]].join(",")})`;
  }

  function designMatrix(iw, ih, aw, ah) {
    const P = quad.map(([x, y]) => [x * aw, y * ah]);
    const d = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
    const qw = (d(P[0], P[1]) + d(P[3], P[2])) / 2;
    const qh = (d(P[0], P[3]) + d(P[1], P[2])) / 2;
    if (qw < 1 || qh < 1) return null;
    const k = Math.min(qw / iw, qh / ih);
    const us = (iw * k) / qw;
    const vs = (ih * k) / qh;
    const u0 = (1 - us) / 2;
    const v0 = (1 - vs) / 2;
    const unit = homography([[0, 0], [1, 0], [1, 1], [0, 1]], P);
    if (!unit) return null;
    const at = (u, v) => applyH(unit, u, v);
    const dst = [at(u0, v0), at(u0 + us, v0), at(u0 + us, v0 + vs), at(u0, v0 + vs)];
    return homography([[0, 0], [iw, 0], [iw, ih], [0, ih]], dst);
  }

  /** A 0-100 slider for the look (realism, curve) in the watermark card's style. */
  function lookSlider(key, label, hintText) {
    const value = h("span", { class: "mk-wm-slider-value num" });
    const hint = h("span", { class: "mk-look-hint", id: uid(`mk-look-${key}`) }, hintText);
    const input = h("input", { type: "range", class: "mk-wm-range", min: "0", max: "100", step: "1", "aria-label": label, "aria-describedby": hint.id });
    const reset = h("button", { type: "button", class: "mk-look-reset", onClick: () => setLook(key, style[`${key}_default`], false) }, icon("undo", { size: 12 }));
    const ctl = { key, input, reset, value };
    ctl.paint = () => {
      const v = Number(input.value);
      value.textContent = percent(v / 100, 0);
      input.style.setProperty("--fill", `${v}%`);
      input.setAttribute("aria-valuetext", percent(v / 100, 0));
      const def = style[`${key}_default`];
      const label2 = t("editor.look_reset", { value: percent(def / 100, 0) });
      reset.title = label2;
      reset.setAttribute("aria-label", label2);
      reset.hidden = v === def;
    };
    input.addEventListener("input", () => setLook(key, Number(input.value), true));
    ctl.el = h(
      "div",
      { class: "mk-wm-slider mk-look" },
      h("span", { class: "mk-wm-slider-row", title: hintText }, h("span", { class: "mk-wm-label" }, label), h("span", { class: "spacer" }), reset, value),
      input,
      hint,
    );
    return ctl;
  }

  function setLook(key, v, isExplicit) {
    look = { ...look, [key]: clamp(Math.round(v), 0, 100) };
    explicit = { ...explicit, [key]: isExplicit };
    renderLook();
    updateState();
    // The last preview stays until the new one has loaded, so the slider does not flicker.
    schedulePreview();
  }

  function renderLook() {
    for (const ctl of [realismCtl, curveCtl]) {
      ctl.input.value = String(look[ctl.key]);
      ctl.paint();
    }
    curveCtl.el.hidden = !(style.curve_offered || look.curve > 0);
  }

  /** What the screen shows, rounded as it is saved: a stale preview is recognised by it. */
  function stateKey() {
    return JSON.stringify([roundArea(area), roundQuad(quad), look.realism, look.curve]);
  }

  function setArea(next) {
    const w = clamp(next.w, minW(), 1);
    const hh = clamp(next.h, minH(), 1);
    area = { x: clamp(next.x, 0, 1 - w), y: clamp(next.y, 0, 1 - hh), w, h: hh };
    markStale();
    renderRect();
    updateState();
  }

  function layout() {
    if (!item || !stage.isConnected) return;
    // The photo fills the stage like the video's (26 px at the sides); above and below it
    // keeps room for the zoom chip and the hint pill, so neither covers the product.
    const padX = 26;
    const padY = 42;
    const sw = stage.clientWidth;
    const sh = stage.clientHeight;
    if (!sw || !sh) return;
    const fit = Math.max(0.02, Math.min((sw - padX * 2) / W(), (sh - padY * 2) / H()));
    const scale = fit * zoom;
    art.style.width = `${Math.max(1, Math.round(W() * scale))}px`;
    art.style.height = `${Math.max(1, Math.round(H() * scale))}px`;
    zoomVal.textContent = percent(zoom, 0);
    fitLabel();
    placeQuadDesign();
  }

  function setZoom(next) {
    const cx = (stage.scrollLeft + stage.clientWidth / 2) / Math.max(1, stage.scrollWidth);
    const cy = (stage.scrollTop + stage.clientHeight / 2) / Math.max(1, stage.scrollHeight);
    zoom = next;
    layout();
    stage.scrollLeft = cx * stage.scrollWidth - stage.clientWidth / 2;
    stage.scrollTop = cy * stage.scrollHeight - stage.clientHeight / 2;
  }

  function openZoomMenu() {
    menu(
      zoomBtn,
      ZOOMS.map((z) => ({ label: percent(z, 0), checked: z === zoom, onClick: () => setZoom(z) })),
      { placement: "bottom-start", width: 130 },
    );
  }

  function renderRect() {
    if (!area) return;
    rect.style.left = `${area.x * 100}%`;
    rect.style.top = `${area.y * 100}%`;
    rect.style.width = `${area.w * 100}%`;
    rect.style.height = `${area.h * 100}%`;
    sizeChip.textContent = `${Math.round(area.w * W())} × ${Math.round(area.h * H())} px`;
    rect.setAttribute("aria-label", t("editor.area_aria", { x: pct(area.x), y: pct(area.y), w: pct(area.w), h: pct(area.h) }));
    rect.classList.toggle("is-ghost", isGhost());
    fitLabel();
    renderFields(false);
  }

  /** The "Baskı alanı" pill only where it fits, measured on screen so zoom counts too. */
  function fitLabel() {
    if (!area) return;
    const w = area.w * art.clientWidth;
    const hh = area.h * art.clientHeight;
    rect.classList.toggle("has-label", w >= LABEL_MIN_W && hh >= LABEL_MIN_H);
  }

  /**
   * A mockup whose print area was never set opens like the video's: the bare product and
   * "draw the print area". The default area stays as a faint outline (drafts use it until
   * the seller saves); it takes no pointer, so any press on the photo starts a new
   * rectangle. The first drag, field edit or arrow key makes it the real one; Vazgeç
   * brings the outline back. It stays focusable for the keyboard.
   */
  function isGhost() {
    return source === "default" && !isDirty();
  }

  function renderFields(force) {
    if (!area) return;
    const px = { x: area.x * W(), y: area.y * H(), w: area.w * W(), h: area.h * H() };
    for (const key of ["x", "y", "w", "h"]) {
      const input = fields[key];
      if (force || document.activeElement !== input.input) input.value = pct(area[key]);
      input.setSuffix(`${Math.round(px[key])} px`);
    }
  }

  function onFieldInput(key, raw, commit) {
    const v = parsePercent(raw);
    if (v === null || !area) {
      if (commit) renderFields(true);
      return;
    }
    const f = v / 100;
    const next = { ...area };
    if (key === "x") next.x = clamp(f, 0, 1 - area.w);
    if (key === "y") next.y = clamp(f, 0, 1 - area.h);
    if (key === "w") next.w = clamp(f, minW(), 1 - area.x);
    if (key === "h") next.h = clamp(f, minH(), 1 - area.y);
    if (!sameArea(next, area)) {
      setArea(next);
      schedulePreview();
    }
    if (commit) renderFields(true);
  }

  function updateState() {
    const dirty = isDirty();
    ctx.setDirty(dirty);
    // The video's labels: "◎ Çiziliyor" until the area is saved (its tooltip still says
    // plainly that it is not saved, or that the default area is in use), "✓ Ayarlı" after.
    let st;
    if (dirty || source === "default") {
      st = badge({ text: t("editor.drawing"), tone: "accent", icon: "target", title: dirty ? t("editor.unsaved") : t("area.default_hint") });
    } else if (source === "own") st = badge({ text: t("editor.set"), tone: "success", icon: "check" });
    else st = badge({ text: t("editor.shared"), tone: "accent", title: t("area.same_size_hint") });
    mount(stateHost, st);
    const done = !dirty && source === "own";
    const key = done ? "saved" : quad ? "corners" : "draw";
    if (underHost.dataset.state !== key) {
      underHost.dataset.state = key;
      mount(
        underHost,
        done
          ? h("span", { class: "mk-stage-pill is-done" }, icon("check", { size: 13, strokeWidth: 2.6 }), h("span", null, t("editor.saved_chip")))
          : h("span", { class: "mk-stage-pill" }, icon("target", { size: 13 }), h("span", null, t(quad ? "editor.corners_hint" : "editor.draw_hint"))),
      );
    }
    // Never drawn: Kaydet is dimmed like the video's until the area is drawn (a class of
    // its own, so a drag's is-waiting stays as it is). It still saves the default area.
    const unset = isGhost();
    saveBtn.classList.toggle("is-unset", unset);
    saveBtn.title = unset ? t("editor.save_unset_hint") : "";
    appliedCard.hidden = !(lastApplied && !dirty);
    nextHost.hidden = !(lastApplied && !dirty) || !nextHost.firstChild;
    main.classList.toggle("is-dirty", dirty);
    if (item) renderSideNotes();
  }

  function renderSide() {
    titleEl.textContent = itemLabel(t, item);
    titleEl.title = item.name;
    sizeEl.textContent = item.width && item.height ? `${item.width}×${item.height} px` : "–";
    noteKey = "";
    renderSideNotes();
  }

  function renderSideNotes() {
    // A never-set area needs no note here: the stage shows the bare product, a faint
    // outline and "draw the print area", and the badge's tooltip says what default means.
    const key = `${item.enabled}|${item.over_limit}`;
    if (key === noteKey) return;
    noteKey = key;
    const notesList = [];
    if (!item.enabled) {
      notesList.push(
        infoNote({
          tone: "warning",
          icon: "eye-off",
          text: t("editor.disabled_note"),
          action: button({
            label: t("editor.enable"),
            size: "sm",
            onClick: async () => {
              const res = await setEnabled(ctx, item, true);
              if (res) {
                Object.assign(item, res);
                renderSide();
                renderLibrary();
              }
            },
          }),
        }),
      );
    } else if (item.over_limit) {
      notesList.push(infoNote({ tone: "warning", icon: "layers", text: t("editor.over_note", { max: maxEnabled }) }));
    }
    mount(disabledHost, notesList);
  }

  /** Same-size mockups with an area of their own that a same-size save would change. */
  function ownDiffering() {
    return siblings.filter((n) => {
      const it = list.find((x) => x.name === n);
      return !!it && it.area_source === "own" && (!sameArea(it.area, saved) || !sameQuad(it.area.quad || null, savedQuad));
    });
  }

  function renderSame() {
    const labels = siblings.map((n) => {
      const it = list.find((x) => x.name === n);
      return it ? itemLabel(t, it) : n;
    });
    const ownOnes = ownDiffering();
    // 36 colour variants must not become one endless line: a few names, then "+33".
    const SHOWN = 3;
    const names = labels.length > SHOWN ? t("editor.same_size_more", { names: labels.slice(0, SHOWN).join(", "), n: labels.length - SHOWN }) : labels.join(", ");
    let sub = siblings.length ? names : t("editor.same_size_none");
    if (ownOnes.length && !lastApplied) sub = t("editor.same_size_own", { names });
    const tog = toggle({
      checked: sameSize && siblings.length > 0,
      disabled: !siblings.length,
      label: t("editor.same_size", { n: siblings.length }),
      sub,
      onChange: (v) => {
        sameSize = v;
        renderLibrary();
      },
    });
    tog.title = siblings.length ? `${labels.join(", ")}\n\n${t("editor.same_size_explain")}` : t("editor.same_size_explain");
    mount(sameHost, tog);
  }

  function renderLibrary() {
    libCount.textContent = t("library.count", { n: list.length });
    mount(
      libList,
      list.map((it) => {
        const current = it.name === name;
        const isApplied = applied.has(it.name);
        // Before the save, the rows the area will also go to (the video's violet "aynı ölçü").
        const twin = !current && !isApplied && sameSize && siblings.includes(it.name);
        let mark = null;
        if (current) mark = h("span", { class: "mk-lib-mark is-current" }, icon("arrow-right", { size: 16 }));
        else if (isApplied) mark = h("span", { class: "mk-lib-mark is-applied" }, icon("check", { size: 14, strokeWidth: 2.8 }));
        else if (it.area_source === "own") mark = h("span", { class: "mk-lib-mark is-own", title: t("area.own") }, icon("check", { size: 12, strokeWidth: 2.6 }));
        const img = h("img", { src: thumbUrl(ctx, it, 160), alt: "", loading: "lazy", decoding: "async", draggable: "false" });
        img.addEventListener("error", () => img.replaceWith(icon("image", { size: 18 })), { once: true });
        return h(
          "a",
          {
            class: cx("mk-lib-item", current && "is-current", !current && isApplied && "is-applied", !it.enabled && "is-disabled"),
            href: editorPath(it.name),
            role: "listitem",
            title: it.name,
            "aria-current": current ? "page" : undefined,
          },
          h("span", { class: "mk-lib-thumb" }, img),
          h(
            "span",
            { class: "mk-lib-text" },
            h("span", { class: "mk-lib-label ellipsis" }, itemLabel(t, it)),
            h(
              "span",
              { class: "mk-lib-sub" },
              h("span", { class: "mono" }, sizeText(it)),
              !current && isApplied ? [h("span", { class: "mk-sep", "aria-hidden": "true" }, "·"), h("span", { class: "mk-lib-applied" }, t("library.applied"))] : null,
              twin ? [h("span", { class: "mk-sep", "aria-hidden": "true" }, "·"), h("span", { class: "mk-lib-twin" }, t("library.same_size"))] : null,
              !it.enabled ? [h("span", { class: "mk-sep", "aria-hidden": "true" }, "·"), h("span", null, t("unused"))] : null,
              it.enabled && it.over_limit ? [h("span", { class: "mk-sep", "aria-hidden": "true" }, "·"), h("span", { class: "mk-lib-over" }, t("over_limit"))] : null,
            ),
          ),
          mark,
        );
      }),
    );
    const cur = libList.querySelector(".is-current");
    if (cur) requestAnimationFrame(() => cur.scrollIntoView({ block: "nearest" }));
  }

  // ---- preview design

  function renderDesignRow() {
    const d = design || SAMPLE;
    const img = h("img", { src: designUrl(ctx, d, 120), alt: "", draggable: "false" });
    img.addEventListener("error", () => img.replaceWith(icon("image", { size: 16 })), { once: true });
    mount(designThumb, img);
    designName.textContent = designLabel(t, d);
  }

  function setDesign(d, { remember = true } = {}) {
    design = { id: d.id, label: d.label, name: d.name, version: d.version };
    if (remember) writeStored("local", DESIGN_KEY, design);
    overlayImg.src = designUrl(ctx, design, 900);
    quadDesign.src = overlayImg.src;
    renderDesignRow();
    markStale();
    loadPreview();
  }

  function setShowDesign(v, { quiet = false } = {}) {
    showDesign = !!v;
    writeStored("local", SHOW_KEY, showDesign);
    art.classList.toggle("show-design", showDesign);
    if (!quiet) {
      markStale();
      loadPreview();
    }
  }

  function markStale() {
    previewSeq += 1;
    previewImg.hidden = true;
    art.classList.remove("has-preview");
    schedulePreview.cancel();
  }

  function loadPreview() {
    // Never-set area: the bare product until the seller draws (the video's first frame).
    if (!showDesign || !design || !area || drag || art.classList.contains("is-loading") || isGhost()) return;
    const seq = ++previewSeq;
    const a = roundArea(area);
    const q = roundQuad(quad);
    const key = stateKey();
    const img = new Image();
    img.className = "mk-preview";
    img.alt = "";
    img.draggable = false;
    img.onload = () => {
      if (seq !== previewSeq || !showDesign || key !== stateKey() || !ctx.isActive()) return;
      img.hidden = false;
      previewImg.replaceWith(img);
      previewImg = img;
      art.classList.add("has-preview");
    };
    const where = q ? { quad: q.flat().join(",") } : { x: a.x, y: a.y, w: a.w, h: a.h };
    img.src = ctx.api.url(`/api/mockups/${enc(name)}/preview`, { design: design.id, ...where, realism: look.realism, curve: look.curve, max: IMAGE_MAX });
  }

  async function pickDesign() {
    if (!designs) {
      try {
        designs = await ctx.api.get("/api/mockups/designs", null, { signal: ctx.signal });
      } catch (err) {
        if (!ctx.api.isAbort(err)) ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
        return;
      }
    }
    const options = [SAMPLE, ...designs.items];
    let m = null;
    const grid = h(
      "div",
      { class: "mk-design-grid" },
      options.map((o) =>
        h(
          "button",
          {
            type: "button",
            class: cx("mk-design-opt", design && design.id === o.id && "is-selected"),
            "aria-pressed": design && design.id === o.id ? "true" : "false",
            title: o.name || designLabel(t, o),
            onClick: () => {
              setDesign(o);
              if (!showDesign) setShowDesign(true);
              if (m) m.close();
            },
          },
          h("span", { class: "mk-design-opt-img checker" }, h("img", { src: designUrl(ctx, o, 240), alt: "", loading: "lazy", draggable: "false" })),
          h("span", { class: "mk-design-opt-label ellipsis" }, designLabel(t, o)),
        ),
      ),
    );
    m = ctx.modal({
      title: t("editor.pick_design"),
      subtitle: t("editor.pick_sub"),
      width: 660,
      body: [designs.items.length ? null : infoNote({ tone: "neutral", icon: "info", text: t("editor.no_designs") }), grid],
    });
  }

  // ---- actions

  async function save() {
    if (saving || !area) return;
    saving = true;
    saveBtn.setLoading(true);
    try {
      // A realism or curve the seller did not touch is sent as null: it keeps following the
      // mockup type (and each same-size mockup's own type).
      const where = quad ? { quad: roundQuad(quad) } : roundArea(area);
      const body = {
        ...where,
        realism: explicit.realism ? look.realism : null,
        curve: explicit.curve ? look.curve : null,
        same_size: !!(sameSize && siblings.length),
      };
      const sent = stateKey();
      const res = await ctx.api.post(`/api/mockups/${enc(name)}/area`, body, { signal: ctx.signal });
      // Edits made while the save was on its way stay on screen (and unsaved).
      const mine = { area: { ...area }, quad: quad && quad.map((pt) => [...pt]), look: { ...look }, explicit: { ...explicit } };
      const changed = stateKey() !== sent;
      takeArea(res);
      if (changed) ({ area, quad, look, explicit } = mine);
      siblings = res.same_size || siblings;
      lastApplied = res.applied_to || [name];
      for (const n of lastApplied) applied.add(n);
      storeApplied();
      appliedTitle.textContent = t("editor.applied_title", { n: lastApplied.length });
      for (const it of list) {
        if (lastApplied.includes(it.name)) {
          it.area = { ...res.area };
          it.area_source = "own";
        }
      }
      renderLibrary();
      renderSame();
      renderRect();
      renderQuad();
      renderLook();
      updateState();
      // In a short window the side column scrolls: bring the notice into view.
      requestAnimationFrame(() => {
        if (!appliedCard.hidden && appliedCard.isConnected) appliedCard.scrollIntoView({ block: "nearest" });
      });
      refreshList();
    } catch (err) {
      if (!ctx.api.isAbort(err)) ctx.toast({ tone: "danger", title: t("editor.save_failed"), message: ctx.api.errorText(err, t) });
    } finally {
      saving = false;
      saveBtn.setLoading(false);
    }
  }

  function cancel() {
    if (isDirty()) {
      area = { ...saved };
      quad = savedQuad ? savedQuad.map((pt) => [...pt]) : null;
      look = { ...savedLook };
      explicit = { ...savedExplicit };
      markStale();
      renderRect();
      renderQuad();
      renderLook();
      updateState();
      loadPreview();
      return;
    }
    ctx.navigate(GRID_PATH);
  }

  async function resetArea() {
    const ok = await ctx.confirm({ title: t("editor.reset_title"), message: t("editor.reset_message"), confirmLabel: t("editor.reset_confirm") });
    if (!ok) return;
    try {
      const res = await ctx.api.del(`/api/mockups/${enc(name)}/area`, null, { signal: ctx.signal });
      takeArea(res);
      lastApplied = null;
      applied.delete(name);
      storeApplied();
      markStale();
      renderRect();
      renderQuad();
      renderLook();
      renderSame();
      updateState();
      loadPreview();
      refreshList();
    } catch (err) {
      if (!ctx.api.isAbort(err)) ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
    }
  }

  /** "Sıradaki: Kupa · Beyaz →" in the footer after a save, while others are still unset. */
  function renderNext() {
    const waiting = list.filter((it) => it.name !== name && it.area_source === "default");
    const next = waiting.find((it) => it.in_use) || waiting[0];
    if (!next) {
      mount(nextHost);
      nextHost.hidden = true;
      return;
    }
    const text = t("editor.next_default", { label: itemLabel(t, next) });
    const count = t("editor.next_default_count", { n: waiting.length });
    mount(
      nextHost,
      h(
        "a",
        { class: "mk-next", href: editorPath(next.name), title: `${text} · ${count}`, "aria-label": `${text}, ${count}` },
        h("span", { class: "mk-next-label" }, text),
        icon("arrow-right", { size: 13 }),
      ),
    );
    nextHost.hidden = !(lastApplied && !isDirty());
  }

  async function refreshList() {
    try {
      const res = await ctx.api.get("/api/mockups", null, { signal: ctx.signal });
      list = res.items;
      maxEnabled = res.max_enabled || maxEnabled;
      const fresh = list.find((it) => it.name === name);
      if (fresh) item = { ...item, ...fresh, area: item.area };
      renderLibrary();
      renderSame();
      renderSide();
      renderNext();
    } catch (err) {
      if (!ctx.api.isAbort(err)) console.warn("[mockups] could not refresh the library", err);
    }
  }

  async function onEditMeta() {
    const res = await editMeta(ctx, item);
    if (!res) return;
    Object.assign(item, res);
    const it = list.find((x) => x.name === name);
    if (it) Object.assign(it, res);
    renderSide();
    renderLibrary();
    renderSame();
    // A new type brings its own realism and curve defaults (and maybe the curve slider).
    try {
      const fresh = await ctx.api.get(`/api/mockups/${enc(name)}/area`, null, { signal: ctx.signal });
      const keep = { look: { ...look }, explicit: { ...explicit } };
      takeStyle(fresh, { stored: fresh.area });
      for (const key of ["realism", "curve"]) {
        if (keep.explicit[key]) {
          look[key] = keep.look[key];
          explicit[key] = true;
        }
      }
      renderLook();
      updateState();
      schedulePreview();
    } catch (err) {
      if (!ctx.api.isAbort(err)) console.warn("[mockups] could not refresh the print area", err);
    }
  }

  function openSideMenu() {
    menu(
      moreBtn,
      [
        { label: t("editor.show_design"), icon: "eye", checked: showDesign, onClick: () => setShowDesign(!showDesign) },
        { label: t("editor.edit_meta"), icon: "edit", onClick: () => onEditMeta() },
        { divider: true },
        { label: t("editor.reset_default"), icon: "undo", disabled: !(source === "own" && !isDirty()), onClick: () => resetArea() },
      ],
      { placement: "bottom-end", width: 240 },
    );
  }

  function uploadHere(files) {
    chain = chain
      .then(() => uploadBatch(ctx, files))
      .then(async (res) => {
        if (!ctx.isActive()) return;
        await refreshList();
        if (res.added.length === 1 && !isDirty()) ctx.navigate(editorPath(res.added[0].name));
      })
      .catch((err) => {
        if (!ctx.api.isAbort(err)) console.error("[mockups] upload failed", err);
      });
    return chain;
  }

  async function pickAndUpload() {
    const files = await pickFiles({ multiple: true });
    if (files.length && ctx.isActive()) uploadHere(files);
  }

  return () => {
    schedulePreview.cancel();
    for (const fn of cleanups) {
      try {
        fn();
      } catch {
        /* ignore */
      }
    }
  };
}
