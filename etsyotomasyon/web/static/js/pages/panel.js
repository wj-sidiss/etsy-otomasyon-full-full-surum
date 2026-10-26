// Panel (/panel), as the video draws it: four KPI cards about etsyotomasyon's own work (drafts
// made this month, seconds per product, time saved, the drafts' SEO), "Son işlemler"
// (running tasks, then the history; reminders fill empty rows) and the "Tasarım yükle"
// drop card. The "Nasıl çalışıyor?" setup tiles sit on top while the setup is incomplete.
//
// GET /api/dashboard is local and instant (setup, recent, the KPIs, the last Etsy numbers);
// GET /api/dashboard/stats asks Etsy (cached server-side): the orders waiting to ship and
// the time of the last sync (the header pill, which also refreshes).

import { badge, button, card, cx, debounce, dropzone, emptyState, h, infoNote, mount, progressBar, setPendingDrop, spinner, svg, uid } from "../ui.js";
import { icon } from "../icons.js";
import { loadNamespaces, translate } from "../i18n.js";
import { lower, monthName, number, relative } from "../format.js";

const STEP_ICON = { account: "user", shop: "link", mockups: "image", designs: "upload" };
const STEP_LINK = { account: "/kurulum/magaza", shop: "/kurulum/magaza", mockups: "/kurulum/mockuplar", designs: "/tasarim-yukle" };
// Job kinds (as the server starts them) whose end changes the numbers.
const STAT_JOBS = new Set(["designs", "publish", "listings-import", "orders"]);
const FEED_ROWS = 5;
const KPIS = [
  { id: "drafts", icon: "file", tone: "accent", href: "/ilanlar?tab=draft" },
  { id: "speed", icon: "clock", tone: "info", href: "/tasarim-yukle" },
  { id: "saved", icon: "zap", tone: "success", href: "/tasarim-yukle" },
  { id: "seo", icon: "target", tone: "warning", href: "/seo" },
];
const BAR_SHAPE = [0.34, 0.5, 0.42, 0.62, 0.55, 0.78, 1]; // the empty card's bars (the video's)
// Notification -> feed row: icon, tone, and the Panel's own title / sub-line / badge.
const NOTE_ICON = { success: "check", warning: "alert", danger: "alert-circle", info: "info" };
const FEED = {
  "designs:notify.done": { icon: "upload", tone: "accent", title: "feed.designs_done", sub: "feed.designs_done_sub", badge: ["feed.badge.done", "success"] },
  "designs:notify.done_errors": { icon: "upload", tone: "accent", title: "feed.designs_done", sub: "feed.designs_errors_sub", badge: ["feed.badge.review", "warning"] },
  "designs:notify.cancelled": { icon: "upload", tone: "warning" },
  "designs:notify.stopped": { icon: "upload", tone: "danger", badge: ["feed.badge.review", "warning"] },
  "designs:notify.checked": { icon: "upload", tone: "neutral", sub: "feed.designs_checked_sub" },
  "designs:notify.failed": { icon: "upload", tone: "danger" },
  "orders:notify.shipped": { icon: "truck", tone: "info", title: "feed.orders_shipped", sub: (p) => carriers(p.carriers) },
  "orders:notify.failed": { icon: "truck", tone: "danger", badge: ["feed.badge.review", "warning"] },
  "orders:notify.restricted": { icon: "truck", tone: "warning" },
  "listings:notify.published": { icon: "list", tone: "success" },
  "listings:notify.publish_partial": { icon: "list", tone: "warning", badge: ["feed.badge.review", "warning"] },
  "listings:notify.imported": { icon: "list", tone: "accent" },
  "connect:notify.connected": { icon: "link", tone: "success" },
  "connect:notify.connected_plain": { icon: "link", tone: "success" },
  "pinterest:notify.connected": { icon: "pin", tone: "accent" },
  "pinterest:notify.posted": { icon: "pin", tone: "accent" },
  "pinterest:notify.posted_problems": { icon: "pin", tone: "warning", badge: ["feed.badge.review", "warning"] },
  "panel:notify.seo_suggest": { icon: "search", tone: "warning", sub: "feed.seo_sub", badge: ["feed.badge.review", "warning"] },
  "panel:notify.profit_month": { icon: "chart", tone: "success", title: "feed.profit_month", sub: "feed.profit_sub" },
  "panel:notify.mockups_saved": { icon: "image", tone: "neutral", sub: (p) => mockupNames(p.types) },
  "common:update.notify": { icon: "sparkles", tone: "accent" },
};
const PIPE = ["mockup", "title", "tags", "draft"];
// The video's MiniLine (PanelEkrani.tsx): from 0 at (2, 40) up to the total at (84, 4).
const LINE_SHAPE = "M2 40 C 14 38, 20 34, 30 30 S 48 22, 58 16 S 74 6, 84 4";

const fmt = (n) => String(Math.round(n * 10) / 10);

/**
 * An SVG path through `pts` ([x, y], x increasing) as a monotone cubic, the way
 * d3.curveMonotoneX draws it: each point's slope is limited by its neighbours' (zero at
 * a peak or a flat run), so the curve stays between every two points: no dip, no
 * overshoot. Two points make a straight line.
 */
function monotonePath(pts) {
  const n = pts.length;
  if (!n) return "";
  let d = `M${fmt(pts[0][0])} ${fmt(pts[0][1])}`;
  if (n === 1) return d;
  if (n === 2) return `${d} L${fmt(pts[1][0])} ${fmt(pts[1][1])}`;
  const h = [];
  const s = [];
  for (let i = 0; i < n - 1; i += 1) {
    h.push(pts[i + 1][0] - pts[i][0]);
    s.push(h[i] ? (pts[i + 1][1] - pts[i][1]) / h[i] : 0);
  }
  const m = new Array(n).fill(0);
  for (let i = 1; i < n - 1; i += 1) {
    const p = (s[i - 1] * h[i] + s[i] * h[i - 1]) / (h[i - 1] + h[i]);
    m[i] = (Math.sign(s[i - 1]) + Math.sign(s[i])) * Math.min(Math.abs(s[i - 1]), Math.abs(s[i]), 0.5 * Math.abs(p)) || 0;
  }
  m[0] = (3 * s[0] - m[1]) / 2;
  m[n - 1] = (3 * s[n - 2] - m[n - 2]) / 2;
  for (let i = 0; i < n - 1; i += 1) {
    const [x0, y0] = pts[i];
    const [x1, y1] = pts[i + 1];
    const dx = (x1 - x0) / 3;
    d += ` C${fmt(x0 + dx)} ${fmt(y0 + dx * m[i])}, ${fmt(x1 - dx)} ${fmt(y1 - dx * m[i + 1])}, ${fmt(x1)} ${fmt(y1)}`;
  }
  return d;
}

/** "UPS · USPS" from a list or a ready string (the orders notification's carriers). */
function carriers(value) {
  if (Array.isArray(value)) return value.filter((x) => typeof x === "string" && x).join(" · ") || null;
  return typeof value === "string" && value ? value : null;
}

/** "Tişört, kupa, poster" from mockup types (the Mockuplar page's names). */
function mockupNames(types) {
  if (!Array.isArray(types) || !types.length) return null;
  const names = types.filter((x) => typeof x === "string" && x).map((type) => translate("mockups", `mockups:type.${type}`));
  return names.map((name, i) => (i ? lower(name) : name)).join(", ") || null;
}

export default {
  async mount(el, ctx) {
    const t = ctx.t;
    let overview = null;
    let overviewAt = null; // when the overview was read (ms)
    let stats = null;
    let statsLoading = false;
    let refreshing = false;
    let statsSeq = 0;
    const timers = new Set();

    // ------------------------------------------------------------------ layout
    // The header pill: when the shop was last read from Etsy; a click reads it again.
    const syncBtn = h("button", { type: "button", class: "sync-pill", title: t("refresh"), onClick: refreshAll });
    ctx.setHeader({ actions: [syncBtn] });

    const setupEl = h("div", { class: "dash-setup-slot" });
    const noteEl = h("div", { class: "dash-note-slot" });
    const kpis = KPIS.map(buildKpi);
    const statsEl = h("section", { class: "dash-stats", "aria-label": t("kpi.label") }, kpis.map((k) => k.el));
    const feedBody = h("div", { class: "feed" });
    const feedCard = h(
      "section",
      { class: "card dash-card dash-feed", "aria-labelledby": "dash-feed-title" },
      h(
        "header",
        { class: "dash-card-head" },
        h("h2", { class: "dash-card-title", id: "dash-feed-title" }, t("recent.title")),
        h("button", { type: "button", class: "feed-all", onClick: () => ctx.openNotifications() }, t("recent.all"), icon("arrow-right", { size: 14 })),
      ),
      feedBody,
    );
    const upload = buildUploadCard();
    el.append(setupEl, noteEl, statsEl, h("div", { class: "dash-bottom" }, feedCard, upload.el));
    mount(feedBody, h("ul", { class: "feed-list is-loading" }, [0, 1, 2, 3].map(() => h("li", { class: "feed-row" }, h("span", { class: "skeleton feed-skel" })))));
    renderSync();

    // ------------------------------------------------------------------ data
    async function loadOverview() {
      try {
        overview = await ctx.api.get("/api/dashboard", null, { signal: ctx.signal });
        overviewAt = Date.now();
      } catch (err) {
        if (ctx.api.isAbort(err)) return;
        mount(noteEl, infoNote({ tone: "danger", icon: "alert", text: [h("b", null, t("error.load")), " ", ctx.api.errorText(err, t)] }));
        return;
      }
      if (!ctx.isActive()) return;
      if (!stats && overview.stats) stats = { available: true, error: null, stats: overview.stats };
      renderSetup();
      renderKpis();
      renderSync();
      await renderFeed();
    }

    async function loadStats(refresh = false) {
      const seq = ++statsSeq;
      statsLoading = true;
      renderSync();
      try {
        const data = await ctx.api.get("/api/dashboard/stats", refresh ? { refresh: 1 } : null, { signal: ctx.signal });
        if (seq !== statsSeq || !ctx.isActive()) return;
        statsLoading = false;
        stats = data;
      } catch (err) {
        if (ctx.api.isAbort(err) || seq !== statsSeq) return;
        statsLoading = false;
        stats = { available: true, error: { code: err.code, message: err.message, params: err.params }, stats: {} };
      }
      renderStatsNote();
      renderSync();
      renderFeed();
    }

    async function refreshAll() {
      if (refreshing) return;
      refreshing = true;
      renderSync();
      try {
        // The setup state too (keys, connection, folders): a problem fixed elsewhere clears here.
        await Promise.all([loadOverview(), loadStats(true), ctx.refreshStatus(true).catch(() => {})]);
      } finally {
        refreshing = false;
        if (ctx.isActive()) renderSync();
      }
    }

    function newest(all) {
      let best = null;
      for (const s of Object.values(all || {})) if (s && s.cached_at && (!best || s.cached_at > best)) best = s.cached_at;
      return best;
    }

    // ------------------------------------------------------------------ header pill
    function renderSync() {
      const err = stats && stats.error;
      const idle = (err && err.code === "setup_needed") || (stats && stats.available === false);
      syncBtn.hidden = !!idle;
      if (idle) return;
      const cachedAt = stats ? newest(stats.stats) : null;
      const busy = refreshing || statsLoading || !stats;
      let tone = "success";
      let label;
      let aria;
      if (busy && !cachedAt) {
        tone = "neutral";
        label = [spinner({ size: 12 }), h("span", null, t("sync.running"))];
        aria = t("sync.aria_running");
      } else if (err && !cachedAt) {
        tone = "warning";
        label = [h("span", { class: "sync-dot", "aria-hidden": "true" }), h("span", null, t("sync.failed"))];
        aria = t("sync.aria_failed");
      } else {
        const time = relative(cachedAt);
        label = [busy ? spinner({ size: 12 }) : h("span", { class: "sync-dot", "aria-hidden": "true" }), h("span", null, t("sync.done", { time }))];
        aria = t("sync.aria", { time });
      }
      syncBtn.className = cx("sync-pill", `tone-${tone}`, busy && "is-busy");
      syncBtn.setAttribute("aria-label", aria);
      syncBtn.setAttribute("aria-busy", busy ? "true" : "false");
      mount(syncBtn, label);
    }

    function renderStatsNote() {
      const err = stats && stats.error;
      if (!err || err.code === "setup_needed") {
        mount(noteEl);
        return;
      }
      const retry = button({ label: t("common.retry"), icon: "refresh", size: "sm", autoLoading: true, onClick: () => loadStats(true) });
      mount(noteEl, infoNote({ tone: "warning", icon: "alert", text: t("stats.error_all", { reason: ctx.api.errorText(err, t) }), action: retry }));
    }

    // ------------------------------------------------------------------ setup tiles (slide t150)
    function renderSetup() {
      const setup = overview && overview.setup;
      if (!setup || setup.complete || setup.checking) {
        mount(setupEl);
        return;
      }
      const tilesEls = setup.steps.map((step, i) => setupTile(step, i, setup));
      const body = h(
        "div",
        { class: "setup-flow" },
        h(
          "div",
          { class: "setup-brackets", "aria-hidden": "true" },
          h("div", { class: "setup-bracket is-once" }, h("span", null, t("setup.once"))),
          h("div", { class: "setup-bracket is-every" }, h("span", null, t("setup.every"))),
        ),
        h("div", { class: "setup-tiles" }, tilesEls),
      );
      mount(setupEl, card({ title: t("setup.title"), subtitle: t("setup.subtitle"), icon: "sparkles", body, class: "dash-setup" }));
    }

    function setupTile(step, i, setup) {
      let sub = t(`setup.${step.id}_sub`);
      if (step.id === "mockups" && setup.mockups > 0) sub = t("setup.mockups_n", { n: setup.mockups });
      if (step.id === "designs") sub = setup.template_title ? t("setup.designs_ready", { title: setup.template_title }) : t("setup.designs_template");
      if (step.state !== "done" && step.id === "account" && setup.problem === "bad_keys") sub = t("setup.bad_keys");
      if (step.state !== "done" && step.id === "shop" && setup.problem === "reconnect") sub = t("setup.reconnect");
      let href = STEP_LINK[step.id];
      if (step.id === "designs" && !setup.template_title && step.state !== "done") href = "/kurulum/sablon";
      const stateLabel = h(
        "span",
        { class: cx("setup-state", `is-${step.state}`) },
        step.state === "done" ? icon("check", { size: 12, strokeWidth: 2.6 }) : null,
        // The fourth step is done every time: "done" there means ready to upload.
        t(step.id === "designs" && step.state === "done" ? "setup.state.ready" : `setup.state.${step.state}`),
      );
      return h(
        "a",
        { class: cx("setup-tile", `is-${step.state}`, step.id === "designs" && "is-every"), href, "aria-current": step.state === "current" ? "step" : undefined },
        h(
          "span",
          { class: "setup-tile-top" },
          h("span", { class: cx("icon-tile", step.state === "done" ? "tone-success" : step.state === "current" ? "tone-accent" : "tone-neutral") }, icon(step.state === "done" ? "check" : STEP_ICON[step.id], { size: 16 })),
          h("span", { class: "setup-num num", "aria-hidden": "true" }, String(i + 1)),
        ),
        h("span", { class: "setup-tile-title" }, t(`setup.${step.id}`)),
        h("span", { class: "setup-tile-sub" }, sub),
        h("span", { class: "setup-tile-foot" }, stateLabel, step.state === "current" ? h("span", { class: "setup-go" }, t("setup.go"), icon("arrow-right", { size: 13 })) : null),
      );
    }

    // ------------------------------------------------------------------ KPI cards (the video's Stat)
    function buildKpi(d) {
      const valueEl = h("p", { class: "kpi-value num" }, h("span", { class: "skeleton", style: { width: 72, height: 26 } }));
      const subEl = h("p", { class: "kpi-sub" }, h("span", { class: "skeleton", style: { width: "60%", height: 10 } }));
      const vizEl = h("div", { class: "kpi-viz", "aria-hidden": "true" });
      const el = h(
        "a",
        { class: cx("kpi", `kpi-${d.id}`, `tone-${d.tone}`), href: d.href },
        h("div", { class: "kpi-head" }, h("span", { class: "kpi-icon" }, icon(d.icon, { size: 16 })), h("span", { class: "kpi-label" }, t(`kpi.${d.id}`))),
        valueEl,
        subEl,
        vizEl,
      );
      return { id: d.id, el, valueEl, subEl, vizEl };
    }

    function renderKpis() {
      const w = overview && overview.work;
      if (!w) return;
      const drafts = w.drafts_month || 0;
      const manual = w.manual_minutes || 20;
      for (const k of kpis) {
        let value = "–";
        let sub = "";
        let viz = null;
        if (k.id === "drafts") {
          value = number(drafts);
          sub = drafts > 0 ? t("kpi.drafts_today", { n: number(w.drafts_today || 0) }) : t("kpi.none");
          viz = miniBars(w.daily || []);
        } else if (k.id === "speed") {
          const s = w.seconds_per_item;
          if (typeof s === "number" && s > 0) value = s < 60 ? t("kpi.speed_s", { n: Math.max(1, Math.round(s)) }) : t("kpi.speed_m", { n: number(s / 60, 1) });
          sub = t("kpi.speed_sub");
          viz = h("span", { class: "kpi-pill" }, t("kpi.manual"), " ", h("s", null, t("kpi.manual_time", { n: manual })));
        } else if (k.id === "saved") {
          const minutes = w.minutes_saved || 0;
          if (minutes > 0) value = minutes >= 60 ? t("kpi.saved_h", { n: Math.round(minutes / 60) }) : t("kpi.saved_m", { n: minutes });
          // n stays a number below 1,000 so that "1 product" finds its singular text.
          sub = drafts > 0 ? t("kpi.saved_sub", { n: drafts < 1000 ? drafts : number(drafts), m: manual }) : t("kpi.none");
          viz = miniLine(w.cumulative || []);
        } else if (k.id === "seo") {
          const seo = w.seo_avg;
          if (typeof seo === "number") value = t("kpi.seo_value", { n: seo });
          sub = typeof seo === "number" ? t("kpi.seo_sub") : t("kpi.none");
          viz = ring(typeof seo === "number" ? seo : null);
        }
        k.valueEl.textContent = value;
        k.subEl.textContent = sub;
        k.el.classList.toggle("is-empty", value === "–" || (k.id === "drafts" && drafts === 0));
        mount(k.vizEl, viz);
      }
    }

    // Seven small bars, one per day (today last, in the lighter violet).
    function miniBars(daily) {
      const values = daily.length ? daily.slice(-7) : [];
      const max = Math.max(0, ...values);
      const heights = max > 0 ? values.map((v) => Math.max(3, Math.round((40 * v) / max))) : BAR_SHAPE.map(() => 3);
      return h("span", { class: "kpi-bars" }, heights.map((px, i) => h("span", { class: cx("kpi-bar", i === heights.length - 1 && "is-last"), style: { height: px } })));
    }

    // The month's drafts added up day by day: a smooth rising line with a soft area under
    // it (the video's MiniLine). Uploads come in batches, so the days on which the total
    // stays the same are left out (the first day, every day it grows and today stay): the
    // line rises across the gaps instead of drawing steps. A monotone cubic joins the
    // points, so it never dips or overshoots; with only the start and today left it is
    // the video's own curve. No drafts: a flat line.
    function miniLine(cumulative) {
      const values = [0, ...cumulative];
      const max = Math.max(0, ...values);
      const last = values.length - 1;
      const x = (i) => 2 + (82 * i) / Math.max(1, last);
      let pts;
      let d;
      if (max <= 0) {
        pts = [[2, 40], [84, 40]];
        d = "M2 40 L84 40";
      } else {
        pts = [];
        values.forEach((v, i) => {
          if (i === 0 || i === last || v !== values[i - 1]) pts.push([x(i), 40 - (36 * v) / max]);
        });
        // Two points are always 0 at the start and the month's total today (the video's curve).
        d = pts.length === 2 && pts[1][1] === 4 ? LINE_SHAPE : monotonePath(pts);
      }
      const end = pts[pts.length - 1];
      const gid = uid("kpi-area");
      return svg(
        "svg",
        { class: "kpi-line", viewBox: "0 0 86 46", width: 76, height: 40 },
        svg("defs", null, svg("linearGradient", { id: gid, x1: "0", y1: "0", x2: "0", y2: "1" }, svg("stop", { offset: "0", "stop-color": "currentColor", "stop-opacity": "0.28" }), svg("stop", { offset: "1", "stop-color": "currentColor", "stop-opacity": "0" }))),
        max > 0 ? svg("path", { d: `${d} L ${fmt(end[0])} 46 L 2 46 Z`, fill: `url(#${gid})` }) : null,
        svg("path", { d, fill: "none", stroke: "currentColor", "stroke-width": 2.6, "stroke-linecap": "round", "stroke-linejoin": "round" }),
        max > 0 ? svg("circle", { cx: fmt(end[0]), cy: fmt(end[1]), r: 3.5, fill: "currentColor" }) : null,
      );
    }

    // An amber ring without a number (the value is already the card's).
    function ring(value) {
      const v = value === null ? 0 : Math.max(0, Math.min(100, value));
      return svg(
        "svg",
        { class: "kpi-ring", viewBox: "0 0 54 54", width: 47, height: 47 },
        svg("circle", { cx: 27, cy: 27, r: 22, fill: "none", stroke: "rgba(255,255,255,0.08)", "stroke-width": 6 }),
        v > 0
          ? svg("circle", { cx: 27, cy: 27, r: 22, fill: "none", stroke: "currentColor", "stroke-width": 6, "stroke-linecap": "round", pathLength: 100, "stroke-dasharray": `${v} 100`, transform: "rotate(-90 27 27)" })
          : null,
      );
    }

    // ------------------------------------------------------------------ Son işlemler
    async function renderFeed() {
      if (!overview) return;
      const items = overview.recent || [];
      const namespaces = new Set();
      for (const it of items) {
        if (it.type === "notification" && it.ns) namespaces.add(it.ns);
        if (it.type === "notification" && it.ns === "panel" && it.key === "notify.mockups_saved") namespaces.add("mockups");
        const m = it.type === "job" && /^([a-z][a-z0-9-]*):/.exec(it.title_key || "");
        if (m) namespaces.add(m[1]);
      }
      namespaces.delete("common");
      if (namespaces.size) await loadNamespaces([...namespaces]);
      if (!ctx.isActive()) return;
      // The video's card is a history: running and queued tasks, then the finished work
      // newest first (the server's order). Reminders only fill the rows left empty, so a
      // shop with orders to ship never pushes its history out.
      const rows = [];
      for (const it of items) if (it.type === "job") rows.push(jobRow(it));
      for (const it of items) if (it.type === "notification") rows.push(noteRow(it));
      if (rows.length < FEED_ROWS) rows.push(...reminderRows().slice(0, FEED_ROWS - rows.length));
      if (!rows.length) {
        mount(feedBody, emptyState({ icon: "clock", title: t("recent.empty"), message: t("recent.empty_msg"), compact: true }));
        return;
      }
      mount(feedBody, h("ul", { class: "feed-list" }, rows.slice(0, FEED_ROWS)));
    }

    function feedRow({ icon: ic, tone, title, sub, badgeText, badgeTone, time, link, extra }) {
      const inner = [
        h("span", { class: cx("feed-icon", `tone-${tone || "neutral"}`) }, typeof ic === "string" ? icon(ic, { size: 18 }) : ic),
        h("span", { class: "feed-text" }, h("span", { class: "feed-title" }, title), sub ? h("span", { class: "feed-sub" }, sub) : null, extra || null),
        badgeText ? badge({ text: badgeText, tone: badgeTone || "neutral", size: "sm" }) : null,
        h("span", { class: "feed-time" }, time || ""),
      ];
      return h("li", { class: "feed-row" }, link ? h("a", { class: "feed-link", href: link }, inner) : h("div", { class: "feed-link" }, inner));
    }

    function jobRow(it) {
      const p = it.progress || {};
      const bar = it.status === "running" ? progressBar({ value: p.total ? p.done : null, max: p.total || 100, size: "sm" }).el : null;
      const title = it.title_key ? translate(ctx.page, it.title_key, it.params || {}) : it.kind;
      return feedRow({
        icon: it.status === "running" ? spinner({ size: 16, tone: "accent" }) : "clock",
        tone: "accent",
        title,
        sub: [t(`recent.${it.status}`), p.total ? h("span", { class: "num" }, ` · ${p.done}/${p.total}`) : null],
        extra: bar,
        time: relative(it.at),
        link: it.kind === "designs" ? "/tasarim-yukle" : null,
      });
    }

    // What still waits on the seller: orders to ship, Pins to look at. They fill the feed's
    // empty rows only (below the history), each timed by when it was last read: the orders
    // from Etsy, the Pins from the local queue with the overview.
    function reminderRows() {
      const out = [];
      const ship = stats && stats.stats && stats.stats.to_ship;
      const toShip = ship ? ship.value : null;
      if (toShip > 0) {
        out.push(feedRow({ icon: "truck", tone: "warning", title: t("feed.to_ship", { n: toShip }), sub: t("feed.to_ship_sub"), badgeText: t("feed.badge.review"), badgeTone: "warning", time: relative(ship.cached_at || overviewAt), link: "/siparisler" }));
      }
      const pin = overview && overview.pinterest;
      if (pin && pin.attention > 0) {
        out.push(feedRow({ icon: "pin", tone: "warning", title: t("feed.pin_attention", { n: pin.attention }), sub: t("feed.pin_sub"), badgeText: t("feed.badge.review"), badgeTone: "warning", time: relative(overviewAt), link: "/pinterest" }));
      } else if (pin && pin.due > 0) {
        out.push(feedRow({ icon: "pin", tone: "accent", title: t("feed.pin_due", { n: pin.due }), sub: t("feed.pin_sub"), time: relative(overviewAt), link: "/pinterest" }));
      }
      return out;
    }

    function noteRow(it) {
      const ns = it.ns || "common";
      const params = it.params || {};
      const spec = FEED[`${ns}:${it.key}`] || {};
      const tone = spec.tone || it.tone || "info";
      const text = (key) => translate("panel", `panel:${key}`, params);
      let title = translate(ns, `${ns}:${it.key}`, params);
      if (spec.title === "feed.profit_month") {
        const m = Number(String(params.month || "").split("-")[1]);
        if (m) title = t("feed.profit_month", { month: monthName(m - 1) });
      } else if (spec.title) {
        title = text(spec.title);
      }
      const sub = typeof spec.sub === "function" ? spec.sub(params) : spec.sub ? text(spec.sub) : null;
      return feedRow({
        icon: spec.icon || NOTE_ICON[it.tone] || "info",
        tone,
        title,
        sub,
        badgeText: spec.badge ? t(spec.badge[0]) : null,
        badgeTone: spec.badge ? spec.badge[1] : null,
        time: relative(it.at),
        link: it.link || null,
      });
    }

    // ------------------------------------------------------------------ Tasarım yükle (drop card)
    // Files dropped here go to Tasarım Yükle, which uploads them with its own rules
    // (folders, download files, limits): the same as a drop on that page.
    function buildUploadCard() {
      const cardsHost = h("span", { class: "up-cards" });
      const fan = h("span", { class: "up-fan is-empty", "aria-hidden": "true" }, cardsHost, h("span", { class: "up-badge" }, icon("upload", { size: 21, strokeWidth: 2.2 })));
      const pick = button({ label: t("upload.button"), icon: "upload", variant: "primary", size: "lg", class: "up-btn", onClick: () => dz.open() });
      pick.tabIndex = -1; // the whole drop area is the one control (Enter or Space opens the picker)
      pick.setAttribute("aria-hidden", "true");
      const dz = dropzone({
        class: "up-drop",
        title: t("upload.drop"),
        subtitle: t("upload.sub"),
        multiple: true,
        content: [fan, h("p", { class: "up-title" }, t("upload.drop")), h("p", { class: "up-sub" }, t("upload.sub")), pick],
        onFiles: (list) => {
          setPendingDrop(list);
          ctx.navigate("/tasarim-yukle");
        },
      });
      const pipe = h(
        "ol",
        { class: "up-pipe", "aria-label": t("upload.pipe_label") },
        PIPE.map((step, i) => h("li", { class: cx("up-chip", i === PIPE.length - 1 && "is-last") }, i > 0 ? icon("arrow-right", { size: 12, class: "up-arrow" }) : null, h("span", null, t(`upload.pipe.${step}`)))),
      );
      const el = h(
        "section",
        { class: "card dash-card dash-upload", "aria-labelledby": "dash-upload-title" },
        h("header", { class: "dash-card-head" }, h("h2", { class: "dash-card-title", id: "dash-upload-title" }, t("upload.title")), badge({ text: t("upload.badge"), tone: "accent", icon: "zap", size: "sm" })),
        dz,
        pipe,
      );

      // Three designs waiting in 2-PRODUCTS fan out above the text (none: the badge alone).
      async function loadFan() {
        let data;
        try {
          data = await ctx.api.get("/api/designs/pending", null, { signal: ctx.signal });
        } catch {
          return;
        }
        if (!ctx.isActive()) return;
        const picks = ((data && data.items) || []).filter((x) => x && x.thumb_path).slice(0, 3);
        if (!picks.length) return;
        // The middle card is drawn last (on top), as in the video.
        const order = picks.length === 3 ? [picks[1], picks[2], picks[0]] : picks;
        const slots = picks.length === 3 ? ["is-left", "is-right", "is-mid"] : picks.length === 2 ? ["is-left", "is-right"] : ["is-mid"];
        mount(
          cardsHost,
          order.map((x, i) =>
            h("span", { class: cx("up-card", slots[i]) }, h("img", { src: ctx.api.url("/api/files/thumb", { path: x.thumb_path, w: 240, v: x.mtime }), alt: "", loading: "lazy", decoding: "async" })),
          ),
        );
        fan.classList.remove("is-empty");
      }
      return { el, loadFan };
    }

    // ------------------------------------------------------------------ live updates
    const reloadOverview = debounce(() => loadOverview(), 250);
    const reloadStats = debounce(() => loadStats(true), 1500);
    ctx.events.on("notification", () => reloadOverview());
    ctx.events.on("job", (job) => {
      reloadOverview();
      if (job && STAT_JOBS.has(job.kind) && ["done", "error", "cancelled"].includes(job.status)) reloadStats();
    });
    ctx.onStatus((s, prev) => {
      reloadOverview();
      const was = prev && prev.state;
      if (s.state === "connected" && was !== "connected" && !statsLoading) loadStats(was === "reconnect" || was === "offline");
    });
    // Relative times ("5 dk önce") age while the page stays open.
    const tick = setInterval(() => {
      if (!ctx.isActive()) return;
      renderSync();
      if (overview) renderFeed();
    }, 60000);
    timers.add(tick);

    upload.loadFan();
    await loadOverview();
    loadStats(false);

    return () => {
      reloadOverview.cancel();
      reloadStats.cancel();
      for (const id of timers) clearInterval(id);
    };
  },
};
