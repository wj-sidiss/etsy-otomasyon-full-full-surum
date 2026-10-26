// DOM helper + component library. Every component returns a DOM Node; the stateful
// ones (chips, tabs, table, progressBar) return {el, update(...)} as documented.
// Styles live in /css/base.css (class names are prefixed per component).

import { icon } from "./icons.js";
import { t as ct } from "./i18n.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const PROP_KEYS = new Set(["value", "checked", "indeterminate", "selected", "muted", "defaultValue"]);

// ---------------------------------------------------------------- core helpers

/** Class names from strings / arrays / {name: bool} objects. */
export function cx(...args) {
  const out = [];
  for (const a of args) {
    if (!a) continue;
    if (typeof a === "string") out.push(a);
    else if (Array.isArray(a)) out.push(cx(...a));
    else if (typeof a === "object") {
      for (const [k, v] of Object.entries(a)) if (v) out.push(k);
    }
  }
  return out.filter(Boolean).join(" ");
}

function applyProps(el, props) {
  if (!props) return;
  const isSvg = el instanceof SVGElement;
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class" || k === "className") {
      const c = cx(v);
      if (c) el.setAttribute("class", c);
    } else if (k === "style") {
      if (typeof v === "string") el.style.cssText = v;
      else {
        for (const [sk, sv] of Object.entries(v)) {
          if (sv === undefined || sv === null || sv === false) continue;
          if (sk.startsWith("--")) el.style.setProperty(sk, String(sv));
          else el.style[sk] = typeof sv === "number" && !UNITLESS.has(sk) ? `${sv}px` : sv;
        }
      }
    } else if (k === "dataset") {
      for (const [dk, dv] of Object.entries(v)) if (dv !== undefined && dv !== null) el.dataset[dk] = dv;
    } else if (k === "ref") {
      if (typeof v === "function") queueMicrotask(() => v(el));
    } else if (k.length > 2 && k.startsWith("on") && typeof v === "function") {
      el.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (!isSvg && PROP_KEYS.has(k)) {
      el[k] = v;
    } else if (v === true) {
      el.setAttribute(k, "");
    } else {
      el.setAttribute(k, String(v));
    }
  }
}

const UNITLESS = new Set(["opacity", "zIndex", "flex", "flexGrow", "flexShrink", "fontWeight", "lineHeight", "order"]);

function appendChildren(el, children) {
  for (const c of children) {
    if (c === null || c === undefined || c === false || c === true) continue;
    if (Array.isArray(c)) appendChildren(el, c);
    else if (c instanceof Node) el.appendChild(c);
    else if (c && c.el instanceof Node) el.appendChild(c.el);
    else el.appendChild(document.createTextNode(String(c)));
  }
}

/**
 * h(tag, props, ...children) -> HTMLElement.
 * props: class (string|array|object), style (object|string), on<Event> handlers,
 * dataset, ref(el), other attributes (true -> "", false/null -> omitted).
 * children: string | number | Node | {el} | array | null | false.
 */
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  applyProps(el, props);
  appendChildren(el, children);
  return el;
}

/** svg(tag, attrs, ...children) -> SVGElement (same props rules as h). */
export function svg(tag, props, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  applyProps(el, props);
  appendChildren(el, children);
  return el;
}

/** Remove all children. */
export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

/** Replace all children of el. */
export function mount(el, ...children) {
  clear(el);
  appendChildren(el, children);
  return el;
}

let uidCounter = 0;
/** Unique id for aria / label wiring. */
export function uid(prefix = "sk") {
  uidCounter += 1;
  return `${prefix}-${uidCounter}`;
}

/** Debounce a function (ms). The returned function has .cancel(). */
export function debounce(fn, ms = 200) {
  let timer = null;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}

function toNodes(v) {
  if (v === null || v === undefined || v === false) return [];
  if (Array.isArray(v)) return v.flatMap(toNodes);
  if (v instanceof Node) return [v];
  if (v && v.el instanceof Node) return [v.el];
  return [document.createTextNode(String(v))];
}

function iconNode(ic, size = 16) {
  if (!ic) return null;
  if (ic instanceof Node) return ic;
  return icon(ic, { size });
}

function isTypingTarget(el) {
  if (!el || !(el instanceof Element)) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag === "INPUT") {
    const type = (el.getAttribute("type") || "text").toLowerCase();
    return !["checkbox", "radio", "button", "submit", "reset", "range", "color", "file"].includes(type);
  }
  return false;
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusables(root) {
  return [...root.querySelectorAll(FOCUSABLE)].filter(
    (el) => el.offsetParent !== null || el === document.activeElement,
  );
}

// ---------------------------------------------------------------- basics

/** Small inline spinner. spinner({size=16, tone}) */
export function spinner({ size = 16, tone, label } = {}) {
  return h("span", {
    class: cx("spinner", tone && `tone-${tone}`),
    role: "status",
    "aria-label": label || ct("common.loading"),
    style: { width: size, height: size, borderWidth: size >= 24 ? 2.5 : 2 },
  });
}

/**
 * button({label, icon, iconRight, variant, size, onClick, disabled, loading, title, type,
 *         count, block, class, ariaLabel, autoLoading})
 * variant: primary | secondary | ghost | soft | danger | success ; size: sm | md | lg.
 * soft: the video's violet-tinted button (accent-soft fill, lavender text, no border).
 * autoLoading: when onClick returns a promise, show the spinner until it settles.
 * The node gets .setLoading(bool), .setLabel(text), .setDisabled(bool), .setCount(n).
 */
export function button(opts = {}) {
  const {
    label,
    icon: ic,
    iconRight,
    variant = "secondary",
    size = "md",
    onClick,
    disabled = false,
    loading = false,
    title,
    type = "button",
    count,
    block,
    ariaLabel,
    autoLoading = false,
  } = opts;
  const b = h("button", {
    type,
    class: cx("btn", `btn-${variant}`, `btn-${size}`, block && "btn-block", !label && "btn-icon-only", opts.class),
    title,
    "aria-label": ariaLabel || (!label ? title : undefined),
  });
  let isLoading = !!loading;
  let isDisabled = !!disabled;
  let text = label;
  let cnt = count;
  const iconSize = size === "lg" ? 18 : size === "sm" ? 14 : 16;

  function render() {
    const kids = [];
    if (isLoading) kids.push(spinner({ size: iconSize - 2 }));
    else if (ic) kids.push(iconNode(ic, iconSize));
    if (text !== undefined && text !== null && text !== "") kids.push(h("span", { class: "btn-label" }, text));
    if (cnt !== undefined && cnt !== null && cnt !== "") kids.push(h("span", { class: "btn-count num" }, String(cnt)));
    if (iconRight) kids.push(iconNode(iconRight, iconSize));
    mount(b, kids);
    b.disabled = isDisabled || isLoading;
    if (isLoading) b.setAttribute("aria-busy", "true");
    else b.removeAttribute("aria-busy");
  }
  render();

  b.setLoading = (v) => {
    isLoading = !!v;
    render();
  };
  b.setLabel = (v) => {
    text = v;
    render();
  };
  b.setDisabled = (v) => {
    isDisabled = !!v;
    render();
  };
  b.setCount = (v) => {
    cnt = v;
    render();
  };
  if (onClick) {
    b.addEventListener("click", async (e) => {
      if (b.disabled) return;
      const r = onClick(e);
      if (autoLoading && r && typeof r.then === "function") {
        b.setLoading(true);
        try {
          await r;
        } finally {
          b.setLoading(false);
        }
      }
    });
  }
  return b;
}

/** Same as btn.setLoading(v) for any button made by button(). */
export function setLoading(btn, v) {
  if (btn && typeof btn.setLoading === "function") btn.setLoading(v);
}

/**
 * iconButton({icon, onClick, title, badge, variant="secondary", size="md", disabled, active, iconSize})
 * `title` is also the accessible name. Node gets .setBadge(n).
 */
export function iconButton(opts = {}) {
  const { icon: ic, onClick, title, badge: badgeValue, variant = "secondary", size = "md", disabled, active } = opts;
  const b = h("button", {
    type: "button",
    class: cx("btn", `btn-${variant}`, `btn-${size}`, "btn-icon", active && "is-active", opts.class),
    title,
    "aria-label": title,
    disabled: !!disabled,
    onClick,
  });
  b.appendChild(iconNode(ic, opts.iconSize || (size === "lg" ? 18 : size === "sm" ? 14 : 16)));
  const badgeEl = h("span", { class: "btn-badge num", "aria-hidden": "true" });
  b.appendChild(badgeEl);
  b.setBadge = (n) => {
    const show = n !== undefined && n !== null && n !== 0 && n !== "";
    badgeEl.textContent = show ? (typeof n === "number" && n > 99 ? "99+" : String(n)) : "";
    badgeEl.hidden = !show;
  };
  b.setBadge(badgeValue);
  return b;
}

/**
 * badge({text, tone="neutral", dot, icon, iconRight, title, size})
 * tone: neutral | accent | success | warning | danger | info
 */
export function badge({ text, tone = "neutral", dot, icon: ic, iconRight, title, size } = {}) {
  return h(
    "span",
    { class: cx("badge", `tone-${tone}`, size === "sm" && "badge-sm", size === "lg" && "badge-lg"), title },
    dot ? h("span", { class: "dot", "aria-hidden": "true" }) : null,
    ic ? iconNode(ic, 12) : null,
    text !== undefined && text !== null ? h("span", null, text) : null,
    iconRight ? iconNode(iconRight, 12) : null,
  );
}

/** Small coloured dot: dot("success"). */
export function dot(tone = "neutral") {
  return h("span", { class: cx("dot", `tone-${tone}`), "aria-hidden": "true" });
}

/** Keyboard key: kbd("/"). */
export function kbd(text) {
  return h("kbd", { class: "kbd" }, text);
}

/**
 * card({title, subtitle, icon, iconTone="accent", actions, body, footer, class, pad=true, tag})
 * pad: true (20px) | "sm" | "lg" | false. The node exposes .body (the body element).
 */
export function card(opts = {}) {
  const { title, subtitle, icon: ic, iconTone = "accent", actions, body, footer, pad = true, tag = "section" } = opts;
  const padClass = pad === false ? "card-flush" : pad === "sm" ? "card-pad-sm" : pad === "lg" ? "card-pad-lg" : "card-pad";
  const el = h(tag, { class: cx("card", padClass, opts.class) });
  if (title || ic || actions) {
    const titleId = title ? uid("card") : null;
    if (titleId) el.setAttribute("aria-labelledby", titleId);
    el.appendChild(
      h(
        "header",
        { class: "card-head" },
        ic ? h("span", { class: cx("icon-tile", `tone-${iconTone}`) }, iconNode(ic, 16)) : null,
        h(
          "div",
          { class: "card-titles" },
          title ? h("h2", { class: "card-title", id: titleId }, title) : null,
          subtitle ? h("p", { class: "card-sub" }, subtitle) : null,
        ),
        actions ? h("div", { class: "card-actions" }, toNodes(actions)) : null,
      ),
    );
  }
  const bodyEl = h("div", { class: "card-body" }, toNodes(body));
  el.appendChild(bodyEl);
  if (footer) el.appendChild(h("footer", { class: "card-foot" }, toNodes(footer)));
  el.body = bodyEl;
  return el;
}

/** sectionTitle(text, {actions}) -> the small uppercase label ("İSTENEN İZİNLER"). */
export function sectionTitle(text, { actions } = {}) {
  return h(
    "div",
    { class: "section-title" },
    h("h3", null, text),
    actions ? h("div", { class: "section-title-actions" }, toNodes(actions)) : null,
  );
}

/**
 * infoNote({icon="info", text, title, tone="info", action}) - the rounded note boxes.
 * text may be a string, Node or array (e.g. [h("b", null, "Silme izni istemiyoruz"), " — ..."]).
 */
export function infoNote({ icon: ic = "info", text, title, tone = "info", action } = {}) {
  return h(
    "div",
    { class: cx("note", `tone-${tone}`) },
    ic ? h("span", { class: "note-icon" }, iconNode(ic, 15)) : null,
    h("div", { class: "note-text" }, title ? h("strong", null, title, " ") : null, toNodes(text)),
    action ? h("div", { class: "note-action" }, toNodes(action)) : null,
  );
}

/** emptyState({icon, title, message, action, compact}) */
export function emptyState({ icon: ic = "box", title, message, action, compact } = {}) {
  return h(
    "div",
    { class: cx("empty", compact && "empty-compact") },
    ic ? h("span", { class: "empty-icon" }, iconNode(ic, compact ? 18 : 22)) : null,
    title ? h("p", { class: "empty-title" }, title) : null,
    message ? h("p", { class: "empty-msg" }, toNodes(message)) : null,
    action ? h("div", { class: "empty-action" }, toNodes(action)) : null,
  );
}

/** skeleton({lines=3, height=12, widths}) - shimmering placeholder lines. */
export function skeleton({ lines = 3, height = 12, widths, gap = 10 } = {}) {
  const pattern = widths || ["100%", "92%", "38%", "80%", "64%"];
  const el = h("div", { class: "skeleton-group", style: { gap }, "aria-hidden": "true" });
  for (let i = 0; i < lines; i += 1) {
    const w = lines === 1 && !widths ? "100%" : pattern[i % pattern.length];
    el.appendChild(h("span", { class: "skeleton", style: { height, width: w } }));
  }
  return el;
}

/** thumb({src, alt="", size=40, radius, icon="image"}) - rounded image with a fallback. */
export function thumb({ src, alt = "", size = 40, radius, icon: ic = "image", fit = "cover" } = {}) {
  const el = h("span", {
    class: "thumb",
    style: { width: size, height: size, borderRadius: radius !== undefined ? radius : undefined },
  });
  const fallback = () => mount(el, iconNode(ic, Math.max(14, Math.round(size * 0.38))));
  if (src) {
    const img = h("img", { src, alt, loading: "lazy", decoding: "async", style: { objectFit: fit } });
    img.addEventListener("error", fallback, { once: true });
    el.appendChild(img);
  } else {
    fallback();
  }
  return el;
}

// ---------------------------------------------------------------- selection controls

/**
 * chips({items:[{id,label,count}], value, onChange}) -> {el, update(items?, value?)}
 * Single-select filter pills (Tümü 6 · Tişört 2 ...).
 */
export function chips({ items = [], value, onChange, ariaLabel } = {}) {
  const el = h("div", { class: "chips", role: "group", "aria-label": ariaLabel });
  let cur = value;
  let list = items;
  function render() {
    mount(
      el,
      list.map((it) =>
        h(
          "button",
          {
            type: "button",
            class: cx("chip", it.id === cur && "is-active"),
            "aria-pressed": it.id === cur ? "true" : "false",
            disabled: !!it.disabled,
            onClick: () => {
              if (cur === it.id) return;
              cur = it.id;
              render();
              if (onChange) onChange(it.id, it);
            },
          },
          it.icon ? iconNode(it.icon, 14) : null,
          h("span", null, it.label),
          it.count !== undefined && it.count !== null ? h("span", { class: "chip-count num" }, String(it.count)) : null,
        ),
      ),
    );
  }
  render();
  return {
    el,
    update(newItems, newValue) {
      if (newItems) list = newItems;
      if (newValue !== undefined) cur = newValue;
      render();
    },
    get value() {
      return cur;
    },
  };
}

/**
 * tabs({items:[{id,label,count}], value, onChange}) -> {el, update(items?, value?)}
 * Segmented tabs (Taslaklar 50 · Aktif 306 · Hepsi 356). Arrow keys / Home / End move.
 */
export function tabs({ items = [], value, onChange, ariaLabel, size } = {}) {
  const el = h("div", { class: cx("tabs", size === "sm" && "tabs-sm"), role: "tablist", "aria-label": ariaLabel });
  let cur = value;
  let list = items;

  function select(id, focus) {
    const it = list.find((x) => x.id === id);
    if (!it || it.disabled) return;
    const changed = cur !== id;
    cur = id;
    render();
    if (focus) {
      const btn = el.querySelector(`[data-id="${CSS.escape(String(id))}"]`);
      if (btn) btn.focus();
    }
    if (changed && onChange) onChange(id, it);
  }

  function render() {
    mount(
      el,
      list.map((it) =>
        h(
          "button",
          {
            type: "button",
            role: "tab",
            class: cx("tab", it.id === cur && "is-active"),
            "aria-selected": it.id === cur ? "true" : "false",
            tabindex: it.id === cur ? "0" : "-1",
            disabled: !!it.disabled,
            dataset: { id: String(it.id) },
            onClick: () => select(it.id, false),
          },
          it.icon ? iconNode(it.icon, 14) : null,
          h("span", null, it.label),
          it.count !== undefined && it.count !== null ? h("span", { class: "tab-count num" }, String(it.count)) : null,
        ),
      ),
    );
    if (!list.some((x) => x.id === cur)) {
      const first = el.querySelector(".tab");
      if (first) first.setAttribute("tabindex", "0");
    }
  }

  el.addEventListener("keydown", (e) => {
    const enabled = list.filter((x) => !x.disabled);
    if (!enabled.length) return;
    const focusedId = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.id : null;
    let idx = enabled.findIndex((x) => String(x.id) === focusedId);
    if (idx < 0) idx = enabled.findIndex((x) => x.id === cur);
    let next = null;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = enabled[(idx + 1) % enabled.length];
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = enabled[(idx - 1 + enabled.length) % enabled.length];
    else if (e.key === "Home") next = enabled[0];
    else if (e.key === "End") next = enabled[enabled.length - 1];
    if (next) {
      e.preventDefault();
      select(next.id, true);
    }
  });

  render();
  return {
    el,
    update(newItems, newValue) {
      if (newItems) list = newItems;
      if (newValue !== undefined) cur = newValue;
      render();
    },
    get value() {
      return cur;
    },
  };
}

/**
 * searchInput({placeholder, value, onInput, onEnter, shortcut="/", debounce=0})
 * Pressing the shortcut key anywhere (outside a text field) focuses it; Esc clears.
 * The node exposes .input, .value (get/set) and .focus().
 */
export function searchInput(opts = {}) {
  const { placeholder = "", value = "", onInput, onEnter, shortcut = "/", debounce: wait = 0, ariaLabel } = opts;
  const input = h("input", {
    type: "search",
    class: "search-input",
    placeholder,
    value,
    autocomplete: "off",
    spellcheck: "false",
    "aria-label": ariaLabel || placeholder || ct("common.search"),
  });
  const key = shortcut ? kbd(shortcut) : null;
  const wrap = h("label", { class: cx("search", opts.class) }, icon("search", { size: 15 }), input, key);

  const emit = wait > 0 ? debounce((v) => onInput && onInput(v), wait) : (v) => onInput && onInput(v);
  const sync = () => wrap.classList.toggle("has-value", input.value !== "");
  input.addEventListener("input", () => {
    sync();
    emit(input.value);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (input.value) {
        e.preventDefault();
        e.stopPropagation();
        input.value = "";
        sync();
        if (onInput) onInput("");
      } else {
        input.blur();
      }
    } else if (e.key === "Enter" && onEnter) {
      onEnter(input.value);
    }
  });
  sync();

  if (shortcut) {
    const onKey = (e) => {
      if (!input.isConnected) {
        document.removeEventListener("keydown", onKey);
        return;
      }
      if (e.key !== shortcut || e.ctrlKey || e.metaKey || e.altKey) return;
      if (isTypingTarget(e.target) || document.querySelector(".modal-backdrop")) return;
      if (input.offsetParent === null) return;
      e.preventDefault();
      input.focus();
      input.select();
    };
    // Registered after the element is in the page (the first keydown cleans up if never mounted).
    document.addEventListener("keydown", onKey);
  }

  wrap.input = input;
  Object.defineProperty(wrap, "value", {
    get: () => input.value,
    set: (v) => {
      input.value = v ?? "";
      sync();
    },
  });
  wrap.focus = () => input.focus();
  return wrap;
}

// ---------------------------------------------------------------- table

/**
 * table({columns:[{key,label,width,align,render(row,i),class,headerClass}], rows, rowKey="id",
 *        selectable, selected:Set, onSelectionChange(Set), onRowClick(row, event), empty,
 *        loading, rowClass(row), class, checkWidth=48})
 * -> {el, update(rows?, selected?), setLoading(bool), selected (Set)}
 * update(null) shows skeleton rows. Shift+click selects a range. Rows are focusable
 * (Enter = click) when onRowClick is given.
 * selectable: true, or a predicate selectable(row, i) -> bool: rows it refuses get no
 * checkbox and are left out of "select all". checkWidth: the checkbox column in px.
 */
export function table(opts = {}) {
  const {
    columns = [],
    rowKey = "id",
    selectable: selectableOpt = false,
    onSelectionChange,
    onRowClick,
    empty,
    rowClass,
    skeletonRows = 6,
    checkWidth = 48,
  } = opts;
  const selectable = !!selectableOpt;
  const canSelect = typeof selectableOpt === "function" ? (row, i) => !!selectableOpt(row, i) : () => true;
  let rows = opts.rows || [];
  let sel = opts.selected instanceof Set ? opts.selected : new Set(opts.selected || []);
  let loading = !!opts.loading;
  let lastIndex = null;

  const keyOf = (row, i) => {
    const k = typeof rowKey === "function" ? rowKey(row, i) : row && row[rowKey];
    return k === undefined || k === null ? i : k;
  };

  const headCheck = selectable
    ? checkbox({
        ariaLabel: ct("table.select_all"),
        onChange: (checked) => {
          rows.forEach((r, i) => {
            if (!canSelect(r, i)) return;
            if (checked) sel.add(keyOf(r, i));
            else sel.delete(keyOf(r, i));
          });
          renderBody();
          notify();
        },
      })
    : null;

  const colgroup = h(
    "colgroup",
    null,
    selectable ? h("col", { style: { width: checkWidth } }) : null,
    columns.map((c) => h("col", { style: c.width ? { width: typeof c.width === "number" ? `${c.width}px` : c.width } : null })),
  );
  const thead = h(
    "thead",
    null,
    h(
      "tr",
      null,
      selectable ? h("th", { class: "tbl-check", scope: "col" }, headCheck) : null,
      columns.map((c) =>
        h("th", { scope: "col", class: cx(c.align && `align-${c.align}`, c.headerClass) }, c.label ?? ""),
      ),
    ),
  );
  const tbody = h("tbody");
  const tableEl = h("table", { class: "tbl" }, colgroup, thead, tbody);
  const el = h("div", { class: cx("tbl-wrap", opts.class) }, tableEl);

  function notify() {
    updateHeadCheck();
    if (onSelectionChange) onSelectionChange(new Set(sel));
  }

  function updateHeadCheck() {
    if (!headCheck) return;
    const keys = [];
    rows.forEach((r, i) => {
      if (canSelect(r, i)) keys.push(keyOf(r, i));
    });
    const n = keys.filter((k) => sel.has(k)).length;
    headCheck.checked = n > 0 && n === keys.length;
    headCheck.indeterminate = n > 0 && n < keys.length;
    headCheck.disabled = keys.length === 0;
  }

  function renderBody() {
    const colSpan = columns.length + (selectable ? 1 : 0);
    if (loading) {
      const out = [];
      for (let i = 0; i < skeletonRows; i += 1) {
        out.push(
          h(
            "tr",
            { class: "tbl-skel" },
            selectable ? h("td", { class: "tbl-check" }, h("span", { class: "skeleton", style: { width: 18, height: 18 } })) : null,
            columns.map((c, ci) =>
              h(
                "td",
                { class: cx(c.align && `align-${c.align}`) },
                h("span", { class: "skeleton", style: { height: 12, width: ci === 0 ? "70%" : "50%" } }),
              ),
            ),
          ),
        );
      }
      mount(tbody, out);
      updateHeadCheck();
      return;
    }
    if (!rows.length) {
      const emptyNode =
        empty instanceof Node ? empty : emptyState({ icon: "list", title: empty || ct("table.empty"), compact: true });
      mount(tbody, h("tr", { class: "tbl-empty" }, h("td", { colspan: String(colSpan) }, emptyNode)));
      updateHeadCheck();
      return;
    }
    mount(
      tbody,
      rows.map((row, i) => {
        const k = keyOf(row, i);
        const isSel = sel.has(k);
        const tr = h("tr", {
          class: cx(isSel && "is-selected", onRowClick && "is-clickable", rowClass && rowClass(row, i)),
          tabindex: onRowClick ? "0" : undefined,
          dataset: { index: String(i) },
          "aria-selected": selectable ? (isSel ? "true" : "false") : undefined,
        });
        if (selectable && !canSelect(row, i)) {
          tr.appendChild(h("td", { class: "tbl-check is-off", dataset: { noRowClick: "1" } }));
        } else if (selectable) {
          const cb = checkbox({
            checked: isSel,
            ariaLabel: ct("table.select_row"),
            onChange: (checked, e) => {
              if (e && e.shiftKey && lastIndex !== null && lastIndex !== i) {
                const [a, b] = lastIndex < i ? [lastIndex, i] : [i, lastIndex];
                for (let j = a; j <= b; j += 1) {
                  if (!canSelect(rows[j], j)) continue;
                  const kk = keyOf(rows[j], j);
                  if (checked) sel.add(kk);
                  else sel.delete(kk);
                }
                lastIndex = i;
                renderBody();
                notify();
                return;
              }
              lastIndex = i;
              if (checked) sel.add(k);
              else sel.delete(k);
              tr.classList.toggle("is-selected", checked);
              tr.setAttribute("aria-selected", checked ? "true" : "false");
              notify();
            },
          });
          const td = h("td", { class: "tbl-check", dataset: { noRowClick: "1" } }, cb);
          td.addEventListener("click", (e) => {
            if (e.target === td) cb.click();
          });
          tr.appendChild(td);
        }
        for (const c of columns) {
          let content;
          if (c.render) content = c.render(row, i);
          else content = row[c.key] === undefined || row[c.key] === null ? "" : String(row[c.key]);
          tr.appendChild(h("td", { class: cx(c.align && `align-${c.align}`, c.class) }, toNodes(content)));
        }
        return tr;
      }),
    );
    updateHeadCheck();
  }

  if (onRowClick) {
    const handle = (e) => {
      const tr = e.target.closest("tbody tr");
      if (!tr || !tbody.contains(tr) || tr.classList.contains("tbl-empty") || tr.classList.contains("tbl-skel")) return;
      if (e.target.closest("button, a, input, label, select, textarea, [data-no-row-click]")) return;
      const i = Number(tr.dataset.index);
      if (Number.isNaN(i) || !rows[i]) return;
      onRowClick(rows[i], e);
    };
    tbody.addEventListener("click", handle);
    tbody.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target.matches("tr")) handle(e);
    });
  }

  renderBody();
  return {
    el,
    update(newRows, newSelected) {
      if (newRows === null) loading = true;
      else if (newRows !== undefined) {
        loading = false;
        rows = newRows;
      }
      if (newSelected !== undefined) sel = newSelected instanceof Set ? newSelected : new Set(newSelected || []);
      renderBody();
    },
    setLoading(v) {
      loading = !!v;
      renderBody();
    },
    get selected() {
      return new Set(sel);
    },
    get rows() {
      return rows;
    },
  };
}

/**
 * pagination({page, pages, onChange}) -> Node (with .update(page, pages)).
 * Pages are 1-based. Renders ‹ 1 2 3 … 7 › as in the video (t250, t280): every number
 * up to 5 pages; beyond that the first and last page, the current one with its
 * neighbours, and "…" for each run of 2 or more hidden pages.
 */
/** The page numbers pagination() shows: numbers, and null for "…". */
export function pageNumbers(cur, total) {
  if (total <= 5) return Array.from({ length: Math.max(1, total) }, (_, i) => i + 1);
  const set = new Set([1, total, cur - 1, cur, cur + 1]);
  if (cur <= 2) [2, 3].forEach((x) => set.add(x));
  if (cur >= total - 1) [total - 1, total - 2].forEach((x) => set.add(x));
  const sorted = [...set].filter((x) => x >= 1 && x <= total).sort((a, b) => a - b);
  const out = [];
  let prev = 0;
  for (const n of sorted) {
    if (n - prev === 2) out.push(n - 1); // a lone hidden page is shown, not replaced by "…"
    else if (n - prev > 2) out.push(null);
    out.push(n);
    prev = n;
  }
  return out;
}

export function pagination({ page = 1, pages = 1, onChange } = {}) {
  const el = h("nav", { class: "pagination", "aria-label": ct("pagination.label") });
  let cur = page;
  let total = pages;

  function go(p) {
    if (p < 1 || p > total || p === cur) return;
    cur = p;
    render();
    if (onChange) onChange(p);
    const btn = el.querySelector('[aria-current="page"]');
    if (btn && el.contains(document.activeElement)) btn.focus();
  }

  function numbers() {
    return pageNumbers(cur, total);
  }

  function render() {
    const kids = [
      h(
        "button",
        {
          type: "button",
          class: "page-btn page-arrow",
          "aria-label": ct("pagination.prev"),
          disabled: cur <= 1,
          onClick: () => go(cur - 1),
        },
        icon("chevron-left", { size: 15 }),
      ),
    ];
    for (const n of numbers()) {
      if (n === null) kids.push(h("span", { class: "page-gap", "aria-hidden": "true" }, "…"));
      else
        kids.push(
          h(
            "button",
            {
              type: "button",
              class: cx("page-btn num", n === cur && "is-active"),
              "aria-current": n === cur ? "page" : undefined,
              "aria-label": ct("pagination.page", { n }),
              onClick: () => go(n),
            },
            String(n),
          ),
        );
    }
    kids.push(
      h(
        "button",
        {
          type: "button",
          class: "page-btn page-arrow",
          "aria-label": ct("pagination.next"),
          disabled: cur >= total,
          onClick: () => go(cur + 1),
        },
        icon("chevron-right", { size: 15 }),
      ),
    );
    mount(el, kids);
  }
  render();
  el.update = (p, n) => {
    if (n !== undefined) total = Math.max(1, n);
    if (p !== undefined) cur = Math.min(Math.max(1, p), total);
    render();
  };
  return el;
}

// ---------------------------------------------------------------- progress & status

/**
 * stepper({steps:[{label, sub, state:"done|current|todo|error"}]}) -> Node (.update(steps))
 * The horizontal setup steps (Hesap ✓ — 2 Mağaza — 3 Mockuplar — 4 Tasarım yükle).
 */
export function stepper({ steps = [] } = {}) {
  const el = h("ol", { class: "stepper" });
  function render(list) {
    const kids = [];
    list.forEach((s, i) => {
      const state = s.state || "todo";
      const circle =
        state === "done"
          ? icon("check", { size: 14, strokeWidth: 2.4 })
          : state === "error"
            ? icon("x", { size: 14, strokeWidth: 2.4 })
            : String(i + 1);
      kids.push(
        h(
          "li",
          { class: cx("step", `is-${state}`), "aria-current": state === "current" ? "step" : undefined },
          h("span", { class: "step-circle num" }, circle),
          h(
            "span",
            { class: "step-text" },
            h("span", { class: "step-label" }, s.label),
            s.sub ? h("span", { class: "step-sub" }, s.sub) : null,
          ),
        ),
      );
      if (i < list.length - 1) {
        kids.push(h("li", { class: cx("step-line", state === "done" && "is-done"), "aria-hidden": "true" }));
      }
    });
    mount(el, kids);
  }
  render(steps);
  el.update = render;
  return el;
}

function scoreTone(score) {
  if (score === null || score === undefined || Number.isNaN(Number(score))) return "neutral";
  if (score >= 80) return "success";
  if (score >= 60) return "warning";
  return "danger";
}

/**
 * scoreRing({score, size=56, stroke, fontSize}) - 0..100 ring (>=80 success, >=60 warning,
 * else danger). fontSize (px) sets the number's size (default: a third of the ring).
 * score null -> empty ring with "–". Node gets .update(score).
 */
export function scoreRing({ score, size = 56, stroke, fontSize } = {}) {
  const sw = stroke || Math.max(3, Math.round(size / 13));
  const r = (size - sw) / 2;
  const c = 2 * Math.PI * r;
  const el = h("span", { class: "score-ring", role: "img", style: { width: size, height: size } });
  const track = svg("circle", { cx: size / 2, cy: size / 2, r, class: "score-track", "stroke-width": sw });
  const arc = svg("circle", {
    cx: size / 2,
    cy: size / 2,
    r,
    class: "score-arc",
    "stroke-width": sw,
    "stroke-dasharray": `${c} ${c}`,
    transform: `rotate(-90 ${size / 2} ${size / 2})`,
  });
  const inner = svg("circle", { cx: size / 2, cy: size / 2, r: r - sw / 2, class: "score-fill" });
  const label = h("span", { class: "score-num num", style: { fontSize: fontSize || Math.round(size * 0.32) } });
  el.append(svg("svg", { width: size, height: size, viewBox: `0 0 ${size} ${size}`, "aria-hidden": "true" }, inner, track, arc), label);
  el.update = (value) => {
    const has = value !== null && value !== undefined && !Number.isNaN(Number(value));
    const v = has ? Math.max(0, Math.min(100, Math.round(Number(value)))) : null;
    // Only the tone changes: classes a page added (e.g. "seo-ring") stay.
    for (const c of [...el.classList]) if (c.startsWith("tone-")) el.classList.remove(c);
    el.classList.add("score-ring", `tone-${scoreTone(v)}`);
    arc.setAttribute("stroke-dashoffset", String(has ? c * (1 - v / 100) : c));
    arc.style.opacity = has && v > 0 ? "1" : "0";
    label.textContent = has ? String(v) : "–";
    el.setAttribute("aria-label", has ? `${v}/100` : "–");
  };
  el.update(score);
  return el;
}

/**
 * progressBar({value, max=100, tone="accent", size="md", label}) -> {el, update(value, max?)}
 * value null -> indeterminate.
 */
export function progressBar({ value = 0, max = 100, tone = "accent", size = "md", label } = {}) {
  const fill = h("span", { class: "progress-fill" });
  const el = h(
    "div",
    { class: cx("progress", `tone-${tone}`, `progress-${size}`), role: "progressbar", "aria-label": label },
    fill,
  );
  let m = max;
  function update(v, newMax) {
    if (newMax !== undefined) m = newMax;
    if (v === null || v === undefined) {
      el.classList.add("is-indeterminate");
      el.removeAttribute("aria-valuenow");
      fill.style.width = "";
      return;
    }
    el.classList.remove("is-indeterminate");
    const pct = m > 0 ? Math.max(0, Math.min(100, (v / m) * 100)) : 0;
    fill.style.width = `${pct}%`;
    el.setAttribute("aria-valuemin", "0");
    el.setAttribute("aria-valuemax", String(m));
    el.setAttribute("aria-valuenow", String(v));
  }
  update(value);
  return {
    el,
    update,
    setTone(t) {
      el.className = el.className.replace(/tone-\S+/, `tone-${t}`);
    },
  };
}

/**
 * stepDots({states:[...], labels}) - pipeline row: circles joined by lines.
 * states: done | running | todo | warn | error. Node gets .update(states).
 */
export function stepDots({ states = [], labels } = {}) {
  const el = h("div", { class: "stepdots", role: "list" });
  function render(list) {
    el.style.gridTemplateColumns = `repeat(${list.length}, minmax(0, 1fr))`;
    mount(
      el,
      list.map((s, i) => {
        const next = list[i + 1];
        // A step that finished with a warning still finished: the line to the next one is
        // green, as in the video (a warning on Kontrol, then Taslak done).
        const lineDone = (s === "done" || s === "warn") && next && next !== "todo";
        const glyph =
          s === "done"
            ? icon("check", { size: 12, strokeWidth: 2.4 })
            : s === "warn"
              ? icon("alert", { size: 11, strokeWidth: 2.2 })
              : s === "error"
                ? icon("x", { size: 11, strokeWidth: 2.6 })
                : null;
        const name = labels && labels[i] ? `${labels[i]}: ` : "";
        return h(
          "div",
          { class: cx("sd", `is-${s}`, next && "has-next", lineDone && "line-done"), role: "listitem", title: labels ? labels[i] : undefined, "aria-label": name + ct(`stepdots.${s}`) },
          h("span", { class: "sd-dot" }, glyph),
        );
      }),
    );
  }
  render(states);
  el.update = render;
  return el;
}

// ---------------------------------------------------------------- overlays

const modalStack = [];

/**
 * modal({title, subtitle, body, actions, onClose, width=460, dismissible=true, class})
 *  -> {el, close(result)}
 * actions: Nodes, or specs {label, variant, icon, onClick({close}), autoLoading}.
 * Esc / backdrop click close it (when dismissible); focus is trapped and restored.
 */
export function modal(opts = {}) {
  const { title, subtitle, body, actions, onClose, width = 460, dismissible = true } = opts;
  const opener = document.activeElement;
  const titleId = uid("modal-title");
  let closed = false;

  const api = {
    el: null,
    close(result) {
      if (closed) return;
      closed = true;
      const i = modalStack.indexOf(api);
      if (i >= 0) modalStack.splice(i, 1);
      document.removeEventListener("keydown", onKey, true);
      backdrop.classList.add("is-leaving");
      setTimeout(() => backdrop.remove(), 120);
      if (opener && typeof opener.focus === "function" && opener.isConnected) opener.focus();
      if (onClose) onClose(result);
    },
  };

  const actionNodes = (actions || []).map((a) => {
    if (a instanceof Node) return a;
    if (a && a.el instanceof Node) return a.el;
    return button({ ...a, onClick: a.onClick ? (e) => a.onClick({ close: api.close, event: e }) : () => api.close() });
  });

  const closeBtn = dismissible
    ? iconButton({ icon: "x", title: ct("common.close"), variant: "ghost", size: "sm", class: "modal-x", onClick: () => api.close() })
    : null;

  const dialog = h(
    "div",
    {
      class: cx("modal", opts.class),
      role: "dialog",
      "aria-modal": "true",
      "aria-labelledby": title ? titleId : undefined,
      "aria-label": title ? undefined : opts.ariaLabel,
      tabindex: "-1",
      style: { width: typeof width === "number" ? `min(${width}px, calc(100vw - 32px))` : width },
    },
    title || closeBtn
      ? h(
          "header",
          { class: "modal-head" },
          h(
            "div",
            { class: "modal-titles" },
            title ? h("h2", { class: "modal-title", id: titleId }, title) : null,
            subtitle ? h("p", { class: "modal-sub" }, subtitle) : null,
          ),
          closeBtn,
        )
      : null,
    body !== undefined && body !== null ? h("div", { class: "modal-body" }, toNodes(body)) : null,
    actionNodes.length ? h("footer", { class: "modal-actions" }, actionNodes) : null,
  );
  const backdrop = h("div", { class: "modal-backdrop" }, dialog);
  api.el = dialog;

  backdrop.addEventListener("mousedown", (e) => {
    if (e.target === backdrop && dismissible) api.close();
  });

  function onKey(e) {
    if (modalStack[modalStack.length - 1] !== api) return;
    if (e.key === "Escape") {
      if (document.querySelector(".popover")) return; // an open menu/popover closes first
      if (e.target && e.target.matches && e.target.matches(".search-input") && e.target.value) return;
      if (dismissible) {
        e.preventDefault();
        e.stopPropagation();
        api.close();
      }
    } else if (e.key === "Tab") {
      const f = focusables(dialog);
      if (!f.length) {
        e.preventDefault();
        dialog.focus();
        return;
      }
      const first = f[0];
      const last = f[f.length - 1];
      if (e.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
        e.preventDefault();
        first.focus();
      }
    }
  }
  document.addEventListener("keydown", onKey, true);

  document.body.appendChild(backdrop);
  modalStack.push(api);
  requestAnimationFrame(() => {
    const auto = dialog.querySelector("[autofocus]") || dialog.querySelector(".modal-actions .btn-primary, .modal-actions .btn-danger");
    const target = auto || focusables(dialog).find((x) => !x.classList.contains("modal-x")) || dialog;
    target.focus();
  });
  return api;
}

/** Close every open modal (the router calls this when a page unmounts). */
export function closeModals() {
  for (const m of [...modalStack].reverse()) m.close();
}

/**
 * confirm({title, message, body, confirmLabel, cancelLabel, danger, icon, width=440, class})
 *  -> Promise<bool>. Danger confirmations start with focus on Cancel. `width` (px) makes
 * room for a list in `body` (e.g. the orders and tracking numbers about to be sent).
 */
export function confirm({ title, message, confirmLabel, cancelLabel, danger = false, icon: ic, body, width = 440, class: klass } = {}) {
  return new Promise((resolve) => {
    let result = false;
    const cancelBtn = button({ label: cancelLabel || ct("common.cancel"), variant: "secondary", onClick: () => m.close() });
    const okBtn = button({
      label: confirmLabel || ct("common.confirm"),
      variant: danger ? "danger" : "primary",
      icon: ic,
      onClick: () => {
        result = true;
        m.close();
      },
    });
    if (danger) cancelBtn.setAttribute("autofocus", "");
    const m = modal({
      title,
      body: [message ? h("p", { class: "modal-msg" }, toNodes(message)) : null, body || null],
      actions: [cancelBtn, okBtn],
      width,
      class: cx(danger && "modal-danger", klass) || undefined,
      onClose: () => resolve(result),
    });
  });
}

const toastHosts = {};
/** The toasts' corner: "bottom" (bottom right) or "top" (top right, under the header). */
function ensureToastHost(place = "bottom") {
  const known = toastHosts[place];
  if (known && known.isConnected) return known;
  // Two hosts, two landmarks: each has its own name (axe landmark-unique).
  const label = ct(place === "top" ? "toast.region_top" : "toast.region");
  const host = h("div", { class: cx("toasts", place === "top" && "is-top"), role: "region", "aria-live": "polite", "aria-label": label });
  document.body.appendChild(host);
  toastHosts[place] = host;
  return host;
}

const TOAST_ICON = { success: "check", danger: "x", warning: "alert", info: "info", accent: "sparkles" };

/**
 * toast({title, message, tone="info", timeout=5000, action, icon, place}) -> {close, el}
 * tone: success | danger | warning | info | accent. timeout 0 = stays until closed.
 * icon: another icon than the tone's. place: "top" puts it top right, under the header
 * (the video's "Kontrol tamam"); every other toast stays bottom right.
 */
export function toast({ title, message, tone = "info", timeout = 5000, action, icon: ic, place } = {}) {
  const host = ensureToastHost(place === "top" ? "top" : "bottom");
  let timer = null;
  let left = timeout;
  let started = 0;
  const el = h(
    "div",
    { class: cx("toast", `tone-${tone}`), role: tone === "danger" ? "alert" : "status" },
    h("span", { class: "toast-icon" }, icon(ic || TOAST_ICON[tone] || "info", { size: 18, strokeWidth: tone === "success" ? 3 : 2.4 })),
    h(
      "div",
      { class: "toast-text" },
      title ? h("p", { class: "toast-title" }, title) : null,
      message ? h("p", { class: "toast-msg" }, toNodes(message)) : null,
      action ? h("div", { class: "toast-action" }, toNodes(action)) : null,
    ),
    iconButton({ icon: "x", title: ct("common.close"), variant: "ghost", size: "sm", class: "toast-x", onClick: () => close() }),
  );
  function close() {
    clearTimeout(timer);
    if (!el.isConnected) return;
    el.classList.add("is-leaving");
    setTimeout(() => el.remove(), 160);
  }
  function start() {
    if (!left) return;
    started = Date.now();
    timer = setTimeout(close, left);
  }
  el.addEventListener("mouseenter", () => {
    clearTimeout(timer);
    if (started) left = Math.max(1200, left - (Date.now() - started));
  });
  el.addEventListener("mouseleave", start);
  host.appendChild(el);
  while (host.children.length > 4) host.firstElementChild.remove();
  start();
  return { close, el };
}

// ---------------------------------------------------------------- popover & menu

const openPopovers = new Set();

/**
 * popover(anchorEl, content, {placement="bottom-start", width, class, onClose, role})
 *  -> {el, close()}. Opening it again on the same anchor closes it (toggle).
 * Closes on Esc (focus returns to the anchor), outside click, resize and scroll.
 * placement: bottom-start | bottom-end | top-start | top-end (flips when there is no room).
 */
export function popover(anchor, content, opts = {}) {
  if (anchor && anchor.__popover) {
    anchor.__popover.close();
    return null;
  }
  const { placement = "bottom-start", width, onClose, role } = opts;
  const el = h("div", { class: cx("popover", opts.class), role, tabindex: "-1" }, toNodes(content));
  if (width) el.style.width = typeof width === "number" ? `${width}px` : width;
  document.body.appendChild(el);
  let closed = false;

  function position() {
    const r = anchor.getBoundingClientRect();
    const pw = el.offsetWidth;
    const ph = el.offsetHeight;
    const gap = 6;
    const [side, align] = placement.split("-");
    let top;
    const below = r.bottom + gap;
    const above = r.top - gap - ph;
    if (side === "top") top = above >= 8 ? above : below;
    else top = below + ph <= window.innerHeight - 8 || above < 8 ? below : above;
    let left = align === "end" ? r.right - pw : r.left;
    left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
    top = Math.max(8, Math.min(top, window.innerHeight - ph - 8));
    el.style.left = `${Math.round(left)}px`;
    el.style.top = `${Math.round(top)}px`;
  }
  position();

  const onDown = (e) => {
    if (el.contains(e.target) || anchor.contains(e.target)) return;
    close();
  };
  const onKeyDoc = (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close(true);
    }
  };
  const onScroll = (e) => {
    if (el.contains(e.target)) return;
    close();
  };
  const onResize = () => close();
  document.addEventListener("pointerdown", onDown, true);
  document.addEventListener("keydown", onKeyDoc, true);
  window.addEventListener("scroll", onScroll, true);
  window.addEventListener("resize", onResize);

  function close(refocus) {
    if (closed) return;
    closed = true;
    openPopovers.delete(api);
    document.removeEventListener("pointerdown", onDown, true);
    document.removeEventListener("keydown", onKeyDoc, true);
    window.removeEventListener("scroll", onScroll, true);
    window.removeEventListener("resize", onResize);
    el.remove();
    if (anchor) {
      anchor.__popover = null;
      anchor.setAttribute("aria-expanded", "false");
      if (refocus && typeof anchor.focus === "function") anchor.focus();
    }
    if (onClose) onClose();
  }
  const api = { el, close: () => close(false), reposition: position };
  if (anchor) {
    anchor.__popover = api;
    anchor.setAttribute("aria-expanded", "true");
  }
  openPopovers.add(api);
  return api;
}

/** Close every open popover / menu. */
export function closePopovers() {
  for (const p of [...openPopovers]) p.close();
}

/**
 * menu(anchorEl, items:[{label, icon, onClick, danger, divider, checked, checkbox, disabled, hint, header}],
 *      {placement, width}) -> {el, close()}
 * checked: a choice among the items (menuitemradio); with checkbox: true, an on/off switch
 * of its own (menuitemcheckbox).
 * Keyboard: ↑/↓/Home/End move, Enter/Space run, Esc closes and refocuses the anchor.
 */
export function menu(anchor, items = [], opts = {}) {
  if (anchor && anchor.__popover) {
    anchor.__popover.close();
    return null;
  }
  const list = h("div", { class: "menu", role: "menu" });
  let pop = null;
  for (const it of items) {
    if (!it) continue;
    if (it.divider) {
      list.appendChild(h("div", { class: "menu-divider", role: "separator" }));
      continue;
    }
    if (it.header) {
      list.appendChild(h("div", { class: "menu-header", role: "presentation" }, it.header));
      continue;
    }
    const item = h(
      "button",
      {
        type: "button",
        role: it.checkbox ? "menuitemcheckbox" : it.checked !== undefined ? "menuitemradio" : "menuitem",
        "aria-checked": it.checked !== undefined || it.checkbox ? (it.checked ? "true" : "false") : undefined,
        class: cx("menu-item", it.danger && "is-danger", it.checked && "is-checked"),
        disabled: !!it.disabled,
        tabindex: "-1",
        onClick: () => {
          if (pop) pop.close();
          if (it.onClick) it.onClick();
        },
      },
      h("span", { class: "menu-icon" }, it.icon ? iconNode(it.icon, 15) : null),
      h("span", { class: "menu-label" }, it.label),
      it.hint ? h("span", { class: "menu-hint" }, it.hint) : null,
      it.checked ? h("span", { class: "menu-check" }, icon("check", { size: 14, strokeWidth: 2.2 })) : null,
    );
    list.appendChild(item);
  }
  anchor.setAttribute("aria-haspopup", "menu");
  pop = popover(anchor, list, { placement: opts.placement || "bottom-start", width: opts.width, class: cx("menu-pop", opts.class), onClose: opts.onClose });
  if (!pop) return null;
  const itemsEls = () => [...list.querySelectorAll(".menu-item:not(:disabled)")];
  list.addEventListener("keydown", (e) => {
    const els = itemsEls();
    if (!els.length) return;
    const i = els.indexOf(document.activeElement);
    let next = null;
    if (e.key === "ArrowDown") next = els[(i + 1) % els.length];
    else if (e.key === "ArrowUp") next = els[(i - 1 + els.length) % els.length];
    else if (e.key === "Home") next = els[0];
    else if (e.key === "End") next = els[els.length - 1];
    else if (e.key === "Tab") {
      pop.close();
      return;
    }
    if (next) {
      e.preventDefault();
      next.focus();
    }
  });
  requestAnimationFrame(() => {
    const els = itemsEls();
    const checked = els.find((x) => x.classList.contains("is-checked"));
    (checked || els[0] || pop.el).focus();
  });
  return pop;
}

// ---------------------------------------------------------------- form controls

/**
 * field({label, hint, error, input, required, aside, class}) -> Node (.setError(msg), .setHint(msg))
 * The label is wired to the first input/select/textarea inside `input`.
 */
export function field({ label, hint, error, input, required, aside, class: klass } = {}) {
  const control = input instanceof Node ? input : input && input.el;
  const inner =
    control && control.matches && control.matches("input, select, textarea, button")
      ? control
      : control && control.querySelector
        ? control.querySelector("input, select, textarea, button")
        : null;
  const id = inner ? inner.id || uid("field") : null;
  if (inner && !inner.id) inner.id = id;
  const hintEl = h("p", { class: "field-hint", id: uid("hint") });
  const errEl = h("p", { class: "field-error", role: "alert", id: uid("err") });
  const el = h(
    "div",
    { class: cx("field", klass) },
    label || aside
      ? h(
          "div",
          { class: "field-top" },
          label ? h("label", { class: "field-label", for: id || undefined }, label, required ? h("span", { class: "field-req", "aria-hidden": "true" }, " *") : null) : null,
          aside ? h("span", { class: "field-aside" }, toNodes(aside)) : null,
        )
      : null,
    control,
    hintEl,
    errEl,
  );
  function setHint(msg) {
    mount(hintEl, toNodes(msg));
    hintEl.hidden = !msg;
    describe();
  }
  function setError(msg) {
    mount(errEl, toNodes(msg));
    errEl.hidden = !msg;
    el.classList.toggle("has-error", !!msg);
    if (inner) {
      if (msg) inner.setAttribute("aria-invalid", "true");
      else inner.removeAttribute("aria-invalid");
    }
    describe();
  }
  function describe() {
    if (!inner) return;
    const ids = [];
    if (!hintEl.hidden) ids.push(hintEl.id);
    if (!errEl.hidden) ids.push(errEl.id);
    if (ids.length) inner.setAttribute("aria-describedby", ids.join(" "));
    else inner.removeAttribute("aria-describedby");
  }
  setHint(hint);
  setError(error);
  el.setError = setError;
  el.setHint = setHint;
  return el;
}

/**
 * textInput({value, placeholder, type="text", onInput(v,e), onChange(v,e), onEnter(v), mono,
 *            prefix, suffix, icon, disabled, readOnly, name, maxLength, min, max, step,
 *            autofocus, align, size, ariaLabel})
 * -> <input>, or (with prefix/suffix/icon) a wrapper with .input and .value.
 */
export function textInput(opts = {}) {
  const {
    value = "",
    placeholder,
    type = "text",
    onInput,
    onChange,
    onEnter,
    mono,
    prefix,
    suffix,
    icon: ic,
    disabled,
    readOnly,
    name,
    maxLength,
    min,
    max,
    step,
    autofocus,
    align,
    size,
    ariaLabel,
    autocomplete = "off",
    inputmode,
  } = opts;
  const input = h("input", {
    type,
    class: cx("input", mono && "mono", align === "right" && "align-right", size === "sm" && "input-sm", size === "lg" && "input-lg", !(prefix || suffix || ic) && opts.class),
    value: value === null || value === undefined ? "" : String(value),
    placeholder,
    disabled: !!disabled,
    readonly: !!readOnly,
    name,
    maxlength: maxLength,
    min,
    max,
    step,
    autofocus: !!autofocus,
    autocomplete,
    inputmode,
    spellcheck: mono ? "false" : undefined,
    "aria-label": ariaLabel,
  });
  if (onInput) input.addEventListener("input", (e) => onInput(input.value, e));
  if (onChange) input.addEventListener("change", (e) => onChange(input.value, e));
  if (onEnter)
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") onEnter(input.value, e);
    });
  if (!(prefix || suffix || ic)) return input;
  const wrap = h(
    "div",
    { class: cx("input-affix", size === "sm" && "input-sm", size === "lg" && "input-lg", disabled && "is-disabled", opts.class) },
    ic ? h("span", { class: "affix-icon" }, iconNode(ic, 15)) : null,
    prefix ? h("span", { class: "affix affix-pre" }, prefix) : null,
    input,
    suffix ? h("span", { class: "affix affix-suf num" }, suffix) : null,
  );
  wrap.addEventListener("mousedown", (e) => {
    if (e.target !== input) {
      e.preventDefault();
      input.focus();
    }
  });
  wrap.input = input;
  Object.defineProperty(wrap, "value", {
    get: () => input.value,
    set: (v) => {
      input.value = v ?? "";
    },
  });
  wrap.setSuffix = (v) => {
    const s = wrap.querySelector(".affix-suf");
    if (s) s.textContent = v;
  };
  return wrap;
}

/**
 * textArea({value, placeholder, rows=4, onInput(v,e), onChange, mono, maxLength, disabled,
 *           readOnly, autoGrow, ariaLabel}) -> <textarea>
 */
export function textArea(opts = {}) {
  const { value = "", placeholder, rows = 4, onInput, onChange, mono, maxLength, disabled, readOnly, autoGrow, ariaLabel, name } = opts;
  const ta = h("textarea", {
    class: cx("input", "textarea", mono && "mono", opts.class),
    placeholder,
    rows: String(rows),
    maxlength: maxLength,
    disabled: !!disabled,
    readonly: !!readOnly,
    name,
    "aria-label": ariaLabel,
  });
  ta.value = value === null || value === undefined ? "" : String(value);
  const grow = () => {
    if (!autoGrow) return;
    ta.style.height = "auto";
    ta.style.height = `${ta.scrollHeight + 2}px`;
  };
  ta.addEventListener("input", (e) => {
    grow();
    if (onInput) onInput(ta.value, e);
  });
  if (onChange) ta.addEventListener("change", (e) => onChange(ta.value, e));
  if (autoGrow) requestAnimationFrame(grow);
  return ta;
}

/**
 * select({options:[{value,label,disabled}] | ["a","b"], value, onChange(v,e), disabled, prefix,
 *         size, ariaLabel}) -> <select> in a styled wrapper (.select; wrapper.value / .input).
 * prefix renders a muted label inside the box ("Sırala:").
 */
export function select(opts = {}) {
  const { options = [], value, onChange, disabled, prefix, size, ariaLabel, name } = opts;
  const sel = h("select", { class: "select-native", disabled: !!disabled, name, "aria-label": ariaLabel || (typeof prefix === "string" ? prefix : undefined) });
  function setOptions(list) {
    mount(
      sel,
      list.map((o) => {
        const opt = typeof o === "object" && o !== null ? o : { value: o, label: String(o) };
        return h("option", { value: String(opt.value), disabled: !!opt.disabled }, opt.label);
      }),
    );
  }
  setOptions(options);
  if (value !== undefined && value !== null) sel.value = String(value);
  if (onChange) sel.addEventListener("change", (e) => onChange(sel.value, e));
  const wrap = h(
    "div",
    { class: cx("select", size === "sm" && "select-sm", disabled && "is-disabled", opts.class) },
    prefix ? h("span", { class: "select-prefix" }, prefix) : null,
    sel,
    h("span", { class: "select-chev" }, icon("chevron-down", { size: 14 })),
  );
  wrap.input = sel;
  Object.defineProperty(wrap, "value", {
    get: () => sel.value,
    set: (v) => {
      sel.value = String(v);
    },
  });
  wrap.setOptions = (list, v) => {
    setOptions(list);
    if (v !== undefined) sel.value = String(v);
  };
  return wrap;
}

/**
 * toggle({checked, onChange(bool), label, sub, disabled}) -> <button role="switch">
 * Node exposes .checked (get) and .update(bool).
 */
export function toggle({ checked = false, onChange, label, sub, disabled, ariaLabel } = {}) {
  let on = !!checked;
  const b = h(
    "button",
    {
      type: "button",
      role: "switch",
      class: "toggle",
      disabled: !!disabled,
      "aria-label": label ? undefined : ariaLabel,
    },
    h("span", { class: "toggle-track", "aria-hidden": "true" }, h("span", { class: "toggle-thumb" })),
    label || sub
      ? h(
          "span",
          { class: "toggle-text" },
          label ? h("span", { class: "toggle-label" }, label) : null,
          sub ? h("span", { class: "toggle-sub" }, sub) : null,
        )
      : null,
  );
  const sync = () => {
    b.setAttribute("aria-checked", on ? "true" : "false");
    b.classList.toggle("is-on", on);
  };
  sync();
  b.addEventListener("click", () => {
    on = !on;
    sync();
    if (onChange) onChange(on);
  });
  Object.defineProperty(b, "checked", { get: () => on });
  b.update = (v) => {
    on = !!v;
    sync();
  };
  return b;
}

/**
 * checkbox({checked, onChange(bool, event), indeterminate, disabled, label, ariaLabel})
 * -> <input type=checkbox> (or a <label> wrapping it when `label` is given; wrapper.input).
 */
export function checkbox({ checked = false, onChange, indeterminate = false, disabled, label, ariaLabel } = {}) {
  const input = h("input", { type: "checkbox", class: "checkbox", disabled: !!disabled, "aria-label": label ? undefined : ariaLabel });
  input.checked = !!checked;
  input.indeterminate = !!indeterminate;
  let lastEvent = null;
  input.addEventListener("click", (e) => {
    lastEvent = e;
  });
  input.addEventListener("change", () => {
    if (onChange) onChange(input.checked, lastEvent);
    lastEvent = null;
  });
  if (!label) return input;
  const wrap = h("label", { class: cx("check-row", disabled && "is-disabled") }, input, h("span", null, label));
  wrap.input = input;
  Object.defineProperty(wrap, "checked", {
    get: () => input.checked,
    set: (v) => {
      input.checked = !!v;
    },
  });
  return wrap;
}

// ---------------------------------------------------------------- files

function acceptMatcher(accept) {
  if (!accept) return () => true;
  const parts = accept
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
  return (file) => {
    const name = (file.name || "").toLowerCase();
    const type = (file.type || "").toLowerCase();
    return parts.some((p) => {
      if (p.startsWith(".")) return name.endsWith(p);
      if (p.endsWith("/*")) return type.startsWith(p.slice(0, -1));
      return type === p;
    });
  };
}

function readAllEntries(reader) {
  return new Promise((resolve) => {
    const out = [];
    const next = () =>
      reader.readEntries(
        (batch) => {
          if (!batch.length) resolve(out);
          else {
            out.push(...batch);
            next();
          }
        },
        () => resolve(out),
      );
    next();
  });
}

async function walkEntry(entry, prefix, out) {
  if (!entry) return;
  if (entry.isFile) {
    const file = await new Promise((resolve) => entry.file(resolve, () => resolve(null)));
    if (file) out.push({ file, path: prefix + file.name });
  } else if (entry.isDirectory) {
    const entries = await readAllEntries(entry.createReader());
    for (const child of entries) await walkEntry(child, `${prefix}${entry.name}/`, out);
  }
}

/** Files from a drop event, folders included (webkitGetAsEntry). -> [{file, path}] */
export async function filesFromDrop(dataTransfer) {
  const out = [];
  const items = dataTransfer && dataTransfer.items ? [...dataTransfer.items] : [];
  const entries = items
    .filter((it) => it.kind === "file")
    .map((it) => (typeof it.webkitGetAsEntry === "function" ? it.webkitGetAsEntry() : null));
  if (entries.length && entries.every(Boolean)) {
    for (const entry of entries) await walkEntry(entry, "", out);
    return out;
  }
  for (const file of dataTransfer && dataTransfer.files ? [...dataTransfer.files] : []) out.push({ file, path: file.name });
  return out;
}

/**
 * dropzone({title, subtitle, accept, multiple=true, directory=false, onFiles([{file, path}]),
 *           onReject([{file, path}]), icon="upload", content, disabled, compact})
 * Drag & drop (folders included) plus click / Enter / Space to pick. `content` replaces the
 * default inner layout. Node gets .setDisabled(bool), .open() and .deliver(list): files
 * handed over from elsewhere ([{file, path}], e.g. takePendingDrop()) go through the same
 * accept / onReject / onFiles path as a drop.
 */
export function dropzone(opts = {}) {
  const {
    title,
    subtitle,
    accept,
    multiple = true,
    directory = false,
    onFiles,
    onReject,
    icon: ic = "upload",
    content,
    compact,
  } = opts;
  let disabled = !!opts.disabled;
  const matches = acceptMatcher(accept);
  const input = h("input", { type: "file", class: "sr-only", tabindex: "-1", "aria-hidden": "true", accept: accept || undefined, multiple: !!multiple });
  if (directory) {
    input.setAttribute("webkitdirectory", "");
    input.setAttribute("directory", "");
  }
  const el = h(
    "div",
    {
      class: cx("dropzone", compact && "dropzone-compact", opts.class),
      role: "button",
      tabindex: "0",
      "aria-label": [title, subtitle].filter((x) => typeof x === "string").join(". ") || ct("dropzone.title"),
      "aria-disabled": disabled ? "true" : undefined,
    },
    content
      ? toNodes(content)
      : [
          h("span", { class: "dropzone-icon" }, iconNode(ic, compact ? 18 : 24)),
          h("p", { class: "dropzone-title" }, title || ct("dropzone.title")),
          h("p", { class: "dropzone-sub" }, subtitle || ct("dropzone.subtitle")),
        ],
    input,
  );

  function deliver(list) {
    const ok = [];
    const bad = [];
    for (const item of list) {
      const base = (item.path || "").split("/").pop() || "";
      if (base.startsWith(".")) continue; // .DS_Store and friends
      (matches(item.file) ? ok : bad).push(item);
    }
    const chosen = multiple ? ok : ok.slice(0, 1);
    if (bad.length && onReject) onReject(bad);
    if (chosen.length && onFiles) onFiles(chosen);
  }

  const open = () => {
    if (!disabled) input.click();
  };
  el.addEventListener("click", (e) => {
    if (e.target === input) return;
    if (e.target.closest("button, a") && e.target.closest("button, a") !== el) return;
    open();
  });
  el.addEventListener("keydown", (e) => {
    if (e.target !== el) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      open();
    }
  });
  input.addEventListener("change", () => {
    const list = [...(input.files || [])].map((file) => ({ file, path: file.webkitRelativePath || file.name }));
    input.value = "";
    deliver(list);
  });

  let depth = 0;
  el.addEventListener("dragenter", (e) => {
    if (disabled || !hasFiles(e)) return;
    e.preventDefault();
    depth += 1;
    el.classList.add("is-over");
  });
  el.addEventListener("dragover", (e) => {
    if (disabled || !hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });
  el.addEventListener("dragleave", () => {
    depth = Math.max(0, depth - 1);
    if (!depth) el.classList.remove("is-over");
  });
  el.addEventListener("drop", async (e) => {
    if (disabled) return;
    e.preventDefault();
    depth = 0;
    el.classList.remove("is-over");
    el.classList.add("is-reading");
    try {
      deliver(await filesFromDrop(e.dataTransfer));
    } finally {
      el.classList.remove("is-reading");
    }
  });

  el.setDisabled = (v) => {
    disabled = !!v;
    el.classList.toggle("is-disabled", disabled);
    if (disabled) el.setAttribute("aria-disabled", "true");
    else el.removeAttribute("aria-disabled");
  };
  el.setDisabled(disabled);
  el.open = open;
  el.deliver = (list) => {
    if (!disabled && Array.isArray(list) && list.length) deliver(list);
  };
  return el;
}

// Files dropped on one page for another (the Panel's "Tasarım yükle" card hands them to
// Tasarım Yükle): kept here for one navigation, taken once, dropped after a minute.
let pendingDrop = null;

/** Keep dropped files ([{file, path}]) for the next page, which calls takePendingDrop(). */
export function setPendingDrop(list, { ttl = 60000 } = {}) {
  pendingDrop = Array.isArray(list) && list.length ? { list, until: Date.now() + ttl } : null;
}

/** The files another page left for this one ([{file, path}]), or null. Taken once. */
export function takePendingDrop() {
  const p = pendingDrop;
  pendingDrop = null;
  return p && Date.now() <= p.until ? p.list : null;
}

function hasFiles(e) {
  const types = e.dataTransfer && e.dataTransfer.types ? [...e.dataTransfer.types] : [];
  return types.includes("Files");
}

// ---------------------------------------------------------------- data display

/**
 * tagChip({text, count, removable, onRemove, tone="neutral", onClick, selected, dashed, title})
 * An Etsy-tag chip ("retro sunset shirt 18"). No text + dashed -> empty slot placeholder.
 */
export function tagChip({ text, count, removable, onRemove, tone = "neutral", onClick, selected, dashed, title } = {}) {
  const cls = cx("tagchip", `tone-${tone}`, selected && "is-selected", (dashed || !text) && "is-slot", onClick && "is-clickable");
  const kids = [
    text ? h("span", { class: "tagchip-text" }, text) : null,
    count !== undefined && count !== null ? h("span", { class: "tagchip-count num" }, String(count)) : null,
  ];
  let el;
  if (onClick) {
    el = h("button", { type: "button", class: cls, title, "aria-pressed": selected !== undefined ? String(!!selected) : undefined, onClick }, kids);
  } else {
    el = h("span", { class: cls, title, "aria-hidden": !text ? "true" : undefined }, kids);
  }
  if (removable && text) {
    const x = h(
      "button",
      {
        type: "button",
        class: "tagchip-x",
        "aria-label": ct("common.remove_item", { name: text }),
        onClick: (e) => {
          e.stopPropagation();
          if (onRemove) onRemove(text);
        },
      },
      icon("x", { size: 12, strokeWidth: 2.2 }),
    );
    if (onClick) return h("span", { class: "tagchip-group" }, el, x);
    el.appendChild(x);
  }
  return el;
}

/**
 * statCard({icon, label, value, sub, tone="neutral", footer, highlight, delta:{text,tone,icon}})
 * The Kâr-Zarar tiles. Node gets .update({value, sub, footer}).
 */
export function statCard({ icon: ic, label, value, sub, tone = "neutral", footer, highlight, delta } = {}) {
  const valueEl = h("p", { class: "stat-value num" }, value ?? "–");
  const subEl = h("p", { class: "stat-sub num" }, toNodes(sub));
  const footEl = h("div", { class: "stat-foot" }, toNodes(footer));
  subEl.hidden = sub === undefined || sub === null;
  footEl.hidden = footer === undefined || footer === null;
  const el = h(
    "div",
    { class: cx("stat", `tone-${tone}`, highlight && "is-highlight") },
    h(
      "div",
      { class: "stat-head" },
      ic ? h("span", { class: cx("icon-tile", "icon-tile-sm", `tone-${tone}`) }, iconNode(ic, 15)) : null,
      h("span", { class: "stat-label" }, label),
      delta ? h("span", { class: "stat-delta" }, badge({ text: delta.text, tone: delta.tone || "success", icon: delta.icon })) : null,
    ),
    valueEl,
    subEl,
    footEl,
  );
  el.update = ({ value: v, sub: s, footer: f } = {}) => {
    if (v !== undefined) valueEl.textContent = v ?? "–";
    if (s !== undefined) {
      mount(subEl, toNodes(s));
      subEl.hidden = s === null;
    }
    if (f !== undefined) {
      mount(footEl, toNodes(f));
      footEl.hidden = f === null;
    }
  };
  return el;
}

/**
 * barChart({data:[{label,value}], format(v)->string, highlightLast=true, height=220, ariaLabel})
 * Vertical SVG bars with value labels; the last bar is the bright one (as in "Aylık net kâr").
 * Negative values hang below the zero line in the danger colour. Node gets .update(data).
 */
export function barChart({ data = [], format = (v) => String(v), highlightLast = true, height = 220, ariaLabel } = {}) {
  const el = h("div", { class: "barchart", role: "img", style: { height } });
  const gid = uid("bar");
  let rows = data;
  let lastWidth = 0;

  function render() {
    const width = Math.max(120, Math.round(el.clientWidth || 480));
    lastWidth = width;
    const n = Math.max(1, rows.length);
    const values = rows.map((d) => Number(d.value) || 0);
    const maxV = Math.max(0, ...values);
    const minV = Math.min(0, ...values);
    const top = maxV > 0 ? 26 : 8;
    const bottom = 26 + (minV < 0 ? 20 : 0);
    const plotH = Math.max(20, height - top - bottom);
    const span = maxV - minV || 1;
    const zeroY = top + (maxV / span) * plotH;
    const slot = width / n;
    const barW = Math.max(8, Math.min(44, slot * 0.5));
    const svgEl = svg(
      "svg",
      { width, height, viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" },
      svg(
        "defs",
        null,
        svg("linearGradient", { id: `${gid}-hi`, x1: "0", y1: "0", x2: "0", y2: "1" }, svg("stop", { offset: "0", "stop-color": "#a89cff" }), svg("stop", { offset: "1", "stop-color": "#7b6cff" })),
        svg("linearGradient", { id: `${gid}-lo`, x1: "0", y1: "0", x2: "0", y2: "1" }, svg("stop", { offset: "0", "stop-color": "#35325f" }), svg("stop", { offset: "1", "stop-color": "#23213d" })),
      ),
    );
    if (minV < 0) svgEl.appendChild(svg("line", { x1: 0, x2: width, y1: zeroY, y2: zeroY, class: "bar-zero" }));
    rows.forEach((d, i) => {
      const v = values[i];
      const isHi = highlightLast && i === rows.length - 1;
      const cxm = slot * i + slot / 2;
      const hgt = Math.max(v === 0 ? 0 : 3, (Math.abs(v) / span) * plotH);
      const x = cxm - barW / 2;
      const y = v >= 0 ? zeroY - hgt : zeroY;
      const r = Math.min(6, barW / 2, hgt / 2);
      const path =
        v >= 0
          ? `M${x},${y + hgt}V${y + r}Q${x},${y} ${x + r},${y}H${x + barW - r}Q${x + barW},${y} ${x + barW},${y + r}V${y + hgt}Z`
          : `M${x},${y}V${y + hgt - r}Q${x},${y + hgt} ${x + r},${y + hgt}H${x + barW - r}Q${x + barW},${y + hgt} ${x + barW},${y + hgt - r}V${y}Z`;
      const fill = v < 0 ? "var(--danger)" : `url(#${gid}-${isHi ? "hi" : "lo"})`;
      const bar = svg("path", { d: path, fill, class: cx("bar", isHi && "is-hi", v < 0 && "is-neg") });
      bar.appendChild(svg("title", null, `${d.label}: ${format(v)}`));
      svgEl.appendChild(bar);
      const vy = v >= 0 ? y - 8 : y + hgt + 14;
      svgEl.appendChild(svg("text", { x: cxm, y: vy, "text-anchor": "middle", class: cx("bar-value", isHi && "is-hi") }, format(v)));
      svgEl.appendChild(svg("text", { x: cxm, y: height - 6, "text-anchor": "middle", class: cx("bar-label", isHi && "is-hi") }, d.label));
    });
    mount(el, svgEl);
    el.setAttribute("aria-label", ariaLabel || rows.map((d) => `${d.label}: ${format(Number(d.value) || 0)}`).join(", "));
  }

  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(() => {
      if (!el.isConnected) return;
      if (Math.abs((el.clientWidth || 0) - lastWidth) > 1) render();
    });
    ro.observe(el);
  }
  requestAnimationFrame(render);
  el.update = (newData) => {
    rows = newData || [];
    render();
  };
  return el;
}

async function writeClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = h("textarea", { style: "position:fixed;opacity:0;top:0;left:0" });
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch {
      ok = false;
    }
    ta.remove();
    return ok;
  }
}

/** Copy text to the clipboard -> Promise<bool>. */
export const copyText = writeClipboard;

/**
 * copyField({value, mono=true, ariaLabel}) - read-only input + copy button.
 * Node gets .update(value).
 */
export function copyField({ value = "", mono = true, ariaLabel } = {}) {
  const input = h("input", { type: "text", class: cx("input", "copy-input", mono && "mono"), readonly: true, value, "aria-label": ariaLabel });
  input.addEventListener("focus", () => input.select());
  const btn = button({
    label: ct("common.copy"),
    icon: "copy",
    size: "sm",
    variant: "secondary",
    onClick: async () => {
      const ok = await writeClipboard(input.value);
      btn.setLabel(ok ? ct("common.copied") : ct("common.copy_failed"));
      btn.classList.toggle("is-done", ok);
      setTimeout(() => {
        btn.setLabel(ct("common.copy"));
        btn.classList.remove("is-done");
      }, 1600);
    },
  });
  const el = h("div", { class: "copy-field" }, input, btn);
  el.update = (v) => {
    input.value = v ?? "";
  };
  el.input = input;
  return el;
}
