// Niş Bulucu: how much a keyword gets, how crowded it is, and a watch list.
//
// GET /api/niche?keyword= (a day's cache on the server), GET/POST /api/niche/watch,
// DELETE /api/niche/watch/:id. Everything is read from Etsy's public search; nothing is
// changed on Etsy.

import { badge, button, card, cx, emptyState, h, iconButton, infoNote, mount } from "../ui.js";
import { icon } from "../icons.js";
import { compact, money, number, percent } from "../format.js";

const RECENT_KEY = "etsyotomasyon.niche.recent";
const SPARK_W = 96;
const SPARK_H = 28;

function recent() {
  try {
    const v = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
    return Array.isArray(v) ? v.filter((x) => typeof x === "string").slice(0, 6) : [];
  } catch {
    return [];
  }
}

function remember(keyword) {
  try {
    const next = [keyword, ...recent().filter((x) => x !== keyword)].slice(0, 6);
    localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    /* only remembered */
  }
}

/** A tiny line of a listing's favourites over time. */
function sparkline(history) {
  const points = (history || []).filter((p) => Array.isArray(p));
  const wrap = h("span", { class: "nf-spark" });
  if (points.length < 2) return wrap;
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const [x0, x1] = [Math.min(...xs), Math.max(...xs)];
  const [y0, y1] = [Math.min(...ys), Math.max(...ys)];
  const px = (x) => (x1 === x0 ? 0 : ((x - x0) / (x1 - x0)) * (SPARK_W - 2) + 1);
  const py = (y) => (y1 === y0 ? SPARK_H / 2 : SPARK_H - 2 - ((y - y0) / (y1 - y0)) * (SPARK_H - 4));
  const d = points.map((p, i) => `${i ? "L" : "M"}${px(p[0]).toFixed(1)},${py(p[1]).toFixed(1)}`).join(" ");
  wrap.innerHTML = `<svg viewBox="0 0 ${SPARK_W} ${SPARK_H}" width="${SPARK_W}" height="${SPARK_H}" aria-hidden="true"><path d="${d}" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  return wrap;
}

export default {
  async mount(el, ctx) {
    const t = ctx.t;
    const st = { keyword: ctx.query.keyword || "", data: null, loading: false, error: null, watch: [], seq: 0 };
    const input = h("input", { type: "search", class: "input nf-input", placeholder: t("search.placeholder"), value: st.keyword, "aria-label": t("search.label") });
    const goBtn = button({ label: t("search.go"), icon: "search", variant: "primary", onClick: () => run() });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") run();
    });
    const recentEl = h("div", { class: "nf-recent" });
    const body = h("div", { class: "nf-body" });
    const watchEl = h("div", { class: "nf-watch" });
    el.append(
      h("div", { class: "nf-bar" }, input, goBtn),
      recentEl,
      infoNote({ icon: "info", tone: "neutral", text: t("note") }),
      body,
      watchEl,
    );

    const watched = () => new Set(st.watch.map((x) => x.id));

    function renderRecent() {
      const list = recent();
      mount(recentEl, ...list.map((k) => h("button", { type: "button", class: "nf-chip", onClick: () => { input.value = k; run(); } }, k)));
    }

    function metric(label, value, sub, tone) {
      return h("div", { class: cx("nf-metric", tone && `tone-${tone}`) }, h("span", { class: "nf-metric-label" }, label), h("strong", { class: "nf-metric-value num" }, value), sub ? h("span", { class: "nf-metric-sub" }, sub) : null);
    }

    function listingRow(r) {
      const on = watched().has(r.id);
      const star = iconButton({
        icon: on ? "check" : "plus",
        title: on ? t("watch.on") : t("watch.add"),
        variant: on ? "primary" : "ghost",
        size: "sm",
        disabled: on,
        onClick: async () => {
          try {
            const res = await ctx.api.post("/api/niche/watch", { item: r });
            st.watch = res.items;
            renderResult();
            renderWatch();
            ctx.toast({ tone: "success", title: t("watch.added") });
          } catch (err) {
            ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
          }
        },
      });
      return h(
        "li",
        { class: "nf-row" },
        h("div", { class: "nf-row-main" }, r.url ? h("a", { href: r.url, target: "_blank", rel: "noopener noreferrer", "data-external": "", class: "ellipsis", title: r.title }, r.title) : h("span", { class: "ellipsis" }, r.title), h("span", { class: "muted nf-row-sub" }, [r.price !== null ? money(r.price, r.currency || "USD") : "–", t("row.age", { n: r.age_days }), r.digital ? t("row.digital") : null].filter(Boolean).join(" · "))),
        h("div", { class: "nf-row-num" }, h("strong", { class: "num" }, number(r.views_per_day, 1)), h("span", { class: "muted" }, t("row.per_day"))),
        h("div", { class: "nf-row-num" }, h("strong", { class: "num" }, compact(r.favorers)), h("span", { class: "muted" }, t("row.favorites"))),
        star,
      );
    }

    function renderResult() {
      const d = st.data;
      if (st.loading) {
        mount(body, h("p", { class: "muted nf-loading" }, icon("refresh", { size: 14 }), t("loading")));
        return;
      }
      if (st.error) {
        mount(body, infoNote({ icon: "alert", tone: "danger", text: st.error, action: button({ label: t("retry"), size: "sm", icon: "refresh", onClick: () => run(true) }) }));
        return;
      }
      if (!d) {
        mount(body, emptyState({ icon: "search", title: t("idle.title"), message: t("idle.message"), compact: true }));
        return;
      }
      if (!d.sampled) {
        mount(body, emptyState({ icon: "search", title: t("none.title"), message: t("none.message"), compact: true }));
        return;
      }
      const op = d.opportunity;
      const cur = (d.price && d.price.currency) || "USD";
      const metrics = h(
        "div",
        { class: "nf-metrics" },
        metric(t("m.opportunity"), String(op.score), t(`level.${op.level}`), op.level === "high" ? "success" : op.level === "mid" ? "warning" : "danger"),
        metric(t("m.demand"), `${number(d.demand.views_per_day, 1)} ${t("row.per_day")}`, t("m.demand_sub", { fav: compact(d.demand.favorers) })),
        metric(t("m.competition"), compact(d.total), t("m.competition_sub", { share: percent(d.competition.top_shops_share || 0), fresh: percent(d.competition.new_share || 0) })),
        metric(t("m.price"), d.price.median !== null ? money(d.price.median, cur) : "–", d.price.min !== null ? `${money(d.price.min, cur)} – ${money(d.price.max, cur)}` : ""),
      );
      const list = (title, sub, rows, empty) =>
        card({ title, subtitle: sub, class: "nf-card", body: rows.length ? h("ol", { class: "nf-list" }, rows.map(listingRow)) : h("p", { class: "muted" }, empty) });
      const chips = (items, key) => h("div", { class: "nf-chips" }, items.map((x) => h("span", { class: "badge tone-neutral", title: t("chips.count", { n: x.count, total: d.sampled }) }, `${x[key]} · ${x.count}`)));
      mount(
        body,
        h("p", { class: "muted nf-sample" }, t("sample", { n: d.sampled, total: number(d.total), cached: d.cached ? t("cached") : "" })),
        metrics,
        h("p", { class: "muted nf-formula" }, t("formula")),
        h("div", { class: "nf-two" }, list(t("rising.title"), t("rising.sub"), d.rising, t("rising.empty")), list(t("trending.title"), t("trending.sub"), d.trending, t("trending.empty"))),
        h("div", { class: "nf-two" }, card({ title: t("tags.title"), class: "nf-card", body: chips(d.tags, "tag") }), card({ title: t("phrases.title"), class: "nf-card", body: chips(d.phrases, "phrase") })),
      );
    }

    function renderWatch() {
      if (!st.watch.length) {
        mount(watchEl, card({ title: t("watch.title"), subtitle: t("watch.sub"), class: "nf-card", body: h("p", { class: "muted" }, t("watch.empty")) }));
        return;
      }
      const rows = st.watch.map((w) =>
        h(
          "li",
          { class: "nf-row" },
          h("div", { class: "nf-row-main" }, w.url ? h("a", { href: w.url, target: "_blank", rel: "noopener noreferrer", "data-external": "", class: "ellipsis", title: w.title }, w.title) : h("span", { class: "ellipsis" }, w.title), h("span", { class: "muted nf-row-sub" }, [w.price !== null && w.price !== undefined ? money(w.price, w.currency || "USD") : "–", t("watch.since", { n: Math.max(0, Math.round((Date.now() / 1000 - (w.added || 0)) / 86400)) })].join(" · "))),
          sparkline(w.history),
          h("div", { class: "nf-row-num" }, h("strong", { class: "num" }, compact(w.favorers)), h("span", { class: cx("nf-delta", w.favorers_change > 0 && "is-up") }, w.favorers_change ? `${w.favorers_change > 0 ? "+" : ""}${w.favorers_change}` : "–")),
          h("div", { class: "nf-row-num" }, h("strong", { class: "num" }, compact(w.views)), h("span", { class: cx("nf-delta", w.views_change > 0 && "is-up") }, w.views_change ? `${w.views_change > 0 ? "+" : ""}${compact(w.views_change)}` : "–")),
          iconButton({
            icon: "trash",
            title: t("watch.remove"),
            variant: "ghost",
            size: "sm",
            onClick: async () => {
              try {
                const res = await ctx.api.del(`/api/niche/watch/${w.id}`);
                st.watch = res.items;
                renderWatch();
                renderResult();
              } catch (err) {
                ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
              }
            },
          }),
        ),
      );
      mount(watchEl, card({ title: t("watch.title"), subtitle: t("watch.sub"), class: "nf-card", actions: badge({ text: String(st.watch.length), tone: "neutral" }), body: h("ol", { class: "nf-list" }, rows) }));
    }

    async function run(refresh = false) {
      const keyword = input.value.trim();
      if (!keyword) return;
      const seq = ++st.seq;
      st.keyword = keyword;
      st.loading = true;
      st.error = null;
      ctx.setQuery({ keyword });
      renderResult();
      try {
        const data = await ctx.api.get("/api/niche", { keyword, refresh: refresh ? 1 : undefined }, { signal: ctx.signal });
        if (seq !== st.seq) return;
        st.data = data;
        remember(data.keyword);
        renderRecent();
      } catch (err) {
        if (ctx.api.isAbort(err) || seq !== st.seq) return;
        st.data = null;
        st.error = ctx.api.errorText(err, t);
      }
      st.loading = false;
      renderResult();
    }

    renderRecent();
    renderResult();
    renderWatch();
    try {
      const res = await ctx.api.get("/api/niche/watch", null, { signal: ctx.signal });
      st.watch = res.items || [];
      renderWatch();
      renderResult();
    } catch (err) {
      if (!ctx.api.isAbort(err)) renderWatch();
    }
    if (st.keyword) run();
  },
};
