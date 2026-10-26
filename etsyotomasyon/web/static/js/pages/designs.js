// Tasarım Yükle: drop designs, then watch each one become an Etsy draft.
//
// Views: idle (dropzone + setup chips + the six steps, video t210; an upload shows as a
// pill inside the drop zone, then the start card), running (pipeline table + "being
// prepared" card, t220) and done (success banner + table + sample draft, t240). A run is
// a server job ("designs"); this page only renders its state and live events, so
// navigating away and back restores the view.

import {
  h, cx, mount, button, iconButton, badge, infoNote, thumb, dropzone, progressBar,
  stepDots, tagChip, spinner, skeleton, select, uid,
} from "../ui.js";
import { icon } from "../icons.js";
import { money, duration, relative, bytes, number } from "../format.js";

const STEPS = ["mockup", "research", "title", "tags", "check", "draft"];
// The video's step icons (YukleEkrani STAGE_ICON): "loader" is its 8-ray burst.
const STEP_ICONS = { mockup: "image", research: "search", title: "loader", tags: "tag", check: "target", draft: "upload" };
const FINAL = new Set(["ok", "partial", "error", "cancelled", "checked"]);
const CHECK_SETTLED = new Set(["done", "warn", "error"]);
// While a drag is over the zone the browser shows each item's kind and type, never the
// files: a count is shown only when every item is one of the images the zone takes.
const DRAG_TYPES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/tiff"]);
// The floating sample tiles (YukleEkrani GHOSTS): centre (x, y), size, turn, opacity.
const GHOSTS = [
  { x: 0.1, y: 0.26, s: 91, r: -9, o: 0.2 },
  { x: 0.21, y: 0.66, s: 74, r: 7, o: 0.15 },
  { x: 0.07, y: 0.8, s: 58, r: -4, o: 0.1 },
  { x: 0.9, y: 0.25, s: 86, r: 8, o: 0.2 },
  { x: 0.79, y: 0.67, s: 72, r: -7, o: 0.15 },
  { x: 0.94, y: 0.8, s: 54, r: 5, o: 0.1 },
];
const GHOST_ICONS = ["frame", "shirt", "mug", "image", "bag", "sun"];
const REVEAL_MOCKUP_MS = 110;
const REVEAL_TAG_MS = 70;
const TYPE_TICK_MS = 24;
// Thumbnail widths: a row's (and the side card's head) and a mockup tile's.
const ROW_THUMB_W = 96;
const TILE_THUMB_W = 320;
// A mockup tile appears once its picture is there (the counter follows the tiles one can
// see), but never waits longer than this for it; the side card stays on its product
// while it fills in, at most REVEAL_MAX_MS.
const TILE_WAIT_MS = 1500;
const GATE_POLL_MS = 40;
const REVEAL_MAX_MS = 5000;
// The dashed tag slots before the tags are known: varied widths as in the video (each
// row its own), as fractions of the row (its three 6 px gaps taken off), so a row holds
// four at any card width.
const SLOT_ROWS = [
  [0.24, 0.19, 0.28, 0.21],
  [0.2, 0.27, 0.18, 0.25],
  [0.27, 0.22, 0.2, 0.2],
];
const SLOT_GAPS_PX = 18;
// listings.build_payload leaves out a weight or size Etsy would refuse (0, or no unit)
// and says so in English ("item_weight 0 not sent ...", "item_length, item_width not
// sent: item_dimensions_unit is empty"); runs before its own code gave it check_warning.
const MEASURE_NOTE = /^item_(weight|length|width|height)\b[^:]*\bnot sent\b/;
// İlanlar keeps the list a detail page's arrows walk here; a draft opened from this
// page gets the run's drafts, in run order.
const NAV_KEY = "etsyotomasyon.listings.nav";
// Problems whose translated line already says everything (the English detail is left out).
const SELF_EXPLAINED = new Set([
  "junk_name", "too_many_pixels", "too_many_images", "no_images", "invalid_image", "stopped",
  "no_deliverable", "too_many_files", "file_type", "file_too_large", "file_empty", "file_missing", "file_unreadable",
  "no_photos", "nested_files", "download_is_photo",
]);
// A digital product folder keeps what the buyer downloads in one of these subfolders.
const FILES_DIRS = new Set(["dosyalar", "files"]);
const DIGITAL = new Set(["download", "both"]);
const JOB_FINAL = new Set(["done", "error", "cancelled"]);
const ACCEPT = ".png,.jpg,.jpeg,.webp,.gif,.bmp,.tif,.tiff";
const MAX_FILES = 500;
const MAX_BYTES = 50 * 1024 * 1024;
const MAX_PHOTOS = 20;
const MAX_DOWNLOADS = 5; // Etsy: download files per listing (client.MAX_LISTING_FILES)
const UPLOAD_PARALLEL = 3;
const VISIBLE_ROWS = 7;
const PENDING_TILES = 18;
const DISMISS_KEY = "etsyotomasyon.designs.dismissed";
const SETUP_LINKS = {
  keys: "/kurulum/magaza",
  connect: "/kurulum/magaza",
  reconnect: "/kurulum/magaza",
  bad_keys: "/kurulum/magaza",
  template: "/kurulum/sablon",
  template_invalid: "/kurulum/sablon",
};

function storageGet(key) {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(key, value) {
  try {
    sessionStorage.setItem(key, value);
  } catch {
    /* private mode: the view simply is not remembered */
  }
}

/** The drafts a detail page's "← 1 / n →" walks (numbers, as listing-detail compares them). */
function storeNav(ids) {
  try {
    sessionStorage.setItem(NAV_KEY, JSON.stringify({ ids: ids.map(Number).filter((x) => x > 0), back: "/tasarim-yukle" }));
  } catch {
    /* private mode: the detail page just has no arrows */
  }
}

function reducedMotion() {
  return typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** A plain design (a transparent PNG) as opposed to a photo: shown on a checkerboard. */
function isDesign(item) {
  if (!item) return false;
  if (item.mode) return item.mode === "composited";
  return item.kind !== "folder";
}

function randomId() {
  const bytesArr = new Uint8Array(8);
  crypto.getRandomValues(bytesArr);
  return [...bytesArr].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function baseName(path) {
  return String(path || "").split("/").pop();
}

/** "Retro Mountain Sunset Shirt, Vintage Hiking Tee, ..." -> "Retro Mountain Sunset Shirt". */
function shortTitle(title) {
  const text = String(title || "");
  const first = text.split(/\s*[,|]\s*|\s+[-–—]\s+/)[0];
  return first.length >= 8 ? first : text;
}

/**
 * What a dropped path is to a digital template's product: "Planner/dosyalar/planner.pdf"
 * is a download file of the product folder "Planner" ({product, sub, name}); a file deeper
 * down ("Planner/dosyalar/A4/art.pdf") is {nested: true}, which Etsy cannot take (files,
 * not folders). null for anything else, and always for a physical template: there a
 * "dosyalar" / "files" folder is just a folder of designs.
 */
export function downloadOf(path, digital) {
  if (!digital) return null;
  const parts = String(path || "").split("/").filter(Boolean);
  // The last "dosyalar" / "files" with a product folder before it and a file after it.
  for (let i = parts.length - 2; i >= 1; i -= 1) {
    if (!FILES_DIRS.has(parts[i].toLowerCase())) continue;
    if (i === parts.length - 2) return { product: parts[i - 1], sub: parts[i], name: parts[i + 1], nested: false };
    return { nested: true };
  }
  return null;
}

/**
 * Server paths for a drop: "name" for a design, "folder/name" for a photo of a product
 * folder and "folder/dosyalar/name" for a download file of one. The download rows come
 * last, and upload() sends them only once every photo is in, so a product folder is
 * always claimed by its first photo. A download whose product has no photo in this drop
 * is marked `attach`: it goes without the batch and joins the folder of that name
 * already in 2-PRODUCTS (the missing "dosyalar" of a product added afterwards).
 * Download files only travel with "every folder is one product"; files nested deeper in
 * "dosyalar" never go (Etsy takes files, not folders).
 */
export function planUploads(files, mode, digital) {
  const byDir = new Map();
  const downloads = [];
  for (const x of files) {
    const dl = downloadOf(x.path, digital);
    if (dl) {
      if (mode === "products" && !dl.nested) downloads.push({ x, dl });
      continue;
    }
    const parts = (x.path || x.file.name).split("/").filter(Boolean);
    const dir = parts.slice(0, -1).join("/");
    if (!byDir.has(dir)) byDir.set(dir, []);
    byDir.get(dir).push(x);
  }
  const out = [];
  const products = new Set();
  for (const [dir, group] of byDir) {
    const leaf = dir ? dir.split("/").pop() : "";
    const asProduct = mode === "products" && dir && group.length <= MAX_PHOTOS;
    if (asProduct) products.add(leaf.toLowerCase());
    for (const x of group) {
      const name = x.file.name;
      out.push({ file: x.file, path: asProduct ? `${leaf}/${name}` : name, label: x.path || name });
    }
  }
  for (const { x, dl } of downloads) {
    out.push({
      file: x.file,
      path: `${dl.product}/${dl.sub}/${dl.name}`,
      label: x.path,
      deliverable: true,
      attach: !products.has(dl.product.toLowerCase()),
    });
  }
  return out;
}

/**
 * How many files a drag over the zone brings: the browser shows each item's kind and
 * type but not the files, and a folder is one item with no type. A number only when
 * every item is an image the zone takes; null otherwise ("Yüklemeye hazır").
 */
export function dragCount(items) {
  const list = [...(items || [])];
  if (!list.length) return null;
  return list.every((it) => it && it.kind === "file" && DRAG_TYPES.has(String(it.type || "").toLowerCase())) ? list.length : null;
}

/**
 * The first of the seven rows shown while a run goes on: pages of seven (the video
 * keeps its rows in place), the page of the first unfinished product, or of the row
 * picked by a click while it stays picked.
 */
export function pageStart(total, firstUnfinished, selected) {
  if (total <= VISIBLE_ROWS) return 0;
  const first = firstUnfinished < 0 ? total - 1 : firstUnfinished;
  const anchor = selected !== null && selected !== undefined && selected >= 0 && selected < total ? selected : first;
  return Math.max(0, Math.min(Math.floor(anchor / VISIBLE_ROWS) * VISIBLE_ROWS, total - VISIBLE_ROWS));
}

/** "~2 dk kaldı" / "~50 sn kaldı" (video): [i18n key, n] for `rem` seconds left. */
export function etaLabel(rem) {
  if (rem >= 60) return ["run.eta_min", Math.round(rem / 60)];
  return ["run.eta_sec", Math.max(10, Math.ceil(rem / 10) * 10)];
}

/** physical | download | both, from whatever the server sent (physical when unknown). */
function listingType(value) {
  return value === "download" || value === "both" ? value : "physical";
}

/** The plain design among a product's images (the rest are mockups). */
function flatPath(item) {
  if (!item) return null;
  if (item.flat) return item.flat;
  // Runs saved before the flat image was marked: the stream names it "<design>--flat".
  if (item.mode !== "composited") return null;
  return (item.images || []).find((p) => /--flat(-\d+)?\.jpg$/i.test(p)) || null;
}

/**
 * The i18n key for a product's warning: warn.<code>; a weight or size Etsy was not sent
 * reads warn.measure_not_sent, never the library's English, whichever code it came with.
 */
export function warnKey(w) {
  if (!w) return "";
  if (w.code === "check_warning" && MEASURE_NOTE.test(String(w.message || ""))) return "warn.measure_not_sent";
  return `warn.${w.code}`;
}

/**
 * The start card's "Mağaza bölümü" select from GET /api/designs/sections (`d`; null when
 * it could not be read): {options: [{value, label}], start}. "Şablondaki gibi (…)" names
 * the template's section; "Bölüm yok" and the shop's sections follow when there are
 * any (or when they could not be read, "Bölüm yok" still can be picked). It starts on
 * the server's choice (the shop's last one) when that is still offered.
 */
export function sectionChoices(d, t) {
  const sections = (d && d.sections) || [];
  let template = t("ready.section.template_plain");
  if (d && d.available) {
    const own = d.template || {};
    const name = own.id == null ? t("ready.section.no_section") : own.missing ? t("ready.section.deleted") : own.title || `#${own.id}`;
    template = t("ready.section.template", { name });
  }
  const options = [{ value: "template", label: template }];
  if (!d || !d.available || sections.length) options.push({ value: "none", label: t("ready.section.none") });
  for (const s of sections) options.push({ value: String(s.id), label: s.title || `#${s.id}` });
  const start = d && options.some((o) => o.value === String(d.choice)) ? String(d.choice) : "template";
  return { options, start };
}

/**
 * What the card says under the select while it shows `value`: [kind ("hint" |
 * "warning"), i18n key, params] each. Sections that cannot be read, or a shop without
 * any, is one hint (that line already says where the drafts go); on "template", a last
 * choice or a template section that is gone on Etsy is a warning.
 */
export function sectionNotes(d, value) {
  if (!d || !d.available) return [["hint", "ready.section_unavailable"]];
  if (!(d.sections || []).length) return [["hint", "ready.section_empty"]];
  const notes = [];
  if (value !== "template") return notes;
  if (d.remembered_missing && d.remembered) {
    notes.push(["warning", "ready.section_remembered_gone", { name: d.remembered.title || `#${d.remembered.choice}` }]);
  }
  if (d.template && d.template.missing) notes.push(["warning", "ready.section_template_gone"]);
  return notes;
}

/** The start request's `section` for the select's value: "template", "none" or an id. */
export function sectionValue(value) {
  return value === "template" || value === "none" ? value : Number(value);
}

/** How long typeTitle() takes to type `text` from character `from` on (ms). */
function typeDuration(text, from) {
  const len = String(text || "").length;
  const step = Math.max(2, Math.ceil(len / 30));
  return Math.ceil(Math.max(0, len - from) / step) * TYPE_TICK_MS;
}

/** The n-th dashed tag slot's width (four to a row): a fraction of the row. */
export function slotWidth(n) {
  const row = SLOT_ROWS[Math.floor(n / 4) % SLOT_ROWS.length];
  return `calc((100% - ${SLOT_GAPS_PX}px) * ${row[n % 4]})`;
}

/**
 * {done}: true once `img` has its picture, could not get it, or `cap` ms have passed
 * (the side card's timeline waits on it; it must never stall).
 */
function imageGate(img, cap) {
  const gate = { done: false };
  const finish = () => {
    gate.done = true;
  };
  if (!img) {
    finish();
    return gate;
  }
  const loaded = typeof img.decode === "function"
    ? img.decode()
    : new Promise((resolve, reject) => {
      img.addEventListener("load", resolve, { once: true });
      img.addEventListener("error", reject, { once: true });
    });
  loaded.then(finish, finish);
  setTimeout(finish, cap);
  return gate;
}

/**
 * The side card's "n/max" counters (video Count: mono, muted at 0, accent while that
 * step runs, green with a tick when done). {el, set(n, state)}; state idle | active | ok | warn.
 */
function counter(max) {
  const el = h("span", { class: "dr-count is-idle" });
  let shown = "";
  const set = (n, state) => {
    const key = `${n}|${state}`;
    if (key === shown) return;
    shown = key;
    el.className = cx("dr-count", `is-${state}`);
    const mark = state === "ok" ? icon("check", { size: 12, strokeWidth: 2.8 }) : state === "warn" ? icon("alert", { size: 12, strokeWidth: 2.4 }) : null;
    mount(el, mark, h("span", { class: "num" }, `${n}/${max}`));
  };
  return { el, set };
}

class DesignsPage {
  constructor(el, ctx) {
    this.el = el;
    this.ctx = ctx;
    this.t = ctx.t;
    this.api = ctx.api;
    this.view = null;
    this.pending = null;
    this.pendingExpanded = false;
    this.lastRun = null;
    this.run = null;
    this.selected = null; // item index pinned by a click
    this.expanded = false;
    this.timers = new Set();
    this.rafPending = false;
    this.dirtyRows = new Set();
    this.typing = null;
    this.revealTimer = null;
    this.uploading = null;
    this.statusDebounce = null;
    this.pendingModal = null;
    this.pillStart = null;
    this.checksAtLoad = true;
    this.checkToasted = false;
    this.eta = null;
    this.destroyed = false;
  }

  // ------------------------------------------------------------------ lifecycle

  async start() {
    const { ctx } = this;
    ctx.events.on("job-event", (ev) => this.onJobEvent(ev));
    ctx.events.on("job", (job) => this.onJob(job));
    ctx.events.on("reconnect", () => this.onReconnect());
    ctx.onStatus((s, prev) => {
      if (this.view !== "idle") return;
      if (prev && s && s.state === prev.state && s.setup?.designs_pending === prev.setup?.designs_pending) return;
      clearTimeout(this.statusDebounce);
      this.statusDebounce = setTimeout(() => this.loadPending(), 400);
    });
    // Leaving mid-upload (a link, Back, a shop switch, a language change) would cut the
    // upload off and leave half a product folder behind: ask first. setDirty makes a
    // reload or closing the tab ask too.
    ctx.onBeforeLeave(async () => {
      const up = this.uploading;
      if (!up) return true;
      const ok = await ctx.confirm({
        title: this.t("leave.title"),
        message: this.t("leave.msg", { done: up.done, total: up.rows.length }),
        confirmLabel: this.t("leave.confirm"),
        cancelLabel: this.t("leave.stay"),
        danger: true,
      });
      if (ok && this.uploading === up) up.controller.abort();
      return ok;
    });
    this.el.classList.add("dz-page");
    let jobs = [];
    try {
      jobs = await this.api.get("/api/jobs", { kind: "designs" }, { signal: ctx.signal });
    } catch (err) {
      if (this.api.isAbort(err)) return;
      jobs = [];
    }
    const latest = Array.isArray(jobs) && jobs.length ? jobs[0] : null;
    if (latest && (!JOB_FINAL.has(latest.status) || storageGet(DISMISS_KEY) !== latest.id)) {
      await this.showJob(latest.id);
      return;
    }
    await this.showIdle();
  }

  destroy() {
    this.destroyed = true;
    for (const id of this.timers) clearInterval(id);
    this.timers.clear();
    clearTimeout(this.statusDebounce);
    this.stopReveal();
    if (this.uploading) {
      if (this.uploading.controller) this.uploading.controller.abort();
      this.revokePreviews(this.uploading);
    }
  }

  /** The side card's animations (title typing, mockups and tags appearing one by one). */
  stopReveal() {
    if (this.typing) clearInterval(this.typing.timer);
    this.typing = null;
    clearTimeout(this.revealTimer);
    this.revealTimer = null;
  }

  /**
   * Runs [{at: ms, fn, gate}] on one timer (this.revealTimer); stopReveal() cancels the
   * rest. An event with a gate ({done}, see imageGate) waits for it, and everything after
   * it moves back by the wait: a mockup tile shows only once its picture is there.
   */
  runTimeline(events) {
    if (!events.length) return;
    events.sort((a, b) => a.at - b.at);
    let t0 = performance.now();
    let waitingSince = 0;
    let i = 0;
    const next = () => {
      this.revealTimer = null;
      if (this.destroyed) return;
      if (waitingSince) {
        t0 += performance.now() - waitingSince;
        waitingSince = 0;
      }
      while (i < events.length && events[i].at <= performance.now() - t0 + 4) {
        const ev = events[i];
        if (ev.gate && !ev.gate.done) {
          waitingSince = performance.now();
          this.revealTimer = setTimeout(next, GATE_POLL_MS);
          return;
        }
        i += 1;
        ev.fn();
      }
      if (i < events.length) this.revealTimer = setTimeout(next, Math.max(0, events[i].at - (performance.now() - t0)));
      else this.revealEnded();
    };
    next();
  }

  every(ms, fn) {
    const id = setInterval(fn, ms);
    this.timers.add(id);
    return id;
  }

  stopTimers() {
    for (const id of this.timers) clearInterval(id);
    this.timers.clear();
  }

  errorText(err) {
    return this.api.errorText(err, this.t);
  }

  toastError(err) {
    if (this.api.isAbort(err)) return;
    this.ctx.toast({ tone: "danger", title: this.errorText(err) });
  }

  /** checker: a transparent design is drawn on a checkerboard instead of white. */
  thumbUrl(path, w, v, checker) {
    if (!path) return null;
    return this.api.url("/api/files/thumb", checker ? { path, w, v, bg: "checker" } : { path, w, v });
  }

  // ------------------------------------------------------------------ texts

  problemText(problem) {
    const { t } = this;
    if (!problem) return "";
    const key = `problem.${problem.code}`;
    if (problem.code === "stopped") {
      const reason = problem.params && problem.params.reason;
      return t("problem.stopped", { reason: this.stoppedReason({ code: reason, params: problem.params }) });
    }
    if (t.has(key)) return t(key, { ...(problem.params || {}), message: problem.message || "" });
    return problem.message || problem.code;
  }

  /** A digital product's download problem in a few words (the pending tiles). */
  fileProblemShort(problem) {
    // no_deliverable: no "dosyalar" folder at all, or one with nothing in it.
    const empty = problem && problem.code === "no_deliverable" && problem.params && problem.params.missing === false;
    const key = empty ? "pending.file.no_deliverable_empty" : `pending.file.${problem && problem.code}`;
    return this.t.has(key) ? this.t(key, (problem && problem.params) || {}) : this.t("pending.file.other");
  }

  /** Whether the library's own (English) words add anything to the translated line. */
  problemDetail(problem) {
    return !!(problem && problem.message && !SELF_EXPLAINED.has(problem.code) && this.t.has(`problem.${problem.code}`));
  }

  warnText(w) {
    const { t } = this;
    const key = warnKey(w);
    if (t.has(key)) return t(key, { ...(w.params || {}), message: w.message || "" });
    return w.message || w.code;
  }

  stoppedReason(stopped) {
    if (!stopped) return "";
    const key = `stopped.${stopped.code}`;
    return this.t.has(key) ? this.t(key, stopped.params || {}) : stopped.message || stopped.code;
  }

  typeLabel(item) {
    const { t } = this;
    if (!item) return "";
    if (item.mode === "photos" || item.kind === "folder") return t("mode.photos", { n: item.files || item.images.length });
    if (item.mode === "as_is") return t("mode.as_is");
    if (!item.mode) return t("mode.design");
    // A digital download is not the product its mockups show (a shirt, a mug).
    if (this.run && this.run.listingType === "download") return t("mode.digital");
    const primary = this.run && this.run.mockups && this.run.mockups.primary;
    return primary && t.has(`type.${primary}`) ? t(`type.${primary}`) : t("mode.design");
  }

  // ------------------------------------------------------------------ idle view (t210)

  async showIdle() {
    this.view = "idle";
    this.run = null;
    this.selected = null;
    this.expanded = false;
    this.badgeKey = null;
    this.stopTimers();
    this.stopReveal();
    this.setIdleHeader();
    this.renderIdle();
    await Promise.all([this.loadPending(), this.loadLastRun()]);
  }

  /**
   * The video's idle page has no header action. After a run, "Son çalışma" (a ghost
   * button, the only one) opens what it did.
   */
  setIdleHeader() {
    const { t } = this;
    const run = this.lastRun;
    const s = run && run.summary;
    const actions = [];
    if (s) {
      const hint = s.dry_run ? t("last.checked", { n: s.checked, when: relative(s.finished_at) }) : t("last.link", { n: s.created, when: relative(s.finished_at) });
      actions.push(button({ label: t(s.dry_run ? "last.short_checked" : "last.short"), icon: "history", variant: "ghost", title: hint, class: "dz-last-btn", onClick: () => this.showSaved(run) }));
    }
    this.ctx.setHeader({ subtitle: t("subtitle"), actions });
  }

  renderIdle() {
    const { t } = this;
    this.pillStart = null;
    this.dzDecorKey = null; // a fresh zone: its sample tiles are filled in again
    this.dz = dropzone({
      accept: ACCEPT,
      multiple: true,
      class: "dz-drop",
      title: t("drop.title"),
      subtitle: t("drop.sub"),
      content: this.dropContent(),
      onFiles: (list) => this.handleFiles(list),
      onReject: (list) => this.onRejected(list),
    });
    // ui.js marks the zone .is-over; these run after its own handlers and add what the
    // video shows while files hover ("Bırakın!", "50 dosya · yüklemeye hazır").
    this.dz.addEventListener("dragenter", (e) => this.onDragEnter(e));
    this.dz.addEventListener("dragleave", () => this.onDragEnd(false));
    this.dz.addEventListener("drop", () => this.onDragEnd(true));
    this.uploadHost = h("div", { class: "dz-upload-host" });
    this.chipsHost = h("div", { class: "dz-status" }, this.statusRow(null));
    this.noteHost = h("div", { class: "dz-note-host" });
    mount(this.el, this.uploadHost, this.dz, this.chipsHost, this.stepsCard(null), this.noteHost);
  }

  /** The floating sample tiles: the designs waiting in the folder, else the last run's. */
  decorEl(images) {
    const decor = h("div", { class: "dz-decor", "aria-hidden": "true" });
    GHOSTS.forEach((g, i) => {
      const src = images && images[i] ? images[i] : null;
      decor.appendChild(
        h(
          "span",
          {
            class: cx("dz-float", !src && "is-icon"),
            style: {
              "--x": String(g.x),
              "--y": String(g.y),
              "--s": `${g.s}px`,
              "--r": `${g.r}deg`,
              "--o": String(g.o),
              // Dragging pulls the tiles a little toward the middle (YukleEkrani DropZone).
              "--ox": `${Math.round((0.5 - g.x) * 62)}px`,
              "--oy": `${Math.round((0.5 - g.y) * 24)}px`,
            },
          },
          src ? h("img", { src, alt: "", loading: "lazy" }) : icon(GHOST_ICONS[i], { size: Math.round(g.s * 0.34) }),
        ),
      );
    });
    this.dzDecor = decor;
    return decor;
  }

  dropContent(images) {
    const { t } = this;
    // "PNG · şeffaf zemin önerilir · tek seferde 500'e kadar", with the video's dim dots.
    const parts = t("drop.sub").split(" · ");
    const sub = h(
      "p",
      { class: "dz-sub" },
      parts.map((part, i) => [i ? h("span", { class: "dz-dot", "aria-hidden": "true" }, "·") : null, h("span", null, part)]),
    );
    const pickRow = h(
      "div",
      { class: "dz-pick-row" },
      button({
        label: t("drop.pick"),
        icon: "file",
        variant: "secondary",
        size: "lg",
        class: "dz-pick",
        onClick: (e) => {
          e.stopPropagation();
          this.dz.open();
        },
      }),
      h("span", { class: "dz-pick-or" }, t("drop.pick_or")),
    );
    // Visual feedback only: the zone's own label already says what to do.
    this.dragPill = h("div", { class: "dz-pill dz-pill-drag", "aria-hidden": "true" });
    this.upHost = h("div", { class: "dz-up-host" });
    this.dzPendingHost = h("div", { class: "dz-wait-host" });
    return [
      h("span", { class: "dz-grid", "aria-hidden": "true" }),
      this.decorEl(images),
      h(
        "div",
        { class: "dz-body" },
        h(
          "span",
          { class: "dz-icon-wrap", "aria-hidden": "true" },
          h("span", { class: "dz-ring dz-ring-a" }),
          h("span", { class: "dz-ring dz-ring-b" }),
          h("span", { class: "dz-icon" }, icon("upload", { size: 46, strokeWidth: 2 })),
        ),
        h(
          "div",
          { class: "dz-titles" },
          h("p", { class: "dz-title" }, t("drop.title")),
          h("p", { class: "dz-title-over", "aria-hidden": "true" }, t("drop.over")),
        ),
        sub,
        h("div", { class: "dz-slot" }, pickRow, this.dragPill, this.upHost),
        this.dzPendingHost,
      ),
    ];
  }

  /**
   * Files entered the zone. Chrome shows each dragged item's kind and type (a folder is
   * one item with no type) but not the files: "8 dosya" only when all are images.
   */
  onDragEnter(e) {
    if (!this.dz || !this.dz.classList.contains("is-over")) return;
    const n = dragCount(e.dataTransfer && e.dataTransfer.items);
    const key = n === null ? "any" : String(n);
    if (this.dz.dataset.count === key) return;
    this.dz.dataset.count = key;
    const { t } = this;
    const tiles = n === null ? 1 : Math.min(3, n);
    const stack = h(
      "span",
      { class: "dz-pill-stack" },
      Array.from({ length: tiles }, () => h("span", { class: "dz-pill-tile" }, icon(n === null ? "folder" : "image", { size: 15, strokeWidth: 2 }))),
    );
    mount(
      this.dragPill,
      stack,
      n === null
        ? h("strong", { class: "dz-pill-n" }, t("drop.ready_any"))
        : [h("strong", { class: "dz-pill-n num" }, t("drop.ready_n", { n })), h("span", { class: "dz-pill-tail" }, t("drop.ready_tail"))],
    );
  }

  /** ui.js has cleared .is-over (the drag left, or the files were dropped). */
  onDragEnd(dropped) {
    if (!this.dz) return;
    if (dropped || !this.dz.classList.contains("is-over")) delete this.dz.dataset.count;
  }

  setupState(p) {
    if (!p) return { ok: null };
    const setupBlockers = p.blockers.filter((b) => b in SETUP_LINKS);
    return { ok: setupBlockers.length === 0, first: setupBlockers[0] };
  }

  statusRow(p) {
    const { t } = this;
    const chip = (ic, label, value, extra, tone, onClick) =>
      h(
        onClick ? "button" : "div",
        { class: cx("dz-chip", tone && `is-${tone}`), type: onClick ? "button" : undefined, onClick },
        h("span", { class: "dz-chip-icon" }, icon(ic, { size: 16 })),
        label ? h("span", { class: "dz-chip-label" }, label) : null,
        value !== null ? h("strong", { class: "dz-chip-value ellipsis" }, value) : skeleton({ lines: 1, height: 10, widths: ["90px"] }),
        extra || null,
      );
    // "Mockup: 6 hazır": the Mockuplar rule (switched on, in order, at most 19), and
    // always a link there, where the seller chooses and orders them.
    const mockups = p ? p.mockups.enabled : null;
    const over = p && p.mockups.over_limit ? h("span", { class: "dz-chip-extra" }, t("chip.mockup_over", { n: p.mockups.switched_on })) : null;
    const mockupChip = chip("image", t("chip.mockup"), p ? (mockups ? t("chip.mockup_ready", { n: mockups }) : t(p.mockups.total ? "chip.mockup_none" : "chip.mockup_empty")) : null, over, p && !mockups ? "warn" : null, () => this.ctx.navigate("/kurulum/mockuplar"));
    mockupChip.title = t("chip.mockup_hint");
    mockupChip.classList.add("dz-chip-mockup");
    // The template listing's full title is up to 140 characters; the chip shows its
    // first part, as the video does, and the whole title on hover.
    const fullTemplate = p && p.template && p.template.title ? p.template.title : null;
    const template = p ? (fullTemplate && shortTitle(fullTemplate)) || (p.template ? t("chip.template_unnamed") : null) : null;
    const shop = p ? p.shop : null;
    const shopOk = shop && shop.connected;
    const templateChip = chip("file", t("chip.template"), p ? template || t("chip.template_none") : null, null, p && !p.template ? "warn" : null, p && (!p.template || p.blockers.includes("template_invalid")) ? () => this.ctx.navigate("/kurulum/sablon") : null);
    if (fullTemplate) templateChip.title = fullTemplate;
    templateChip.classList.add("dz-chip-template");
    // A digital template's drafts are digital: say so next to the template (a physical
    // one needs no word here, as before).
    const kind = listingType(p && p.template && p.template.listing_type);
    let typeChip = null;
    if (DIGITAL.has(kind)) {
      typeChip = chip("download", t("chip.type"), t(`chip.type_${kind}`), null, null, () => this.ctx.navigate("/kurulum/sablon"));
      typeChip.title = t(`chip.type_${kind}_hint`);
      typeChip.classList.add("dz-chip-type");
    }
    const shopChip = chip(
      "link",
      t("chip.shop"),
      p ? (shop && shop.name) || (shopOk ? t("chip.shop_connected") : t("chip.shop_none")) : null,
      shopOk ? h("span", { class: "dz-chip-ok" }, icon("check", { size: 12, strokeWidth: 3 })) : null,
      p && !shopOk ? "warn" : null,
      p && !shopOk ? () => this.ctx.navigate("/kurulum/magaza") : null,
    );
    shopChip.classList.add("dz-chip-shop");
    const row = [mockupChip, templateChip, typeChip, shopChip];
    const setup = this.setupState(p);
    let right = null;
    if (setup.ok === true) {
      right = h("span", { class: "dz-setup is-ok" }, icon("check", { size: 14, strokeWidth: 2.4 }), t("setup.ok"));
    } else if (setup.ok === false) {
      const link = SETUP_LINKS[setup.first];
      right = h(
        "span",
        { class: "dz-setup is-missing" },
        icon("alert", { size: 13 }),
        t("setup.missing"),
        link ? h("a", { href: link, class: "dz-setup-link" }, t("setup.fix"), icon("arrow-right", { size: 12 })) : null,
      );
    }
    return [h("div", { class: cx("dz-chips", typeChip && "has-type") }, row), h("div", { class: "spacer" }), right];
  }

  stepsCard(p) {
    const { t } = this;
    const n = p ? p.mockups.enabled : null;
    const subs = {
      mockup: n === null ? t("step.mockup_sub", { n: "…" }) : n ? t("step.mockup_sub", { n }) : t("step.mockup_sub_none"),
      research: t("step.research_sub"),
      title: t("step.title_sub"),
      tags: t("step.tags_sub"),
      check: t("step.check_sub"),
      draft: p && p.template && p.template.digital ? t("step.draft_sub_digital") : t("step.draft_sub"),
    };
    const items = [];
    STEPS.forEach((s, i) => {
      if (i) items.push(h("span", { class: "dz-step-arrow", "aria-hidden": "true" }, icon("arrow-right", { size: 16 })));
      items.push(
        h(
          "li",
          { class: "dz-step" },
          h("span", { class: "dz-step-icon" }, icon(STEP_ICONS[s], { size: 18 })),
          h(
            "span",
            { class: "dz-step-text" },
            h("span", { class: "dz-step-name" }, h("span", { class: "dz-step-num" }, String(i + 1)), t(`step.${s}`)),
            h("span", { class: "dz-step-sub" }, subs[s]),
          ),
        ),
      );
    });
    this.stepsEl = h(
      "section",
      { class: "card dz-steps" },
      h(
        "div",
        { class: "dz-steps-head" },
        h("span", { class: "dz-steps-title" }, t("steps.title")),
        h("span", { class: "dz-steps-note" }, icon("lock", { size: 14 }), t("steps.note")),
      ),
      h("ol", { class: "dz-steps-row" }, items),
    );
    return this.stepsEl;
  }

  async loadPending() {
    if (this.view !== "idle") return null;
    try {
      const p = await this.api.get("/api/designs/pending", null, { signal: this.ctx.signal });
      if (this.view !== "idle" || this.destroyed) return p;
      this.pending = p;
      this.renderPendingParts();
      return p;
    } catch (err) {
      if (this.api.isAbort(err)) return null;
      mount(this.noteHost, infoNote({ tone: "danger", icon: "alert", text: this.errorText(err), action: button({ label: this.t("common.retry"), size: "sm", icon: "refresh", onClick: () => this.loadPending() }) }));
      return null;
    }
  }

  async loadLastRun() {
    try {
      const data = await this.api.get("/api/designs/last", null, { signal: this.ctx.signal });
      this.lastRun = data && data.run ? data.run : null;
      if (this.view === "idle" && !this.destroyed) {
        this.setIdleHeader();
        this.renderPendingParts();
      }
    } catch (err) {
      if (!this.api.isAbort(err)) this.lastRun = null;
    }
  }

  renderPendingParts() {
    const p = this.pending;
    if (!p || this.view !== "idle") return;
    const { t } = this;
    mount(this.chipsHost, this.statusRow(p));
    const steps = this.stepsCard(p);
    const old = this.el.querySelector(".dz-steps");
    if (old) old.replaceWith(steps);
    // Floating decoration: the designs waiting in the folder, else the last run's.
    let srcs = p.items.filter((x) => x.thumb_path).slice(0, GHOSTS.length).map((x) => this.thumbUrl(x.thumb_path, 160, x.mtime, isDesign(x)));
    if (!srcs.length && this.lastRun && Array.isArray(this.lastRun.items)) {
      const v = (this.lastRun.summary && this.lastRun.summary.batch) || "last";
      srcs = this.lastRun.items.filter((x) => x.thumb_path && x.status !== "error").slice(0, GHOSTS.length).map((x) => this.thumbUrl(x.thumb_path, 160, v, isDesign(x)));
    }
    if (this.dzDecor && srcs.length && this.dzDecorKey !== srcs.join("|")) {
      this.dzDecorKey = srcs.join("|");
      const fresh = this.decorEl(srcs);
      this.el.querySelector(".dz-decor")?.replaceWith(fresh);
    }

    const notes = [];
    if (p.review && p.review.length) {
      notes.push(
        infoNote({
          tone: "warning",
          icon: "alert",
          text: t("review.note", { n: p.review.length }),
          action: button({ label: t("review.open"), size: "sm", onClick: () => this.openReview() }),
        }),
      );
    }
    if (p.blockers.includes("locked")) {
      notes.push(
        infoNote({
          tone: "warning",
          icon: "lock",
          text: this.lockText(p),
          action: button({ label: t("blocked.unlock"), size: "sm", onClick: () => this.unlock() }),
        }),
      );
    }
    mount(this.noteHost, notes);
    this.renderWaitPill(p);
    if (this.pendingModal) this.renderPendingModal();
  }

  /** Whether a waiting design cannot go as it is (the tile says why, in the modal). */
  pendingProblem(it, digital) {
    return !!(it.junk_reason || it.too_many || it.no_photos || (digital && it.deliverable_problem));
  }

  /**
   * Designs already waiting in the folder (a drop that was not started, or files put
   * there by hand): one pill inside the drop zone, with the problems counted apart and
   * the list in a modal, so the page keeps the video's three blocks.
   */
  renderWaitPill(p) {
    const host = this.dzPendingHost;
    if (!host) return;
    const { t } = this;
    this.pillStart = null;
    if (!p || !p.count) {
      mount(host);
      this.syncStartButtons();
      return;
    }
    const digital = !!(p.template && p.template.digital);
    const bad = p.items.filter((it) => this.pendingProblem(it, digital)).length;
    const stack = h(
      "span",
      { class: "dz-wait-stack" },
      p.items
        .filter((x) => x.thumb_path)
        .slice(0, 3)
        .map((x) => thumb({ src: this.thumbUrl(x.thumb_path, 96, x.mtime, isDesign(x)), size: 26, radius: 7, fit: "contain", icon: x.kind === "folder" ? "folder" : "image" })),
    );
    const open = (e) => {
      e.stopPropagation();
      this.openPending();
    };
    const show = h(
      "button",
      { type: "button", class: "dz-wait-show", "aria-haspopup": "dialog", title: t("pending.show_hint"), onClick: open },
      stack,
      h("span", { class: "dz-wait-text" }, t("pending.pill", { n: p.count })),
    );
    const warn = bad ? h("button", { type: "button", class: "dz-wait-bad", "aria-haspopup": "dialog", title: t("pending.show_hint"), onClick: open }, icon("alert", { size: 13 }), t("pending.problems", { n: bad })) : null;
    this.pillStart = button({
      label: t("ready.start"),
      icon: "zap",
      variant: "primary",
      size: "sm",
      class: "dz-wait-start",
      onClick: (e) => {
        e.stopPropagation();
        this.openStart();
      },
    });
    // The rest of the zone still opens the file picker; the pill itself does not.
    mount(host, h("div", { class: "dz-wait", role: "group", "aria-label": t("pending.title"), onClick: (e) => e.stopPropagation() }, show, warn, this.pillStart));
    this.syncStartButtons();
  }

  /** The waiting designs, one tile each: remove one, open the folder, check or start. */
  openPending() {
    const { t } = this;
    const p = this.pending;
    if (!p || !p.count) return;
    if (this.pendingModal) return;
    const body = h("div", { class: "dz-pending-body" });
    const startBtn = button({
      label: t("ready.start"),
      icon: "zap",
      variant: "primary",
      onClick: () => {
        m.close();
        this.openStart();
      },
    });
    const m = this.ctx.modal({
      title: t("pending.title"),
      subtitle: t("pending.sub", { n: p.count }),
      width: 760,
      class: "dz-pending-modal",
      body,
      actions: [
        button({ label: t("pending.open"), icon: "folder", variant: "ghost", class: "dz-open-folder", onClick: () => this.openFolder("products") }),
        button({
          label: t("ready.dry"),
          variant: "ghost",
          title: t("ready.dry_hint"),
          onClick: () => {
            m.close();
            this.startDry();
          },
        }),
        startBtn,
      ],
      onClose: () => {
        if (this.pendingModal && this.pendingModal.m === m) this.pendingModal = null;
        this.syncStartButtons();
      },
    });
    this.pendingModal = { m, body, startBtn };
    this.renderPendingModal();
    this.syncStartButtons();
  }

  renderPendingModal() {
    const pm = this.pendingModal;
    const p = this.pending;
    if (!pm) return;
    if (!p || !p.count) {
      pm.m.close();
      return;
    }
    const sub = pm.m.el.querySelector(".modal-sub");
    if (sub) sub.textContent = this.t("pending.sub", { n: p.count });
    mount(pm.body, h("div", { class: "dz-tiles" }, this.pendingTiles(p)));
  }

  pendingTiles(p) {
    const { t } = this;
    const items = p.items;
    const shown = this.pendingExpanded ? items : items.slice(0, PENDING_TILES);
    const digital = !!(p.template && p.template.digital);
    const tiles = shown.map((it) => {
      const fileProblem = digital ? it.deliverable_problem : null;
      const bad = this.pendingProblem(it, digital);
      const downloads = it.deliverables || [];
      let sub;
      if (bad) {
        const why = it.junk_reason ? t("pending.junk") : it.too_many ? t("pending.too_many") : it.no_photos ? t("pending.no_photos") : this.fileProblemShort(fileProblem);
        sub = h("span", { class: "dz-tile-bad" }, icon("alert", { size: 11 }), why);
      } else if (digital && it.kind === "folder") {
        sub = t("pending.folder_files", { n: it.files, m: downloads.length });
      } else if (digital && downloads.length) {
        // (A made-to-order template attaches no loose design: then it is a plain tile.)
        sub = h("span", { class: "dz-tile-dl" }, icon("download", { size: 11 }), bytes(it.size));
      } else {
        sub = it.kind === "folder" ? t("pending.folder", { n: it.files }) : bytes(it.size);
      }
      // Hover: what a buyer would download, or why it cannot go.
      const tip = [it.name];
      if (it.no_photos) tip.push(this.problemText({ code: "no_photos", params: { name: it.name, folder: "dosyalar" } }));
      else if (fileProblem) tip.push(this.problemText({ code: fileProblem.code, params: fileProblem.params }));
      else if (digital && downloads.length) tip.push(`${t("pending.downloads")}: ${downloads.map((d) => d.name).join(", ")}`);
      return h(
        "div",
        { class: cx("dz-tile", bad && "is-bad"), title: tip.join("\n") },
        thumb({ src: this.thumbUrl(it.thumb_path, 200, it.mtime, isDesign(it)), size: 64, radius: 10, fit: "contain" }),
        h("span", { class: "dz-tile-name ellipsis" }, it.name),
        h("span", { class: "dz-tile-sub" }, sub),
        iconButton({
          icon: "x",
          title: t("pending.remove"),
          variant: "ghost",
          size: "sm",
          class: "dz-tile-x",
          onClick: (e) => {
            e.stopPropagation();
            this.removePending(it);
          },
        }),
      );
    });
    const more = items.length - shown.length;
    if (more > 0 || this.pendingExpanded) {
      tiles.push(
        h(
          "button",
          {
            type: "button",
            class: "dz-tile dz-tile-more",
            onClick: () => {
              this.pendingExpanded = !this.pendingExpanded;
              this.renderPendingModal();
            },
          },
          more > 0 ? t("pending.more", { n: more }) : t("pending.less"),
        ),
      );
    }
    return tiles;
  }

  /** Başlat waits while files are still uploading: a run started now would miss them. */
  syncStartButtons() {
    const busy = !!this.uploading;
    const list = [this.pillStart, this.pendingModal && this.pendingModal.startBtn].filter(Boolean);
    for (const b of list) {
      b.setDisabled(busy);
      b.title = busy ? this.t("ready.wait_upload") : "";
    }
  }

  async openFolder(which) {
    try {
      await this.api.post("/api/open-folder", { which });
    } catch (err) {
      this.toastError(err);
    }
  }

  async removePending(it) {
    try {
      await this.api.del(this.api.url("/api/designs/files", { path: it.name }));
      this.ctx.toast({ tone: "info", title: this.t("pending.removed", { name: it.name }) });
    } catch (err) {
      this.toastError(err);
    }
    await this.loadPending();
  }

  // ------------------------------------------------------------------ dropping files

  /** Whether the template makes digital drafts: only then is "dosyalar" a download folder. */
  isDigital() {
    return !!(this.pending && this.pending.template && this.pending.template.digital);
  }

  /**
   * Files the drop zone turned away (it takes images only). A digital product's download
   * files — a PDF or ZIP in a dropped folder's "dosyalar" / "files" subfolder — are kept
   * and go up with the same drop (onFiles follows this call at once); the rest are said.
   * With a physical template a "dosyalar" folder means nothing special: its non-images
   * are turned away like any other.
   */
  onRejected(list) {
    const { t } = this;
    const digital = this.isDigital();
    // Download files, and files nested deeper in "dosyalar" (handleFiles says why those
    // do not go, once, together with any nested images of the same drop).
    const keep = list.filter((x) => x && x.file && downloadOf(x.path, digital));
    const other = list.filter((x) => !(x && x.file && downloadOf(x.path, digital)));
    if (other.length) {
      this.ctx.toast({ tone: "warning", title: t("upload.rejected", { n: other.length }), message: other.slice(0, 3).map((x) => baseName(x.path)).join(", ") });
    }
    if (!keep.length) return;
    this.stashed = keep;
    queueMicrotask(() => {
      // Only download files were dropped (no image came with them): upload them alone.
      if (this.stashed) this.handleFiles([]);
    });
  }

  /** Files deeper inside "dosyalar" than its own files: Etsy takes files, not folders. */
  toastNested(list) {
    this.ctx.toast({ tone: "warning", title: this.t("upload.nested_downloads", { n: list.length }), message: list.slice(0, 3).map((x) => x.path).join(", ") });
  }

  async handleFiles(list) {
    const { t } = this;
    const digital = this.isDigital();
    let files = list.filter((x) => x && x.file);
    if (this.stashed) {
      files = files.concat(this.stashed);
      this.stashed = null;
    }
    if (this.uploading) return;
    // Images nested inside a "dosyalar" subfolder are never a product of their own.
    const nested = files.filter((x) => (downloadOf(x.path, digital) || {}).nested);
    if (nested.length) {
      this.toastNested(nested);
      files = files.filter((x) => !(downloadOf(x.path, digital) || {}).nested);
    }
    if (!files.length) return;
    if (files.length > MAX_FILES) {
      this.ctx.toast({ tone: "warning", title: t("upload.too_many", { n: MAX_FILES }) });
      files = files.slice(0, MAX_FILES);
    }
    const hasFolders = files.some((x) => (x.path || "").includes("/"));
    let mode = "designs";
    if (hasFolders) {
      mode = await this.chooseFolderMode(files);
      if (!mode) return;
    }
    const entries = planUploads(files, mode, digital);
    const skipped = mode === "products" ? 0 : files.filter((x) => downloadOf(x.path, digital)).length;
    if (skipped) this.ctx.toast({ tone: "warning", title: t("upload.downloads_skipped", { n: skipped }) });
    if (!entries.length) return;
    await this.upload(entries);
  }

  chooseFolderMode(files) {
    const { t } = this;
    const digital = this.isDigital();
    const dirs = new Map();
    let downloads = 0;
    for (const x of files) {
      const dl = downloadOf(x.path, digital);
      if (dl) {
        downloads += 1;
        // Its product folder counts as a folder of the drop, photos or not.
        const parts = (x.path || "").split("/").filter(Boolean);
        const dir = parts.slice(0, -2).join("/");
        if (!dirs.has(dir)) dirs.set(dir, 0);
        continue;
      }
      const parts = (x.path || "").split("/").filter(Boolean);
      if (parts.length > 1) {
        const dir = parts.slice(0, -1).join("/");
        dirs.set(dir, (dirs.get(dir) || 0) + 1);
      }
    }
    const big = [...dirs.values()].some((n) => n > MAX_PHOTOS);
    return new Promise((resolve) => {
      // Download files in a "dosyalar" folder only make sense for product folders.
      let choice = downloads ? "products" : "designs";
      let result = null;
      const option = (id, title, sub, ic) =>
        h(
          "label",
          { class: cx("dz-option", id === choice && "is-selected"), dataset: { id } },
          h("input", {
            type: "radio",
            name: "dz-folder-mode",
            value: id,
            checked: id === choice,
            onChange: () => {
              choice = id;
              for (const o of box.querySelectorAll(".dz-option")) o.classList.toggle("is-selected", o.dataset.id === id);
            },
          }),
          h("span", { class: "dz-option-icon" }, icon(ic, { size: 16 })),
          h("span", { class: "dz-option-text" }, h("strong", null, title), h("span", null, sub)),
        );
      const box = h(
        "div",
        { class: "dz-options" },
        option("designs", t("folders.designs"), t("folders.designs_sub"), "image"),
        option("products", t("folders.products"), t("folders.products_sub"), "folder"),
      );
      const m = this.ctx.modal({
        title: t("folders.title"),
        subtitle: t("folders.summary", { folders: dirs.size, files: files.length }),
        width: 500,
        body: [
          box,
          big ? h("p", { class: "dz-modal-hint" }, t("folders.big", { n: MAX_PHOTOS })) : null,
          downloads ? h("p", { class: "dz-modal-hint" }, icon("download", { size: 13 }), " ", t("folders.downloads", { n: downloads })) : null,
        ],
        actions: [
          { label: t("common.cancel"), variant: "secondary" },
          {
            label: t("folders.upload"),
            variant: "primary",
            icon: "upload",
            onClick: ({ close }) => {
              result = choice;
              close();
            },
          },
        ],
        onClose: () => resolve(result),
      });
      return m;
    });
  }

  /** The object URLs of an upload's preview tiles, freed once they are off screen. */
  revokePreviews(state) {
    for (const url of state.previews || []) URL.revokeObjectURL(url);
    state.previews = [];
  }

  /**
   * The drop zone stays where it is (video t208): the upload shows as one pill in it,
   * "[3 tiles] 3 / 8 dosya · yükleniyor", with its Durdur. The start card follows.
   * Files that did not go in are listed afterwards, with the reason, above the zone.
   */
  async upload(entries) {
    const { t } = this;
    const batch = randomId();
    const controller = new AbortController();
    const state = {
      controller,
      rows: entries.map((e) => ({ ...e, status: "queued", fraction: 0, message: "" })),
      done: 0,
      previews: [],
    };
    this.uploading = state;
    this.ctx.setDirty(true); // a reload or a closed tab would cut the upload off: ask
    this.syncStartButtons();
    mount(this.uploadHost); // an earlier upload's list of problems
    const total = state.rows.length;
    const pictures = state.rows.filter((r) => !r.deliverable && /^image\//.test(r.file.type || "")).slice(0, 3);
    state.previews = pictures.map((r) => URL.createObjectURL(r.file));
    const countEl = h("strong", { class: "dz-pill-n num" });
    const fill = h("span", { class: "dz-pill-fill" });
    const pill = h(
      "div",
      {
        class: "dz-pill dz-pill-up",
        role: "progressbar",
        "aria-label": t("upload.title"),
        "aria-valuemin": "0",
        "aria-valuemax": String(total),
        "aria-valuenow": "0",
        "aria-valuetext": t("upload.progress", { done: 0, total }),
      },
      h(
        "span",
        { class: "dz-pill-stack" },
        state.previews.length
          ? state.previews.map((src) => h("span", { class: "dz-pill-tile is-image" }, h("img", { src, alt: "" })))
          : h("span", { class: "dz-pill-tile" }, icon("file", { size: 15, strokeWidth: 2 })),
      ),
      countEl,
      h("span", { class: "dz-pill-tail" }, t("upload.pill_tail")),
      h("span", { class: "dz-pill-track", "aria-hidden": "true" }, fill),
    );
    const stopBtn = button({
      label: t("upload.cancel"),
      size: "sm",
      variant: "ghost",
      class: "dz-pill-stop",
      onClick: (e) => {
        e.stopPropagation();
        controller.abort();
      },
    });
    if (this.upHost) mount(this.upHost, h("div", { class: "dz-up-pill", onClick: (e) => e.stopPropagation() }, pill, stopBtn));
    if (this.dz) {
      this.dz.setDisabled(true);
      this.dz.classList.add("is-uploading");
    }
    const refresh = () => {
      let parts = 0;
      for (const r of state.rows) parts += r.status === "queued" ? 0 : r.status === "uploading" ? r.fraction : 1;
      countEl.textContent = t("upload.progress", { done: state.done, total });
      fill.style.width = `${Math.round((parts / Math.max(total, 1)) * 100)}%`;
      pill.setAttribute("aria-valuenow", String(state.done));
      pill.setAttribute("aria-valuetext", countEl.textContent);
    };
    refresh();

    let next = 0;
    const worker = async (limit) => {
      while (next < limit && !controller.signal.aborted && !this.destroyed) {
        const i = next;
        next += 1;
        const r = state.rows[i];
        if (r.file.size > MAX_BYTES) {
          r.status = "error";
          r.message = t("upload.too_large", { mb: 50 });
        } else {
          r.status = "uploading";
          try {
            const res = await this.api.upload("/api/designs/files", r.file, {
              // A download for a product already in the folder goes without the batch.
              query: r.attach ? { path: r.path } : { path: r.path, batch },
              signal: controller.signal,
              onProgress: ({ fraction }) => {
                r.fraction = fraction;
                refresh();
              },
            });
            r.result = res;
            r.status = res.known ? "known" : res.ignored ? "ignored" : res.duplicate ? "duplicate" : res.replaced ? "replaced" : "done";
          } catch (err) {
            if (this.api.isAbort(err)) {
              r.status = "skipped";
            } else {
              r.status = "error";
              r.message = this.errorText(err);
            }
          }
        }
        state.done += 1;
        refresh();
      }
    };
    // Every photo is in before the first download file starts (planUploads puts them
    // last): a product folder's batch claim is always made by a photo, never by a small
    // PDF that overtook it and would open a "-2" copy of the product.
    const pool = (limit) => Promise.all(Array.from({ length: Math.max(0, Math.min(UPLOAD_PARALLEL, limit - next)) }, () => worker(limit)));
    const firstDownload = state.rows.findIndex((r) => r.deliverable);
    if (firstDownload > 0) await pool(firstDownload);
    await pool(state.rows.length);
    for (const r of state.rows) if (r.status === "queued") r.status = "skipped";
    this.uploading = null;
    this.revokePreviews(state);
    if (this.destroyed) return;
    this.ctx.setDirty(false);
    this.syncStartButtons();
    if (this.upHost) mount(this.upHost);
    if (this.dz) {
      this.dz.setDisabled(false);
      this.dz.classList.remove("is-uploading");
    }
    if (this.view !== "idle") return;

    const saved = state.rows.filter((r) => r.status === "done" || r.status === "duplicate" || r.status === "replaced").length;
    const problems = state.rows.filter((r) => ["error", "known", "ignored", "skipped"].includes(r.status));
    if (problems.length) mount(this.uploadHost, this.uploadProblems(state.rows, problems, saved));
    const p = await this.loadPending();
    if (p && saved > 0 && !controller.signal.aborted) this.openStart(p);
  }

  /** What did not go in, and why (a failed, known, preview-named or stopped file). */
  uploadProblems(rows, problems, saved) {
    const { t } = this;
    const count = (s) => rows.filter((r) => r.status === s).length;
    const failed = count("error");
    const known = count("known");
    const skipped = count("skipped");
    const text = {
      known: t("upload.known"),
      ignored: t("upload.ignored"),
      skipped: t("upload.skipped"),
    };
    const list = h(
      "div",
      { class: "dz-up-list" },
      problems.map((r) => {
        const why = r.status === "error" ? r.message : text[r.status];
        return h(
          "div",
          { class: cx("dz-up-row", `is-${r.status}`) },
          icon(r.deliverable ? "download" : "file", { size: 14 }),
          h("span", { class: "dz-up-name ellipsis", title: r.label }, r.label),
          h("span", { class: "dz-up-size num" }, bytes(r.file.size)),
          h("span", { class: "dz-up-status", title: why }, r.status === "error" ? icon("alert", { size: 12 }) : null, h("span", { class: "ellipsis" }, why)),
        );
      }),
    );
    const panel = h(
      "section",
      { class: "card dz-up", "aria-label": t("upload.problems_title") },
      h(
        "div",
        { class: "dz-up-head" },
        h("span", { class: cx("dz-up-done", failed ? "has-errors" : !saved && "is-none") }, icon(failed ? "alert" : saved ? "check" : "info", { size: 15 })),
        h("strong", null, saved ? t("upload.done", { n: saved }) : t("upload.none")),
        failed ? h("span", { class: "dz-up-failed" }, t("upload.failed", { n: failed })) : null,
        known ? h("span", { class: "dz-up-known" }, t("upload.known_n", { n: known })) : null,
        skipped ? h("span", { class: "dz-up-known" }, t("upload.skipped_n", { n: skipped })) : null,
        h("span", { class: "spacer" }),
        button({ label: t("common.close"), size: "sm", variant: "ghost", onClick: () => mount(this.uploadHost) }),
      ),
      list,
    );
    return panel;
  }

  // ------------------------------------------------------------------ starting a run

  /**
   * given: the pending view (read afresh when null). opaque: the seller's earlier choice
   * for loose designs on a solid background, kept when the card opens again.
   */
  async openStart(given, { refreshSections = false, opaque: chosen = null } = {}) {
    const { t } = this;
    if (this.uploading) {
      this.ctx.toast({ tone: "info", title: t("ready.wait_upload") });
      return;
    }
    let p = given;
    if (!p) {
      p = await this.loadPending();
      if (!p) return;
    }
    const blockers = p.blockers;
    if (blockers.length) {
      this.openBlocked(p, blockers);
      return;
    }
    // The shop section every draft of this run goes into: read while the card opens.
    const section = this.sectionField({ refresh: refreshSections });
    const notes = [];
    const kind = listingType(p.template && p.template.listing_type);
    const digital = DIGITAL.has(kind);
    // What the drafts will be: a digital template's are digital, with a download file
    // each (the design itself; a folder's "dosyalar"). Physical runs read as before.
    if (digital) {
      const folders = p.items.some((x) => x.kind === "folder" && !x.junk_reason && !x.too_many);
      notes.push(
        h(
          "div",
          { class: "dz-modal-type" },
          h("span", { class: "dz-modal-type-icon" }, icon("download", { size: 15 })),
          h(
            "div",
            { class: "dz-modal-type-text" },
            h("span", { class: "dz-modal-type-main" }, h("strong", null, t(`ready.type_${kind}`)), " · ", t(`ready.type_${kind}_sub`)),
            folders ? h("span", { class: "dz-modal-type-more" }, t("ready.type_folders", { max: MAX_DOWNLOADS })) : null,
          ),
        ),
      );
    }
    // "Filigran: açık · dijital ürünlerde" (or off): the watermark the run stamps on the
    // listing photos, set on the Mockuplar page.
    const wmLine = this.watermarkLine(p, () => m.close());
    if (wmLine) notes.push(wmLine);
    const junk = p.items.filter((x) => x.junk_reason || x.too_many).length;
    if (junk) notes.push(infoNote({ tone: "warning", icon: "alert", text: t("ready.junk", { n: junk }) }));
    const noPhotos = p.items.filter((x) => x.no_photos && !x.junk_reason).length;
    if (noPhotos) notes.push(infoNote({ tone: "warning", icon: "image", text: t("ready.no_photos", { n: noPhotos }) }));
    const noFiles = digital ? p.items.filter((x) => !x.junk_reason && !x.too_many && !x.no_photos && x.deliverable_problem).length : 0;
    if (noFiles) notes.push(infoNote({ tone: "warning", icon: "download", text: t("ready.deliverables", { n: noFiles }) }));
    if (p.warnings.includes("no_mockups")) notes.push(infoNote({ tone: "warning", icon: "image", text: t("ready.no_mockups") }));
    if (p.warnings.includes("no_shipping_profile")) notes.push(infoNote({ tone: "warning", icon: "truck", text: t("ready.no_shipping") }));
    // Sentences about the template listing's own design would go onto every draft: said
    // once here, with the way to Şablon İlan's description template (opened on arrival).
    if (p.warnings.includes("description_flagged")) {
      const n = (p.template && p.template.description_flags) || 1;
      const toTemplate = button({
        label: t("ready.go_template"),
        size: "sm",
        iconRight: "arrow-right",
        onClick: () => {
          m.close();
          this.ctx.navigate("/kurulum/sablon?aciklama=1");
        },
      });
      notes.push(infoNote({ tone: "warning", icon: "file", text: t("ready.description_flagged", { n }), action: toTemplate }));
    }
    // The request estimate only matters when the day's Etsy allowance may run out.
    if (p.warnings.includes("quota")) {
      const estimate =
        typeof p.quota_remaining === "number"
          ? t("ready.estimate", { n: number(p.estimate_requests), left: number(p.quota_remaining) })
          : t("ready.estimate_nq", { n: number(p.estimate_requests) });
      notes.push(infoNote({ tone: "warning", icon: "clock", text: [t("ready.quota"), " ", h("span", { class: "dz-modal-hint" }, estimate)] }));
    }
    const templateTitle = (p.template && p.template.title && shortTitle(p.template.title)) || t("chip.template_unnamed");
    const mk = p.mockups;
    // The shop's info images (Şablon İlan) end every draft and take their room of the 20.
    const infoN = (p.info_images && p.info_images.count) || 0;
    const infoText = infoN ? t("ready.info_part", { n: infoN }) : "";
    // More mockups switched on than Etsy's 20 images leave room for: say which go.
    if (mk.enabled && mk.over_limit) {
      notes.push(infoNote({ tone: "warning", icon: "image", text: infoN ? t("ready.mockups_over_info", { n: mk.enabled, on: mk.switched_on, info: infoText }) : t("ready.mockups_over", { n: mk.enabled, on: mk.switched_on }) }));
    }
    // Product folders whose own photos leave room for only some of them.
    if (p.warnings.includes("info_cut")) notes.push(infoNote({ tone: "warning", icon: "image", text: t("ready.info_cut", { n: p.info_images.cut }) }));
    const typeName = (type) => (t.has(`type.${type}`) ? t(`type.${type}`) : t("type.other"));
    const toMockups = () => button({ label: t("ready.go_mockups"), size: "sm", iconRight: "arrow-right", onClick: () => { m.close(); this.ctx.navigate("/kurulum/mockuplar"); } });
    // A download's photos show no physical product: the run leaves those mockups out and
    // the card names the ones it uses.
    if (kind === "download") {
      const left = mk.left_out || [];
      if (left.length) {
        const types = [...new Set(left.map((x) => typeName(x.type)))].join(", ");
        notes.push(infoNote({ tone: "warning", icon: "image", text: t("ready.digital_physical", { n: left.length, types }) }));
      }
      // Named the way Mockuplar names them ("Poster · Meşe"); a mockup of type "other" by
      // its file name without the extension.
      const colorName = (c) => (c && t.has(`color.${c}`) ? t(`color.${c}`) : c || "");
      const labelOf = (m) => {
        const base = m.type === "other" ? String(m.name || "").replace(/\.[^.]+$/, "") : typeName(m.type);
        const color = colorName(m.color);
        return color ? `${base} · ${color}` : base;
      };
      const labels = mk.used_items ? mk.used_items.map(labelOf) : mk.names || [];
      notes.push(h("p", { class: "dz-modal-hint dz-ready-names" }, labels.length ? t("ready.digital_mockups", { names: labels.join(", ") }) : t("ready.digital_flat_only")));
    } else {
      // One template sets every draft's product: a main image of another product (a mug
      // template, a T-shirt first on Mockuplar) makes every draft contradict itself.
      const product = p.template && p.template.product;
      const main = mk.main_type;
      if (product && main && main !== "other" && main !== product) {
        notes.push(infoNote({ tone: "warning", icon: "image", text: t("ready.main_mismatch", { template: typeName(product), main: typeName(main) }), action: toMockups() }));
      }
    }
    // Loose designs with nothing to see through: up as they are (a finished photo), or,
    // on a solid background, onto the mockups with that background removed. The seller
    // picks for the batch; the choice is offered only when such a design is there.
    const op = (!digital && p.opaque) || {};
    const flat = op.flat || 0;
    const photos = op.photo || 0;
    let opaque = chosen === "place" || chosen === "as_is" ? chosen : flat ? "place" : "as_is";
    if (flat || photos) {
      const said = h("div", { class: "dz-ready-opaque-note" });
      const say = () =>
        mount(
          said,
          infoNote({
            tone: "warning",
            icon: "image",
            text: opaque === "place" ? [t("ready.opaque_place", { n: flat }), photos ? ` ${t("ready.opaque_photos", { n: photos })}` : ""] : t("ready.opaque_as_is", { n: flat + photos }),
          }),
        );
      say();
      notes.push(said);
      if (flat) {
        const option = (id, title, sub, ic) =>
          h(
            "label",
            { class: cx("dz-option", id === opaque && "is-selected"), dataset: { id } },
            h("input", {
              type: "radio",
              name: "dz-opaque",
              value: id,
              checked: id === opaque,
              onChange: () => {
                opaque = id;
                for (const o of box.querySelectorAll(".dz-option")) o.classList.toggle("is-selected", o.dataset.id === id);
                say();
              },
            }),
            h("span", { class: "dz-option-icon" }, icon(ic, { size: 16 })),
            h("span", { class: "dz-option-text" }, h("strong", null, title), h("span", null, sub)),
          );
        const box = h(
          "div",
          { class: "dz-options dz-ready-opaque", role: "radiogroup", "aria-label": t("ready.opaque_label") },
          option("place", t("ready.opaque.place"), t("ready.opaque.place_sub"), "layers"),
          option("as_is", t("ready.opaque.as_is"), t("ready.opaque.as_is_sub"), "image"),
        );
        notes.push(box);
      }
    }
    notes.push(section.el);
    // The video's start card (t210): a title, one line and Başlat. No ×: Escape or a
    // click outside still closes it. Başlat waits for the sections (a spinner) so the
    // run gets the choice the select shows.
    const m = this.ctx.modal({
      title: t("ready.title", { n: p.runnable ?? p.count }),
      subtitle: infoN
        ? t("ready.sub_info", { n: mk.enabled, mockups: mk.enabled, info: infoText, template: templateTitle })
        : t("ready.sub", { n: mk.enabled, mockups: mk.enabled, template: templateTitle }),
      width: 456,
      class: "dz-ready",
      body: notes,
      onClose: () => section.close(),
      actions: [
        button({
          label: t("ready.start"),
          icon: "zap",
          variant: "primary",
          size: "lg",
          class: "dz-ready-start",
          autoLoading: true,
          onClick: async () => {
            const choice = await section.value();
            if (section.closed) return;
            m.close();
            this.startRun(false, { opaque, section: choice });
          },
        }),
      ],
    });
  }

  /**
   * The start card's watermark line (the pending view's `watermark`, drop.watermark
   * describe): "Filigran: açık · dijital ürünlerde", "(bu şablon fiziksel, eklenmez)" when
   * this template's run stamps nothing, or "Filigran: kapalı"; "Ayarla" opens its card.
   */
  watermarkLine(p, close) {
    const { t } = this;
    const w = p && p.watermark;
    if (!w) return null;
    const on = !!w.on;
    const parts = [h("strong", null, t("ready.wm_label")), " ", t(on ? "ready.wm_on" : "ready.wm_off")];
    if (on) {
      parts.push(" · ", t(w.scope === "all" ? "ready.wm_scope_all" : "ready.wm_scope_digital"));
      if (w.applies === false) parts.push(h("span", { class: "dz-modal-wm-skip" }, ` (${t("ready.wm_not_this")})`));
    }
    const path = "/kurulum/mockuplar#filigran";
    return h(
      "div",
      { class: cx("dz-modal-wm", on && w.applies !== false && "is-on") },
      h("span", { class: "dz-modal-wm-icon" }, icon("droplet", { size: 14 })),
      h("span", { class: "dz-modal-wm-text" }, parts),
      h(
        "a",
        {
          class: "dz-modal-wm-edit",
          href: path,
          onClick: (e) => {
            e.preventDefault();
            close();
            this.ctx.navigate(path);
          },
        },
        t("ready.wm_edit"),
      ),
    );
  }

  /**
   * The start card's "Mağaza bölümü": the shop's sections, starting on the template's
   * (or this shop's last choice). {el, value(): Promise<"template" | "none" | id>, close()}.
   * Notes under it: a shop without sections, a template or remembered section that is
   * gone, sections that cannot be read now (the template's is used then).
   */
  sectionField({ refresh = false } = {}) {
    const { t } = this;
    const id = uid("dz-section");
    const sel = select({
      options: [{ value: "template", label: t("ready.section.loading") }],
      value: "template",
      disabled: true,
      class: "dz-section-select",
    });
    sel.input.id = id;
    sel.classList.add("is-loading");
    const note = h("div", { class: "dz-section-note" });
    const el = h(
      "div",
      { class: "dz-ready-section" },
      h("label", { class: "dz-section-label", for: id }, icon("store", { size: 15 }), h("span", null, t("ready.section_label"))),
      sel,
      note,
    );
    const controller = new AbortController();
    const state = { closed: false, data: null };
    const say = () => {
      const lines = sectionNotes(state.data, sel.value).map(([kind, key, params]) =>
        kind === "hint" ? h("p", { class: "dz-modal-hint" }, t(key, params)) : infoNote({ tone: "warning", icon: "alert", text: t(key, params) }),
      );
      mount(note, lines);
      note.hidden = !lines.length;
    };
    const fill = (d) => {
      state.data = d;
      const { options, start } = sectionChoices(d, t);
      sel.setOptions(options, start);
      sel.input.disabled = options.length < 2;
      sel.classList.toggle("is-disabled", options.length < 2);
      sel.classList.remove("is-loading");
      say();
    };
    sel.input.addEventListener("change", say);
    note.hidden = true;
    const loaded = this.api
      .get("/api/designs/sections", refresh ? { refresh: 1 } : null, { signal: controller.signal })
      .then(fill, (err) => {
        if (!this.api.isAbort(err)) fill(null);
      });
    return {
      el,
      get closed() {
        return state.closed;
      },
      async value() {
        await loaded;
        return sectionValue(sel.value);
      },
      close() {
        state.closed = true;
        controller.abort();
      },
    };
  }

  /** "Yalnızca kontrol et" (the waiting designs' modal): every step but the draft. */
  async startDry() {
    if (this.uploading) {
      this.ctx.toast({ tone: "info", title: this.t("ready.wait_upload") });
      return;
    }
    const p = await this.loadPending();
    if (!p) return;
    if (p.blockers.length) {
      this.openBlocked(p, p.blockers);
      return;
    }
    await this.startRun(true);
  }

  openBlocked(p, blockers) {
    const { t } = this;
    let m = null;
    const go = (path) => () => {
      m.close();
      this.ctx.navigate(path);
    };
    const rows = blockers.map((b) => {
      let action = null;
      if (SETUP_LINKS[b]) action = button({ label: t(SETUP_LINKS[b] === "/kurulum/sablon" ? "blocked.go_template" : "blocked.go_connect"), size: "sm", iconRight: "arrow-right", onClick: go(SETUP_LINKS[b]) });
      else if (b === "locked") action = button({ label: t("blocked.unlock"), size: "sm", onClick: () => { m.close(); this.unlock(); } });
      else if (b === "offline") action = button({ label: t("blocked.retry"), size: "sm", icon: "refresh", autoLoading: true, onClick: async () => { await this.ctx.refreshStatus(true).catch(() => null); m.close(); this.openStart(); } });
      else if (b === "running") action = button({ label: t("blocked.show_run"), size: "sm", onClick: async () => { m.close(); await this.showActive(); } });
      else if (b === "watermark") action = button({ label: t("blocked.go_watermark"), size: "sm", iconRight: "arrow-right", onClick: go("/kurulum/mockuplar#filigran") });
      const text = b === "template_invalid" ? t("blocked.template_invalid", { message: (p.template && p.template.invalid) || "" }) : b === "locked" ? this.lockText(p) : t(`blocked.${b}`);
      return infoNote({ tone: b === "empty" ? "info" : "warning", icon: b === "empty" ? "info" : "alert", text, action });
    });
    const onlyEmpty = blockers.length === 1 && (blockers[0] === "empty" || blockers[0] === "junk_only");
    m = this.ctx.modal({
      title: onlyEmpty ? t("blocked.empty_title") : t("blocked.title"),
      width: 520,
      body: rows,
      actions: [{ label: t("common.close"), variant: "secondary" }],
    });
  }

  async showActive() {
    try {
      const jobs = await this.api.get("/api/jobs", { kind: "designs", active: 1 });
      if (jobs && jobs.length) await this.showJob(jobs[0].id);
    } catch (err) {
      this.toastError(err);
    }
  }

  /** The lock note: a crashed run's lock, or one a running process still holds. */
  lockText(p) {
    const lock = p && p.lock;
    if (lock && lock.stale) return this.t("blocked.locked_stale");
    if (lock && lock.alive && lock.pid) return this.t("blocked.locked_active", { pid: lock.pid });
    return this.t("blocked.locked");
  }

  async unlock() {
    const { t } = this;
    const lock = this.pending && this.pending.lock;
    const active = !!(lock && lock.alive && lock.pid);
    // A lock whose process still runs gets the stronger question straight away.
    const ok = await this.ctx.confirm(
      active
        ? { title: t("unlock.active_title"), message: t("unlock.active_msg", { pid: lock.pid }), confirmLabel: t("blocked.unlock"), danger: true }
        : { title: t("unlock.title"), message: t("unlock.msg"), confirmLabel: t("blocked.unlock"), danger: true },
    );
    if (!ok) return;
    try {
      try {
        await this.api.post("/api/designs/unlock", active ? { confirm: true, force: true } : { confirm: true });
      } catch (err) {
        if (!err || err.code !== "lock_active") throw err;
        // The process that holds the lock still runs (or its id went to another program).
        const sure = await this.ctx.confirm({ title: t("unlock.active_title"), message: t("unlock.active_msg", { pid: (err.params && err.params.pid) || "?" }), confirmLabel: t("blocked.unlock"), danger: true });
        if (!sure) return;
        await this.api.post("/api/designs/unlock", { confirm: true, force: true });
      }
      this.ctx.toast({ tone: "success", title: t("unlock.done") });
    } catch (err) {
      this.toastError(err);
    } finally {
      await this.loadPending();
    }
  }

  /**
   * opaque: what becomes of loose designs with nothing to see through ("as_is" | "place").
   * section: the start card's shop section ("template" | "none" | a section id).
   */
  async startRun(dryRun, { opaque, section } = {}) {
    this.starting = true;
    try {
      const body = { dry_run: !!dryRun };
      if (opaque === "place" || opaque === "as_is") body.opaque = opaque;
      if (section === "template" || section === "none" || (Number.isInteger(section) && section > 0)) body.section = section;
      const job = await this.api.post("/api/designs/start", body);
      await this.showJob(job.id, job);
    } catch (err) {
      if (err && err.code === "section_gone") {
        // Deleted on Etsy since the card opened: the card again, with the list read afresh
        // and the seller's other choices kept.
        this.ctx.toast({ tone: "warning", title: this.errorText(err) });
        this.openStart(null, { refreshSections: true, opaque });
        return;
      }
      if (err && err.code === "setup_incomplete" && this.pending) {
        const blockers = (err.params && err.params.blockers) || [];
        await this.loadPending();
        this.openBlocked(this.pending || { blockers, template: null }, blockers);
        return;
      }
      this.toastError(err);
    } finally {
      this.starting = false;
    }
  }

  // ------------------------------------------------------------------ review (history) items

  openReview() {
    const { t } = this;
    const p = this.pending;
    if (!p || !p.review || !p.review.length) return;
    let m = null;
    const rows = p.review.map((r) => {
      // The library's own words (English) are only the muted detail; what happened is
      // said with a translated code: partial / drafted / uncertain.
      const problem = r.problem || (r.status === "partial" ? "partial" : r.listing_id ? "drafted" : "uncertain");
      const open = () => {
        if (m) m.close();
        // The detail page's "← 1 / n →" walks these drafts.
        storeNav(p.review.filter((x) => x.listing_id).map((x) => x.listing_id));
        // A draft etsyotomasyon made: its own address (the page corrects it once published).
        this.ctx.navigate(`/ilanlar/taslak/${r.listing_id}`);
      };
      // A design with a draft on Etsy is never offered for a retry: that would be a
      // second draft of it. The draft is opened instead, to finish it by hand.
      const action = r.listing_id
        ? button({ label: t("review.open_draft"), size: "sm", variant: "secondary", iconRight: "arrow-right", onClick: open })
        : button({
            label: t("review.retry"),
            size: "sm",
            icon: "refresh",
            onClick: async () => {
              const ok = await this.ctx.confirm({ title: t("review.retry_title", { name: r.name }), message: t("review.retry_msg"), confirmLabel: t("review.retry"), danger: true });
              if (!ok) return;
              try {
                await this.api.post("/api/designs/review/forget", { name: r.name, confirm: true });
                this.ctx.toast({ tone: "success", title: t("review.retried", { name: r.name }) });
                m.close();
                await this.loadPending();
              } catch (err) {
                this.toastError(err);
              }
            },
          });
      return h(
        "div",
        { class: "dz-review-row" },
        h(
          "div",
          { class: "dz-review-text" },
          h("strong", { class: "ellipsis", title: r.name }, r.name),
          h(
            "span",
            { class: "dz-review-meta" },
            badge({ text: t(`review.badge.${problem}`), tone: problem === "uncertain" ? "danger" : "warning", size: "sm" }),
            r.listing_id ? h("span", { class: "dz-review-id num" }, t("review.listing", { id: r.listing_id })) : null,
          ),
          h("span", { class: "dz-review-why" }, t(`review.problem.${problem}`)),
          r.message ? h("span", { class: "dz-review-msg", title: r.message }, r.message) : null,
        ),
        action,
      );
    });
    m = this.ctx.modal({
      title: t("review.title"),
      width: 600,
      body: [h("p", { class: "dz-modal-hint" }, t("review.intro")), h("div", { class: "dz-review" }, rows)],
      actions: [{ label: t("common.close"), variant: "secondary" }],
    });
  }

  // ------------------------------------------------------------------ run view (t220 / t240)

  async showJob(jobId, summary) {
    let job = null;
    this.run = null;
    this.earlyEvents = [];
    try {
      job = await this.api.get(`/api/jobs/${encodeURIComponent(jobId)}`, null, { signal: this.ctx.signal });
    } catch (err) {
      if (this.api.isAbort(err)) return;
      if (summary) job = { ...summary, state: {} };
      else {
        this.earlyEvents = null;
        await this.showIdle();
        return;
      }
    }
    const early = this.earlyEvents || [];
    this.earlyEvents = null;
    if (this.destroyed) return;
    const model = this.modelFromJob(job);
    if (JOB_FINAL.has(model.status) && !model.items.length && !model.error) {
      // A run that had nothing to do (or was stopped before it began): nothing to show.
      storageSet(DISMISS_KEY, job.id);
      await this.showIdle();
      return;
    }
    this.run = model;
    // "Kontrol tamam" is a moment seen live: not when a run already past it is opened.
    this.checksAtLoad = !this.isRunning() || this.checksSettled(model);
    this.checkToasted = false;
    // Events that came while the state was on its way: the newer of the two wins, per
    // product (updated_at), so a product that finished meanwhile is not shown running.
    for (const ev of early) this.onJobEvent(ev);
    this.dirtyRows.clear();
    this.enterRun();
  }

  showSaved(saved) {
    const s = saved.summary || {};
    const items = Array.isArray(saved.items) ? saved.items : [];
    this.run = {
      jobId: null,
      v: s.batch || "saved",
      saved: true,
      status: s.cancelled ? "cancelled" : "done",
      dryRun: !!s.dry_run,
      items,
      counts: this.countsFrom(items, s),
      startedAt: s.started_at,
      finishedAt: s.finished_at,
      result: s,
      template: saved.template || null,
      mockups: saved.mockups || {},
      listingType: listingType(s.listing_type || (saved.template && saved.template.listing_type)),
      concurrency: 3,
      error: null,
    };
    this.checksAtLoad = true;
    this.enterRun();
  }

  countsFrom(items, s) {
    const c = { total: items.length, done: 0, created: 0, errors: 0, warnings: 0, checked: 0, cancelled_items: 0, current: null };
    for (const it of items) {
      if (FINAL.has(it.status)) c.done += 1;
      if (it.status === "ok" || it.status === "partial") c.created += 1;
      if (it.status === "error") c.errors += 1;
      if (it.status === "checked") c.checked += 1;
      if (it.status === "cancelled") c.cancelled_items += 1;
      c.warnings += (it.warnings || []).length;
    }
    if (s && typeof s.total === "number") c.total = Math.max(c.total, s.total);
    return c;
  }

  modelFromJob(job) {
    const st = job.state || {};
    const items = Array.isArray(st.items) ? st.items : [];
    return {
      jobId: job.id,
      v: st.batch || job.id,
      saved: false,
      status: job.status,
      dryRun: !!(st.dry_run ?? (job.params && job.params.dry_run)),
      items,
      counts: { ...this.countsFrom(items, st.result), total: st.total || items.length, current: st.current ?? null },
      startedAt: job.started_at || st.started_at || job.created_at,
      finishedAt: job.finished_at || st.finished_at,
      result: st.result || job.result || null,
      template: st.template || null,
      mockups: st.mockups || {},
      listingType: listingType(st.listing_type || (st.template && st.template.listing_type)),
      concurrency: st.concurrency || 3,
      error: job.error,
    };
  }

  isRunning() {
    return this.run && !JOB_FINAL.has(this.run.status);
  }

  enterRun() {
    const { t } = this;
    this.view = "run";
    this.stopTimers();
    this.stopReveal();
    this.selected = null;
    this.expanded = false;
    this.headKey = null;
    this.badgeKey = null;
    this.sideKey = null;
    this.eta = null;
    this.reveal = null;
    this.markSeen();
    this.pipeEls = null;
    this.headEl = h("div", { class: "dr-head-host" });
    this.pipeHost = h("div", { class: "dr-pipe-host" });
    this.sideHost = h("div", { class: "dr-side-host" });
    mount(this.el, this.headEl, h("div", { class: "dr-grid" }, this.pipeHost, this.sideHost));
    this.renderRun();
    this.every(1000, () => this.tickElapsed());
    if (this.isRunning()) {
      // Belt and braces: re-read the state now and then, in case an event was missed.
      this.every(8000, () => this.refreshJob());
    }
    this.ctx.setHeader({ subtitle: t("subtitle") });
  }

  async refreshJob() {
    if (!this.run || !this.run.jobId || this.destroyed) return;
    try {
      const job = await this.api.get(`/api/jobs/${encodeURIComponent(this.run.jobId)}`, null, { signal: this.ctx.signal });
      if (!this.run || job.id !== this.run.jobId) return;
      const model = this.modelFromJob(job);
      // Live events can be newer than the copied state: keep whichever item moved last.
      model.items = model.items.map((it) => {
        const mine = this.run.items[it.index];
        return mine && (mine.updated_at || 0) > (it.updated_at || 0) ? mine : it;
      });
      const wasRunning = this.isRunning();
      this.run = model;
      if (this.pipeEls) this.pipeEls.order = ""; // redraw the rows from the fresh state
      if (wasRunning && !this.isRunning()) this.onFinished();
      this.maybeCheckToast();
      this.renderRun();
    } catch (err) {
      if (!this.api.isAbort(err) && err.code === "not_found") {
        // The app restarted: the job is gone; show what was saved, else the drop zone.
        this.run = null;
        await this.showIdle();
      }
    }
  }

  onReconnect() {
    if (this.view === "run") this.refreshJob();
    else if (this.view === "idle") this.loadPending();
  }

  onJob(job) {
    if (!job || job.kind !== "designs" || this.destroyed) return;
    if (this.view === "idle" && !JOB_FINAL.has(job.status) && !this.uploading && !this.starting) {
      this.showJob(job.id);
      return;
    }
    if (this.view !== "run" || !this.run || job.id !== this.run.jobId) return;
    const was = this.run.status;
    this.run.status = job.status;
    if (job.started_at) this.run.startedAt = job.started_at;
    if (JOB_FINAL.has(job.status) && !JOB_FINAL.has(was)) {
      this.run.finishedAt = job.finished_at;
      this.run.error = job.error;
      // run.status is final now, so refreshJob() no longer sees the change: reset here.
      this.onFinished();
      this.refreshJob();
    } else {
      this.scheduleRender();
    }
  }

  /**
   * The run ended: a row picked during it lets go, so the finished page shows the sample
   * draft ("Örnek taslak") and no highlighted row, as the video does (t240).
   */
  onFinished() {
    this.stopTimers();
    this.every(1000, () => this.tickElapsed());
    this.selected = null;
    this.eta = null;
    if (this.pipeEls) this.pipeEls.order = "";
  }

  /** Every product is past Kontrol (or failed before it). */
  checksSettled(run) {
    const items = run && run.items ? run.items.filter(Boolean) : [];
    if (!items.length || items.length < (run.counts && run.counts.total ? run.counts.total : 0)) return false;
    return items.every((it) => it.status === "error" || CHECK_SETTLED.has(it.steps && it.steps.check));
  }

  /**
   * "Kontrol tamam: 0 hata · gönderilmeden önce" (video t236): once, when the last
   * product's check ends while this page watches. The number is every product that has
   * failed so far, so "0 hata" never stands next to a failed one.
   */
  maybeCheckToast() {
    const r = this.run;
    if (!r || r.saved || r.dryRun || this.checkToasted || this.checksAtLoad || !this.isRunning()) return;
    if (r.items.some((it) => it && it.status === "cancelled")) return;
    if (!this.checksSettled(r)) return;
    this.checkToasted = true;
    const n = r.items.filter((it) => it && (it.status === "error" || (it.steps && it.steps.check === "error"))).length;
    // The video's toast: violet, a tick, one line, top right under the header.
    this.ctx.toast({ tone: "accent", icon: "check", place: "top", title: this.t("toast.check", { n }) });
  }

  onJobEvent(ev) {
    if (!ev || ev.kind !== "designs") return;
    if (!this.run) {
      // The run is being loaded (GET /api/jobs/{id}): an event now is newer than, or as
      // new as, the state that request returns. Kept, and applied once it has arrived.
      if (this.earlyEvents && this.earlyEvents.length < 2000) this.earlyEvents.push(ev);
      return;
    }
    if (ev.job_id !== this.run.jobId) return;
    const data = ev.data || {};
    if (ev.type === "batch") {
      if (this.run.items.length) return; // once per run: the loaded state already has it
      this.run.items = Array.isArray(data.items) ? data.items : [];
      if (data.state) this.run.counts = { ...this.run.counts, ...data.state };
      this.markSeen();
      this.pipeEls = null;
      this.scheduleRender();
      return;
    }
    if (ev.type === "item" && data.item) {
      const item = data.item;
      if (typeof item.index !== "number") return;
      const mine = this.run.items[item.index];
      if (mine && (mine.updated_at || 0) > (item.updated_at || 0)) return; // older than what is shown
      this.run.items[item.index] = item;
      if (data.state) this.run.counts = { ...this.run.counts, ...data.state };
      if (this.run.status === "queued") this.run.status = "running";
      this.dirtyRows.add(item.index);
      this.scheduleRender();
      this.maybeCheckToast();
    }
  }

  scheduleRender() {
    if (this.rafPending) return;
    this.rafPending = true;
    requestAnimationFrame(() => {
      this.rafPending = false;
      if (!this.destroyed && this.view === "run") this.renderRun();
    });
  }

  renderRun() {
    if (!this.run) return;
    const grid = this.el.querySelector(".dr-grid");
    if (grid) {
      grid.hidden = !this.run.items.length && !this.isRunning();
      // The cards share a bottom edge, except when the whole list is shown: the side
      // card then keeps its own height, its "Taslağı aç" in reach.
      grid.classList.toggle("is-expanded", this.expanded);
    }
    this.renderHead();
    this.renderPipe();
    this.renderSide();
    this.renderHeaderBadge();
  }

  runPhase() {
    const r = this.run;
    if (this.isRunning()) return "running";
    if (r.status === "error") return r.items.length ? "stopped" : "failed";
    const res = r.result || {};
    if (res.stopped) return "stopped";
    if (r.status === "cancelled" || res.cancelled) return "cancelled";
    if (r.dryRun) return "checked";
    if (!r.counts.created && r.counts.errors) return "none";
    return "done";
  }

  renderHeaderBadge() {
    const { t } = this;
    const phase = this.runPhase();
    const key = `badge:${phase}`;
    if (this.badgeKey === key) return;
    this.badgeKey = key;
    if (phase === "running") {
      // The video's header while it runs: its outlined ghost "Duraklat" (a box, a dim
      // label: this app's secondary button), no badge. stopRun() turns it into
      // "Duraklatılıyor…" (this.stopBtn).
      this.stopBtn = button({ label: t("run.stop"), icon: "clock", variant: "secondary", class: "dr-pause", onClick: () => this.stopRun() });
      this.ctx.setHeader({ actions: [this.stopBtn] });
      return;
    }
    this.stopBtn = null;
    const spec = {
      done: { text: t("badge.done"), tone: "success", icon: "check" },
      checked: { text: t("badge.checked"), tone: "info", icon: "check" },
      cancelled: { text: t("badge.paused"), tone: "warning", icon: "pause" },
      stopped: { text: t("badge.stopped"), tone: "danger" },
      none: { text: t("badge.failed"), tone: "danger" },
      failed: { text: t("badge.failed"), tone: "danger" },
    }[phase];
    this.ctx.setHeader({ actions: [badge({ ...spec, size: "lg" })] });
  }

  /**
   * How far the run is, 0..1: the products finished (the headline's count, plus any
   * that failed), and the images of the draft being sent now. Products being prepared
   * ahead do not count yet, so "1 / 8" never stands next to "%46" (video t220: 18/50, %35).
   */
  progressFraction() {
    const r = this.run;
    const total = Math.max(r.counts.total || r.items.length, 1);
    let done = 0;
    for (const it of r.items) {
      if (!it) continue;
      const parts = (it.images_total || 0) + (it.files_total || 0);
      if (FINAL.has(it.status)) done += 1;
      else if (it.step === "draft" && parts) done += 0.9 * Math.min(1, ((it.images_uploaded || 0) + (it.files_uploaded || 0)) / parts);
    }
    return Math.min(1, done / total);
  }

  elapsedSeconds() {
    const r = this.run;
    const start = Number(r.startedAt) || 0;
    if (!start) return 0;
    const end = this.isRunning() ? Date.now() / 1000 : Number(r.finishedAt) || Date.now() / 1000;
    return Math.max(0, end - start);
  }

  /**
   * "~2 dk kaldı" / "~50 sn kaldı" (video t213): the pace so far, carried forward. The
   * guess is blended with the previous one counted down, so it does not jump each time
   * a product finishes.
   */
  etaText() {
    const { t } = this;
    const frac = this.progressFraction();
    const spent = this.elapsedSeconds();
    if (frac >= 0.999) return t("run.eta_end");
    if (frac < 0.02 || spent < 3) return t("run.eta_calc");
    const raw = (spent * (1 - frac)) / frac;
    const now = Date.now() / 1000;
    let rem = raw;
    if (this.eta) rem = Math.max(0, this.eta.rem - (now - this.eta.at)) * 0.75 + raw * 0.25;
    this.eta = { rem, at: now };
    const [key, n] = etaLabel(rem);
    return t(key, { n });
  }

  /** Once a second: the time left while it runs; the finished banner's "Süre:" after. */
  tickElapsed() {
    if (this.isRunning()) {
      if (this.etaEl) this.etaEl.textContent = this.etaText();
      return;
    }
    if (this.elapsedEl) this.elapsedEl.textContent = duration(this.elapsedSeconds());
  }

  renderHead() {
    const { t } = this;
    const r = this.run;
    const phase = this.runPhase();
    const c = r.counts;
    if (phase === "running") {
      if (this.headKey !== "running") {
        this.headKey = "running";
        this.headCount = h("strong", { class: "dr-big num" });
        this.headTotal = h("span", { class: "dr-total num" });
        this.headPct = h("span", { class: "dr-pct num" });
        this.headBar = progressBar({ value: 0, max: 100, size: "md" });
        this.elapsedEl = null;
        this.etaEl = h("strong", { class: "dr-eta-value" });
        mount(
          this.headEl,
          h(
            "section",
            { class: "card dr-head is-running" },
            h("span", { class: "dr-tile" }, icon("zap", { size: 23 })),
            h(
              "div",
              { class: "dr-head-text" },
              h("h2", { class: "dr-head-title" }, r.dryRun ? t("run.title_dry") : t("run.title")),
              h("p", { class: "dr-head-sub" }, r.dryRun ? t("run.sub_dry") : t("run.sub", { n: r.concurrency || 3 })),
            ),
            h(
              "div",
              { class: "dr-progress" },
              h("div", { class: "dr-progress-top" }, this.headCount, this.headTotal, h("span", { class: "dr-count-label" }, r.dryRun ? t("run.count_dry") : t("run.count")), h("span", { class: "spacer" }), this.headPct),
              this.headBar.el,
            ),
            h("span", { class: "dr-divider", "aria-hidden": "true" }),
            h(
              "div",
              { class: "dr-eta" },
              h("span", { class: "dr-eta-top" }, icon("clock", { size: 17, strokeWidth: 2 }), this.etaEl),
              h("span", { class: "dr-eta-label" }, t("run.eta_label")),
            ),
          ),
        );
      }
      const doneCount = r.dryRun ? c.checked || 0 : c.created || 0;
      this.headCount.textContent = String(doneCount);
      this.headTotal.textContent = `/ ${c.total || r.items.length}`;
      const pct = this.progressFraction();
      const shown = Math.floor(pct * 100);
      this.headPct.textContent = this.ctx.lang === "en" ? `${shown}%` : `%${shown}`;
      this.headBar.update(Math.round(pct * 1000) / 10, 100);
      if (this.etaEl && !this.etaEl.textContent) this.etaEl.textContent = this.etaText();
      return;
    }
    this.etaEl = null;
    const key = `final:${phase}:${c.created}:${c.errors}:${c.warnings}`;
    if (this.headKey === key) {
      this.tickElapsed();
      return;
    }
    this.headKey = key;
    this.elapsedEl = h("span", { class: "num" });
    const res = r.result || {};
    const left = Math.max(0, (c.total || r.items.length) - (c.created || 0) - (c.errors || 0) - (c.checked || 0));
    let tone = "success";
    let tileIcon = "check";
    let title;
    let sub;
    let actions;
    const newBtn = button({ label: t("done.new"), icon: "upload", variant: "secondary", onClick: () => this.dismissRun() });
    const viewBtn = button({ label: t("done.view"), icon: "list", variant: "primary", onClick: () => this.ctx.navigate("/ilanlar?tab=draft") });
    if (phase === "done") {
      title = t("done.title", { n: c.created });
      sub = t("done.sub");
      // Some designs failed: say so up front instead of a plain success tick.
      if (c.errors) {
        tone = "warning";
        tileIcon = "alert";
        sub = t("done.sub_errors", { n: c.errors });
      }
      actions = [newBtn, viewBtn];
    } else if (phase === "checked") {
      tone = "info";
      title = t("done.dry_title", { n: c.checked });
      sub = t("done.dry_sub");
      actions = [newBtn, button({ label: t("done.dry_start"), icon: "zap", variant: "primary", onClick: () => this.openStartFromRun() })];
    } else if (phase === "cancelled") {
      tone = "warning";
      tileIcon = "pause";
      title = t("done.cancelled_title", { n: c.created });
      sub = t("done.cancelled_sub", { left });
      actions = [newBtn, button({ label: t("done.resume"), icon: "zap", variant: "secondary", onClick: () => this.openStartFromRun() }), viewBtn];
    } else if (phase === "stopped") {
      tone = "danger";
      tileIcon = "alert";
      const reason = res.stopped ? this.stoppedReason(res.stopped) : this.errorText(r.error || {});
      title = t("done.stopped_title", { reason });
      sub = t("done.stopped_sub", { n: c.created, left });
      actions = [newBtn, button({ label: t("done.resume"), icon: "refresh", variant: "secondary", onClick: () => this.openStartFromRun() }), viewBtn];
    } else if (phase === "none") {
      tone = "danger";
      tileIcon = "x";
      title = t("done.none_title");
      sub = t("done.none_sub");
      actions = [newBtn];
    } else {
      tone = "danger";
      tileIcon = "alert";
      title = t("done.failed_title");
      sub = r.error ? this.errorText(r.error) : "";
      actions = [newBtn, button({ label: t("common.retry"), icon: "refresh", variant: "primary", onClick: () => this.openStartFromRun() })];
      const fix = r.error && ({ template_gone: "/kurulum/sablon", setup_needed: "/kurulum/magaza", reconnect: "/kurulum/magaza", bad_keys: "/kurulum/magaza" })[r.error.code];
      if (fix) actions.splice(1, 0, button({ label: t(fix === "/kurulum/sablon" ? "done.go_template" : "blocked.go_connect"), variant: "secondary", iconRight: "arrow-right", onClick: () => this.ctx.navigate(fix) }));
    }
    const chips = [];
    if (phase !== "failed") {
      chips.push(badge({ text: t("done.errors", { n: c.errors || 0 }), tone: c.errors ? "danger" : "success", icon: c.errors ? "x" : "check" }));
      chips.push(badge({ text: t("done.warnings", { n: c.warnings || 0 }), tone: c.warnings ? "warning" : "neutral", icon: "alert" }));
      chips.push(h("span", { class: "badge tone-neutral dr-duration" }, icon("clock", { size: 12 }), t("done.duration"), " ", this.elapsedEl));
    }
    // The run's output folder (its CSV): an icon in the banner, not in the page header,
    // which shows only the video's "Tamamlandı".
    const folder = res.csv && !r.saved ? iconButton({ icon: "folder", title: t("done.open_drafts"), variant: "ghost", class: "dr-folder", onClick: () => this.openFolder("drafts") }) : null;
    mount(
      this.headEl,
      h(
        "section",
        { class: cx("card dr-head is-final", `tone-${tone}`) },
        h("span", { class: "dr-tile" }, icon(tileIcon, { size: 22, strokeWidth: 2.4 })),
        h("div", { class: "dr-head-text" }, h("h2", { class: "dr-head-title" }, title), sub ? h("p", { class: "dr-head-sub" }, sub) : null),
        h("div", { class: "dr-chips" }, chips),
        h("span", { class: "spacer" }),
        h("div", { class: "dr-actions" }, folder, actions),
      ),
    );
    this.tickElapsed();
  }

  async stopRun() {
    const { t } = this;
    if (!this.run || !this.run.jobId) return;
    const ok = await this.ctx.confirm({ title: t("run.stop_title"), message: t("run.stop_msg"), confirmLabel: t("run.stop_confirm"), danger: true });
    if (!ok || !this.isRunning()) return;
    try {
      if (this.stopBtn) {
        this.stopBtn.setLabel(t("run.stopping"));
        this.stopBtn.setLoading(true);
      }
      await this.api.post(`/api/jobs/${encodeURIComponent(this.run.jobId)}/cancel`, {});
    } catch (err) {
      if (this.stopBtn) this.stopBtn.setLoading(false);
      this.toastError(err);
    }
  }

  dismissRun() {
    if (this.run && this.run.jobId) storageSet(DISMISS_KEY, this.run.jobId);
    this.showIdle();
  }

  async openStartFromRun() {
    if (this.run && this.run.jobId) storageSet(DISMISS_KEY, this.run.jobId);
    await this.showIdle();
    this.openStart();
  }

  // --- pipeline table

  visibleIndices() {
    const r = this.run;
    const n = r.items.length;
    if (this.expanded || n <= VISIBLE_ROWS) return r.items.map((_, i) => i);
    if (this.isRunning()) {
      // Pages of seven, as the video keeps its seven rows in place: the page turns when
      // every product on it is done, never one row at a time under the pointer. A row
      // picked by a click keeps its page until the pick is let go.
      const first = r.items.findIndex((it) => it && !FINAL.has(it.status));
      const start = pageStart(n, first, this.selected !== null && r.items[this.selected] ? this.selected : null);
      return Array.from({ length: VISIBLE_ROWS }, (_, i) => start + i);
    }
    // Finished: problems first, so a failed product is never hidden behind "+ 43 more".
    const errors = r.items.filter((it) => it && it.status === "error").map((it) => it.index);
    const rest = r.items.map((_, i) => i).filter((i) => !errors.includes(i));
    return [...errors, ...rest].slice(0, VISIBLE_ROWS);
  }

  renderPipe() {
    const { t } = this;
    const r = this.run;
    if (!this.pipeEls) {
      const legend = h(
        "div",
        { class: "dr-legend" },
        h("span", { class: "dr-legend-item is-done" }, h("i"), t("legend.done")),
        h("span", { class: "dr-legend-item is-running" }, h("i"), t("legend.running")),
        h("span", { class: "dr-legend-item is-todo" }, h("i"), t("legend.todo")),
      );
      const countEl = h("span", { class: "dr-pipe-count" });
      const head = h(
        "div",
        { class: "dr-thead", role: "row" },
        h("span", { role: "columnheader" }, t("pipe.file")),
        ...STEPS.map((s) => h("span", { class: "dr-th-step", role: "columnheader" }, t(`step.${s}`))),
      );
      const body = h("div", { class: "dr-tbody", role: "rowgroup" });
      const foot = h("div", { class: "dr-tfoot" });
      mount(
        this.pipeHost,
        h(
          "section",
          { class: "card card-flush dr-pipe" },
          h("header", { class: "dr-pipe-head" }, h("h2", { class: "dr-pipe-title" }, t("pipe.title")), countEl, h("span", { class: "spacer" }), legend),
          h("div", { class: "dr-table", role: "table", "aria-label": t("pipe.title") }, head, body),
          foot,
        ),
      );
      this.pipeEls = { countEl, body, foot, rows: new Map(), order: "" };
      this.dirtyRows.clear();
    }
    const els = this.pipeEls;
    els.countEl.textContent = `· ${t("pipe.count", { n: r.counts.total || r.items.length })}`;
    const indices = this.visibleIndices().filter((i) => r.items[i]);
    // The row the side card follows is marked while the run goes on (or once picked);
    // a finished run marks none (video t240), its sample draft is just the side card's.
    const selectedIndex = this.isRunning() || this.selected !== null ? this.sideIndex() : null;
    if (!indices.length) {
      // The run is starting: the product list arrives with the first event.
      if (els.order !== "empty") {
        els.order = "empty";
        els.rows.clear();
        mount(els.body, Array.from({ length: 4 }, () => h("div", { class: "dr-row is-skeleton" }, skeleton({ lines: 2, height: 10, widths: ["40%", "22%"] }))));
      }
      this.dirtyRows.clear();
      this.renderFoot(indices);
      return;
    }
    const order = `${indices.join(",")}|${selectedIndex}`;
    if (order !== els.order) {
      els.order = order;
      els.rows.clear();
      mount(
        els.body,
        indices.map((i) => {
          const row = this.rowEl(r.items[i], i === selectedIndex);
          els.rows.set(i, row);
          return row;
        }),
      );
    } else {
      for (const i of this.dirtyRows) {
        const old = els.rows.get(i);
        if (!old || !r.items[i]) continue;
        const row = this.rowEl(r.items[i], i === selectedIndex);
        old.replaceWith(row);
        els.rows.set(i, row);
      }
    }
    this.dirtyRows.clear();
    this.renderFoot(indices);
  }

  rowStatus(it) {
    const { t } = this;
    const warns = (it.warnings || []).length;
    switch (it.status) {
      case "queued":
        return { text: t("row.queued"), tone: "muted" };
      case "running":
        if (it.step === "draft" && it.files_total && (it.images_uploaded || 0) >= (it.images_total || 0)) return { text: t("row.draft_files", { n: it.files_uploaded || 0, total: it.files_total }), tone: "accent" };
        if (it.step === "draft" && it.images_total) return { text: t("row.draft_images", { n: it.images_uploaded || 0, total: it.images_total }), tone: "accent" };
        return { text: t(`row.${it.step || "mockup"}`), tone: "accent" };
      case "waiting":
        return { text: this.run.dryRun ? t("row.check") : t("row.waiting"), tone: "accent" };
      case "ok":
        return warns ? { text: `${t("row.ok")} · ${t("row.warnings", { n: warns })}`, tone: "warning" } : { text: t("row.ok"), tone: "success" };
      case "checked":
        return warns ? { text: `${t("row.checked")} · ${t("row.warnings", { n: warns })}`, tone: "warning" } : { text: t("row.checked"), tone: "success" };
      case "partial":
        return { text: t("row.partial"), tone: "warning" };
      case "cancelled":
        // Paused by the seller ("Duraklat"), or cut off because the run stopped on an error.
        return { text: it.error ? this.problemText(it.error) : this.runPhase() === "stopped" ? t("row.stopped") : t("row.cancelled"), tone: "muted" };
      case "error":
        return { text: it.error ? this.problemText(it.error) : t("row.error"), tone: "danger" };
      default:
        return { text: "", tone: "muted" };
    }
  }

  rowEl(it, current) {
    const { t } = this;
    const st = this.rowStatus(it);
    const states = STEPS.map((s) => it.steps[s] || "todo");
    // Waiting for its turn to be sent: Taslak has not begun (a grey dot, not a spinner).
    if (it.status === "waiting" && states[5] === "running") states[5] = "todo";
    const dots = stepDots({ states, labels: STEPS.map((s) => t(`step.${s}`)) });
    [...dots.children].forEach((node, i) => {
      const s = STEPS[i];
      const notes = [];
      if (states[i] === "warn") for (const w of it.warnings || []) if (w.step === s) notes.push(this.warnText(w));
      if (states[i] === "error" && it.error) notes.push(this.problemText(it.error) + (this.problemDetail(it.error) ? ` — ${it.error.message}` : ""));
      if (notes.length) node.title = `${t(`step.${s}`)}: ${notes.join(" · ")}`;
    });
    return h(
      "div",
      {
        class: cx("dr-row", current && "is-current", `is-${it.status}`),
        role: "row",
        tabindex: "0",
        dataset: { index: String(it.index) },
        onClick: () => this.select(it.index),
        onKeydown: (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            this.select(it.index);
          }
        },
      },
      h(
        "div",
        { class: "dr-file", role: "cell" },
        thumb({ src: this.thumbUrl(it.thumb_path, ROW_THUMB_W, this.run.v, isDesign(it)), size: 37, radius: 9, fit: "contain", icon: it.kind === "folder" ? "folder" : "image" }),
        h(
          "div",
          { class: "dr-file-text" },
          h("span", { class: "dr-name ellipsis", title: it.name }, it.name),
          h("span", { class: cx("dr-state ellipsis", `is-${st.tone}`), title: st.tone === "danger" && it.error ? it.error.message || st.text : st.text }, st.text),
        ),
      ),
      h("div", { class: "dr-dots", role: "cell" }, dots),
    );
  }

  renderFoot(indices) {
    const { t } = this;
    const r = this.run;
    const total = r.items.length;
    const hidden = r.items.filter((_, i) => !indices.includes(i));
    const els = this.pipeEls;
    const doneFrac = total ? r.items.filter((it) => it && FINAL.has(it.status)).length / total : 0;
    const running = this.isRunning();
    const phase = this.runPhase();
    const stopped = phase === "cancelled" || phase === "stopped";
    const key = `${hidden.length}|${Math.round(doneFrac * 200)}|${running}|${this.expanded}|${phase}`;
    if (els.footKey === key) return;
    els.footKey = key;
    if (!hidden.length && !this.expanded) {
      mount(els.foot);
      els.foot.hidden = true;
      return;
    }
    els.foot.hidden = false;
    const stack = h(
      "span",
      { class: "dr-stack" },
      hidden.slice(0, 3).map((it) => thumb({ src: this.thumbUrl(it.thumb_path, 64, this.run.v, isDesign(it)), size: 30, radius: 8, fit: "contain" })),
    );
    const line = h("span", { class: cx("dr-foot-line", !running && (stopped ? "is-stopped" : "is-done")) }, h("span", { style: { width: `${Math.round(doneFrac * 100)}%` } }));
    mount(
      els.foot,
      h(
        "button",
        {
          type: "button",
          class: "dr-more",
          onClick: () => {
            this.expanded = !this.expanded;
            this.pipeEls.order = "";
            this.pipeEls.footKey = "";
            this.el.querySelector(".dr-grid")?.classList.toggle("is-expanded", this.expanded);
            this.renderPipe();
          },
          title: this.expanded ? t("pipe.show_less") : t("pipe.show_all"),
        },
        this.expanded ? null : stack,
        this.expanded ? t("pipe.show_less") : t("pipe.more", { n: hidden.length }),
      ),
      line,
      h("span", { class: cx("dr-foot-note", !running && !stopped && "is-done") }, running ? t("pipe.parallel") : phase === "cancelled" ? t("pipe.paused") : stopped ? t("pipe.stopped") : t("pipe.all_done")),
    );
  }

  select(index) {
    this.selected = this.selected === index ? null : index;
    if (this.pipeEls) this.pipeEls.order = "";
    this.renderPipe();
    this.renderSide();
  }

  // --- the "being prepared" / "sample draft" card

  sideIndex() {
    const r = this.run;
    if (!r || !r.items.length) return null;
    if (this.selected !== null && r.items[this.selected]) return this.selected;
    if (this.isRunning()) {
      // Its title is still being typed (its tags still popping in): stay on it a moment,
      // even though the run has moved on (a product's last steps take no time at all).
      if (this.revealing() && r.items[this.reveal.index]) return this.reveal.index;
      const cur = r.counts.current;
      if (typeof cur === "number" && r.items[cur]) return cur;
      const running = r.items.find((it) => it && it.status === "running");
      if (running) return running.index;
      const lastDone = [...r.items].reverse().find((it) => it && FINAL.has(it.status));
      return lastDone ? lastDone.index : 0;
    }
    const good = r.items.find((it) => it && (it.status === "ok" || it.status === "checked"));
    const any = good || r.items.find((it) => it && it.status === "partial") || r.items[0];
    return any ? any.index : null;
  }

  /** The step the side card's pill names ("Başlık yazılıyor", "Etsy'ye gönderiliyor"). */
  stageKey(it) {
    if (it.status === "waiting") return this.run.dryRun ? "check" : "waiting";
    if (it.status === "running") {
      const step = it.step || "mockup";
      return this.t.has(`side.stage.${step}`) ? step : "mockup";
    }
    return "queued";
  }

  /** The run's drafts, in run order, for the detail page's "← 1 / n →". */
  storeRunNav() {
    const r = this.run;
    if (!r) return;
    storeNav(r.items.filter((x) => x && x.listing_id && (x.status === "ok" || x.status === "partial")).map((x) => x.listing_id));
  }

  renderSide() {
    const { t } = this;
    const r = this.run;
    const index = this.sideIndex();
    const it = index === null ? null : r.items[index];
    const running = this.isRunning();
    const live = running && this.selected === null;
    // `running` too: a card picked during the run turns into the sample when it ends.
    const key = it ? `${index}|${it.updated_at}|${it.status}|${live}|${this.selected}|${running}` : "none";
    if (this.sideKey === key) return;
    this.sideKey = key;
    this.stopReveal();
    if (!it) {
      mount(this.sideHost, h("section", { class: "card dr-side" }, h("p", { class: "dr-side-empty" }, t("side.empty"))));
      return;
    }
    const final = FINAL.has(it.status);
    // The video's card fills in as the product moves: the mockups one by one, then the
    // title typed, then the tags popping in. Only for the product followed live, once
    // per part. A part appears this way once, when it comes in while the page watches:
    // what was already there when the run was opened (this.seen) is drawn whole.
    const reduce = reducedMotion();
    const animate = live && !reduce;
    const seen = this.seenParts(index);
    // The next step's event often comes a moment later and draws the card again: what
    // was still appearing goes on from where it was (same product, same words), rather
    // than jumping to the end or starting over.
    const carry = animate && this.reveal && this.reveal.index === index ? this.reveal : null;
    const rv = { index, started: carry ? carry.started : 0, title: "", typed: 0, tagsKey: "", tagsShown: 0, imagesKey: "", tiles: 0 };
    this.reveal = rv;
    const timeline = [];
    this.preloadAhead(index);
    const follow = running && this.selected !== null ? button({ label: t("side.follow"), size: "sm", variant: "ghost", icon: "play", onClick: () => this.select(this.selected) }) : null;
    // Set once the head exists (below); the reveal's steps call it as the card fills in.
    let paintHead = () => {};

    // Mockups: for a design, only the composites count and show here (the plain design
    // goes up too, but it is not a mockup: "6/6" with 6 mockups, as in the video t240);
    // a ready photo or a folder of photos shows its photos under "Görseller".
    const composited = it.mode === "composited" || (!it.mode && it.kind !== "folder");
    const flat = flatPath(it);
    const images = (it.images || []).filter((p) => p !== flat);
    const expected = composited && this.run.mockups && typeof this.run.mockups.enabled === "number" ? this.run.mockups.enabled : images.length;
    const mockupDone = it.steps.mockup === "done" || it.steps.mockup === "warn";
    const grid = h("div", { class: "dr-mockups" });
    // No mockup switched on: the plain design is all there is to show.
    const pictures = images.length || !flat ? images : [flat];
    const shown = pictures.slice(0, 4);
    const tiles = shown.map((path, i) => {
      const extra = i === 3 && pictures.length > 4 ? pictures.length - 4 : 0;
      // Fetched at once (not lazily): a tile waits for its picture before it shows.
      const img = h("img", { src: this.thumbUrl(path, TILE_THUMB_W, this.run.v), alt: "", loading: "eager", decoding: "async" });
      img.addEventListener("error", () => img.remove(), { once: true });
      return h(
        "a",
        { class: "dr-mockup", href: this.api.url("/api/files/workspace", { path, v: this.run.v }), target: "_blank", rel: "noopener", title: baseName(path) },
        img,
        extra ? h("span", { class: "dr-mockup-more num" }, `+${extra}`) : null,
        h("span", { class: "dr-mockup-slot", "aria-hidden": "true" }, icon("image", { size: 20 })),
      );
    });
    grid.append(...tiles);
    for (let i = shown.length; i < 4 && !mockupDone && !final; i += 1) grid.appendChild(h("span", { class: "dr-mockup is-empty" }, i === shown.length && it.steps.mockup === "running" ? spinner({ size: 18 }) : icon("image", { size: 20 })));
    const short = composited && images.length < expected;
    const mockMax = composited ? expected : images.length;
    const mockState = (n) => {
      if (n < images.length) return "active";
      if (mockupDone) return short ? "warn" : "ok";
      if (it.steps.mockup === "error") return "warn";
      return it.steps.mockup === "running" || n > 0 ? "active" : "idle";
    };
    const mockCount = mockMax > 0 ? counter(mockMax) : null;
    const imagesKey = images.join("|");
    let tilesFrom = tiles.length;
    if (images.length) {
      if (carry) {
        // The same product with more mockups since (they come one by one): the tiles
        // already showing stay, the new ones come in like the first.
        const before = carry.imagesKey ? carry.imagesKey.split("|") : [];
        let same = 0;
        while (same < Math.min(carry.tiles, before.length, images.length) && before[same] === images[same]) same += 1;
        tilesFrom = Math.min(same, tiles.length);
      } else if (animate && !seen.images) tilesFrom = 0;
    }
    rv.imagesKey = imagesKey;
    rv.tiles = tilesFrom;
    // Four tiles show; the last one brings the count to all of them ("+2": 6/6). The
    // count follows the tiles one can see: each waits for its picture (at most
    // TILE_WAIT_MS), its dashed slot staying until then.
    const countFor = (k) => (k >= tiles.length ? images.length : k);
    if (mockCount) mockCount.set(countFor(tilesFrom), mockState(countFor(tilesFrom)));
    tiles.forEach((tile, i) => {
      if (i < tilesFrom) return;
      tile.classList.add("is-pending");
      timeline.push({
        at: (i - tilesFrom) * REVEAL_MOCKUP_MS,
        gate: imageGate(tile.querySelector("img"), TILE_WAIT_MS),
        fn: () => {
          tile.classList.remove("is-pending");
          tile.classList.add("is-in");
          rv.tiles = i + 1;
          if (mockCount) mockCount.set(countFor(i + 1), mockState(countFor(i + 1)));
          paintHead();
        },
      });
    });
    if (images.length) seen.images = true;
    // The title and the tags come after the tiles (the video's order), so later steps
    // of the reveal start at `cursor`; a tile still waiting for its picture moves them on.
    let cursor = (tiles.length - tilesFrom) * REVEAL_MOCKUP_MS;

    // Title: typed out live, a fixed three-line box either way (no jump when it lands).
    const titleText = it.title || "";
    const titleCount = counter(140);
    const titleBox = h("div", { class: cx("dr-titlebox", live && it.steps.title === "running" && "is-live") });
    const placeholder = () => (final ? h("span", { class: "muted" }, "–") : skeleton({ lines: 3, height: 11, widths: ["92%", "74%", "40%"] }));
    let typedFrom = titleText.length;
    if (titleText) {
      if (carry && carry.title === titleText) typedFrom = Math.min(carry.typed, titleText.length);
      else if (animate && !seen.title) typedFrom = 0;
    }
    rv.title = titleText;
    rv.typed = typedFrom;
    if (titleText && typedFrom < titleText.length) {
      const startTyping = () => {
        mount(titleBox);
        titleCount.set(typedFrom, "active");
        this.typeTitle(titleBox, titleText, typedFrom, (n, done) => {
          rv.typed = n;
          titleCount.set(n, done ? "ok" : "active");
          if (done) paintHead();
        });
      };
      if (cursor) {
        // Its turn comes after the tiles: until then the box waits as it is.
        if (typedFrom) titleBox.textContent = titleText.slice(0, typedFrom);
        else titleBox.appendChild(placeholder());
        titleCount.set(typedFrom, typedFrom ? "active" : "idle");
        timeline.push({ at: cursor, fn: startTyping });
      } else {
        startTyping();
      }
      cursor += typeDuration(titleText, typedFrom) + 150;
    } else if (titleText) {
      titleBox.textContent = titleText;
      titleBox.classList.add("is-final");
      titleCount.set(titleText.length, "ok");
    } else {
      titleCount.set(0, it.steps.title === "running" ? "active" : "idle");
      titleBox.appendChild(placeholder());
    }
    if (titleText) seen.title = true;

    // Tags: each one pops into the dashed slot it will fill, the newest lit up.
    const tags = it.tags || [];
    const tagsKey = tags.join("|");
    let tagsFrom = tags.length;
    if (tags.length) {
      if (carry && carry.tagsKey === tagsKey) tagsFrom = Math.min(carry.tagsShown, tags.length);
      else if (animate && !seen.tags) tagsFrom = 0;
    }
    rv.tagsKey = tagsKey;
    rv.tagsShown = tagsFrom;
    const settled = final || it.steps.tags === "done" || it.steps.tags === "warn";
    const tagState = (n) => {
      if (n >= 13) return "ok";
      if (n < tags.length) return "active";
      if (settled && n > 0) return "warn";
      return it.steps.tags === "running" || n > 0 ? "active" : "idle";
    };
    const tagsCount = counter(13);
    tagsCount.set(tagsFrom, tagState(tagsFrom));
    const tagEls = tags.map((tag, i) => {
      const chip = tagChip({ text: tag });
      chip.classList.add("dr-tag");
      if (i >= tagsFrom) chip.classList.add("is-pending");
      else if (i === tagsFrom - 1 && tagsFrom < tags.length) chip.classList.add("is-hot");
      return chip;
    });
    const tagsEl = h("div", { class: "dr-tags" }, tagEls);
    // Before the tags are known: ragged dashed slots, four to a row (video t219).
    if (!final && tags.length < 13) {
      for (let i = tags.length; i < 13; i += 1) {
        const slot = tagChip({ dashed: true });
        slot.style.width = slotWidth(i);
        tagsEl.appendChild(slot);
      }
    }
    if (tagsFrom < tags.length) {
      const start = cursor;
      for (let i = tagsFrom; i < tagEls.length; i += 1) {
        const chip = tagEls[i];
        timeline.push({
          at: start + (i - tagsFrom) * REVEAL_TAG_MS,
          fn: () => {
            if (i) tagEls[i - 1].classList.remove("is-hot");
            chip.classList.remove("is-pending");
            chip.classList.add("is-in", "is-hot");
            rv.tagsShown = i + 1;
            tagsCount.set(i + 1, tagState(i + 1));
            paintHead();
          },
        });
      }
      timeline.push({ at: start + (tagEls.length - tagsFrom) * REVEAL_TAG_MS + 450, fn: () => tagEls[tagEls.length - 1].classList.remove("is-hot") });
    }
    if (tags.length) seen.tags = true;

    // Download files (digital templates): found at Kontrol, sent in the Taslak step after
    // the images. Before Kontrol only their number is known.
    let filesSection = null;
    if (DIGITAL.has(this.run.listingType) || it.files_total || (it.deliverables || []).length) {
      const paths = it.deliverables || [];
      const total = paths.length || it.files_total || 0;
      const sent = it.files_uploaded || 0;
      const draftDone = it.steps.draft === "done" || it.steps.draft === "warn";
      const sending = it.status === "running" && it.step === "draft";
      let filesCount = null;
      if (total && (draftDone || sending)) {
        const whole = sent >= total;
        filesCount = h("span", { class: cx("dr-count", draftDone ? (whole ? "is-ok" : "is-warn") : "is-active") }, draftDone ? icon(whole ? "check" : "alert", { size: 12, strokeWidth: 2.6 }) : null, `${sent}/${total}`);
      } else if (total) {
        filesCount = h("span", { class: "dr-count is-idle" }, t("side.files_n", { n: total }));
      }
      const list = paths.length
        ? h(
            "ul",
            { class: "dr-files" },
            paths.map((path, i) =>
              h(
                "li",
                { class: cx("dr-file-item", i < sent && "is-sent") },
                icon(i < sent ? "check" : "file", { size: 12, strokeWidth: i < sent ? 2.6 : 2 }),
                h("span", { class: "ellipsis", title: path }, baseName(path)),
              ),
            ),
          )
        : h("p", { class: "muted dr-none" }, final ? "–" : t("side.files_pending"));
      // One line when the files fit beside the heading (they usually do: one design file).
      filesSection = h(
        "div",
        { class: "dr-section dr-files-section" },
        h("div", { class: "dr-section-head" }, icon("download", { size: 14 }), h("span", { class: "dr-files-label" }, t("side.files")), list, h("span", { class: "spacer" }), filesCount),
      );
    }

    // Problems of this product
    const notes = [];
    if (it.error) notes.push(infoNote({ tone: it.status === "cancelled" ? "neutral" : "danger", icon: "alert", text: [h("b", null, this.problemText(it.error)), this.problemDetail(it.error) ? h("span", { class: "dr-detail" }, ` ${it.error.message}`) : null] }));
    const warns = it.warnings || [];
    if (warns.length) {
      notes.push(
        h(
          "ul",
          { class: "dr-warns" },
          warns.map((w) => h("li", { title: w.message || "" }, icon("alert", { size: 12 }), this.warnText(w))),
        ),
      );
    }

    // From the template
    const tpl = this.run.template || {};
    const fromTpl = [];
    if (tpl.price !== undefined && tpl.price !== null) fromTpl.push(h("span", { class: "dr-tpl-chip num" }, t("side.price", { price: money(tpl.price, tpl.currency || "USD") })));
    if (tpl.description) fromTpl.push(h("span", { class: "dr-tpl-chip" }, t("side.description")));
    if (this.run.listingType === "download") fromTpl.push(h("span", { class: "dr-tpl-chip" }, t("side.digital")));
    else fromTpl.push(tpl.shipping_profile ? h("span", { class: "dr-tpl-chip" }, t("side.shipping")) : h("span", { class: "dr-tpl-chip is-warn" }, t("side.no_shipping")));

    // The head. While the card still fills in it is the product being prepared, with the
    // part appearing now as its step ("Başlık yazılıyor", "Etiketler seçiliyor"),
    // whatever the run has done with it meanwhile: "✓ Son hazırlanan" and "Taslak hazır"
    // come once all of it shows (video: those only when every stage is done).
    const stagePill = (stage) => {
      const text = t(`side.stage.${stage}`);
      return badge({ text, tone: "accent", title: text });
    };
    const statusPill = () => {
      if (!final) return stagePill(this.stageKey(it));
      if (it.status === "ok") return badge({ text: t("side.ready"), tone: "success", icon: "check" });
      if (it.status === "partial") return badge({ text: t("side.partial"), tone: "warning", icon: "alert" });
      if (it.status === "error") return badge({ text: t("side.failed"), tone: "danger", icon: "x" });
      if (it.status === "checked") return badge({ text: t("side.checked"), tone: "info", icon: "check" });
      if (it.status === "cancelled") return badge({ text: this.runPhase() === "stopped" ? t("row.stopped") : t("row.cancelled"), tone: "neutral" });
      return null;
    };
    const liveDot = () => h("span", { class: "dr-live-dot", "aria-hidden": "true" });
    const tick = () => icon("check", { size: 16, strokeWidth: 2.6 });
    const headFor = (stage) => {
      if (stage) return { label: [liveDot(), t("side.current")], check: false, pill: stagePill(stage) };
      if (live && !final) return { label: [liveDot(), t("side.current")], check: false, pill: statusPill() };
      if (live) return { label: [tick(), t("side.last")], check: true, pill: statusPill() };
      if (this.selected !== null) return { label: [t("side.selected")], check: false, pill: statusPill() };
      return { label: [tick(), r.dryRun ? t("side.sample_dry") : t("side.sample")], check: true, pill: statusPill() };
    };
    // Which part is appearing now (null once everything shows).
    const stageNow = () => {
      if (!animate) return null;
      if (rv.tiles < tiles.length) return "mockup";
      if (rv.typed < titleText.length) return "title";
      if (rv.tagsShown < tags.length) return "tags";
      return null;
    };

    // The card's own head opens the draft once there is one (the video's card ends at
    // "Kargo profili": no link in the footer). Its thumb is the rows' own size, so the
    // picture the row already fetched serves here too (never a blank head).
    const headThumb = thumb({ src: this.thumbUrl(it.thumb_path, ROW_THUMB_W, this.run.v, isDesign(it)), size: 39, radius: 9, fit: "contain", icon: it.kind === "folder" ? "folder" : "image" });
    const titleEl = h("h2", { class: "dr-side-title" });
    const titles = h(
      "div",
      { class: "dr-side-titles" },
      titleEl,
      h("p", { class: "dr-side-sub ellipsis", title: it.name }, `${it.name} · ${this.typeLabel(it)}`),
    );
    const canOpen = it.listing_id && (it.status === "ok" || it.status === "partial");
    const headMain = canOpen
      ? h(
          "a",
          { class: "dr-side-link", href: `/ilanlar/taslak/${it.listing_id}`, title: t("side.open_draft"), onClick: () => this.storeRunNav() },
          headThumb,
          titles,
          h("span", { class: "sr-only" }, t("side.open_draft")),
        )
      : [headThumb, titles];
    const header = h("header", { class: "dr-side-head" }, headMain);
    let shownStage;
    let pillEl = null;
    paintHead = () => {
      const stage = stageNow();
      if (shownStage !== undefined && stage === shownStage) return;
      shownStage = stage;
      const head = headFor(stage);
      mount(titleEl, head.label);
      titleEl.classList.toggle("is-sample", head.check);
      const next = follow || head.pill;
      if (next === pillEl) return;
      if (next && next !== follow) next.classList.add("dr-stage");
      if (pillEl && next) pillEl.replaceWith(next);
      else if (pillEl) pillEl.remove();
      else if (next) header.appendChild(next);
      pillEl = next;
    };
    paintHead();

    mount(
      this.sideHost,
      h(
        "section",
        { class: cx("card dr-side", live && "is-live") },
        header,
        h(
          "div",
          { class: "dr-section" },
          h("div", { class: "dr-section-head" }, icon("image", { size: 14 }), h("span", null, composited ? t("side.mockups") : t("side.images")), h("span", { class: "spacer" }), mockCount && mockCount.el),
          pictures.length || !final ? grid : h("p", { class: "muted dr-none" }, "–"),
        ),
        h(
          "div",
          { class: "dr-section" },
          h("div", { class: "dr-section-head" }, icon("loader", { size: 14 }), h("span", null, t("side.title")), h("span", { class: "spacer" }), titleCount.el),
          titleBox,
        ),
        h(
          "div",
          { class: "dr-section" },
          h("div", { class: "dr-section-head" }, icon("tag", { size: 14 }), h("span", null, t("side.tags")), h("span", { class: "spacer" }), tagsCount.el),
          tagsEl,
        ),
        filesSection,
        notes.length ? h("div", { class: "dr-section dr-notes" }, notes) : null,
        h("footer", { class: "dr-side-foot" }, icon("file", { size: 13 }), h("span", { class: "dr-tpl-label" }, t("side.from_template")), fromTpl),
      ),
    );
    if ((timeline.length || this.typing) && !rv.started) rv.started = performance.now();
    this.runTimeline(timeline);
  }

  /**
   * The next products' pictures, asked for before the card gets to them: each one's
   * head thumb (the rows' size, so one request serves both) and the first four tiles of
   * the next one that has its mockups. The server makes a thumbnail on the first request
   * for it; this way the card seldom has to wait for one.
   */
  preloadAhead(index) {
    const r = this.run;
    if (!r || !this.isRunning() || typeof Image !== "function") return;
    if (!this.preloaded || this.preloaded.size > 400) this.preloaded = new Set();
    const get = (url) => {
      if (!url || this.preloaded.has(url)) return;
      this.preloaded.add(url);
      const img = new Image();
      img.decoding = "async";
      img.src = url;
    };
    let tilesLeft = 1;
    const end = Math.min(r.items.length, index + 1 + (r.concurrency || 3));
    for (let k = index + 1; k < end; k += 1) {
      const it = r.items[k];
      if (!it || FINAL.has(it.status)) continue;
      get(this.thumbUrl(it.thumb_path, ROW_THUMB_W, r.v, isDesign(it)));
      const flat = flatPath(it);
      const imgs = (it.images || []).filter((p) => p !== flat);
      if (tilesLeft && imgs.length) {
        tilesLeft -= 1;
        for (const path of imgs.slice(0, 4)) get(this.thumbUrl(path, TILE_THUMB_W, r.v));
      }
    }
  }

  /** Which parts of product `index` this page has shown ({title, tags, images}). */
  seenParts(index) {
    if (!this.seen) this.seen = new Map();
    if (!this.seen.has(index)) this.seen.set(index, { title: false, tags: false, images: false });
    return this.seen.get(index);
  }

  /** A run opened as it is: everything it already has counts as shown. */
  markSeen() {
    this.seen = new Map();
    for (const it of (this.run && this.run.items) || []) {
      if (!it) continue;
      this.seen.set(it.index, { title: !!it.title, tags: (it.tags || []).length > 0, images: (it.images || []).length > 0 });
    }
  }

  /** Whether the side card is still filling in (it stays on that product meanwhile). */
  revealing() {
    const rv = this.reveal;
    return !!(rv && rv.started && (this.typing || this.revealTimer) && performance.now() - rv.started < REVEAL_MAX_MS);
  }

  /** The card has filled in: it may move on to the product the run is at now. */
  revealEnded() {
    if (this.typing || this.revealTimer || this.destroyed || this.view !== "run") return;
    this.sideKey = null;
    if (this.pipeEls) this.pipeEls.order = "";
    this.scheduleRender();
  }

  /**
   * Types `text` into `box` from character `from` on; onTick(n, done) follows it.
   * Returns how long the rest takes (ms).
   */
  typeTitle(box, text, from, onTick) {
    const caret = h("span", { class: "dr-caret", "aria-hidden": "true" });
    const textNode = document.createTextNode(text.slice(0, from));
    box.append(textNode, caret);
    box.setAttribute("aria-label", text);
    let i = from;
    const step = Math.max(2, Math.ceil(text.length / 30));
    const timer = setInterval(() => {
      i = Math.min(text.length, i + step);
      textNode.data = text.slice(0, i);
      const done = i >= text.length;
      if (onTick) onTick(i, done);
      if (done) {
        clearInterval(timer);
        if (this.typing && this.typing.timer === timer) this.typing = null;
        box.classList.add("is-final");
        setTimeout(() => caret.remove(), 900);
        this.revealEnded();
      }
    }, TYPE_TICK_MS);
    this.typing = { timer };
    return typeDuration(text, from);
  }
}

export default {
  async mount(el, ctx) {
    const page = new DesignsPage(el, ctx);
    await page.start();
    return () => page.destroy();
  },
};
