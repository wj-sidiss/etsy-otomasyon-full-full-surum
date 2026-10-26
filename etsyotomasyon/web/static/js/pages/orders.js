// Siparişler (/siparisler): paid orders, the carrier and tracking number of each, and
// the upload to Etsy (a confirmed job: Etsy e-mails every buyer, which cannot be undone).
//
// Server: GET /api/orders, /api/orders/summary, /api/orders/carriers,
// POST /api/orders/country, /api/orders/ship, /api/orders/export.csv,
// /api/orders/import-tracking, GET/POST /api/orders/drafts (the carriers and numbers
// not sent yet). The ship job is kind "orders"; each row it finishes arrives as a
// job-event of type "row".

import {
  badge,
  button,
  card,
  cx,
  emptyState,
  h,
  iconButton,
  infoNote,
  menu,
  modal,
  mount,
  pagination,
  progressBar,
  searchInput,
  select,
  table,
  tabs,
  thumb,
} from "../ui.js";
import { icon } from "../icons.js";
import { date, dateTime, money, relative, time } from "../format.js";

const TABS = ["unshipped", "shipped", "delivered", "all"];
const SETUP_STATES = new Set(["keys", "bad_keys", "disconnected"]);
const ACTIVE = new Set(["queued", "running"]);
const OTHER = "other";
const SOLD_URL = "https://www.etsy.com/your/orders/sold";
// Ship-from countries offered in the selector (plus the shop's own, whatever it is).
const COUNTRIES = ["TR", "US", "GB", "CA", "AU", "DE", "FR", "NL", "IT", "ES", "PL", "IE", "BE", "AT",
  "SE", "DK", "FI", "NO", "PT", "GR", "UA", "IN", "JP", "MX", "NZ"];
const ROW_H = 57;
const CONFIRM_PREVIEW = 8;
// A row Etsy just accepted flashes and its badge flips to "Kargoda" (t280's upload).
const FLIP_MS = 450;
// "Select all" ticks the rows top to bottom: the video's eight rows in 0.4 s, each box
// in 150 ms (orders.css o-tick-*).
const TICK_SPAN_MS = 400;
const TICK_STEP_MS = 45;
const TICK_MS = 150;
// The upload pill shows its Stop control once a send has run this long: a short send
// looks like the video, a long one can still be stopped.
const STOP_AFTER_MS = 1500;
// Numbers typed or imported but not sent are saved this long after the last change.
const DRAFT_SAVE_MS = 600;
const MAX_DRAFTS = 500; // the server's SHIP_MAX_ROWS
// Rows read per request when the whole waiting list is selected (the server's maximum).
const WAITING_PAGE = 100;
// What each ship job this tab started sends (job id -> [{receipt_id, carrier_name,
// tracking_code}]). A job waiting behind another one (a long Tasarım Yükle run) has no
// state on the server until it starts, so a page that comes back meanwhile takes its rows
// from here; once the job runs, the server's own state.queue is read instead. Also kept
// in sessionStorage (this tab only) so a reload finds them; the page works without it.
const SENT_KEY = "etsyotomasyon.orders.sent";
const SENT_QUEUES = new Map(readSent());

function readSent() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(SENT_KEY) || "{}");
    return saved && typeof saved === "object" ? Object.entries(saved).filter(([, q]) => Array.isArray(q)) : [];
  } catch {
    return [];
  }
}

function writeSent() {
  try {
    if (SENT_QUEUES.size) sessionStorage.setItem(SENT_KEY, JSON.stringify(Object.fromEntries(SENT_QUEUES)));
    else sessionStorage.removeItem(SENT_KEY);
  } catch {
    /* private window or storage off: in-page memory only */
  }
}

/** Rows per page: as many as fit without scrolling, 8 at least (the video's 790 px). */
function pageSize() {
  const hgt = window.innerHeight || 800;
  return Math.max(8, Math.min(25, Math.floor((hgt - 334) / ROW_H)));
}

/** "Retro Mountain Sunset Shirt, Vintage Hiking Tee, ..." -> "Retro Mountain Sunset Shirt". */
function shortTitle(title) {
  const text = String(title || "");
  const first = text.split(/\s*[,|]\s*|\s+[-–—]\s+/)[0];
  return first.length >= 8 ? first : text;
}

function reducedMotion() {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

function today() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Put nodes where "{name}" placeholders are in a translated string. */
function rich(text, nodes) {
  const out = [];
  let rest = text;
  for (;;) {
    const m = /\{([a-z]+)\}/.exec(rest);
    if (!m) break;
    if (m.index) out.push(rest.slice(0, m.index));
    out.push(nodes[m[1]] !== undefined ? nodes[m[1]] : m[0]);
    rest = rest.slice(m.index + m[0].length);
  }
  if (rest) out.push(rest);
  return out;
}

export default {
  async mount(el, ctx) {
    const t = ctx.t;
    const q0 = ctx.query || {};
    const S = {
      tab: TABS.includes(q0.tab) ? q0.tab : "unshipped",
      q: String(q0.q || ""),
      page: Math.max(1, parseInt(q0.page, 10) || 1),
      perPage: pageSize(),
      data: null,
      rows: [],
      known: new Map(), // receipt_id -> row, from every page loaded
      edits: new Map(), // receipt_id -> {carrier_name, tracking_code, note_to_buyer}
      selected: new Set(),
      results: new Map(), // receipt_id -> the ship job's answer for that row
      summary: null,
      carriers: null,
      defaultCarrier: "",
      job: null,
      jobIds: [],
      queueFetch: null, // id of the job whose state.queue is being read
      finished: new Set(), // ids of ship jobs already wrapped up
      lastDone: null,
      loadSeq: 0,
      setup: false,
      waiting: null, // every receipt id of the waiting list, in order (from the last full read)
      knowing: null, // the read of waiting rows not loaded yet (after "select all"), while it runs
      posting: false, // the ship request is on its way
      flipAt: new Map(), // receipt_id -> when Etsy accepted it on this page (performance.now())
      tickAt: null, // when "select all" started its cascade (performance.now())
      tickIds: new Set(), // the rows that cascade
      jobSeenAt: 0, // when this page first saw the running send (Date.now())
      stopTimer: null,
      stopFor: null, // the send whose Stop control is showing
      draftsLoad: null, // the saved carriers and numbers, read once
    };
    // Unsent carriers and numbers, saved in the shop's home (POST /api/orders/drafts).
    const drafts = { timer: null, saving: null, failed: false, version: 0, saved: 0 };

    // --------------------------------------------------------------- setup gate

    function renderSetup() {
      S.setup = true;
      ctx.setHeader({ subtitle: t("subtitle"), actions: [] });
      mount(
        el,
        card({
          class: "o-setup",
          body: emptyState({
            icon: "truck",
            title: t("setup.title"),
            message: t("setup.msg"),
            action: button({
              label: t("setup.action"),
              variant: "primary",
              iconRight: "arrow-right",
              onClick: () => ctx.navigate("/kurulum/magaza"),
            }),
          }),
        }),
      );
    }

    ctx.onStatus((s, prev) => {
      if (S.setup && s && s.state === "connected" && (!prev || prev.state !== "connected")) ctx.remount();
    });

    const st = ctx.status();
    if (st && SETUP_STATES.has(st.state)) {
      renderSetup();
      return () => {};
    }

    // --------------------------------------------------------------- carriers

    const carrierNames = () => ((S.carriers && S.carriers.carriers) || []).map((c) => c.name);

    function canonicalCarrier(name) {
      const value = String(name || "").trim();
      if (!value) return "";
      const folded = value.toLowerCase();
      if (folded === OTHER) return OTHER;
      return carrierNames().find((n) => n.toLowerCase() === folded) || value;
    }

    function carrierKnown(name) {
      if (!name) return false;
      if (!S.carriers || !S.carriers.carriers.length) return true; // no list: Etsy decides
      const folded = name.toLowerCase();
      return folded === OTHER || carrierNames().some((n) => n.toLowerCase() === folded);
    }

    const carrierFor = (id) => {
      const e = S.edits.get(id);
      return (e && e.carrier_name) || S.defaultCarrier || "";
    };
    const trackingFor = (id) => {
      const e = S.edits.get(id);
      return ((e && e.tracking_code) || "").trim();
    };

    function isEditable(row) {
      const r = S.results.get(row.receipt_id);
      return row.status === "unshipped" && !(r && r.status === "ok");
    }

    const inFlight = (id) => !!S.job && S.jobIds.includes(id) && !S.results.has(id);

    function isReady(id) {
      // Sent from this page, or being sent: never offered again, loaded or not.
      const r = S.results.get(id);
      if ((r && r.status === "ok") || inFlight(id)) return false;
      const row = S.known.get(id);
      if (row && !isEditable(row)) return false;
      const carrier = carrierFor(id);
      return !!trackingFor(id) && !!carrier && carrierKnown(carrier);
    }

    /** Ticked orders that are not uploaded yet (an uploaded one keeps its tick, t280). */
    const freshSelectedIds = () =>
      [...S.selected].filter((id) => {
        const r = S.results.get(id);
        return !(r && r.status === "ok");
      });
    const freshSelected = () => freshSelectedIds().length;

    /** Waiting orders with a carrier and a number that are not sent yet, in list order. */
    function allReadyIds() {
      if (S.waiting) return S.waiting.filter((id) => S.edits.has(id) && isReady(id));
      return [...S.edits.keys()].filter((id) => isReady(id));
    }

    /** What the upload button sends (the video's "Takip numaralarını yükle (24)"): the
     *  ticked orders that are ready or, with nothing ticked, every ready waiting order
     *  (`tick`: those get ticked when the button is pressed). */
    function sendPlan() {
      const picked = freshSelectedIds();
      if (picked.length) return { ids: picked.filter((id) => isReady(id)), tick: false };
      return { ids: allReadyIds(), tick: true };
    }

    function regionName(code) {
      try {
        return new Intl.DisplayNames([ctx.lang === "en" ? "en" : "tr"], { type: "region" }).of(code) || code;
      } catch {
        return code;
      }
    }

    function countryOptions(current) {
      const codes = new Set(COUNTRIES);
      if (current) codes.add(current);
      return [...codes]
        .map((code) => ({ value: code, label: regionName(code) }))
        .sort((a, b) => a.label.localeCompare(b.label, ctx.lang === "en" ? "en" : "tr"));
    }

    // --------------------------------------------------------------- header

    // "Takip numaralarını yükle (n)" (sendPlan). Always in full colour, as in the video:
    // with nothing ready it opens the CSV picker; while Etsy works it only says so
    // (aria-disabled), and the pill by the search box shows how far the upload is.
    const sendBtn = button({
      label: t("send.none"),
      icon: "truck",
      variant: "primary",
      class: "o-send",
      onClick: () => send(),
    });
    // "CSV içe aktar": the video's ghost button, it imports at once. The rest (export,
    // refresh, Etsy's own order page, the ship-from country) is the ⋯ menu in the footer.
    const csvBtn = button({
      label: t("csv.button"),
      icon: "file",
      variant: "ghost",
      title: t("csv.import"),
      class: "o-csv",
      onClick: () => fileInput.click(),
    });
    const prepareAllBtn = button({
      label: t("print.all"),
      icon: "layers",
      variant: "ghost",
      title: t("print.all_tip"),
      class: "o-prepare-all",
      onClick: () => prepareAll(),
    });
    ctx.setHeader({ actions: [prepareAllBtn, csvBtn, sendBtn] });

    // Every order waiting to ship: its print files, one after another. Local only.
    async function prepareAll() {
      const ids = (S.waiting || []).slice();
      if (!ids.length) {
        ctx.toast({ tone: "warning", title: t("print.all_none") });
        return;
      }
      prepareAllBtn.disabled = true;
      let made = 0;
      let skipped = 0;
      for (const [i, id] of ids.entries()) {
        prepareAllBtn.querySelector(".btn-label")?.replaceChildren(t("print.all_progress", { i: i + 1, n: ids.length }));
        try {
          const d = await ctx.api.post(`/api/orders/${id}/print`, { dpi: savedDpi(), bleed: savedBleed() });
          made += d.items.filter((it) => it.made).length;
        } catch (err) {
          if (err.code === "nothing_to_print") skipped += 1;
          else {
            ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
            break;
          }
        }
      }
      prepareAllBtn.disabled = false;
      prepareAllBtn.querySelector(".btn-label")?.replaceChildren(t("print.all"));
      ctx.toast({ tone: skipped ? "warning" : "success", title: t("print.all_done", { n: made }), message: skipped ? t("print.all_skipped", { n: skipped }) : undefined });
    }
    const moreBtn = iconButton({
      icon: "more",
      variant: "ghost",
      size: "sm",
      title: t("csv.more"),
      class: "o-more-btn",
      onClick: () => openCsvMenu(),
    });
    moreBtn.setAttribute("aria-haspopup", "menu");

    const jobTotal = () => {
      const p = (S.job && S.job.progress) || {};
      return p.total || S.jobIds.length || (S.job && S.job.params && S.job.params.n) || 0;
    };

    function syncSend() {
      syncDirty();
      csvBtn.setDisabled(!!S.job);
      const busy = !!S.job || S.posting;
      // During a send the button keeps the number being sent; afterwards it counts what
      // is still ready (never the rows just sent: they are not ready any more).
      const n = S.job ? jobTotal() : sendPlan().ids.length;
      sendBtn.setLabel(n ? t("send", { n }) : t("send.none"));
      if (busy) sendBtn.setAttribute("aria-disabled", "true");
      else sendBtn.removeAttribute("aria-disabled");
      sendBtn.title = busy ? t("uploading") : n ? "" : t("send.hint");
      renderSlot();
    }

    function updateSubtitle() {
      let sub = t("subtitle");
      const sm = S.summary;
      if (S.lastDone && S.lastDone.sent > 0) sub = t("subtitle.shipped", { n: S.lastDone.sent });
      else if (sm) {
        const waiting = sm.counts.unshipped;
        if (waiting > 0) sub = t("subtitle.waiting", { n: waiting });
        // Orders that went out this month (by shipment date); "n+" when Etsy had more
        // shipped orders changed this month than the server reads for the count.
        else if (sm.shipped_month > 0) sub = t(sm.shipped_month_partial ? "subtitle.month_more" : "subtitle.month", { n: sm.shipped_month });
        else sub = t("subtitle.none");
      }
      ctx.setHeader({ subtitle: sub });
    }

    // --------------------------------------------------------------- toolbar

    function countFor(id) {
      if (S.data && S.data.tab === id && !S.data.q) return S.data.total;
      return S.summary ? S.summary.counts[id] : undefined;
    }

    const tabItems = () =>
      TABS.map((id) => ({
        id,
        label: t(`tab.${id}`),
        // Like the video: the waiting count always, the others on their own tab.
        count: id === "unshipped" || id === S.tab ? countFor(id) : undefined,
      }));

    const tabsC = tabs({
      items: tabItems(),
      value: S.tab,
      ariaLabel: t("tabs.label"),
      onChange: (id) => {
        S.tab = id;
        S.page = 1;
        ctx.setQuery({ tab: id === "unshipped" ? null : id, page: null });
        tabsC.update(tabItems());
        load();
      },
    });

    // The ship-from country decides Etsy's carrier list. It sits in the ⋯ menu (the
    // video's toolbar holds only the tabs, the status pill and the search box).
    const countryLabel = (code) => {
      const name = regionName(code);
      return name.length <= 14 ? name : code;
    };

    function openCountryMenu() {
      const current = S.carriers && S.carriers.country;
      menu(
        moreBtn,
        [
          { header: t("country.label") },
          ...countryOptions(current).map((o) => ({
            label: o.label,
            hint: o.value,
            checked: o.value === current,
            onClick: () => {
              if (o.value !== current) changeCountry(o.value);
            },
          })),
        ],
        { placement: "top-end", width: 280, class: "o-country-menu" },
      );
    }

    // The pill left of the search box (t280): "✓ 12 sipariş seçili" while orders are
    // ticked, the upload's progress while Etsy works, "12/12 takip numarası Etsy'ye
    // yüklendi" once it is done.
    const slotEl = h("div", { class: "o-pill", role: "status" });
    slotEl.hidden = true;
    let slotMode = "";
    const upBar = progressBar({ value: 0, max: 1, label: t("uploading") });
    const upCount = h("span", { class: "o-pill-count num", "aria-hidden": "true" });
    const upStop = iconButton({ icon: "stop", title: t("send.stop"), variant: "ghost", size: "sm", class: "o-pill-stop", onClick: () => stopJob() });
    const upNodes = [
      h("span", { class: "o-pill-spin" }, icon("refresh", { size: 15, strokeWidth: 2.2 })),
      h("span", { class: "o-pill-text" }, t("uploading")),
      upBar.el,
      upCount,
      upStop,
    ];

    /** How long the send has been running, in ms (from the server's start time when known). */
    function jobAge() {
      if (!S.job) return 0;
      const started = S.job.started_at ? S.job.started_at * 1000 : S.jobSeenAt || Date.now();
      return Date.now() - started;
    }

    /** Stop is the only way to call off a send (Etsy e-mails each buyer as it goes): it
     *  shows once the send has run STOP_AFTER_MS, and stays. */
    function stopShown() {
      if (!S.job || !S.job.cancellable) return false;
      if (S.stopFor === S.job.id) return true; // once shown, it stays (a queued send starting)
      const wait = STOP_AFTER_MS - jobAge();
      if (wait <= 0) {
        S.stopFor = S.job.id;
        return true;
      }
      if (!S.stopTimer) {
        S.stopTimer = setTimeout(() => {
          S.stopTimer = null;
          if (ctx.isActive() && S.job) renderSlot();
        }, wait + 20);
      }
      return false;
    }

    function renderSlot() {
      const done = S.lastDone;
      const picked = freshSelected();
      let mode = "";
      if (S.job) mode = "progress";
      else if (picked > 0) mode = "selected";
      else if (done && done.sent > 0) mode = done.sent < done.total ? "partial" : "done";
      slotEl.hidden = !mode;
      const stop = mode === "progress" && stopShown();
      slotEl.className = cx("o-pill", mode && `is-${mode}`, stop && "has-stop");
      if (mode === "progress") {
        const p = S.job.progress || {};
        const total = jobTotal();
        upBar.update(p.done || 0, total || 1);
        upCount.textContent = `${p.done || 0}/${total || "?"}`;
        upStop.hidden = !stop;
        if (slotMode !== mode) mount(slotEl, upNodes); // kept, so the icon keeps turning
      } else if (mode === "selected") {
        mount(slotEl, icon("check", { size: 15, strokeWidth: 2.4 }), h("span", null, t("selected", { n: picked })));
      } else if (mode) {
        mount(slotEl, icon("check", { size: 15, strokeWidth: 2.4 }), h("span", null, t("banner.done", { sent: done.sent, total: done.total })));
      } else mount(slotEl);
      slotMode = mode;
    }

    const search = searchInput({
      placeholder: t("search"),
      value: S.q,
      debounce: 300,
      class: "o-search",
      onInput: (v) => {
        S.q = v.trim();
        S.page = 1;
        ctx.setQuery({ q: S.q || null, page: null });
        load();
      },
    });

    const bar = h("div", { class: "o-bar" }, tabsC.el, h("div", { class: "spacer" }), slotEl, search);
    const alerts = h("div", { class: "o-alerts" });

    // --------------------------------------------------------------- table

    function renderOrder(row) {
      return h(
        "div",
        { class: "o-cell" },
        h("span", { class: "o-no num" }, `#${row.receipt_id}`),
        h("span", { class: "o-sub o-date num", title: row.created ? dateTime(row.created) : null }, row.created ? `${date(row.created)} · ${time(row.created)}` : "–"),
        shipBy(row),
        row.status === "unshipped" ? h("button", { type: "button", class: "o-printbtn", onClick: () => openPrint(row) }, icon("layers", { size: 12 }), t("print.button")) : null,
      );
    }

    // Baskı dosyası: each item's print-ready file, made from its listing's print file
    // (drop/production.py). Local only: nothing is sent to Etsy.
    // Üretim ortağı bağla: the seller's own partner (a name, the address it takes orders at,
    // an optional token and the seller's costs). Saved on this computer only.
    async function openPartnerSetup(done) {
      let current = {};
      try {
        current = await ctx.api.get("/api/partner");
      } catch {
        /* a blank form */
      }
      const field = (label, input, hint) => h("label", { class: "o-pt-field" }, h("span", null, label), input, hint ? h("small", { class: "muted" }, hint) : null);
      const name = h("input", { type: "text", class: "input", value: current.name || "", placeholder: t("partner.f_name_ph") });
      const url = h("input", { type: "url", class: "input", value: current.url || "", placeholder: "https://" });
      const token = h("input", { type: "password", class: "input", placeholder: current.token_set ? t("partner.f_token_kept") : "", autocomplete: "off" });
      const costSqm = h("input", { type: "number", min: "0", step: "0.01", class: "input", value: current.cost_sqm ?? "" });
      const fixed = h("input", { type: "number", min: "0", step: "0.01", class: "input", value: current.fixed ?? "" });
      const files = h("input", { type: "checkbox", checked: !!current.send_files });
      const err = h("p", { class: "o-print-warn", hidden: true });
      const body = h(
        "div",
        { class: "o-pt-form" },
        h("p", { class: "muted" }, t("partner.intro")),
        field(t("partner.f_name"), name),
        field(t("partner.f_url"), url, t("partner.f_url_hint")),
        field(t("partner.f_token"), token),
        h("label", { class: "o-pt-check" }, files, h("span", null, t("partner.f_files"))),
        field(t("partner.f_cost"), costSqm, t("partner.f_cost_hint")),
        field(t("partner.f_fixed"), fixed),
        err,
      );
      const buttons = [
        { label: t("common.cancel"), variant: "ghost" },
      ];
      if (current.configured) {
        buttons.push({
          label: t("partner.remove"),
          variant: "ghost",
          onClick: async ({ close }) => {
            await ctx.api.post("/api/partner", { clear: true }).catch(() => {});
            close();
            if (done) done();
          },
        });
      }
      buttons.push({
        label: t("partner.save"),
        variant: "primary",
        onClick: async ({ close }) => {
          err.hidden = true;
          const payload = { name: name.value, url: url.value, send_files: files.checked, cost_sqm: costSqm.value, fixed: fixed.value };
          if (token.value) payload.token = token.value;
          try {
            await ctx.api.post("/api/partner", payload);
            close();
            if (done) done();
          } catch (e) {
            err.hidden = false;
            err.textContent = ctx.api.errorText(e, t);
          }
        },
      });
      modal({ title: t("partner.setup_title"), body, width: 520, actions: buttons });
    }

    const PRINT_DPI_KEY = "etsyotomasyon.orders.print_dpi";
    function savedDpi() {
      try {
        const v = Number(localStorage.getItem(PRINT_DPI_KEY));
        return v === 300 ? 300 : 150;
      } catch {
        return 150;
      }
    }
    const PRINT_BLEED_KEY = "etsyotomasyon.orders.print_bleed";
    function savedBleed() {
      try {
        const v = Number(localStorage.getItem(PRINT_BLEED_KEY));
        return [0.25, 0.5].includes(v) ? v : 0;
      } catch {
        return 0;
      }
    }
    function saveBleed(v) {
      try {
        localStorage.setItem(PRINT_BLEED_KEY, String(v));
      } catch {
        /* the choice is only remembered */
      }
    }
    function saveDpi(v) {
      try {
        localStorage.setItem(PRINT_DPI_KEY, String(v));
      } catch {
        /* the choice is only remembered */
      }
    }

    async function openPrint(row) {
      const body = h("div", { class: "o-print" }, h("p", { class: "muted" }, t("print.loading")));
      const makeBtn = button({ label: t("print.make"), icon: "layers", variant: "primary", disabled: true, onClick: () => make() });
      const folderBtn = button({
        label: t("print.folder"),
        icon: "folder-open",
        variant: "ghost",
        onClick: () => ctx.api.post(`/api/orders/${row.receipt_id}/print/open`).catch((err) => ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) })),
      });
      modal({ title: t("print.title", { id: row.receipt_id }), subtitle: t("print.sub"), body, width: 560, actions: [folderBtn, makeBtn] });
      let dpi = savedDpi();
      let bleed = savedBleed();
      const bleedSel = select({
        value: String(bleed),
        options: [{ value: "0", label: t("print.bleed_none") }, { value: "0.25", label: "0.25 in" }, { value: "0.5", label: "0.5 in" }],
        onChange: (v) => {
          bleed = Number(v);
          saveBleed(bleed);
          load();
        },
      });
      const dpiSel = select({
        value: String(dpi),
        options: [{ value: "150", label: "150 dpi" }, { value: "300", label: "300 dpi" }],
        onChange: (v) => {
          dpi = Number(v);
          saveDpi(dpi);
          load();
        },
      });
      function show(d) {
        const lines = d.items.map((it) => {
          const bits = [];
          if (it.plan) {
            bits.push(t("print.px", { w: it.plan.file_width_px, h: it.plan.file_height_px, dpi: it.plan.dpi }));
            if (it.plan.bleed_px) bits.push(t("print.bleed_in", { n: it.plan.bleed_in }));
            bits.push(it.plan.mode === "pattern" ? t("print.repeats", { n: it.plan.repeats }) : t("print.cover"));
            if (it.plan.lowered) bits.push(t("print.lowered", { dpi: it.plan.dpi }));
          }
          const seamBad = it.seam && !it.seam.ok;
          const soft = it.plan && it.plan.soft;
          const fixed = it.seam && it.seam.fixed && it.seam.ok;
          const warn = it.problem
            ? t(`print.problem.${it.problem}`)
            : seamBad
              ? t("print.seam", { a: it.seam.across, b: it.seam.down })
              : soft
                ? t("print.soft", { x: it.plan.upscale })
                : "";
          const note = !warn && fixed ? t("print.seam_fixed") : "";
          return h(
            "div",
            { class: cx("o-print-item", (it.problem || seamBad || soft) && "is-warn") },
            h("strong", null, `${it.quantity}× ${it.size ? it.size.label : it.title}`, it.material ? ` · ${it.material}` : ""),
            h("span", { class: "muted" }, bits.join(" · ") || "–"),
            warn ? h("span", { class: "o-print-warn" }, icon("alert", { size: 12 }), warn) : null,
            note ? h("span", { class: "o-print-done" }, icon("check", { size: 12 }), note) : null,
            it.made ? h("span", { class: "o-print-done" }, icon("check", { size: 12 }), it.file) : null,
          );
        });
        const sheet = (d.files || []).find((f) => /-invoice\.pdf$/i.test(f.name));
        mount(
          body,
          h("div", { class: "o-print-dpi" }, h("span", { class: "muted" }, t("print.quality")), dpiSel),
          h("div", { class: "o-print-dpi", title: t("print.bleed_tip") }, h("span", { class: "muted" }, t("print.bleed")), bleedSel),
          lines.length ? lines : h("p", { class: "muted" }, t("print.none")),
          sheet ? h("div", { class: "o-print-item" }, h("strong", null, t("print.invoice")), h("span", { class: "o-print-done" }, icon("check", { size: 12 }), sheet.name)) : null,
          partnerBlock(d),
        );
        makeBtn.disabled = !d.ready;
      }

      // The seller's own production partner: nothing is sent until the button is pressed.
      function partnerBlock(d) {
        const pt = d.partner || {};
        if (!pt.configured) {
          return h("div", { class: "o-print-item" }, h("strong", null, t("partner.title")), h("span", { class: "muted" }, t("partner.none")), button({ label: t("partner.connect"), icon: "link", variant: "secondary", size: "sm", onClick: () => openPartnerSetup(() => load()) }));
        }
        const cost = pt.cost;
        const loss = cost && pt.income !== null && pt.income !== undefined && cost.total >= pt.income;
        const money2 = (v) => `${pt.currency || ""} ${Number(v).toFixed(2)}`.trim();
        const sent = pt.sent && pt.sent.ok ? pt.sent : null;
        const send = button({
          label: sent ? t("partner.resend") : t("partner.send", { name: pt.name || t("partner.title") }),
          icon: "send",
          variant: sent ? "ghost" : "primary",
          size: "sm",
          disabled: !d.ready,
          onClick: () => confirmSend(d, !!sent),
        });
        return h(
          "div",
          { class: cx("o-print-item", loss && "is-warn") },
          h("strong", null, pt.name || t("partner.title")),
          cost ? h("span", { class: "muted" }, t("partner.cost", { cost: money2(cost.total), sqm: cost.sqm }), pt.income !== null && pt.income !== undefined ? ` · ${t("partner.income", { income: money2(pt.income) })}` : "") : null,
          loss ? h("span", { class: "o-print-warn" }, icon("alert", { size: 12 }), t("partner.loss")) : null,
          sent ? h("span", { class: "o-print-done" }, icon("check", { size: 12 }), t("partner.sent", { time: dateTime(sent.at) })) : null,
          h("div", { class: "o-print-actions" }, send, button({ label: t("partner.settings"), variant: "ghost", size: "sm", onClick: () => openPartnerSetup(() => load()) })),
        );
      }

      function confirmSend(d, again) {
        const name = (d.partner && d.partner.name) || t("partner.title");
        const dlg = modal({
          title: t("partner.confirm_title", { name }),
          body: h("p", null, t("partner.confirm_body", { name, id: row.receipt_id })),
          actions: [
            { label: t("common.cancel"), variant: "ghost" },
            {
              label: t("partner.send_now"),
              variant: "primary",
              icon: "send",
              onClick: async ({ close }) => {
                close();
                try {
                  const res = await ctx.api.post(`/api/orders/${row.receipt_id}/partner`, { confirm: true, force: again || undefined, dpi, bleed });
                  show(res);
                  ctx.toast({ tone: "success", title: t("partner.done", { name }) });
                } catch (err) {
                  ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
                }
              },
            },
          ],
        });
        return dlg;
      }
      async function make() {
        makeBtn.disabled = true;
        try {
          const d = await ctx.api.post(`/api/orders/${row.receipt_id}/print`, { dpi, bleed });
          show(d);
          ctx.toast({ tone: "success", title: t("print.done", { n: d.items.filter((i) => i.made).length }) });
        } catch (err) {
          makeBtn.disabled = false;
          ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
        }
      }
      async function load() {
        try {
          const plan = await ctx.api.get(`/api/orders/${row.receipt_id}/print`, { dpi, bleed }, { signal: ctx.signal });
          show(plan);
          // "Siparişi hazırla" is one click: whatever can be made is made now.
          if (plan.ready && plan.items.some((i) => i.plan && !i.made)) await make();
        } catch (err) {
          mount(body, h("p", { class: "o-print-warn" }, ctx.api.errorText(err, t)));
        }
      }
      await load();
    }

    // The ship-by date of an order still to send: late and due-today orders stand out,
    // since Etsy counts a late shipment against the shop.
    function shipBy(row) {
      if (row.status !== "unshipped" || !row.ship_by) return null;
      const days = Math.floor((row.ship_by * 1000 - Date.now()) / 86400000);
      const tone = days < 0 ? "late" : days <= 1 ? "soon" : "ok";
      const text = days < 0 ? t("ship.late", { n: -days }) : days === 0 ? t("ship.today") : days === 1 ? t("ship.tomorrow") : t("ship.by", { date: date(row.ship_by) });
      return h("span", { class: cx("o-shipby", `is-${tone}`), title: t("ship.title", { date: date(row.ship_by) }) }, icon("clock", { size: 11 }), text);
    }

    function renderBuyer(row) {
      const place = [row.city, row.state || row.country].filter(Boolean).join(", ") || row.country || "";
      return h(
        "div",
        { class: "o-cell" },
        h("span", { class: "o-name ellipsis" }, row.buyer || "–", row.is_gift ? h("span", { class: "o-gift" }, t("gift")) : null),
        place ? h("span", { class: "o-sub ellipsis" }, place) : null,
      );
    }

    function renderProduct(row) {
      const first = row.items[0];
      if (!first) return h("span", { class: "muted" }, "–");
      // "Kupa · 11oz", "Bez Çanta": the kind of product, then its variation.
      const kind = first.type && first.type !== "other" && t.has(`type.${first.type}`) ? t(`type.${first.type}`) : "";
      const parts = [kind, ...first.variations.map((v) => v.value || v.name)].filter(Boolean);
      if (first.quantity > 1) parts.push(t("qty", { n: first.quantity }));
      const more = row.items.length - 1;
      // An order still to make whose listing has no print file kept (drop/printfiles):
      // it cannot be printed until the design is added on the listing's page.
      const unprintable = row.status === "unshipped" ? row.items.filter((it) => it.print_file === false).length : 0;
      // A mark on the picture, so the size and colour beside it stay readable.
      const noPrint = unprintable
        ? h("span", { class: "o-noprint", role: "img", "aria-label": t("print.missing"), title: t("print.missing_tip", { n: unprintable }) }, icon("alert", { size: 11, strokeWidth: 2.4 }))
        : null;
      const sub =
        parts.length || more > 0
          ? h(
              "span",
              { class: "o-sub o-variant" },
              parts.length ? h("span", { class: "ellipsis" }, parts.join(" · ")) : null,
              more > 0 ? h("span", { class: "o-more" }, t("more_items", { n: more })) : null,
            )
          : null;
      const titles = row.items.map((it) => (it.quantity > 1 ? `${it.title} (${it.quantity})` : it.title)).join("\n");
      return h(
        "div",
        { class: "o-product", title: titles },
        h("span", { class: "o-thumb-wrap" }, thumb({ src: first.thumb, size: 36, icon: "box", alt: "" }), noPrint),
        h(
          "div",
          { class: "o-cell" },
          // The listing's page in the app (İlanlar detail), when Etsy says which listing.
          Number.isInteger(first.listing_id) && first.listing_id > 0
            ? h("a", { class: "o-title o-title-link ellipsis", href: `/ilanlar/${first.listing_id}`, title: shortTitle(first.title) }, shortTitle(first.title))
            : h("span", { class: "o-title ellipsis", title: shortTitle(first.title) }, shortTitle(first.title)),
          sub,
        ),
      );
    }

    function renderTotal(row) {
      return h("span", { class: "o-total num" }, row.total ? money(row.total) : "–");
    }

    const lastShipment = (row) => (row.shipments && row.shipments.length ? row.shipments[row.shipments.length - 1] : null);

    function carrierPill(name) {
      return h("span", { class: "o-carrier", title: name }, icon("truck", { size: 13 }), h("span", { class: "o-carrier-name" }, name));
    }

    function carrierWrapClass(wrap, value) {
      const bad = value && !carrierKnown(value);
      const country = S.carriers && S.carriers.country ? regionName(S.carriers.country) : "";
      wrap.className = cx("o-carrier", "is-edit", !value && "is-empty", bad && "is-bad");
      wrap.title = bad ? t("carrier.unknown", { name: value, country }) : value === OTHER ? t("carrier.other") : value || t("carrier.placeholder");
    }

    function carrierOptions(value) {
      const opts = [h("option", { value: "" }, t("carrier.placeholder"))];
      for (const name of carrierNames()) opts.push(h("option", { value: name }, name));
      opts.push(h("option", { value: OTHER }, t("carrier.other")));
      if (value && !carrierKnown(value)) opts.push(h("option", { value }, value));
      return opts;
    }

    function renderCarrier(row) {
      const id = row.receipt_id;
      if (!isEditable(row)) {
        const s = lastShipment(row);
        return s && s.carrier_name ? carrierPill(s.carrier_name) : h("span", { class: "muted" }, "–");
      }
      const value = canonicalCarrier(carrierFor(id));
      const sel = h("select", {
        class: "o-carrier-select",
        "aria-label": t("carrier.label"),
        disabled: !!S.job,
        dataset: { id: String(id) },
      }, carrierOptions(value));
      sel.value = value;
      const wrap = h("label", { class: "o-carrier is-edit" }, icon("truck", { size: 13 }), sel, h("span", { class: "o-chev" }, icon("chevron-down", { size: 12 })));
      carrierWrapClass(wrap, value);
      sel.addEventListener("change", () => {
        const edit = { ...(S.edits.get(id) || {}), carrier_name: sel.value };
        S.edits.set(id, edit);
        carrierWrapClass(wrap, sel.value);
        clearResult(id);
        if (!S.defaultCarrier && sel.value) {
          S.defaultCarrier = sel.value;
          refreshDefaultCarriers();
        }
        if (trackingFor(id)) ensureSelected(sel, id);
        saveDraftsSoon();
        syncSend();
      });
      return wrap;
    }

    /** Rows without their own carrier follow the default: update them in place (keeps focus). */
    function refreshDefaultCarriers() {
      for (const sel of el.querySelectorAll("select.o-carrier-select")) {
        const id = Number(sel.dataset.id);
        const own = S.edits.get(id);
        if (own && own.carrier_name) continue;
        const value = canonicalCarrier(S.defaultCarrier);
        mount(sel, carrierOptions(value));
        sel.value = value;
        carrierWrapClass(sel.closest(".o-carrier"), value);
      }
    }

    function renderTracking(row) {
      const id = row.receipt_id;
      if (!isEditable(row)) {
        const s = lastShipment(row);
        if (!s || !s.tracking_code) return h("span", { class: "muted o-track-none" }, "–");
        return h(
          "span",
          { class: "o-track", title: s.tracking_code },
          icon("check", { size: 13, strokeWidth: 2.4 }),
          h("span", { class: "mono ellipsis" }, s.tracking_code),
        );
      }
      const input = h("input", {
        type: "text",
        class: "o-track-input mono",
        value: (S.edits.get(id) || {}).tracking_code || "",
        placeholder: t("tracking.placeholder"),
        autocomplete: "off",
        spellcheck: "false",
        maxlength: "64",
        disabled: !!S.job,
        "aria-label": t("tracking.label", { id }),
        dataset: { id: String(id) },
      });
      input.addEventListener("input", () => {
        S.edits.set(id, { ...(S.edits.get(id) || {}), tracking_code: input.value });
        clearResult(id);
        if (input.value.trim()) ensureSelected(input, id);
        saveDraftsSoon();
        syncSend();
      });
      input.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault();
        const all = [...el.querySelectorAll("input.o-track-input:not(:disabled)")];
        const next = all[all.indexOf(input) + 1];
        if (next) next.focus();
        else input.blur();
      });
      return input;
    }

    function statusBadge(row) {
      const id = row.receipt_id;
      // A row waiting its turn keeps "Hazırlanıyor" until Etsy takes it (the pill shows
      // the progress); then it flips to "Kargoda ✓".
      const r = inFlight(id) ? null : S.results.get(id);
      if (r && r.status === "error") {
        return badge({ text: t("status.error"), tone: "danger", icon: "alert", title: rowErrorText(r) });
      }
      if (r && r.status === "skipped") {
        return badge({ text: t("status.skipped"), tone: "muted", title: rowErrorText(r) });
      }
      switch (row.status) {
        case "shipped":
          return badge({ text: t("status.shipped"), tone: "success", iconRight: "check" });
        case "delivered":
          return badge({ text: t("status.delivered"), tone: "neutral", iconRight: "check" });
        case "canceled":
          return badge({ text: t("status.canceled"), tone: "muted" });
        default:
          return badge({ text: t("status.unshipped"), tone: "warning", dot: true });
      }
    }

    function renderStatus(row) {
      return h("span", { class: "o-status", dataset: { sid: String(row.receipt_id) } }, statusBadge(row));
    }

    // Codes whose own words say more than Etsy's message; for the rest, Etsy's message.
    const WORDED = new Set(["tracking_restricted", "missing_scope", "not_waiting", "offline", "reconnect",
      "bad_keys", "rate_limited", "setup_needed", "cancelled"]);

    function rowErrorText(r) {
      const code = r.code || "internal";
      if ((WORDED.has(code) || !r.message) && t.has(`errors.${code}`)) {
        return t(`errors.${code}`, { message: r.message || "", status: r.http_status || "" });
      }
      if (code === "etsy_error" && r.http_status) {
        return `${t("errors.etsy_error", { status: r.http_status })} ${r.message || ""}`.trim();
      }
      return r.message || code;
    }

    function clearResult(id) {
      const r = S.results.get(id);
      if (!r || r.status === "ok") return;
      S.results.delete(id);
      const cell = el.querySelector(`[data-sid="${id}"]`);
      const row = S.known.get(id);
      if (cell && row) mount(cell, statusBadge(row));
      const tr = cell && cell.closest("tr");
      if (tr) tr.classList.remove("is-error");
    }

    /** Tick a row's checkbox the way a click would (no re-render: the input keeps focus).
     *  Only while some orders are ticked: with none, every ready order is sent anyway. */
    function ensureSelected(node, id) {
      if (S.selected.has(id) || !freshSelected()) return;
      const tr = node.closest("tr");
      const cb = tr && tr.querySelector("td.tbl-check input.checkbox");
      if (cb && !cb.checked) {
        cb.checked = true;
        cb.dispatchEvent(new Event("change"));
      }
    }

    /** Still flipping to "Kargoda" (and forgets flips that are over). */
    function isFlipping(id, now) {
      const at = S.flipAt.get(id);
      if (at === undefined) return false;
      if (now - at < FLIP_MS) return true;
      S.flipAt.delete(id);
      return false;
    }

    /** Job events re-render the rows several times a second: a flipping row carries on
     *  where its animation was (a negative delay) instead of starting over. The same
     *  goes for the "select all" cascade (--tick-delay). */
    function syncFlips() {
      const now = performance.now();
      for (const tr of tbl.el.querySelectorAll("tbody tr.is-flipping")) {
        const row = S.rows[Number(tr.dataset.index)];
        const at = row ? S.flipAt.get(row.receipt_id) : undefined;
        if (at !== undefined) tr.style.setProperty("--flip-delay", `${Math.round(at - now)}ms`);
      }
      if (S.tickAt === null) return;
      const step = tickStep();
      for (const tr of tbl.el.querySelectorAll("tbody tr.is-cascade")) {
        const i = Number(tr.dataset.index) || 0;
        tr.style.setProperty("--tick-delay", `${Math.round(i * step - (now - S.tickAt))}ms`);
      }
    }

    /** The gap between two rows of the cascade: 0.4 s for the whole page. */
    const tickStep = () => Math.min(TICK_STEP_MS, TICK_SPAN_MS / Math.max(1, S.rows.length));

    /** Still ticking in the "select all" cascade (and forgets a cascade that is over). */
    function isCascading(id, now) {
      if (S.tickAt === null) return false;
      if (now - S.tickAt >= TICK_SPAN_MS + TICK_MS + 50) {
        S.tickAt = null;
        S.tickIds = new Set();
        return false;
      }
      return S.tickIds.has(id);
    }

    /** Tick these orders, the ones on this page one after another (the video's cascade). */
    function tickRows(ids) {
      const next = new Set(S.selected);
      const fresh = ids.filter((id) => !next.has(id));
      for (const id of ids) next.add(id);
      S.selected = next;
      if (fresh.length && !reducedMotion()) {
        S.tickAt = performance.now();
        S.tickIds = new Set(fresh);
      }
      tbl.update(undefined, S.selected);
      syncFlips();
      syncHeadCheck();
      syncSend();
    }

    const emptyHost = h("div", { class: "o-empty" });
    const tbl = table({
      columns: [
        { key: "order", label: t("col.order"), width: 146, render: renderOrder },
        { key: "buyer", label: t("col.buyer"), width: 160, render: renderBuyer },
        { key: "product", label: t("col.product"), render: renderProduct },
        { key: "total", label: t("col.total"), width: 108, align: "right", render: renderTotal },
        { key: "carrier", label: t("col.carrier"), width: 131, render: renderCarrier, class: "o-td-carrier" },
        { key: "tracking", label: t("col.tracking"), width: 206, render: renderTracking, class: "o-td-tracking", headerClass: "o-th-tracking" },
        { key: "status", label: t("col.status"), width: 160, render: renderStatus },
      ],
      rows: [],
      rowKey: "receipt_id",
      // Only a waiting order can be ticked (for sending), and one sent from this page
      // keeps its tick (t280); t280's narrower check column.
      selectable: (row) => row.status === "unshipped" || S.results.has(row.receipt_id),
      checkWidth: 42,
      selected: S.selected,
      skeletonRows: S.perPage,
      empty: emptyHost,
      class: "o-table",
      rowClass: (row) => {
        const r = S.results.get(row.receipt_id);
        const now = performance.now();
        return cx(
          r && r.status === "error" && "is-error",
          !isEditable(row) && "is-done",
          isFlipping(row.receipt_id, now) && "is-flipping",
          isCascading(row.receipt_id, now) && "is-cascade",
        );
      },
      onSelectionChange: (sel) => {
        S.selected = sel;
        syncHeadCheck();
        syncSend();
      },
    });

    // On the waiting list the header checkbox ticks every waiting order, on every page
    // (t280: "select all, upload"). table() would tick only the rows on screen, so its
    // change is caught on the way down; other tabs and searches keep the per-page tick.
    const wholeList = () => S.tab === "unshipped" && !S.q && Array.isArray(S.waiting) && S.waiting.length > 0;
    tbl.el.addEventListener(
      "change",
      (e) => {
        if (!e.target.matches("thead input.checkbox") || !wholeList()) return;
        e.stopPropagation();
        selectWaiting(e.target.checked);
      },
      true,
    );

    function selectWaiting(on) {
      if (on) {
        tickRows(S.waiting);
        knowWaiting();
        return;
      }
      const next = new Set(S.selected);
      for (const id of S.waiting) next.delete(id);
      S.selected = next;
      S.tickAt = null;
      S.tickIds = new Set();
      tbl.update(undefined, S.selected);
      syncFlips();
      syncHeadCheck();
      syncSend();
    }

    /** The header checkbox against the whole waiting list: ticked, part (–) or empty. */
    function syncHeadCheck() {
      const head = tbl.el.querySelector("thead input.checkbox");
      if (!head || !wholeList()) return;
      const n = S.waiting.filter((id) => S.selected.has(id)).length;
      head.checked = n > 0 && n === S.waiting.length;
      head.indeterminate = n > 0 && n < S.waiting.length;
      head.disabled = false;
    }

    function remember(row) {
      const r = S.results.get(row.receipt_id);
      if (r && r.status === "ok") markShipped(row, r);
      S.known.set(row.receipt_id, row);
    }

    /** Read the waiting orders not loaded yet (other pages), so the send guard and the
     *  confirm list know them. The list is cached on the server: no extra Etsy call. */
    function knowWaiting() {
      if (S.knowing) return S.knowing;
      const waiting = S.waiting || [];
      const pages = new Set();
      waiting.forEach((id, i) => {
        if (!S.known.has(id)) pages.add(Math.floor(i / WAITING_PAGE) + 1);
      });
      if (!pages.size) return Promise.resolve();
      S.knowing = (async () => {
        try {
          for (const page of pages) {
            const data = await ctx.api.get("/api/orders", { tab: "unshipped", per_page: WAITING_PAGE, page }, { signal: ctx.signal });
            for (const row of data.rows || []) if (!S.known.has(row.receipt_id)) remember(row);
          }
        } catch (err) {
          if (!ctx.api.isAbort(err) && !(err instanceof ctx.api.ApiError)) console.warn("[orders] waiting", err);
        } finally {
          S.knowing = null;
        }
        if (ctx.isActive()) syncSend();
      })();
      return S.knowing;
    }

    const rangeEl = h("span", { class: "o-range" });
    const pag = pagination({
      page: S.page,
      pages: 1,
      onChange: (p) => {
        S.page = p;
        ctx.setQuery({ page: p > 1 ? p : null });
        load();
      },
    });
    // The footer stays while the list loads, is empty or failed: its ⋯ menu holds
    // "Listeyi yenile" and the Etsy link, needed most then. Only the range and pages go.
    const foot = h("div", { class: "o-foot" }, rangeEl, h("div", { class: "spacer" }), moreBtn, pag);
    const tableHost = h("div", { class: "o-table-host" }, tbl.el);
    const tableCard = card({ pad: false, class: "o-card", body: [tableHost, foot] });
    const note = h("p", { class: "o-note" }, icon("info", { size: 14 }), h("span", null, t("note")));

    const fileInput = h("input", { type: "file", accept: ".csv,text/csv,text/plain", class: "sr-only", tabindex: "-1", "aria-hidden": "true" });
    fileInput.addEventListener("change", () => {
      const file = fileInput.files && fileInput.files[0];
      fileInput.value = "";
      if (file) importCsv(file);
    });

    el.append(bar, alerts, tableCard, note, fileInput);

    function setEmpty() {
      const searching = !!(S.data && S.data.q);
      const key = searching ? "search" : S.tab;
      mount(
        emptyHost,
        emptyState({
          icon: searching ? "search" : "truck",
          title: t(`empty.${key}`),
          message: searching ? t("empty.search_msg") : S.tab === "unshipped" ? t("empty.unshipped_msg") : null,
          compact: true,
        }),
      );
    }

    function renderRows() {
      setEmpty();
      tbl.update(S.rows, S.selected);
      syncFlips();
      syncHeadCheck();
      const d = S.data;
      if (d && d.total > 0) {
        const from = (d.page - 1) * d.per_page + 1;
        const to = Math.min(d.total, from + S.rows.length - 1);
        const parts = rich(t("range", { from, to }), { total: h("b", { class: "num" }, String(d.total)) });
        if (d.truncated && d.q) parts.push(" · ", t("range.truncated", { n: d.scanned }));
        mount(rangeEl, parts);
      } else mount(rangeEl);
      rangeEl.hidden = !d || d.total === 0;
      pag.hidden = !d || d.pages <= 1;
      if (d) pag.update(d.page, d.pages);
      tabsC.update(tabItems());
      syncSend();
    }

    function showError(err) {
      mount(
        tableHost,
        h(
          "div",
          { class: "o-error" },
          infoNote({
            tone: "danger",
            icon: "alert",
            title: t("load_error"),
            text: ctx.api.errorText(err, t),
            action: button({ label: t("common.retry"), icon: "refresh", size: "sm", onClick: () => load({ fresh: true }) }),
          }),
        ),
      );
      rangeEl.hidden = true;
      pag.hidden = true;
    }

    function renderBanner() {
      const done = S.lastDone;
      renderSlot();
      const restricted = (S.summary && S.summary.tracking_restricted) || (done && done.restricted);
      const etsyLink = () =>
        h(
          "a",
          { class: "btn btn-secondary btn-sm", href: SOLD_URL, target: "_blank", rel: "noopener noreferrer" },
          h("span", { class: "btn-label" }, t("restricted.link")),
          icon("external", { size: 14 }),
        );
      let note = null;
      if (restricted) {
        note = infoNote({
          tone: "warning",
          icon: "alert",
          text: [t("restricted.text"), " ", h("span", { class: "muted" }, t("restricted.csv"))],
          action: etsyLink(),
        });
      } else if (mayBeRestricted()) {
        // Said before the first send, not only after Etsy's refusal: since 2024 Etsy
        // refuses tracking from newer keys for shops shipping from Türkiye. Gone for good
        // once Etsy has accepted a number from this key (summary.tracking_sent).
        note = infoNote({
          tone: "info",
          icon: "info",
          text: t("restricted.maybe"),
          action: [
            button({ label: t("csv.export"), icon: "download", variant: "secondary", size: "sm", onClick: () => exportCsv() }),
            etsyLink(),
          ],
        });
      }
      mount(alerts, note);
    }

    /** A Türkiye ship-from whose key has never had a tracking number accepted. */
    function mayBeRestricted() {
      const sum = S.summary;
      if (!sum || sum.tracking_sent !== false) return false;
      if (S.lastDone && S.lastDone.sent > 0) return false;
      return !!(S.carriers && S.carriers.country === "TR");
    }

    // --------------------------------------------------------------- loading

    async function load({ fresh = false } = {}) {
      const seq = ++S.loadSeq;
      if (!tableHost.contains(tbl.el)) mount(tableHost, tbl.el);
      tbl.update(null);
      rangeEl.hidden = true;
      pag.hidden = true;
      try {
        const data = await ctx.api.get(
          "/api/orders",
          { tab: S.tab, q: S.q || undefined, page: S.page, per_page: S.perPage, fresh: fresh ? 1 : undefined },
          { signal: ctx.signal },
        );
        if (seq !== S.loadSeq) return;
        if (!data.rows.length && data.total > 0 && S.page > data.pages) {
          S.page = data.pages;
          ctx.setQuery({ page: S.page > 1 ? S.page : null });
          load();
          return;
        }
        if (data.tab === "unshipped" && !data.q && Array.isArray(data.ids)) S.waiting = data.ids;
        // New orders get their print files before the seller looks (a background job).
        if (S.waiting && !S.autoPrint) {
          S.autoPrint = true;
          ctx.api.post("/api/orders/print-waiting", { dpi: savedDpi(), bleed: savedBleed() }).catch(() => {});
        }
        for (const row of data.rows) remember(row);
        // The carriers and numbers saved last time, before the first rows show (asked
        // after the list, so the server checks them against the waiting list it read).
        if (!S.draftsLoad) S.draftsLoad = loadDrafts();
        await S.draftsLoad;
        if (seq !== S.loadSeq) return;
        S.data = data;
        S.rows = data.rows;
        renderRows();
      } catch (err) {
        if (ctx.api.isAbort(err) || seq !== S.loadSeq) return;
        if (err.code === "setup_needed") {
          renderSetup();
          return;
        }
        showError(err);
      }
    }

    async function loadSummary(fresh = false) {
      try {
        S.summary = await ctx.api.get("/api/orders/summary", fresh ? { fresh: 1 } : null, { signal: ctx.signal });
        tabsC.update(tabItems());
        updateSubtitle();
        renderBanner();
      } catch (err) {
        // The list shows what went wrong; the counts only stay empty.
        if (!ctx.api.isAbort(err) && !(err instanceof ctx.api.ApiError)) console.warn("[orders] summary", err);
      }
    }

    function applyCarriers(c, changedCountry) {
      S.carriers = { ...c, carriers: c.carriers || [] };
      const last = canonicalCarrier(c.last_carrier || "");
      if (changedCountry || !S.defaultCarrier || !carrierKnown(S.defaultCarrier)) S.defaultCarrier = last;
      if (S.data) renderRows();
      renderBanner(); // the ship-from country decides the advance note
    }

    async function loadCarriers() {
      try {
        applyCarriers(await ctx.api.get("/api/orders/carriers", null, { signal: ctx.signal }), false);
      } catch (err) {
        // Without the list the carrier menu offers only "other"; the list shows the error.
        if (!ctx.api.isAbort(err) && !(err instanceof ctx.api.ApiError)) console.warn("[orders] carriers", err);
      }
    }

    async function changeCountry(code) {
      moreBtn.disabled = true;
      moreBtn.setAttribute("aria-busy", "true");
      try {
        applyCarriers(await ctx.api.post("/api/orders/country", { country: code }, { signal: ctx.signal }), true);
      } catch (err) {
        if (!ctx.api.isAbort(err)) ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
      } finally {
        moreBtn.disabled = false;
        moreBtn.removeAttribute("aria-busy");
      }
    }

    // --------------------------------------------------------------- drafts
    //
    // Carriers and numbers typed or imported but not sent yet are kept in the shop's home
    // (GET/POST /api/orders/drafts): the page opens again the way it was left, like the
    // video's rows with their carrier and number in place. Saved DRAFT_SAVE_MS after the
    // last change; leaving asks only while a save is pending or has failed.

    async function loadDrafts() {
      try {
        const res = await ctx.api.get("/api/orders/drafts", null, { signal: ctx.signal });
        let added = 0;
        for (const d of res.rows || []) {
          const id = Number(d.receipt_id);
          if (!Number.isInteger(id) || id <= 0 || S.edits.has(id) || S.results.has(id)) continue;
          S.edits.set(id, { carrier_name: d.carrier_name || "", tracking_code: d.tracking_code || "", note_to_buyer: d.note_to_buyer || "" });
          added += 1;
        }
        // Not ticked: the video's rows start with their numbers in and no ticks.
        if (added && S.data && ctx.isActive()) renderRows();
      } catch (err) {
        if (!ctx.api.isAbort(err) && !(err instanceof ctx.api.ApiError)) console.warn("[orders] drafts", err);
      }
    }

    /** The unsent carriers and numbers of waiting orders, as the server keeps them. */
    function draftRows() {
      const rows = [];
      for (const [id, e] of S.edits) {
        if (!e) continue;
        const r = S.results.get(id);
        if (r && r.status === "ok") continue;
        const row = S.known.get(id);
        if (row && row.status !== "unshipped") continue;
        const tracking = String(e.tracking_code || "").trim();
        const carrier = String(e.carrier_name || "").trim();
        if (!tracking && !carrier) continue;
        rows.push({ receipt_id: id, carrier_name: carrier, tracking_code: tracking, note_to_buyer: e.note_to_buyer || "" });
      }
      return rows.slice(-MAX_DRAFTS);
    }

    const draftsPending = () => !!drafts.timer || !!drafts.saving || drafts.failed;

    /** A save pending or failed: closing or reloading the tab asks first. */
    function syncDirty() {
      ctx.setDirty(draftsPending());
    }

    function saveDraftsSoon() {
      drafts.version += 1;
      clearTimeout(drafts.timer);
      drafts.timer = setTimeout(() => {
        drafts.timer = null;
        flushDrafts();
      }, DRAFT_SAVE_MS);
      syncDirty();
    }

    /** Save now; resolves true once what is on the page is saved. */
    async function flushDrafts() {
      clearTimeout(drafts.timer);
      drafts.timer = null;
      while (drafts.saving) await drafts.saving;
      if (drafts.saved === drafts.version && !drafts.failed) return true;
      const version = drafts.version;
      const wasFailing = drafts.failed;
      // No ctx.signal: a save started just before leaving must still arrive.
      drafts.saving = ctx.api
        .post("/api/orders/drafts", { rows: draftRows() })
        .then(() => {
          drafts.saved = Math.max(drafts.saved, version);
          drafts.failed = false;
        })
        .catch((err) => {
          drafts.failed = true;
          if (!wasFailing && ctx.isActive()) {
            ctx.toast({ tone: "warning", title: t("drafts.failed"), message: ctx.api.errorText(err, t), timeout: 8000 });
          }
        })
        .finally(() => {
          drafts.saving = null;
          if (ctx.isActive()) syncDirty();
        });
      await drafts.saving;
      return !drafts.failed && drafts.saved === drafts.version;
    }

    /** The tab is going away with a save pending: send it anyway (keepalive). */
    function flushDraftsOnExit() {
      if (!drafts.timer && !drafts.failed) return;
      clearTimeout(drafts.timer);
      drafts.timer = null;
      try {
        fetch("/api/orders/drafts", {
          method: "POST",
          keepalive: true,
          credentials: "same-origin",
          headers: { "X-Etsyotomasyon": "1", "Content-Type": "application/json" },
          body: JSON.stringify({ rows: draftRows() }),
        }).catch(() => {});
      } catch {
        /* the page is closing: nothing more to do */
      }
    }

    // --------------------------------------------------------------- the ship job

    function markShipped(row, r) {
      row.status = "shipped";
      row.shipments = r.shipments && r.shipments.length ? r.shipments : [{ carrier_name: r.carrier_name, tracking_code: r.tracking_code }];
    }

    /** One row's answer from the ship job; `live` when it just arrived (it flips). */
    function applyResult(r, live = false) {
      if (!r || r.receipt_id === undefined) return;
      S.results.set(r.receipt_id, r);
      const row = S.known.get(r.receipt_id);
      if (row && r.status === "ok") markShipped(row, r);
      if (live && r.status === "ok") S.flipAt.set(r.receipt_id, performance.now());
    }

    function attachJob(job, ids) {
      S.job = job;
      S.jobSeenAt = Date.now();
      if (ids) S.jobIds = ids;
      syncSend();
      if (S.data) renderRows();
    }

    async function finishJob(job) {
      if (S.finished.has(job.id)) return;
      S.finished.add(job.id);
      if (SENT_QUEUES.delete(job.id)) writeSent();
      let full = job;
      if (!job.result || job.status !== "done") {
        try {
          full = await ctx.api.get(`/api/jobs/${job.id}`, null, { signal: ctx.signal });
        } catch {
          full = job;
        }
      }
      const state = full.state || {};
      for (const r of (full.result && full.result.rows) || state.rows || []) applyResult(r);
      S.job = null;
      S.jobIds = [];
      clearTimeout(S.stopTimer);
      S.stopTimer = null;
      const res = full.result;
      if (full.status === "done" && res) {
        S.lastDone = res;
        doneToast(res);
      } else if (full.status === "cancelled") {
        S.lastDone = { sent: state.sent || 0, total: state.total || 0, restricted: false };
        ctx.toast({ tone: "info", title: t("done.cancelled", { sent: state.sent || 0 }) });
      } else if (full.status === "error") {
        ctx.toast({ tone: "danger", title: ctx.api.errorText(full.error || {}, t), timeout: 8000 });
      }
      renderRows();
      renderBanner();
      updateSubtitle();
      loadSummary(true);
    }

    function doneToast(res) {
      const skippedMsg = res.skipped && !res.stopped ? t("done.skipped", { n: res.skipped }) : "";
      if (res.restricted) {
        ctx.toast({ tone: "warning", title: t("done.restricted"), message: t("errors.tracking_restricted"), timeout: 9000 });
      } else if (res.sent > 0 && !res.failed) {
        const note = ctx.toast({
          tone: "success",
          title: t("done.title", { n: res.sent }),
          message: [`${t("done.msg")} · ${relative(Date.now())}`, skippedMsg].filter(Boolean).join(" "),
          timeout: 8000,
        });
        // The video's wider card with the haloed mint check (styled in orders.css).
        if (note && note.el) note.el.classList.add("o-toast-lg");
      } else if (res.sent > 0) {
        ctx.toast({ tone: "warning", title: t("done.partial", { sent: res.sent, failed: res.failed }), message: t("done.partial_msg"), timeout: 9000 });
      } else {
        const first = (res.rows || []).find((r) => r.status === "error");
        ctx.toast({ tone: "warning", title: t("done.nothing"), message: first ? rowErrorText(first) : skippedMsg, timeout: 9000 });
      }
    }

    // Typed numbers are saved in the shop's home: leaving (a link, Back, a shop switch, a
    // language change) saves what is pending first, and asks only when that fails.
    ctx.onBeforeLeave(async () => {
      if (!draftsPending() || (await flushDrafts())) return true;
      const n = draftRows().length;
      if (!n) return true;
      return ctx.confirm({ title: t("common:leave.title"), message: t("leave.unsaved", { n }), confirmLabel: t("common:leave.confirm"), danger: true });
    });

    ctx.events.on("job", (job) => {
      if (!job || job.kind !== "orders") return;
      if (S.job && job.id === S.job.id) {
        S.job = job;
        if (ACTIVE.has(job.status)) {
          // Progress only moves the pill; the rows change with each "row" event.
          syncSend();
          if (job.status === "running" && !S.jobIds.length) restoreQueue(job.id);
        } else finishJob(job);
      } else if (!S.job && ACTIVE.has(job.status) && !S.finished.has(job.id)) {
        attachJob(job, []); // started a moment ago (the answer is on its way) or in another tab
      }
    });

    ctx.events.on("job-event", (ev) => {
      if (!ev || ev.kind !== "orders" || ev.type !== "row" || !S.job || ev.job_id !== S.job.id) return;
      applyResult(ev.data, true);
      if (S.data) renderRows();
      else syncSend();
    });

    /** Show which rows a job sends (their carrier and number filled in). */
    function useQueue(queue) {
      for (const q of queue) {
        const id = Number(q.receipt_id);
        if (!S.results.has(id)) S.edits.set(id, { carrier_name: q.carrier_name, tracking_code: q.tracking_code });
      }
      return queue.map((q) => Number(q.receipt_id));
    }

    /** A job attached without its rows (started in another tab, or found queued): read
     *  them from the server once it runs. One request at a time. */
    async function restoreQueue(jobId) {
      if (S.queueFetch === jobId) return;
      S.queueFetch = jobId;
      try {
        const full = await ctx.api.get(`/api/jobs/${jobId}`, null, { signal: ctx.signal });
        const queue = (full.state && full.state.queue) || [];
        if (!queue.length || !S.job || S.job.id !== jobId || S.jobIds.length) return;
        S.jobIds = useQueue(queue);
        syncSend();
        if (S.data) renderRows();
      } catch (err) {
        if (!ctx.api.isAbort(err) && !(err instanceof ctx.api.ApiError)) console.warn("[orders] job", err);
      } finally {
        if (S.queueFetch === jobId) S.queueFetch = null;
      }
    }

    async function resumeJob() {
      try {
        const jobs = await ctx.api.get("/api/jobs", { kind: "orders" }, { signal: ctx.signal });
        const list = Array.isArray(jobs) ? jobs : [];
        // Remembered rows of jobs that ended while this page was away are not needed.
        const active = new Set(list.filter((j) => ACTIVE.has(j.status)).map((j) => j.id));
        const gone = [...SENT_QUEUES.keys()].filter((id) => !active.has(id));
        for (const id of gone) SENT_QUEUES.delete(id);
        if (gone.length) writeSent();
        const latest = list[0] || null;
        if (!latest) return;
        const full = await ctx.api.get(`/api/jobs/${latest.id}`, null, { signal: ctx.signal });
        for (const r of (full.state && full.state.rows) || []) applyResult(r);
        if (ACTIVE.has(full.status)) {
          // A queued job has no state yet: the rows this tab sent are remembered.
          const queue = (full.state && full.state.queue) || SENT_QUEUES.get(full.id) || [];
          attachJob(full, useQueue(queue));
        }
        else if (full.status === "done" && full.result) S.lastDone = full.result;
        renderBanner();
        updateSubtitle();
        if (S.data) renderRows();
      } catch (err) {
        if (!ctx.api.isAbort(err) && !(err instanceof ctx.api.ApiError)) console.warn("[orders] jobs", err);
      }
    }

    async function send() {
      if (S.job || S.posting) return; // Etsy is busy with the last send (aria-disabled)
      const plan = sendPlan();
      if (!plan.ids.length) {
        // Nothing to send yet: the numbers usually come from the carrier's CSV.
        fileInput.click();
        return;
      }
      // Nothing ticked: the ready orders get ticked (the video's "select all, upload").
      if (plan.tick) tickRows(plan.ids);
      // Orders on other pages: read them first, so the list below names each buyer.
      if (sendPlan().ids.some((id) => !S.known.has(id))) {
        S.posting = true;
        syncSend();
        try {
          await knowWaiting();
        } finally {
          S.posting = false;
        }
        if (!ctx.isActive()) return;
        syncSend();
        if (S.job) return;
      }
      const ids = sendPlan().ids;
      if (!ids.length) return;
      const list = ids.map((id) => ({
        receipt_id: id,
        carrier_name: canonicalCarrier(carrierFor(id)),
        tracking_code: trackingFor(id),
        note_to_buyer: (S.edits.get(id) || {}).note_to_buyer || undefined,
      }));
      const preview = h(
        "div",
        { class: "o-confirm-list" },
        list.slice(0, CONFIRM_PREVIEW).map((item) => {
          const row = S.known.get(item.receipt_id);
          return h(
            "div",
            { class: "o-confirm-row" },
            h(
              "div",
              { class: "o-cell" },
              h("span", { class: "o-no num" }, `#${item.receipt_id}`),
              h("span", { class: "o-sub ellipsis" }, row ? row.buyer : t("confirm.not_loaded")),
            ),
            carrierPill(item.carrier_name === OTHER ? t("carrier.other") : item.carrier_name),
            h("span", { class: "mono ellipsis o-confirm-track", title: item.tracking_code }, item.tracking_code),
          );
        }),
        list.length > CONFIRM_PREVIEW ? h("p", { class: "o-confirm-more" }, t("confirm.more", { n: list.length - CONFIRM_PREVIEW })) : null,
      );
      const restricted = S.summary && S.summary.tracking_restricted;
      const ok = await confirmSend(list.length, [
        h("p", { class: "modal-msg" }, t("confirm.msg", { n: list.length })),
        preview,
        restricted ? infoNote({ tone: "warning", icon: "alert", text: t("confirm.restricted") }) : null,
      ]);
      if (!ok || S.job || S.posting) return;
      S.posting = true;
      syncSend();
      // Old answers go now: the new job's first rows may arrive before the POST's answer.
      for (const id of ids) S.results.delete(id);
      try {
        // confirm: the seller said yes in the modal above; the server refuses without it.
        const job = await ctx.api.post("/api/orders/ship", {
          country: (S.carriers && S.carriers.country) || undefined,
          rows: list,
          confirm: true,
        });
        S.posting = false;
        if (!S.finished.has(job.id)) {
          SENT_QUEUES.set(job.id, list.map((x) => ({ receipt_id: x.receipt_id, carrier_name: x.carrier_name, tracking_code: x.tracking_code })));
          writeSent();
          S.lastDone = null;
          renderBanner();
          attachJob(job, ids);
        } else syncSend();
      } catch (err) {
        S.posting = false;
        if (err.code === "invalid" && err.params && Array.isArray(err.params.rows)) {
          for (const p of err.params.rows) {
            if (p.receipt_id) S.results.set(p.receipt_id, { receipt_id: p.receipt_id, status: "error", code: "invalid", message: p.message });
          }
          renderRows();
        }
        ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t), timeout: 8000 });
        syncSend();
      }
    }

    /** ctx.confirm, wide enough for the list of orders and tracking numbers. */
    function confirmSend(n, body) {
      return ctx.confirm({ title: t("confirm.title"), body, width: 540, class: "o-confirm", confirmLabel: t("confirm.ok", { n }), icon: "truck" });
    }

    async function stopJob() {
      if (!S.job) return;
      try {
        await ctx.api.post(`/api/jobs/${S.job.id}/cancel`, {});
      } catch (err) {
        ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
      }
    }

    // --------------------------------------------------------------- CSV

    function openCsvMenu() {
      const country = S.carriers && S.carriers.country;
      const showCountry = !!country && (S.tab === "unshipped" || S.tab === "all");
      menu(
        moreBtn,
        [
          { label: t("csv.export"), icon: "download", onClick: () => exportCsv() },
          { divider: true },
          { label: t("menu.refresh"), icon: "refresh", onClick: () => refreshAll() },
          { label: t("menu.etsy"), icon: "external", onClick: () => window.open(SOLD_URL, "_blank", "noopener") },
          showCountry ? { divider: true } : null,
          showCountry ? { label: t("country.item", { name: countryLabel(country) }), icon: "globe", onClick: () => openCountryMenu() } : null,
        ],
        { placement: "top-end", width: 290 },
      );
    }

    function refreshAll() {
      load({ fresh: true });
      loadSummary(true);
    }

    async function exportCsv() {
      const edits = {};
      for (const [id, e] of S.edits) {
        if (!e || !(e.tracking_code || "").trim()) continue;
        edits[id] = {
          tracking_code: e.tracking_code.trim(),
          carrier_name: canonicalCarrier(e.carrier_name || S.defaultCarrier),
          note_to_buyer: e.note_to_buyer || "",
        };
      }
      try {
        // The server names the file (and writes the BOM Excel needs for Turkish letters).
        await ctx.api.download("/api/orders/export.csv", {
          method: "POST",
          body: { tab: S.tab, edits },
          filename: `${t("csv.filename")}-${S.tab}-${today()}.csv`,
          signal: ctx.signal,
        });
        ctx.toast({ tone: "success", title: t("csv.exported") });
      } catch (err) {
        if (!ctx.api.isAbort(err)) ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
      }
    }

    async function importCsv(file) {
      let res;
      try {
        res = await ctx.api.upload("/api/orders/import-tracking", file, { method: "POST", signal: ctx.signal });
      } catch (err) {
        if (!ctx.api.isAbort(err)) ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t), timeout: 8000 });
        return;
      }
      if (!S.waiting) {
        // Not on the waiting list yet: read which orders wait, so no shipped one gets a number.
        try {
          const waiting = await ctx.api.get("/api/orders", { tab: "unshipped", per_page: S.perPage }, { signal: ctx.signal });
          if (Array.isArray(waiting.ids)) S.waiting = waiting.ids;
        } catch (err) {
          if (ctx.api.isAbort(err)) return;
        }
      }
      let unknown = 0;
      let notWaiting = 0;
      const filled = [];
      // Like a number typed in: it joins the ticked orders when some are ticked; with
      // none, the upload button counts every ready order anyway.
      const joinSelection = freshSelected() > 0;
      for (const r of res.rows || []) {
        const carrier = r.carrier_name ? canonicalCarrier(r.carrier_name) : "";
        const row = S.known.get(r.receipt_id);
        // Only orders that still wait for shipment (when the waiting list is known).
        if ((S.waiting && !S.waiting.includes(r.receipt_id)) || (row && !isEditable(row))) {
          notWaiting += 1;
          continue;
        }
        if (carrier && !carrierKnown(carrier)) unknown += 1;
        S.edits.set(r.receipt_id, { tracking_code: r.tracking_code, carrier_name: carrier, note_to_buyer: r.note_to_buyer || "" });
        S.results.delete(r.receipt_id);
        if (joinSelection) S.selected.add(r.receipt_id);
        filled.push(r.receipt_id);
      }
      S.selected = new Set(S.selected);
      if (filled.length) saveDraftsSoon();
      const errors = res.errors || [];
      const extra = [
        errors.length ? t("csv.import_errors", { n: errors.length, line: errors[0].line }) : "",
        unknown ? t("csv.import_unknown_carrier", { n: unknown }) : "",
        notWaiting ? t("csv.import_not_waiting", { n: notWaiting }) : "",
      ].filter(Boolean);
      if (!filled.length) {
        ctx.toast({ tone: "warning", title: t("csv.import_empty"), message: extra.join(" ") || null, timeout: 8000 });
        return;
      }
      ctx.toast({
        tone: extra.length ? "warning" : "success",
        title: t("csv.imported", { n: filled.length }),
        message: [t("csv.imported_msg"), ...extra].join(" "),
        timeout: 8000,
      });
      // Show the waiting list at the page of the first number filled in.
      const at = S.waiting ? S.waiting.indexOf(filled[0]) : -1;
      const page = at >= 0 ? Math.floor(at / S.perPage) + 1 : 1;
      if (S.tab !== "unshipped" || S.q || page !== S.page) {
        S.tab = "unshipped";
        S.q = "";
        S.page = page;
        search.value = "";
        ctx.setQuery({ tab: null, q: null, page: page > 1 ? page : null });
        tabsC.update(tabItems(), "unshipped");
        load();
      } else renderRows();
      // The numbers may be on other pages: know those orders before the confirm lists them.
      knowWaiting();
    }

    // --------------------------------------------------------------- start

    const onResize = () => {
      const n = pageSize();
      if (n === S.perPage) return;
      S.perPage = n;
      S.page = 1;
      load();
    };
    let resizeTimer = null;
    const resized = () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(onResize, 250);
    };
    window.addEventListener("resize", resized);
    window.addEventListener("pagehide", flushDraftsOnExit);

    await Promise.all([load(), loadSummary(), loadCarriers(), resumeJob()]);

    return () => {
      window.removeEventListener("resize", resized);
      window.removeEventListener("pagehide", flushDraftsOnExit);
      clearTimeout(resizeTimer);
      clearTimeout(S.stopTimer);
      // Unmounted with a save still waiting (the guard was not asked): send it now.
      flushDraftsOnExit();
    };
  },
};
