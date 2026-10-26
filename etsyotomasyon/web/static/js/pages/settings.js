// Ayarlar (/ayarlar): language, hiding the shop name, the version and new-version
// checks, quitting; the shops on this computer; the products folder; the Etsy
// connection; the setup checklist; about.
// Two columns of cards, in the same visual language as the setup pages.

import { relative } from "../format.js";
import { icon, logoMark } from "../icons.js";
import { badge, button, card, cx, field, h, infoNote, mount, select, skeleton, textInput, toggle } from "../ui.js";

const DOCTOR_ICON = { ok: "check", warn: "alert", missing: "x", unknown: "help" };
const DOCTOR_TONE = { ok: "success", warn: "warning", missing: "danger", unknown: "muted" };
const STATE_TONE = { connected: "success", reconnect: "warning", offline: "warning", error: "warning", bad_keys: "danger" };
const SUBFOLDERS = ["mockups", "products", "drafts"];
const RELEASE_URL = /^https:\/\/github\.com\//;

/**
 * A path in running text that wraps after its \ and / separators, not inside a folder
 * name ("Etsy Studio - 12345", "my-shop-files"): each name moves to the next line whole, and
 * only a name longer than the whole line breaks inside (settings.css .st-path-part).
 */
function pathNode(path) {
  const kids = [];
  let part = "";
  const flush = () => {
    if (!part) return;
    kids.push(h("span", { class: "st-path-part" }, part));
    part = "";
  };
  for (const ch of String(path || "")) {
    part += ch;
    if (ch === "\\" || ch === "/") {
      flush();
      kids.push(h("wbr"));
    }
  }
  flush();
  return h("span", { class: "st-note-path", title: path }, kids);
}

/** t(key, {[name]: path}) as text with the path as a pathNode(). */
function withPath(t, key, name, path) {
  const mark = "\u0000";
  const text = t(key, { [name]: mark });
  const at = text.indexOf(mark);
  if (at < 0) return text;
  return [text.slice(0, at), pathNode(path), text.slice(at + mark.length)];
}

export default {
  async mount(el, ctx) {
    const t = ctx.t;
    const data = { session: ctx.session() || {}, settings: null, info: null, shops: null, doctor: null, update: null };
    let editingFolder = false;
    let doctorRunning = false;

    const cols = [h("div", { class: "st-col" }), h("div", { class: "st-col" })];
    el.append(h("div", { class: "st-grid" }, cols));

    const general = card({ title: t("general.title"), subtitle: t("general.sub"), icon: "settings", class: "st-card" });
    const etsy = card({ title: t("etsy.title"), subtitle: t("etsy.sub"), icon: "link", class: "st-card" });
    const doctorBtn = button({ label: t("doctor.run"), icon: "shield-check", size: "sm", onClick: () => runDoctor() });
    const doctor = card({ title: t("doctor.title"), subtitle: t("doctor.sub"), icon: "shield-check", actions: [doctorBtn], class: "st-card" });
    const addShopBtn = button({ label: t("shops.add"), icon: "plus", size: "sm", autoLoading: true, onClick: () => addShop() });
    const shops = card({ title: t("shops.title"), subtitle: t("shops.sub"), icon: "store", actions: [addShopBtn], class: "st-card" });
    const folders = card({ title: t("folders.title"), subtitle: t("folders.sub"), icon: "folder", class: "st-card" });
    const about = card({ title: t("about.title"), subtitle: t("about.sub"), icon: "info", class: "st-card" });
    cols[0].append(general, etsy, doctor);
    cols[1].append(shops, folders, about);
    for (const c of [etsy, shops, folders]) mount(c.body, skeleton({ lines: 3, height: 12 }));

    const errorNote = (err, retry) =>
      infoNote({
        icon: "alert-circle",
        tone: "danger",
        text: ctx.api.errorText(err, t),
        action: retry ? button({ label: t("common.retry"), icon: "refresh", size: "sm", autoLoading: true, onClick: retry }) : null,
      });
    const toastError = (err) => ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });

    function row(label, sub, control, extraClass) {
      return h(
        "div",
        { class: cx("st-row", extraClass) },
        h("div", { class: "st-row-text" }, h("span", { class: "st-row-label" }, label), sub ? h("span", { class: "st-row-sub" }, sub) : null),
        control ? h("div", { class: "st-row-control" }, control) : null,
      );
    }

    // ---- Genel
    function renderGeneral() {
      const lang = select({
        options: [
          { value: "tr", label: t("lang.tr") },
          { value: "en", label: t("lang.en") },
        ],
        value: data.session.language || ctx.lang,
        ariaLabel: t("general.language"),
        onChange: (v) => ctx.setLanguage(v).catch(toastError),
      });
      const hide = toggle({
        checked: !!data.session.anonymise,
        ariaLabel: t("general.anonymise"),
        onChange: async (v) => {
          try {
            await ctx.api.post("/api/prefs", { anonymise: v });
            location.reload(); // every page shows names: start them all again
          } catch (err) {
            hide.update(!v);
            toastError(err);
          }
        },
      });
      const quitBtn = button({ label: t("general.quit_button"), icon: "power", variant: "secondary", onClick: () => quit() });
      mount(
        general.body,
        h(
          "div",
          { class: "st-rows" },
          row(t("general.language"), t("general.language_sub"), lang),
          row(t("general.anonymise"), t("general.anonymise_sub"), hide),
          row(t("general.version"), upd.line, h("div", { class: "st-update-ctrl" }, upd.version, upd.checkBtn)),
          row(t("update.auto"), t("update.auto_sub"), upd.auto),
          row(t("general.quit"), t("general.quit_sub"), quitBtn),
        ),
      );
      renderUpdate();
    }

    // ---- Sürüm: the new-version check (GET /api/update, pushed on "update")
    const upd = {
      line: h("span", { class: "st-update-line" }),
      version: h("span", { class: "mono st-version" }),
      checkBtn: button({ label: t("update.check"), icon: "refresh", size: "sm", autoLoading: true, onClick: () => checkUpdate() }),
      auto: toggle({ checked: true, ariaLabel: t("update.auto"), onChange: (v) => setAutoUpdate(v) }),
    };

    function renderUpdate() {
      const u = data.update;
      upd.version.textContent = `v${(u && !(u instanceof Error) && u.current) || data.session.version || ""}`;
      if (!u || u instanceof Error) {
        mount(upd.line, u ? ctx.api.errorText(u, t) : "");
        return;
      }
      upd.checkBtn.setDisabled(!!u.blocked);
      upd.auto.update(!!u.auto);
      upd.auto.disabled = !!u.blocked;
      const parts = [];
      const latest = u.latest;
      if (u.blocked) parts.push(t("update.blocked"));
      else if (u.available && latest && RELEASE_URL.test(latest.url || "")) {
        const version = `v${latest.version}`;
        parts.push(h("span", { class: "st-update-new" }, t("update.available", { version })));
        parts.push(h("a", { href: latest.url, target: "_blank", rel: "noopener noreferrer", class: "st-ext" }, t("update.download"), icon("external", { size: 12 })));
        parts.push(h("button", { type: "button", class: "st-link", onClick: () => ctx.showReleaseNotes(u) }, t("update.whats_new")));
      } else if (u.checked_at) parts.push(t("update.up_to_date"));
      if (!u.blocked) {
        if (u.checked_at) parts.push(t("update.checked", { when: relative(u.checked_at) }));
        else if (!u.error) parts.push(t("update.never"));
        if (u.error) parts.push(h("span", { class: "st-update-err" }, t("update.last_failed", { reason: t(`update.error.${u.error}`) })));
      }
      mount(upd.line, parts.flatMap((p, i) => (i ? [" · ", p] : [p])));
    }

    async function loadUpdate() {
      try {
        data.update = await ctx.api.get("/api/update", null, { signal: ctx.signal });
      } catch (err) {
        if (ctx.api.isAbort(err)) return;
        data.update = err;
      }
      renderUpdate();
    }

    async function checkUpdate() {
      try {
        const u = await ctx.api.post("/api/update/check", {}, { signal: ctx.signal });
        data.update = u;
        renderUpdate();
        if (u.error) ctx.toast({ tone: "warning", title: t("update.toast_failed"), message: t(`update.error.${u.error}`) });
        else if (u.available && u.latest) ctx.toast({ tone: "info", title: t("update.toast_new", { version: `v${u.latest.version}` }) });
        else ctx.toast({ tone: "success", title: t("update.toast_current"), message: t("update.toast_current_msg", { version: `v${u.current}` }) });
      } catch (err) {
        if (ctx.api.isAbort(err)) return;
        toastError(err);
      }
    }

    async function setAutoUpdate(on) {
      try {
        data.update = await ctx.api.post("/api/update/auto", { enabled: on });
        renderUpdate();
      } catch (err) {
        upd.auto.update(!on);
        toastError(err);
      }
    }

    async function quit() {
      // The app's own flow: confirm, quit (a running task asks again), the stopped page.
      await ctx.quit();
    }

    // ---- Mağazalar
    function renderShops() {
      if (data.shops instanceof Error) {
        mount(shops.body, errorNote(data.shops, loadShops));
        return;
      }
      const list = (data.shops && data.shops.shops) || [];
      mount(
        shops.body,
        h(
          "ul",
          { class: "st-shops" },
          list.map((s, i) => {
            const name = s.name || s.label || t("shop.unnamed", { n: i + 1 });
            const actions = [];
            if (!s.current) actions.push(button({ label: t("shops.switch"), size: "sm", autoLoading: true, onClick: () => switchShop(s) }));
            if (s.id) {
              actions.push(
                button({ label: t("shops.remove"), icon: "trash", size: "sm", variant: "ghost", class: "st-danger-ghost", onClick: () => removeShop(s, name) }),
              );
            }
            return h(
              "li",
              { class: cx("st-shop", s.current && "is-current") },
              h("span", { class: "st-shop-icon" }, icon("store", { size: 16 })),
              h(
                "span",
                { class: "st-shop-text" },
                h("span", { class: "st-shop-name" }, h("span", { class: "ellipsis" }, name), s.current ? badge({ text: t("shops.current"), tone: "accent", size: "sm" }) : null),
                h(
                  "span",
                  { class: cx("st-shop-state", s.connected ? "tone-success" : "tone-muted") },
                  h("span", { class: "dot", "aria-hidden": "true" }),
                  s.connected ? t("shops.connected") : t("shops.not_connected"),
                ),
              ),
              h("span", { class: "st-shop-actions" }, actions),
            );
          }),
        ),
        list.length && !list.some((s) => s.id) ? null : h("p", { class: "st-muted-note" }, t("shops.base_hint")),
      );
    }

    async function switchShop(s) {
      try {
        await ctx.api.post("/api/shops/switch", { id: s.id });
        // The app reopens this page for the other shop when the switch event arrives.
      } catch (err) {
        toastError(err);
      }
    }

    async function removeShop(s, name) {
      const ok = await ctx.confirm({
        title: t("shops.remove_title", { name }),
        message: t("shops.remove_msg"),
        confirmLabel: t("shops.remove"),
        danger: true,
        icon: "trash",
      });
      if (!ok) return;
      try {
        data.shops = await ctx.api.post("/api/shops/remove", { id: s.id, confirm: true });
        ctx.toast({ tone: "success", title: t("shops.removed", { name }) });
        renderShops();
      } catch (err) {
        toastError(err);
      }
    }

    async function addShop() {
      try {
        await ctx.api.post("/api/shops/add", {});
        ctx.toast({ tone: "success", title: t("shops.added"), message: t("shops.added_msg") });
        ctx.navigate("/kurulum/magaza");
      } catch (err) {
        toastError(err);
      }
    }

    async function loadShops() {
      try {
        data.shops = await ctx.api.get("/api/shops", null, { signal: ctx.signal });
      } catch (err) {
        if (ctx.api.isAbort(err)) return;
        data.shops = err;
      }
      renderShops();
    }

    // ---- Klasörler
    function renderFolders() {
      if (data.settings instanceof Error) {
        mount(folders.body, errorNote(data.settings, loadSettings));
        return;
      }
      if (!data.settings) return;
      const ws = data.settings.workspace;
      const byWhich = Object.fromEntries(ws.folders.map((f) => [f.which, f]));
      const openBtn = (which, label, variant = "secondary") =>
        button({ label, icon: "folder-open", size: "sm", variant, autoLoading: true, onClick: () => openFolder(which) });
      const kids = [
        h(
          "div",
          { class: "st-folder-root" },
          h(
            "div",
            { class: "st-folder-head" },
            h("span", { class: "st-row-label" }, t("folders.root")),
            badge({ text: ws.custom ? t("folders.custom") : t("folders.default"), tone: ws.custom ? "accent" : "neutral", size: "sm" }),
          ),
          h("div", { class: "st-path mono", title: ws.root }, ws.root),
          editingFolder
            ? null
            : h(
                "div",
                { class: "st-folder-actions" },
                openBtn("workspace", t("folders.open")),
                button({
                  label: t("folders.change"),
                  icon: "edit",
                  size: "sm",
                  variant: "ghost",
                  onClick: () => {
                    editingFolder = true;
                    renderFolders();
                  },
                }),
              ),
        ),
      ];
      if (editingFolder) kids.push(folderForm(ws));
      else if (ws.nested_in) {
        // Saved by an older version: a folder of another products folder (2-PRODUCTS, ...).
        const note = infoNote({
          tone: "warning",
          icon: "alert",
          text: withPath(t, "folders.nested", "root", ws.nested_in),
          action: button({ label: t("folders.use_parent"), size: "sm", autoLoading: true, onClick: () => useFolder(ws.nested_in).catch(toastError) }),
        });
        note.classList.add("st-nested-note"); // the button below the text: room for the path
        kids.push(note);
      }
      kids.push(
        h(
          "ul",
          { class: "st-subfolders" },
          SUBFOLDERS.map((which) => {
            const f = byWhich[which] || { name: which, path: "", exists: false };
            return h(
              "li",
              { class: "st-subfolder" },
              h("span", { class: "st-subfolder-icon" }, icon("folder", { size: 15 })),
              h(
                "span",
                { class: "st-subfolder-text" },
                h("span", { class: "st-subfolder-name" }, t(`folders.${which}`)),
                h("span", { class: "mono st-subfolder-dir", title: f.path }, f.name, f.exists ? null : h("span", { class: "st-missing" }, ` · ${t("folders.missing")}`)),
              ),
              openBtn(which, t("common.open"), "ghost"),
            );
          }),
        ),
      );
      mount(folders.body, kids);
    }

    function folderForm(ws) {
      const input = textInput({ mono: true, value: ws.custom ? ws.root : "", placeholder: t("folders.change_ph") });
      const f = field({ label: t("folders.change_label"), hint: t("folders.change_hint"), input });
      const save = button({ label: t("folders.save"), variant: "primary", size: "sm", type: "submit" });
      const submit = async (path) => {
        f.setError("");
        save.setLoading(true);
        try {
          await useFolder(path);
        } catch (err) {
          f.setError(ctx.api.errorText(err, t));
        } finally {
          save.setLoading(false);
        }
      };
      requestAnimationFrame(() => input.focus());
      return h(
        "form",
        {
          class: "st-folder-form",
          onSubmit: (e) => {
            e.preventDefault();
            submit(input.value.trim());
          },
        },
        f,
        h(
          "div",
          { class: "st-form-actions" },
          save,
          button({
            label: t("folders.cancel"),
            size: "sm",
            variant: "ghost",
            onClick: () => {
              editingFolder = false;
              renderFolders();
            },
          }),
          ws.custom ? h("span", { class: "spacer" }) : null,
          ws.custom ? button({ label: t("folders.reset"), icon: "undo", size: "sm", variant: "ghost", onClick: () => submit("") }) : null,
        ),
      );
    }

    /** Save the products folder ("" = the default); throws the API error for the caller to show. */
    async function useFolder(path) {
      const res = await ctx.api.post("/api/settings/workspace", { path });
      data.settings.workspace = res.workspace;
      editingFolder = false;
      renderFolders();
      const adj = res.adjusted;
      if (adj) {
        // A folder of a products folder was picked: the server kept the main one instead.
        // (inside false: the folder is only named like one, so its parent was used.)
        const name = adj.chosen.split(/[\\/]/).filter(Boolean).pop() || adj.chosen;
        const msg = adj.inside === false ? "folders.adjusted_named_msg" : "folders.adjusted_msg";
        ctx.toast({ tone: "info", title: t("folders.adjusted_title"), message: t(msg, { name, root: adj.root }), timeout: 9000 });
      } else {
        ctx.toast({ tone: "success", title: t("folders.saved"), message: res.workspace.root });
      }
      const moved = res.migrated || [];
      if (moved.length) {
        // A nested products folder's template and upload history came along.
        const items = moved.map((k) => t(`folders.migrated_${k}`)).join(", ");
        ctx.toast({ tone: "info", title: t("folders.migrated", { items }), timeout: 9000 });
      }
    }

    async function openFolder(which) {
      try {
        await ctx.api.post("/api/open-folder", { which });
      } catch (err) {
        toastError(err);
      }
    }

    async function loadSettings() {
      try {
        data.settings = await ctx.api.get("/api/settings", null, { signal: ctx.signal });
      } catch (err) {
        if (ctx.api.isAbort(err)) return;
        data.settings = err;
      }
      renderFolders();
      renderAbout();
    }

    // ---- Etsy bağlantısı
    function renderEtsy() {
      const st = ctx.status() || {};
      const state = st.state || "checking";
      const info = data.info instanceof Error ? null : data.info;
      if (data.info instanceof Error) {
        mount(etsy.body, errorNote(data.info, loadInfo));
        return;
      }
      if (!info) return;
      const shopName = (st.shop && st.shop.name) || info.shop_name;
      const lines = [
        h(
          "div",
          { class: cx("st-etsy-state", `tone-${STATE_TONE[state] || "muted"}`) },
          h("span", { class: "dot", "aria-hidden": "true" }),
          h("span", null, t(`status.${state}`)),
          shopName ? h("b", { class: "ellipsis" }, shopName) : null,
        ),
        h(
          "dl",
          { class: "st-facts" },
          h("dt", null, "Keystring"),
          h("dd", { class: "mono" }, info.keys ? t("etsy.keys", { prefix: info.keystring_prefix, n: info.secret_length }) : t("etsy.no_keys")),
          h("dt", null, t("etsy.callback")),
          h("dd", { class: "mono" }, info.redirect_uri),
        ),
      ];
      if (st.quota_remaining !== null && st.quota_remaining !== undefined) {
        lines.push(h("p", { class: "st-muted-note" }, t("etsy.quota", { n: st.quota_remaining })));
      }
      const actions = [button({ label: t("etsy.go"), iconRight: "arrow-right", size: "sm", onClick: () => ctx.navigate("/kurulum/magaza") })];
      if (info.keys) {
        // Mağaza Bağlantısı opens straight on the key form (its card has no button for it).
        actions.push(button({ label: t("etsy.keys_edit"), icon: "key", size: "sm", variant: "ghost", onClick: () => ctx.navigate("/kurulum/magaza?keys=1") }));
      }
      if (info.connected) {
        actions.push(button({ label: t("etsy.disconnect"), icon: "logout", size: "sm", variant: "ghost", class: "st-danger-ghost", onClick: () => disconnect() }));
      }
      lines.push(h("div", { class: "st-actions" }, actions));
      mount(etsy.body, lines);
    }

    async function disconnect() {
      const ok = await ctx.confirm({ title: t("etsy.disconnect_title"), message: t("etsy.disconnect_msg"), confirmLabel: t("etsy.disconnect"), danger: true, icon: "logout" });
      if (!ok) return;
      try {
        await ctx.api.post("/api/connect/disconnect", {});
        ctx.toast({ tone: "info", title: t("etsy.disconnected") });
        await loadInfo();
      } catch (err) {
        toastError(err);
      }
    }

    async function loadInfo() {
      try {
        data.info = await ctx.api.get("/api/connect/info", null, { signal: ctx.signal });
      } catch (err) {
        if (ctx.api.isAbort(err)) return;
        data.info = err;
      }
      renderEtsy();
    }

    // ---- Kontrol listesi
    function renderDoctor() {
      doctorBtn.setLabel(data.doctor && !(data.doctor instanceof Error) ? t("doctor.again") : t("doctor.run"));
      doctorBtn.setLoading(doctorRunning);
      if (data.doctor instanceof Error) {
        mount(doctor.body, errorNote(data.doctor, runDoctor));
        return;
      }
      if (!data.doctor) {
        mount(doctor.body, h("p", { class: "st-muted-note" }, t("doctor.intro")), doctorRunning ? skeleton({ lines: 4, height: 12 }) : null);
        return;
      }
      const d = data.doctor;
      const requiredOk = d.items.every((it) => !it.required || it.state === "ok");
      mount(
        doctor.body,
        h(
          "div",
          { class: "st-doctor-sum" },
          badge({ text: t("doctor.summary", { ok: d.ok, total: d.total }), tone: requiredOk ? "success" : "warning", icon: requiredOk ? "check" : "alert" }),
          h("span", { class: "st-muted-note" }, t("doctor.checked", { when: relative(d.checked_at) })),
        ),
        h("ol", { class: "st-checks" }, d.items.map(checkItem)),
      );
    }

    // Step 10 (the products folder) has three different gaps; its English detail says which.
    function folderGap(it) {
      const detail = it.detail || "";
      if (/no drop workspace/i.test(detail)) return "folder";
      if (/template/i.test(detail) && it.state === "missing") return "template";
      return "mockups";
    }

    function helpText(it) {
      if (it.state === "ok") return null;
      if (it.number === 10) return t(`doctor.help.10_${folderGap(it)}`);
      if (it.state === "unknown") return t.has(`doctor.ask.${it.number}`) ? t(`doctor.ask.${it.number}`) : null;
      if (it.state === "warn" && t.has(`doctor.help.${it.number}_warn`)) return t(`doctor.help.${it.number}_warn`);
      return t.has(`doctor.help.${it.number}`) ? t(`doctor.help.${it.number}`) : null;
    }

    function fixLink(it) {
      if (it.state === "ok") return null;
      let href = null;
      if (it.number >= 4 && it.number <= 9) href = "/kurulum/magaza";
      else if (it.number === 10) href = folderGap(it) === "template" ? "/kurulum/sablon" : "/kurulum/mockuplar";
      if (!href) return null;
      return h("a", { href, class: "st-fix" }, t("doctor.fix"), icon("arrow-right", { size: 12 }));
    }

    function checkItem(it) {
      const title = t.has(`doctor.step.${it.number}`) ? t(`doctor.step.${it.number}`) : it.title;
      const help = helpText(it);
      return h(
        "li",
        { class: cx("st-check", `is-${it.state}`) },
        h(
          "span",
          { class: cx("st-check-icon", `tone-${DOCTOR_TONE[it.state] || "muted"}`), title: t(`doctor.state.${it.state}`) },
          icon(DOCTOR_ICON[it.state] || "help", { size: 13, strokeWidth: 2.4 }),
        ),
        h(
          "div",
          { class: "st-check-text" },
          h("span", { class: "st-check-title" }, title, it.required ? null : h("span", { class: "st-optional" }, ` · ${t("common.optional")}`)),
          help ? h("span", { class: "st-check-help" }, help) : null,
          it.detail ? h("span", { class: "st-check-detail mono", lang: "en" }, it.detail) : null,
        ),
        fixLink(it),
      );
    }

    async function runDoctor() {
      if (doctorRunning) return;
      doctorRunning = true;
      renderDoctor();
      try {
        data.doctor = await ctx.api.post("/api/settings/doctor", {}, { signal: ctx.signal });
      } catch (err) {
        if (ctx.api.isAbort(err)) return;
        data.doctor = err;
      } finally {
        doctorRunning = false;
      }
      renderDoctor();
    }

    // ---- Hakkında
    function renderAbout() {
      const s = data.settings instanceof Error ? null : data.settings;
      const version = (s && s.version) || data.session.version || "";
      mount(
        about.body,
        h(
          "div",
          { class: "st-about-head" },
          logoMark({ size: 36 }),
          h("div", null, h("b", null, t("about.version", { version })), h("span", { class: "st-row-sub" }, t("app.tagline"))),
        ),
        h(
          "dl",
          { class: "st-facts" },
          h("dt", null, t("about.license")),
          h("dd", null, t("about.license_value")),
          h("dt", null, t("about.repo")),
          h(
            "dd",
            null,
            s ? h("a", { href: s.repo, target: "_blank", rel: "noopener noreferrer", class: "st-ext" }, s.repo.replace(/^https:\/\//, ""), icon("external", { size: 12 })) : "",
          ),
          h("dt", null, t("about.keys_file")),
          h("dd", { class: "mono st-path-small", title: s ? s.env_file : "" }, s ? s.env_file : ""),
        ),
        h("p", { class: "st-muted-note" }, t("about.local")),
        h("p", { class: "st-trademark", lang: "en" }, t("app.trademark")),
      );
    }

    // ---- start
    renderGeneral();
    renderDoctor();
    renderAbout();
    ctx.onStatus(() => renderEtsy());
    ctx.events.on("update", (u) => {
      if (!u || typeof u !== "object") return;
      data.update = u;
      renderUpdate();
    });
    const clock = setInterval(renderUpdate, 60000); // "checked 5 min ago" keeps up
    await Promise.all([loadSettings(), loadInfo(), loadShops(), loadUpdate()]);
    return () => clearInterval(clock);
  },
};
