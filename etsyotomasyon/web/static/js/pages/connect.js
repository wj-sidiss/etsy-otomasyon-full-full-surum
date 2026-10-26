// Mağaza Bağlantısı (/kurulum/magaza): the Etsy app keys, connecting the shop, the
// connection's status. The consent tab's landing page (/oauth-done) is drawn by app.js
// without any API call (that tab has no session cookie); it tells this tab through a
// BroadcastChannel, relayed as the local event "oauth-done", and this page re-checks.
//
// The left card follows the setup (the video's MagazaBaglaEkrani): keys missing or
// refused -> how to create the app and the key form (the first run beside the brand
// panel of the video's first screen); keys fine -> "Etsy mağazanızı bağlayın";
// connected -> "Mağaza bağlandı" with the shop row and the granted permissions. The right
// card shows the connection as a diagram (the line fills while connecting) and 3 steps.
//
// Setup has to be foolproof (real reports: Etsy's "The requested redirect URL is not
// permitted" because the callback was never added on Etsy), so:
// - the callback address is shown with a copy button, the exact place on Etsy and its
//   rules, in the how-to and right after the keys are saved;
// - the first connect of a shop waits for "I added the callback" (remembered per shop
//   and address, skippable with a small link), asked in the keys step;
// - GET /api/connect/preflight checks the keys, the callback and its port before Etsy
//   opens, and each problem comes with its fix;
// - while the connect job waits for longer than SLOW_AFTER, "Etsy bir hata mı gösterdi?"
//   lists the errors Etsy's page shows and what to do about each.

import { number } from "../format.js";
import { icon, logoMark } from "../icons.js";
import {
  badge,
  button,
  checkbox,
  copyField,
  copyText,
  cx,
  field,
  h,
  iconButton,
  infoNote,
  mount,
  progressBar,
  sectionTitle,
  skeleton,
  stepper,
  textInput,
} from "../ui.js";

const SELLER_APP_URL = "https://www.etsy.com/developers/register-seller-app";
const YOUR_APPS_URL = "https://www.etsy.com/developers/your-apps";
const DEFAULT_CALLBACK = "http://localhost:3003/oauth/redirect";
const DEFAULT_SCOPES = ["shops_r", "listings_r", "listings_w", "transactions_r", "transactions_w"];

// What each scope lets etsyotomasyon do, in the words of the consent card (3 lines, as in the
// video). shops_r belongs to the listings line: drafts need the shop's shipping profiles,
// and Etsy's own consent screen still names every scope. `chip` is the short form the
// connected card shows. A scope not listed here is shown by its raw name.
const PERMISSIONS = [
  { scopes: ["listings_r", "listings_w", "shops_r"], key: "perm.listings", chip: "chip.listings" },
  { scopes: ["transactions_r"], key: "perm.orders", chip: "chip.orders" },
  { scopes: ["transactions_w"], key: "perm.tracking", chip: "chip.tracking", note: "perm.tracking_note" },
];
const WORDED = new Set(PERMISSIONS.flatMap((p) => p.scopes));

// The connect job's phases, in order, and how far along the bar each one is. As in the
// video, the badge, the bar, the hint and the spinning step change together at thirds:
// step 2 ("Siz izin verirsiniz") spins below 67% (the consent, then the code checked:
// "İzniniz doğrulanıyor…"), step 3 only while the shop is read.
const PHASES = ["starting", "opened", "code_received", "fetching_shop", "done"];
const PHASE_PCT = { starting: 5, opened: 16, code_received: 64, fetching_shop: 88, done: 100 };
const CREEP_TO = 62; // while waiting for the person, the bar creeps towards this (< code_received)
const TERMINAL = new Set(["done", "error", "cancelled"]);
const SLOW_AFTER = 15; // seconds on Etsy's page before "Etsy bir hata mı gösterdi?" appears
// A connect that ended with one of these gets the same help, open.
const TROUBLE_AFTER = new Set(["connect_timeout", "token_refused", "state_mismatch"]);

/**
 * The connected card's "Devam" from /api/status's `setup`: the first setup step still to
 * do (mockups, then the template listing), else Tasarım Yükle. `key` ends the string key
 * (connected.next, connected.next_template, connected.next_upload and their _hint).
 */
export function nextSetupStep(su) {
  const s = su || {};
  if (!(s.mockups > 0)) return { key: "", path: "/kurulum/mockuplar" };
  if (!s.template) return { key: "_template", path: "/kurulum/sablon" };
  return { key: "_upload", path: "/tasarim-yukle" };
}

export default {
  async mount(el, ctx) {
    return mountConnect(el, ctx);
  },
};

// ------------------------------------------------------------------ pasted keys
// The same cleaning the server does (api/connect.py clean_keys), run on paste so the
// fields show what will be saved: invisible characters, quotes and labels go, and
// "keystring:secret" or a labelled two-line copy fills both fields.

const INVISIBLE = /[\u200b\u200c\u200d\u2060\ufeff\u00ad]/g;
const EDGE = "\"'`\u201c\u201d\u2018\u2019\u201e\u00ab\u00bb\u2039\u203a<>[](){},;. \t";
const LABEL = /^(?:etsy\s+)?(key\s*string|x-api-key|api\s*key|client[\s_-]*id|shared\s*secret|secret)(?![a-z0-9])\s*[:=]?\s*(.*)$/i;

function cleanText(value) {
  const v = String(value || "")
    .replace(INVISIBLE, "")
    .replace(/[\u00a0\u202f]/g, " ")
    .trim();
  let a = 0;
  let b = v.length;
  while (a < b && EDGE.includes(v[a])) a++;
  while (b > a && EDGE.includes(v[b - 1])) b--;
  return v.slice(a, b).trim();
}

function keyPairs(role, value) {
  const i = value.indexOf(":");
  if (i >= 0) return [["key", cleanText(value.slice(0, i))], ["secret", cleanText(value.slice(i + 1))]];
  return [[role, value]];
}

/** [["key"|"secret", value], ...] found in one field; `role` is the field's own. */
function keyPieces(text, role) {
  const found = [];
  const plain = [];
  let waiting = "";
  for (const raw of String(text || "").replace(INVISIBLE, "").split(/\r\n|\r|\n/)) {
    const line = cleanText(raw);
    if (!line) continue;
    const m = LABEL.exec(line);
    if (m) {
      const labelRole = /secret/i.test(m[1]) ? "secret" : "key";
      const rest = cleanText(m[2]);
      if (rest) found.push(...keyPairs(labelRole, rest));
      else waiting = labelRole;
    } else if (waiting) {
      found.push(...keyPairs(waiting, line));
      waiting = "";
    } else {
      plain.push(line);
    }
  }
  if (plain.length === 2 && !found.length && !plain.join("").includes(":")) {
    found.push(["key", plain[0]], ["secret", plain[1]]);
  } else if (plain.length) {
    found.push(...keyPairs(role, plain.join(" ")));
  }
  return found.filter(([, v]) => v);
}

// ------------------------------------------------------------------ shared pieces

function permissionRows(t, scopes, extra) {
  const set = new Set(scopes);
  const rows = [];
  for (const p of PERMISSIONS) {
    if (p.scopes.some((s) => set.has(s))) rows.push({ text: t(p.key), note: p.note ? t(p.note) : null, isNew: p.scopes.some((s) => extra.includes(s)) });
  }
  for (const s of scopes) if (!WORDED.has(s)) rows.push({ raw: s, isNew: extra.includes(s) });
  return rows;
}

function permList(t, rows) {
  return h(
    "ul",
    { class: "cx-perms" },
    rows.map((r) =>
      h(
        "li",
        // The line reads as in the video; the note stays for screen readers and on hover.
        { class: cx("cx-perm", r.isNew && "is-new"), title: r.note || undefined },
        h("span", { class: "cx-perm-check", "aria-hidden": "true" }, icon("check", { size: 13, strokeWidth: 2.4 })),
        h(
          "span",
          { class: "cx-perm-text" },
          r.raw ? h("code", { class: "cx-scope mono" }, r.raw) : r.text,
          r.note ? h("span", { class: "sr-only" }, " " + r.note) : null,
        ),
        r.isNew ? badge({ text: t("connect.extra_badge"), tone: "accent", size: "sm" }) : null,
      ),
    ),
  );
}

function safeNote(t) {
  return infoNote({
    icon: "shield-check",
    tone: "info",
    text: [h("b", null, t("perm.safe_strong")), " ", t("perm.safe_rest")],
  });
}

/** The granted permissions as the connected card's row of badges (the video's 4 chips). */
function permChips(t, granted) {
  const set = new Set(granted);
  const chips = [];
  for (const p of PERMISSIONS) {
    if (!p.scopes.some((s) => set.has(s))) continue;
    chips.push(badge({ text: t(p.chip), tone: "success", icon: "check", title: p.note ? t(p.note) : undefined }));
  }
  for (const s of granted) if (!WORDED.has(s)) chips.push(badge({ text: s, tone: "neutral" }));
  // Only true while Etsy has not granted the delete scope (?scope=listings_d can ask for it).
  if (!set.has("listings_d")) chips.push(badge({ text: t("chip.no_delete"), tone: "info", icon: "lock" }));
  return h("div", { class: "cx-chips" }, chips);
}

/** "· 306 ilan" with the number in bold (a count from /api/connect/counts). */
function countText(t, key, n) {
  const text = t(key, { n });
  const raw = String(n);
  const i = text.indexOf(raw);
  const sep = h("span", { class: "cx-sep", "aria-hidden": "true" }, "·");
  if (i < 0) return h("span", { class: "cx-count" }, sep, text);
  return h("span", { class: "cx-count" }, sep, text.slice(0, i), h("b", { class: "num" }, number(n)), text.slice(i + raw.length));
}

function extLink(href, label, variant = "secondary") {
  return h(
    "a",
    { href, target: "_blank", rel: "noopener noreferrer", class: cx("btn", `btn-${variant}`, "btn-sm", "cx-ext") },
    h("span", { class: "btn-label" }, label),
    icon("external", { size: 13 }),
  );
}

const BRAND_BENEFITS = [
  ["image", "brand.b1"],
  ["file", "brand.b2"],
  ["chart", "brand.b3"],
];

/** The video's first screen (Kayıt) brand panel, beside the first run's key form. */
function brandPanel(t) {
  // Each arrow stays with the pill after it, so a wrapped strip never ends on an arrow.
  const flow = [];
  for (let i = 1; i <= 4; i++) {
    flow.push(
      h(
        "li",
        { class: "cx-brand-step" },
        i > 1 ? h("span", { class: "cx-brand-arrow", "aria-hidden": "true" }, icon("arrow-right", { size: 14 })) : null,
        h("span", { class: cx("cx-brand-pill", i === 4 && "is-last") }, t(`brand.flow.${i}`)),
      ),
    );
  }
  return h(
    "aside",
    { class: "cx-brand", "aria-label": t("brand.label") },
    h(
      "div",
      { class: "cx-brand-body" },
      h(
        "h2",
        { class: "cx-brand-title" },
        h("span", null, t("brand.title_1")),
        h("span", { class: "cx-brand-grad" }, h("span", null, t("brand.title_2")), h("span", null, t("brand.title_3"))),
      ),
      h("p", { class: "cx-brand-sub" }, t("brand.sub")),
      h(
        "ul",
        { class: "cx-brand-list" },
        BRAND_BENEFITS.map(([ic, key]) =>
          h(
            "li",
            { class: "cx-brand-item" },
            h("span", { class: "cx-brand-icon", "aria-hidden": "true" }, icon(ic, { size: 21 })),
            h("span", { class: "cx-brand-text" }, h("b", null, t(`${key}.title`)), h("span", null, t(`${key}.sub`))),
          ),
        ),
      ),
    ),
    h("ol", { class: "cx-brand-flow", "aria-label": t("brand.flow_label") }, flow),
  );
}

function head(iconName, title, tone = "accent") {
  return h(
    "div",
    { class: "cx-head" },
    h("span", { class: cx("cx-tile", `tone-${tone}`), "aria-hidden": "true" }, icon(iconName, { size: 20 })),
    h("h2", { class: "cx-title" }, title),
  );
}

/** What Etsy checks in a callback address, read from the one in use. */
function callbackParts(uri) {
  try {
    const u = new URL(uri);
    return {
      http: u.protocol === "http:",
      localhost: u.hostname.toLowerCase() === "localhost",
      port: u.port || (u.protocol === "http:" ? "80" : ""),
      slash: uri.endsWith("/"),
    };
  } catch {
    return { http: false, localhost: false, port: "", slash: false };
  }
}

// ------------------------------------------------------------------ /kurulum/magaza

async function mountConnect(el, ctx) {
  const t = ctx.t;
  const s = {
    status: ctx.status(),
    info: null,
    infoError: null,
    mode: null,
    // ?keys=1 (Ayarlar → "Anahtarları değiştir") opens the key form
    editKeys: !!ctx.query.keys,
    keyResult: null, // the last POST /api/connect/keys answer, shown once
    conn: null, // a connect in progress: {jobId, phase, pct, url, popup, blocked, since, slow}
    connError: null, // why the last connect failed (ApiError or job.error)
    extra: /^[a-z_]{3,32}$/.test(ctx.query.scope || "") ? ctx.query.scope : null,
    // "Callback adresini Etsy'ye eklediniz mi?": asked in the keys step right after keys
    // Etsy accepts were saved (keysConfirm); a shop whose keys were saved before that
    // gets one checkbox line above the connect button (gateAsk, kept for the visit); the
    // full box opens on demand ("Etsy'de nasıl eklenir?", gateOpen).
    keysConfirm: false,
    afterSave: false,
    gateAsk: null,
    gateOpen: false,
    pre: null, // the last pre-flight: {loading, data, error}
    troubleOpen: null, // the person opened/closed the help panel (null: not touched)
    counts: null, // {listings, to_ship} from /api/connect/counts, for the shop row
    announceDone: false, // a connect just ended here: focus goes to "Mağaza bağlandı"
  };
  if (ctx.query.keys) ctx.setQuery({ keys: null });
  let creepTimer = null;
  let pollTimer = null;
  let slowTimer = null;
  let preSeq = 0;
  let countsSeq = 0;
  let form = null; // the key form's nodes, kept so typing survives re-renders
  const live = { gateChecks: [] }; // nodes updated in place (progress, connect button, pre-flight, help)

  // ---- layout
  const steps = stepper({ steps: [] });
  steps.classList.add("cx-stepper");
  // A linked step: steps 3 and 4 lead to their pages; a finished "Hesap" opens the key
  // form (the page's way to change the keys, as the video's status card has no links).
  function followStep(li) {
    if (li.dataset.action === "keys") {
      openKeys();
      if (form && form.key.isConnected) form.key.focus({ preventScroll: true });
    } else if (li.dataset.href) {
      ctx.navigate(li.dataset.href);
    }
  }
  steps.addEventListener("click", (e) => {
    const li = e.target.closest(".step.is-link");
    if (li) followStep(li);
  });
  steps.addEventListener("keydown", (e) => {
    const li = e.target.closest(".step.is-link");
    if (li && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      followStep(li);
    }
  });
  const main = h("section", { class: "card cx-main" });
  const side = buildSide();
  const brand = brandPanel(t); // shown on the first run only (css: wide windows)
  const grid = h("div", { class: "cx-grid" }, brand, main, side.el);
  el.append(steps, grid);
  // The brand panel is as tall as the window below the stepper.
  let stepsObserver = null;
  if (typeof ResizeObserver === "function") {
    stepsObserver = new ResizeObserver(() => el.style.setProperty("--cx-steps-h", `${steps.offsetHeight}px`));
    stepsObserver.observe(steps);
  }

  // ---- state helpers
  const state = () => (s.status && s.status.state) || "checking";
  const setup = () => (s.status && s.status.setup) || {};
  const connecting = () => !!(s.conn && s.conn.active);
  const callbackUri = () => (s.info && s.info.redirect_uri) || DEFAULT_CALLBACK;
  const callbackPort = () => (s.info && s.info.callback_port) || callbackParts(callbackUri()).port || "3003";

  function computeMode() {
    if (connecting()) return "connect";
    const st = state();
    if (!s.info) return s.infoError ? "error" : "loading";
    if (s.editKeys || s.keysConfirm) return "keys";
    if (st === "keys" || st === "bad_keys" || !s.info.keys) return "keys";
    if (st === "connected") return "connected";
    if (st === "disconnected" || st === "reconnect") return "connect";
    // checking / offline / error: go by what is saved on this computer
    return s.info.connected ? "connected" : "connect";
  }

  function shopName() {
    return (s.status && s.status.shop && s.status.shop.name) || (s.info && s.info.shop_name) || null;
  }

  /** The shop's name, or before the first connect the sidebar shop card's label
   *  ("Mağaza 1"): the video shows one name in both places. */
  function shownShopName() {
    const label = typeof ctx.shopLabel === "function" ? ctx.shopLabel() : null;
    return shopName() || label || t("diagram.shop");
  }

  // ---- stepper
  function stepperSteps() {
    const st = state();
    const su = setup();
    const known = st !== "checking" || !!s.info;
    const keysOk = known && (s.info ? s.info.keys : su.keys) && st !== "keys" && st !== "bad_keys";
    const shopOk = st === "connected" || (["checking", "offline", "error"].includes(st) && !!(s.info && s.info.connected));
    const mockups = su.mockups || 0;
    const list = [
      { label: t("steps.account"), state: st === "bad_keys" ? "error" : keysOk ? "done" : "todo", doneSub: t("steps.done"), errSub: t("steps.bad_keys") },
      { label: t("steps.shop"), state: st === "reconnect" ? "error" : shopOk ? "done" : "todo", doneSub: t("steps.done"), errSub: t("steps.reconnect") },
      { label: t("steps.mockups"), state: mockups > 0 ? "done" : "todo", doneSub: t("steps.done") },
      { label: t("steps.upload"), state: su.template ? "done" : "todo", doneSub: t("steps.done") },
    ];
    const current = list.findIndex((x) => x.state !== "done");
    if (current >= 0 && list[current].state === "todo") list[current].state = "current";
    return list.map((x) => ({
      label: x.label,
      state: x.state,
      sub: x.state === "done" ? x.doneSub : x.state === "error" ? x.errSub : x.state === "current" ? t("steps.current") : t("steps.todo"),
    }));
  }

  function renderSteps() {
    const list = stepperSteps();
    steps.update(list);
    const links = [null, null, "/kurulum/mockuplar", setup().template ? "/tasarim-yukle" : "/kurulum/sablon"];
    const mode = computeMode();
    const keysLink = list[0].state === "done" && !connecting() && (mode === "connect" || mode === "connected");
    steps.querySelectorAll(".step").forEach((li, i) => {
      if (i === 0 && keysLink) {
        li.dataset.action = "keys";
        li.title = t("keys.edit");
      } else if (links[i]) {
        li.dataset.href = links[i];
      } else {
        return;
      }
      li.classList.add("is-link");
      li.tabIndex = 0;
      li.setAttribute("role", "link");
    });
  }

  // ---- the right card
  function buildSide() {
    const badgeSlot = h("span", { class: "cx-side-badge" });
    const lock = h("span", { class: "cx-link-lock", title: t("diagram.lock") }, icon("lock", { size: 16, strokeWidth: 2 }));
    const nameEl = h("b", { class: "cx-node-name" });
    // The link: a dashed rail from tile to tile (under both), the fill that grows with the
    // connect's progress, the dot riding its end, and the lock in the middle.
    const diagram = h(
      "div",
      { class: "cx-diagram" },
      h(
        "div",
        { class: "cx-node" },
        h("span", { class: "cx-node-tile is-app" }, logoMark({ size: 54 })),
        h("b", { class: "cx-node-name" }, t("app.name")),
        h("span", { class: "cx-node-sub" }, t("diagram.app_sub")),
      ),
      h(
        "div",
        { class: "cx-link", "aria-hidden": "true" },
        h("span", { class: "cx-link-rail" }, h("span", { class: "cx-link-fill" }), h("span", { class: "cx-link-dot" })),
        lock,
      ),
      h(
        "div",
        { class: "cx-node" },
        h("span", { class: "cx-node-tile is-shop" }, icon("store", { size: 44, strokeWidth: 1.6 })),
        nameEl,
        h("span", { class: "cx-node-sub" }, t("diagram.shop_sub")),
      ),
    );
    const flow = h("ol", { class: "cx-flow" });
    const card = h(
      "section",
      { class: "card cx-side" },
      h("header", { class: "cx-side-head" }, h("h2", { class: "cx-side-title" }, t("status.title")), badgeSlot),
      diagram,
      flow,
      // One line in every state, as in the video: the footer never moves.
      h("footer", { class: "cx-side-foot" }, h("p", { class: "cx-side-note" }, icon("info", { size: 14 }), h("span", null, t("status.footer")))),
    );
    return { el: card, badgeSlot, lock, nameEl, diagram, flow };
  }

  function openKeys() {
    s.editKeys = true;
    s.keyResult = null;
    s.connError = null;
    render(true);
  }

  function openGate() {
    s.gateOpen = true;
    render(true);
    const g = main.querySelector(".cx-gate");
    if (g) g.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function flowStates() {
    if (connecting()) {
      const p = s.conn.phase;
      if (p === "starting") return ["running", "todo", "todo"];
      // The code exchanged for a token is still "Siz izin verirsiniz" (the hint reads
      // "İzniniz doğrulanıyor…"); step 3 spins only with "Mağaza bilgileri alınıyor…".
      if (p === "opened" || p === "code_received") return ["done", "running", "todo"];
      if (p === "done") return ["done", "done", "done"];
      return ["done", "done", "running"];
    }
    if (state() === "connected" || computeMode() === "connected") return ["done", "done", "done"];
    return ["todo", "todo", "todo"];
  }

  function renderSide() {
    const st = state();
    let b;
    if (connecting()) b = badge({ text: t("status.connecting", { pct: Math.round(s.conn.pct) }), tone: "accent" });
    else if (st === "connected") b = badge({ text: t("status.connected"), tone: "success", icon: "check" });
    else if (st === "reconnect") b = badge({ text: t("status.reconnect"), tone: "warning", dot: true });
    else if (st === "bad_keys") b = badge({ text: t("status.bad_keys"), tone: "danger", dot: true });
    else if (st === "offline" || st === "error") b = badge({ text: t("status.offline"), tone: "warning", dot: true });
    else if (st === "checking") b = badge({ text: t("status.checking"), tone: "neutral" });
    else b = badge({ text: t("status.not_connected"), tone: "neutral" });
    mount(side.badgeSlot, b);

    const linked = st === "connected" && !connecting();
    const pct = connecting() ? Math.round(s.conn.pct) : linked ? 100 : 0;
    side.diagram.className = cx("cx-diagram", connecting() && "is-connecting", linked && "is-connected", connecting() && pct >= 50 && "is-half");
    side.diagram.style.setProperty("--p", String(pct));
    side.nameEl.textContent = shownShopName();
    side.nameEl.title = side.nameEl.textContent;

    const states = flowStates();
    mount(
      side.flow,
      [1, 2, 3].map((n, i) => {
        const st2 = states[i];
        const marker =
          st2 === "running"
            ? h("span", { class: "cx-flow-ring", role: "img", "aria-label": t("common.loading") })
            : st2 === "done"
              ? icon("check", { size: 14, strokeWidth: 2.6 })
              : String(n);
        return h(
          "li",
          { class: cx("cx-flow-step", `is-${st2}`) },
          h("span", { class: "cx-flow-mark num" }, marker),
          h("span", { class: "cx-flow-text" }, h("b", null, t(`flow.${n}.title`)), h("span", null, t(`flow.${n}.sub`))),
        );
      }),
    );
  }

  // ---- the callback address: copy it, where it goes on Etsy, Etsy's rules
  // dense (the question before the first Bağlan): the same facts in fewer lines, so the
  // question and the button fit on screen - no "Etsy'de" label, the rules as one line
  // of small chips, the approval note in its short form.
  function callbackGuide({ compact = false, dense = false } = {}) {
    const uri = callbackUri();
    const parts = callbackParts(uri);
    const copy = copyField({ value: uri, ariaLabel: t("cb.title") });
    copy.classList.add("cx-cb-copy");
    const rules = [
      [parts.http, t("cb.rule.http")],
      [parts.localhost, t("cb.rule.localhost")],
      [!!parts.port, t("cb.rule.port", { port: parts.port || "?" })],
      [true, parts.slash ? t("cb.rule.exact") : t("cb.rule.slash")],
    ];
    return h(
      "div",
      { class: cx("cx-cb", (compact || dense) && "is-compact", dense && "is-dense") },
      compact || dense ? null : h("div", { class: "cx-cb-head" }, h("b", null, t("cb.title")), h("span", null, t("cb.sub"))),
      copy,
      h(
        "div",
        { class: "cx-cb-where" },
        dense ? null : h("span", { class: "cx-cb-label" }, t("cb.path_label")),
        h(
          "ol",
          { class: "cx-cb-path" },
          h("li", null, h("a", { href: YOUR_APPS_URL, target: "_blank", rel: "noopener noreferrer", class: "cx-cb-apps" }, "Your apps", icon("external", { size: 11 }))),
          h("li", null, t("cb.path.menu_pre"), h("span", { class: "cx-kbd", "aria-label": t("cb.menu_label") }, "⋮"), t("cb.path.menu_post")),
          h("li", null, h("b", null, "Edit callback URLs")),
          h("li", null, t("cb.path.paste"), " ", h("b", null, "Save")),
        ),
      ),
      h(
        "div",
        { class: "cx-cb-foot" },
        h(
          "ul",
          { class: "cx-cb-rules", "aria-label": t("cb.rules_label") },
          rules.map(([ok, text]) => h("li", { class: cx("cx-cb-rule", !ok && "is-bad") }, icon(ok ? "check" : "x", { size: 12, strokeWidth: 2.4 }), h("span", null, text))),
        ),
        h("p", { class: "cx-cb-note" }, icon("info", { size: 13 }), h("span", null, t(dense ? "cb.approval_short" : "cb.approval"))),
      ),
    );
  }

  // ---- buttons the notes and the help panel share
  function editKeysButton(label = t("keys.edit")) {
    return button({
      label,
      size: "sm",
      icon: "key",
      onClick: () => {
        if (connecting()) cancelConnect();
        openKeys();
      },
    });
  }

  function changeCallbackButton(suggested) {
    return button({
      label: t("connect.fix_callback"),
      size: "sm",
      onClick: () => {
        if (connecting()) cancelConnect();
        s.editKeys = true;
        s.connError = null;
        render(true);
        if (form) {
          if (suggested) form.cb.value = suggested;
          form.adv.open = true;
          form.cb.focus();
          form.cb.select();
        }
      },
    });
  }

  function recheckButton() {
    return button({
      label: t("pre.recheck"),
      icon: "refresh",
      size: "sm",
      autoLoading: true,
      onClick: () => {
        s.connError = null;
        return runPreflight(true);
      },
    });
  }

  /** One problem (a pre-flight check or a refused /start) with the way to fix it. */
  function problemNote(err) {
    const code = err.code;
    const text = ctx.api.errorText(err, t);
    let tone = "danger";
    let ic = "alert-circle";
    let action = null;
    if (code === "callback_not_local" || code === "bad_redirect") {
      action = changeCallbackButton(code === "callback_not_local" ? (err.params && err.params.suggested) || DEFAULT_CALLBACK : null);
    } else if (code === "keys_rejected" || code === "bad_keys" || code === "setup_needed") {
      action = editKeysButton();
    } else if (code === "port_in_use") {
      action = [recheckButton(), changeCallbackButton()];
    } else if (code === "offline" || code === "network") {
      tone = "warning";
      ic = "alert";
      action = recheckButton();
    }
    return infoNote({ icon: ic, tone, text, action });
  }

  // ---- the left card
  function statusNotes() {
    const st = state();
    const retry = () =>
      button({
        label: t("retry"),
        icon: "refresh",
        size: "sm",
        autoLoading: true,
        onClick: () => ctx.refreshStatus(true).catch((err) => ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) })),
      });
    if (st === "offline") return [infoNote({ icon: "alert", tone: "warning", text: t("offline.note"), action: retry() })];
    if (st === "error") return [infoNote({ icon: "alert", tone: "warning", text: t("error.note", { detail: s.status.detail || "" }), action: retry() })];
    return [];
  }

  // "Etsy accepts these keys" is the toast's to say (saveKeys): a note here as well said
  // it twice and pushed Bağlan below the fold. Only what the toast does not say stays.
  function keyResultNotes() {
    const r = s.keyResult;
    if (!r) return [];
    const out = [];
    if (r.token_cleared) out.push(infoNote({ icon: "alert", tone: "warning", text: t("keys.token_cleared") }));
    return out;
  }

  function loadingView() {
    return [
      h(
        "div",
        { class: "cx-head" },
        h("span", { class: "cx-tile", "aria-hidden": "true" }, icon("link", { size: 20 })),
        h("div", { class: "cx-head-skel" }, skeleton({ lines: 1, height: 22, widths: ["60%"] })),
      ),
      h("div", { class: "cx-loading" }, skeleton({ lines: 5, height: 14 })),
    ];
  }

  function errorView() {
    return [
      head("link", t("title")),
      infoNote({
        icon: "alert-circle",
        tone: "danger",
        text: [t("info.error"), " ", ctx.api.errorText(s.infoError, t)],
        action: button({ label: t("retry"), icon: "refresh", size: "sm", autoLoading: true, onClick: () => loadInfo() }),
      }),
    ];
  }

  // keys: how to make the app + the form
  function howStep(n, title, body) {
    return h(
      "li",
      { class: "cx-how" },
      h("span", { class: "cx-how-num num", "aria-hidden": "true" }, String(n)),
      h("div", { class: "cx-how-body" }, h("h3", { class: "cx-how-title" }, title), body),
    );
  }

  function whyBlock() {
    const text = t("keys.why");
    const copy = button({
      label: t("common.copy"),
      icon: "copy",
      size: "sm",
      onClick: async () => {
        const ok = await copyText(text);
        copy.setLabel(ok ? t("common.copied") : t("common.copy_failed"));
        copy.classList.toggle("is-done", ok);
        setTimeout(() => {
          copy.setLabel(t("common.copy"));
          copy.classList.remove("is-done");
        }, 1600);
      },
    });
    return h("div", { class: "cx-why" }, h("p", { class: "cx-why-text", lang: "en", "aria-label": t("keys.why_label") }, text), copy);
  }

  /** Keystring vs Shared secret: which is which, and where each one is on Etsy. */
  function whichKey() {
    const item = (name, iconName, text) =>
      h(
        "div",
        { class: "cx-which-item" },
        h("span", { class: "cx-which-icon", "aria-hidden": "true" }, icon(iconName, { size: 15 })),
        h("div", null, h("b", null, name), h("p", null, text)),
      );
    return h("div", { class: "cx-which" }, item("Keystring", "eye", t("keys.which.key")), item("Shared secret", "eye-off", t("keys.which.secret")));
  }

  function howto() {
    return h(
      "ol",
      { class: "cx-howto" },
      howStep(1, t("keys.step1.title"), [h("p", null, t("keys.step1.text")), extLink(SELLER_APP_URL, t("keys.step1.button"))]),
      howStep(2, t("keys.step2.title"), [h("p", null, t("keys.step2.text")), whyBlock()]),
      howStep(3, t("keys.step3.title"), [h("p", null, t("keys.step3.text")), callbackGuide({ compact: true })]),
      howStep(4, t("keys.step4.title"), [h("p", null, t("keys.step4.text")), whichKey(), extLink(YOUR_APPS_URL, t("keys.step4.button"))]),
    );
  }

  /** A paste into a key field: cleaned, and both fields filled when both were copied. */
  function onKeyPaste(e, role) {
    const text = e.clipboardData && e.clipboardData.getData("text");
    if (!text || !form) return;
    const pieces = keyPieces(text, role);
    const own = pieces.find(([r]) => r === role);
    const otherRole = role === "key" ? "secret" : "key";
    const other = pieces.find(([r]) => r === otherRole);
    if (!own && !other) return;
    e.preventDefault();
    const target = role === "key" ? form.key : form.secret;
    const otherInput = role === "key" ? form.secret : form.key;
    if (own) target.value = own[1];
    if (other && (!otherInput.value.trim() || !own)) otherInput.value = other[1];
    for (const f of [form.keyField, form.secretField]) f.setError("");
    ctx.setDirty(typedKeys());
    syncKeyActions();
  }

  /** Right after saving, the callback question takes the save row's place; typing into a
   *  key field brings the save row back. */
  function syncKeyActions() {
    if (live.keyActions) live.keyActions.hidden = !!s.keysConfirm && !typedKeys();
  }

  function buildForm() {
    const key = textInput({ mono: true, placeholder: t("keys.keystring_ph") });
    const secret = textInput({ type: "password", mono: true, placeholder: t("keys.secret_ph"), autocomplete: "off" });
    const eye = iconButton({
      icon: "eye",
      variant: "ghost",
      size: "sm",
      title: t("keys.show"),
      onClick: () => {
        const show = secret.type === "password";
        secret.type = show ? "text" : "password";
        mount(eye, icon(show ? "eye-off" : "eye", { size: 14 }));
        eye.title = t(show ? "keys.hide" : "keys.show");
        eye.setAttribute("aria-label", eye.title);
      },
    });
    const secretBox = h("div", { class: "input-affix cx-secret" }, secret, eye);
    const cb = textInput({ mono: true, value: callbackUri() });
    const keyField = field({ label: t("keys.keystring"), hint: t("keys.keystring_hint"), input: key });
    const secretField = field({ label: t("keys.secret"), hint: t("keys.secret_hint"), input: secretBox });
    const cbField = field({ label: t("keys.callback"), hint: t("keys.callback_hint", { url: DEFAULT_CALLBACK }), input: cb });
    const adv = h(
      "details",
      { class: "cx-adv" },
      h("summary", null, icon("chevron-right", { size: 14 }), h("span", null, t("keys.advanced"))),
      h("div", { class: "cx-adv-body" }, cbField),
    );
    const result = h("div", { class: "cx-result", role: "status" });
    const save = button({ label: t("keys.save"), variant: "primary", size: "lg", icon: "check", type: "submit" });
    for (const input of [key, secret]) {
      input.addEventListener("input", () => {
        ctx.setDirty(typedKeys());
        syncKeyActions();
      });
    }
    key.addEventListener("paste", (e) => onKeyPaste(e, "key"));
    secret.addEventListener("paste", (e) => onKeyPaste(e, "secret"));
    return { key, secret, cb, keyField, secretField, cbField, adv, result, save };
  }

  /** Keys typed into the form but not saved yet (lost when the page is left). */
  function typedKeys() {
    if (!form || !form.key.isConnected) return false;
    return [form.key, form.secret].some((input) => String(input.value || "").trim() !== "");
  }
  ctx.onBeforeLeave(() => {
    if (!typedKeys()) return true;
    return ctx.confirm({ title: t("common:leave.title"), message: t("common:leave.message"), confirmLabel: t("common:leave.confirm"), danger: true });
  });

  function renderKeyResult() {
    if (!form) return;
    const r = s.keyResult;
    if (!r || r.check === "ok") {
      mount(form.result);
      return;
    }
    const map = {
      rejected: ["danger", "x-circle", t("keys.result.rejected", { reason: r.reason || "" })],
      offline: ["warning", "alert", t("keys.result.offline")],
      unknown: ["warning", "alert", t("keys.result.unknown", { reason: r.reason || "" })],
    };
    const [tone, ic, text] = map[r.check] || map.unknown;
    mount(form.result, infoNote({ tone, icon: ic, text: r.check === "rejected" ? [text, " ", t("keys.bad_help")] : text }));
  }

  function keysView() {
    const info = s.info || {};
    if (!form) form = buildForm();
    form.key.placeholder = info.keys ? t("keys.saved_ph", { prefix: info.keystring_prefix }) : t("keys.keystring_ph");
    form.secret.placeholder = info.keys ? t("keys.secret_saved_ph", { n: info.secret_length }) : t("keys.secret_ph");
    renderKeyResult();
    const kids = [head("key", t("keys.title")), h("p", { class: "cx-lead" }, t("keys.lead"))];
    if (state() === "bad_keys" && !(s.keyResult && s.keyResult.check === "rejected")) {
      kids.push(
        infoNote({
          icon: "alert-circle",
          tone: "danger",
          text: [h("b", null, t("keys.bad")), " ", s.status.detail ? h("span", { class: "mono cx-detail" }, s.status.detail) : null, " ", t("keys.bad_help")],
        }),
      );
    }
    if (s.editKeys && info.keys) {
      kids.push(
        infoNote({
          icon: "key",
          tone: "neutral",
          text: t("keys.editing", { prefix: info.keystring_prefix, n: info.secret_length }),
          action: button({
            label: t("keys.cancel_edit"),
            size: "sm",
            variant: "ghost",
            onClick: () => {
              s.editKeys = false;
              render(true);
            },
          }),
        }),
      );
    }
    kids.push(...statusNotes());
    if (info.keys) {
      kids.push(h("details", { class: "cx-howto-wrap" }, h("summary", null, icon("chevron-right", { size: 14 }), h("span", null, t("keys.howto_again"))), howto()));
    } else {
      kids.push(sectionTitle(t("keys.howto")), howto());
    }
    live.keyActions = h("div", { class: "cx-actions" }, form.save, h("span", { class: "cx-local" }, icon("lock", { size: 13 }), t("keys.local")));
    syncKeyActions();
    kids.push(
      sectionTitle(t("keys.form")),
      h(
        "form",
        {
          class: "cx-form",
          autocomplete: "off",
          onSubmit: (e) => {
            e.preventDefault();
            saveKeys();
          },
        },
        h("div", { class: "cx-form-grid" }, form.keyField, form.secretField),
        h("p", { class: "cx-form-hint" }, icon("sparkles", { size: 13 }), h("span", null, t("keys.combined_hint"))),
        form.adv,
        form.result,
        live.keyActions,
      ),
    );
    // Keys Etsy accepts were just saved: the callback question, then on to connecting.
    if (s.keysConfirm) kids.push(gateBox({ next: true }));
    kids.push(infoNote({ icon: "info", tone: "neutral", text: t("keys.one_app") }));
    return kids;
  }

  async function saveKeys() {
    if (!form) return;
    for (const f of [form.keyField, form.secretField, form.cbField]) f.setError("");
    const body = { keystring: form.key.value, shared_secret: form.secret.value, redirect_uri: form.cb.value.trim() };
    form.save.setLoading(true);
    try {
      const res = await ctx.api.post("/api/connect/keys", body, { signal: ctx.signal });
      form.key.value = "";
      form.secret.value = ""; // the secret never stays in the page
      ctx.setDirty(false);
      s.keyResult = res;
      s.pre = null; // the keys changed: checked again below
      if (res.status) s.status = res.status;
      if (res.check === "ok") {
        s.editKeys = false;
        s.afterSave = true; // loadInfo decides whether the callback question comes next
        ctx.toast({ tone: "success", title: t("keys.saved_toast"), message: t("keys.result.ok") });
      }
      await loadInfo();
      const gate = s.keysConfirm && main.querySelector(".cx-gate");
      if (gate) gate.scrollIntoView({ block: "nearest", behavior: "smooth" });
    } catch (err) {
      if (ctx.api.isAbort(err)) return;
      const which = err.params && err.params.field;
      const target = which === "keystring" ? form.keyField : which === "shared_secret" ? form.secretField : which === "redirect_uri" ? form.cbField : null;
      if (target) {
        if (target === form.cbField) form.adv.open = true;
        target.setError(ctx.api.errorText(err, t));
      } else {
        mount(form.result, infoNote({ tone: "danger", icon: "x-circle", text: ctx.api.errorText(err, t) }));
      }
    } finally {
      form.save.setLoading(false);
    }
  }

  function actionFirst() {
    return !connecting() && state() === "reconnect";
  }

  // connect: the video's card (title, lead, İSTENEN İZİNLER, the note, the button at the
  // bottom), plus the pre-flight problems, the callback question when still open, and the
  // help while waiting for Etsy
  function connectView() {
    const info = s.info || {};
    const kids = [head("link", t("connect.title")), h("p", { class: "cx-lead" }, t("connect.lead"))];
    if (state() === "reconnect" && !connecting()) kids.push(infoNote({ icon: "alert", tone: "warning", text: t("connect.reconnect_note") }));
    kids.push(...statusNotes(), ...keyResultNotes());
    live.problems = h("div", { class: "cx-problems" });
    kids.push(live.problems);
    // "Etsy'de nasıl eklenir?": the whole callback box, under the lead, on demand
    if (s.gateOpen && !connecting()) kids.push(gateBox());
    const scopes = [...(info.scopes_requested || DEFAULT_SCOPES)];
    const extra = [];
    if (s.extra && !scopes.includes(s.extra)) {
      scopes.push(s.extra);
      extra.push(s.extra);
    }
    const perms = [sectionTitle(t("connect.perms")), permList(t, permissionRows(t, scopes, extra)), safeNote(t)];
    // Keys saved by an older version, or the tick taken back: one line above the button.
    const gate = s.gateAsk && !s.gateOpen && !connecting() ? gateLine() : null;
    if (actionFirst()) {
      // A reconnect (the note above): the button comes before the permissions, so it is
      // on screen without scrolling (1280x720 included).
      kids.push(gate, connectRow(), ...perms);
    } else {
      kids.push(...perms, gate, connectRow());
    }
    live.trouble = h("div", { class: "cx-trouble-slot" });
    kids.push(live.trouble);
    renderProblems();
    renderTrouble();
    return kids.filter(Boolean);
  }

  function gateCheckbox() {
    const box = checkbox({ checked: !!(s.info && s.info.callback_confirmed), label: t("gate.check"), onChange: (v) => setCallbackConfirmed(v) });
    box.classList.add("cx-gate-check");
    live.gateChecks.push(box);
    return box;
  }

  /** The callback question in one line: the tick and a way to the whole box. */
  function gateLine() {
    return h(
      "div",
      { class: cx("cx-gate-line", s.info && s.info.callback_confirmed && "is-done") },
      gateCheckbox(),
      h("button", { type: "button", class: "cx-link-btn", onClick: () => openGate() }, t("connect.callback_how")),
    );
  }

  // -- "Callback adresini Etsy'ye eklediniz mi?": once per shop and address. `next`: in the
  // keys step, with "Devam: mağazayı bağla" (enabled once ticked).
  function gateBox({ next = false } = {}) {
    const info = s.info || {};
    const skip = h("button", { type: "button", class: "cx-link-btn cx-gate-skip", onClick: () => skipGate() }, t("gate.skip"));
    let go = null;
    if (next) {
      go = button({
        label: t("gate.next"),
        variant: "primary",
        iconRight: "arrow-right",
        disabled: !info.callback_confirmed,
        class: "cx-gate-next",
        onClick: () => {
          s.keysConfirm = false;
          s.gateOpen = false;
          s.gateAsk = false; // answered
          render(true);
          if (!s.pre) runPreflight(true); // the keys step had no pre-flight
        },
      });
      live.gateNext = go;
    }
    return h(
      "section",
      { class: cx("cx-gate", info.callback_confirmed && "is-done", next && "is-step"), "aria-labelledby": "cx-gate-title" },
      h(
        "div",
        { class: "cx-gate-head" },
        h("span", { class: "cx-gate-icon", "aria-hidden": "true" }, icon("help", { size: 17 })),
        h("div", { class: "cx-gate-text" }, h("h3", { class: "cx-gate-title", id: "cx-gate-title" }, t("gate.title")), h("p", null, t("gate.lead"))),
      ),
      callbackGuide({ dense: true }),
      h("div", { class: "cx-gate-foot" }, gateCheckbox(), h("span", { class: "spacer" }), skip, go),
    );
  }

  /** The tick's look everywhere it is shown (box, line, Devam, the connect button). */
  function showConfirmed(value) {
    for (const box of live.gateChecks) box.checked = value;
    main.querySelectorAll(".cx-gate, .cx-gate-line").forEach((n) => n.classList.toggle("is-done", value));
    if (live.gateNext) live.gateNext.setDisabled(!value);
    updateConnectButton();
  }

  async function setCallbackConfirmed(value, { quiet = false } = {}) {
    if (!s.info) return;
    const before = !!s.info.callback_confirmed;
    s.info.callback_confirmed = value;
    showConfirmed(value);
    try {
      const res = await ctx.api.post("/api/connect/callback-confirmed", { confirmed: value });
      if (s.info) s.info.callback_confirmed = !!res.callback_confirmed;
    } catch (err) {
      if (s.info) s.info.callback_confirmed = before;
      if (!quiet) ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
    }
    if (s.info) showConfirmed(!!s.info.callback_confirmed);
  }

  function skipGate() {
    s.gateOpen = false;
    s.gateAsk = false;
    s.keysConfirm = false;
    setCallbackConfirmed(true, { quiet: true });
    render(true);
    if (!s.pre) runPreflight(true);
  }

  function gateBlocks() {
    return !!(s.gateAsk && s.info && !s.info.callback_confirmed);
  }

  // -- pre-flight: what would stop "Bağlan", found before Etsy's page opens
  function preflightProblems() {
    const data = s.pre && s.pre.data;
    if (!data || !data.checks) return [];
    return Object.values(data.checks).filter((c) => c && !c.ok && c.code);
  }

  function preflightBlocks() {
    return preflightProblems().length > 0;
  }

  function renderProblems() {
    if (!live.problems) return;
    const notes = [];
    const seen = new Set();
    if (s.connError) {
      notes.push(problemNote(s.connError));
      seen.add(s.connError.code);
    }
    if (state() === "offline") seen.add("offline"); // the status note above says it already
    if (!connecting()) {
      for (const c of preflightProblems()) {
        if (seen.has(c.code)) continue;
        seen.add(c.code);
        notes.push(problemNote(c));
      }
    }
    mount(live.problems, notes);
  }

  async function runPreflight(force = false) {
    if (connecting() || computeMode() !== "connect") return;
    if (!force && s.pre && s.pre.loading) return;
    const seq = ++preSeq;
    s.pre = { ...(s.pre || {}), loading: true };
    updateConnectButton();
    try {
      const data = await ctx.api.get("/api/connect/preflight", null, { signal: ctx.signal });
      if (seq !== preSeq) return;
      s.pre = { data, loading: false };
    } catch (err) {
      if (ctx.api.isAbort(err) || seq !== preSeq) return;
      s.pre = { error: err, loading: false }; // not a blocker: /start checks the same again
    }
    renderProblems();
    updateConnectButton();
  }

  function phaseText() {
    const p = s.conn ? s.conn.phase : "starting";
    if (p === "starting") return t("connect.phase.starting");
    if (p === "opened") return s.conn && s.conn.blocked && !s.conn.restored ? t("connect.popup_blocked") : t("connect.phase.opened");
    if (p === "done") return t("connect.phase.done");
    if (p === "code_received") return t("connect.phase.verifying"); // the code -> a token
    return t("connect.phase.fetching");
  }

  function connectRow() {
    const running = connecting();
    const btn = button({
      label: running ? t("connect.connecting") : s.connError ? t("connect.retry") : t("connect.button"),
      loading: running,
      variant: "primary",
      size: "lg",
      icon: "link",
      class: "cx-connect-btn",
      onClick: () => startConnect(),
    });
    live.btn = btn;
    let aside;
    if (running) {
      const prog = progressBar({ value: s.conn.pct, tone: "accent", size: "sm", label: phaseText() });
      const text = h("span", { class: "cx-phase", role: "status" }, phaseText());
      const links = h("span", { class: "cx-progress-links" });
      live.prog = prog;
      live.text = text;
      live.links = links;
      live.hint = null;
      aside = h("div", { class: "cx-progress" }, h("div", { class: "cx-progress-top" }, text, links), prog.el);
      renderLinks();
    } else {
      live.prog = null;
      live.hint = h("p", { class: "cx-hint", role: "status" });
      aside = live.hint;
    }
    // The button and its hint only, as in the video: the keys change from the stepper's
    // "Hesap" (and Ayarlar), the callback how-to is in the question line above.
    const row = h("div", { class: "cx-actions cx-connect-row" }, btn, aside);
    updateConnectButton();
    return row;
  }

  /** The connect button and its hint follow the callback question and the pre-flight. */
  function updateConnectButton() {
    if (!live.btn || connecting()) return;
    const gate = gateBlocks();
    const pre = preflightBlocks();
    live.btn.setDisabled(gate || pre);
    if (!live.hint) return;
    let text = t("connect.hint");
    if (gate) text = t("gate.needed");
    else if (pre) text = t("pre.blocked");
    else if (s.pre && s.pre.loading && !s.pre.data) text = t("pre.checking");
    live.hint.textContent = text;
    live.hint.classList.toggle("is-warn", gate || pre);
  }

  function renderLinks() {
    if (!live.links || !s.conn) return;
    const kids = [];
    if (s.conn.url && (s.conn.blocked || s.conn.restored)) {
      kids.push(h("a", { href: s.conn.url, target: "_blank", rel: "noopener noreferrer", class: "cx-link-btn" }, t("connect.reopen"), icon("external", { size: 12 })));
    }
    if (s.conn.jobId) {
      kids.push(h("button", { type: "button", class: "cx-link-btn", onClick: cancelConnect }, t("connect.cancel")));
    }
    mount(live.links, kids);
  }

  // -- "Etsy bir hata mı gösterdi?": after SLOW_AFTER seconds of waiting, or after a
  // connect that timed out or was refused at the end
  function troubleWanted() {
    if (connecting()) return !!s.conn.slow;
    return !!(s.connError && TROUBLE_AFTER.has(s.connError.code));
  }

  function renderTrouble() {
    if (!live.trouble) return;
    if (!troubleWanted()) {
      mount(live.trouble);
      return;
    }
    if (live.trouble.firstChild) return; // already shown: keep its open/closed state
    const open = s.troubleOpen !== null ? s.troubleOpen : !connecting() && s.connError && s.connError.code === "connect_timeout";
    mount(live.trouble, troublePanel(open));
  }

  function troublePanel(open) {
    const port = callbackPort();
    const retry = () => button({ label: t("trouble.retry"), icon: "refresh", size: "sm", variant: "primary", onClick: () => startConnect({ restart: true }) });
    const item = (ic, title, fix, extra) =>
      h(
        "li",
        { class: "cx-tr-item" },
        h("span", { class: "cx-tr-icon", "aria-hidden": "true" }, icon(ic, { size: 15 })),
        h("div", { class: "cx-tr-body" }, h("b", { class: "cx-tr-title" }, title), h("p", null, fix), extra ? h("div", { class: "cx-tr-extra" }, extra) : null),
      );
    const details = h(
      "details",
      { class: "cx-trouble", open: !!open },
      h(
        "summary",
        null,
        icon("chevron-right", { size: 14 }),
        h("span", { class: "cx-tr-sum" }, h("b", null, t("trouble.title")), h("span", null, t("trouble.sub"))),
      ),
      h(
        "ol",
        { class: "cx-tr-list" },
        item("link", t("trouble.redirect.title"), t("trouble.redirect.fix"), [callbackGuide({ compact: true }), h("div", { class: "cx-tr-actions" }, retry())]),
        item("key", t("trouble.client.title"), t("trouble.client.fix"), h("div", { class: "cx-tr-actions" }, editKeysButton(), extLink(YOUR_APPS_URL, t("trouble.apps")))),
        item("clock", t("trouble.pending.title"), t("trouble.pending.fix"), h("div", { class: "cx-tr-actions" }, extLink(YOUR_APPS_URL, t("trouble.apps")))),
        item("globe", t("trouble.unreachable.title", { port }), t("trouble.unreachable.fix"), h("div", { class: "cx-tr-actions" }, retry())),
        item("alert", t("trouble.port.title", { port }), t("trouble.port.fix", { port }), h("div", { class: "cx-tr-actions" }, changeCallbackButton())),
      ),
    );
    details.addEventListener("toggle", () => {
      s.troubleOpen = details.open;
    });
    return details;
  }

  function checkSlow() {
    const conn = s.conn;
    if (!conn || conn.slow || !conn.since || conn.phase !== "opened") return;
    if (Date.now() / 1000 - conn.since < SLOW_AFTER) return;
    conn.slow = true;
    renderTrouble();
  }

  // connected: the video's success card. Disconnecting lives in Ayarlar → Etsy bağlantısı,
  // changing the keys behind the stepper's "Hesap".
  function connectedView() {
    const info = s.info || {};
    const linked = state() === "connected";
    const notes = [...statusNotes(), ...keyResultNotes()];
    if (s.connError) notes.push(problemNote(s.connError));
    const granted = info.scopes_granted && info.scopes_granted.length ? info.scopes_granted : (s.status && s.status.scopes) || [];
    const missing = info.missing_scopes || [];
    const next = nextSetupStep(setup());
    if (missing.length) {
      notes.push(
        infoNote({
          icon: "alert",
          tone: "warning",
          text: t("connected.missing", { scopes: missing.join(", ") }),
          action: button({ label: t("connected.reconnect"), size: "sm", icon: "refresh", onClick: () => startConnect() }),
        }),
      );
    }
    if (s.extra && !granted.includes(s.extra)) {
      notes.push(
        infoNote({
          icon: "key",
          tone: "accent",
          text: t("connected.extra", { scope: s.extra }),
          action: button({ label: t("connected.extra_button"), size: "sm", variant: "primary", onClick: () => startConnect() }),
        }),
      );
    }
    live.shopCounts = h("span", { class: "cx-shoprow-counts" });
    renderCounts();
    return [
      h("div", { class: "cx-medal", "aria-hidden": "true" }, h("span", null, icon("check", { size: 32, strokeWidth: 3 }))),
      h("h2", { class: "cx-done-title", tabIndex: -1 }, t("connected.title")),
      h("p", { class: "cx-done-lead" }, t("connected.sub")),
      notes.length ? h("div", { class: "cx-done-notes" }, notes) : null,
      h(
        "div",
        { class: "cx-shoprow" },
        h("span", { class: "cx-shoprow-tile", "aria-hidden": "true" }, icon("store", { size: 25 })),
        h("div", { class: "cx-shoprow-text" }, h("b", { class: "cx-shoprow-name" }, shownShopName()), live.shopCounts),
        linked ? badge({ text: t("status.connected_long"), tone: "success", icon: "check" }) : null,
      ),
      permChips(t, granted),
      h(
        "div",
        { class: "cx-actions cx-done-actions" },
        button({ label: t(`connected.next${next.key}`), variant: "primary", size: "lg", iconRight: "arrow-right", onClick: () => ctx.navigate(next.path) }),
        h("span", { class: "cx-hint" }, t(`connected.next${next.key}_hint`)),
      ),
    ].filter(Boolean);
  }

  // The shop row's "· 44 ilan · 12 bekleyen sipariş": two counts, cached by the server;
  // a count that could not be read is left out.
  function renderCounts() {
    if (!live.shopCounts) return;
    const data = s.counts && s.counts.data;
    const kids = [];
    if (data) {
      for (const [key, stat] of [["connected.listings", data.listings], ["connected.pending", data.to_ship]]) {
        if (stat && typeof stat.value === "number") kids.push(countText(t, key, stat.value));
      }
    }
    mount(live.shopCounts, kids);
  }

  async function loadCounts() {
    const seq = ++countsSeq;
    s.counts = { loading: true, data: s.counts && s.counts.data };
    try {
      const data = await ctx.api.get("/api/connect/counts", null, { signal: ctx.signal });
      if (seq !== countsSeq) return;
      s.counts = { loading: false, data };
    } catch (err) {
      if (ctx.api.isAbort(err) || seq !== countsSeq) return;
      s.counts = { loading: false, data: null, error: err };
    }
    renderCounts();
  }

  // ---- connecting
  function openBlank() {
    let w = null;
    try {
      w = window.open("", "_blank");
    } catch {
      w = null;
    }
    if (!w) return null;
    try {
      w.opener = null; // Etsy's page gets no handle on this app
    } catch {
      /* ignore */
    }
    try {
      const d = w.document;
      d.title = "Etsy";
      d.body.style.cssText =
        "margin:0;height:100vh;display:grid;place-items:center;background:#0b0c0f;color:#b6b9c4;font:15px system-ui,sans-serif";
      d.body.textContent = t("popup.opening");
    } catch {
      /* not ours to write into: fine */
    }
    return w;
  }

  // Only a tab still on this origin (the blank tab, or /oauth-done) may be closed from
  // here: once it shows Etsy's page it is no longer ours to close (its opener was cut).
  function closePopup(w) {
    if (!w || w.closed) return;
    let ours = false;
    try {
      ours = w.location.origin === location.origin;
    } catch {
      ours = false;
    }
    if (ours) w.close();
  }

  /** "Bağlan". With {restart}, a connect that is still waiting is replaced (the server
   *  cancels it); the old Etsy tab is left to the person. */
  function startConnect({ restart = false } = {}) {
    if (connecting() && !restart) return;
    if (!connecting() && computeMode() === "connect" && (gateBlocks() || preflightBlocks())) return;
    // Opened here, inside the click, or the browser blocks it; pointed at Etsy below.
    const popup = openBlank();
    const extra = s.extra && !((s.info && s.info.scopes_requested) || []).includes(s.extra) ? [s.extra] : [];
    stopTimers();
    s.connError = null;
    s.keyResult = null;
    s.troubleOpen = null;
    s.conn = { active: true, phase: "starting", pct: PHASE_PCT.starting, jobId: null, url: null, popup, blocked: !popup, restored: false, since: null, slow: false };
    render(true);
    startTimers();
    ctx.api
      .post("/api/connect/start", { extra_scopes: extra })
      .then((res) => {
        const conn = s.conn;
        if (!conn || conn.popup !== popup) return;
        conn.jobId = res.job_id;
        conn.url = res.url;
        conn.since = Date.now() / 1000;
        if (popup && !popup.closed) {
          try {
            popup.location.replace(res.url);
          } catch {
            conn.blocked = true;
          }
        } else {
          conn.blocked = true;
        }
        advance("opened");
        renderLinks();
      })
      .catch((err) => {
        closePopup(popup);
        if (ctx.api.isAbort(err)) return;
        if (s.conn && s.conn.popup !== popup) return;
        stopTimers();
        s.conn = null;
        s.connError = err;
        render(true);
        runPreflight(true);
      });
  }

  async function cancelConnect() {
    const conn = s.conn;
    if (!conn || !conn.jobId) return;
    closePopup(conn.popup);
    try {
      await ctx.api.post(`/api/jobs/${encodeURIComponent(conn.jobId)}/cancel`, {});
    } catch (err) {
      ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
    }
  }

  function advance(phase) {
    if (!s.conn || PHASES.indexOf(phase) <= PHASES.indexOf(s.conn.phase)) return;
    s.conn.phase = phase;
    s.conn.pct = Math.max(s.conn.pct, PHASE_PCT[phase] || 0);
    if (phase !== "opened" && s.conn.slow) {
      s.conn.slow = false; // Etsy answered: the help is no longer the point
      renderTrouble();
    }
    updateLive();
  }

  function updateLive() {
    if (live.prog && s.conn) {
      live.prog.update(s.conn.pct);
      live.text.textContent = phaseText();
    }
    renderSide();
  }

  function finish(job) {
    const conn = s.conn;
    if (!conn) return;
    stopTimers();
    s.conn = null;
    // The consent tab closes itself on /oauth-done; this is the fallback if it is still
    // there (on another site it is left alone, see closePopup).
    if (conn.popup) setTimeout(() => closePopup(conn.popup), 1600);
    if (job.status === "done") {
      s.connError = null;
      s.editKeys = false;
      if (s.extra) s.extra = null;
      if (s.info) s.info.callback_confirmed = true;
      s.gateOpen = false;
      s.gateAsk = false;
      s.counts = null; // the shop row reads them for the shop just connected
      // As in the video, the card turning into "Mağaza bağlandı" is the feedback: no
      // toast, and the bell's "connected" notification (kept for a connect that ends while
      // the person is on another page) is read already. Focus goes to the new heading.
      s.announceDone = true;
      const note = job.result && job.result.notification;
      if (note && typeof ctx.markNotificationsRead === "function") ctx.markNotificationsRead([note]);
      ctx.refreshStatus(false).catch(() => {});
    } else if (job.status === "error") {
      s.connError = job.error || { code: "internal" };
    }
    loadInfo();
  }

  function startTimers() {
    stopTimers();
    creepTimer = setInterval(() => {
      if (!s.conn || s.conn.phase !== "opened" || s.conn.pct >= CREEP_TO) return;
      s.conn.pct = Math.min(CREEP_TO, s.conn.pct + Math.max(0.25, (CREEP_TO - s.conn.pct) * 0.025));
      updateLive();
    }, 900);
    // Events can be missed (a reconnecting stream); ask now and then as well.
    pollTimer = setInterval(pollJob, 2500);
    slowTimer = setInterval(checkSlow, 1000);
  }

  async function pollJob() {
    const conn = s.conn;
    if (!conn || !conn.jobId) return;
    try {
      const job = await ctx.api.get(`/api/jobs/${encodeURIComponent(conn.jobId)}`, null, { signal: ctx.signal });
      if (s.conn !== conn) return;
      if (job.state && job.state.phase) advance(job.state.phase);
      if (TERMINAL.has(job.status)) finish(job);
    } catch {
      /* next time */
    }
  }

  function stopTimers() {
    clearInterval(creepTimer);
    clearInterval(pollTimer);
    clearInterval(slowTimer);
    creepTimer = null;
    pollTimer = null;
    slowTimer = null;
  }

  ctx.events.on("job-event", (ev) => {
    if (!ev || ev.kind !== "connect" || ev.type !== "phase" || !s.conn) return;
    if (!s.conn.jobId || ev.job_id !== s.conn.jobId) return;
    advance(ev.data && ev.data.phase);
  });
  ctx.events.on("job", (job) => {
    if (!job || job.kind !== "connect" || !s.conn || job.id !== s.conn.jobId) return;
    if (TERMINAL.has(job.status)) finish(job);
  });
  // The consent tab reached /oauth-done (told by app.js): Etsy handed over a code.
  ctx.events.on("oauth-done", (msg) => {
    if (!msg || msg.service !== "etsy") return;
    if (s.conn) {
      advance("code_received");
      pollJob();
    } else {
      loadInfo();
    }
  });

  // ---- data
  async function loadInfo() {
    try {
      s.info = await ctx.api.get("/api/connect/info", null, { signal: ctx.signal });
      s.infoError = null;
    } catch (err) {
      if (ctx.api.isAbort(err)) return;
      s.infoError = err;
    }
    if (s.extra && s.info && !(s.info.known_scopes || []).includes(s.extra)) s.extra = null;
    if (s.info && s.afterSave) {
      // Keys Etsy accepts were just saved: an address not confirmed yet is asked about
      // right there, in the keys step, before the connect card.
      s.keysConfirm = !!s.info.keys && !s.info.callback_confirmed;
      s.afterSave = false;
      if (s.keysConfirm) s.gateAsk = false; // asked there, not again on the connect card
    }
    // Asked until answered (per shop and address); once shown it stays for this visit.
    if (s.info && !s.info.callback_confirmed && !s.keysConfirm) s.gateAsk = true;
    const job = s.info && s.info.job;
    if (job && !s.conn && !TERMINAL.has(job.status)) {
      // A connect started earlier (another visit, another tab) is still waiting.
      const phase = (job.state && job.state.phase) || "opened";
      s.conn = {
        active: true,
        phase,
        pct: Math.max(PHASE_PCT[phase] || 16, 20),
        jobId: job.id,
        url: job.state && job.state.url,
        popup: null,
        blocked: false,
        restored: true,
        since: job.started_at || Date.now() / 1000,
        slow: false,
      };
      startTimers();
      checkSlow();
    }
    render(true);
    if (computeMode() === "connect" && !connecting()) runPreflight(true);
  }

  function render(force) {
    renderSteps();
    const mode = computeMode();
    if (force || mode !== s.mode) {
      const previous = s.mode;
      s.mode = mode;
      const views = { loading: loadingView, error: errorView, keys: keysView, connect: connectView, connected: connectedView };
      const modeChanged = previous !== null && previous !== "loading" && mode !== previous;
      if (mode !== "connect") {
        live.btn = null;
        live.hint = null;
        live.problems = null;
        live.trouble = null;
      }
      live.gateChecks = [];
      live.gateNext = null;
      live.keyActions = null;
      live.shopCounts = null;
      const focused = document.activeElement;
      const hadFocus = !focused || focused === document.body || main.contains(focused);
      const onDoneTitle = !!(focused && focused.classList && focused.classList.contains("cx-done-title"));
      mount(main, (views[mode] || loadingView)());
      // Right after a connect the clicked button is gone: focus (and a screen reader) goes
      // to "Mağaza bağlandı", and a re-render keeps it there.
      if (mode === "connected" && ((s.announceDone && hadFocus) || onDoneTitle)) {
        const title = main.querySelector(".cx-done-title");
        if (title) title.focus({ preventScroll: true });
      }
      if (mode === "connected") s.announceDone = false;
      if (modeChanged) {
        const scroller = el.closest(".content");
        if (scroller) scroller.scrollTop = 0;
      }
      main.className = cx("card", "cx-main", `is-${mode}`, mode === "connect" && actionFirst() && "action-first");
      // The first run (no keys saved yet): the key form beside the brand panel.
      grid.className = cx("cx-grid", `mode-${mode}`, mode === "keys" && s.info && !s.info.keys && "is-first-run");
    }
    if (mode === "connected" && state() === "connected" && !s.counts) loadCounts();
    renderSide();
  }

  ctx.onStatus((st, prev) => {
    s.status = st;
    if (!prev || prev.state !== st.state || (prev.setup && st.setup && prev.setup.connected !== st.setup.connected)) {
      loadInfo();
    } else {
      render(false);
    }
  });

  render(true);
  await loadInfo();
  return () => {
    stopTimers();
    if (stepsObserver) stepsObserver.disconnect();
  };
}
