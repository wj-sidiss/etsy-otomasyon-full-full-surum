// SEO: the audit list (left), competitor tag research (right) and the "Düzelt" dialog.
//
// Data: GET /api/seo/audit (cached 5 min on the server, "Yeniden tara" forces a rescan),
// GET /api/seo/research (7-day cache), POST /api/seo/fix/{id} after a confirmation.
// "+ Ekle" collects tags per listing in memory; the dialog applies them with the rest.

import {
  h,
  cx,
  mount,
  button,
  iconButton,
  tabs,
  searchInput,
  scoreRing,
  progressBar,
  emptyState,
  infoNote,
  textInput,
  checkbox,
  tagChip,
  popover,
  thumb,
  badge,
  field,
} from "../ui.js";
import { icon } from "../icons.js";
import { money, percent, list as listText, lower } from "../format.js";

const PAGE = 40; // cards rendered at once; "N ilan daha göster" adds the next page
const RING = 72;
const RING_FONT = 22; // the number inside the ring: 0.3 x the ring, as in SeoEkrani.tsx
const MAX_TAGS = 13;
const MAX_TAG_LEN = 20;
const CHIP_LIMIT = 3;
const SAMPLE = 300; // RESEARCH_SAMPLE in api/seo.py
const TAG_ROWS = 5; // research rows on the card (the video's top 5); the rest are in Düzelt
const REVEAL_GAP = 180; // ms between research rows arriving
const COUNT_MS = 800; // a research row's % and bar growing from 0
// Etsy's tag and material character rules (updateListing in the OpenAPI spec).
const TAG_OK = /^[\p{L}\p{Nd}\s\-'™©®]+$/u;
const MATERIAL_OK = /^[\p{L}\p{Nd}\s]+$/u;
const REDUCED_MOTION = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Turkish suffix after a numeral: 4'ü, 9'u, 6'sı ("13 etiketten 4'ü kullanılmış"). */
function trSuffix(n) {
  const ones = ["", "i", "si", "ü", "ü", "i", "sı", "si", "i", "u"];
  const tens = ["", "u", "si", "u", "ı", "si", "ı", "i", "i", "ı"];
  const v = Math.abs(Math.trunc(Number(n) || 0));
  if (v === 0) return "ı";
  if (v % 10) return ones[v % 10];
  if (v % 100) return tens[Math.floor(v / 10) % 10];
  return v % 1000 ? "ü" : "i";
}

function cleanTag(s) {
  return String(s || "").trim().split(/\s+/).filter(Boolean).join(" ");
}

/** A ring's colour follows the listing's final score from the first frame (the video). */
function toneOf(score) {
  return score >= 80 ? "success" : score >= 60 ? "warning" : "danger";
}

function setRing(ring, value, tone) {
  ring.update(value);
  for (const c of [...ring.classList]) if (c.startsWith("tone-")) ring.classList.remove(c);
  ring.classList.add(`tone-${tone}`);
}

function isTyping(el) {
  return !!(el && el.closest && el.closest("input, textarea, select, [contenteditable=''], [contenteditable='true']"));
}

export default {
  async mount(el, ctx) {
    const t = ctx.t;
    const state = {
      tab: ctx.query.state === "draft" ? "draft" : "active",
      data: null,
      items: [],
      counts: {},
      query: "",
      shown: PAGE,
      loading: true,
      error: null,
      setup: null, // "keys" | "connect" | null
      selected: null,
      pending: new Map(), // listing_id -> [tag, ...] picked with "+ Ekle"
      keyword: "",
      research: null,
      researchLoading: false,
      researchError: null,
      revealed: false, // the audit list has had its first (slow) count-up
      slowReveal: false,
    };
    const views = new Map(); // listing_id -> {el, ring, actionSlot, item}
    // Research answers, shared by the right card and the dialog: "<listing_id>|<keyword>" -> Promise.
    const researchCache = new Map();
    let auditCtl = null;
    let auditSeq = 0;
    let researchSeq = 0;
    ctx.signal.addEventListener("abort", () => {
      if (auditCtl) auditCtl.abort();
    });

    // ------------------------------------------------------------ header actions

    // The video's header holds "Yeniden tara" (and the shell's bell), nothing else. The
    // list's other controls wait behind one button in the Denetim card's head (openTools).
    const rescanBtn = button({
      label: t("rescan"),
      icon: "refresh",
      variant: "secondary",
      class: "seo-rescan",
      autoLoading: true,
      onClick: () => loadAudit({ refresh: true, announce: true }),
    });
    ctx.setHeader({ actions: [rescanBtn] });

    // ------------------------------------------------------------ left card: Denetim

    const auditSub = h("p", { class: "card-sub" }, t("audit.scanning"));
    // "● 80+ iyi": the word hides in narrow windows, the range stays.
    const legendItem = (tone, text) => {
      const [range, ...word] = text.split(" ");
      return h(
        "span",
        { class: "seo-legend-item", title: text },
        h("span", { class: cx("dot", `tone-${tone}`) }),
        h("span", null, range, word.length ? h("span", { class: "seo-legend-word" }, ` ${word.join(" ")}`) : null),
      );
    };
    const legend = h(
      "div",
      { class: "seo-legend", role: "note", "aria-label": t("legend.label") },
      legendItem("success", t("legend.good")),
      legendItem("warning", t("legend.fair")),
      legendItem("danger", t("legend.poor")),
    );

    // The list's controls, in a popover under one button after the legend: the Aktif /
    // Taslaklar switch, the list search ("/" opens it), the shop-wide notes (a dot on
    // the button while there are any) and the CSV report. The nodes live on between
    // openings, so a choice or a query stays as it was.
    const tabsCtl = tabs({
      items: tabItems(),
      value: state.tab,
      size: "sm",
      ariaLabel: t("tabs.label"),
      onChange: (id) => {
        state.tab = id;
        state.shown = PAGE;
        ctx.setQuery({ state: id === "active" ? null : id });
        loadAudit();
      },
    });
    tabsCtl.el.classList.add("seo-tabs");
    const listSearch = searchInput({
      placeholder: t("search"),
      shortcut: "/",
      debounce: 120,
      class: "seo-search",
      onInput: (v) => {
        state.query = v;
        state.shown = PAGE;
        renderList(false);
        renderToolsBtn();
      },
    });
    const exportBtn = button({
      label: t("export"),
      icon: "download",
      variant: "ghost",
      size: "sm",
      class: "seo-tools-btn",
      onClick: () => {
        if (toolsPop) toolsPop.close();
        // api.download: an error becomes a toast, not a saved file.
        ctx.api
          .download("/api/seo/export.csv", { params: { state: state.tab }, filename: `seo-${state.tab}.csv`, signal: ctx.signal })
          .catch((err) => {
            if (!ctx.api.isAbort(err)) ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
          });
      },
    });
    const toolsShop = h("div", { class: "seo-tools-shop" });
    const toolsDot = h("span", { class: "seo-head-dot", "aria-hidden": "true", hidden: true });
    const toolsBtn = iconButton({
      icon: "more",
      title: t("tools.title"),
      variant: "ghost",
      size: "sm",
      class: "seo-head-btn",
      onClick: () => openTools(),
    });
    toolsBtn.append(toolsDot);
    toolsBtn.setAttribute("aria-haspopup", "dialog");
    toolsBtn.setAttribute("aria-expanded", "false");
    let toolsPop = null;

    function openTools(focusSearch = false) {
      if (toolsPop) {
        if (focusSearch) listSearch.input.focus();
        else toolsPop.close();
        return;
      }
      renderToolsShop();
      const pop = popover(
        toolsBtn,
        h(
          "div",
          { class: "seo-tools" },
          h("p", { class: "seo-pop-label" }, t("tabs.label")),
          tabsCtl.el,
          listSearch,
          toolsShop,
          h("div", { class: "seo-tools-data" }, exportBtn),
        ),
        { placement: "bottom-end", width: 320, class: "seo-pop seo-tools-pop", role: "dialog", onClose: () => (toolsPop = null) },
      );
      if (!pop) return;
      toolsPop = pop;
      pop.el.setAttribute("aria-label", t("tools.title"));
      if (focusSearch) listSearch.input.focus();
      else pop.el.focus();
    }

    /** The button's dot and name: shop-wide notes are waiting; a draft list or a query is on. */
    function renderToolsBtn() {
      const issues = shopIssues();
      const worst = issues.some((i) => i.severity === "error") ? "danger" : "warning";
      toolsDot.hidden = !issues.length;
      toolsDot.className = cx("seo-head-dot", `tone-${worst}`);
      toolsBtn.classList.toggle("is-active", state.tab === "draft" || !!state.query.trim());
      const label = issues.length ? t("tools.title_issues", { n: issues.length }) : t("tools.title");
      toolsBtn.title = label;
      toolsBtn.setAttribute("aria-label", label);
      toolsBtn.hidden = !!state.setup;
    }

    const onSlash = (e) => {
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
      if (!toolsBtn.isConnected || toolsBtn.offsetParent === null) return;
      if (isTyping(e.target) || document.querySelector(".modal-backdrop")) return;
      e.preventDefault();
      openTools(true);
    };
    document.addEventListener("keydown", onSlash);

    const headTools = h("div", { class: "seo-head-tools" }, legend, toolsBtn);
    const listNote = h("div", { class: "seo-list-note" });
    const listEl = h("div", { class: "seo-list", role: "list", "aria-label": t("list.label"), "aria-busy": "true" });
    const auditCard = h(
      "section",
      { class: "card seo-card seo-audit", "aria-label": t("audit.title") },
      h(
        "header",
        { class: "card-head" },
        h("span", { class: "icon-tile tone-accent" }, icon("target", { size: 16 })),
        h("div", { class: "card-titles" }, h("h2", { class: "card-title" }, t("audit.title")), auditSub),
        headTools,
      ),
      listNote,
      listEl,
    );

    // ------------------------------------------------------------ right card: research

    const researchSub = h("p", { class: "card-sub" }, t("research.sub", { n: SAMPLE }));
    const kwInput = textInput({
      icon: "search",
      size: "lg",
      class: "seo-kw",
      placeholder: t("research.placeholder"),
      ariaLabel: t("research.input_label"),
      onEnter: (v) => runResearch(v),
    });
    // A 7-day cached answer says so and can be fetched again: a small button at the end
    // of the keyword field (textInput has no slot for one, so it joins the field's box).
    const kwTail = h("span", { class: "seo-kw-tail" });
    kwInput.append(kwTail);
    const explainEl = h("div", { class: "seo-explain" });
    const hintEl = h("div", { class: "seo-hint" });
    const tagsEl = h("div", { class: "seo-tags", role: "list", "aria-label": t("research.title") });
    const marketEl = h("div", { class: "seo-market" });
    const pendingEl = h("div", { class: "seo-pending" });
    const researchCard = h(
      "section",
      { class: "card seo-card seo-research", "aria-label": t("research.title") },
      h(
        "header",
        { class: "card-head" },
        h("span", { class: "icon-tile tone-accent" }, icon("search", { size: 16 })),
        h("div", { class: "card-titles" }, h("h2", { class: "card-title" }, t("research.title")), researchSub),
      ),
      kwInput,
      explainEl,
      marketEl,
      hintEl,
      tagsEl,
      h("footer", { class: "seo-foot" }, pendingEl, infoNote({ icon: "info", tone: "neutral", text: t("research.note") })),
    );

    el.append(h("div", { class: "seo-grid" }, auditCard, researchCard));

    // ------------------------------------------------------------ helpers

    function itemById(id) {
      return state.items.find((it) => it.listing_id === id) || null;
    }
    function pendingFor(id) {
      return state.pending.get(id) || [];
    }
    function freeSlots(item) {
      return Math.max(0, (item.fix ? item.fix.free_slots : MAX_TAGS - item.tags.length) - pendingFor(item.listing_id).length);
    }
    function tabItems() {
      return [
        { id: "active", label: t("tab.active"), count: state.counts.active ?? undefined },
        { id: "draft", label: t("tab.draft"), count: state.counts.draft ?? undefined },
      ];
    }
    function issueText(issue, primary = false) {
      const p = issue.params || {};
      if (issue.code === "tags.unused") {
        // Mostly empty: "13 etiketten 4'ü kullanılmış". As the main problem: "4 etiket yeri boş".
        // Otherwise the short "9/13 etiket".
        const used = Number(p.tags_used) || 0;
        if (used <= 6) return t("issue.tags.unused", { tags_used: used, sfx: trSuffix(used) });
        if (primary) return t("issue.tags.unused_slots", { n: p.free_slots ?? MAX_TAGS - used });
        return t("issue.tags.unused_short", { tags_used: used });
      }
      if (issue.code === "tags.too_long") return t("issue.tags.too_long", { n: p.count ?? 1 });
      const key = `issue.${issue.code}`;
      return t.has(key) ? t(key, p) : issue.code;
    }

    // The video: the first chip of a listing is red, every other one amber.
    function issueChip(issue, primary) {
      const tone = primary ? "danger" : "warning";
      const text = issueText(issue, primary);
      return h(
        "span",
        { class: cx("seo-chip", `tone-${tone}`), title: text, dataset: { code: issue.code } },
        tone === "danger" ? icon("alert", { size: 13, strokeWidth: 2 }) : null,
        h("span", null, text),
      );
    }

    /** The heaviest problem of each area first (title, tags, description, ...), then the rest. */
    function chipOrder(issues) {
      const first = [];
      const rest = [];
      const areas = new Set();
      for (const iss of issues) {
        const area = iss.code.split(".")[0];
        if (iss.severity !== "info" && !areas.has(area)) {
          areas.add(area);
          first.push(iss);
        } else rest.push(iss);
      }
      return [...first, ...rest];
    }

    function morePill(rest) {
      return h("span", { class: "seo-chip tone-muted seo-chip-more", title: rest.map((iss) => issueText(iss)).join("\n") }, t("more_issues", { n: rest.length }));
    }

    function issueChips(item, limit = CHIP_LIMIT) {
      const issues = chipOrder(item.issues || []);
      if (!issues.length || item.ready) {
        // A "Hazır" row reads green: its minor notes wait in the "+N" pill's tooltip
        // (and in the dialog the Hazır button opens).
        const ok = h("span", { class: "seo-chip tone-success" }, icon("check", { size: 13, strokeWidth: 2.4 }), h("span", null, t(issues.length ? "ready_minor" : "no_issues")));
        return issues.length ? [ok, morePill(issues)] : [ok];
      }
      // The first chip is the listing's main problem: red, like an error.
      const shown = issues.slice(0, limit);
      const chips = shown.map((iss, i) => issueChip(iss, i === 0 && !item.ready && iss.severity !== "info"));
      const rest = issues.slice(limit);
      if (rest.length) chips.push(morePill(rest));
      return chips;
    }

    // One line of whole chips, as in the video: when a row is too narrow for all of them,
    // the last ones join the "+N" pill (its tooltip lists them) instead of being cut
    // mid-word. Checked again whenever the row's width changes. (Below 1360 px the CSS
    // lets the chips wrap, so they always fit there.)
    const chipRows = new WeakMap(); // .seo-chips element -> {item, width}
    const chipObserver =
      typeof ResizeObserver === "function"
        ? new ResizeObserver((entries) => {
            for (const entry of entries) {
              const info = chipRows.get(entry.target);
              const width = Math.round(entry.contentRect.width);
              if (!info || !width || width === info.width) continue;
              info.width = width;
              fitChips(entry.target, info.item);
            }
          })
        : null;

    function fitChips(box, item) {
      const count = (item.issues || []).length;
      box.classList.remove("is-tight");
      if (!count || item.ready) return; // the green chip (and "+N") only
      for (let n = Math.min(CHIP_LIMIT, count); n >= 1; n--) {
        mount(box, issueChips(item, n));
        if (box.scrollWidth <= box.clientWidth + 1) return;
      }
      box.classList.add("is-tight"); // even one chip and "+N" are too wide: the chip shortens
    }

    /**
     * Count a ring up from `from` to `to` (ease-out). It wears the final score's colour
     * from the start, as in the video, so a good listing never flashes red on the way.
     */
    function animateRing(ring, from, to, { delay = 0, dur = 750 } = {}) {
      const tone = toneOf(to);
      if (REDUCED_MOTION || from === to) {
        setRing(ring, to, tone);
        return;
      }
      setRing(ring, from, tone);
      ring.classList.add("is-counting");
      let start = null;
      const step = (now) => {
        if (!ring.isConnected && start !== null) return;
        if (start === null) start = now + delay;
        const k = Math.min(1, Math.max(0, (now - start) / dur));
        const eased = 1 - (1 - k) ** 3;
        setRing(ring, Math.round(from + (to - from) * eased), tone);
        if (k < 1) requestAnimationFrame(step);
        else ring.classList.remove("is-counting", "is-revealing");
      };
      requestAnimationFrame(step);
    }

    // ------------------------------------------------------------ the audit list

    function renderAction(view) {
      const { item } = view;
      const picked = pendingFor(item.listing_id).length;
      const selected = state.selected === item.listing_id;
      let btn;
      if (item.ready && !picked) {
        btn = button({
          label: t("ready"),
          icon: "check",
          variant: "ghost",
          class: "seo-ready-btn",
          title: t("ready_hint"),
          onClick: () => openFix(item.listing_id),
        });
      } else {
        btn = button({
          label: t("fix"),
          icon: "loader",
          variant: "secondary",
          class: cx("seo-fix-btn", selected && "is-selected"),
          count: picked || undefined,
          onClick: () => openFix(item.listing_id),
        });
      }
      mount(view.actionSlot, btn);
    }

    function itemView(item, index, animate) {
      const ring = scoreRing({ score: animate ? 0 : item.score, size: RING, stroke: 7, fontSize: RING_FONT });
      ring.classList.add("seo-ring");
      // The first load of the page counts up slowly, as in the video (each row a little
      // later and longer); rescans and tab switches stay quick.
      const slow = animate && state.slowReveal;
      const ringDelay = Math.min(index, 8) * (slow ? 150 : 60);
      const ringDur = slow ? Math.min(2600, 1400 + index * 300) : 750;
      const enter = animate && !REDUCED_MOTION;
      // Chips and the Düzelt button fade in while the ring is a third of the way up.
      const timing = enter
        ? { "--d": `${ringDelay + (slow ? Math.round(ringDur * 0.35) : 380)}ms`, "--dur": `${slow ? Math.round(ringDur * 0.45) : 320}ms` }
        : null;
      const actionSlot = h("div", { class: cx("seo-item-action", enter && "is-entering"), style: timing });
      const chipsEl = h("div", { class: cx("seo-chips", enter && "is-entering"), style: timing }, issueChips(item));
      if (enter) {
        ring.classList.add("is-revealing");
        ring.style.setProperty("--rd", `${ringDelay}ms`);
        ring.style.setProperty("--rdur", `${ringDur}ms`);
      }
      if (chipObserver) {
        chipRows.set(chipsEl, { item, width: 0 });
        chipObserver.observe(chipsEl);
      }
      const row = h(
        "div",
        {
          class: cx("seo-item", state.selected === item.listing_id && "is-selected"),
          role: "listitem",
          tabindex: "0",
          "aria-current": state.selected === item.listing_id ? "true" : undefined,
          dataset: { id: String(item.listing_id) },
          onClick: (e) => {
            if (e.target.closest("button, a")) return;
            select(item.listing_id);
          },
          onKeydown: (e) => {
            if (e.target !== row) return;
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              select(item.listing_id);
            }
          },
        },
        ring,
        h(
          "div",
          { class: "seo-item-main" },
          h("div", { class: "seo-item-head" }, h("h3", { class: "seo-item-title ellipsis", title: item.title }, item.title || "–"), actionSlot),
          chipsEl,
        ),
      );
      const view = { el: row, ring, actionSlot, item };
      renderAction(view);
      if (animate) animateRing(ring, 0, item.score, { delay: ringDelay, dur: ringDur });
      return view;
    }

    function refreshView(id) {
      const view = views.get(id);
      if (!view) return;
      const selected = state.selected === id;
      view.el.classList.toggle("is-selected", selected);
      if (selected) view.el.setAttribute("aria-current", "true");
      else view.el.removeAttribute("aria-current");
      renderAction(view);
    }

    function skeletonItem() {
      return h(
        "div",
        { class: "seo-item is-skeleton", "aria-hidden": "true" },
        scoreRing({ score: null, size: RING, stroke: 7, fontSize: RING_FONT }),
        h(
          "div",
          { class: "seo-item-main" },
          h(
            "div",
            { class: "seo-item-head" },
            h("span", { class: "skeleton seo-skel-title" }),
            h("div", { class: "seo-item-action" }, button({ label: t("fix"), icon: "loader", variant: "ghost", disabled: true, class: "seo-fix-btn" })),
          ),
          h("div", { class: "seo-chips" }, h("span", { class: "skeleton seo-skel-chip" }), h("span", { class: "skeleton seo-skel-chip is-short" })),
        ),
      );
    }

    function visibleItems() {
      const q = lower(state.query.trim());
      if (!q) return state.items;
      return state.items.filter((it) => lower(it.title).includes(q) || String(it.listing_id).includes(q));
    }

    function goSetupButton() {
      return button({ label: t("setup.action"), icon: "store", variant: "primary", onClick: () => ctx.navigate("/kurulum/magaza") });
    }

    function renderList(animate) {
      views.clear();
      listEl.setAttribute("aria-busy", state.loading ? "true" : "false");
      mount(listNote);
      if (state.loading) {
        mount(listEl, [0, 1, 2, 3].map(skeletonItem));
        return;
      }
      if (state.setup) {
        const keys = state.setup === "keys";
        mount(
          listEl,
          emptyState({
            icon: keys ? "lock" : "link",
            title: t(keys ? "setup.keys.title" : "setup.connect.title"),
            message: t(keys ? "setup.keys.msg" : "setup.connect.msg"),
            action: goSetupButton(),
          }),
        );
        return;
      }
      if (state.error) {
        mount(
          listEl,
          emptyState({
            icon: "alert",
            title: t("audit.load_error"),
            message: ctx.api.errorText(state.error, t),
            action: button({ label: t("common.retry"), icon: "refresh", onClick: () => loadAudit() }),
          }),
        );
        return;
      }
      if (state.data && state.data.truncated) {
        mount(listNote, infoNote({ icon: "info", tone: "warning", text: t("audit.truncated", { n: state.items.length }) }));
      }
      const q = state.query.trim();
      if (q && state.items.length) {
        // The search field is in the tools popover: a filtered list says so, with a way out.
        listNote.append(
          h(
            "div",
            { class: "seo-query", role: "status" },
            icon("search", { size: 13 }),
            h("span", { class: "ellipsis" }, t("search.active", { q, n: visibleItems().length })),
            button({
              label: t("common.clear"),
              icon: "x",
              variant: "ghost",
              size: "sm",
              class: "seo-query-clear",
              onClick: () => {
                listSearch.value = "";
                state.query = "";
                state.shown = PAGE;
                renderList(false);
                renderToolsBtn();
              },
            }),
          ),
        );
      }
      if (!state.items.length) {
        mount(listEl, emptyState({ icon: "search", title: t(state.tab === "draft" ? "empty.draft" : "empty.active"), message: t("empty.msg") }));
        return;
      }
      const items = visibleItems();
      if (!items.length) {
        mount(listEl, emptyState({ icon: "search", compact: true, title: t("empty.search", { q: state.query.trim() }) }));
        return;
      }
      const nodes = items.slice(0, state.shown).map((item, i) => {
        const view = itemView(item, i, animate);
        views.set(item.listing_id, view);
        return view.el;
      });
      const rest = items.length - state.shown;
      if (rest > 0) {
        nodes.push(
          button({
            label: t("show_more", { n: Math.min(rest, PAGE) }),
            icon: "chevron-down",
            variant: "ghost",
            class: "seo-more",
            onClick: () => {
              state.shown += PAGE;
              renderList(false);
            },
          }),
        );
      }
      mount(listEl, nodes);
    }

    /** The shop-wide notes of the list on screen (none while it loads). */
    function shopIssues() {
      return (!state.loading && state.data && state.data.shop_issues) || [];
    }

    /** The tools popover's shop-wide part: the notes and the tags most listings share. */
    function renderToolsShop() {
      const issues = shopIssues();
      if (!issues.length) {
        mount(toolsShop);
        return;
      }
      const overlap = issues.find((i) => i.code === "shop.tag_overlap");
      const top = overlap ? overlap.params.top || [] : [];
      mount(
        toolsShop,
        h("p", { class: "seo-pop-title" }, t("shop.title")),
        h("p", { class: "seo-pop-sub" }, t("shop.sub")),
        issues.map((i) =>
          h(
            "div",
            { class: cx("seo-pop-issue", `tone-${i.severity === "error" ? "danger" : "warning"}`) },
            icon("alert", { size: 14 }),
            // n picks the "_one" wording for a single tag or listing
            h("p", null, t.has(`shop_issue.${i.code}`) ? t(`shop_issue.${i.code}`, { ...i.params, n: i.params.tags ?? i.params.crowded }) : i.code),
          ),
        ),
        top.length
          ? [
              h("p", { class: "seo-pop-label" }, t("shop.common_tags")),
              h(
                "div",
                { class: "seo-pop-tags" },
                top.map((row) => tagChip({ text: row.tag, count: t("shop.tag_count", { count: row.count, listings: overlap.params.listings }) })),
              ),
            ]
          : null,
      );
    }

    function renderSub() {
      const d = state.data;
      // The Aktif / Taslaklar switch is in the tools popover: the line says which list it is.
      const drafts = state.tab === "draft";
      if (state.loading) auditSub.textContent = t("audit.scanning");
      else if (d && !state.setup && !state.error) {
        auditSub.textContent = d.needs_fix
          ? t(drafts ? "audit.sub_draft" : "audit.sub", { n: d.scanned, m: d.needs_fix })
          : t(drafts ? "audit.sub_ready_draft" : "audit.sub_ready", { n: d.scanned });
      } else auditSub.textContent = "";
      auditSub.title = auditSub.textContent; // the line ends in "…" in a narrow card
    }

    function renderAudit(animate) {
      renderSub();
      tabsCtl.update(tabItems(), state.tab);
      if (state.setup && toolsPop) toolsPop.close();
      renderToolsBtn();
      if (toolsPop) {
        renderToolsShop();
        toolsPop.reposition();
      }
      renderList(animate);
    }

    // ------------------------------------------------------------ loading the audit

    async function loadAudit({ refresh = false, announce = false } = {}) {
      const seq = ++auditSeq;
      if (auditCtl) auditCtl.abort();
      auditCtl = new AbortController();
      // No keys or no sign-in yet: say so without asking the server for a 409.
      const status = ctx.status();
      const missing = status && (status.state === "keys" ? "keys" : status.state === "disconnected" ? "connect" : null);
      if (missing) {
        state.loading = false;
        state.error = null;
        state.data = null;
        state.items = [];
        state.setup = missing;
        renderAudit(false);
        renderResearch();
        return;
      }
      state.loading = true;
      state.error = null;
      renderAudit(false);
      try {
        const data = await ctx.api.get("/api/seo/audit", { state: state.tab, refresh: refresh ? 1 : undefined }, { signal: auditCtl.signal });
        if (seq !== auditSeq) return;
        state.data = data;
        state.items = data.items || [];
        state.counts = { ...state.counts, ...(data.counts || {}) };
        state.setup = null;
        state.loading = false;
        if (!state.items.some((it) => it.listing_id === state.selected)) state.selected = null;
        state.slowReveal = !state.revealed && state.items.length > 0;
        renderAudit(true);
        if (state.items.length) state.revealed = true;
        state.slowReveal = false;
        if (announce) ctx.toast({ tone: "success", title: t("rescanned"), timeout: 2500 });
        if (state.selected === null) {
          const first = state.items.find((it) => !it.ready) || state.items[0];
          if (first) select(first.listing_id);
          else renderResearch();
        } else {
          renderResearch();
        }
      } catch (err) {
        if (ctx.api.isAbort(err) || seq !== auditSeq) return;
        state.loading = false;
        state.data = null;
        state.items = [];
        if (err.code === "setup_needed") {
          state.setup = err.params && err.params.step === "connect" ? "connect" : "keys";
        } else {
          state.error = err;
        }
        renderAudit(false);
        renderResearch();
      }
    }

    // ------------------------------------------------------------ selection + research

    function select(id) {
      if (state.selected === id) return;
      const prev = state.selected;
      state.selected = id;
      refreshView(prev);
      refreshView(id);
      const item = itemById(id);
      if (item && item.concept) {
        kwInput.value = item.concept;
        runResearch(item.concept);
      } else {
        renderResearch();
      }
    }

    /** GET /api/seo/research once per (listing, keyword) while the page is open. */
    function fetchResearch(keyword, listingId, refresh = false) {
      const key = `${listingId ?? ""}|${keyword.toLowerCase()}`;
      if (!refresh && researchCache.has(key)) return researchCache.get(key);
      const promise = ctx.api.get(
        "/api/seo/research",
        { keyword, listing_id: listingId ?? undefined, refresh: refresh ? 1 : undefined },
        { signal: ctx.signal },
      );
      researchCache.set(key, promise);
      promise.catch(() => {
        if (researchCache.get(key) === promise) researchCache.delete(key);
      });
      return promise;
    }

    function forgetResearch(listingId) {
      for (const key of [...researchCache.keys()]) if (key.startsWith(`${listingId}|`)) researchCache.delete(key);
    }

    async function runResearch(keyword, { refresh = false } = {}) {
      const kw = cleanTag(keyword);
      state.keyword = kw;
      const seq = ++researchSeq;
      if (!kw) {
        state.research = null;
        state.researchError = null;
        state.researchLoading = false;
        renderResearch();
        return;
      }
      state.researchLoading = true;
      state.researchError = null;
      renderResearch();
      try {
        const data = await fetchResearch(kw, state.selected, refresh);
        if (seq !== researchSeq) return;
        state.research = data;
      } catch (err) {
        if (ctx.api.isAbort(err) || seq !== researchSeq) return;
        state.research = null;
        state.researchError = err;
      }
      state.researchLoading = false;
      renderResearch();
    }

    function togglePending(tag) {
      const item = itemById(state.selected);
      if (!item) return;
      const list = pendingFor(item.listing_id).slice();
      const at = list.indexOf(tag);
      if (at >= 0) list.splice(at, 1);
      else if (freeSlots(item) > 0) list.push(tag);
      else return;
      if (list.length) state.pending.set(item.listing_id, list);
      else state.pending.delete(item.listing_id);
      const hadFocus = tagsEl.contains(document.activeElement);
      refreshView(item.listing_id);
      renderResearch();
      if (hadFocus) {
        const again = tagsEl.querySelector(`.seo-add[data-tag="${CSS.escape(tag)}"]`);
        if (again && !again.disabled) again.focus();
      }
    }

    /** Count a research row's share up from 0 (the video's reveal). */
    function countShare(el, share, done) {
      let start = null;
      const step = (now) => {
        if (!el.isConnected) return;
        if (start === null) start = now;
        const k = Math.min(1, (now - start) / COUNT_MS);
        el.textContent = percent(share * (1 - (1 - k) ** 3));
        if (k < 1) requestAnimationFrame(step);
        else done();
      };
      requestAnimationFrame(step);
    }

    /** reveal: the row's place in a new answer (it fades in, its % and bar grow), or -1. */
    function tagRow(row, sampled, item, reveal = -1) {
      const picked = item ? pendingFor(item.listing_id) : [];
      const added = picked.includes(row.tag);
      const room = item ? freeSlots(item) : 0;
      const value = Math.round(row.share * 1000) / 10;
      const bar = progressBar({ value: reveal >= 0 ? 0 : value, max: 100, tone: "accent", size: "lg" });
      const btn = button({
        label: added ? t("research.added") : t("research.add"),
        icon: added ? "check" : "plus",
        variant: "secondary",
        class: cx("seo-add", added && "is-added"),
        disabled: !item || (!added && room <= 0),
        title: item ? t("research.add_title", { tag: row.tag, listing: item.title }) : t("research.pick_listing"),
        onClick: () => togglePending(row.tag),
      });
      btn.setAttribute("aria-pressed", added ? "true" : "false");
      btn.dataset.tag = row.tag;
      const final = percent(row.share);
      const shareEl = h("span", { class: "seo-tag-share num", title: t("research.share_title", { count: row.count, sampled }) }, reveal >= 0 ? percent(0) : final);
      // While the number counts, screen readers get the final one.
      const srShare = reveal >= 0 ? h("span", { class: "sr-only" }, final) : null;
      if (srShare) shareEl.setAttribute("aria-hidden", "true");
      const el = h(
        "div",
        { class: cx("seo-tag-row", reveal >= 0 && "is-entering"), role: "listitem", style: reveal >= 0 ? { "--d": `${reveal * REVEAL_GAP}ms` } : null },
        h(
          "div",
          { class: "seo-tag-main" },
          h(
            "div",
            { class: "seo-tag-line" },
            h("span", { class: "seo-tag-icon" }, icon("tag", { size: 15 })),
            h("span", { class: "seo-tag-name ellipsis", title: row.tag }, row.tag),
            shareEl,
            srShare,
          ),
          bar.el,
        ),
        btn,
      );
      if (reveal >= 0) {
        setTimeout(() => {
          if (!el.isConnected) return;
          requestAnimationFrame(() => {
            void bar.el.offsetWidth; // the 0 width is laid out, so the bar grows from it
            bar.update(value);
            countShare(shareEl, row.share, () => {
              shareEl.textContent = final;
              shareEl.removeAttribute("aria-hidden");
              if (srShare) srShare.remove();
            });
          });
        }, reveal * REVEAL_GAP);
      }
      return el;
    }

    function skeletonRow() {
      return h(
        "div",
        { class: "seo-tag-row is-skeleton", "aria-hidden": "true" },
        h("div", { class: "seo-tag-main" }, h("span", { class: "skeleton", style: { height: 14, width: "42%" } }), h("span", { class: "skeleton", style: { height: 7, width: "100%", borderRadius: 999 } })),
        h("span", { class: "skeleton", style: { height: 32, width: 72, borderRadius: 10 } }),
      );
    }

    const revealedResearch = new WeakSet(); // research answers whose rows already arrived

    // Rakip analizi beyond the tags: what the ranking listings charge, how liked they are,
    // the title phrases they share and the five most favourited (research.market).
    function renderMarket(data) {
      const m = data && data.market;
      if (!m || !data.sampled) {
        mount(marketEl);
        return;
      }
      const cur = m.currency || "USD";
      const price = m.price && m.price.median !== null && m.price.median !== undefined
        ? h("p", { class: "seo-market-line" }, h("strong", null, t("market.price")), ` ${money(m.price.min, cur)} – ${money(m.price.max, cur)} · ${t("market.median")} ${money(m.price.median, cur)}`, m.price.sample && m.price.sample < data.sampled ? h("span", { class: "muted" }, ` (${t("market.of", { n: m.price.sample, total: data.sampled })})`) : null)
        : null;
      const fav = m.median_favorers !== null && m.median_favorers !== undefined ? h("p", { class: "seo-market-line" }, h("strong", null, t("market.favorers")), ` ${t("market.median")} ${Math.round(m.median_favorers)}`) : null;
      const phrases = (m.phrases || []).length
        ? h("div", { class: "seo-market-block" }, h("strong", null, t("market.phrases")), h("div", { class: "seo-market-chips" }, m.phrases.map((p) => h("span", { class: "badge tone-neutral", title: t("market.phrase_count", { count: p.count, sampled: data.sampled }) }, `${p.phrase} · ${p.count}`))))
        : null;
      const top = (m.top || []).length
        ? h("div", { class: "seo-market-block" }, h("strong", null, t("market.top")), h("ol", { class: "seo-market-top" }, m.top.map((row) => h("li", null, row.url ? h("a", { href: row.url, target: "_blank", rel: "noopener noreferrer", "data-external": "", class: "ellipsis", title: row.title }, row.title) : h("span", { class: "ellipsis" }, row.title), h("span", { class: "muted num" }, `${row.price ? money(Number(row.price), row.currency || cur) : "–"} · ♥ ${row.favorers}`)))))
        : null;
      mount(marketEl, price, fav, phrases, top);
    }

    function renderResearch() {
      const data = state.research;
      renderMarket(data);
      const item = itemById(state.selected);
      const sampled = data ? data.sampled : SAMPLE;
      researchSub.textContent = t("research.sub", { n: data && data.sampled ? data.sampled : SAMPLE });
      kwInput.input.disabled = state.setup === "keys";
      mount(hintEl);
      mount(pendingEl);

      if (state.setup === "keys") {
        mount(explainEl);
        mount(tagsEl, emptyState({ icon: "lock", compact: true, title: t("setup.research"), action: goSetupButton() }));
        return;
      }

      // The explanation line: the sentence alone, as in the video. The top 5 are on the
      // card; the Düzelt dialog of the chosen listing lists every suggestion.
      if (state.keyword && ((data && data.sampled) || state.researchLoading)) {
        const excluding = data ? data.listing_id !== null && data.listing_id !== undefined : !!item;
        mount(explainEl, h("p", null, t(excluding ? "research.explain" : "research.explain_all", { n: sampled || SAMPLE })));
      } else {
        mount(explainEl);
      }
      const cachedLabel = `${t("research.cached")} · ${t("research.refresh")}`;
      mount(
        kwTail,
        data && data.cached && !state.researchLoading && state.keyword
          ? h(
              "button",
              {
                type: "button",
                class: "seo-cached",
                title: cachedLabel,
                "aria-label": cachedLabel,
                onClick: () => runResearch(state.keyword, { refresh: true }),
              },
              icon("history", { size: 14 }),
            )
          : null,
      );

      if (state.researchLoading) {
        tagsEl.setAttribute("aria-busy", "true");
        mount(tagsEl, Array.from({ length: TAG_ROWS }, skeletonRow));
        return;
      }
      tagsEl.setAttribute("aria-busy", "false");
      if (state.researchError) {
        mount(
          tagsEl,
          h(
            "div",
            { class: "seo-tags-msg" },
            infoNote({
              icon: "alert",
              tone: "danger",
              title: t("research.error"),
              text: ctx.api.errorText(state.researchError, t),
              action: button({ label: t("common.retry"), size: "sm", icon: "refresh", onClick: () => runResearch(state.keyword) }),
            }),
          ),
        );
        return;
      }
      if (!state.keyword || !data) {
        // While the audit loads, the first listing's keyword is on its way: stay empty.
        mount(tagsEl, state.loading && !state.setup ? null : emptyState({ icon: "search", compact: true, message: t("research.idle") }));
        return;
      }
      if (!data.tags.length) {
        mount(
          tagsEl,
          emptyState({
            icon: "tag",
            compact: true,
            title: data.sampled ? t("research.nothing_new") : t("research.empty"),
            message: data.sampled ? null : t("research.empty_msg"),
          }),
        );
        return;
      }

      // who the "+ Ekle" buttons add to
      if (!item) {
        mount(hintEl, infoNote({ icon: "info", tone: "neutral", text: t("research.pick_listing") }));
      } else if (freeSlots(item) <= 0 && !pendingFor(item.listing_id).length) {
        mount(
          hintEl,
          infoNote({
            icon: "alert",
            tone: "warning",
            text: t("research.full", { listing: item.title }),
            action: button({ label: t("research.make_room"), size: "sm", variant: "secondary", onClick: () => openFix(item.listing_id) }),
          }),
        );
      }
      // A new answer's rows arrive one by one, their % and bars growing from 0 (the
      // video); an answer drawn again (after "+ Ekle", or its listing picked again)
      // stays still.
      const fresh = !revealedResearch.has(data) && !REDUCED_MOTION;
      revealedResearch.add(data);
      mount(tagsEl, data.tags.slice(0, TAG_ROWS).map((row, i) => tagRow(row, data.sampled, item, fresh ? i : -1)));

      const picked = item ? pendingFor(item.listing_id) : [];
      if (picked.length) {
        mount(
          pendingEl,
          h(
            "div",
            { class: "seo-pending-row" },
            icon("check", { size: 14, strokeWidth: 2.2 }),
            h("span", { class: "ellipsis" }, t("research.pending", { n: picked.length, listing: item.title })),
            button({ label: t("research.apply"), size: "sm", variant: "secondary", icon: "loader", class: "seo-pending-apply", onClick: () => openFix(item.listing_id) }),
          ),
        );
      }
    }

    // ------------------------------------------------------------ the fix dialog

    function openFix(id) {
      const item = itemById(id);
      if (!item) return;
      if (state.selected !== id) select(id);
      const fix = item.fix || { keep: item.tags, remove: [], free_slots: MAX_TAGS - item.tags.length, title: null, materials: false, manual: [] };

      // One row per existing tag: kept (removable by hand) or proposed for removal.
      const keepLeft = new Set(fix.keep);
      const removals = fix.remove.slice();
      const rows = [];
      for (const raw of item.tags) {
        const tag = cleanTag(raw);
        if (!tag) continue;
        if (keepLeft.has(tag)) {
          keepLeft.delete(tag);
          rows.push({ tag, kind: "keep", dropped: false });
        } else {
          const at = removals.findIndex((r) => r.tag === tag);
          const r = at >= 0 ? removals.splice(at, 1)[0] : { tag, reason: "duplicate", of: "" };
          rows.push({ tag, kind: "remove", reason: r.reason, of: r.of || "", checked: true, locked: r.reason !== "near_duplicate" });
        }
      }
      const adds = pendingFor(id).map((tag) => ({ tag, share: null, checked: true, picked: true }));
      let suggestions = null; // research rows for this listing
      let suggestionsError = null;
      // A title change is offered, never pre-ticked: the seller reads it first.
      let titleOn = false;
      const original = rows.map((r) => r.tag);

      const finalTags = () => [
        ...rows.filter((r) => (r.kind === "keep" ? !r.dropped : !r.checked)).map((r) => r.tag),
        ...adds.filter((a) => a.checked).map((a) => a.tag),
      ];
      const room = () => MAX_TAGS - finalTags().length;
      const has = (tag) => finalTags().some((x) => lower(x) === lower(tag));

      // --- static parts
      const countEl = h("span", { class: "seo-fix-count num" });
      const removeEl = h("div", { class: "seo-fix-list" });
      const addEl = h("div", { class: "seo-fix-list seo-fix-grid" });
      const addSource = h("p", { class: "seo-fix-source" });
      const resultEl = h("div", { class: "seo-fix-result" });
      const fullEl = h("div");
      const customInput = textInput({
        placeholder: t("fix.custom_placeholder"),
        maxLength: 40,
        size: "sm",
        onEnter: () => addCustom(),
        onInput: () => customField.setError(""),
      });
      const customBtn = button({ label: t("fix.custom_add"), icon: "plus", size: "sm", variant: "secondary", onClick: () => addCustom() });
      const customField = field({ input: h("div", { class: "seo-fix-custom" }, customInput, customBtn) });

      let materialsInput = null;
      let materialsField = null;
      if (fix.materials) {
        materialsInput = textInput({
          placeholder: t("fix.materials_placeholder"),
          onInput: () => {
            materialsField.setError("");
            update();
          },
        });
        materialsField = field({ label: t("fix.materials"), hint: t("fix.materials_hint"), input: materialsInput });
      }

      const cancelBtn = button({ label: t("common.cancel"), variant: "secondary", onClick: () => dlg.close() });
      const applyBtn = button({ label: t("fix.apply"), icon: "loader", variant: "primary", onClick: () => apply() });

      function materials() {
        if (!materialsInput) return [];
        return materialsInput.value
          .split(",")
          .map((m) => cleanTag(m))
          .filter(Boolean);
      }
      function materialsValid(list) {
        return list.length <= MAX_TAGS && list.every((m) => m.length <= 45 && MATERIAL_OK.test(m));
      }
      function tagsChanged() {
        const now = finalTags();
        return now.length !== original.length || now.some((tag, i) => tag !== original[i]);
      }

      function addCustom() {
        const tag = cleanTag(customInput.value);
        if (!tag) return;
        if (tag.length > MAX_TAG_LEN || !TAG_OK.test(tag)) {
          customField.setError(t("fix.custom_invalid"));
          return;
        }
        if (has(tag)) {
          customField.setError(t("fix.custom_exists"));
          return;
        }
        const existing = adds.find((a) => lower(a.tag) === lower(tag));
        if (room() <= 0) {
          customField.setError(t("fix.full"));
          return;
        }
        if (existing) existing.checked = true;
        else adds.push({ tag, share: null, checked: true, picked: true });
        customInput.value = "";
        update();
      }

      function reasonText(r) {
        return t(`fix.reason.${r.reason}`, { of: r.of });
      }

      function renderRemovals() {
        const list = rows.filter((r) => r.kind === "remove");
        if (!list.length) {
          mount(removeEl);
          removeEl.hidden = true;
          return;
        }
        removeEl.hidden = false;
        const left = room();
        mount(
          removeEl,
          h("p", { class: "seo-fix-label" }, t("fix.remove_title")),
          list.map((r) =>
            h(
              "label",
              { class: cx("seo-fix-row", r.checked && "is-removed") },
              checkbox({
                checked: r.checked,
                disabled: r.locked || (r.checked && left <= 0),
                ariaLabel: r.tag,
                onChange: (v) => {
                  r.checked = v;
                  update();
                },
              }),
              h("span", { class: "seo-fix-tag" }, r.tag),
              h("span", { class: "seo-fix-why" }, reasonText(r)),
            ),
          ),
        );
      }

      function renderAdds() {
        const left = room();
        const fromResearch = suggestions ? suggestions.tags : [];
        // research rows not yet in the list (picked ones come first)
        for (const row of fromResearch) {
          if (!adds.some((a) => lower(a.tag) === lower(row.tag))) adds.push({ tag: row.tag, share: row.share, checked: false, picked: false });
          else {
            const a = adds.find((x) => lower(x.tag) === lower(row.tag));
            if (a.share === null) a.share = row.share;
          }
        }
        mount(addSource, suggestions ? t("fix.add_source", { keyword: suggestions.keyword }) : "");
        const kids = [];
        if (!suggestions && !suggestionsError) kids.push(h("p", { class: "seo-fix-muted" }, h("span", { class: "seo-inline-spin" }), t("fix.add_loading")));
        if (suggestionsError) kids.push(h("p", { class: "seo-fix-muted" }, t("fix.add_error", { message: ctx.api.errorText(suggestionsError, t) })));
        if (suggestions && !adds.length) kids.push(h("p", { class: "seo-fix-muted" }, t("fix.add_none")));
        if (adds.some((a) => !a.picked)) kids.push(h("p", { class: "seo-fix-muted seo-fix-pick" }, t("fix.pick_hint")));
        kids.push(
          adds.map((a) =>
            h(
              "label",
              { class: cx("seo-fix-row", a.checked && "is-added") },
              checkbox({
                checked: a.checked,
                disabled: !a.checked && left <= 0,
                ariaLabel: a.tag,
                onChange: (v) => {
                  a.checked = v;
                  update();
                },
              }),
              h("span", { class: "seo-fix-tag" }, a.tag),
              a.share !== null && a.share !== undefined ? h("span", { class: "seo-fix-share num" }, percent(a.share)) : null,
            ),
          ),
        );
        mount(addEl, kids);
      }

      function renderResult() {
        const final = finalTags();
        const chips = final.map((tag) => {
          const isNew = !original.includes(tag);
          return tagChip({
            text: tag,
            tone: isNew ? "accent" : "neutral",
            removable: true,
            onRemove: () => {
              const add = adds.find((a) => a.tag === tag && a.checked);
              if (add) add.checked = false;
              else {
                const row = rows.find((r) => r.tag === tag && (r.kind === "keep" ? !r.dropped : !r.checked));
                if (row && row.kind === "keep") row.dropped = true;
                else if (row) row.checked = true;
              }
              update();
            },
          });
        });
        for (let i = final.length; i < MAX_TAGS; i += 1) chips.push(tagChip({ dashed: true }));
        mount(resultEl, chips);
        countEl.textContent = t("fix.tags_count", { from: original.length, to: final.length });
        mount(fullEl, room() <= 0 && adds.some((a) => !a.checked) ? infoNote({ icon: "info", tone: "neutral", text: t("fix.full") }) : null);
      }

      function update() {
        renderRemovals();
        renderAdds();
        renderResult();
        const mats = materials();
        const changed = tagsChanged() || (titleOn && !!fix.title) || mats.length > 0;
        applyBtn.setDisabled(!changed || finalTags().length === 0);
        applyBtn.setLabel(changed ? t("fix.apply") : t("fix.nothing"));
      }

      // --- title section
      let titleSection = null;
      if (fix.title) {
        const afterEl = h("p", { class: "seo-fix-after" }, h("span", { class: "seo-fix-k" }, t("fix.after")), h("span", null, fix.title.after));
        const beforeEl = h("p", { class: "seo-fix-before" }, h("span", { class: "seo-fix-k" }, t("fix.before")), h("s", null, fix.title.before));
        const box = h("div", { class: cx("seo-fix-title-box", !titleOn && "is-off") }, beforeEl, afterEl);
        const dropped = (fix.title.phrases || fix.title.words || []).map((p) => `“${p}”`);
        titleSection = h(
          "section",
          { class: "seo-fix-sec" },
          h("h3", { class: "seo-fix-h" }, t("fix.title_section")),
          checkbox({
            checked: titleOn,
            label: t("fix.title_check", { words: dropped.join(", ") }),
            onChange: (v) => {
              titleOn = v;
              box.classList.toggle("is-off", !v);
              update();
            },
          }),
          box,
        );
      }

      // --- manual section
      let manualSection = null;
      if (fix.manual && fix.manual.length) {
        const issues = item.issues.filter((i) => fix.manual.includes(i.code));
        const desc = fix.manual.some((c) => c.startsWith("description."));
        manualSection = h(
          "section",
          { class: "seo-fix-sec" },
          h("h3", { class: "seo-fix-h" }, t("fix.manual_title")),
          h("div", { class: "seo-fix-manual" }, issues.map((i) => issueChip(i, false))),
          h(
            "a",
            {
              class: "seo-fix-link",
              href: `/ilanlar/${id}`,
              onClick: () => dlg.close(),
            },
            icon("edit", { size: 14 }),
            t(desc ? "fix.edit_description" : "fix.edit_listing"),
            icon("arrow-right", { size: 14 }),
          ),
        );
      }

      const headRing = scoreRing({ score: item.score, size: 44, fontSize: 15 });
      const body = h(
        "div",
        { class: "seo-fix" },
        h(
          "div",
          { class: "seo-fix-head" },
          thumb({ src: item.thumb || null, size: 48, radius: 10 }),
          h(
            "div",
            { class: "seo-fix-head-text" },
            h("p", { class: "seo-fix-name" }, item.title),
            h(
              "div",
              { class: "row" },
              badge({ text: t(item.state === "draft" ? "fix.state.draft" : "fix.state.active"), tone: item.state === "draft" ? "neutral" : "success", size: "sm", dot: true }),
              h("span", { class: "muted num seo-fix-id" }, `#${item.listing_id}`),
            ),
          ),
          headRing,
        ),
        h(
          "section",
          { class: "seo-fix-sec" },
          h("div", { class: "seo-fix-hrow" }, h("h3", { class: "seo-fix-h" }, t("fix.tags")), countEl),
          removeEl,
          h("div", { class: "seo-fix-list-head" }, h("p", { class: "seo-fix-label" }, t("fix.add_title")), addSource),
          addEl,
          customField,
          h("p", { class: "seo-fix-label" }, t("fix.result")),
          resultEl,
          fullEl,
        ),
        titleSection,
        materialsField ? h("section", { class: "seo-fix-sec" }, materialsField) : null,
        manualSection,
      );

      const dlg = ctx.modal({
        title: t("fix.title"),
        subtitle: t("fix.sub"),
        body,
        actions: [cancelBtn, applyBtn],
        width: 640,
        class: "seo-fix-modal",
      });
      update();

      // Research suggestions for this listing: the right card's search when it is for
      // this listing (the request is shared), else the listing's own keyword.
      const kw = state.selected === id && state.keyword ? state.keyword : item.concept;
      if (kw) {
        fetchResearch(kw, id)
          .then((data) => {
            suggestions = data;
            preselect();
          })
          .catch((err) => {
            if (ctx.api.isAbort(err) || !body.isConnected) return;
            suggestionsError = err;
            update();
          });
      } else {
        suggestions = { keyword: "", tags: [] };
        update();
      }

      // Tags picked with "+ Ekle" come ticked; every other suggestion is listed unticked,
      // for the seller to tick the ones that describe the product (the video adds each
      // tag by its own "Ekle": "Sadece ürününüzü dürüstçe anlatan etiketleri ekleyin").
      function preselect() {
        if (!body.isConnected) return;
        update();
      }

      async function apply() {
        const tags = finalTags();
        const payload = { tags, confirm: true };
        const what = [];
        if (tagsChanged()) what.push(t("fix.what.tags"));
        if (titleOn && fix.title) {
          payload.title = fix.title.after;
          what.push(t("fix.what.title"));
        }
        const mats = materials();
        if (mats.length) {
          if (!materialsValid(mats)) {
            materialsField.setError(t("fix.materials_invalid"));
            return;
          }
          payload.materials = mats;
          what.push(t("fix.what.materials"));
        }
        if (!what.length || !tags.length) return;
        const ok = await ctx.confirm({
          title: t(item.state === "draft" ? "fix.confirm_title_draft" : "fix.confirm_title"),
          message: t("fix.confirm_msg", { title: item.title, what: listText(what) }),
          confirmLabel: t("fix.confirm_ok"),
          icon: "check",
        });
        if (!ok || !body.isConnected) return;
        applyBtn.setLoading(true);
        cancelBtn.setDisabled(true);
        try {
          const res = await ctx.api.post(`/api/seo/fix/${id}`, payload, { signal: ctx.signal });
          dlg.close();
          applied(id, res);
        } catch (err) {
          if (ctx.api.isAbort(err)) return;
          if (err.code === "seo_stale") {
            // Changed on Etsy since the audit: nothing was sent. Show the listing as it is now.
            dlg.close();
            ctx.toast({ tone: "warning", title: t("fix.stale"), message: t("fix.stale_msg"), timeout: 8000 });
            forgetResearch(id);
            loadAudit({ refresh: true });
            return;
          }
          ctx.toast({ tone: "danger", title: t("fix.failed"), message: ctx.api.errorText(err, t), timeout: 8000 });
        } finally {
          applyBtn.setLoading(false);
          cancelBtn.setDisabled(false);
        }
      }
    }

    function applied(id, res) {
      const fresh = res && res.item;
      const old = itemById(id);
      if (!fresh || !old) return;
      state.items[state.items.indexOf(old)] = fresh;
      state.pending.delete(id);
      forgetResearch(id);
      if (state.data) {
        state.data.items = state.items;
        state.data.needs_fix = state.items.filter((it) => !it.ready).length;
        renderSub();
      }
      const view = views.get(id);
      if (view) {
        const next = itemView(fresh, 0, false);
        views.set(id, next);
        view.el.replaceWith(next.el);
        animateRing(next.ring, old.score, fresh.score);
      }
      const before = res.before ?? old.score;
      ctx.toast({ tone: "success", title: t("fix.done"), message: t("fix.done_msg", { before, after: fresh.score }) });
      if (state.selected === id && state.keyword) runResearch(state.keyword);
      else renderResearch();
    }

    // ------------------------------------------------------------ start

    // Keys saved or the shop connected in another tab: try again (a working page stays as it is).
    ctx.onStatus((s, prev) => {
      if (!prev || !s || s.state === prev.state) return;
      if ((state.setup || state.error) && (s.state === "connected" || s.state === "disconnected")) loadAudit();
    });

    renderResearch();
    await loadAudit();

    return () => {
      if (toolsPop) toolsPop.close();
      document.removeEventListener("keydown", onSlash);
      researchCache.clear();
      if (chipObserver) chipObserver.disconnect();
    };
  },
};
