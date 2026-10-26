// İlanlar: the shop's drafts and live listings in one table (frame t250).
//
// GET /api/listings?tab=&q=&sort=&type=&seo=&page=&per_page= gives one page plus the tab
// counts; the tab, search, sort, filters and page live in the URL (?tab=draft&page=2).
// Publishing, CSV export and CSV import are here too; every write asks first.

import {
  badge,
  button,
  card,
  chips,
  cx,
  emptyState,
  h,
  iconButton,
  infoNote,
  mount,
  pagination,
  popover,
  progressBar,
  scoreRing,
  searchInput,
  select,
  spinner,
  table,
  tabs,
  thumb,
} from "../ui.js";
import { icon } from "../icons.js";
import { money, relative } from "../format.js";
import { ROUTES } from "../app.js";

const TABS = ["draft", "active", "all"];
const SORTS = ["updated", "seo", "title", "price"];
const SEO_BANDS = ["high", "mid", "low"];
const PRINT_FILTERS = ["missing", "present"];
const STATE_TONE = { draft: "warning", active: "success", inactive: "muted", expired: "danger", sold_out: "neutral" };
const NAV_KEY = "etsyotomasyon.listings.nav";
const MAX_TAGS = 13;
const ROW_HEIGHT = 65; // one table row (listings.css), as in the video (74 canvas px)
const FOOT_HEIGHT = 46; // the card's footer: selection, range, pagination
const CONTENT_PAD = 24; // .content's bottom padding
const ENTER_ROWS = 8; // rows that slide in one by one on the first load
const ENTER_GAP = 110; // ms between them (video: 8 rows over 26 frames)
// The video opens a draft at /ilanlar/taslak/<id>. Used once the shell has that route
// (app.js ROUTES); until then every listing opens at /ilanlar/<id>.
const DRAFT_ROUTE = ROUTES.some((r) => r.path === "/ilanlar/taslak/:id");
const REDUCED_MOTION = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

/** The detail page's address: drafts under /ilanlar/taslak/ as in the video. */
function detailPath(r) {
  return DRAFT_ROUTE && r.state === "draft" ? `/ilanlar/taslak/${r.id}` : `/ilanlar/${r.id}`;
}

function storeNav(value) {
  try {
    sessionStorage.setItem(NAV_KEY, JSON.stringify(value));
  } catch {
    /* private mode: the detail page just has no arrows */
  }
}

/**
 * Rows that fit the window with the footer in view and no page scroll: 8 at 1545x790
 * as in the video, 7 at 1280x720. Measured from the table's body once it is on screen
 * (a running job's panel above it takes room too); before that, from the plain layout.
 */
function fittingRows(tbody) {
  const top = (tbody && tbody.isConnected && tbody.getBoundingClientRect().top) || 189;
  const free = (window.innerHeight || 790) - top - FOOT_HEIGHT - 1 - CONTENT_PAD;
  return Math.max(5, Math.min(50, Math.floor(free / ROW_HEIGHT)));
}

export default {
  async mount(el, ctx) {
    const t = ctx.t;
    const q0 = ctx.query;
    const st = {
      tab: TABS.includes(q0.tab) ? q0.tab : "draft",
      tabGiven: TABS.includes(q0.tab),
      q: q0.q || "",
      sort: SORTS.includes(q0.sort) ? q0.sort : "updated",
      type: q0.type || "",
      seo: SEO_BANDS.includes(q0.seo) ? q0.seo : "",
      print: PRINT_FILTERS.includes(q0.print) ? q0.print : "",
      page: Math.max(1, parseInt(q0.page, 10) || 1),
      perPage: fittingRows(),
      data: null,
      known: new Map(), // listing id -> state, for rows seen on any page
      seq: 0,
      mode: "table", // table | setup | error
      jobs: new Map(), // job id -> live panel of a running publish / import
      // The drafts tab opens with its page ticked, ready for "Seçilileri yayınla" (the
      // video); once the person ticks or unticks anything, their choice is kept.
      selTouched: false,
      animated: false, // the rows' entrance plays on the first load only
    };
    const timers = [];

    const typeLabel = (kind, name) => (kind && kind !== "other" ? t(`type.${kind}`) : name || t("type.other"));

    // ------------------------------------------------------------ header
    const filterBtn = button({ label: t("filter.button"), icon: "list", variant: "secondary", class: "lst-filter-btn", onClick: () => openFilter() });
    const publishBtn = button({
      label: t("publish.button"),
      icon: "upload",
      variant: "primary",
      disabled: true,
      onClick: () => publishSelected(),
    });
    // The header holds Filtrele and Seçilileri yayınla only (the video); the CSV and
    // reload actions live at the bottom of the Filtrele popover.
    ctx.setHeader({ actions: [filterBtn, publishBtn] });

    // ------------------------------------------------------------ toolbar
    const tabsCtl = tabs({
      items: tabItems(null),
      value: st.tab,
      ariaLabel: t("title"),
      onChange: (id) => {
        st.tab = id;
        st.tabGiven = true;
        st.page = 1;
        st.type = "";
        syncQuery();
        load();
      },
    });
    const search = searchInput({
      placeholder: t("search.placeholder"),
      value: st.q,
      shortcut: "/",
      debounce: 250,
      class: "lst-search",
      onInput: (v) => {
        st.q = v.trim();
        st.page = 1;
        syncQuery();
        load();
      },
    });
    const sortSel = select({
      prefix: t("sort.prefix"),
      value: st.sort,
      class: "lst-sort",
      options: SORTS.map((s) => ({ value: s, label: t(`sort.${s}`) })),
      onChange: (v) => {
        st.sort = v;
        st.page = 1;
        syncQuery();
        load();
      },
    });
    // The video's sort box ends in a down arrow.
    const chev = sortSel.querySelector(".select-chev");
    if (chev) mount(chev, icon("arrow-down", { size: 14 }));
    const toolbar = h("div", { class: "lst-toolbar" }, tabsCtl.el, h("div", { class: "spacer" }), search, sortSel);

    // ------------------------------------------------------------ table
    // Column widths follow the video's row (IlanlarEkrani COL, GAP, padding): each is the
    // content width plus 9.6 px on either side; the check column also takes the row's
    // 21 px start and the last one its 21 px end (listings.css).
    const tbl = table({
      rowKey: "id",
      selectable: true,
      checkWidth: 50,
      skeletonRows: st.perPage,
      class: "lst-table",
      columns: [
        {
          key: "img",
          label: t("col.product"),
          width: 72,
          render: (r) => thumb({ src: r.thumb, size: 52, radius: 9, icon: "image" }),
        },
        {
          key: "title",
          label: t("col.title"),
          render: (r) =>
            h(
              "div",
              { class: "lst-title" },
              h("a", { class: "cell-title", href: detailPath(r), title: r.title, onClick: (e) => openRow(r, e) }, r.title || t("untitled")),
              h(
                "div",
                { class: "cell-sub" },
                h("span", { class: "lst-type" }, typeLabel(r.type, r.type_name)),
                r.source ? [h("span", { class: "lst-dot", "aria-hidden": "true" }, "·"), h("span", { class: "mono lst-file", title: r.source }, r.source)] : null,
                r.print_file === false ? [h("span", { class: "lst-dot", "aria-hidden": "true" }, "·"), h("span", { class: "lst-noprint", title: t("print.missing_tip") }, icon("alert", { size: 12 }), t("print.missing"))] : null,
              ),
            ),
        },
        {
          key: "state",
          label: t("col.state"),
          width: 121,
          render: (r) => badge({ text: t(`state.${r.state}`), tone: STATE_TONE[r.state] || "neutral", dot: true }),
        },
        { key: "tags", label: t("col.tags"), width: 128, render: (r) => tagMeter(r.tags) },
        { key: "seo", label: t("col.seo"), width: 110, render: (r) => seoPill(r) },
        {
          key: "price",
          label: t("col.price"),
          width: 103,
          align: "right",
          render: (r) => h("span", { class: "lst-price num" }, r.price === null || r.price === undefined ? "–" : money(r.price, r.currency || "USD")),
        },
        {
          key: "updated",
          label: t("col.updated"),
          width: 134,
          align: "right",
          render: (r) => h("span", { class: "lst-updated", title: r.updated ? new Date(r.updated * 1000).toLocaleString() : "" }, icon("clock", { size: 13 }), relative(r.updated)),
        },
      ],
      rows: null,
      onSelectionChange: () => {
        // Only a click or a key calls this: the pre-ticked page is now the person's own choice.
        st.selTouched = true;
        updateSelection();
      },
      onRowClick: (r, e) => openRow(r, e),
      empty: "",
    });

    const selInfo = h("span", { class: "lst-selinfo", hidden: true });
    const selSep = h("span", { class: "lst-sep", "aria-hidden": "true", hidden: true }, "·");
    const rangeInfo = h("span", { class: "lst-range" });
    const pager = pagination({
      page: st.page,
      pages: 1,
      onChange: (p) => {
        st.page = p;
        syncQuery();
        load();
      },
    });
    // The video's pager has arrows; the shared one draws chevrons.
    function pagerArrows() {
      const [prev, next] = pager.querySelectorAll(".page-arrow");
      if (prev) mount(prev, icon("arrow-left", { size: 15 }));
      if (next) mount(next, icon("arrow-right", { size: 15 }));
    }
    function updatePager(page, pages) {
      pager.update(page, pages);
      pagerArrows();
    }
    pagerArrows();
    const foot = h("div", { class: "lst-foot" }, selInfo, selSep, rangeInfo, h("div", { class: "spacer" }), pager);
    const tableCard = card({ pad: false, class: "lst-card", body: [tbl.el, foot] });
    const jobSlot = h("div", { class: "lst-jobs" });
    const body = h("div", { class: "lst-body" }, tableCard);
    el.append(jobSlot, toolbar, body);
    // The page is on screen now: count the rows from where the table really starts.
    st.perPage = fittingRows(tbl.el.querySelector("tbody"));

    // ------------------------------------------------------------ helpers
    function tabItems(counts) {
      const c = counts || {};
      return [
        { id: "draft", label: t("tab.draft"), count: c.draft ?? null },
        { id: "active", label: t("tab.active"), count: c.active ?? null },
        { id: "all", label: t("tab.all"), count: counts ? c.all : null },
      ];
    }

    function tagMeter(n) {
      const count = Math.max(0, Number(n) || 0);
      const tone = count >= MAX_TAGS ? "success" : count === 0 ? "danger" : "warning";
      const segs = [];
      for (let i = 0; i < MAX_TAGS; i += 1) segs.push(h("span", { class: cx("lst-seg", i < count && "is-on") }));
      return h(
        "div",
        { class: cx("lst-tags", `tone-${tone}`), title: t("tags.tooltip", { n: count, max: MAX_TAGS }) },
        h("span", { class: "lst-tags-top" }, icon("tag", { size: 13 }), h("span", { class: "num" }, `${count}/${MAX_TAGS}`)),
        h("span", { class: "lst-tags-bar", "aria-hidden": "true" }, segs),
      );
    }

    function seoPill(r) {
      const s = Number(r.seo) || 0;
      const tone = s >= 80 ? "success" : s >= 60 ? "warning" : "danger";
      return h(
        "span",
        { class: cx("lst-seo", `tone-${tone}`), title: r.issues ? t("seo.tooltip", { n: r.issues }) : t("seo.tooltip_clean") },
        scoreRing({ score: s, size: 15, stroke: 2.5 }),
        h("span", { class: "num" }, String(s)),
      );
    }

    function syncQuery() {
      ctx.setQuery({
        tab: st.tab === "draft" && !st.tabGiven ? null : st.tab,
        q: st.q || null,
        sort: st.sort === "updated" ? null : st.sort,
        type: st.type || null,
        seo: st.seo || null,
        print: st.print || null,
        page: st.page > 1 ? st.page : null,
      });
    }

    function selectedDrafts() {
      return [...tbl.selected].filter((id) => st.known.get(id) === "draft");
    }

    function updateSelection() {
      const n = tbl.selected.size;
      const drafts = selectedDrafts().length;
      publishBtn.setCount(drafts || null);
      publishBtn.setDisabled(!drafts || st.mode !== "table");
      publishBtn.title = drafts ? "" : t("publish.hint");
      mount(selInfo, icon("check", { size: 14, strokeWidth: 2.4 }), t("foot.selected", { n }));
      selInfo.hidden = n === 0;
      selSep.hidden = n === 0 || !rangeInfo.textContent;
    }

    function filterCount() {
      return (st.type ? 1 : 0) + (st.seo ? 1 : 0) + (st.print ? 1 : 0);
    }

    function updateFilterBtn() {
      filterBtn.setCount(filterCount() || null);
      filterBtn.classList.toggle("is-active", filterCount() > 0);
    }

    function openRow(r, e) {
      const ids = (st.data && st.data.ids) || [r.id];
      storeNav({ ids, tab: st.tab, back: location.pathname + location.search });
      if (e && (e.ctrlKey || e.metaKey || e.shiftKey) && e.currentTarget && e.currentTarget.tagName === "A") return; // new tab
      if (e) e.preventDefault();
      ctx.navigate(detailPath(r));
    }

    function emptyNode() {
      const filtered = st.q || st.type || st.seo || st.print;
      if (filtered) {
        return emptyState({
          icon: "search",
          title: t("empty.filtered"),
          message: t("empty.filtered_msg"),
          compact: true,
          action: button({ label: t("filter.clear"), variant: "secondary", size: "sm", onClick: clearFilters }),
        });
      }
      return emptyState({
        icon: st.tab === "draft" ? "upload" : "list",
        title: t(`empty.${st.tab}`),
        message: t(`empty.${st.tab}_msg`),
        action:
          st.tab === "draft"
            ? button({ label: t("empty.upload"), icon: "upload", variant: "primary", onClick: () => ctx.navigate("/tasarim-yukle") })
            : null,
      });
    }

    function clearFilters() {
      st.q = "";
      search.value = "";
      st.type = "";
      st.seo = "";
      st.print = "";
      st.page = 1;
      syncQuery();
      load();
    }

    function setMode(mode, node) {
      st.mode = mode;
      toolbar.hidden = mode === "setup";
      filterBtn.hidden = mode === "setup";
      publishBtn.hidden = mode === "setup";
      if (mode === "table") mount(body, tableCard);
      else mount(body, node);
      updateSelection();
    }

    function setupView(err) {
      const step = (err.params && err.params.step) || "keys";
      return card({
        class: "lst-setup",
        body: emptyState({
          icon: "link",
          title: t(`setup.${step === "connect" ? "connect" : "keys"}_title`),
          message: t("setup.message"),
          action: button({ label: t("setup.action"), iconRight: "arrow-right", variant: "primary", onClick: () => ctx.navigate("/kurulum/magaza") }),
        }),
      });
    }

    function errorView(err) {
      return card({
        class: "lst-error",
        body: infoNote({
          icon: "alert",
          tone: "danger",
          title: t("error.title"),
          text: ctx.api.errorText(err, t),
          action: button({ label: t("common.retry"), icon: "refresh", size: "sm", onClick: () => load() }),
        }),
      });
    }

    // ------------------------------------------------------------ loading
    async function load({ refresh = false } = {}) {
      const seq = ++st.seq;
      // No keys or no sign-in yet: say so without asking the server for a 409.
      const status = ctx.status();
      const missing = status && (status.state === "keys" ? "keys" : status.state === "disconnected" ? "connect" : null);
      if (missing) {
        setMode("setup", setupView({ params: { step: missing } }));
        ctx.setHeader({ subtitle: t("subtitle") });
        return;
      }
      if (st.mode !== "table") setMode("table");
      tbl.update(null);
      pager.hidden = true;
      rangeInfo.textContent = "";
      selSep.hidden = true;
      let data;
      try {
        data = await ctx.api.get(
          "/api/listings",
          {
            tab: st.tab,
            q: st.q || undefined,
            sort: st.sort,
            type: st.type || undefined,
            seo: st.seo || undefined,
            print: st.print || undefined,
            page: st.page,
            per_page: st.perPage,
            refresh: refresh ? 1 : undefined,
          },
          { signal: ctx.signal },
        );
      } catch (err) {
        if (ctx.api.isAbort(err) || seq !== st.seq) return;
        if (err.code === "setup_needed") setMode("setup", setupView(err));
        else setMode("error", errorView(err));
        ctx.setHeader({ subtitle: t("subtitle") });
        return;
      }
      if (seq !== st.seq) return;
      // A shop without drafts opens on everything rather than an empty tab.
      if (!st.tabGiven && st.tab === "draft" && !data.counts.draft && data.counts.all && !st.q && !filterCount()) {
        st.tab = "all";
        tabsCtl.update(null, "all");
        return load();
      }
      render(data);
    }

    function render(data) {
      st.data = data;
      st.page = data.page;
      for (const r of data.items) st.known.set(r.id, r.state);
      tabsCtl.update(tabItems(data.counts), st.tab);
      const all = data.counts.all || 0;
      const drafts = data.counts.draft || 0;
      // "yeni" only for the drafts of etsyotomasyon's latest run (counts.new_drafts); older
      // drafts are just drafts.
      const fresh = Math.min(drafts, data.counts.new_drafts || 0);
      const parts = [t("count.listings", { n: all })];
      if (fresh) parts.push(t("count.new_drafts", { n: fresh }));
      else if (drafts) parts.push(t("count.drafts", { n: drafts }));
      ctx.setHeader({ subtitle: parts.join(" · ") });
      if (!st.selTouched) {
        // Untouched: the drafts tab ticks its page (the video's "8 ilan seçili"); the
        // other tabs start with nothing ticked.
        const ids = st.tab === "draft" ? data.items.filter((r) => r.state === "draft").map((r) => r.id) : [];
        tbl.update(data.items, new Set(ids));
      } else {
        tbl.update(data.items);
      }
      if (!data.items.length) {
        const tr = tbl.el.querySelector(".tbl-empty td");
        if (tr) mount(tr, emptyNode());
      } else if (!st.animated) {
        st.animated = true;
        enterRows();
      }
      updatePager(data.page, data.pages);
      pager.hidden = data.pages <= 1;
      foot.hidden = !data.total && !tbl.selected.size;
      rangeInfo.textContent = data.total
        ? t(`range.${st.tab}`, { total: data.total, start: data.start, end: data.end }) + (st.q || filterCount() ? ` · ${t("range.filtered")}` : "")
        : "";
      updateFilterBtn();
      updateSelection();
      if (data.truncated) ctx.toast({ tone: "warning", title: t("truncated") });
    }

    /** The first load's rows fade in and rise one after another (the video). */
    function enterRows() {
      if (REDUCED_MOTION) return;
      const trs = [...tbl.el.querySelectorAll("tbody tr")].slice(0, ENTER_ROWS);
      trs.forEach((tr, i) => {
        tr.style.setProperty("--i", String(i));
        tr.style.setProperty("--gap", `${ENTER_GAP}ms`);
        tr.classList.add("is-entering");
        tr.addEventListener(
          "animationend",
          () => {
            tr.classList.remove("is-entering");
            tr.style.removeProperty("--i");
            tr.style.removeProperty("--gap");
          },
          { once: true },
        );
      });
    }

    // ------------------------------------------------------------ filter popover
    function openFilter() {
      const types = (st.data && st.data.types) || [];
      const typeChips = chips({
        items: [{ id: "", label: t("filter.all") }, ...types.map((x) => ({ id: x.id, label: typeLabel(x.id), count: x.count }))],
        value: st.type,
        ariaLabel: t("filter.type"),
        onChange: (id) => {
          st.type = id;
          st.page = 1;
          syncQuery();
          load();
        },
      });
      const seoChips = chips({
        items: [{ id: "", label: t("filter.all") }, ...SEO_BANDS.map((b) => ({ id: b, label: t(`filter.seo_${b}`) }))],
        value: st.seo,
        ariaLabel: t("filter.seo"),
        onChange: (id) => {
          st.seo = id;
          st.page = 1;
          syncQuery();
          load();
        },
      });
      // Baskı dosyası: the listings an order of which could not be printed (drop/printfiles).
      const counts = (st.data && st.data.prints) || {};
      const printChips = chips({
        items: [
          { id: "", label: t("filter.all") },
          { id: "missing", label: t("filter.print_missing"), count: counts.missing ?? null },
          { id: "present", label: t("filter.print_present"), count: counts.present ?? null },
        ],
        value: st.print,
        ariaLabel: t("filter.print"),
        onChange: (id) => {
          st.print = id;
          st.page = 1;
          syncQuery();
          load();
        },
      });
      const pop = popover(
        filterBtn,
        h(
          "div",
          { class: "lst-filter" },
          h("p", { class: "lst-filter-label" }, t("filter.type")),
          types.length ? typeChips.el : h("p", { class: "muted lst-filter-none" }, t("filter.no_types")),
          h("p", { class: "lst-filter-label" }, t("filter.seo")),
          seoChips.el,
          h("p", { class: "lst-filter-label" }, t("filter.print")),
          printChips.el,
          h(
            "div",
            { class: "lst-filter-foot" },
            button({
              label: t("filter.clear"),
              variant: "ghost",
              size: "sm",
              disabled: !filterCount(),
              onClick: () => {
                if (pop) pop.close();
                st.type = "";
                st.seo = "";
                st.print = "";
                st.page = 1;
                syncQuery();
                load();
              },
            }),
          ),
        ),
        { placement: "bottom-end", width: 340, class: "lst-filter-pop", role: "dialog" },
      );
      if (!pop) return;
      pop.el.setAttribute("aria-label", t("filter.button"));
      // CSV and reload: below the filters; each one closes the popover first.
      const act = (label, ic, fn) =>
        button({
          label,
          icon: ic,
          variant: "ghost",
          size: "sm",
          class: "lst-data-btn",
          onClick: () => {
            pop.close();
            fn();
          },
        });
      pop.el.firstElementChild.append(
        h(
          "div",
          { class: "lst-data", role: "group", "aria-label": t("filter.data") },
          h("p", { class: "lst-filter-label" }, t("filter.data")),
          act(t("more.export", { tab: t(`tab.${st.tab}`) }), "download", exportCsv),
          act(t("more.import"), "upload", () => fileInput.click()),
          act(t("more.template"), "file", downloadTemplate),
          act(t("more.refresh"), "refresh", () => load({ refresh: true })),
        ),
      );
      pop.reposition();
    }

    // ------------------------------------------------------------ CSV import file picker
    const fileInput = h("input", { type: "file", accept: ".csv,text/csv", class: "sr-only", tabindex: "-1", "aria-hidden": "true" });
    fileInput.addEventListener("change", () => {
      const file = fileInput.files && fileInput.files[0];
      fileInput.value = "";
      if (file) importCsv(file);
    });
    el.append(fileInput);

    function exportCsv() {
      ctx.api.download("/api/listings/export.csv", { params: { tab: st.tab }, filename: "listings.csv" }).catch((err) => ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) }));
    }

    function downloadTemplate() {
      ctx.api.download("/api/listings/template.csv", { filename: "listings-template.csv" }).catch((err) => ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) }));
    }

    // ------------------------------------------------------------ jobs (publish / import)
    function jobPanel(job, kind) {
      const bar = progressBar({ value: 0, max: job.params && job.params.n ? job.params.n : 1, tone: "accent", size: "sm" });
      const label = h("span", { class: "lst-job-label" });
      const stopBtn = button({
        label: t("common.stop"),
        size: "sm",
        variant: "secondary",
        onClick: async () => {
          try {
            await ctx.api.post(`/api/jobs/${job.id}/cancel`, {});
          } catch (err) {
            ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
          }
        },
      });
      const lines = h("ul", { class: "lst-job-lines" });
      const node = h(
        "div",
        { class: "lst-job card" },
        h("div", { class: "lst-job-head" }, spinner({ size: 16, tone: "accent" }), label, h("div", { class: "spacer" }), stopBtn),
        bar.el,
        lines,
      );
      const panel = { job, kind, node, bar, label, lines, stopBtn, done: false, seen: new Set() };
      update(job);
      function update(j) {
        const total = (j.progress && j.progress.total) || (j.params && j.params.n) || 0;
        const done = (j.progress && j.progress.done) || 0;
        bar.update(done, total || 1);
        label.textContent = t(`job.${kind}_running`, { done, total });
      }
      panel.update = update;
      return panel;
    }

    // A note or refusal from the CSV check: in the seller's language when the server gave
    // it a code this page has words for (import.warn.* / import.err.*), else as it came.
    function importText(note, group) {
      if (!note) return "";
      if (typeof note === "string") return note;
      const key = note.code ? `import.${group}.${note.code}` : "";
      return key && t.has(key) ? t(key, note.params || {}) : note.text || "";
    }
    function importProblems(r) {
      let list = r.problems || [];
      // "Nothing left to send" only follows from the row's other refusals: say those.
      if (list.length > 1) list = list.filter((p) => p.code !== "nothing_to_update");
      const parts = list.map((p) => importText(p, "err")).filter(Boolean);
      return parts.length ? parts : [r.message].filter(Boolean);
    }
    function lineText(panel, item) {
      // The server's "already active" is English; a skipped publish is "zaten yayında".
      if (panel.kind === "publish" && item.status === "skipped") return t("result.skipped");
      if (item.problems && item.problems.length) return importProblems(item).join("; ");
      return item.message || t(`result.${item.status}`);
    }

    function addLine(panel, item) {
      if (item.status === "ok" || item.status === "dry-run") return;
      const key = String(item.id ?? item.row ?? item.title);
      if (panel.seen.has(key)) return;
      panel.seen.add(key);
      const tone = item.status === "error" ? "danger" : "warning";
      const name = item.title || `#${item.id || item.listing_id || item.row}`;
      panel.lines.appendChild(
        h(
          "li",
          { class: cx("lst-job-line", `tone-${tone}`) },
          icon(tone === "danger" ? "x-circle" : "alert", { size: 14 }),
          h("span", { class: "lst-job-name" }, name),
          h("span", { class: "lst-job-msg", title: item.hint || undefined }, lineText(panel, item)),
        ),
      );
    }

    function watchJob(job, kind) {
      if (st.jobs.has(job.id)) return st.jobs.get(job.id);
      const panel = jobPanel(job, kind);
      st.jobs.set(job.id, panel);
      jobSlot.appendChild(panel.node);
      return panel;
    }

    async function finishJob(panel, summary) {
      if (panel.done) return;
      panel.done = true;
      let full = summary;
      try {
        full = await ctx.api.get(`/api/jobs/${summary.id}`, null, { signal: ctx.signal });
      } catch {
        /* the summary is enough */
      }
      const r = full.result || {};
      const ok = full.status === "done";
      let tone = "success";
      let text;
      if (panel.kind === "publish") {
        text = ok ? t("job.publish_done", { n: r.published || 0 }) : t(`job.${full.status}`);
        if (!ok || r.failed) tone = r.published ? "warning" : "danger";
        if (r.failed) text += ` · ${t("job.publish_failed", { n: r.failed })}`;
        for (const item of r.results || []) {
          const title = (st.data && st.data.items.find((x) => x.id === item.id)) || null;
          if (item.status !== "ok") addLine(panel, { ...item, title: title ? title.title : "" });
        }
        const published = new Set((r.results || []).filter((x) => x.status === "ok").map((x) => x.id));
        tbl.update(undefined, new Set([...tbl.selected].filter((id) => !published.has(id))));
      } else {
        text = ok ? t("job.import_done", { created: r.created || 0, updated: r.updated || 0 }) : t(`job.${full.status}`);
        if (!ok || r.errors || r.partial) tone = "warning";
        if (r.errors) text += ` · ${t("job.import_errors", { n: r.errors })}`;
        if (r.reason) addLine(panel, { status: "error", title: t("import.aborted"), message: r.reason });
      }
      if (full.status === "error" && full.error) addLine(panel, { status: "error", title: t("job.error"), message: ctx.api.errorText(full.error, t) });
      mount(
        panel.node,
        h(
          "div",
          { class: "lst-job-head" },
          h("span", { class: cx("icon-tile", "icon-tile-sm", `tone-${tone}`) }, icon(tone === "success" ? "check" : "alert", { size: 14, strokeWidth: 2.2 })),
          h("span", { class: "lst-job-label" }, text),
          h("div", { class: "spacer" }),
          iconButton({ icon: "x", title: t("common.close"), variant: "ghost", size: "sm", onClick: () => dropPanel(panel) }),
        ),
        panel.lines.children.length ? panel.lines : null,
      );
      if (tone === "success") timers.push(setTimeout(() => dropPanel(panel), 6000));
      load();
    }

    function dropPanel(panel) {
      panel.node.remove();
      st.jobs.delete(panel.job.id);
    }

    ctx.events.on("job", (job) => {
      // Tasarım Yükle made new drafts: show them (the server dropped its cache already).
      if (job && job.kind === "designs" && ["done", "cancelled", "error"].includes(job.status) && !(job.params && job.params.dry_run)) {
        load();
        return;
      }
      if (!job || (job.kind !== "publish" && job.kind !== "listings-import")) return;
      const kind = job.kind === "publish" ? "publish" : "import";
      const panel = st.jobs.get(job.id) || (job.status === "running" || job.status === "queued" ? watchJob(job, kind) : null);
      if (!panel) return;
      if (job.status === "queued" || job.status === "running") panel.update(job);
      else finishJob(panel, job);
    });
    ctx.events.on("job-event", (ev) => {
      const panel = ev && st.jobs.get(ev.job_id);
      if (!panel || ev.type !== "row") return;
      const item = ev.data || {};
      if (panel.kind === "publish") {
        const row = st.data && st.data.items.find((x) => x.id === item.id);
        addLine(panel, { ...item, title: row ? row.title : "" });
      } else {
        addLine(panel, item);
      }
    });

    async function resumeJobs() {
      for (const kind of ["publish", "listings-import"]) {
        try {
          const list = await ctx.api.get("/api/jobs", { kind, active: 1 }, { signal: ctx.signal });
          for (const job of list || []) watchJob(job, kind === "publish" ? "publish" : "import");
        } catch {
          /* nothing to resume */
        }
      }
    }

    // ------------------------------------------------------------ publish
    async function publishSelected() {
      // From here on the selection is the person's: the next drafts are not ticked for them.
      st.selTouched = true;
      const ids = selectedDrafts();
      if (!ids.length) return;
      const ok = await ctx.confirm({
        title: t("publish.confirm_title", { n: ids.length }),
        message: t("publish.confirm_msg", { n: ids.length }),
        confirmLabel: t("publish.confirm", { n: ids.length }),
        icon: "upload",
      });
      if (!ok) return;
      try {
        const job = await ctx.api.post("/api/listings/publish", { ids, confirm: true });
        watchJob(job, "publish");
      } catch (err) {
        ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
      }
    }

    // ------------------------------------------------------------ CSV import
    async function importCsv(file) {
      const bodyEl = h("div", { class: "lst-import" }, h("div", { class: "lst-import-wait" }, spinner({ size: 18, tone: "accent" }), h("span", null, t("import.checking", { name: file.name }))));
      let check = null;
      const sendBtn = button({
        label: t("import.send"),
        icon: "upload",
        variant: "primary",
        disabled: true,
        onClick: async () => {
          if (!check) return;
          const parts = [t("import.confirm_msg", { creates: check.creates, updates: check.updates })];
          if (check.publishes) parts.push(t("import.confirm_publish", { n: check.publishes }));
          const yes = await ctx.confirm({
            title: t("import.confirm_title", { n: check.rows }),
            message: parts.join(" "),
            confirmLabel: t("import.send"),
            icon: "upload",
          });
          if (!yes) return;
          try {
            const job = await ctx.api.post("/api/listings/import/apply", { token: check.token, confirm: true });
            m.close();
            watchJob(job, "import");
          } catch (err) {
            ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
          }
        },
      });
      const m = ctx.modal({
        title: t("import.title"),
        subtitle: file.name,
        width: 760,
        body: bodyEl,
        actions: [button({ label: t("common.cancel"), variant: "secondary", onClick: () => m.close() }), sendBtn],
      });
      try {
        check = await ctx.api.upload("/api/listings/import", file, { method: "POST", signal: ctx.signal });
      } catch (err) {
        if (ctx.api.isAbort(err)) return;
        mount(bodyEl, infoNote({ tone: "danger", icon: "alert", text: ctx.api.errorText(err, t) }));
        return;
      }
      const summary = h(
        "div",
        { class: "lst-import-sum" },
        badge({ text: t("import.rows", { n: check.rows }), tone: "neutral" }),
        check.creates ? badge({ text: t("import.creates", { n: check.creates }), tone: "accent", icon: "plus" }) : null,
        check.updates ? badge({ text: t("import.updates", { n: check.updates }), tone: "accent", icon: "edit" }) : null,
        check.errors ? badge({ text: t("import.errors", { n: check.errors }), tone: "danger", icon: "x" }) : badge({ text: t("import.no_errors"), tone: "success", icon: "check" }),
        check.warnings ? badge({ text: t("import.warnings", { n: check.warnings }), tone: "warning", icon: "alert" }) : null,
      );
      const rows = check.results.map((r) =>
        h(
          "tr",
          { class: cx(r.status === "error" && "is-error") },
          h("td", { class: "num muted" }, String(r.row)),
          h("td", null, badge({ text: t(`import.action_${r.action}`), tone: r.action === "create" ? "accent" : r.action === "update" ? "info" : "neutral", size: "sm" })),
          h("td", { class: "lst-import-title" }, r.title || (r.listing_id ? `#${r.listing_id}` : "–")),
          h(
            "td",
            { class: "lst-import-msg" },
            r.status === "error"
              ? importProblems(r).map((text) => h("span", { class: "tone-danger lst-import-err" }, icon("x-circle", { size: 13 }), text))
              : h("span", { class: "tone-success lst-import-ok" }, icon("check", { size: 13 }), t("import.ready")),
            (r.warnings || []).map((w) => h("span", { class: "lst-import-warn" }, icon("alert", { size: 12 }), importText(w, "warn"))),
          ),
        ),
      );
      mount(
        bodyEl,
        summary,
        check.errors ? infoNote({ tone: "danger", icon: "alert", text: t("import.fix_first") }) : infoNote({ tone: "info", icon: "info", text: t("import.note") }),
        h(
          "div",
          { class: "lst-import-table" },
          h(
            "table",
            { class: "tbl" },
            h("thead", null, h("tr", null, h("th", { style: { width: 56 } }, t("import.col_row")), h("th", { style: { width: 120 } }, t("import.col_action")), h("th", { style: { width: "34%" } }, t("import.col_title")), h("th", null, t("import.col_result")))),
            h("tbody", null, rows),
          ),
        ),
      );
      sendBtn.setDisabled(!!check.errors || !(check.creates + check.updates));
    }

    // ------------------------------------------------------------ live updates
    ctx.onStatus((s, prev) => {
      if (!s || (prev && s.state === prev.state)) return;
      if (s.state === "connected" && st.mode !== "table") load();
      else if ((s.state === "keys" || s.state === "disconnected") && st.mode !== "setup") load();
    });

    // A taller or shorter window changes how many rows fit; the page then starts at the
    // listing that was first on screen.
    let resizeTimer = null;
    const onResize = () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (st.mode !== "table") return;
        const rows = fittingRows(tbl.el.querySelector("tbody"));
        if (rows === st.perPage) return;
        st.perPage = rows;
        st.page = st.data && st.data.start ? Math.floor((st.data.start - 1) / rows) + 1 : 1;
        syncQuery();
        load();
      }, 250);
    };
    window.addEventListener("resize", onResize);

    updateFilterBtn();
    await Promise.all([load(), resumeJobs()]);
    return () => {
      window.removeEventListener("resize", onResize);
      clearTimeout(resizeTimer);
      timers.forEach(clearTimeout);
    };
  },
};
