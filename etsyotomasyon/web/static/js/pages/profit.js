// Kâr-Zarar (/kar-zarar): revenue, Etsy fees, product cost, shipping and net profit for a
// month, per product, and the last six months. Frames t290 / t300 of the video.
//
// Data: GET /api/profit?month=YYYY-MM. When Etsy data for a month was never fetched the
// answer is {state: "loading", job} and a "profit" job reads it; the page shows skeletons
// and the job's progress, then asks again. Costs live in costs.json on this computer
// (GET/POST /api/profit/costs); TRY amounts use TCMB's rate (GET/POST /api/profit/fx).

import {
  badge,
  button,
  barChart,
  card,
  cx,
  emptyState,
  h,
  infoNote,
  menu,
  mount,
  popover,
  progressBar,
  searchInput,
  select,
  skeleton,
  spinner,
  statCard,
  svg,
  table,
  textInput,
  thumb,
} from "../ui.js";
import { icon } from "../icons.js";
import { date as fmtDate, money, monthName, number, percent, relative } from "../format.js";

const SORTS = ["sales", "profit", "margin"];
const DISPLAYS = ["primary", "secondary", "both"];
const TOP_N = 6;
const BUCKETS = ["listing", "transaction", "processing", "ads", "other"];
const TYPES = ["tshirt", "sweatshirt", "hoodie", "mug", "poster", "canvas", "phone_case", "tote", "pillow", "sticker", "other"];
const JOB_POLL_MS = 2500;
// The opening of the video's Kâr-Zarar: every amount counts up from 0 and the bars grow
// (the chart's one after another); then the money-losing row pulses three times.
const COUNT_MS = 900;
const PULSE_END_MS = 900 + 3 * 2400 + 200; // matches the delay and cycles in profit.css

const easeOutCubic = (x) => 1 - Math.pow(1 - x, 3);

function reducedMotion() {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

/** "-$6.60" -> "−$6.60", "-%8,8" -> "−%8,8" (a real minus sign, as in the video). */
function minus(text) {
  return typeof text === "string" ? text.replace(/^-/, "−") : text;
}

/** "Retro Mountain Sunset Shirt, Vintage Hiking Tee, ..." -> "Retro Mountain Sunset Shirt"
 *  (the product name the video shows; same rule as Siparişler). The full title is the tooltip. */
function shortTitle(title) {
  const text = String(title || "");
  const first = text.split(/\s*[,|]\s*|\s+[-–—]\s+/)[0];
  return first.length >= 8 ? first : text;
}

function currencyLabel(code) {
  return code === "TRY" ? "TL" : code || "";
}

function parseMonth(ym) {
  const m = /^(\d{4})-(\d{2})$/.exec(ym || "");
  return m ? { year: Number(m[1]), month: Number(m[2]) } : null;
}

/** This month on this computer ("2026-09"), the one the API serves by default. */
function currentYm() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function shiftMonth(ym, delta) {
  const p = parseMonth(ym);
  if (!p) return ym;
  const index = p.year * 12 + (p.month - 1) + delta;
  const y = Math.floor(index / 12);
  const mo = (index % 12) + 1;
  return `${y}-${String(mo).padStart(2, "0")}`;
}

/** "12,5" / "12.5" / "1.234,50" / "$12" -> 12.5 ...; "" -> null; anything else -> NaN. */
export function parseAmount(raw) {
  let s = String(raw ?? "")
    .trim()
    .replace(/\s|\$|€|£|₺|TL|USD|TRY/gi, "");
  if (s === "") return null;
  const lastComma = s.lastIndexOf(",");
  const lastDot = s.lastIndexOf(".");
  if (lastComma >= 0 && lastDot >= 0) {
    s = lastComma > lastDot ? s.replace(/\./g, "").replace(",", ".") : s.replace(/,/g, "");
  } else if (lastComma >= 0) {
    s = s.replace(",", ".");
  }
  if (!/^\d+(\.\d+)?$/.test(s)) return NaN;
  const v = Number(s);
  return Number.isFinite(v) && v >= 0 && v <= 1e6 ? v : NaN;
}

export default {
  async mount(el, ctx) {
    const t = ctx.t;
    const decimalSep = ctx.lang === "en" ? "." : ",";
    const askedMonth = ctx.query.month;
    const state = {
      month: parseMonth(askedMonth) ? askedMonth : null,
      // A ?month= that cannot be shown (an old bookmark): the latest month opens instead
      // with a note. Malformed ones are caught here, out-of-range ones by the API.
      monthNotice: askedMonth && !parseMonth(askedMonth) ? String(askedMonth).slice(0, 40) : null,
      sort: SORTS.includes(ctx.query.sort) ? ctx.query.sort : "sales",
      display: "both",
      showAll: false,
      data: null, // the last "ready" answer
      fx: null, // {base, quote, rate, source, date, stale, shop_is_quote}
      fxInfo: null, // GET /api/profit/fx answer
      fxLoading: false, // today's rate is being fetched (the first answer had none yet)
      jobId: null,
      refreshJob: null, // a job refreshing stale months while cached data is shown
      refreshError: null,
      mode: "loading", // loading | ready | error | setup
      loadSeq: 0,
      pollTimer: null,
      finished: new Map(), // job id -> summary, for events that beat the GET answer
      animateNext: false, // the next ready render opens with the count-up (a full load)
      countRun: 0, // the count-up running now (a newer render stops an older one)
      animTimer: null,
      pulseTimer: null,
    };
    // While an animated render builds its nodes: the numbers that count up.
    let counting = null;
    if (state.monthNotice) ctx.setQuery({ month: null });

    // ---------------------------------------------------------------- money helpers

    const shopCur = () => (state.data && state.data.currency) || "USD";
    const pairOk = () => !!(state.fx && state.fx.rate > 0);
    const secCur = () => (state.fx ? (state.fx.shop_is_quote ? state.fx.base : state.fx.quote) : null);
    const display = () => (pairOk() ? state.display : "primary");
    const conv = (v) => (state.fx.shop_is_quote ? v / state.fx.rate : v * state.fx.rate);
    // Lira amounts are shown whole ("249.469 ₺", as in the video); they get long otherwise.
    const digitsFor = (cur, d) => (cur === "TRY" ? 0 : d);

    /** The main amount in the chosen display currency. */
    function mainMoney(v, digits = 2) {
      if (v === null || v === undefined) return "–";
      if (display() === "secondary") return minus(money(conv(v), secCur(), { digits: digitsFor(secCur(), digits) }));
      return minus(money(v, shopCur(), { digits: digitsFor(shopCur(), digits) }));
    }
    /** The second line under a KPI ("249.469 ₺"), only in the "both" display. */
    function subMoney(v) {
      if (display() !== "both" || v === null || v === undefined) return null;
      return minus(money(conv(v), secCur(), { digits: digitsFor(secCur(), 2) }));
    }
    const shownCur = () => (display() === "secondary" ? secCur() : shopCur());
    const pct = (ratio, digits = 1) => (ratio === null || ratio === undefined || !Number.isFinite(ratio) ? "–" : minus(percent(ratio, digits)));
    // Month-over-month change: up green, down red; one that rounds to %0,0 is flat and
    // grey (a red "↓ %0,0" reads like a loss that is not there).
    const trend = (ratio) =>
      Math.abs(ratio) < 0.0005
        ? { tone: "neutral", icon: "minus" }
        : ratio > 0
          ? { tone: "success", icon: "arrow-up" }
          : { tone: "danger", icon: "arrow-down" };
    const monthLong = (ym) => {
      const p = parseMonth(ym);
      if (!p) return ym || "";
      const name = monthName(p.month - 1);
      return p.year === new Date().getFullYear() ? name : t("month_year", { month: name, year: p.year });
    };
    /** Month select labels: "Eylül", or "Ara 2025" for last year (keeps the select narrow). */
    const monthOption = (ym) => {
      const p = parseMonth(ym);
      if (!p) return ym || "";
      if (p.year === new Date().getFullYear()) return monthName(p.month - 1);
      return t("month_year", { month: monthName(p.month - 1, true).replace(/\.$/, ""), year: p.year });
    };
    const monthShort = (ym) => {
      const p = parseMonth(ym);
      return p ? monthName(p.month - 1, true).replace(/\.$/, "") : ym;
    };

    /** t() with some params rendered as nodes (the bold numbers in the KPI footers). */
    function rich(key, params) {
      const nodes = {};
      const plain = {};
      for (const [k, v] of Object.entries(params || {})) {
        if (v instanceof Node) {
          plain[k] = `\u0001${k}\u0001`;
          nodes[k] = v;
        } else plain[k] = v;
      }
      return t(key, plain)
        .split("\u0001")
        .map((part, i) => (i % 2 ? nodes[part] : part));
    }
    const b = (text) => h("b", { class: "num" }, text);

    /** fmt(value), as a text node that counts up from 0 when this render is animated. */
    function counted(value, fmt) {
      if (!counting || value === null || value === undefined || !Number.isFinite(value)) return fmt(value);
      const text = fmt(value);
      const node = document.createTextNode(fmt(0));
      counting.push({ node, value, fmt, text });
      return node;
    }

    /** easeOutCubic over COUNT_MS, and always the exact final text at the end. */
    function runCount(list) {
      const run = ++state.countRun;
      if (!list || !list.length) return;
      const start = performance.now();
      const step = (now) => {
        if (run !== state.countRun) return;
        const x = Math.min(1, (now - start) / COUNT_MS);
        const e = easeOutCubic(x);
        for (const c of list) c.node.nodeValue = x >= 1 ? c.text : c.fmt(c.value * e);
        if (x < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    }

    /** The CSS half of the opening: bars grow (.pf-anim), the loss row pulses (.pf-pulse). */
    function setAnimating(on) {
      clearTimeout(state.animTimer);
      clearTimeout(state.pulseTimer);
      content.classList.toggle("pf-anim", on);
      productsCard.classList.toggle("pf-pulse", on);
      if (!on) return;
      state.animTimer = setTimeout(() => content.classList.remove("pf-anim"), COUNT_MS + 700);
      state.pulseTimer = setTimeout(() => productsCard.classList.remove("pf-pulse"), PULSE_END_MS);
    }

    // ---------------------------------------------------------------- header

    const rateTip = h("span", { class: "pf-tip", role: "tooltip" });
    let rateText = ""; // "1 USD = 34,12 TL · TCMB · bugün"
    // Turns while Etsy data is being refreshed in the background (the popover says when
    // the data was fetched and refreshes it).
    const rateIcon = h("span", { class: "pf-rate-icon" }, icon("refresh", { size: 14 }));
    const rateBtn = h(
      "button",
      { type: "button", class: "pf-rate", "aria-haspopup": "dialog", onClick: () => openRate() },
      rateIcon,
      h("span", { class: "pf-rate-text" }, t("rate.auto")),
      rateTip,
    );

    const segButtons = DISPLAYS.map((id) =>
      h("button", { type: "button", class: "pf-seg-btn", dataset: { id }, onClick: () => setDisplay(id) }),
    );
    const seg = h("div", { class: "pf-seg", role: "group", "aria-label": t("seg.label") }, segButtons);

    const monthSel = select({
      options: [],
      prefix: icon("clock", { size: 14 }),
      ariaLabel: t("month.label"),
      class: "pf-month",
      onChange: (v) => {
        state.month = v;
        state.monthNotice = null;
        state.showAll = false;
        ctx.setQuery({ month: v });
        load();
      },
    });

    function renderHeader() {
      const d = state.data;
      const month = state.month;
      if (d && d.state === "ready") {
        ctx.setHeader({ subtitle: t("subtitle_month", { month: monthLong(d.summary.month), n: d.summary.orders }) });
      } else if (month) {
        ctx.setHeader({ subtitle: monthLong(month) });
      }
      // Segmented control: USD | TL | İkisi (the shop currency first). Until the first
      // answer says whether a rate exists, the chosen display stays chosen (the video's
      // "İkisi"); only a ready answer without a usable rate falls back to the shop's own.
      const labels = { primary: currencyLabel(shopCur()), secondary: currencyLabel(secCur() || (shopCur() === "TRY" ? "USD" : "TRY")), both: t("seg.both") };
      const rateUnknown = !state.fx && (state.mode === "loading" || state.fxLoading);
      const shown = rateUnknown ? state.display : display();
      for (const btn of segButtons) {
        const id = btn.dataset.id;
        const disabled = !rateUnknown && id !== "primary" && !pairOk();
        btn.textContent = labels[id];
        btn.disabled = disabled;
        btn.title = disabled ? t("seg.no_rate") : "";
        btn.setAttribute("aria-pressed", shown === id ? "true" : "false");
        btn.classList.toggle("is-active", shown === id);
      }
      // Rate tooltip: "1 USD = 34,12 TL · TCMB · bugün".
      const p = state.fx;
      if (p) {
        // TCMB's bulletin of day D is the rate in force until the next one, so a rate
        // fetched today is "today's" even when the bulletin is dated the day before.
        const when = p.source === "manual" ? null : !p.stale || !p.date ? t("rate.today") : fmtDate(`${p.date}T12:00:00`);
        rateText = [
          `1 ${currencyLabel(p.base)} = ${number(p.rate, 2)} ${currencyLabel(p.quote)}`,
          t(`rate.source.${p.source}`),
          when,
        ]
          .filter(Boolean)
          .join(" · ");
      } else {
        rateText = t("rate.none");
      }
      rateTip.textContent = state.refreshJob ? `${rateText} · ${t("refreshing")}` : rateText;
      rateBtn.setAttribute("aria-label", `${t("rate.auto")}. ${rateTip.textContent}`);
      // Month select: the last 12 months, newest first.
      const months = (d && d.months) || (month ? [month] : []);
      monthSel.setOptions(
        months.map((ym) => ({ value: ym, label: monthOption(ym) })),
        month || months[0],
      );
    }

    let headerActions = false;
    function showActions(on) {
      if (on === headerActions) return;
      headerActions = on;
      ctx.setHeader({ actions: on ? [rateBtn, seg, monthSel] : [] });
    }
    showActions(true);

    // ---------------------------------------------------------------- page skeleton

    const statusSlot = h("div", { class: "pf-status" });
    const kpiRow = h("div", { class: "pf-kpis" });
    const noteSlot = h("div", { class: "pf-notes" });

    // Products card. "Satışa göre ⌄" (the video's pill) opens a menu: the sorts, then
    // "Tüm ürünleri göster (24)" as a toggle; the button always names the sort in force.
    const sortBtn = button({
      label: t(`sort.${state.sort}`),
      iconRight: "chevron-down",
      variant: "secondary",
      size: "sm",
      title: t("common.sort"),
      class: "pf-sort",
      onClick: () => openSortMenu(),
    });
    sortBtn.setAttribute("aria-haspopup", "menu");

    function openSortMenu() {
      const d = state.data;
      const total = d && d.state === "ready" ? (d.summary.products || []).length : 0;
      const items = SORTS.map((id) => ({
        label: t(`sort.${id}`),
        checked: state.sort === id,
        onClick: () => setSort(id),
      }));
      if (total > TOP_N) {
        // An on/off switch, not one choice among the sorts.
        items.push({ divider: true }, {
          label: t("products.show_all", { n: total }),
          checked: state.showAll,
          checkbox: true,
          onClick: () => setShowAll(!state.showAll),
        });
      }
      menu(sortBtn, items, { placement: "bottom-end", width: 250 });
    }

    function setSort(id) {
      if (!SORTS.includes(id) || id === state.sort) return;
      state.sort = id;
      sortBtn.setLabel(t(`sort.${id}`));
      ctx.setQuery({ sort: id === "sales" ? null : id });
      productsCard.classList.remove("pf-pulse");
      renderProducts();
    }

    function setShowAll(on) {
      state.showAll = !!on;
      productsCard.classList.remove("pf-pulse");
      renderProducts();
    }
    const tbl = table({
      columns: [
        {
          key: "product",
          label: t("col.product"),
          render: (r) =>
            h(
              "div",
              { class: "pf-prod" },
              thumb({ src: r.image || null, size: 36, radius: 9, icon: "image" }),
              h(
                "div",
                { class: "pf-prod-text" },
                h("span", { class: "pf-prod-title ellipsis", title: r.title || null }, shortTitle(r.title) || "–"),
                r.net !== null && r.net < 0
                  ? h("span", { class: "pf-loss-chip" }, icon("alert", { size: 11.4, strokeWidth: 2.4 }), t("products.loss"))
                  : h("span", { class: "pf-prod-type" }, t(`type.${r.type}`)),
              ),
            ),
        },
        { key: "qty", label: t("col.sold"), align: "right", width: 60, class: "pf-c-qty", render: (r) => counted(r.qty, (v) => number(Math.round(v))) },
        { key: "revenue", label: t("col.revenue"), align: "right", width: 98, class: "pf-c-rev", render: (r) => counted(r.revenue, mainMoney) },
        { key: "cost", label: t("col.cost"), align: "right", width: 98, class: "pf-c-cost", render: (r) => costCell(r) },
        {
          key: "net",
          label: t("col.net"),
          align: "right",
          width: 90,
          render: (r) => h("span", { class: cx("pf-net", r.net !== null && (r.net < 0 ? "is-neg" : "is-pos")) }, counted(r.net, mainMoney)),
        },
        {
          // Wide enough for "−%100,0" in its pill (the cell may lend it its padding).
          key: "margin",
          label: t("col.margin"),
          align: "right",
          width: 104,
          class: "pf-c-margin",
          render: (r) =>
            r.margin === null || r.margin === undefined
              ? h("span", { class: "muted" }, "–")
              : h("span", { class: cx("pf-pill", r.margin < 0 ? "is-neg" : "is-pos") }, counted(r.margin, pct)),
        },
      ],
      rows: null,
      rowKey: "key",
      rowClass: (r) => (r.net !== null && r.net < 0 ? "pf-row-loss" : null),
      empty: emptyState({ icon: "receipt", title: t("products.empty"), message: t("products.empty_msg"), compact: true }),
    });
    const productsCard = card({
      title: t("products.title"),
      subtitle: " ",
      actions: [sortBtn],
      body: tbl.el,
      pad: false,
      class: "pf-products",
    });
    const productsSub = productsCard.querySelector(".card-sub");

    // Chart card
    const chartDelta = h("div", { class: "pf-chart-delta" });
    const chartHost = h("div", { class: "pf-chart-host" });
    const chartNote = h("p", { class: "pf-chart-note" });
    const feesHost = h("div", { class: "pf-fees" });
    const chartCard = card({
      title: t("chart.title"),
      subtitle: " ",
      actions: chartDelta,
      body: h("div", { class: "pf-chart-body" }, h("div", { class: "pf-chart-col" }, chartHost, chartNote), feesHost),
      class: "pf-chart",
    });
    const chartTitle = chartCard.querySelector(".card-title");
    const chartSub = chartCard.querySelector(".card-sub");
    let chart = null;

    /** The video's chart lines behind the bars: a baseline under them and two faint
     *  guides at half and full height (the tallest bar). barChart() draws its SVG again
     *  on every update and resize; the guides follow each drawing. */
    function addGuides() {
      const svgEl = chart && chart.querySelector("svg");
      if (!svgEl || svgEl.querySelector(".pf-guide")) return;
      const boxes = [...svgEl.querySelectorAll("path.bar:not(.is-neg)")].map((p) => p.getBBox());
      if (!boxes.length) return;
      const base = Math.max(...boxes.map((r) => r.y + r.height));
      const tall = base - Math.min(...boxes.map((r) => r.y));
      if (!(tall > 4)) return;
      const width = Number(svgEl.getAttribute("width")) || svgEl.clientWidth;
      const line = (y, cls) => svg("line", { x1: 0, x2: width, y1: y, y2: y, class: cx("pf-guide", cls) });
      const lines = [line(base - tall, "is-grid"), line(base - tall / 2, "is-grid")];
      // With a loss month barChart() draws its own zero line.
      if (!svgEl.querySelector(".bar-zero")) lines.push(line(base, "is-base"));
      const first = svgEl.querySelector("path.bar");
      for (const node of lines) svgEl.insertBefore(node, first);
    }
    const guides = typeof MutationObserver !== "undefined" ? new MutationObserver(addGuides) : null;

    const grid = h("div", { class: "pf-grid" }, productsCard, chartCard);
    const content = h("div", { class: "pf-content" }, statusSlot, kpiRow, noteSlot, grid);
    const other = h("div", { class: "pf-other" });
    el.append(content, other);

    // ---------------------------------------------------------------- KPI row

    function bar(ratio, tone) {
      const w = Number.isFinite(ratio) ? Math.max(0, Math.min(1, ratio)) * 100 : 0;
      return h("div", { class: "pf-bar" }, h("span", { class: cx("pf-bar-fill", `pf-t-${tone}`), style: { width: `${w}%` } }));
    }

    function stackBar(s) {
      const parts = [
        ["fees", s.fees.total],
        ["cost", s.product_cost || 0],
        ["ship", s.shipping_cost || 0],
        ["net", Math.max(0, s.net || 0)],
      ].map(([k, v]) => [k, Math.max(0, v || 0)]);
      const sum = parts.reduce((a, [, v]) => a + v, 0);
      // Shares of revenue; what is not accounted for yet (costs not entered) stays empty.
      const total = Math.max(sum, s.revenue || 0);
      if (!total) return h("div", { class: "pf-bar" });
      return h(
        "div",
        { class: "pf-bar pf-stack" },
        parts.filter(([, v]) => v > 0).map(([k, v]) => h("span", { class: cx("pf-bar-fill", `pf-t-${k}`), style: { flexGrow: v / total, flexBasis: 0 } })),
        total > sum ? h("span", { class: "pf-bar-fill pf-t-rest", style: { flexGrow: (total - sum) / total, flexBasis: 0 } }) : null,
      );
    }

    /** − / = between the cards: even SVG strokes, so the sign sits in the middle of its
     *  circle whatever the font's baseline (the video's Op). */
    function opSign(op) {
      return svg(
        "svg",
        { viewBox: "0 0 14 14", fill: "none", stroke: "currentColor", "stroke-width": 2, "stroke-linecap": "round", "aria-hidden": "true", focusable: "false" },
        svg("path", { d: op === "=" ? "M3 4.8h8M3 9.2h8" : "M3 7h8" }),
      );
    }

    function withOp(node, op) {
      return h(
        "div",
        { class: "pf-kpi" },
        op ? h("span", { class: cx("pf-op", op === "=" && "is-eq"), role: "img", "aria-label": t(op === "=" ? "op.equals" : "op.minus") }, opSign(op)) : null,
        node,
      );
    }

    const nbsp = " ";

    function renderKpis(s) {
      if (!s) {
        const sk = (w) => skeleton({ lines: 1, height: 24, widths: [w] });
        mount(
          kpiRow,
          [
            ["revenue", "info", "coins"],
            ["fees", "warning", "tag"],
            ["cost", "neutral", "box"],
            ["shipping", "neutral", "truck"],
            ["net", "success", "chart"],
          ].map(([k, tone, ic], i) =>
            withOp(
              statCard({
                icon: ic,
                label: t(`kpi.${k}`),
                tone,
                highlight: k === "net",
                value: sk(k === "net" ? "78%" : "70%"),
                sub: skeleton({ lines: 1, height: 11, widths: ["42%"] }),
                footer: [h("div", { class: "pf-bar" }), skeleton({ lines: 1, height: 10, widths: ["60%"] })],
              }),
              i === 0 ? null : i === 4 ? "=" : "−",
            ),
          ),
        );
        kpiRow.classList.add("is-loading");
        return;
      }
      kpiRow.classList.remove("is-loading");
      const rev = s.revenue || 0;
      const share = (v) => (rev > 0 && v !== null && v !== undefined ? v / rev : null);
      const estimated = s.fees.source === "estimate";
      const productReady = s.product_cost !== null;
      const prev = state.data.previous;

      // Every amount of the row counts up on an animated render (counted()).
      const subCounted = (v) => (subMoney(v) === null ? nbsp : counted(v, subMoney));
      const shareOf = (v) => b(counted(share(v), pct));

      const revenueCard = statCard({
        icon: "coins",
        label: t("kpi.revenue"),
        tone: "info",
        value: counted(s.revenue, mainMoney),
        sub: subCounted(s.revenue),
        footer: [stackBar(s), h("span", null, rich("kpi.orders_avg", { n: s.orders, avg: b(counted(s.avg_order, mainMoney)) }))],
      });
      revenueCard.title = t("kpi.revenue_tip", {
        items: mainMoney(s.items_revenue + (s.gift_wrap || 0)),
        shipping: mainMoney(s.shipping_charged),
        refunds: mainMoney(s.refunds),
        tax: mainMoney(s.tax),
      });

      const feesCard = statCard({
        icon: "tag",
        label: t("kpi.fees"),
        tone: "warning",
        value: estimated ? counted(s.fees.total, (v) => `≈ ${mainMoney(v)}`) : counted(s.fees.total, mainMoney),
        sub: subCounted(s.fees.total),
        footer: [
          bar(share(s.fees.total), "fees"),
          h(
            "span",
            null,
            rich("kpi.share", { pct: shareOf(s.fees.total) }),
            estimated ? [" · ", h("span", { class: "pf-warn" }, t("kpi.estimated"))] : null,
          ),
        ],
      });
      if (estimated) feesCard.title = feeReason(s.fees);

      const costFoot = !productReady
        ? h("span", { class: "pf-warn" }, t("kpi.no_costs"))
        : s.missing_cost_qty > 0
          ? h("span", { class: "pf-warn" }, t("kpi.missing_costs", { n: s.missing_cost_qty }))
          : h("span", null, rich("kpi.share", { pct: shareOf(s.product_cost) }));
      const costCard = statCard({
        icon: "box",
        label: t("kpi.cost"),
        tone: "neutral",
        value: productReady ? counted(s.product_cost, mainMoney) : "–",
        sub: productReady ? subCounted(s.product_cost) : nbsp,
        footer: [bar(share(s.product_cost), "cost"), costFoot],
      });
      editsCosts(costCard, t("kpi.cost"), productReady ? mainMoney(s.product_cost) : t("kpi.no_costs"));

      const shipReady = s.shipping_cost !== null;
      const shipFoot = !shipReady
        ? h("span", { class: "pf-warn" }, t("kpi.no_costs"))
        : s.shipping_source === "labels"
          ? h("span", null, t("kpi.labels"))
          : s.missing_shipping_orders > 0
            ? h("span", { class: "pf-warn" }, t("kpi.missing_shipping", { n: s.missing_shipping_orders }))
            : h("span", null, rich("kpi.share", { pct: shareOf(s.shipping_cost) }));
      const shipCard = statCard({
        icon: "truck",
        label: t("kpi.shipping"),
        tone: "neutral",
        value: shipReady ? counted(s.shipping_cost, mainMoney) : "–",
        sub: shipReady ? subCounted(s.shipping_cost) : nbsp,
        footer: [bar(share(s.shipping_cost), "ship"), shipFoot],
      });
      editsCosts(shipCard, t("kpi.shipping"), shipReady ? mainMoney(s.shipping_cost) : t("kpi.no_costs"));

      let delta = null;
      if (s.net !== null && prev && prev.net !== null && prev.net !== 0) {
        const ratio = (s.net - prev.net) / Math.abs(prev.net);
        delta = { text: counted(Math.abs(ratio), pct), ...trend(ratio) };
      }
      const netCard = statCard({
        icon: "chart",
        label: t("kpi.net"),
        tone: "success",
        highlight: true,
        delta,
        value: s.net === null ? "–" : counted(s.net, mainMoney),
        sub: s.net === null ? nbsp : subCounted(s.net),
        footer: [
          bar(share(s.net), "net"),
          s.net === null ? h("span", { class: "pf-warn" }, t("kpi.net_pending")) : h("span", null, rich("kpi.margin", { pct: b(counted(s.margin, pct)) })),
        ],
      });
      netCard.classList.add("pf-net-card");
      if (s.net !== null && s.net < 0) netCard.classList.add("is-loss");
      if (delta && prev) {
        const d = netCard.querySelector(".stat-delta");
        if (d) d.title = t("kpi.delta_title", { month: monthLong(prev.month) });
      }

      mount(kpiRow, [
        withOp(revenueCard, null),
        withOp(feesCard, "−"),
        withOp(costCard, "−"),
        withOp(shipCard, "−"),
        withOp(netCard, "="),
      ]);
    }

    /** Ürün maliyeti and Kargo open the costs (the video's products card has no costs
     *  button, only the sort control): a card that is a button, with a pen on hover. */
    function editsCosts(cardEl, label, valueText) {
      cardEl.classList.add("pf-kpi-edit");
      cardEl.setAttribute("role", "button");
      cardEl.tabIndex = 0;
      cardEl.title = t("products.costs_edit");
      cardEl.setAttribute("aria-label", `${label}: ${valueText}. ${t("products.costs_edit")}`);
      const head = cardEl.querySelector(".stat-head");
      if (head) head.appendChild(h("span", { class: "pf-kpi-pen", "aria-hidden": "true" }, icon("edit", { size: 13 })));
      cardEl.addEventListener("click", () => openCosts());
      cardEl.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        e.preventDefault();
        openCosts();
      });
    }

    function feeReason(fees) {
      if (!fees || fees.source !== "estimate") return "";
      const key = `fees.reason.${fees.reason || "unavailable"}`;
      return t.has(key) ? t(key, { status: fees.status || "?" }) : t("fees.reason.unavailable", { status: fees.status || "?" });
    }

    // ---------------------------------------------------------------- notes / CTA

    function renderNotes(s) {
      const nodes = [];
      if (state.monthNotice) {
        nodes.push(
          infoNote({
            tone: "info",
            icon: "calendar",
            text: t("note.month_fallback", { asked: state.monthNotice, month: monthLong(state.month || currentYm()) }),
            action: button({
              label: t("common.ok"),
              size: "sm",
              variant: "ghost",
              onClick: () => {
                state.monthNotice = null;
                renderNotes(state.mode === "ready" && state.data ? state.data.summary : null);
              },
            }),
          }),
        );
      }
      if (s) {
        const productReady = s.product_cost !== null;
        const shipReady = s.shipping_cost !== null;
        if (!productReady || !shipReady) {
          const onlyShipping = productReady && !shipReady;
          nodes.push(
            h(
              "div",
              { class: "pf-cta" },
              h("span", { class: "pf-cta-icon" }, icon(onlyShipping ? "truck" : "box", { size: 20 })),
              h(
                "div",
                { class: "pf-cta-text" },
                h("p", { class: "pf-cta-title" }, t(onlyShipping ? "cta.shipping_title" : "cta.title")),
                h("p", { class: "pf-cta-msg" }, t(onlyShipping ? "cta.shipping_message" : "cta.message")),
              ),
              button({ label: t("cta.action"), icon: "edit", variant: "primary", onClick: () => openCosts() }),
            ),
          );
        } else {
          if (s.missing_cost_qty > 0) {
            nodes.push(infoNote({ tone: "warning", icon: "alert", text: t("note.missing", { n: s.missing_cost_qty }), action: button({ label: t("note.edit"), size: "sm", variant: "ghost", onClick: () => openCosts() }) }));
          }
          if (s.shipping_source === "costs" && s.missing_shipping_orders > 0) {
            nodes.push(infoNote({ tone: "warning", icon: "truck", text: t("note.missing_shipping", { n: s.missing_shipping_orders }), action: button({ label: t("note.edit"), size: "sm", variant: "ghost", onClick: () => openCosts() }) }));
          }
        }
      }
      if (s && state.refreshError && !state.refreshJob) {
        // Cached figures stay on screen; the refresh that failed says so here.
        nodes.push(
          infoNote({
            tone: "warning",
            icon: "alert",
            text: t("refresh_failed", { error: ctx.api.errorText(state.refreshError, t) }),
            action: button({ label: t("common.retry"), icon: "refresh", size: "sm", variant: "ghost", onClick: () => refreshData() }),
          }),
        );
      }
      if (s && s.partial) {
        // More paid orders than the app reads for one month: say so quietly.
        const limit = number(s.partial_limit || s.orders);
        nodes.push(h("p", { class: "pf-partial" }, icon("info", { size: 13 }), h("span", null, t("note.partial", { n: limit }))));
      }
      mount(noteSlot, nodes);
      noteSlot.hidden = !nodes.length;
    }

    // ---------------------------------------------------------------- products

    function sortedProducts(s) {
      const all = [...(s.products || [])];
      const num = (v) => (v === null || v === undefined ? -Infinity : v);
      const key = {
        sales: (p) => p.qty,
        profit: (p) => num(p.net),
        margin: (p) => num(p.margin),
      }[state.sort];
      all.sort((a, b2) => key(b2) - key(a) || b2.qty - a.qty || b2.revenue - a.revenue || String(a.title).localeCompare(String(b2.title)));
      return all;
    }

    function renderProducts() {
      const d = state.data;
      if (!d || d.state !== "ready") {
        tbl.update(null);
        productsSub.textContent = " ";
        return;
      }
      const all = sortedProducts(d.summary);
      let rows = all;
      if (!state.showAll) {
        const top = all.slice(0, TOP_N);
        const losers = all.slice(TOP_N).filter((p) => p.net !== null && p.net < 0);
        rows = top.concat(losers);
      }
      tbl.update(rows);
      // Just the video's line ("En çok satan 6 ürün · net = gelir − ürün maliyeti"); the
      // whole list is the sort menu's "Tüm ürünleri göster".
      productsSub.textContent =
        state.showAll && all.length > TOP_N
          ? t("products.sub.all", { n: all.length })
          : t(`products.sub.${state.sort}`, { n: Math.min(TOP_N, all.length) });
    }

    function costCell(r) {
      const text = r.cost === null ? t("products.cost_enter") : mainMoney(r.cost);
      if (!r.listing_id) return h("span", { class: "pf-cost-static" }, r.cost === null ? "–" : counted(r.cost, mainMoney));
      const hint =
        r.cost_source === "listing"
          ? t("products.cost_from_listing")
          : r.cost_source === "type"
            ? t("products.cost_from_type", { type: t(`type.${r.type}`) })
            : t("products.cost_edit");
      const btn = h(
        "button",
        {
          type: "button",
          class: cx("pf-cost-btn", r.cost === null && "is-empty", r.cost_source === "listing" && "is-own"),
          title: `${t("products.cost_edit")} · ${hint}`,
          onClick: (e) => {
            e.stopPropagation();
            startCostEdit(r, btn);
          },
        },
        r.cost === null ? icon("plus", { size: 12, strokeWidth: 2.2 }) : null,
        r.cost === null ? text : counted(r.cost, mainMoney),
      );
      return btn;
    }

    function costCurrency() {
      const c = state.data && state.data.costs;
      return (c && c.currency) || shopCur();
    }

    function inputValue(v) {
      if (v === null || v === undefined) return "";
      return String(Math.round(v * 10000) / 10000).replace(".", decimalSep);
    }

    function startCostEdit(r, btn) {
      const input = h("input", {
        type: "text",
        class: "input pf-cost-input num",
        value: r.cost_source === "listing" ? inputValue(r.unit_input) : "",
        placeholder: r.cost_source === "type" ? inputValue(r.unit_input) : "0" + decimalSep + "00",
        inputmode: "decimal",
        autocomplete: "off",
        "aria-label": `${t("products.cost_edit")}: ${r.title}`,
      });
      const wrap = h(
        "span",
        { class: "pf-cost-edit" },
        input,
        h("span", { class: "pf-cost-unit" }, `${currencyLabel(costCurrency())} / ${t("products.cost_unit")}`),
      );
      btn.replaceWith(wrap);
      input.focus();
      input.select();
      let done = false;
      const cancel = () => {
        if (done) return;
        done = true;
        if (wrap.isConnected) wrap.replaceWith(btn);
      };
      const commit = async () => {
        if (done) return;
        const value = parseAmount(input.value);
        if (Number.isNaN(value)) {
          input.setAttribute("aria-invalid", "true");
          ctx.toast({ tone: "warning", title: t("products.cost_invalid"), timeout: 2500 });
          input.focus();
          return;
        }
        const before = r.cost_source === "listing" ? r.unit_input : null;
        if (value === before || (value === null && before === null)) {
          cancel();
          return;
        }
        done = true;
        input.disabled = true;
        wrap.appendChild(spinner({ size: 12 }));
        try {
          await ctx.api.post("/api/profit/costs", { set: { [`listing:${r.listing_id}`]: value } });
          ctx.toast({ tone: "success", title: t("products.cost_saved"), timeout: 2000 });
          await load({ quiet: true });
        } catch (err) {
          if (ctx.api.isAbort(err)) return;
          ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
          if (wrap.isConnected) wrap.replaceWith(btn);
        }
      };
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          commit();
        } else if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          cancel();
        }
      });
      input.addEventListener("input", () => input.removeAttribute("aria-invalid"));
      input.addEventListener("blur", () => {
        // A click on the toast or elsewhere commits, like a spreadsheet cell.
        setTimeout(() => {
          if (!done && document.activeElement !== input) commit();
        }, 0);
      });
    }

    // ---------------------------------------------------------------- chart + fee breakdown

    function renderChart() {
      const d = state.data;
      if (!d || d.state !== "ready") {
        chartTitle.textContent = t("chart.title");
        chartSub.textContent = " ";
        mount(chartDelta);
        mount(chartHost, h("div", { class: "pf-chart-skel" }, [0.45, 0.55, 0.62, 0.7, 0.82, 1].map((f) => h("span", { class: "skeleton", style: { height: `${Math.round(f * 150)}px` } }))));
        chartNote.hidden = true;
        mount(feesHost, skeleton({ lines: 5, height: 12, widths: ["40%", "90%", "80%", "70%", "60%"], gap: 16 }));
        chart = null;
        return;
      }
      const series = d.series || [];
      const hasNet = series.some((p) => p.net !== null && p.net !== undefined);
      const valueOf = (p) => (hasNet ? p.net : p.revenue);
      const toShown = (v) => (v === null || v === undefined ? 0 : display() === "secondary" ? conv(v) : v);
      const data = series.map((p) => ({ label: monthShort(p.month), value: toShown(valueOf(p)) }));
      const fmt0 = (v) => minus(money(v, shownCur(), { digits: 0 }));
      chartTitle.textContent = hasNet ? t("chart.title") : t("chart.title_revenue");
      chartSub.textContent = t("chart.sub", { cur: currencyLabel(shownCur()) });
      if (chart) chart.update(data);
      else {
        chart = barChart({ data, format: fmt0, highlightLast: true, height: 212, ariaLabel: chartTitle.textContent });
        if (guides) guides.observe(chart, { childList: true });
        mount(chartHost, chart);
      }
      chart.setAttribute("aria-label", data.map((p) => `${p.label}: ${fmt0(p.value)}`).join(", "));
      chartNote.textContent = t("chart.revenue_note");
      chartNote.hidden = hasNet;

      // "↑ %23,6 Ağustos'a göre"
      const cur = series[series.length - 1];
      const prev = series[series.length - 2];
      mount(chartDelta);
      if (cur && prev && valueOf(cur) !== null && valueOf(prev) !== null && valueOf(prev) !== 0) {
        const ratio = (valueOf(cur) - valueOf(prev)) / Math.abs(valueOf(prev));
        const p = parseMonth(prev.month);
        chartDelta.appendChild(
          badge({
            ...trend(ratio),
            text: t("chart.delta", { pct: pct(Math.abs(ratio)), month: t(`month_dat.${p.month}`) }),
          }),
        );
      }
      renderFees(d.summary);
    }

    function renderFees(s) {
      const f = s.fees;
      const estimated = f.source === "estimate";
      const rows = BUCKETS.filter((k) => k !== "other" || Math.abs(f.buckets.other || 0) >= 0.005);
      const max = Math.max(0.0001, ...rows.map((k) => Math.max(0, f.buckets[k] || 0)));
      mount(
        feesHost,
        h(
          "div",
          { class: "pf-fees-head" },
          h("h3", { class: "pf-fees-title" }, t("fees.title"), estimated ? badge({ text: t("fees.estimated"), tone: "warning", size: "sm" }) : null),
          h("span", { class: "pf-fees-total" }, t("fees.total"), " ", h("b", { class: "num" }, counted(f.total, mainMoney))),
        ),
        h(
          "div",
          { class: "pf-fee-rows" },
          rows.map((k) => {
            const v = f.buckets[k] || 0;
            const unknown = estimated && k === "ads";
            return h(
              "div",
              { class: "pf-fee-row", title: k === "other" ? t("fees.other_tip") : unknown ? t("fees.ads_unknown") : undefined },
              h("span", { class: "pf-fee-label" }, t(`fees.${k}`)),
              h("span", { class: "pf-fee-track" }, h("span", { class: "pf-fee-fill", style: { width: `${Math.max(0, Math.min(1, v / max)) * 100}%` } })),
              h("span", { class: "pf-fee-val num" }, unknown ? "–" : counted(v, mainMoney)),
            );
          }),
        ),
        estimated
          ? h(
              "p",
              { class: "pf-fees-note" },
              icon("info", { size: 13 }),
              h(
                "span",
                null,
                feeReason(f),
                " ",
                t("fees.basis"),
                // No permission to read the ledger: the connect page asks Etsy for it again.
                f.reason === "scope" ? [" ", h("a", { class: "pf-fees-link", href: "/kurulum/magaza?scope=transactions_r" }, t("fees.grant"))] : null,
              ),
            )
          : null,
      );
    }

    // ---------------------------------------------------------------- status / refresh

    function progressText(job) {
      if (!job) return t("progress.start");
      if (job.status === "queued") return t("progress.queued");
      const label = (job.progress && job.progress.label) || "";
      const [ym, step, n] = label.split("|");
      if (step === "receipts" || step === "ledger") return t(`progress.${step}`, { month: monthLong(ym), n: Number(n) || 0 });
      if (step === "images") return t("progress.images");
      if (step === "done") return t("progress.done");
      return t("progress.start");
    }

    function renderLoading(job) {
      state.mode = "loading";
      showActions(true);
      content.hidden = false;
      other.hidden = true;
      const prog = job && job.progress && job.progress.total ? progressBar({ value: job.progress.done, max: job.progress.total, size: "sm" }) : progressBar({ value: null, size: "sm" });
      mount(
        statusSlot,
        h(
          "div",
          { class: "pf-loading", title: t("loading.message") },
          spinner({ size: 16, tone: "accent" }),
          h("div", { class: "pf-loading-text" }, h("p", { class: "pf-loading-title" }, t("loading.title")), h("p", { class: "pf-loading-msg" }, progressText(job))),
          h("div", { class: "pf-loading-bar" }, prog.el),
        ),
      );
      statusSlot.hidden = false;
      renderKpis(null);
      renderNotes(null);
      renderProducts();
      renderChart();
      renderRefresh();
      renderHeader();
    }

    /** The page has no footer line (t300 ends with its cards): a refresh of Etsy data
     *  running in the background turns the rate button's icon; the rate popover says
     *  when the data was fetched and refreshes it; a failed refresh is a note. */
    function renderRefresh() {
      mount(rateIcon, state.refreshJob ? spinner({ size: 13 }) : icon("refresh", { size: 14 }));
      rateBtn.classList.toggle("is-busy", !!state.refreshJob);
    }

    function refreshData() {
      state.refreshError = null;
      load({ quiet: true, refresh: true });
    }

    function renderReady() {
      state.mode = "ready";
      showActions(true);
      content.hidden = false;
      other.hidden = true;
      mount(statusSlot);
      statusSlot.hidden = true;
      const s = state.data.summary;
      // The count-up opens a full load or a new month, not a quiet reload, a currency
      // switch or a new sort (and never with reduced motion).
      const animate = state.animateNext && !reducedMotion();
      state.animateNext = false;
      counting = animate ? [] : null;
      renderHeader();
      renderKpis(s);
      renderNotes(s);
      renderProducts();
      renderChart();
      const list = counting;
      counting = null;
      setAnimating(animate);
      runCount(list);
      renderRefresh();
    }

    function renderSetup(step) {
      state.mode = "setup";
      showActions(false);
      content.hidden = true;
      other.hidden = false;
      ctx.setHeader({ subtitle: t("subtitle") });
      mount(
        other,
        card({
          class: "pf-setup",
          body: emptyState({
            icon: "store",
            title: step === "keys" ? t("setup.keys_title") : t("setup.title"),
            message: t("setup.message"),
            action: button({ label: t("setup.action"), icon: "arrow-right", variant: "primary", onClick: () => ctx.navigate("/kurulum/magaza") }),
          }),
        }),
      );
    }

    function renderError(err) {
      if (err && err.code === "setup_needed") {
        renderSetup(err.params && err.params.step);
        return;
      }
      state.mode = "error";
      content.hidden = true;
      other.hidden = false;
      const message = err && err.code === "cancelled" ? t("error.cancelled") : ctx.api.errorText(err, t);
      mount(
        other,
        card({
          class: "pf-error",
          body: emptyState({
            icon: "alert",
            title: t("error.title"),
            message,
            action: button({ label: t("common.retry"), icon: "refresh", onClick: () => load({ refresh: true }) }),
          }),
        }),
      );
    }

    // ---------------------------------------------------------------- loading

    function clearPoll() {
      if (state.pollTimer) clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }

    function jobError(job) {
      if (job.status === "cancelled") return { code: "cancelled", message: "cancelled", params: {} };
      return job.error || { code: "internal", message: "", params: {} };
    }

    /** A profit job moved on: show progress, or load the result. */
    function onJob(job) {
      if (!job || job.kind !== "profit") return;
      if (job.status !== "queued" && job.status !== "running") state.finished.set(job.id, job);
      if (job.id !== state.jobId) return;
      if (job.status === "queued" || job.status === "running") {
        if (state.mode === "loading") renderLoading(job);
        return;
      }
      state.jobId = null;
      clearPoll();
      const hadData = state.mode === "ready";
      if (job.status === "done") {
        state.refreshJob = null;
        load({ quiet: hadData });
      } else if (hadData) {
        state.refreshJob = null;
        state.refreshError = jobError(job);
        renderRefresh();
        renderHeader();
        renderNotes(state.data && state.data.summary);
      } else {
        renderError(jobError(job));
      }
    }

    function watchJob(job) {
      state.jobId = job ? job.id : null;
      clearPoll();
      if (!job) return;
      const early = state.finished.get(job.id);
      if (early) {
        onJob(early);
        return;
      }
      if (job.status !== "queued" && job.status !== "running") {
        onJob(job);
        return;
      }
      // Events are the fast path; this poll only covers a dropped event stream.
      const poll = async () => {
        state.pollTimer = null;
        if (!ctx.isActive() || state.jobId !== job.id) return;
        try {
          const fresh = await ctx.api.get(`/api/jobs/${job.id}`, null, { signal: ctx.signal });
          onJob(fresh);
        } catch (err) {
          if (ctx.api.isAbort(err)) return;
        }
        if (state.jobId === job.id && ctx.isActive()) state.pollTimer = setTimeout(poll, JOB_POLL_MS);
      };
      state.pollTimer = setTimeout(poll, JOB_POLL_MS);
    }

    async function load({ quiet = false, refresh = false } = {}) {
      const seq = ++state.loadSeq;
      if (!quiet) {
        state.animateNext = true;
        renderLoading(null);
      }
      try {
        const res = await ctx.api.get("/api/profit", { month: state.month || undefined, refresh: refresh ? 1 : undefined }, { signal: ctx.signal });
        if (seq !== state.loadSeq || !ctx.isActive()) return;
        state.month = res.month;
        if (res.month_refused) {
          state.monthNotice = res.month_refused;
          ctx.setQuery({ month: null });
        }
        state.display = DISPLAYS.includes(res.display) ? res.display : "both";
        if (res.fx) state.fx = res.fx;
        // No rate yet but today's is on its way: the switch keeps the chosen display.
        const fxComing = !!res.fx_due && !state.fxInfo && !state.fx;
        if (fxComing) state.fxLoading = true;
        if (res.state === "loading") {
          if (state.data && state.data.month !== res.month) state.data = null;
          renderLoading(res.job);
          if (!res.job) renderError({ code: "internal", message: "no job", params: {} });
          else watchJob(res.job);
        } else {
          state.data = res;
          state.refreshJob = res.refreshing || null;
          state.refreshError = res.refresh_error || null;
          renderReady();
          if (res.refreshing) watchJob(res.refreshing);
        }
        if (res.fx_due && !state.fxInfo) loadFx();
      } catch (err) {
        if (ctx.api.isAbort(err) || seq !== state.loadSeq) return;
        if (quiet && state.data && err.code !== "setup_needed") {
          ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
          return;
        }
        renderError(err);
      }
    }

    async function loadFx(body) {
      try {
        const res = body
          ? await ctx.api.post("/api/profit/fx", body, { signal: ctx.signal })
          : await ctx.api.get("/api/profit/fx", null, { signal: ctx.signal });
        state.fxInfo = res;
        if (res.pair) state.fx = res.pair;
        state.fxLoading = false;
        rerender();
        return res;
      } catch (err) {
        if (ctx.api.isAbort(err)) return null;
        if (!body && state.fxLoading) {
          // No rate today: the switch falls back to the shop's currency now.
          state.fxLoading = false;
          rerender();
        }
        if (body) throw err;
        return null;
      }
    }

    /** Re-render with the data in hand (display or rate changed). */
    function rerender() {
      if (state.mode === "ready" && state.data) renderReady();
      else renderHeader();
    }

    async function setDisplay(id) {
      if (!DISPLAYS.includes(id) || state.display === id) return;
      state.display = id;
      rerender();
      try {
        await ctx.api.post("/api/profit/prefs", { display: id }, { signal: ctx.signal });
      } catch (err) {
        if (!ctx.api.isAbort(err)) ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
      }
    }

    // ---------------------------------------------------------------- rate popover

    /** The popover's last line: when the Etsy figures were fetched, and "Yenile". */
    function dataRow(close) {
      const d = state.mode === "ready" ? state.data : null;
      if (state.refreshJob) {
        return h("div", { class: "pf-ratepop-data" }, spinner({ size: 12 }), h("span", null, t("refreshing")));
      }
      if (!d || !d.fetched_at) return null;
      return h(
        "div",
        { class: "pf-ratepop-data" },
        icon("clock", { size: 13 }),
        h("span", null, t("updated", { when: relative(d.fetched_at) })),
        button({
          label: t("common.refresh"),
          icon: "refresh",
          size: "sm",
          variant: "ghost",
          onClick: () => {
            close();
            refreshData();
          },
        }),
      );
    }

    function openRate() {
      const info = state.fxInfo;
      const p = state.fx;
      const manualInput = textInput({
        placeholder: t("rate.manual_placeholder", { example: `48${decimalSep}90` }),
        inputmode: "decimal",
        size: "sm",
        align: "right",
        suffix: p ? currencyLabel(p.quote) : "TL",
        ariaLabel: t("rate.manual_label"),
      });
      const saveBtn = button({
        label: t("rate.manual_save"),
        size: "sm",
        variant: "secondary",
        autoLoading: true,
        onClick: async () => {
          const v = parseAmount(manualInput.value);
          if (v === null || Number.isNaN(v) || v <= 0) {
            manualInput.input.setAttribute("aria-invalid", "true");
            ctx.toast({ tone: "warning", title: t("rate.invalid"), timeout: 2500 });
            return;
          }
          try {
            await loadFx({ rate: v });
            ctx.toast({ tone: "success", title: t("rate.saved"), timeout: 2000 });
            if (pop) pop.close();
          } catch (err) {
            ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
          }
        },
      });
      const refreshBtn = button({
        label: t("rate.refresh"),
        icon: "refresh",
        size: "sm",
        variant: "ghost",
        autoLoading: true,
        onClick: async () => {
          try {
            const res = await loadFx({ refresh: true });
            if (res && !res.error) ctx.toast({ tone: "success", title: t("rate.updated"), timeout: 2000 });
            if (pop) pop.close();
            if (res && res.error) openRate();
          } catch (err) {
            ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
          }
        },
      });
      const clearBtn = button({
        label: t("rate.manual_clear"),
        size: "sm",
        variant: "ghost",
        autoLoading: true,
        onClick: async () => {
          try {
            await loadFx({ rate: null });
            if (pop) pop.close();
          } catch (err) {
            ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
          }
        },
      });
      const body = h(
        "div",
        { class: "pf-ratepop" },
        h("p", { class: "pf-ratepop-title" }, t("rate.title")),
        h("p", { class: "pf-ratepop-desc" }, t("rate.desc")),
        p
          ? h(
              "div",
              { class: "pf-ratepop-rate" },
              h("span", { class: "muted" }, t("rate.current")),
              h("b", { class: "num" }, rateText),
            )
          : null,
        info && info.error ? infoNote({ tone: "warning", icon: "alert", text: p ? t("rate.offline") : t("rate.offline_none") }) : null,
        p && p.source === "manual" ? infoNote({ tone: "neutral", icon: "edit", text: t("rate.manual_active"), action: clearBtn }) : null,
        h("label", { class: "pf-ratepop-label" }, t("rate.manual_label")),
        h("div", { class: "pf-ratepop-row" }, manualInput, saveBtn),
        h("div", { class: "pf-ratepop-foot" }, refreshBtn),
        dataRow(() => pop && pop.close()),
      );
      const pop = popover(rateBtn, body, { placement: "bottom-end", width: 320, role: "dialog" });
      if (pop) requestAnimationFrame(() => manualInput.input.focus());
    }

    // ---------------------------------------------------------------- costs modal

    async function openCosts() {
      let info;
      try {
        info = await ctx.api.get("/api/profit/costs", { month: state.month || undefined }, { signal: ctx.signal });
      } catch (err) {
        if (!ctx.api.isAbort(err)) ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
        return;
      }
      const shop = info.shop_currency || shopCur();
      let cur = info.currency || shop;
      const fields = []; // {key, input, before, name}
      const suffixed = [];

      function amount(key, before, name, placeholder) {
        const input = textInput({
          value: inputValue(before),
          placeholder: placeholder || "",
          inputmode: "decimal",
          size: "sm",
          align: "right",
          suffix: currencyLabel(cur),
          ariaLabel: name,
          class: "pf-cm-input",
        });
        input.input.addEventListener("input", () => input.input.removeAttribute("aria-invalid"));
        fields.push({ key, input, before: before === undefined ? null : before, name });
        suffixed.push(input);
        return input;
      }

      const typeList = info.types && info.types.length ? info.types : TYPES.filter((x) => x !== "other").map((id) => ({ id, unit: null, shipping: null, products: 0 }));
      const typeValue = {};
      for (const tp of typeList) typeValue[tp.id] = tp.unit;
      const typeRows = typeList.map((tp) =>
        h(
          "div",
          { class: "pf-cm-row" },
          h(
            "div",
            { class: "pf-cm-type" },
            h("span", { class: "pf-cm-type-name" }, t(`type.${tp.id}`)),
            tp.products ? h("span", { class: "pf-cm-type-count" }, t("costs.products_count", { n: tp.products })) : null,
          ),
          amount(`type:${tp.id}`, tp.unit, `${t(`type.${tp.id}`)} · ${t("costs.col.unit")}`),
          amount(`shipping:${tp.id}`, tp.shipping, `${t(`type.${tp.id}`)} · ${t("costs.col.shipping")}`),
        ),
      );
      typeRows.push(
        h(
          "div",
          { class: "pf-cm-row pf-cm-row-default" },
          h(
            "div",
            { class: "pf-cm-type" },
            h("span", { class: "pf-cm-type-name" }, t("costs.default_shipping")),
            h("span", { class: "pf-cm-type-count" }, t("costs.default_shipping_hint")),
          ),
          h("span"),
          amount("shipping:order", info.shipping_order, t("costs.default_shipping")),
        ),
      );

      const products = (info.products || []).filter((p) => p.listing_id);
      const productRows = products.map((p) => {
        const typeUnit = typeValue[p.type];
        const row = h(
          "div",
          { class: "pf-cm-prod", dataset: { q: `${p.title} ${t(`type.${p.type}`)}`.toLocaleLowerCase() } },
          thumb({ src: p.image || null, size: 32, radius: 8 }),
          h("div", { class: "pf-cm-prod-text" }, h("span", { class: "pf-cm-prod-title ellipsis", title: p.title }, shortTitle(p.title)), h("span", { class: "pf-cm-prod-type" }, `${t(`type.${p.type}`)} · ${number(p.qty)}`)),
          amount(`listing:${p.listing_id}`, p.unit, p.title, typeUnit !== null && typeUnit !== undefined ? t("costs.type_value", { value: inputValue(typeUnit) }) : ""),
        );
        return row;
      });
      const productList = h("div", { class: "pf-cm-prods" }, productRows.length ? productRows : h("p", { class: "pf-cm-empty" }, t("costs.no_products")));
      const search =
        productRows.length > 8
          ? searchInput({
              placeholder: t("costs.search"),
              shortcut: null,
              onInput: (q) => {
                const needle = q.trim().toLocaleLowerCase();
                for (const row of productRows) row.hidden = !!needle && !row.dataset.q.includes(needle);
              },
            })
          : null;

      // Currency of the costs: the shop's, or TL (converted with TCMB's rate).
      const curChoices = shop === "TRY" ? ["TRY", "USD"] : [shop, "TRY"];
      const curBtns = curChoices.map((code) =>
        h("button", { type: "button", class: "pf-seg-btn", dataset: { code }, onClick: () => setCur(code) }, currencyLabel(code)),
      );
      function setCur(code) {
        cur = code;
        for (const btnEl of curBtns) {
          const on = btnEl.dataset.code === cur;
          btnEl.classList.toggle("is-active", on);
          btnEl.setAttribute("aria-pressed", on ? "true" : "false");
        }
        for (const input of suffixed) input.setSuffix(currencyLabel(cur));
      }
      setCur(cur);
      const curRow = pairOk() || cur !== shop
        ? h("div", { class: "pf-cm-cur" }, h("span", { class: "pf-cm-label" }, t("costs.currency")), h("div", { class: "pf-seg pf-seg-sm", role: "group", "aria-label": t("costs.currency") }, curBtns))
        : null;

      const labelsNote = state.data && state.data.summary && state.data.summary.shipping_source === "labels" ? infoNote({ tone: "info", icon: "truck", text: t("costs.labels_note") }) : null;

      const bodyEl = h(
        "div",
        { class: "pf-cm" },
        curRow,
        labelsNote,
        h(
          "section",
          { class: "pf-cm-section" },
          h("h3", { class: "pf-cm-h" }, t("costs.by_type")),
          h("div", { class: "pf-cm-row pf-cm-row-head" }, h("span", null, t("costs.col.type")), h("span", null, t("costs.col.unit")), h("span", null, t("costs.col.shipping"))),
          typeRows,
        ),
        h(
          "section",
          { class: "pf-cm-section" },
          h("div", { class: "pf-cm-h-row" }, h("div", null, h("h3", { class: "pf-cm-h" }, t("costs.by_product")), h("p", { class: "pf-cm-hint" }, t("costs.by_product_hint"))), search),
          productList,
        ),
      );

      const saveBtn = button({
        label: t("common.save"),
        variant: "primary",
        autoLoading: true,
        onClick: async () => {
          const set = {};
          let firstBad = null;
          for (const f of fields) {
            const v = parseAmount(f.input.value);
            if (Number.isNaN(v)) {
              f.input.input.setAttribute("aria-invalid", "true");
              if (!firstBad) firstBad = f;
              continue;
            }
            if (v !== f.before) set[f.key] = v;
          }
          if (firstBad) {
            ctx.toast({ tone: "warning", title: t("costs.invalid", { name: firstBad.name }), timeout: 3500 });
            firstBad.input.input.focus();
            return;
          }
          try {
            await ctx.api.post("/api/profit/costs", { set, currency: cur });
            m.close();
            ctx.toast({ tone: "success", title: t("costs.saved"), timeout: 2500 });
            await load({ quiet: state.mode === "ready" });
          } catch (err) {
            if (!ctx.api.isAbort(err)) ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
          }
        },
      });
      const m = ctx.modal({
        title: t("costs.title"),
        subtitle: t("costs.sub"),
        body: bodyEl,
        width: 660,
        class: "pf-costs-modal",
        actions: [button({ label: t("common.cancel"), variant: "secondary", onClick: () => m.close() }), saveBtn],
      });
    }

    // ---------------------------------------------------------------- wiring

    ctx.events.on("job", onJob);
    ctx.onStatus((s, prev) => {
      if (!prev || !s) return;
      const was = prev.state;
      if (s.state === was) return;
      if (s.state === "connected" && (state.mode === "setup" || state.mode === "error")) load();
      else if (state.mode === "setup" && (s.state === "keys" || s.state === "bad_keys")) renderSetup("keys");
      else if (state.mode === "setup" && s.state === "disconnected") renderSetup("connect");
    });

    renderHeader();
    // No keys / never connected: say so without asking the server for a 409 first.
    const st = ctx.status();
    if (st && (st.state === "keys" || st.state === "bad_keys")) renderSetup("keys");
    else if (st && st.state === "disconnected") renderSetup("connect");
    else await load();
    return () => {
      clearPoll();
      clearTimeout(state.animTimer);
      clearTimeout(state.pulseTimer);
      state.countRun += 1;
      if (guides) guides.disconnect();
    };
  },
};
