// Şablon İlan (/kurulum/sablon): the listing every new draft copies its business
// settings from. Left: the shop's active listings (cached by the server, filtered and
// paged here). Right: the seven fields that would be copied, with their names resolved.
// Saving writes product.json in the products folder; nothing on Etsy changes.
//
// The saved template's description is copied too, so it has its own editor, the
// "Açıklama şablonu" dialog (opened from a line under the fields, or on arrival with
// ?aciklama=1 from Tasarım Yükle's start card): the listing's own description with its
// title as {başlık}, sentences about the listing's own design highlighted.

import { badge, button, card, cx, debounce, h, iconButton, infoNote, mount, searchInput, spinner, thumb } from "../ui.js";
import { icon } from "../icons.js";
import { lower, money, monthName, number, relative } from "../format.js";

// Until "N ilan daha göster" is pressed the list shows only the rows that fit whole in its
// card (the video's five, then empty room, then the link): at most FIRST_PAGE, at least
// MIN_FIT. Each press adds MORE_PAGE and the list scrolls.
const FIRST_PAGE = 20;
const MIN_FIT = 3;
const MORE_PAGE = 20;
const FIELDS = ["price", "shipping", "category", "who_made", "when_made", "processing", "returns"];
const FIELD_ICON = {
  price: "coins",
  shipping: "truck",
  category: "grid",
  who_made: "user",
  when_made: "loader",
  processing: "clock",
  returns: "refresh",
};

function fieldIcon(key) {
  return icon(FIELD_ICON[key], { size: 15 });
}

/** Etsy's category names as i18n keys: "Home & Living" -> home_living, "T-shirts" -> t_shirts. */
function catSlug(name) {
  return String(name || "")
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}
const STATE_TONE = { active: "success", draft: "muted", inactive: "muted", sold_out: "warning", expired: "warning" };
// Etsy's listing types, and what the drafts made from such a template are.
const TYPE_ICON = { physical: "box", download: "download", both: "package" };
// A pasted Etsy listing link, or a bare listing number (the old window's "listing number" field).
const LISTING_URL = /etsy\.com\/(?:[a-z]{2}(?:-[a-z]{2})?\/)?listing\/(\d{5,13})/i;
const LISTING_NUMBER = /^#?(\d{5,13})$/;

export default {
  async mount(el, ctx) {
    const t = ctx.t;
    const st = {
      items: null, // null while loading
      count: 0,
      truncated: false,
      currency: null,
      listError: null,
      setupStep: null, // "keys" | "connect" when the shop is not set up
      query: "",
      expanded: false, // "daha göster" pressed: show `limit` rows and scroll
      limit: FIRST_PAGE,
      fit: null, // how many whole rows fit in the list before that (measured)
      sales: null, // {months: ["YYYY-MM"]} when the rows carry units sold
      current: undefined, // undefined while loading, null when there is none
      currentProblem: null,
      selectedId: null,
      pinnedId: null, // the saved template's listing when the list was (re)loaded
      pinnedRow: null, // its row, for a template that is not an active listing (a draft)
      previews: new Map(), // listing id -> summary (the saved template is kept here too)
      extras: new Map(), // listings reached by number that are not in the active list
      previewError: null,
      previewLoading: false,
      saving: false,
    };
    let previewSeq = 0;
    // The description template: `draft` holds edits the dialog was closed without saving
    // (null: none), so reopening it brings them back and leaving the page asks first.
    const desc = { draft: null, open: false };

    // ------------------------------------------------------------------ layout

    const refreshBtn = button({
      label: t("refresh"),
      icon: "refresh",
      variant: "secondary",
      autoLoading: true,
      onClick: () => loadListings(true),
    });
    ctx.setHeader({ actions: [refreshBtn] });

    const countSlot = h("div", { class: "tpl-count" });
    const search = searchInput({
      placeholder: t("search"),
      shortcut: "/",
      ariaLabel: t("search_aria"),
      onInput: (v) => {
        st.query = v.trim();
        st.expanded = false;
        renderList();
      },
      onEnter: () => {
        const id = manualId(st.query);
        if (id) select(id);
      },
    });
    const listEl = h("div", { class: "tpl-list", role: "radiogroup", "aria-label": t("list.title") });
    const moreEl = h("div", { class: "tpl-more" });
    const leftCard = card({
      title: t("list.title"),
      subtitle: t("list.subtitle"),
      actions: countSlot,
      class: "tpl-card tpl-listings",
      body: [search, listEl, moreEl],
    });

    const sourceEl = h("span", { class: "tpl-source" });
    const badgesEl = h("div", { class: "tpl-badges" });
    const fieldsEl = h("div", { class: "tpl-fields-list" });
    const notesEl = h("div", { class: "tpl-notes" });
    const hintEl = h("div", { class: "tpl-hint" });
    // Always "Şablon olarak kaydet", as in the video; the last save is in its tooltip.
    const saveBtn = button({ label: t("save"), icon: "file", variant: "primary", size: "lg", onClick: save });
    // Rows revealed one by one after a pick (the video): the id they were last played for.
    let revealedId = null;
    const rightCard = card({
      title: t("fields.title"),
      subtitle: sourceEl,
      actions: badgesEl,
      class: "tpl-card tpl-fields",
      body: [
        fieldsEl,
        h(
          "div",
          { class: "tpl-bottom" },
          notesEl,
          infoNote({ icon: "info", tone: "info", text: [h("b", null, t("note.bold")), " ", t("note.text")] }),
          h("div", { class: "tpl-actions" }, hintEl, saveBtn),
        ),
      ],
    });
    const grid = h("div", { class: "tpl-grid" }, leftCard, rightCard);
    el.append(grid);

    // Under the two cards (the video's screen stays as it is): the shop's info images,
    // the cards every draft ends with (materials, sizes, installation, ...). Ticked among
    // the template listing's photos or added from the computer; each change is saved at
    // once, one at a time.
    const info = {
      state: null, // GET /api/info-images: {items, count, max, mockup_max, mockups, unused}
      error: null,
      source: undefined, // undefined while loading; {listing_id, problem, photos}
      sourceError: null,
      busy: new Set(), // Etsy picture ids being copied
      pending: 0, // files being uploaded
      removing: new Set(), // names being taken out
      drag: null, // the name of the picture being dragged
    };
    let infoChain = Promise.resolve();
    const infoCountEl = h("div", { class: "tpl-info-count" });
    const fileInput = h("input", {
      type: "file",
      accept: "image/png,image/jpeg,image/webp,image/gif,image/bmp,image/tiff",
      multiple: true,
      hidden: true,
      onChange: () => {
        const picked = [...fileInput.files];
        fileInput.value = "";
        uploadInfo(picked);
      },
    });
    const infoAddBtn = button({ label: t("info.add"), icon: "upload", variant: "secondary", size: "sm", onClick: () => fileInput.click() });
    // Files may be dropped on the strip (app.js lets a [data-drop-target] take them).
    const infoOrderEl = h("div", { class: "tpl-info-order", role: "list", "aria-label": t("info.order_title"), dataset: { dropTarget: "" } });
    const infoNotesEl = h("div", { class: "tpl-info-notes" });
    const infoPhotosEl = h("div", { class: "tpl-info-photos" });
    const infoCard = card({
      title: t("info.title"),
      subtitle: t("info.subtitle"),
      actions: [infoCountEl, infoAddBtn, fileInput],
      class: "tpl-card tpl-info",
      body: [
        h(
          "div",
          { class: "tpl-info-section" },
          h("div", { class: "tpl-info-label" }, h("span", null, t("info.order_title")), h("span", { class: "tpl-info-sub" }, icon("move", { size: 12 }), t("info.order_hint"))),
          infoOrderEl,
        ),
        infoNotesEl,
        h(
          "div",
          { class: "tpl-info-section" },
          h("div", { class: "tpl-info-label" }, h("span", null, t("info.source_title")), h("span", { class: "tpl-info-sub" }, t("info.source_hint"))),
          infoPhotosEl,
        ),
      ],
    });
    el.append(infoCard);
    // In the fields card, under the footnote: how many there are, and a way down to them.
    const infoLinkEl = h("button", {
      type: "button",
      class: "tpl-hint-line tpl-info-link",
      onClick: () => infoCard.scrollIntoView({ behavior: "smooth", block: "start" }),
    });
    // The line that opens the description template's dialog (descLink fills it).
    const descLinkEl = h("button", { type: "button", class: "tpl-desc-link", onClick: () => openDescription() });

    // The cards fill the visible height under the top bar (and under the app's warning
    // banner when one shows), so the list scrolls inside its card like in the video.
    const content = el.closest(".content");
    function fit() {
      if (!content || !grid.isConnected) return;
      const cs = getComputedStyle(content);
      const padTop = parseFloat(cs.paddingTop) || 0;
      const padBottom = parseFloat(cs.paddingBottom) || 0;
      const offset = grid.getBoundingClientRect().top - content.getBoundingClientRect().top + content.scrollTop - padTop;
      const avail = content.clientHeight - padTop - padBottom - offset;
      grid.style.setProperty("--tpl-fill", `${Math.max(0, Math.floor(avail))}px`);
    }
    const resizer = typeof ResizeObserver === "function" ? new ResizeObserver(() => fit()) : null;
    if (resizer && content) {
      resizer.observe(content);
      const banner = content.querySelector(".banner-slot");
      if (banner) resizer.observe(banner);
    }
    fit();
    // The list's own height decides how many whole rows it shows (it follows the window
    // and the fields card); the rows are only re-rendered when that number changes.
    const listSizer = typeof ResizeObserver === "function" ? new ResizeObserver(() => refit()) : null;
    if (listSizer) listSizer.observe(listEl);

    // ------------------------------------------------------------------ helpers

    function findItem(id) {
      return (st.items || []).find((it) => it.listing_id === id) || null;
    }

    function manualId(q) {
      const m = LISTING_URL.exec(q) || LISTING_NUMBER.exec((q || "").trim());
      return m ? Number(m[1]) : null;
    }

    function isCurrent(id) {
      return !!st.current && st.current.listing_id === id;
    }

    function shopCurrency() {
      const s = ctx.status();
      return st.currency || (s && s.shop && s.shop.currency) || "USD";
    }

    function tOr(key, fallback, params) {
      return t.has(key) ? t(key, params) : fallback;
    }

    function country(iso) {
      if (!iso) return "";
      const key = `country.${String(iso).toUpperCase()}`;
      if (t.has(key)) return t(key);
      try {
        return new Intl.DisplayNames([ctx.lang === "en" ? "en-US" : "tr-TR"], { type: "region" }).of(String(iso).toUpperCase()) || iso;
      } catch {
        return iso;
      }
    }

    function stateEl(state) {
      const s = state || "active";
      return h(
        "span",
        { class: cx("tpl-state", `tone-${STATE_TONE[s] || "muted"}`) },
        h("span", { class: "dot", "aria-hidden": "true" }),
        tOr(`state.${s}`, s),
      );
    }

    function listingType(value) {
      return value === "download" || value === "both" ? value : "physical";
    }

    /**
     * "Fiziksel" / "Dijital" / "Fiziksel + dijital" at the head of the source line: the
     * drafts keep the template's type. (The card's title row has no room left for it.)
     */
    function typeTag(value) {
      const kind = listingType(value);
      return h(
        "span",
        { class: cx("tpl-type", `is-${kind}`), title: t(`type.${kind}_hint`) },
        icon(TYPE_ICON[kind], { size: 12 }),
        h("span", null, t(`type.${kind}`)),
      );
    }

    function dotSep() {
      return h("span", { class: "tpl-sep", "aria-hidden": "true" }, "·");
    }

    function errorNote(err, retry) {
      const code = err && err.code;
      const toConnect = code === "reconnect" || code === "bad_keys" || code === "setup_needed";
      const action = toConnect
        ? button({ label: t("setup.action"), size: "sm", iconRight: "arrow-right", onClick: () => ctx.navigate("/kurulum/magaza") })
        : button({ label: t("common.retry"), size: "sm", icon: "refresh", autoLoading: true, onClick: retry });
      return infoNote({ icon: "alert", tone: "danger", text: ctx.api.errorText(err, t), action });
    }

    // ------------------------------------------------------------------ left: listings

    // Rows the list can show: the saved template first (as it was when the list loaded, so
    // a row does not jump away right after saving it), then listings reached by number,
    // then the shop's active listings in Etsy's order.
    function allRows() {
      const rows = [];
      const seen = new Set();
      const add = (row) => {
        if (!row || seen.has(row.listing_id)) return;
        seen.add(row.listing_id);
        rows.push(row);
      };
      if (st.pinnedId !== null) add(findItem(st.pinnedId) || st.extras.get(st.pinnedId) || st.pinnedRow);
      for (const row of st.extras.values()) add(row);
      for (const row of st.items || []) add(row);
      return rows;
    }

    function rowFromSummary(s) {
      const price = (s.fields || []).find((f) => f.key === "price");
      return {
        listing_id: s.listing_id,
        title: s.title || t("listing_number", { id: s.listing_id }),
        price: price && price.value ? price.value.amount : null,
        currency: s.currency,
        thumb_url: s.thumb_url,
        state: s.state,
        num_favorers: null,
        sold: null,
        product_type: null,
        product_type_key: null,
        listing_type: s.listing_type,
      };
    }

    /** "Tişört" for Etsy's "T-shirts" when the server knows the product; else Etsy's name. */
    function productType(row) {
      const key = row.product_type_key ? `product.${row.product_type_key}` : null;
      return key && t.has(key) ? t(key) : row.product_type;
    }

    function matches(row, q) {
      if (!q) return true;
      if (manualId(q) === row.listing_id) return true;
      if (String(row.listing_id).startsWith(q.replace(/^#/, ""))) return true;
      return lower(row.title).includes(lower(q));
    }

    function listRow(row) {
      const id = row.listing_id;
      const selected = id === st.selectedId;
      const input = h("input", {
        type: "radio",
        name: "tpl-listing",
        class: "tpl-radio-input",
        value: String(id),
        checked: selected,
        onChange: () => select(id),
      });
      const sub = [];
      const kind = listingType(row.listing_type);
      if (kind !== "physical") sub.push(h("span", { class: "tpl-row-type" }, icon(TYPE_ICON[kind], { size: 12 }), t(`type.${kind}_short`)));
      const product = productType(row);
      if (product) sub.push(h("span", null, product));
      // Units sold (Kâr-Zarar's months on disk) as in the video; the favourites without them.
      if (row.sold !== null && row.sold !== undefined) {
        sub.push(h("span", { class: "tpl-sold", title: salesHint() }, t("sales", { n: row.sold, count: number(row.sold) })));
      } else if (row.num_favorers !== null && row.num_favorers !== undefined) {
        sub.push(h("span", null, t("favorites", { n: row.num_favorers, count: number(row.num_favorers) })));
      }
      if (row.state) sub.push(stateEl(row.state));
      const parts = [];
      sub.forEach((node, i) => {
        if (i) parts.push(dotSep());
        parts.push(node);
      });
      // The saved template has no mark of its own (the video has none): it is pinned first
      // and picked at load; its tooltip and the screen reader say which one it is.
      const current = isCurrent(id);
      return h(
        "label",
        { class: cx("tpl-row", selected && "is-selected"), dataset: { id: String(id) }, title: current ? t("current") : undefined },
        input,
        h("span", { class: "tpl-radio", "aria-hidden": "true" }),
        thumb({ src: row.thumb_url, size: 64, radius: 9, icon: "image" }),
        h(
          "span",
          { class: "tpl-row-main" },
          h(
            "span",
            { class: "tpl-row-head" },
            current ? h("span", { class: "sr-only" }, `${t("current")}: `) : null,
            h("span", { class: "tpl-row-title ellipsis", title: current ? `${t("current")}: ${row.title}` : row.title }, row.title),
          ),
          h("span", { class: "tpl-row-sub" }, parts),
        ),
        h("span", { class: "tpl-row-price mono" }, row.price === null || row.price === undefined ? "–" : money(row.price, row.currency || shopCurrency())),
      );
    }

    function skeletonRows(n) {
      const out = [];
      for (let i = 0; i < n; i += 1) {
        out.push(
          h(
            "div",
            { class: "tpl-row is-skeleton", "aria-hidden": "true" },
            h("span", { class: "tpl-radio" }),
            h("span", { class: "skeleton tpl-skel-thumb" }),
            h(
              "span",
              { class: "tpl-row-main" },
              h("span", { class: "skeleton", style: { height: 13, width: `${70 - i * 6}%` } }),
              h("span", { class: "skeleton", style: { height: 10, width: "34%" } }),
            ),
            h("span", { class: "skeleton", style: { height: 14, width: 52 } }),
          ),
        );
      }
      return out;
    }

    /** "Nis 2026 – Eyl 2026": the months the units sold were counted over. */
    function salesHint() {
      const months = (st.sales && st.sales.months) || [];
      if (!months.length) return "";
      const label = (ym) => {
        const [y, m] = String(ym).split("-").map(Number);
        return y && m ? `${monthName(m - 1, true)} ${y}` : String(ym);
      };
      const first = label(months[0]);
      const last = label(months[months.length - 1]);
      return t("sales_hint", { n: months.length, range: first === last ? first : `${first} – ${last}` });
    }

    /** Rows shown before "daha göster" is pressed: what fits whole, else FIRST_PAGE. */
    function rowLimit() {
      return st.expanded ? st.limit : st.fit || FIRST_PAGE;
    }

    /**
     * How many whole rows fit in the list's box: the rows as rendered, in order, and more
     * of the last one's height when every rendered row fits. null before it is laid out.
     */
    function measureFit() {
      if (!listEl.isConnected || !listEl.clientHeight) return null;
      const rowsEls = [...listEl.children].filter((node) => node.matches(".tpl-row[data-id]"));
      if (!rowsEls.length) return null;
      const cs = getComputedStyle(listEl);
      const gap = parseFloat(cs.rowGap) || 0;
      let avail = listEl.clientHeight - (parseFloat(cs.paddingTop) || 0) - (parseFloat(cs.paddingBottom) || 0);
      // Anything above the rows (the "use listing #id" button) takes its room first.
      for (const node of listEl.children) {
        if (node === rowsEls[0]) break;
        avail -= node.getBoundingClientRect().height + gap;
      }
      let used = 0;
      let n = 0;
      let last = 0;
      for (const rowEl of rowsEls) {
        const height = rowEl.getBoundingClientRect().height;
        const next = used + (n ? gap : 0) + height;
        if (next > avail + 0.5) return Math.max(MIN_FIT, n);
        used = next;
        n += 1;
        last = height;
      }
      return Math.max(MIN_FIT, n + Math.max(0, Math.floor((avail - used) / (last + gap))));
    }

    /** The list's height changed: re-render only when a different number of rows fits. */
    function refit() {
      if (st.expanded || !st.items || st.setupStep) return;
      const fitted = measureFit();
      if (fitted !== null && fitted !== st.fit) {
        st.fit = fitted;
        renderList();
      }
    }

    function renderCount() {
      mount(
        countSlot,
        st.items ? badge({ icon: "list", text: t("count", { n: st.count, count: number(st.count) }), tone: "neutral" }) : null,
      );
    }

    function renderList(again = false) {
      renderCount();
      search.hidden = !!st.setupStep;
      refreshBtn.setDisabled(!!st.setupStep);
      if (st.setupStep) {
        mount(
          listEl,
          h(
            "div",
            { class: "tpl-setup" },
            h("span", { class: "empty-icon" }, icon("link", { size: 22 })),
            h("p", { class: "empty-title" }, t("setup.title")),
            h("p", { class: "empty-msg" }, t(`setup.${st.setupStep === "connect" ? "connect" : "keys"}`)),
            button({ label: t("setup.action"), variant: "primary", iconRight: "arrow-right", onClick: () => ctx.navigate("/kurulum/magaza") }),
          ),
        );
        mount(moreEl);
        return;
      }
      if (st.listError && !st.items) {
        mount(listEl, errorNote(st.listError, () => loadListings(false)));
        mount(moreEl);
        return;
      }
      if (!st.items) {
        mount(listEl, skeletonRows(5));
        mount(moreEl);
        return;
      }

      const q = st.query;
      const rows = allRows().filter((row) => matches(row, q));
      const shown = rows.slice(0, rowLimit());
      const nodes = [];
      const wanted = manualId(q);
      if (wanted && !rows.some((row) => row.listing_id === wanted)) {
        nodes.push(
          h(
            "button",
            { type: "button", class: "tpl-byid", onClick: () => select(wanted) },
            h("span", { class: "tpl-byid-icon" }, icon("search", { size: 15 })),
            h("span", { class: "tpl-byid-text" }, h("b", null, t("by_id.title", { id: wanted })), h("span", null, t("by_id.sub"))),
            icon("arrow-right", { size: 15 }),
          ),
        );
      }
      const rowNodes = shown.map(listRow);
      nodes.push(...rowNodes);
      if (!rows.length && !nodes.length) {
        nodes.push(
          h(
            "div",
            { class: "tpl-empty" },
            h("p", { class: "empty-title" }, q ? t("empty.search_title") : t("empty.title")),
            h("p", { class: "empty-msg" }, q ? t("empty.search_msg") : t("empty.msg")),
          ),
        );
      }
      mount(listEl, nodes);
      let count = shown.length;
      if (!st.expanded) {
        // Whole rows only: measured on the rows just rendered, then shown once more when
        // the number differs. Rows of different heights could disagree between the two
        // passes: then whatever does not fit whole is dropped instead of rendered again.
        const fitted = measureFit();
        if (fitted !== null && fitted !== st.fit) {
          st.fit = fitted;
          if (!again && Math.min(rows.length, fitted) !== count) {
            renderList(true);
            return;
          }
        }
        if (fitted !== null && fitted < count) {
          for (const node of rowNodes.slice(fitted)) node.remove();
          count = fitted;
        }
      }

      const left = rows.length - count;
      const hidden = !q && st.truncated ? Math.max(0, st.count - (st.items || []).length) : 0;
      mount(
        moreEl,
        left > 0
          ? h(
              "button",
              {
                type: "button",
                class: "tpl-more-btn",
                onClick: () => {
                  st.limit = count + MORE_PAGE;
                  st.expanded = true;
                  renderList();
                },
              },
              t("more", { n: left + hidden, count: number(left + hidden) }),
              icon("arrow-down", { size: 14 }),
            )
          : hidden > 0
            ? h("p", { class: "tpl-more-note" }, t("truncated", { n: (st.items || []).length }))
            : null,
      );
    }

    // Selection changes only toggle classes, so keyboard focus stays on the radio.
    function renderSelection() {
      for (const rowEl of listEl.querySelectorAll(".tpl-row[data-id]")) {
        const on = Number(rowEl.dataset.id) === st.selectedId;
        rowEl.classList.toggle("is-selected", on);
        const input = rowEl.querySelector("input");
        if (input && input.checked !== on) input.checked = on;
      }
    }

    // ------------------------------------------------------------------ right: fields

    function describe(f) {
      const v = f.value || {};
      switch (f.key) {
        case "price":
          return { text: money(v.amount, v.currency || shopCurrency()) };
        case "shipping":
          if (v.digital) return { text: t("shipping.digital") };
          if (v.title) return { text: v.origin_country ? `${v.title} · ${country(v.origin_country)}` : v.title };
          return { text: t("shipping.id", { id: v.id }) };
        case "category":
          if (v.path && v.path.length) return { text: categoryText(v.path), title: v.path.join(" › ") };
          return { text: t("category.id", { id: v.id }) };
        case "who_made":
          return { code: v.code, text: tOr(`who.${v.code}`, v.code) };
        case "when_made":
          return { code: v.code, text: whenText(v.code) };
        case "processing":
          return { text: processingText(v) };
        case "returns":
          return { text: returnsText(v) };
        default:
          return { text: "" };
      }
    }

    /**
     * Etsy's root and leaf ("Giyim › Tişörtler", the video's), in the UI's language by the
     * exact Etsy name. When either has no string, both stay in Etsy's English rather than
     * a Turkish-English mix. The full path is the tooltip.
     */
    function categoryText(path) {
      const root = String(path[0]);
      const leaf = String(path[path.length - 1]);
      const rootKey = `category_root.${catSlug(root)}`;
      if (path.length === 1 || root === leaf) return t.has(rootKey) ? t(rootKey) : root;
      const leafKey = `category_leaf.${catSlug(leaf)}`;
      if (t.has(rootKey) && t.has(leafKey)) return `${t(rootKey)} › ${t(leafKey)}`;
      return `${root} › ${leaf}`;
    }

    function whenText(code) {
      if (!code) return "";
      if (t.has(`when.${code}`)) return t(`when.${code}`);
      let m = /^(\d{4})_(\d{4})$/.exec(code);
      if (m) return `${m[1]}–${m[2]}`;
      m = /^before_(\d{4})$/.exec(code);
      if (m) return t("when.before", { year: m[1] });
      m = /^(\d{4})s$/.exec(code);
      if (m) return t("when.decade", { decade: m[1] });
      return code;
    }

    function processingText(v) {
      if (v.digital && v.readiness_state_id === null && v.min === null && v.max === null) return t("processing.digital");
      if (v.min !== null || v.max !== null) {
        const lo = v.min ?? v.max;
        const hi = v.max ?? v.min;
        return lo === hi ? t("processing.days", { n: lo }) : t("processing.range", { min: lo, max: hi });
      }
      if (v.label) return v.label;
      return t("processing.id", { id: v.readiness_state_id });
    }

    function returnsText(v) {
      if (v.accepts_returns === null || v.accepts_returns === undefined) return t("returns.id", { id: v.id });
      if (v.accepts_returns) return v.deadline ? t("returns.days", { n: v.deadline }) : t("returns.yes");
      return v.accepts_exchanges ? t("returns.exchanges") : t("returns.none");
    }

    function fieldRow(key, f, mode, reveal = false) {
      // mode: "value" | "loading" | "blank"; reveal: the values fade in over grey bars
      const label = h("span", { class: "tpl-field-label" }, t(`field.${key}`));
      let value = null;
      let mark = null;
      let why = null;
      if (mode === "loading") {
        value = h("span", { class: "skeleton", style: { height: 13, width: 96 } });
        mark = h("span", { class: "tpl-check is-idle", "aria-hidden": "true" });
      } else if (mode === "blank") {
        // Nothing picked yet: a grey bar where the value will come (the video's first state).
        value = h("span", { class: "tpl-bar", "aria-hidden": "true" });
        mark = h("span", { class: "tpl-check is-idle", "aria-hidden": "true" });
      } else if (f.ok) {
        const d = describe(f);
        value = [
          // The video keeps each row's grey bar until its value has faded in over it.
          reveal ? h("span", { class: "tpl-bar tpl-bar-out", "aria-hidden": "true" }) : null,
          d.code ? h("code", { class: "tpl-code" }, d.code) : null,
          h("span", { class: cx("tpl-value ellipsis", key === "price" && "mono"), title: d.title || d.text }, d.text),
        ];
        mark = h("span", { class: "tpl-check", title: t("copied"), role: "img", "aria-label": t("copied") }, icon("check", { size: 12, strokeWidth: 2.6 }));
      } else {
        why = h("span", { class: cx("tpl-field-why", !f.required && "is-optional") }, t(`why.${key}`));
        mark = badge({ text: t("missing"), tone: "warning", size: "sm", icon: "alert" });
      }
      const missing = mode === "value" && !f.ok;
      return h(
        "div",
        {
          class: cx("tpl-field", missing && (f.required ? "is-missing" : "is-optional"), mode !== "value" && "is-idle"),
        },
        h("span", { class: "tpl-field-icon" }, fieldIcon(key)),
        h("span", { class: "tpl-field-text" }, label, why),
        h("span", { class: "tpl-field-value" }, value),
        mark,
      );
    }

    function renderRight() {
      const id = st.selectedId;
      const p = id !== null ? st.previews.get(id) : null;
      const current = p && isCurrent(p.listing_id);
      const known = p || (id !== null ? findItem(id) || st.extras.get(id) : null);

      // The type goes first only when it is not the usual physical one (digital, both).
      const shownType = p ? p.listing_type : known && known.listing_type;
      if (id === null) {
        mount(sourceEl, h("span", { class: "tpl-source-none" }, t("source_none")));
      } else {
        mount(
          sourceEl,
          shownType && listingType(shownType) !== "physical" ? typeTag(shownType) : null,
          h("span", { class: "tpl-source-label" }, t("source")),
          " ",
          known && known.title
            ? h("span", { class: "tpl-source-title", title: known.title }, known.title)
            : h("span", { class: "tpl-source-none" }, t("listing_number", { id })),
        );
      }

      mount(
        badgesEl,
        !p
          ? badge({ text: t("fields.count", { n: FIELDS.length }), tone: "neutral" })
          : p.ok_count === p.total
            ? badge({ icon: "check", text: t("fields.count", { n: p.total }), tone: "success" })
            : badge({ icon: "alert", text: t("fields.partial", { ok: p.ok_count, total: p.total }), tone: "warning" }),
      );

      if (p) {
        // Values fade in row by row the first time a listing's fields are shown after a pick;
        // not again for the same listing, and not for the saved template shown at load.
        const reveal = p.listing_id !== revealedId;
        revealedId = p.listing_id;
        mount(
          fieldsEl,
          p.fields.map((f, i) => {
            const row = fieldRow(f.key, f, "value", reveal);
            row.style.setProperty("--i", String(i));
            return row;
          }),
        );
        fieldsEl.classList.toggle("is-reveal", reveal);
      } else if (st.previewError && id !== null) {
        fieldsEl.classList.remove("is-reveal");
        const err = st.previewError;
        mount(
          fieldsEl,
          err.code === "not_found"
            ? infoNote({ icon: "alert", tone: "warning", text: t("preview_not_found", { id }) })
            : errorNote(err, () => loadPreview(id)),
        );
      } else {
        fieldsEl.classList.remove("is-reveal");
        const mode = st.previewLoading || st.current === undefined ? "loading" : "blank";
        mount(fieldsEl, FIELDS.map((key) => fieldRow(key, null, mode)));
      }

      const notes = [];
      if (st.currentProblem === "malformed" && !p) {
        notes.push(infoNote({ icon: "alert", tone: "warning", text: t("malformed") }));
      }
      mount(notesEl, notes);

      // Left of the save button: the video's footnote, then what the page must still say.
      const hints = [h("span", { class: "tpl-hint-line tpl-footnote" }, t("footnote"))];
      if (p && p.fields.some((f) => !f.ok && f.required)) {
        hints.push(h("span", { class: "tpl-hint-line is-warning" }, icon("alert", { size: 13 }), h("span", null, t("missing_hint"))));
      }
      if (p && p.has_variations) {
        hints.push(h("span", { class: "tpl-hint-line is-strong" }, icon("layers", { size: 13 }), t("variations")));
      }
      const kind = p ? listingType(p.listing_type) : "physical";
      if (kind !== "physical") {
        hints.push(h("span", { class: "tpl-hint-line is-strong" }, icon("download", { size: 13 }), t(`type.${kind}_note`)));
      }
      // The saved template's description template: a quiet link, or what it still holds
      // about the listing's own design. Only for the saved template (it is product.json's).
      if (current) hints.push(descLink(st.current.description));
      renderInfoLink();
      hints.push(infoLinkEl);
      // The two links are the same nodes every time: one that had the focus (the
      // description dialog just gave it back) keeps it through the re-mount.
      const focused = hintEl.contains(document.activeElement) ? document.activeElement : null;
      mount(hintEl, hints);
      if (focused && focused.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
      saveBtn.title = current && st.current.saved_at ? t("saved_ago", { when: relative(st.current.saved_at) }) : "";
      saveBtn.setDisabled(!p || !!st.setupStep);
      saveBtn.setLoading(st.saving);
    }

    // ------------------------------------------------------------------ info images

    function infoItems() {
      return (info.state && info.state.items) || [];
    }

    /** No room for another: what is there plus what is on its way reaches the limit. */
    function infoFull() {
      const s = info.state;
      return !!s && infoItems().length + info.pending + info.busy.size >= s.max;
    }

    function renderInfoLink() {
      const n = infoItems().length;
      infoLinkEl.hidden = !info.state;
      infoLinkEl.classList.toggle("is-none", !n);
      infoLinkEl.title = n ? t("info.count", { n }) : t("info.link_none");
      mount(infoLinkEl, icon("image", { size: 13 }), h("span", null, n ? t("info.link", { n }) : t("info.link_none")), icon("arrow-down", { size: 12 }));
    }

    function renderInfo() {
      // Every photo and tile is rebuilt: the keyboard stays on the same one (or the next).
      const focus = infoFocusKey();
      paintInfo();
      restoreInfoFocus(focus);
    }

    /** What has the keyboard in the info card, to find its new node after a re-render. */
    function infoFocusKey() {
      const a = document.activeElement;
      if (!a || !infoCard.contains(a)) return null;
      const photo = a.closest(".tpl-photo[data-id]");
      if (photo) return { photo: photo.dataset.id };
      if (a.classList.contains("tpl-info-add")) return { add: true };
      const tile = a.closest(".tpl-info-tile[data-name]");
      if (tile && a.classList.contains("is-remove")) {
        const tiles = [...infoOrderEl.querySelectorAll(".tpl-info-tile[data-name]")];
        return { remove: tile.dataset.name, index: tiles.indexOf(tile) };
      }
      return null;
    }

    function restoreInfoFocus(key) {
      if (!key || infoCard.contains(document.activeElement)) return;
      let target = null;
      if (key.photo) {
        target = [...infoPhotosEl.querySelectorAll(".tpl-photo[data-id]")].find((node) => node.dataset.id === key.photo) || null;
      } else if (key.remove) {
        // Its own × while it goes; once it is gone, the next picture's (or the last one's),
        // else the add tile.
        const tiles = [...infoOrderEl.querySelectorAll(".tpl-info-tile[data-name]")];
        const tile = tiles.find((node) => node.dataset.name === key.remove) || tiles[Math.min(key.index, tiles.length - 1)];
        target = tile ? tile.querySelector(".is-remove") : null;
      }
      if (!target) target = infoOrderEl.querySelector(".tpl-info-add");
      if (target) target.focus({ preventScroll: true });
    }

    function paintInfo() {
      const s = info.state;
      const n = infoItems().length;
      mount(
        infoCountEl,
        s ? badge({ icon: n ? "check" : "image", text: n ? t("info.count", { n }) : t("info.count_none"), tone: n ? "success" : "neutral" }) : null,
      );
      const full = infoFull();
      infoAddBtn.setDisabled(!s || full);
      infoAddBtn.title = s && full ? t("info.full", { max: s.max }) : t("info.add_hint");
      renderInfoOrder();
      renderInfoNotes();
      renderInfoPhotos();
      renderInfoLink();
    }

    function renderInfoOrder() {
      const s = info.state;
      if (!s) {
        mount(
          infoOrderEl,
          info.error
            ? infoNote({
                tone: "danger",
                icon: "alert",
                text: [h("b", null, t("info.load_failed")), " ", ctx.api.errorText(info.error, t)],
                action: button({ label: t("common.retry"), size: "sm", icon: "refresh", autoLoading: true, onClick: () => loadInfo() }),
              })
            : [0, 1, 2].map(() => h("span", { class: "tpl-info-tile is-skeleton skeleton", "aria-hidden": "true" })),
        );
        return;
      }
      const items = infoItems();
      const tiles = items.map((item, i) => orderTile(item, i, items.length));
      for (let i = 0; i < info.pending + info.busy.size; i += 1) {
        tiles.push(h("span", { class: "tpl-info-tile is-pending", role: "listitem" }, spinner({ size: 18 }), h("span", { class: "tpl-info-pending" }, t("info.adding"))));
      }
      if (!infoFull()) {
        tiles.push(
          h(
            "button",
            { type: "button", class: "tpl-info-tile tpl-info-add", title: t("info.add_hint"), onClick: () => fileInput.click() },
            icon("plus", { size: 18 }),
            h("span", null, t("info.add")),
          ),
        );
      }
      if (!items.length && !info.pending && !info.busy.size) tiles.push(h("p", { class: "tpl-info-empty" }, t("info.empty")));
      mount(infoOrderEl, tiles);
    }

    function orderTile(item, i, n) {
      const removing = info.removing.has(item.name);
      const from = item.source === "etsy" ? t("info.from_etsy") : t("info.from_file");
      const label = [t("info.pos", { n: i + 1 }), from, item.alt ? t("info.alt", { alt: item.alt }) : item.name].join(" · ");
      const tile = h(
        "div",
        { class: cx("tpl-info-tile", removing && "is-removing"), role: "listitem", draggable: "true", dataset: { name: item.name }, title: label, "aria-label": label },
        h(
          "span",
          { class: "tpl-info-img" },
          h("img", { src: ctx.api.url("/api/files/thumb", { path: item.path, w: 320, v: item.v }), alt: "", loading: "lazy", decoding: "async", draggable: "false" }),
        ),
        h("span", { class: "tpl-info-num num", "aria-hidden": "true" }, String(i + 1)),
        h("span", { class: "tpl-info-kind", "aria-hidden": "true" }, icon(item.source === "etsy" ? "store" : "file", { size: 11 })),
        h(
          "span",
          { class: "tpl-info-tools" },
          moveBtn(item.name, -1, i === 0 || removing),
          moveBtn(item.name, 1, i === n - 1 || removing),
          removeBtn(item.name, removing),
        ),
        removing ? h("span", { class: "tpl-info-busy" }, spinner({ size: 18 })) : null,
      );
      tile.addEventListener("dragstart", (e) => {
        info.drag = item.name;
        tile.classList.add("is-dragging");
        if (e.dataTransfer) {
          e.dataTransfer.effectAllowed = "move";
          try {
            e.dataTransfer.setData("text/plain", item.name);
          } catch {
            /* some browsers refuse data on a drag they did not start */
          }
        }
      });
      tile.addEventListener("dragend", () => {
        info.drag = null;
        tile.classList.remove("is-dragging");
        clearInfoDrop();
      });
      return tile;
    }

    /** The × of a picture; while it goes it stays focusable (aria-disabled), so the
     * keyboard is not thrown back to the top of the page. */
    function removeBtn(name, removing) {
      const b = iconButton({ icon: "x", title: t("info.remove"), size: "sm", variant: "ghost", class: "tpl-info-btn is-remove", onClick: () => removeInfo(name) });
      if (removing) b.setAttribute("aria-disabled", "true");
      return b;
    }

    function moveBtn(name, step, disabled) {
      const b = iconButton({
        icon: step < 0 ? "arrow-left" : "arrow-right",
        title: t(step < 0 ? "info.move_left" : "info.move_right"),
        size: "sm",
        variant: "ghost",
        class: "tpl-info-btn",
        disabled,
        onClick: () => moveInfo(name, step),
      });
      b.dataset.step = String(step);
      return b;
    }

    /** Where a dragged picture would land: the index among the others, by the pointer's x. */
    function infoDropIndex(e) {
      const tiles = [...infoOrderEl.querySelectorAll(".tpl-info-tile[data-name]")].filter((node) => node.dataset.name !== info.drag);
      let index = tiles.length;
      for (let i = 0; i < tiles.length; i += 1) {
        const r = tiles[i].getBoundingClientRect();
        const sameRow = e.clientY >= r.top && e.clientY <= r.bottom;
        if ((sameRow && e.clientX < r.left + r.width / 2) || e.clientY < r.top) {
          index = i;
          break;
        }
      }
      return { index, tiles };
    }

    function clearInfoDrop() {
      infoOrderEl.classList.remove("is-file-over");
      for (const node of infoOrderEl.querySelectorAll(".is-drop-before, .is-drop-after")) node.classList.remove("is-drop-before", "is-drop-after");
    }

    const hasFiles = (e) => !!e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files");
    infoOrderEl.addEventListener("dragover", (e) => {
      if (!info.drag && !hasFiles(e)) return;
      e.preventDefault();
      if (!info.drag) {
        if (e.dataTransfer) e.dataTransfer.dropEffect = infoFull() ? "none" : "copy";
        infoOrderEl.classList.toggle("is-file-over", !infoFull());
        return;
      }
      if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
      clearInfoDrop();
      const { index, tiles } = infoDropIndex(e);
      if (tiles[index]) tiles[index].classList.add("is-drop-before");
      else if (tiles.length) tiles[tiles.length - 1].classList.add("is-drop-after");
    });
    infoOrderEl.addEventListener("dragleave", (e) => {
      if (!infoOrderEl.contains(e.relatedTarget)) clearInfoDrop();
    });
    infoOrderEl.addEventListener("drop", (e) => {
      if (!info.drag && !hasFiles(e)) return;
      e.preventDefault();
      clearInfoDrop();
      if (info.drag) {
        const { index, tiles } = infoDropIndex(e);
        const names = tiles.map((node) => node.dataset.name);
        names.splice(index, 0, info.drag);
        info.drag = null;
        const current = infoItems().map((it) => it.name);
        if (names.join("\n") !== current.join("\n")) saveInfoOrder(names);
        return;
      }
      if (!infoFull()) uploadInfo([...((e.dataTransfer && e.dataTransfer.files) || [])]);
    });

    function renderInfoNotes() {
      const s = info.state;
      const notes = [];
      if (s) {
        const over = (s.mockups && s.mockups.over_limit) || 0;
        const toMockups = button({ label: t("info.go_mockups"), size: "sm", iconRight: "arrow-right", onClick: () => ctx.navigate("/kurulum/mockuplar") });
        if (over) notes.push(infoNote({ tone: "warning", icon: "alert", text: t("info.mockups_over", { n: over, max: s.mockup_max }), action: toMockups }));
        else if (s.count) notes.push(infoNote({ tone: "info", icon: "info", text: [t("info.mockups", { max: s.mockup_max }), " ", t("info.no_watermark")] }));
        const unused = (s.unused || []).length;
        if (unused) notes.push(infoNote({ tone: "warning", icon: "image", text: t("info.unused", { n: unused, max: s.max }) }));
      }
      mount(infoNotesEl, notes);
    }

    function renderInfoPhotos() {
      const src = info.source;
      if (src === undefined && !info.sourceError) {
        mount(infoPhotosEl, [0, 1, 2, 3, 4].map(() => h("span", { class: "tpl-photo is-skeleton skeleton", "aria-hidden": "true" })));
        return;
      }
      if (info.sourceError) {
        const err = info.sourceError;
        const setup = err.code === "setup_needed" || err.code === "reconnect" || err.code === "bad_keys";
        mount(
          infoPhotosEl,
          setup
            ? h("p", { class: "tpl-info-empty" }, t("info.source_setup"))
            : errorNote(err, () => loadInfoSource()),
        );
        return;
      }
      if (src.problem === "no_template") {
        mount(infoPhotosEl, h("p", { class: "tpl-info-empty" }, t("info.source_none")));
        return;
      }
      if (!src.photos.length) {
        mount(infoPhotosEl, h("p", { class: "tpl-info-empty" }, t("info.source_empty")));
        return;
      }
      const picked = new Map(infoItems().filter((it) => it.listing_image_id).map((it) => [it.listing_image_id, it.name]));
      const order = infoItems().map((it) => it.name);
      const full = infoFull();
      mount(
        infoPhotosEl,
        src.photos.map((photo) => {
          const name = picked.get(photo.listing_image_id) || null;
          const pos = name ? order.indexOf(name) + 1 : 0;
          const busy = info.busy.has(photo.listing_image_id) || (name && info.removing.has(name));
          const title = pos ? t("info.picked", { n: pos }) : full ? t("info.full", { max: info.state.max }) : t("info.pick");
          // A busy photo keeps the focus (aria-disabled; the click does nothing meanwhile).
          return h(
            "button",
            {
              type: "button",
              class: cx("tpl-photo", pos && "is-picked", busy && "is-busy"),
              dataset: { id: String(photo.listing_image_id) },
              "aria-pressed": pos ? "true" : "false",
              "aria-disabled": busy ? "true" : null,
              title: photo.alt ? `${title} · ${t("info.alt", { alt: photo.alt })}` : title,
              disabled: !busy && ((!pos && full) || !info.state),
              onClick: () => {
                if (busy) return;
                if (name) removeInfo(name);
                else addInfoFromEtsy(photo.listing_image_id);
              },
            },
            h("span", { class: "tpl-photo-img" }, photo.thumb || photo.url ? h("img", { src: photo.thumb || photo.url, alt: photo.alt || "", loading: "lazy", decoding: "async" }) : icon("image", { size: 20 })),
            h("span", { class: "tpl-photo-check", "aria-hidden": "true" }, pos ? h("span", { class: "num" }, String(pos)) : null),
            busy ? h("span", { class: "tpl-info-busy" }, spinner({ size: 18 })) : null,
          );
        }),
      );
    }

    function setInfoState(s) {
      info.state = s;
      info.error = null;
    }

    /** One change at a time, in the order they were asked for; the page shows each answer. */
    function queueInfo(fn) {
      infoChain = infoChain.then(fn).catch(() => {});
      return infoChain;
    }

    function infoFailed(err) {
      if (ctx.api.isAbort(err)) return;
      ctx.toast({ tone: "danger", title: t("info.failed"), message: ctx.api.errorText(err, t) });
    }

    function addInfoFromEtsy(id) {
      if (info.busy.has(id) || infoFull()) return;
      info.busy.add(id);
      renderInfo();
      queueInfo(async () => {
        try {
          setInfoState(await ctx.api.post("/api/info-images/etsy", { listing_image_ids: [id] }, { signal: ctx.signal }));
        } catch (err) {
          infoFailed(err);
          if (err && err.code === "photo_gone") loadInfoSource();
        } finally {
          info.busy.delete(id);
          if (ctx.isActive()) renderInfo();
        }
      });
    }

    function uploadInfo(fileList) {
      const s = info.state;
      if (!s || !fileList.length) return;
      const room = Math.max(0, s.max - infoItems().length - info.pending - info.busy.size);
      if (fileList.length > room) ctx.toast({ tone: "warning", title: t("info.full", { max: s.max }) });
      for (const file of fileList.slice(0, room)) {
        info.pending += 1;
        queueInfo(async () => {
          try {
            setInfoState(await ctx.api.upload("/api/info-images/files", file, { query: { name: file.name }, method: "PUT", signal: ctx.signal }));
          } catch (err) {
            infoFailed(err);
          } finally {
            info.pending -= 1;
            if (ctx.isActive()) renderInfo();
          }
        });
      }
      renderInfo();
    }

    function removeInfo(name) {
      if (info.removing.has(name)) return;
      info.removing.add(name);
      renderInfo();
      queueInfo(async () => {
        try {
          setInfoState(await ctx.api.del(`/api/info-images/${encodeURIComponent(name)}`, null, { signal: ctx.signal }));
        } catch (err) {
          infoFailed(err);
        } finally {
          info.removing.delete(name);
          if (ctx.isActive()) renderInfo();
        }
      });
    }

    function moveInfo(name, step) {
      const names = infoItems().map((it) => it.name);
      const i = names.indexOf(name);
      const j = i + step;
      if (i < 0 || j < 0 || j >= names.length) return;
      [names[i], names[j]] = [names[j], names[i]];
      saveInfoOrder(names, { name, step });
    }

    function saveInfoOrder(names, focus) {
      // Shown in the new order at once; the server's answer follows.
      const byName = new Map(infoItems().map((it) => [it.name, it]));
      if (info.state) info.state = { ...info.state, items: names.map((n) => byName.get(n)).filter(Boolean) };
      renderInfo();
      focusMoved(focus);
      queueInfo(async () => {
        try {
          setInfoState(await ctx.api.post("/api/info-images/order", { names }, { signal: ctx.signal }));
        } catch (err) {
          infoFailed(err);
          await loadInfo();
          return;
        }
        if (!ctx.isActive()) return;
        const had = !!focus && infoOrderEl.contains(document.activeElement);
        renderInfo();
        if (had) focusMoved(focus);
      });
    }

    /** Keep the keyboard on the moved picture's arrow (the tiles are rebuilt), or on its
     * other arrow once it reached an end. */
    function focusMoved(focus) {
      if (!focus) return;
      const tile = [...infoOrderEl.querySelectorAll(".tpl-info-tile[data-name]")].find((node) => node.dataset.name === focus.name);
      if (!tile) return;
      const same = tile.querySelector(`.tpl-info-btn[data-step="${focus.step}"]`);
      const other = tile.querySelector(`.tpl-info-btn[data-step="${-focus.step}"]`);
      const target = same && !same.disabled ? same : other;
      if (target) target.focus({ preventScroll: true });
    }

    async function loadInfo() {
      try {
        setInfoState(await ctx.api.get("/api/info-images", null, { signal: ctx.signal }));
      } catch (err) {
        if (ctx.api.isAbort(err)) return;
        info.error = err;
      }
      if (ctx.isActive()) renderInfo();
    }

    /** The saved template listing's photos (and the info images with them). */
    async function loadInfoSource() {
      info.sourceError = null;
      try {
        const r = await ctx.api.get("/api/info-images/source", null, { signal: ctx.signal });
        info.source = r;
        if (r.state) setInfoState(r.state);
      } catch (err) {
        if (ctx.api.isAbort(err)) return;
        info.sourceError = err;
        if (!info.state) await loadInfo();
      }
      if (ctx.isActive()) renderInfo();
    }

    // ------------------------------------------------------------------ description template

    /**
     * The line under the fields that opens the dialog: warning-coloured while the saved
     * text flags sentences, or while edits wait unsaved (the dialog was closed on them).
     */
    function descLink(state) {
      const flags = (state && state.flags) || 0;
      const unsaved = desc.draft !== null;
      const text = unsaved ? t("desc.unsaved") : flags ? t("desc.hint_flagged", { n: flags }) : t("desc.open");
      // The words wrap under a narrow column (a 1280 px window) rather than being cut.
      // One button, updated in place: the dialog gives the focus back to it.
      descLinkEl.className = cx("tpl-desc-link", (flags || unsaved) && "is-warning");
      return mount(
        descLinkEl,
        unsaved ? h("span", { class: "tpl-desc-dot", "aria-hidden": "true" }) : icon(flags ? "alert" : "edit", { size: 13 }),
        h(
          "span",
          { class: "tpl-desc-link-text" },
          text,
          flags || unsaved ? [" ", h("span", { class: "tpl-desc-link-action" }, t("desc.hint_action"))] : null,
        ),
      );
    }

    function setDescDraft(text) {
      desc.draft = text;
      ctx.setDirty(text !== null);
    }

    // Unsaved edits of the description template: asked before any in-app navigation, a
    // shop switch or a language change; setDirty makes a reload or closing the tab ask too.
    ctx.onBeforeLeave(() =>
      desc.draft === null
        ? true
        : ctx.confirm({ title: t("desc.leave.title"), message: t("desc.leave.message"), confirmLabel: t("desc.leave.confirm"), danger: true }),
    );

    async function openDescription() {
      if (desc.open) return;
      if (!st.current) {
        ctx.toast({ tone: "warning", title: t("errors.no_template") });
        return;
      }
      desc.open = true;
      let data;
      try {
        data = await ctx.api.get("/api/template/description", null, { signal: ctx.signal });
      } catch (err) {
        desc.open = false;
        if (ctx.api.isAbort(err)) return;
        ctx.toast({ tone: "danger", title: t("desc.load_failed"), message: ctx.api.errorText(err, t) });
        return;
      }
      if (!ctx.isActive()) {
        desc.open = false;
        return;
      }
      descDialog(data);
    }

    /** The saved template's summary follows what the dialog saved: {custom, flags}. */
    function noteDescState(data) {
      if (!st.current) return;
      st.current.description = { custom: !!data.custom, flags: (data.flags || []).length };
      renderRight();
    }

    function descDialog(data) {
      const saved = data.text;
      const ph = data.placeholders || { title: "{başlık}", design: "{tasarım}" };
      const kept = desc.draft !== null && desc.draft !== saved;
      let text = kept ? desc.draft : saved;
      let flags = kept ? [] : data.flags || [];
      let unknown = kept ? [] : data.unknown || [];
      let checking = false;
      let saving = false;
      let active = -1; // the flag the caret is in
      let closing = null; // "saved" | "discard" when a button closed the dialog

      // The editor: a transparent textarea over a copy of its text laid out the same way,
      // where the flagged sentences are marked (a textarea cannot colour its own text).
      const marks = h("div", { class: "tpl-desc-marks" });
      const backdrop = h("div", { class: "tpl-desc-backdrop", "aria-hidden": "true" }, marks);
      const ta = h("textarea", {
        class: "tpl-desc-input",
        spellcheck: "true",
        maxlength: data.max || undefined,
        "aria-label": t("desc.aria"),
        autofocus: true,
      });
      ta.value = text;
      const editor = h("div", { class: "tpl-desc-editor" }, backdrop, ta);

      const stateSlot = h("span", { class: "tpl-desc-state" });
      const phButton = (value, label, hint) =>
        h(
          "button",
          { type: "button", class: "tpl-desc-ph", title: `${value}: ${hint}`, onClick: () => edit(ta.selectionStart, ta.selectionEnd, value) },
          h("code", null, value),
          h("span", null, label),
        );
      const toolbar = h(
        "div",
        { class: "tpl-desc-toolbar" },
        h("span", { class: "tpl-desc-insert" }, t("desc.insert")),
        phButton(ph.title, t("desc.ph.title"), t("desc.ph.title_hint")),
        phButton(ph.design, t("desc.ph.design"), t("desc.ph.design_hint")),
        h("span", { class: "spacer" }),
        stateSlot,
      );
      const countEl = h("span", { class: "tpl-desc-count num" });
      const legend = h(
        "div",
        { class: "tpl-desc-legend" },
        h(
          "span",
          { class: "tpl-desc-legend-text" },
          h("code", null, ph.title),
          ` ${t("desc.ph.title_hint")} · `,
          h("code", null, ph.design),
          ` ${t("desc.ph.design_hint")}`,
        ),
        countEl,
      );
      const problemsEl = h("div", { class: "tpl-desc-problems" });
      const flagsEl = h("div", { class: "tpl-desc-flags" });

      const resetBtn = button({ label: t("desc.reset"), icon: "undo", variant: "ghost", class: "tpl-desc-reset", onClick: () => edit(0, ta.value.length, data.initial) });
      const cancelBtn = button({
        label: t("common.cancel"),
        variant: "secondary",
        onClick: () => {
          closing = "discard";
          m.close();
        },
      });
      const saveBtn = button({ label: t("desc.save"), icon: "save", variant: "primary", onClick: () => save() });

      const m = ctx.modal({
        title: t("desc.title"),
        subtitle: t("desc.sub", { ph: ph.title }),
        width: 780,
        class: "tpl-desc-modal",
        body: [toolbar, editor, legend, problemsEl, flagsEl],
        actions: [resetBtn, cancelBtn, saveBtn],
        onClose: () => {
          desc.open = false;
          check.cancel();
          // Closed with × / Escape / a click outside: the edits wait for the next opening
          // (desc.draft already holds them, see paint).
          if (closing !== null) setDescDraft(null);
          if (ctx.isActive()) renderRight();
        },
      });

      // --- editing

      /** Replace text[a, b) with `value` the way typing would (so Ctrl+Z undoes it). */
      function edit(a, b, value) {
        const before = ta.value;
        ta.focus();
        ta.setSelectionRange(a, b);
        let done = false;
        try {
          done = value ? document.execCommand("insertText", false, value) : document.execCommand("delete");
        } catch {
          done = false;
        }
        if (!done || ta.value === before) {
          ta.setRangeText(value, a, b, "end");
          changed();
        }
      }

      /** The text changed: move the marks along until the server has checked it again. */
      function changed() {
        const before = text;
        text = ta.value;
        if (text === before) return;
        let p = 0;
        const max = Math.min(before.length, text.length);
        while (p < max && before[p] === text[p]) p += 1;
        let s = 0;
        while (s < max - p && before[before.length - 1 - s] === text[text.length - 1 - s]) s += 1;
        const end = before.length - s;
        const delta = text.length - before.length;
        flags = flags
          .filter((f) => f.end < p || f.start > end)
          .map((f) => (f.start > end ? { ...f, start: f.start + delta, end: f.end + delta } : f));
        checking = true;
        paint();
        check();
      }

      const check = debounce(async () => {
        const sent = text;
        try {
          const r = await ctx.api.post("/api/template/description/check", { text: sent }, { signal: ctx.signal });
          if (sent !== text) return; // typed on meanwhile: the next check answers
          flags = r.flags || [];
          unknown = r.unknown || [];
        } catch (err) {
          if (ctx.api.isAbort(err) || sent !== text) return;
          // The marks stay where they were moved to; saving still checks on the server.
        }
        checking = false;
        paint();
      }, 280);

      function removeSentence(f) {
        let a = f.start;
        let b = f.end;
        // The space after it (or before it, at a line's end) goes too.
        if (text[b] === " ") b += 1;
        else if (a > 0 && text[a - 1] === " ") a -= 1;
        const lineStart = text.lastIndexOf("\n", a - 1) + 1;
        let lineEnd = text.indexOf("\n", b);
        if (lineEnd < 0) lineEnd = text.length;
        if (!text.slice(lineStart, a).trim() && !text.slice(b, lineEnd).trim()) {
          // It was the whole line: the line goes, and a paragraph of its own takes one of
          // the blank lines around it along, so no double gap is left.
          a = lineStart;
          b = lineEnd;
          if (b < text.length) b += 1;
          else if (a > 0) a -= 1;
          const blankBefore = a === 0 || (a >= 2 && text[a - 1] === "\n" && text[a - 2] === "\n");
          if (blankBefore && text[b] === "\n") b += 1;
        }
        edit(a, b, "");
      }

      function showSentence(i) {
        const f = flags[i];
        if (!f) return;
        ta.focus();
        ta.setSelectionRange(f.start, f.end);
        const mark = marks.querySelector(`mark[data-i="${i}"]`);
        if (mark) ta.scrollTop = Math.max(0, mark.offsetTop - 28);
        syncScroll();
        caret();
      }

      function syncScroll() {
        backdrop.scrollTop = ta.scrollTop;
        backdrop.scrollLeft = ta.scrollLeft;
      }

      /** The flag the caret sits in, lit in the text and in the list. */
      function caret() {
        const at = ta.selectionStart;
        const next = flags.findIndex((f) => at >= f.start && at <= f.end);
        if (next === active) return;
        active = next;
        for (const node of marks.querySelectorAll("mark")) node.classList.toggle("is-active", Number(node.dataset.i) === active);
        for (const node of flagsEl.querySelectorAll(".tpl-desc-flag")) node.classList.toggle("is-active", Number(node.dataset.i) === active);
      }

      async function save() {
        // The Kaydet button's own rule (paint), so Ctrl+S never "saves" an unchanged text.
        if (saving || ta.value === saved || !ta.value.trim()) return;
        saving = true;
        saveBtn.setLoading(true);
        try {
          const r = await ctx.api.put("/api/template/description", { text: ta.value }, { signal: ctx.signal });
          closing = "saved";
          m.close();
          noteDescState(r);
          ctx.toast(r.custom ? { tone: "success", title: t("desc.saved"), message: t("desc.saved_msg") } : { tone: "success", title: t("desc.reset_done") });
        } catch (err) {
          if (ctx.api.isAbort(err)) return;
          ctx.toast({ tone: "danger", title: t("desc.save_failed"), message: ctx.api.errorText(err, t) });
        } finally {
          saving = false;
          saveBtn.setLoading(false);
        }
      }

      // --- drawing

      function paint() {
        // Unsaved edits are the page's from the first keystroke: leaving asks, closing the
        // dialog keeps them.
        setDescDraft(text !== saved ? text : null);
        if (document.activeElement === ta) {
          const at = ta.selectionStart;
          active = flags.findIndex((f) => at >= f.start && at <= f.end);
        }
        // The marked copy: the text in order, each flag a <mark>, and a trailing space so
        // a final line break keeps its line.
        const nodes = [];
        let at = 0;
        flags.forEach((f, i) => {
          if (f.start < at || f.end > text.length || f.end <= f.start) return;
          if (f.start > at) nodes.push(text.slice(at, f.start));
          nodes.push(h("mark", { class: cx("tpl-desc-mark", i === active && "is-active"), dataset: { i: String(i) } }, text.slice(f.start, f.end)));
          at = f.end;
        });
        nodes.push(text.slice(at), " ");
        mount(marks, nodes);
        syncScroll();

        const dirty = text !== saved;
        mount(
          stateSlot,
          dirty
            ? badge({ text: t("desc.state.unsaved"), tone: "warning", dot: true, size: "sm" })
            : data.custom
              ? badge({ text: t("desc.state.custom"), tone: "success", icon: "check", size: "sm" })
              : badge({ text: t("desc.state.listing"), tone: "neutral", size: "sm" }),
        );
        const n = text.length;
        countEl.textContent = t("desc.chars", { n, count: number(n) });

        const problems = [];
        if (!text.trim()) problems.push(infoNote({ tone: "danger", icon: "alert", text: t("desc.empty") }));
        if (unknown.length) problems.push(infoNote({ tone: "warning", icon: "alert", text: t("desc.unknown", { list: unknown.join(", ") }) }));
        mount(problemsEl, problems);

        mount(flagsEl, flagList());
        resetBtn.setDisabled(text === data.initial);
        saveBtn.setDisabled(!dirty || !text.trim());
      }

      function flagList() {
        if (!flags.length) {
          if (checking) return h("p", { class: "tpl-desc-flags-head is-muted" }, h("span", { class: "tpl-desc-flags-icon" }, icon("loader", { size: 13 })), t("desc.checking"));
          return h("p", { class: "tpl-desc-flags-head is-ok" }, h("span", { class: "tpl-desc-flags-icon" }, icon("check", { size: 13, strokeWidth: 2.6 })), t("desc.flags.none"));
        }
        return [
          h(
            "p",
            { class: "tpl-desc-flags-head is-warning" },
            h("span", { class: "tpl-desc-flags-icon" }, icon("alert", { size: 13 })),
            t("desc.flags.title", { n: flags.length }),
            checking ? h("span", { class: "tpl-desc-checking" }, t("desc.checking")) : null,
          ),
          h(
            "ul",
            { class: "tpl-desc-flag-list" },
            flags.map((f, i) =>
              h(
                "li",
                { class: cx("tpl-desc-flag", i === active && "is-active"), dataset: { i: String(i) } },
                h(
                  "div",
                  { class: "tpl-desc-flag-main" },
                  h("span", { class: "tpl-desc-flag-text", title: f.text }, f.text),
                  h(
                    "span",
                    { class: "tpl-desc-flag-why" },
                    t("desc.flag.message"),
                    " ",
                    h("span", { class: "tpl-desc-flag-words" }, t("desc.flag.words", { words: (f.words || []).join(", ") })),
                  ),
                ),
                h(
                  "div",
                  { class: "tpl-desc-flag-actions" },
                  button({ label: t("desc.flag.show"), icon: "eye", size: "sm", variant: "ghost", onClick: () => showSentence(i) }),
                  button({ label: t("desc.flag.remove"), icon: "trash", size: "sm", variant: "ghost", onClick: () => removeSentence(flags[i]) }),
                ),
              ),
            ),
          ),
        ];
      }

      ta.addEventListener("input", changed);
      ta.addEventListener("scroll", syncScroll);
      for (const type of ["keyup", "click", "select", "focus"]) ta.addEventListener(type, caret);
      ta.addEventListener("keydown", (e) => {
        if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "s") {
          e.preventDefault();
          save();
        }
      });
      // Edits kept from an earlier opening: their marks come from a fresh check.
      if (kept) {
        checking = true;
        check();
      }
      paint();
      requestAnimationFrame(() => {
        ta.setSelectionRange(0, 0);
        ta.scrollTop = 0;
        syncScroll();
      });
    }

    // ------------------------------------------------------------------ data

    async function loadCurrent() {
      try {
        const r = await ctx.api.get("/api/template", null, { signal: ctx.signal });
        st.current = r.template || null;
        st.currentProblem = r.problem || null;
        if (st.current) {
          st.previews.set(st.current.listing_id, st.current);
          if (st.selectedId === null) {
            st.selectedId = st.current.listing_id;
            revealedId = st.current.listing_id; // shown at load: no row-by-row reveal
          }
          pinCurrent();
        }
      } catch (err) {
        if (ctx.api.isAbort(err)) return;
        st.current = null;
        ctx.toast({ tone: "danger", title: t("current_failed"), message: ctx.api.errorText(err, t) });
      }
      renderList();
      renderRight();
    }

    // What the last status check says is missing (null: go and ask the server).
    function setupStepFromStatus() {
      const s = ctx.status();
      if (!s) return null;
      if (s.state === "keys") return "keys";
      if (s.state === "disconnected") return "connect";
      return null;
    }

    async function loadListings(refresh) {
      const hadItems = !!st.items;
      st.listError = null;
      const known = refresh ? null : setupStepFromStatus();
      if (known) {
        st.setupStep = known;
        st.items = null;
        renderList();
        renderRight();
        return;
      }
      if (!hadItems) renderList();
      try {
        const r = await ctx.api.get("/api/template/listings", refresh ? { refresh: 1 } : null, { signal: ctx.signal });
        st.setupStep = null;
        st.items = r.items || [];
        st.count = r.count || st.items.length;
        st.truncated = !!r.truncated;
        st.currency = r.currency || null;
        st.sales = r.sales || null;
        if (refresh) {
          // Everything but the saved template may have changed on Etsy.
          for (const key of [...st.previews.keys()]) if (!isCurrent(key)) st.previews.delete(key);
          if (st.selectedId !== null && !st.previews.has(st.selectedId)) loadPreview(st.selectedId);
          pinCurrent();
        }
      } catch (err) {
        if (ctx.api.isAbort(err)) return;
        if (err.code === "setup_needed") {
          st.setupStep = (err.params && err.params.step) || "keys";
          st.items = null;
        } else if (hadItems) {
          ctx.toast({ tone: "danger", title: t("refresh_failed"), message: ctx.api.errorText(err, t) });
        } else {
          st.listError = err;
        }
      }
      renderList();
      renderRight();
    }

    async function loadPreview(id) {
      st.previewError = null;
      if (st.previews.has(id)) {
        st.previewLoading = false;
        renderRight();
        return;
      }
      const seq = ++previewSeq;
      st.previewLoading = true;
      renderRight();
      try {
        const p = await ctx.api.get(`/api/template/preview/${encodeURIComponent(id)}`, null, { signal: ctx.signal });
        if (seq !== previewSeq) return;
        st.previews.set(id, p);
        if (!findItem(id) && !isCurrent(id)) {
          st.extras.set(id, rowFromSummary(p));
          renderList();
        }
      } catch (err) {
        if (ctx.api.isAbort(err) || seq !== previewSeq) return;
        st.previewError = err;
      }
      st.previewLoading = false;
      renderRight();
    }

    function pinCurrent() {
      st.pinnedId = st.current ? st.current.listing_id : null;
      st.pinnedRow = st.current ? rowFromSummary(st.current) : null;
    }

    function select(id) {
      if (st.selectedId === id && (st.previews.has(id) || st.previewLoading)) return;
      st.selectedId = id;
      renderSelection();
      loadPreview(id);
    }

    async function save() {
      const id = st.selectedId;
      const p = id !== null ? st.previews.get(id) : null;
      if (!p || st.saving) return;
      if (st.current && st.current.listing_id !== id) {
        // Another listing starts from its own description: a saved (or unsaved) description
        // template of the old one goes with it.
        const mine = (st.current.description && st.current.description.custom) || desc.draft !== null;
        const ok = await ctx.confirm({
          title: t("replace.title"),
          message: [t("replace.message", { old: st.current.title, next: p.title }), mine ? ` ${t("desc.replace_note")}` : ""],
          confirmLabel: t("replace.confirm"),
          icon: "file",
        });
        if (!ok || !ctx.isActive()) return;
      }
      st.saving = true;
      renderRight();
      try {
        const r = await ctx.api.post("/api/template", { listing_id: id }, { signal: ctx.signal });
        if (!st.current || st.current.listing_id !== id) setDescDraft(null);
        st.current = r.template;
        st.currentProblem = null;
        st.previews.set(id, r.template);
        // The info images are picked among the saved template's photos: show the new ones.
        if (!info.source || info.source.listing_id !== id) loadInfoSource();
        ctx.toast({
          tone: "success",
          title: t("saved.title"),
          message: listingType(r.template && r.template.listing_type) === "download" ? t("saved.message_digital") : t("saved.message"),
          action: button({ label: t("saved.next"), size: "sm", iconRight: "arrow-right", onClick: () => ctx.navigate("/tasarim-yukle") }),
        });
      } catch (err) {
        if (ctx.api.isAbort(err)) return;
        ctx.toast({ tone: "danger", title: t("save_failed"), message: ctx.api.errorText(err, t) });
      } finally {
        st.saving = false;
      }
      if (!ctx.isActive()) return;
      renderList();
      renderRight();
    }

    // A shop connected in another tab (or the status check finishing) fills the list.
    ctx.onStatus((s, prev) => {
      const was = prev && prev.state;
      if (!s || s.state === was) return;
      if (s.state === "connected" && info.sourceError) loadInfoSource();
      if (s.state === "connected" && (st.setupStep || st.listError)) {
        loadListings(false);
      } else if (st.setupStep) {
        const step = setupStepFromStatus();
        if (step && step !== st.setupStep) {
          st.setupStep = step;
          renderList();
        }
      }
    });

    renderList();
    renderRight();
    renderInfo();
    // Tasarım Yükle's start card sends the seller here for the description (?aciklama=1):
    // the dialog opens as soon as the saved template is known, not after the shop's list.
    const openDesc = !!ctx.query.aciklama;
    if (openDesc) ctx.setQuery({ aciklama: null });
    await Promise.all([
      loadCurrent().then(() => {
        if (openDesc && ctx.isActive()) openDescription();
      }),
      loadListings(false),
      loadInfoSource(),
    ]);
    if (st.selectedId !== null && !st.previews.has(st.selectedId) && !st.previewLoading) loadPreview(st.selectedId);
    return () => {
      if (resizer) resizer.disconnect();
      if (listSizer) listSizer.disconnect();
    };
  },
};
