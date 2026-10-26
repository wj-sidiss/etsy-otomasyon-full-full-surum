// Pinterest (/pinterest): the seller's own Pinterest app and account, boards, a picker
// that turns active listings into queued Pins, the queue itself and the daily post.
//
// Not connected: a setup card (app, callback, keys, connect) and a "how it works" card.
// Connected: queue numbers, "Sıraya ekle" + account / boards / schedule, then the queue.

import {
  badge,
  button,
  card,
  checkbox,
  chips,
  copyField,
  cx,
  debounce,
  emptyState,
  field,
  h,
  iconButton,
  infoNote,
  mount,
  pagination,
  progressBar,
  searchInput,
  sectionTitle,
  select,
  skeleton,
  spinner,
  statCard,
  table,
  tabs,
  textInput,
  thumb,
  toggle,
} from "../ui.js";
import { icon } from "../icons.js";
import { date as fmtDate, lower, number, toDate } from "../format.js";

const SCOPES = ["boards:read", "pins:read", "pins:write", "user_accounts:read"];
const PAGE_SIZE = 20;
const FINISHED = new Set(["done", "error", "cancelled"]);
const CONNECT_TITLE = "pinterest:job.connect";
const POST_TITLE = "pinterest:job.post";

export default {
  async mount(el, ctx) {
    const t = ctx.t;
    // A count in the reader's number format ("2.575" / "2,575"); t() picks "_one" only
    // for the number 1 itself, so the singular key is chosen here before formatting.
    const tn = (key, n, params) => t(n === 1 && t.has(`${key}_one`) ? `${key}_one` : key, { ...params, n: number(n) });
    const errText = (err) => ctx.api.errorText(err, t);
    const S = {
      status: null,
      mode: null,
      boards: null,
      boardsError: null,
      boardsLoading: false,
      defaultBoard: null,
      listings: null,
      listingsError: null,
      queue: null,
      queueError: null,
      picked: new Set(),
      search: "",
      filter: "all",
      queueTab: "all",
      queuePage: 1,
      queueSel: new Set(),
      postJob: null,
      connect: null, // {jobId, url, phase, blocked}
      popup: null,
      form: { board: "", perDay: 2, images: "", ai: true },
    };

    // ------------------------------------------------------------------ header
    const postBtn = button({ label: t("post.button"), icon: "send", variant: "primary", onClick: postDue, disabled: true });
    ctx.setHeader({ actions: [postBtn] });

    const noteSlot = h("div", { class: "pin-note-slot" });
    const kpiSlot = h("div", { class: "pin-kpi-slot" });
    const mainSlot = h("div", { class: "pin-main-slot" });
    const queueSlot = h("div", { class: "pin-queue-slot" });
    el.append(noteSlot, kpiSlot, mainSlot, queueSlot);
    mount(mainSlot, h("div", { class: "pin-grid" }, card({ body: skeleton({ lines: 6, height: 14, gap: 14 }) }), card({ body: skeleton({ lines: 4, height: 14, gap: 14 }) })));

    // ------------------------------------------------------------------ data
    async function loadStatus() {
      try {
        S.status = await ctx.api.get("/api/pinterest/status", null, { signal: ctx.signal });
      } catch (err) {
        if (ctx.api.isAbort(err)) return;
        mount(mainSlot, card({ body: emptyState({ icon: "alert", title: t("error.status"), message: errText(err), action: button({ label: t("common.retry"), icon: "refresh", onClick: () => loadStatus() }) }) }));
        return;
      }
      if (!ctx.isActive()) return;
      const conn = S.status.connecting;
      if (conn && (!S.connect || S.connect.jobId !== conn.job.id)) S.connect = { jobId: conn.job.id, url: conn.url, phase: "waiting", blocked: false };
      if (!conn && S.connect && !S.connect.pending) S.connect = null;
      const d = S.status.defaults || {};
      if (!S.formTouched) S.form = { board: (S.status.board && S.status.board.id) || "", perDay: d.per_day || 2, images: d.images || "", ai: d.ai_modified !== false };
      render();
    }

    async function loadBoards(refresh = false) {
      S.boardsLoading = true;
      renderBoards();
      try {
        const r = await ctx.api.get("/api/pinterest/boards", refresh ? { refresh: 1 } : null, { signal: ctx.signal });
        S.boards = r.items || [];
        S.defaultBoard = r.default || null;
        S.boardsError = null;
        if (!S.form.board || !S.boards.some((b) => b.id === S.form.board)) S.form.board = S.defaultBoard || (S.boards[0] && S.boards[0].id) || "";
      } catch (err) {
        if (ctx.api.isAbort(err)) return;
        S.boardsError = err;
      }
      S.boardsLoading = false;
      if (!ctx.isActive()) return;
      renderBoards();
      renderBuilderForm();
      renderQueue();
    }

    async function loadListings(refresh = false) {
      S.listingsError = null;
      if (refresh) S.listings = null;
      renderPicker();
      try {
        const r = await ctx.api.get("/api/pinterest/listings", refresh ? { refresh: 1 } : null, { signal: ctx.signal });
        S.listings = r;
      } catch (err) {
        if (ctx.api.isAbort(err)) return;
        S.listingsError = err;
      }
      if (ctx.isActive()) renderPicker();
    }

    async function loadQueue() {
      try {
        S.queue = await ctx.api.get("/api/pinterest/queue", null, { signal: ctx.signal });
        S.queueError = null;
        const ids = new Set(S.queue.items.map((i) => i.id));
        for (const id of [...S.queueSel]) if (!ids.has(id)) S.queueSel.delete(id);
      } catch (err) {
        if (ctx.api.isAbort(err)) return;
        S.queueError = err;
      }
      if (ctx.isActive()) {
        renderQueue();
        renderKpis();
        renderHeader();
      }
    }

    async function loadActiveJobs() {
      try {
        const jobs = await ctx.api.get("/api/jobs", { kind: "pinterest", active: 1 }, { signal: ctx.signal });
        S.postJob = (jobs || []).find((j) => j.title_key === POST_TITLE) || null;
      } catch {
        S.postJob = null;
      }
    }

    // ------------------------------------------------------------------ layout by mode
    function ready() {
      const s = S.status;
      return !!s && s.connected && !s.needs_reconnect;
    }

    function render() {
      const mode = ready() ? "ready" : "setup";
      renderHeader();
      renderNote();
      if (mode !== S.mode) {
        const scroller = el.closest(".content");
        if (S.mode && scroller) scroller.scrollTop = 0;
        S.mode = mode;
        if (mode === "ready") buildReady();
        else buildSetup();
      } else {
        refreshParts();
      }
    }

    function renderHeader() {
      const s = S.status;
      const q = (S.queue && S.queue.summary) || (s && s.queue) || null;
      const due = q ? q.due : 0;
      postBtn.hidden = !ready();
      postBtn.setCount(due > 0 ? due : undefined);
      postBtn.setDisabled(!ready() || due === 0 || isActive(S.postJob));
      postBtn.title = !ready() ? t("post.need_connect") : due === 0 ? t("post.nothing_due") : "";
      if (q && q.total > 0) ctx.setHeader({ subtitle: t("subtitle_queue", { n: q.pending, due }) });
      else ctx.setHeader({ subtitle: t("subtitle") });
    }

    function renderNote() {
      const s = S.status;
      if (s && s.queue_error) {
        mount(noteSlot, infoNote({ tone: "danger", icon: "alert", text: [h("b", null, t("queue.broken")), " ", s.queue_error] }));
      } else if (s && s.needs_reconnect) {
        mount(noteSlot, infoNote({ tone: "warning", icon: "alert", text: t("account.expired") }));
      } else {
        mount(noteSlot);
      }
    }

    // ------------------------------------------------------------------ setup mode
    const parts = {};

    function buildSetup() {
      mount(kpiSlot);
      parts.setupCard = setupCard();
      parts.howCard = howCard();
      mount(mainSlot, h("div", { class: "pin-grid pin-grid-setup" }, parts.setupCard.el, parts.howCard));
      if (S.status.queue && S.status.queue.total > 0) {
        buildQueueCard();
        loadQueue();
      } else {
        mount(queueSlot);
      }
    }

    function setupCard() {
      const s = S.status;
      const form = appForm(false);
      const connectEl = h("div", { class: "pin-connect" });
      const redirectCopy = copyField({ value: s.app.redirect_uri, ariaLabel: t("app.redirect") });
      const steps = h(
        "ol",
        { class: "pin-steps" },
        stepItem(1, t("setup.step1"), [
          h("p", { class: "pin-step-text" }, t("setup.step1_text")),
          h("a", { class: "btn btn-secondary btn-sm pin-ext", href: s.app.apps_url, target: "_blank", rel: "noopener noreferrer" }, icon("external", { size: 14 }), h("span", { class: "btn-label" }, t("setup.open_apps"))),
        ]),
        stepItem(2, t("setup.step2"), [h("p", { class: "pin-step-text" }, t("setup.step2_text")), redirectCopy]),
        stepItem(3, t("setup.step3"), [form.el]),
        stepItem(4, t("setup.step4"), [
          sectionTitle(t("setup.scopes")),
          h("ul", { class: "pin-scopes" }, SCOPES.map((sc) => h("li", null, h("span", { class: "pin-scope-check" }, icon("check", { size: 12, strokeWidth: 2.6 })), t(`scope.${sc.replace(":", "_")}`)))),
          connectEl,
        ]),
      );
      const el = card({
        title: t("setup.title"),
        subtitle: t("setup.subtitle"),
        icon: "pin",
        body: [steps, infoNote({ tone: "success", icon: "shield", text: [h("b", null, t("setup.private_title")), " — ", t("setup.private_text")] })],
        class: "pin-setup-card",
        pad: "lg",
      });
      const api = {
        el,
        update() {
          const st = S.status;
          redirectCopy.update(st.app.redirect_uri);
          form.update();
          renderConnect(connectEl);
        },
      };
      parts.connectEl = connectEl;
      api.update();
      return api;
    }

    function stepItem(n, title, body) {
      return h(
        "li",
        { class: "pin-step" },
        h("span", { class: "pin-step-num num" }, String(n)),
        h("div", { class: "pin-step-body" }, h("p", { class: "pin-step-title" }, title), body),
      );
    }

    function howCard() {
      const rows = [
        ["image", "how.photos", "how.photos_text"],
        ["calendar", "how.spread", "how.spread_text"],
        ["shield-check", "how.once", "how.once_text"],
        ["clock", "how.daily", "how.daily_text"],
      ];
      return card({
        title: t("how.title"),
        icon: "info",
        iconTone: "neutral",
        body: h(
          "ul",
          { class: "pin-how" },
          rows.map(([ic, title, text]) =>
            h("li", null, h("span", { class: "icon-tile icon-tile-sm tone-accent" }, icon(ic, { size: 15 })), h("div", null, h("p", { class: "pin-how-title" }, t(title)), h("p", { class: "pin-how-text" }, t(text)))),
          ),
        ),
        class: "pin-how-card",
      });
    }

    // App settings form, used in the setup card and (collapsed) in the account card.
    function appForm(compact) {
      const s = S.status;
      const appId = textInput({ value: s.app.app_id, mono: true, placeholder: "1234567", autocomplete: "off", ariaLabel: t("app.app_id") });
      const secret = textInput({ value: "", type: "password", mono: true, placeholder: s.app.has_secret ? t("app.secret_saved", { n: s.app.secret_length }) : t("app.secret_placeholder"), autocomplete: "new-password" });
      const eye = iconButton({ icon: "eye", title: t("app.show_secret"), variant: "ghost", size: "sm", onClick: () => {
        const show = secret.type === "password";
        secret.type = show ? "text" : "password";
        mount(eye, icon(show ? "eye-off" : "eye", { size: 14 }));
      } });
      const redirect = textInput({ value: s.app.redirect_uri, mono: true, placeholder: s.app.redirect_default });
      const sandbox = toggle({ checked: s.app.sandbox, label: t("app.sandbox"), sub: t("app.sandbox_sub") });
      const fId = field({ label: t("app.app_id"), input: appId });
      const fSecret = field({ label: t("app.secret"), input: h("div", { class: "pin-secret" }, secret, eye) });
      const fRedirect = field({ label: t("app.redirect"), hint: t("app.redirect_hint"), input: redirect });
      const saveBtn = button({ label: t("app.save"), icon: "save", type: "submit", variant: compact ? "secondary" : s.configured ? "secondary" : "primary" });
      const advanced = h("details", { class: "pin-advanced" }, h("summary", null, icon("chevron-right", { size: 14 }), t("app.advanced")), h("div", { class: "pin-advanced-body" }, fRedirect, sandbox));
      if (!s.app.redirect_ok || s.app.sandbox) advanced.open = true;
      const savedMark = h("span", { class: "pin-saved muted" }, icon("check", { size: 13 }), t("app.saved"));
      savedMark.hidden = !s.configured;
      const el = h(
        "form",
        {
          class: cx("pin-form", compact && "is-compact"),
          novalidate: true,
          onSubmit: async (e) => {
            e.preventDefault();
            if (saveBtn.disabled) return;
            saveBtn.setLoading(true);
            try {
              await save();
            } finally {
              saveBtn.setLoading(false);
            }
          },
        },
        h("div", { class: "pin-form-row" }, fId, fSecret),
        advanced,
        h("div", { class: "pin-form-actions" }, saveBtn, savedMark),
      );

      async function save() {
        fId.setError(null);
        fSecret.setError(null);
        fRedirect.setError(null);
        try {
          const body = { app_id: appId.value.trim(), redirect_uri: redirect.value.trim(), sandbox: sandbox.checked };
          if (secret.value.trim()) body.app_secret = secret.value.trim();
          S.status = await ctx.api.post("/api/pinterest/keys", body);
          secret.value = "";
          ctx.toast({ tone: "success", title: t("app.saved_toast"), message: S.status.token_cleared ? t("app.token_cleared") : undefined });
          render();
        } catch (err) {
          const f = err.params && err.params.field;
          const target = f === "app_id" ? fId : f === "app_secret" ? fSecret : f === "redirect_uri" ? fRedirect : null;
          if (target) {
            if (f === "redirect_uri") advanced.open = true;
            target.setError(t(`app.invalid.${f}`));
          } else {
            ctx.toast({ tone: "danger", title: errText(err) });
          }
        }
      }
      return {
        el,
        update() {
          const st = S.status;
          secret.placeholder = st.app.has_secret ? t("app.secret_saved", { n: st.app.secret_length }) : t("app.secret_placeholder");
          savedMark.hidden = !st.configured;
        },
      };
    }

    function renderConnect(host) {
      if (!host) return;
      const s = S.status;
      if (!s.configured) {
        mount(host, h("p", { class: "pin-connect-hint muted" }, icon("info", { size: 14 }), t("connect.need_keys")));
        return;
      }
      if (!s.app.redirect_ok) {
        mount(host, infoNote({ tone: "warning", icon: "alert", text: t("connect.callback_not_local", { uri: s.app.redirect_uri }) }));
        return;
      }
      const c = S.connect;
      if (c) {
        const bar = progressBar({ value: null, size: "sm", label: t("connect.waiting") });
        const phase = c.phase === "exchanging" ? t("connect.exchanging") : c.pending ? t("connect.opening") : t("connect.waiting");
        mount(
          host,
          h(
            "div",
            { class: "pin-connecting" },
            button({ label: t("connect.connecting"), variant: "primary", size: "lg", loading: true, disabled: true }),
            h(
              "div",
              { class: "pin-connecting-text" },
              h("p", null, phase),
              bar.el,
              h(
                "p",
                { class: "pin-connecting-links" },
                c.url ? h("a", { href: c.url, target: "_blank", rel: "noopener noreferrer" }, icon("external", { size: 13 }), t(c.blocked ? "connect.open_blocked" : "connect.open_again")) : null,
                c.jobId ? h("button", { type: "button", class: "pin-link-btn", onClick: cancelConnect }, t("common.cancel")) : null,
              ),
            ),
          ),
        );
        return;
      }
      mount(
        host,
        h(
          "div",
          { class: "pin-connect-row" },
          button({ label: s.needs_reconnect || s.connected ? t("connect.again") : t("connect.button"), icon: "link", variant: "primary", size: "lg", onClick: startConnect }),
          h("p", { class: "muted pin-connect-sub" }, t("connect.sub")),
        ),
      );
    }

    async function startConnect() {
      // Opened synchronously in the click, so the browser does not treat it as a popup.
      let w = null;
      try {
        w = window.open("", "_blank");
        if (w) {
          // Pinterest's page (and anything later loaded in that tab) gets no handle on
          // this app: this tab keeps its own reference `w` to fill and close it.
          try {
            w.opener = null;
          } catch {
            /* ignore */
          }
          w.document.title = "Pinterest";
          w.document.body.style.cssText = "background:#0b0c0f;color:#b6b9c4;font:15px system-ui;display:grid;place-items:center;height:100vh;margin:0";
          w.document.body.textContent = t("connect.opening");
        }
      } catch {
        /* a blocked or cross-origin window: fall back to the link */
      }
      S.connect = { jobId: null, url: null, phase: "opening", blocked: false, pending: true };
      refreshConnect();
      try {
        const r = await ctx.api.post("/api/pinterest/connect", {});
        S.connect = { jobId: r.job.id, url: r.url, phase: "waiting", blocked: !w || w.closed };
        S.popup = w;
        if (w && !w.closed) w.location.href = r.url;
      } catch (err) {
        S.connect = null;
        if (w) w.close();
        ctx.toast({ tone: "danger", title: t("connect.failed"), message: errText(err) });
      }
      refreshConnect();
    }

    async function cancelConnect() {
      const c = S.connect;
      if (!c || !c.jobId) return;
      try {
        await ctx.api.post(`/api/jobs/${c.jobId}/cancel`, {});
      } catch (err) {
        ctx.toast({ tone: "danger", title: errText(err) });
      }
    }

    function refreshConnect() {
      if (S.mode === "setup" && parts.connectEl) renderConnect(parts.connectEl);
      if (S.mode === "ready" && parts.account) parts.account.update();
    }

    function closePopup() {
      // Only a tab still on this origin (the blank tab, or /oauth-done) is closed from here.
      // Once it shows Pinterest's page it is no longer ours: its opener was cut when it
      // opened, and the browser refuses (and logs) a close from a page that is not its opener.
      const w = S.popup;
      S.popup = null;
      if (!w || w.closed) return;
      let ours = false;
      try {
        ours = w.location.origin === location.origin;
      } catch {
        ours = false;
      }
      if (ours) {
        try {
          w.close();
        } catch {
          /* ignore */
        }
      }
    }

    // ------------------------------------------------------------------ ready mode
    function buildReady() {
      parts.kpis = buildKpis();
      mount(kpiSlot, parts.kpis.el);
      parts.builder = builderCard();
      parts.account = accountCard();
      parts.boards = boardsCard();
      parts.schedule = scheduleCard();
      mount(
        mainSlot,
        h("div", { class: "pin-grid" }, h("div", { class: "pin-side" }, parts.builder.el, parts.schedule), h("div", { class: "pin-side" }, parts.account.el, parts.boards.el)),
      );
      buildQueueCard();
      loadBoards();
      loadListings();
      loadQueue();
    }

    function refreshParts() {
      if (S.mode === "setup") {
        if (parts.setupCard) parts.setupCard.update();
      } else {
        if (parts.account) parts.account.update();
      }
      renderKpis();
    }

    // KPI row
    function buildKpis() {
      const defs = [
        { id: "pending", icon: "clock", tone: "accent" },
        { id: "due", icon: "calendar", tone: "info" },
        { id: "posted", icon: "check", tone: "success" },
        { id: "attention", icon: "alert", tone: "warning" },
      ];
      const cards = defs.map((d) => ({ id: d.id, el: statCard({ icon: d.icon, label: t(`kpi.${d.id}`), value: "–", sub: t(`kpi.${d.id}_sub`), tone: d.tone }) }));
      return { el: h("section", { class: "pin-kpis" }, cards.map((c) => c.el)), cards };
    }

    function renderKpis() {
      if (!parts.kpis || S.mode !== "ready") return;
      const q = (S.queue && S.queue.summary) || S.status.queue || { pending: 0, due: 0, posted: 0, failed: 0, uncertain: 0 };
      const values = { pending: q.pending, due: q.due, posted: q.posted, attention: q.failed + q.uncertain };
      for (const c of parts.kpis.cards) {
        c.el.update({ value: number(values[c.id]) });
        c.el.classList.toggle("is-alert", c.id === "attention" && values.attention > 0);
      }
    }

    // Account card
    function accountCard() {
      const info = h("div", { class: "pin-account" });
      const connectHost = h("div", { class: "pin-account-connect" });
      parts.accountConnect = connectHost;
      const actionsHost = h("span", { class: "pin-account-badges" });
      const form = appForm(true);
      const details = h("details", { class: "pin-advanced pin-app-details" }, h("summary", null, icon("chevron-right", { size: 14 }), t("account.app_settings")), h("div", { class: "pin-advanced-body" }, form.el));
      const el = card({ title: t("account.title"), icon: "pin", actions: actionsHost, body: [info, connectHost, details], class: "pin-account-card" });
      function update() {
        const s = S.status;
        const tok = s.token || {};
        mount(
          actionsHost,
          s.app.sandbox ? badge({ text: t("account.sandbox"), tone: "neutral", size: "sm" }) : null,
          badge({ text: t("account.connected"), tone: "success", dot: true, size: "sm" }),
        );
        const session =
          tok.source === "env" ? t("account.env_token") : tok.refreshable ? (tok.refresh_expires_at ? t("account.auto_until", { date: fmtDate(tok.refresh_expires_at) }) : t("account.auto")) : t("account.no_refresh");
        mount(
          info,
          h(
            "dl",
            { class: "pin-dl" },
            h("dt", null, t("account.app")),
            h("dd", { class: "mono" }, s.app.app_id || "–"),
            h("dt", null, t("account.session")),
            h("dd", null, session),
            h("dt", null, t("account.board")),
            h("dd", null, (s.board && (s.board.name || s.board.id)) || t("account.no_board")),
          ),
          S.connect || tok.source === "env"
            ? null
            : h(
                "div",
                { class: "pin-account-actions" },
                button({ label: t("connect.again"), icon: "refresh", size: "sm", variant: "secondary", onClick: startConnect }),
                button({ label: t("account.disconnect"), icon: "logout", size: "sm", variant: "ghost", class: "pin-danger-ghost", onClick: disconnect }),
              ),
        );
        form.update();
        if (S.connect) renderConnect(connectHost);
        else mount(connectHost);
      }
      update();
      return { el, update };
    }

    async function disconnect() {
      const ok = await ctx.confirm({ title: t("account.disconnect_title"), message: t("account.disconnect_msg"), confirmLabel: t("account.disconnect"), danger: true, icon: "logout" });
      if (!ok) return;
      try {
        S.status = await ctx.api.post("/api/pinterest/disconnect", {});
        ctx.toast({ tone: "success", title: t("account.disconnected") });
        render();
      } catch (err) {
        ctx.toast({ tone: "danger", title: errText(err) });
      }
    }

    // Boards card
    function boardsCard() {
      const body = h("div", { class: "pin-boards" });
      const refresh = iconButton({ icon: "refresh", title: t("boards.refresh"), variant: "ghost", size: "sm", onClick: () => loadBoards(true) });
      const el = card({ title: t("boards.title"), subtitle: t("boards.subtitle"), icon: "layers", iconTone: "neutral", actions: refresh, body, class: "pin-boards-card" });
      return { el, body };
    }

    function renderBoards() {
      const p = parts.boards;
      if (!p || S.mode !== "ready") return;
      if (S.boardsLoading && !S.boards) {
        mount(p.body, skeleton({ lines: 3, height: 30, gap: 10 }));
        return;
      }
      if (S.boardsError) {
        mount(p.body, infoNote({ tone: "danger", icon: "alert", text: errText(S.boardsError), action: button({ label: t("common.retry"), size: "sm", onClick: () => loadBoards(true) }) }));
        return;
      }
      if (!S.boards || !S.boards.length) {
        mount(p.body, emptyState({ icon: "layers", title: t("boards.empty"), message: t("boards.empty_msg"), compact: true }));
        return;
      }
      mount(
        p.body,
        h(
          "div",
          { class: "pin-board-list", role: "radiogroup", "aria-label": t("boards.title") },
          S.boards.map((b) => {
            const isDefault = b.id === S.defaultBoard;
            const meta = [b.pin_count !== null && b.pin_count !== undefined ? t("boards.pins", { n: b.pin_count }) : null, b.privacy ? t(`boards.privacy.${String(b.privacy).toLowerCase()}`) : null].filter(Boolean).join(" · ");
            return h(
              "button",
              { type: "button", role: "radio", "aria-checked": isDefault ? "true" : "false", class: cx("pin-board", isDefault && "is-default"), onClick: () => setDefaultBoard(b) },
              b.cover ? thumb({ src: b.cover, size: 32, radius: 8 }) : h("span", { class: "icon-tile icon-tile-sm tone-neutral" }, icon("pin", { size: 14 })),
              h("span", { class: "pin-board-text" }, h("span", { class: "pin-board-name" }, b.name || b.id), meta ? h("span", { class: "pin-board-meta" }, meta) : null),
              isDefault ? badge({ text: t("boards.default"), tone: "accent", size: "sm", icon: "check" }) : null,
            );
          }),
        ),
      );
    }

    async function setDefaultBoard(b) {
      if (b.id === S.defaultBoard) return;
      try {
        await ctx.api.post("/api/pinterest/board", { board_id: b.id, name: b.name });
        S.defaultBoard = b.id;
        S.form.board = b.id;
        if (S.status) S.status.board = { id: b.id, name: b.name };
        renderBoards();
        renderBuilderForm();
        if (parts.account) parts.account.update();
      } catch (err) {
        ctx.toast({ tone: "danger", title: errText(err) });
      }
    }

    // Schedule card
    function scheduleCard() {
      const sc = S.status.schedule;
      const win = sc.platform !== "mac";
      const winSteps = h(
        "details",
        { class: "pin-advanced pin-howto" },
        h("summary", null, icon("chevron-right", { size: 14 }), t("schedule.windows")),
        h(
          "ol",
          { class: "pin-howto-steps" },
          h("li", null, t("schedule.win1")),
          h("li", null, t("schedule.win2")),
          h("li", null, t("schedule.win3"), h("div", { class: "pin-howto-kv" }, h("span", null, t("schedule.program")), copyField({ value: sc.program }), h("span", null, t("schedule.arguments")), copyField({ value: sc.arguments }))),
          h("li", null, t("schedule.win4")),
        ),
      );
      const macSteps = h(
        "details",
        { class: "pin-advanced pin-howto" },
        h("summary", null, icon("chevron-right", { size: 14 }), t("schedule.mac")),
        h(
          "ol",
          { class: "pin-howto-steps" },
          h("li", null, t("schedule.mac1")),
          h("li", null, t("schedule.mac2"), copyField({ value: `0 10 * * * ${sc.command}` })),
          h("li", null, t("schedule.mac3")),
        ),
      );
      return card({
        title: t("schedule.title"),
        icon: "calendar",
        iconTone: "neutral",
        body: [h("p", { class: "pin-schedule-text" }, t("schedule.text")), copyField({ value: sc.command, ariaLabel: t("schedule.command") }), win ? [winSteps, macSteps] : [macSteps, winSteps]],
        class: "pin-schedule-card",
      });
    }

    // ------------------------------------------------------------------ "Sıraya ekle"
    function builderCard() {
      const search = searchInput({ placeholder: t("pick.search"), shortcut: "/", debounce: 120, onInput: (v) => { S.search = v; renderPickerList(); } });
      const filter = chips({ items: [], value: S.filter, onChange: (id) => { S.filter = id; renderPickerList(); }, ariaLabel: t("pick.filter") });
      const allBox = checkbox({ label: t("pick.all_visible"), onChange: (on) => { for (const it of visibleListings()) on ? S.picked.add(it.listing_id) : S.picked.delete(it.listing_id); renderPickerList(); } });
      const list = h("div", { class: "pin-pick-list", role: "group", "aria-label": t("pick.list") });
      const formHost = h("div", { class: "pin-pick-form" });
      const countEl = h("span", { class: "pin-pick-count" });
      const previewBtn = button({ label: t("pick.preview"), icon: "eye", variant: "primary", autoLoading: true, onClick: preview });
      const refresh = iconButton({ icon: "refresh", title: t("pick.refresh"), variant: "ghost", size: "sm", onClick: () => loadListings(true) });
      const totalBadge = h("span");
      const body = h(
        "div",
        { class: "pin-builder" },
        h("div", { class: "pin-pick-toolbar" }, search, filter.el, h("div", { class: "spacer" }), allBox),
        list,
        formHost,
        h("div", { class: "pin-pick-foot" }, countEl, h("div", { class: "spacer" }), previewBtn),
      );
      const el = card({ title: t("pick.title"), subtitle: t("pick.subtitle"), icon: "plus", actions: [totalBadge, refresh], body, class: "pin-builder-card" });
      return { el, list, formHost, countEl, previewBtn, filter, allBox, totalBadge, search };
    }

    function visibleListings() {
      const items = (S.listings && S.listings.items) || [];
      const q = lower(S.search.trim());
      return items.filter((it) => {
        if (S.filter === "fresh" && (it.queued > 0 || it.posted > 0)) return false;
        if (!q) return true;
        return lower(it.title).includes(q) || String(it.listing_id).includes(q);
      });
    }

    function renderPicker() {
      const p = parts.builder;
      if (!p || S.mode !== "ready") return;
      renderPickerList();
      renderBuilderForm();
    }

    function renderPickerList() {
      const p = parts.builder;
      if (!p) return;
      const items = (S.listings && S.listings.items) || [];
      mount(p.totalBadge, S.listings ? badge({ text: tn("pick.total", S.listings.total ?? items.length), tone: "neutral", size: "sm" }) : null);
      p.filter.update(
        [
          { id: "all", label: t("pick.filter_all"), count: S.listings ? items.length : undefined },
          { id: "fresh", label: t("pick.filter_fresh"), count: S.listings ? items.filter((i) => !i.queued && !i.posted).length : undefined },
        ],
        S.filter,
      );
      if (S.listingsError) {
        const e = S.listingsError;
        if (e.code === "setup_needed") {
          mount(p.list, emptyState({ icon: "link", title: t("pick.need_etsy"), message: t("pick.need_etsy_msg"), compact: true, action: h("a", { class: "btn btn-secondary btn-sm", href: "/kurulum/magaza" }, h("span", { class: "btn-label" }, t("pick.go_connect"))) }));
        } else {
          mount(p.list, infoNote({ tone: "danger", icon: "alert", text: errText(e), action: button({ label: t("common.retry"), size: "sm", onClick: () => loadListings(true) }) }));
        }
        updatePickCount();
        return;
      }
      if (!S.listings) {
        mount(p.list, Array.from({ length: 5 }, () => h("div", { class: "pin-pick is-skel" }, h("span", { class: "skeleton", style: { width: 18, height: 18 } }), h("span", { class: "skeleton", style: { width: 44, height: 44, borderRadius: 8 } }), h("span", { class: "skeleton", style: { flex: 1, height: 12 } }))));
        updatePickCount();
        return;
      }
      const vis = visibleListings();
      if (!items.length) {
        mount(p.list, emptyState({ icon: "list", title: t("pick.empty"), message: t("pick.empty_msg"), compact: true }));
      } else if (!vis.length) {
        mount(p.list, emptyState({ icon: "search", title: t("pick.no_match"), compact: true }));
      } else {
        mount(p.list, vis.map(pickRow));
      }
      const allOn = vis.length > 0 && vis.every((it) => S.picked.has(it.listing_id));
      const someOn = vis.some((it) => S.picked.has(it.listing_id));
      p.allBox.input.checked = allOn;
      p.allBox.input.indeterminate = someOn && !allOn;
      p.allBox.input.disabled = !vis.length;
      // The list holds the newest `shown` listings; `total` (the badge) is the shop's count.
      if (S.listings.truncated) p.list.append(h("p", { class: "pin-pick-more muted" }, tn("pick.truncated", S.listings.shown ?? items.length)));
      updatePickCount();
    }

    function pickRow(it) {
      const on = S.picked.has(it.listing_id);
      const box = checkbox({ checked: on, ariaLabel: it.title, onChange: (v) => { v ? S.picked.add(it.listing_id) : S.picked.delete(it.listing_id); row.classList.toggle("is-picked", v); renderPickerAll(); } });
      const row = h(
        "label",
        { class: cx("pin-pick", on && "is-picked") },
        box,
        thumb({ src: it.thumb, size: 44, radius: 8 }),
        h(
          "span",
          { class: "pin-pick-text" },
          h("span", { class: "pin-pick-title" }, it.title),
          h("span", { class: "pin-pick-meta" }, h("span", { class: "mono" }, `#${it.listing_id}`), " · ", t("pick.images", { n: it.images })),
        ),
        it.queued ? badge({ text: tn("pick.queued", it.queued), tone: "accent", size: "sm" }) : null,
        it.posted ? badge({ text: tn("pick.posted", it.posted), tone: "success", size: "sm", icon: "check" }) : null,
      );
      return row;
    }

    function renderPickerAll() {
      // Only the header checkbox and the count change when one row is ticked.
      const p = parts.builder;
      const vis = visibleListings();
      const allOn = vis.length > 0 && vis.every((it) => S.picked.has(it.listing_id));
      const someOn = vis.some((it) => S.picked.has(it.listing_id));
      p.allBox.input.checked = allOn;
      p.allBox.input.indeterminate = someOn && !allOn;
      updatePickCount();
    }

    function updatePickCount() {
      const p = parts.builder;
      if (!p) return;
      const n = S.picked.size;
      p.countEl.textContent = n ? t("pick.selected", { n }) : t("pick.none_selected");
      p.countEl.classList.toggle("is-on", n > 0);
      p.previewBtn.setCount(n || undefined);
      p.previewBtn.setDisabled(!n || !S.form.board);
    }

    function renderBuilderForm() {
      const p = parts.builder;
      if (!p || S.mode !== "ready") return;
      const boards = S.boards || [];
      const boardOpts = boards.length
        ? boards.map((b) => ({ value: b.id, label: b.name || b.id }))
        : [{ value: "", label: S.boardsLoading ? t("common.loading") : t("form.no_boards"), disabled: true }];
      const boardSel = select({ options: boardOpts, value: S.form.board, onChange: (v) => { S.form.board = v; S.formTouched = true; updatePickCount(); }, disabled: !boards.length, ariaLabel: t("form.board") });
      if (!boards.length) boardSel.input.value = "";
      const perDay = textInput({ type: "number", value: S.form.perDay, min: 1, max: 25, step: 1, align: "right", inputmode: "numeric", onInput: (v) => { S.form.perDay = v; S.formTouched = true; }, suffix: t("form.per_day_suffix") });
      const images = textInput({ value: S.form.images, mono: true, placeholder: t("form.images_placeholder"), onInput: (v) => { S.form.images = v; S.formTouched = true; } });
      const ai = toggle({ checked: S.form.ai, label: t("form.ai"), sub: t("form.ai_sub"), onChange: (v) => { S.form.ai = v; S.formTouched = true; } });
      p.fPerDay = field({ label: t("form.per_day"), input: perDay });
      p.fImages = field({ label: t("form.images"), hint: t("form.images_hint"), input: images });
      p.fBoard = field({ label: t("form.board"), input: boardSel });
      mount(p.formHost, h("div", { class: "pin-form-grid" }, p.fBoard, p.fPerDay, p.fImages), ai);
      updatePickCount();
    }

    function formBody(dryRun) {
      const board = (S.boards || []).find((b) => b.id === S.form.board);
      return {
        listing_ids: [...S.picked],
        board_id: S.form.board,
        board_name: board ? board.name : "",
        per_day: Number.parseInt(S.form.perDay, 10),
        images: String(S.form.images || "").trim(),
        ai_modified: !!S.form.ai,
        dry_run: dryRun,
      };
    }

    function fieldErrors(err) {
      const p = parts.builder;
      const f = err && err.params && err.params.field;
      if (f === "per_day" && p.fPerDay) p.fPerDay.setError(t("form.per_day_error"));
      else if (f === "images" && p.fImages) p.fImages.setError(t("form.images_error"));
      else return false;
      return true;
    }

    async function preview() {
      const p = parts.builder;
      p.fPerDay.setError(null);
      p.fImages.setError(null);
      const body = formBody(true);
      if (!Number.isInteger(body.per_day) || body.per_day < 1 || body.per_day > 25) {
        p.fPerDay.setError(t("form.per_day_error"));
        return;
      }
      let r;
      try {
        r = await ctx.api.post("/api/pinterest/queue", body);
      } catch (err) {
        if (!fieldErrors(err)) ctx.toast({ tone: "danger", title: errText(err) });
        return;
      }
      showPreview(r, body);
    }

    function showPreview(r, body) {
      const n = r.added.length;
      const board = (S.boards || []).find((b) => b.id === body.board_id);
      const notes = [];
      if (r.problems.length) {
        notes.push(
          infoNote({
            tone: "warning",
            icon: "alert",
            text: [h("b", null, t("preview.problems", { n: r.problems.length })), h("ul", { class: "pin-problems" }, r.problems.map((pr) => h("li", null, h("span", { class: "mono" }, `#${pr.listing_id}`), " ", pr.title ? `${pr.title} — ` : "— ", t(`preview.problem.${pr.code}`))))],
          }),
        );
      }
      if (r.skipped) notes.push(infoNote({ tone: "info", icon: "info", text: t("preview.skipped", { n: r.skipped }) }));
      const rows = r.added.slice(0, 200);
      const tbl = table({
        columns: [
          { key: "pin", label: t("queue.col.pin"), render: (e) => pinCell(e) },
          { key: "due", label: t("queue.col.due"), width: 120, render: (e) => dueLabel(e.due) },
        ],
        rows,
        rowKey: "id",
        class: "pin-preview-table",
      });
      const summary = n
        ? h(
            "div",
            { class: "pin-preview-summary" },
            summaryItem("calendar", t("preview.range"), r.first_due === r.last_due ? dueLabel(r.first_due) : `${dueLabel(r.first_due)} – ${dueLabel(r.last_due)}`),
            summaryItem("layers", t("preview.board"), board ? board.name : body.board_id),
            summaryItem("clock", t("preview.per_day"), t("preview.per_day_value", { n: body.per_day })),
            body.ai_modified ? summaryItem("sparkles", t("preview.ai"), t("preview.ai_on")) : null,
          )
        : null;
      const addBtn = {
        label: t("preview.confirm", { n }),
        icon: "plus",
        variant: "primary",
        autoLoading: true,
        onClick: async ({ close }) => {
          try {
            const done = await ctx.api.post("/api/pinterest/queue", { ...body, dry_run: false });
            close();
            S.picked.clear();
            ctx.toast({ tone: "success", title: t("preview.added", { n: done.added.length }), message: done.first_due ? t("preview.added_msg", { first: dueLabel(done.first_due), last: dueLabel(done.last_due) }) : undefined });
            loadQueue();
            loadListings(false);
            loadStatus();
          } catch (err) {
            ctx.toast({ tone: "danger", title: errText(err) });
          }
        },
      };
      ctx.modal({
        title: n ? t("preview.title", { n }) : t("preview.title_none"),
        subtitle: n ? t("preview.subtitle") : undefined,
        width: 640,
        class: "pin-preview-modal",
        body: [notes, summary, n ? h("div", { class: "pin-preview-list" }, tbl.el) : emptyState({ icon: "check", title: t("preview.nothing"), message: t("preview.nothing_msg"), compact: true })],
        actions: n ? [{ label: t("common.cancel"), variant: "secondary" }, addBtn] : [{ label: t("common.close"), variant: "secondary" }],
      });
    }

    function summaryItem(ic, label, value) {
      return h("div", { class: "pin-sum" }, icon(ic, { size: 14 }), h("span", { class: "pin-sum-label" }, label), h("span", { class: "pin-sum-value" }, value));
    }

    // ------------------------------------------------------------------ the queue
    function buildQueueCard() {
      const tabsEl = tabs({ items: [], value: S.queueTab, onChange: (id) => { S.queueTab = id; S.queuePage = 1; renderQueue(); }, ariaLabel: t("queue.title") });
      const bulk = h("div", { class: "pin-bulk" });
      const progressHost = h("div", { class: "pin-post-progress" });
      const tableHost = h("div", { class: "pin-queue-table" });
      const pager = pagination({ page: 1, pages: 1, onChange: (pg) => { S.queuePage = pg; renderQueue(); } });
      const pagerRow = h("div", { class: "pin-pager" }, h("span", { class: "pin-pager-text muted" }), pager);
      const body = [
        h("div", { class: "pin-queue-toolbar" }, tabsEl.el, h("div", { class: "spacer" }), bulk),
        progressHost,
        tableHost,
        pagerRow,
        infoNote({ tone: "neutral", icon: "info", text: t("queue.note") }),
      ];
      const el = card({ title: t("queue.title"), subtitle: t("queue.subtitle"), icon: "list", iconTone: "neutral", body, class: "pin-queue-card" });
      parts.queue = { el, tabsEl, bulk, progressHost, tableHost, pager, pagerRow };
      mount(queueSlot, el);
      renderQueue();
    }

    function queueRows() {
      const items = (S.queue && S.queue.items) || [];
      const tab = S.queueTab;
      return items.filter((i) => tab === "all" || (tab === "pending" && i.status === "pending") || (tab === "posted" && i.status === "posted") || (tab === "problems" && (i.status === "failed" || i.status === "uncertain")));
    }

    function renderQueue() {
      const p = parts.queue;
      if (!p) return;
      const items = (S.queue && S.queue.items) || [];
      const count = (fn) => items.filter(fn).length;
      p.tabsEl.update(
        [
          { id: "all", label: t("queue.tab.all"), count: items.length },
          { id: "pending", label: t("queue.tab.pending"), count: count((i) => i.status === "pending") },
          { id: "posted", label: t("queue.tab.posted"), count: count((i) => i.status === "posted") },
          { id: "problems", label: t("queue.tab.problems"), count: count((i) => i.status === "failed" || i.status === "uncertain") },
        ],
        S.queueTab,
      );
      renderPostProgress();
      if (S.queueError) {
        mount(p.tableHost, infoNote({ tone: "danger", icon: "alert", text: errText(S.queueError), action: button({ label: t("common.retry"), size: "sm", onClick: loadQueue }) }));
        p.pagerRow.hidden = true;
        mount(p.bulk);
        return;
      }
      if (!S.queue) {
        mount(p.tableHost, skeleton({ lines: 4, height: 16, gap: 14 }));
        p.pagerRow.hidden = true;
        return;
      }
      const rows = queueRows();
      const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
      if (S.queuePage > pages) S.queuePage = pages;
      const start = (S.queuePage - 1) * PAGE_SIZE;
      const pageRows = rows.slice(start, start + PAGE_SIZE);
      const selectable = pageRows.some((r) => r.status !== "posted");
      const tbl = table({
        columns: [
          { key: "pin", label: t("queue.col.pin"), render: (e) => pinCell(e) },
          { key: "board", label: t("queue.col.board"), width: "16%", render: (e) => h("span", { class: "ellipsis pin-board-cell", title: boardName(e) }, boardName(e)) },
          { key: "due", label: t("queue.col.due"), width: 120, render: (e) => h("span", { class: cx("pin-due", isToday(e.due) && e.status === "pending" && "is-today") }, dueLabel(e.due, e.status)) },
          { key: "status", label: t("queue.col.status"), width: 130, render: (e) => statusBadge(e) },
          { key: "actions", label: "", width: 190, align: "right", render: (e) => rowActions(e) },
        ],
        rows: pageRows,
        rowKey: "id",
        selectable,
        selected: new Set([...S.queueSel].filter((id) => pageRows.some((r) => r.id === id))),
        onSelectionChange: (sel) => {
          for (const r of pageRows) S.queueSel.delete(r.id);
          for (const id of sel) S.queueSel.add(id);
          renderBulk();
        },
        rowClass: (e) => cx(e.status === "uncertain" && "is-uncertain", e.status === "failed" && "is-failed"),
        empty: emptyState({ icon: "pin", title: items.length ? t("queue.empty_tab") : t("queue.empty"), message: items.length ? null : t("queue.empty_msg"), compact: true }),
      });
      mount(p.tableHost, tbl.el);
      p.pagerRow.hidden = rows.length <= PAGE_SIZE;
      p.pager.update(S.queuePage, pages);
      p.pagerRow.querySelector(".pin-pager-text").textContent = rows.length ? t("queue.showing", { from: start + 1, to: Math.min(rows.length, start + PAGE_SIZE), n: rows.length }) : "";
      renderBulk();
    }

    function renderBulk() {
      const p = parts.queue;
      if (!p) return;
      const items = (S.queue && S.queue.items) || [];
      const sel = items.filter((i) => S.queueSel.has(i.id));
      const retryable = sel.filter((i) => i.status === "failed" || i.status === "uncertain");
      const removable = sel.filter((i) => i.status !== "posted");
      mount(
        p.bulk,
        sel.length ? h("span", { class: "pin-bulk-count" }, t("queue.selected", { n: sel.length })) : null,
        retryable.length ? button({ label: t("queue.retry"), icon: "refresh", size: "sm", count: retryable.length, onClick: () => retryPins(retryable) }) : null,
        removable.length ? button({ label: t("queue.remove"), icon: "trash", size: "sm", variant: "ghost", class: "pin-danger-ghost", count: removable.length, onClick: () => removePins(removable) }) : null,
      );
    }

    function pinCell(e) {
      return h(
        "div",
        { class: "cell-main" },
        thumb({ src: e.thumb, size: 40, radius: 8 }),
        h(
          "div",
          { class: "pin-cell-text" },
          h("span", { class: "cell-title ellipsis", title: e.title }, e.title || `#${e.listing_id}`),
          h("span", { class: "cell-sub" }, h("span", { class: "mono" }, `#${e.listing_id}`), " · ", t("queue.image", { n: e.rank }), e.ai_modified ? [" · ", t("queue.ai")] : null),
        ),
      );
    }

    function boardName(e) {
      if (e.board_name) return e.board_name;
      const b = (S.boards || []).find((x) => x.id === e.board_id);
      return b ? b.name : e.board_id;
    }

    function statusBadge(e) {
      if (e.status === "posted") return badge({ text: t("pin.status.posted"), tone: "success", icon: "check", size: "sm" });
      if (e.status === "failed") return h("span", { title: e.message || "" }, badge({ text: t("pin.status.failed"), tone: "danger", icon: "x", size: "sm" }));
      if (e.status === "uncertain") return h("span", { title: e.message || "" }, badge({ text: t("pin.status.uncertain"), tone: "warning", icon: "alert", size: "sm" }));
      if (isToday(e.due) || isPast(e.due)) return badge({ text: t("pin.status.due"), tone: "accent", dot: true, size: "sm" });
      return badge({ text: t("pin.status.pending"), tone: "neutral", dot: true, size: "sm" });
    }

    function rowActions(e) {
      if (e.status === "posted") {
        return e.pin_id ? h("a", { class: "pin-row-link", href: `https://www.pinterest.com/pin/${encodeURIComponent(e.pin_id)}/`, target: "_blank", rel: "noopener noreferrer" }, t("queue.open_pin"), icon("external", { size: 12 })) : null;
      }
      return h(
        "div",
        { class: "pin-row-actions" },
        e.status === "failed" || e.status === "uncertain" ? button({ label: t("queue.retry"), size: "sm", variant: "secondary", onClick: () => retryPins([e]) }) : null,
        iconButton({ icon: "trash", title: t("queue.remove_one"), size: "sm", variant: "ghost", onClick: () => removePins([e]) }),
      );
    }

    async function retryPins(list) {
      const uncertain = list.filter((e) => e.status === "uncertain").length;
      const ok = await ctx.confirm({
        title: t("retry.title", { n: list.length }),
        message: uncertain ? t("retry.uncertain_msg") : t("retry.msg"),
        confirmLabel: t("retry.confirm"),
        icon: "refresh",
        danger: uncertain > 0,
      });
      if (!ok) return;
      try {
        const r = await ctx.api.post("/api/pinterest/retry", { ids: list.map((e) => e.id) });
        for (const e of list) S.queueSel.delete(e.id);
        ctx.toast({ tone: "success", title: t("retry.done", { n: r.retried }) });
        loadQueue();
      } catch (err) {
        ctx.toast({ tone: "danger", title: errText(err) });
      }
    }

    async function removePins(list) {
      const ok = await ctx.confirm({ title: t("remove.title", { n: list.length }), message: t("remove.msg"), confirmLabel: t("queue.remove"), danger: true, icon: "trash" });
      if (!ok) return;
      try {
        const r = await ctx.api.post("/api/pinterest/remove", { ids: list.map((e) => e.id) });
        for (const e of list) S.queueSel.delete(e.id);
        ctx.toast({ tone: "success", title: t("remove.done", { n: r.removed }) });
        loadQueue();
        loadListings(false);
      } catch (err) {
        ctx.toast({ tone: "danger", title: errText(err) });
      }
    }

    // ------------------------------------------------------------------ posting
    function isActive(job) {
      return !!job && !FINISHED.has(job.status);
    }

    async function postDue() {
      const q = (S.queue && S.queue.summary) || (S.status && S.status.queue) || { due: 0 };
      const ok = await ctx.confirm({ title: t("post.confirm_title", { n: q.due }), message: t("post.confirm_msg", { n: q.due }), confirmLabel: t("post.confirm"), icon: "send" });
      if (!ok) return;
      postBtn.setLoading(true);
      try {
        // confirm: the seller said yes in the modal above; the server refuses without it.
        const r = await ctx.api.post("/api/pinterest/post", { confirm: true });
        S.postJob = r.job;
        renderPostProgress();
      } catch (err) {
        ctx.toast({ tone: "danger", title: errText(err) });
      } finally {
        postBtn.setLoading(false);
        renderHeader();
      }
    }

    function renderPostProgress() {
      const p = parts.queue;
      if (!p) return;
      const job = S.postJob;
      if (!isActive(job)) {
        mount(p.progressHost);
        return;
      }
      const pr = job.progress || {};
      const bar = progressBar({ value: pr.total ? pr.done : null, max: pr.total || 1 });
      mount(
        p.progressHost,
        h(
          "div",
          { class: "pin-progress" },
          spinner({ size: 16, tone: "accent" }),
          h("div", { class: "pin-progress-text" }, h("p", null, job.status === "queued" ? t("post.queued") : t("post.running", { done: pr.done || 0, total: pr.total || job.params.n || 0 })), bar.el),
          job.cancellable ? button({ label: t("post.stop"), size: "sm", variant: "ghost", onClick: () => ctx.api.post(`/api/jobs/${job.id}/cancel`, {}).catch((err) => ctx.toast({ tone: "danger", title: errText(err) })) }) : null,
        ),
      );
    }

    // ------------------------------------------------------------------ helpers
    function isoToday() {
      const d = new Date();
      return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    }
    function isToday(iso) {
      return iso === isoToday();
    }
    function isPast(iso) {
      return !!iso && iso < isoToday();
    }
    function dueLabel(iso, status = "pending") {
      if (!iso) return "–";
      const today = isoToday();
      if (iso === today) return t("due.today");
      const d = toDate(`${iso}T12:00:00`);
      const tomorrow = new Date();
      tomorrow.setDate(tomorrow.getDate() + 1);
      const tIso = `${tomorrow.getFullYear()}-${String(tomorrow.getMonth() + 1).padStart(2, "0")}-${String(tomorrow.getDate()).padStart(2, "0")}`;
      if (iso === tIso) return t("due.tomorrow");
      if (iso < today && status === "pending") return t("due.overdue", { date: fmtDate(d) });
      return fmtDate(d);
    }

    // ------------------------------------------------------------------ live updates
    const reloadQueue = debounce(() => loadQueue(), 300);
    const settledConnects = new Set(); // job ids already handled (event and poll may both see one)
    function onConnectJob(job) {
      if (FINISHED.has(job.status)) {
        if (settledConnects.has(job.id)) return;
        settledConnects.add(job.id);
        const wasMine = S.connect && S.connect.jobId === job.id;
        S.connect = null;
        closePopup();
        if (job.status === "done") {
          ctx.toast({ tone: "success", title: t("connect.done") });
        } else if (job.status === "error" && wasMine) {
          ctx.toast({ tone: "danger", title: t("connect.failed"), message: errText(job.error || {}), timeout: 9000 });
        }
        S.mode = S.mode === "ready" && job.status === "done" ? null : S.mode; // rebuild the account part
        loadStatus();
      } else if (S.connect && S.connect.jobId === job.id) {
        refreshConnect();
      }
    }
    ctx.events.on("job", (job) => {
      if (!job || job.kind !== "pinterest") return;
      if (job.title_key === CONNECT_TITLE) {
        onConnectJob(job);
        return;
      }
      if (job.title_key === POST_TITLE) {
        S.postJob = job;
        renderPostProgress();
        renderHeader();
        if (FINISHED.has(job.status)) {
          const r = job.result;
          if (job.status === "done" && r) {
            const problems = r.failed + r.uncertain;
            ctx.toast({ tone: problems ? "warning" : "success", title: t("post.done", { n: r.posted }), message: problems ? t("post.done_problems", { n: problems }) : undefined });
          } else if (job.status === "error") {
            ctx.toast({ tone: "danger", title: t("post.failed"), message: errText(job.error || {}), timeout: 9000 });
          }
          loadQueue();
          loadListings(false);
        } else {
          reloadQueue();
        }
      }
    });
    ctx.events.on("job-event", (ev) => {
      if (!ev || ev.kind !== "pinterest") return;
      if (ev.type === "phase" && S.connect && S.connect.jobId === ev.job_id) {
        S.connect.phase = ev.data.phase;
        refreshConnect();
      }
    });
    // The consent tab reached /oauth-done (app.js relays it; that tab has no session and
    // makes no API call). The job's own event follows; ask now in case it was missed.
    ctx.events.on("oauth-done", async (msg) => {
      if (!msg || msg.service !== "pinterest" || !S.connect || !S.connect.jobId) return;
      const jobId = S.connect.jobId;
      S.connect.phase = "exchanging";
      refreshConnect();
      await new Promise((resolve) => setTimeout(resolve, 1200));
      try {
        const job = await ctx.api.get(`/api/jobs/${encodeURIComponent(jobId)}`, null, { signal: ctx.signal });
        if (job && ctx.isActive()) onConnectJob(job);
      } catch {
        /* the job event arrives anyway */
      }
    });

    await loadActiveJobs();
    await loadStatus();

    return () => {
      reloadQueue.cancel();
    };
  },
};
