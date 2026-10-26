// Taslak İlan: one listing, its images and its words (frames t015 while loading, t230).
//
// GET /api/listings/:id; edits go back with PATCH (a live listing asks first), a draft
// is published with POST /api/listings/:id/publish after a confirm. The arrows in the
// header walk the list the seller came from (ids kept in sessionStorage by İlanlar).

import {
  badge,
  button,
  card,
  cx,
  emptyState,
  h,
  iconButton,
  infoNote,
  mount,
  popover,
  skeleton,
  tagChip,
  textArea,
  thumb,
} from "../ui.js";
import { icon } from "../icons.js";
import { bytes, money } from "../format.js";

const NAV_KEY = "etsyotomasyon.listings.nav";
const STATE_TONE = { draft: "warning", active: "success", inactive: "muted", expired: "danger", sold_out: "neutral" };
const SLOT_WIDTHS = [138, 96, 150, 120, 112, 132, 104, 144, 98, 126, 116, 140, 108];
// Etsy's rules (OAS updateListing): title letters, digits, punctuation, maths symbols,
// spaces and ™©®, with % : & + at most once each; tags letters, digits, spaces, - ' ™©®.
const TITLE_BAD = /[^\p{L}\p{Nd}\p{P}\p{Sm}\p{Zs}™©®]/gu;
const TAG_BAD = /[^\p{L}\p{Nd}\p{Zs}\-'™©®]/gu;
const TITLE_ONCE = ["%", ":", "&", "+"];
const MIN_SLOTS = 10;
const STATES = ["draft", "active", "inactive", "expired", "sold_out"];
// What the print-file row takes (drop/printfiles.py PRINT_SUFFIXES).
const PRINT_ACCEPT = ".png,.jpg,.jpeg,.webp,.gif,.bmp,.tif,.tiff,.pdf,.svg,.psd,.ai,.eps,.zip";

/** badge() with extra classes (the mono counters). */
function tagBadge(opts, cls) {
  const b = badge(opts);
  if (cls) b.classList.add(...cls.split(" "));
  return b;
}

function readNav() {
  try {
    const v = JSON.parse(sessionStorage.getItem(NAV_KEY) || "null");
    return v && Array.isArray(v.ids) ? v : null;
  } catch {
    return null;
  }
}

const squash = (s) => String(s ?? "").replace(/\s+/g, " ").trim();

/** A listing's address: /ilanlar/taslak/<id> for a draft (the video's), else /ilanlar/<id>. */
export function listingPath(id, state) {
  return state === "draft" ? `/ilanlar/taslak/${id}` : `/ilanlar/${id}`;
}

/** Etsy's category name as a string key: "Tops & Tees" -> "tops_tees", "T-shirts" -> "t_shirts". */
export function catSlug(name) {
  return String(name || "")
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

export default {
  async mount(el, ctx) {
    const t = ctx.t;
    const id = Number.parseInt(ctx.params.id, 10);
    const st = {
      data: null,
      draft: null, // {title, tags, description} being edited
      image: 0,
      lastTag: null,
      busy: false,
      tagError: "",
    };

    // ------------------------------------------------------------ header: prev / next
    const nav = readNav();
    const pos = nav ? nav.ids.indexOf(id) : -1;
    if (nav && pos >= 0 && nav.ids.length > 1) {
      const go = (delta) => {
        const next = nav.ids[pos + delta];
        if (next === undefined) return;
        // A walk through drafts stays on draft addresses (setData corrects any other);
        // the leave guard asks about unsaved edits.
        const draft = st.data ? (st.data.listing.state || "draft") === "draft" : location.pathname.startsWith("/ilanlar/taslak/");
        ctx.navigate(listingPath(next, draft ? "draft" : ""));
      };
      ctx.setHeader({
        actions: [
          h(
            "div",
            { class: "ld-pager" },
            iconButton({ icon: "arrow-left", title: t("nav.prev"), disabled: pos === 0, onClick: () => go(-1) }),
            h("span", { class: "ld-pager-pos num" }, h("b", null, String(pos + 1)), ` / ${nav.ids.length}`),
            iconButton({ icon: "arrow-right", title: t("nav.next"), disabled: pos >= nav.ids.length - 1, onClick: () => go(1) }),
          ),
        ],
      });
    }

    if (!Number.isFinite(id) || id <= 0) {
      showProblem({ code: "not_found" });
      return () => {};
    }

    const status = ctx.status();
    if (status && (status.state === "keys" || status.state === "disconnected")) {
      showProblem({ code: "setup_needed" });
      const off = ctx.onStatus((s) => {
        if (s && s.state === "connected") {
          off();
          ctx.remount();
        }
      });
      return () => {};
    }

    // ------------------------------------------------------------ loading view (t015)
    el.append(loadingView());

    let data;
    try {
      data = await ctx.api.get(`/api/listings/${id}`, null, { signal: ctx.signal });
    } catch (err) {
      if (ctx.api.isAbort(err)) return () => {};
      showProblem(err);
      return () => {};
    }
    setData(data);

    // Unsaved edits: the app asks this guard before any in-app navigation (the arrows,
    // the sidebar, Back), a shop switch or a language change; syncDirty() keeps the
    // app's flag current, so a reload or closing the tab asks as well.
    ctx.onBeforeLeave(() => leaveOk());
    const onKey = (e) => {
      if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
      const tag = (e.target && e.target.tagName) || "";
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || document.querySelector(".modal-backdrop")) return;
      const n = (st.data && st.data.images.length) || 0;
      if (!n) return;
      if (e.key === "ArrowRight") showImage((st.image + 1) % n);
      else if (e.key === "ArrowLeft") showImage((st.image - 1 + n) % n);
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };

    // ============================================================ helpers

    function setData(d) {
      st.data = d;
      st.draft = { title: d.listing.title, tags: [...d.listing.tags], description: d.listing.description };
      st.image = Math.min(st.image, Math.max(0, d.images.length - 1));
      const state = d.listing.state || "draft";
      const known = STATES.includes(state);
      ctx.setHeader({ title: known ? t(`head.${state}`) : t("title"), subtitle: known ? t(`sub.${state}`) : "" });
      showPath(state);
      render();
    }

    // The address bar names what the listing is now: /ilanlar/taslak/<id> for a draft
    // (the video's), /ilanlar/<id> otherwise, also after publishing. Same history entry
    // (its {idx} kept); ctx.setQuery then brings the app's own record of it up to date.
    function showPath(state) {
      const want = listingPath(id, state);
      if (!ctx.isActive() || location.pathname === want) return;
      history.replaceState(history.state, "", want + location.search + location.hash);
      ctx.setQuery({});
    }

    function isDirty() {
      if (!st.data || !st.draft) return false;
      const l = st.data.listing;
      return (
        squash(st.draft.title) !== squash(l.title) ||
        st.draft.description.trim() !== String(l.description || "").trim() ||
        JSON.stringify(st.draft.tags) !== JSON.stringify(l.tags)
      );
    }

    async function leaveOk() {
      if (!isDirty()) return true;
      return ctx.confirm({
        title: t("leave.title"),
        message: t("leave.message"),
        confirmLabel: t("leave.confirm"),
        danger: true,
      });
    }

    function typeText(kind, name) {
      if (kind && kind !== "other") return t(`type.${kind}`);
      return name || "";
    }

    function showProblem(err) {
      const code = err && err.code;
      let node;
      if (code === "setup_needed") {
        node = emptyState({
          icon: "link",
          title: t("problem.setup_title"),
          message: t("problem.setup_msg"),
          action: button({ label: t("problem.setup_action"), iconRight: "arrow-right", variant: "primary", onClick: () => ctx.navigate("/kurulum/magaza") }),
        });
      } else if (code === "not_found") {
        node = emptyState({
          icon: "search",
          title: t("problem.not_found"),
          message: t("problem.not_found_msg"),
          action: button({ label: t("problem.back"), icon: "arrow-left", variant: "secondary", onClick: () => ctx.navigate("/ilanlar") }),
        });
      } else {
        node = infoNote({
          icon: "alert",
          tone: "danger",
          title: t("problem.error_title"),
          text: ctx.api.errorText(err, t),
          action: button({ label: t("common.retry"), icon: "refresh", size: "sm", onClick: () => ctx.remount() }),
        });
      }
      ctx.setHeader({ title: t("title"), subtitle: "" });
      mount(el, card({ class: "ld-problem", body: node }));
    }

    // ------------------------------------------------------------ the loading skeleton
    function loadingView() {
      const slots = [];
      for (let i = 0; i < MIN_SLOTS; i += 1) slots.push(h("span", { class: "ld-thumb is-empty" }));
      const tagSlots = SLOT_WIDTHS.map((w) => h("span", { class: "tagchip is-slot", style: { width: w } }));
      return h(
        "div",
        { class: "ld-grid is-loading", "aria-busy": "true" },
        h(
          "div",
          { class: "ld-left" },
          h("div", { class: "ld-hero" }, h("span", { class: "skeleton ld-hero-skel" })),
          h("div", { class: "ld-strip-head" }, h("b", null, t("images.title")), h("span", { class: "muted" }, t("common.loading"))),
          h("div", { class: "ld-strip" }, slots),
        ),
        h(
          "div",
          { class: "ld-right" },
          h(
            "div",
            { class: "ld-source" },
            h("span", { class: "skeleton", style: { width: 32, height: 32, borderRadius: 8 } }),
            h("div", { class: "ld-source-text" }, skeleton({ lines: 2, height: 10, widths: ["60%", "40%"], gap: 6 })),
            h("div", { class: "spacer" }),
            badge({ text: t("seo.checking"), icon: "clock", tone: "neutral" }),
          ),
          card({
            class: "ld-card",
            title: t("title_card.label"),
            icon: "loader",
            iconTone: "neutral",
            actions: tagBadge({ text: "0/140", tone: "neutral" }, "mono ld-counter"),
            body: [skeleton({ lines: 3, height: 11, widths: ["92%", "80%", "30%"], gap: 14 }), h("div", { class: "ld-title-bar" }, h("span", { class: "ld-bar" }))],
          }),
          card({
            class: "ld-card",
            title: [t("tags.label"), " ", h("span", { class: "ld-hint" }, t("tags.hint", { max: 20 }))],
            icon: "tag",
            iconTone: "neutral",
            actions: tagBadge({ text: "0/13", tone: "neutral" }, "mono ld-counter"),
            body: h("div", { class: "ld-tags" }, tagSlots),
          }),
          h(
            "div",
            { class: "ld-facts" },
            ["price", "category", "state"].map((k) => h("div", { class: "ld-fact" }, h("p", { class: "ld-fact-label" }, t(`facts.${k}`)), h("span", { class: "skeleton", style: { width: "70%", height: 16 } }))),
          ),
        ),
      );
    }

    // ------------------------------------------------------------ the full view
    function render() {
      const d = st.data;
      const l = d.listing;
      const grid = h("div", { class: "ld-grid" }, leftColumn(d), rightColumn(d, l));
      mount(el, grid);
    }

    // --- left: hero image + strip
    function leftColumn(d) {
      const images = d.images || [];
      const hero = h("div", { class: "ld-hero" });
      const strip = h("div", { class: "ld-strip", role: "listbox", "aria-label": t("images.title") });
      const count = images.length;
      const placed = imageCount(d, count);
      const head = h(
        "div",
        { class: "ld-strip-head" },
        h("b", null, t("images.title")),
        h("span", { class: "muted" }, d.source ? t("images.auto") : t("images.order")),
        h("div", { class: "spacer" }),
        tagBadge({ text: t("images.count", { n: count, max: placed.max }), tone: placed.tone, icon: placed.icon }, "mono ld-img-count"),
      );
      st.heroEl = hero;
      st.stripEl = strip;
      renderHero();
      renderStrip();
      return h("div", { class: "ld-left" }, hero, head, strip);
    }

    function renderHero() {
      const d = st.data;
      const images = d.images || [];
      const hero = st.heroEl;
      if (!images.length) {
        mount(hero, h("div", { class: "ld-hero-empty" }, emptyState({ icon: "image", title: t("images.none"), message: t("images.none_msg"), compact: true })));
        return;
      }
      const img = images[st.image] || images[0];
      const srcset = [img.url ? `${img.url} 570w` : null, img.full && img.width ? `${img.full} ${img.width}w` : null].filter(Boolean).join(", ");
      // Each picture's own product and colour ("Kupa · Beyaz": the mockup it was made on),
      // none on the plain design (video). A picture with nothing known about it: the
      // listing's own product, for the main image only.
      // The shop's info images (Şablon İlan) say so instead: they show no product.
      const plain = img.flat || img.info;
      const main = st.image === 0 && !plain;
      const kind = plain ? "" : typeText(img.type || (main ? d.listing.type : ""), main ? d.listing.type_name : "");
      const colour = plain ? "" : img.color_key && t.has(`color.${img.color_key}`) ? t(`color.${img.color_key}`) : img.color;
      const chip = img.info ? t("images.info") : [kind, colour].filter(Boolean).join(" · ");
      const n = images.length;
      mount(
        hero,
        h("div", { class: "ld-hero-blur", style: { backgroundImage: img.url ? `url("${img.url.replace(/"/g, "%22")}")` : "none" }, "aria-hidden": "true" }),
        h("img", { class: "ld-hero-img", src: img.url || img.full, srcset: srcset || undefined, sizes: "(max-width: 1200px) 50vw, 640px", alt: img.alt || d.listing.title, decoding: "async" }),
        h("span", { class: "ld-chip ld-chip-tl" }, icon("image", { size: 13 }), st.image === 0 ? t("images.main") : t("images.nth", { n: st.image + 1 })),
        h("span", { class: "ld-chip ld-chip-tr num" }, `${st.image + 1} / ${n}`),
        chip ? h("span", { class: "ld-chip ld-chip-bl" }, chip) : null,
        n > 1 ? iconButton({ icon: "chevron-left", title: t("images.prev"), variant: "ghost", class: "ld-hero-nav ld-hero-prev", onClick: () => showImage((st.image - 1 + n) % n) }) : null,
        n > 1 ? iconButton({ icon: "chevron-right", title: t("images.next"), variant: "ghost", class: "ld-hero-nav ld-hero-next", onClick: () => showImage((st.image + 1) % n) }) : null,
      );
    }

    function renderStrip() {
      const images = st.data.images || [];
      const kids = images.map((img, i) =>
        h(
          "button",
          {
            type: "button",
            class: cx("ld-thumb", i === st.image && "is-active"),
            role: "option",
            "aria-selected": i === st.image ? "true" : "false",
            title: t("images.nth", { n: i + 1 }),
            onClick: () => showImage(i),
          },
          h("img", { src: img.thumb || img.url, alt: "", loading: "lazy", decoding: "async" }),
        ),
      );
      // Only the listing's own images (video t230); the empty slots stand in for none.
      if (!images.length) for (let i = 0; i < MIN_SLOTS; i += 1) kids.push(h("span", { class: "ld-thumb is-empty", "aria-hidden": "true" }));
      mount(st.stripEl, kids);
    }

    function showImage(i) {
      st.image = i;
      renderHero();
      renderStrip();
    }

    // --- right: source, title, tags, description, facts, actions
    function rightColumn(d, l) {
      return h(
        "div",
        { class: "ld-right" },
        sourceRow(d, l),
        printRow(d, l),
        titleCard(d),
        tagsCard(d),
        facts(d, l),
        actions(d, l),
      );
    }

    function sourceRow(d, l) {
      const s = d.source;
      let pic;
      let name;
      let sub;
      if (s) {
        const params = s.transparent ? { path: s.rel, w: 96, v: s.mtime, bg: "checker" } : { path: s.rel, w: 96, v: s.mtime };
        pic = thumb({ src: s.rel ? ctx.api.url("/api/files/thumb", params) : null, size: 32, radius: 8, icon: s.kind === "folder" ? "folder" : "image", fit: "contain" });
        // A transparent design reads as one on a checkerboard (video: the source tile).
        if (s.transparent) pic.classList.add("is-transparent");
        name = s.name;
        if (!s.exists) sub = t("source.missing");
        else if (s.kind === "folder") sub = t("source.folder", { n: s.files });
        else if (s.transparent) sub = t("source.transparent", { fmt: (s.format || "png").toUpperCase() });
        else sub = t("source.file", { fmt: (s.format || "").toUpperCase() });
      } else {
        const first = d.images[0];
        pic = thumb({ src: first ? first.thumb : null, size: 32, radius: 8 });
        name = `#${l.id}`;
        sub = t("source.unknown");
      }
      const score = d.audit.score;
      const tone = score >= 80 ? "success" : score >= 60 ? "warning" : "danger";
      const seoBtn = h(
        "button",
        { type: "button", class: cx("badge", "ld-seo", `tone-${tone}`), "aria-haspopup": "dialog", title: t("seo.open") },
        icon("target", { size: 12 }),
        h("span", null, t("seo.score", { n: score })),
      );
      seoBtn.addEventListener("click", () => openIssues(seoBtn));
      return h(
        "div",
        { class: "ld-source" },
        pic,
        h("div", { class: "ld-source-text" }, h("p", { class: "mono ld-source-name", title: name }, name), h("p", { class: "ld-source-sub" }, sub)),
        h("div", { class: "spacer" }),
        seoBtn,
      );
    }

    // The listing's print file (print-files/<id>): what an order of it is printed from.
    // A file dropped on the row or picked with "Dosya ekle" is kept on this computer only.
    function printRow(d, l) {
      const p = d.print_files;
      if (!p || (!p.needed && !p.files.length)) return null;
      const first = p.files[0];
      const sub = h("p", { class: "ld-source-sub" });
      let pic;
      let name;
      if (first) {
        pic = thumb({ src: first.thumb ? ctx.api.url("/api/files/thumb", { path: first.rel, w: 96, v: first.mtime }) : null, size: 32, radius: 8, icon: "file", fit: "contain" });
        name = first.name;
        const facts = [];
        if (first.width && first.height) facts.push(t("print.pixels", { w: first.width, h: first.height }));
        facts.push(bytes(first.size));
        if (p.files.length > 1) facts.push(t("print.more", { n: p.files.length - 1 }));
        sub.textContent = facts.join(" · ");
      } else {
        pic = h("span", { class: "ld-print-icon" }, icon("alert", { size: 16 }));
        name = t("print.missing");
        sub.textContent = t("print.missing_sub");
      }
      const input = h("input", { type: "file", accept: PRINT_ACCEPT, multiple: true, hidden: true });
      const addBtn = button({ label: t("print.add"), icon: "plus", variant: first ? "ghost" : "secondary", size: "sm", onClick: () => input.click() });
      const folderBtn = iconButton({ icon: "folder-open", title: t("print.folder"), variant: "ghost", size: "sm", onClick: () => openFolder() });
      const row = h(
        "div",
        { class: cx("ld-source", "ld-print", !first && "is-missing"), title: first ? t("print.title", { folder: p.folder }) : "" },
        pic,
        h("div", { class: "ld-source-text" }, h("p", { class: "ld-source-name", title: name }, first ? h("span", { class: "mono" }, name) : name), sub),
        h("div", { class: "spacer" }),
        addBtn,
        folderBtn,
        input,
      );
      input.addEventListener("change", () => {
        const picked = [...input.files];
        input.value = "";
        addFiles(picked);
      });
      row.addEventListener("dragover", (e) => {
        if (![...(e.dataTransfer?.types || [])].includes("Files")) return;
        e.preventDefault();
        row.classList.add("is-drop");
      });
      row.addEventListener("dragleave", () => row.classList.remove("is-drop"));
      row.addEventListener("drop", (e) => {
        e.preventDefault();
        row.classList.remove("is-drop");
        addFiles([...(e.dataTransfer?.files || [])]);
      });

      async function addFiles(picked) {
        if (!picked.length) return;
        addBtn.disabled = true;
        let latest = null;
        for (const file of picked) {
          if (file.size > p.max_bytes) {
            ctx.toast({ tone: "danger", title: t("print.too_large", { name: file.name, mb: Math.floor(p.max_bytes / 1048576) }) });
            continue;
          }
          try {
            const res = await ctx.api.upload(`/api/listings/${l.id}/print-files`, file, {
              query: { name: file.name },
              onProgress: ({ fraction }) => {
                sub.textContent = t("print.uploading", { name: file.name, pct: Math.round(fraction * 100) });
              },
            });
            latest = res.print_files;
            ctx.toast({ tone: "success", title: res.duplicate ? t("print.duplicate", { name: res.file }) : t("print.added", { name: res.file }) });
          } catch (err) {
            ctx.toast({ tone: "danger", title: t("print.failed", { name: file.name }), message: ctx.api.errorText(err, t) });
          }
        }
        if (latest) d.print_files = { ...latest, needed: p.needed };
        const next = printRow(d, l);
        if (next) row.replaceWith(next);
      }

      async function openFolder() {
        try {
          await ctx.api.post(`/api/listings/${l.id}/print-files/open`);
        } catch (err) {
          ctx.toast({ tone: "danger", title: ctx.api.errorText(err, t) });
        }
      }
      return row;
    }

    function openIssues(anchor) {
      const issues = st.data.audit.issues || [];
      const list = issues.length
        ? h(
            "ul",
            { class: "ld-issues" },
            issues.map((i) =>
              h(
                "li",
                { class: cx("ld-issue", `tone-${i.severity === "error" ? "danger" : i.severity === "warn" ? "warning" : "info"}`) },
                icon(i.severity === "error" ? "x-circle" : i.severity === "warn" ? "alert" : "info", { size: 14 }),
                h("span", null, t.has(`issue.${i.code}`) ? t(`issue.${i.code}`) : i.message),
              ),
            ),
          )
        : h("p", { class: "ld-issues-none" }, icon("check-circle", { size: 14 }), t("seo.clean"));
      const pop = popover(anchor, [h("p", { class: "ld-issues-head" }, t("seo.title", { n: st.data.audit.score })), list, h("p", { class: "ld-issues-foot muted" }, t("seo.note"))], {
        placement: "bottom-end",
        width: 340,
        class: "ld-issues-pop",
        role: "dialog",
      });
      if (pop) pop.el.setAttribute("aria-label", t("seo.title", { n: st.data.audit.score }));
    }

    // Title
    function titleCard(d) {
      const max = d.limits.title;
      const counter = tagBadge({ text: "", tone: "success" }, "mono ld-counter");
      const bar = h("span", { class: "ld-bar" });
      const err = h("p", { class: "ld-field-error", role: "alert", hidden: true });
      const ta = textArea({
        value: st.draft.title,
        rows: 2,
        autoGrow: true,
        class: "ld-title-input",
        ariaLabel: t("title_card.label"),
        onInput: (v) => {
          st.draft.title = v.replace(/\n/g, " ");
          if (v.includes("\n")) ta.value = st.draft.title;
          update();
          syncDirty();
        },
      });
      ta.addEventListener("keydown", (e) => {
        if (e.key === "Enter") e.preventDefault();
      });
      function update() {
        const title = squash(st.draft.title);
        const n = title.length;
        const problems = titleProblems(title, max);
        const over = n > max;
        mount(counter, over || problems.length ? icon("alert", { size: 12 }) : n ? icon("check", { size: 12, strokeWidth: 2.4 }) : null, h("span", null, `${n}/${max}`));
        counter.className = cx("badge", "mono", "ld-counter", `tone-${!n || problems.length ? "danger" : "success"}`);
        bar.style.width = `${Math.min(100, (n / max) * 100)}%`;
        bar.className = cx("ld-bar", (over || problems.length) && "is-over");
        err.hidden = !problems.length;
        err.textContent = problems.join(" ");
        st.titleOk = n > 0 && !problems.length;
      }
      update();
      // The description is edited from here: the video's column has no description block
      // and its actions row only the note, "Etsy'de aç" and "Yayınla".
      st.descBtn = button({ label: t("desc.label"), icon: "file", variant: "ghost", size: "sm", class: "ld-desc-btn", title: t("desc.label"), ariaLabel: t("desc.label"), onClick: () => openDescription() });
      return card({
        class: "ld-card ld-title-card",
        title: t("title_card.label"),
        icon: "loader",
        iconTone: "neutral",
        actions: [st.descBtn, counter],
        body: [ta, err, h("div", { class: "ld-title-bar" }, bar)],
      });
    }

    function titleProblems(title, max) {
      const out = [];
      if (!title) return [t("title_card.empty")];
      if (title.length > max) out.push(t("title_card.too_long", { n: title.length, max }));
      const bad = [...new Set(title.match(TITLE_BAD) || [])];
      if (bad.length) out.push(t("title_card.bad_chars", { chars: bad.join(" ") }));
      const twice = TITLE_ONCE.filter((ch) => title.split(ch).length > 2);
      if (twice.length) out.push(t("title_card.once", { chars: twice.join(" ") }));
      return out;
    }

    // Tags
    function tagsCard(d) {
      const max = d.limits.tags;
      const maxLen = d.limits.tag_len;
      const counter = tagBadge({ text: "", tone: "accent" }, "mono ld-counter");
      const wrap = h("div", { class: "ld-tags" });
      const err = h("p", { class: "ld-field-error", role: "alert", hidden: true });
      const input = h("input", {
        type: "text",
        class: "ld-tag-input",
        placeholder: t("tags.add"),
        maxlength: String(maxLen + 10),
        "aria-label": t("tags.add"),
        autocomplete: "off",
        spellcheck: "false",
      });
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === ",") {
          e.preventDefault();
          addTag(input.value);
        } else if (e.key === "Backspace" && !input.value && st.draft.tags.length) {
          removeTag(st.draft.tags[st.draft.tags.length - 1]);
        }
      });
      input.addEventListener("input", () => {
        const v = input.value;
        if (v.includes(",")) {
          v.split(",").slice(0, -1).forEach((part) => addTag(part, true));
          input.value = v.split(",").pop();
        }
        const len = squash(input.value).length;
        input.classList.toggle("is-over", len > maxLen);
        input.dataset.len = len ? String(len) : "";
        if (st.tagError) setError("");
      });
      input.addEventListener("blur", () => {
        if (squash(input.value)) addTag(input.value, true);
      });

      function setError(msg) {
        st.tagError = msg;
        err.textContent = msg;
        err.hidden = !msg;
      }

      function addTag(raw, quiet) {
        const tag = squash(raw);
        if (!tag) {
          input.value = "";
          return;
        }
        const problem = tagProblem(tag, maxLen, max);
        if (problem) {
          setError(problem);
          if (!quiet) input.focus();
          return;
        }
        st.draft.tags.push(tag);
        st.lastTag = tag;
        input.value = "";
        input.dataset.len = "";
        setError("");
        renderTags();
        syncDirty();
        input.focus();
      }

      function removeTag(tag) {
        st.draft.tags = st.draft.tags.filter((x) => x !== tag);
        if (st.lastTag === tag) st.lastTag = null;
        setError("");
        renderTags();
        syncDirty();
        input.focus();
      }

      function tagProblem(tag, len, limit) {
        if (st.draft.tags.length >= limit) return t("tags.full", { max: limit });
        if (tag.length > len) return t("tags.too_long", { n: tag.length, max: len });
        const bad = [...new Set(tag.match(TAG_BAD) || [])];
        if (bad.length) return t("tags.bad_chars", { chars: bad.join(" ") });
        if (st.draft.tags.some((x) => x.toLocaleLowerCase() === tag.toLocaleLowerCase())) return t("tags.duplicate", { tag });
        return "";
      }

      function renderTags() {
        const tags = st.draft.tags;
        const n = tags.length;
        mount(counter, n >= max ? icon("check", { size: 12, strokeWidth: 2.4 }) : null, h("span", null, `${n}/${max}`));
        counter.className = cx("badge", "mono", "ld-counter", `tone-${n > max ? "danger" : n >= max ? "success" : n ? "accent" : "neutral"}`);
        const chipsEls = tags.map((tag) => {
          const bad = tag.length > maxLen || (tag.match(TAG_BAD) || []).length > 0;
          return tagChip({
            text: tag,
            count: tag.length,
            removable: true,
            onRemove: removeTag,
            selected: tag === st.lastTag,
            tone: bad ? "danger" : "neutral",
            title: bad ? t("tags.invalid") : undefined,
          });
        });
        const free = Math.max(0, max - n);
        const slots = [];
        if (free > 0) slots.push(h("label", { class: "tagchip is-slot ld-tag-slot" }, icon("plus", { size: 13 }), input));
        for (let i = 1; i < free; i += 1) slots.push(h("span", { class: "tagchip is-slot", style: { width: SLOT_WIDTHS[(n + i) % SLOT_WIDTHS.length] }, "aria-hidden": "true" }));
        mount(wrap, chipsEls, slots);
      }
      renderTags();
      return card({
        class: "ld-card ld-tags-card",
        title: [t("tags.label"), " ", h("span", { class: "ld-hint" }, t("tags.hint", { max: maxLen }))],
        icon: "tag",
        iconTone: "neutral",
        actions: counter,
        body: [wrap, err],
      });
    }

    // Description: not on the page (the video's right column has none), a button in the
    // title card's head; the modal edits a copy, Tamam puts it in the draft (Kaydet saves).
    function openDescription() {
      let copy = st.draft.description;
      const count = h("span", { class: "num" }, t("desc.chars", { n: copy.trim().length }));
      const ta = textArea({
        value: copy,
        rows: 12,
        class: "ld-desc-input",
        ariaLabel: t("desc.label"),
        onInput: (v) => {
          copy = v;
          count.textContent = t("desc.chars", { n: v.trim().length });
        },
      });
      ta.setAttribute("autofocus", "");
      ctx.modal({
        title: t("desc.label"),
        subtitle: count,
        width: 640,
        class: "ld-desc-modal",
        body: ta,
        actions: [
          { label: t("common.cancel"), variant: "secondary" },
          {
            label: t("common.ok"),
            variant: "primary",
            onClick: ({ close }) => {
              st.draft.description = copy;
              close();
              syncDirty();
            },
          },
        ],
      });
    }

    function catName(name) {
      const key = `cat.${catSlug(name)}`;
      return t.has(key) ? t(key) : null;
    }

    // "Giyim › Tişörtler": Etsy's root and leaf, in the UI's language (video t230). One
    // that is not in the list keeps Etsy's own last two names; the full path is the tooltip.
    function categoryText(d) {
      const parts = String(d.category.path || "").split(" > ").map((x) => x.trim()).filter(Boolean);
      if (parts.length) {
        const root = catName(parts[0]);
        const leaf = catName(parts[parts.length - 1]);
        if (root && leaf) return parts.length > 1 ? `${root} › ${leaf}` : root;
      }
      return d.category.short || t("facts.no_category");
    }

    // "✓ 10/10 görsel" when etsyotomasyon placed every image of the draft (upload history);
    // "7/8" in amber when one did not go up; else the count against Etsy's 20.
    function imageCount(d, count) {
      const src = d.source || {};
      const total = Number(src.images_total) || 0;
      const uploaded = src.images_uploaded === undefined || src.images_uploaded === null ? count : Number(src.images_uploaded);
      if (count && (src.status === "partial" || (total && uploaded < total))) return { max: Math.max(total, count), tone: "warning", icon: "alert" };
      if (count && src.status === "ok") return { max: count, tone: "success", icon: "check" };
      return {
        max: d.limits.images,
        tone: count === 0 ? "danger" : count >= MIN_SLOTS ? "success" : "accent",
        icon: count >= MIN_SLOTS ? "check" : count === 0 ? "alert" : null,
      };
    }

    function facts(d, l) {
      const state = l.state || "draft";
      return h(
        "div",
        { class: "ld-facts" },
        h(
          "div",
          { class: "ld-fact" },
          h("p", { class: "ld-fact-label" }, t("facts.price")),
          h("p", { class: "ld-price num" }, l.price === null || l.price === undefined ? "–" : money(l.price, l.currency || "USD"), l.currency ? h("span", { class: "ld-cur" }, l.currency) : null),
        ),
        h(
          "div",
          { class: "ld-fact", title: d.category.path || "" },
          h("p", { class: "ld-fact-label" }, t("facts.category")),
          h("p", { class: "ld-cat" }, categoryText(d)),
        ),
        h(
          "div",
          { class: "ld-fact" },
          h("p", { class: "ld-fact-label" }, t("facts.state")),
          h(
            "div",
            { class: "ld-state" },
            badge({ text: t(`state.${state}`), tone: STATE_TONE[state] || "neutral", dot: true }),
            h("span", { class: "muted", title: t(`state_note.${state}`) }, t(`state_note.${state}`)),
          ),
        ),
      );
    }

    // Actions (bottom right)
    function actions(d, l) {
      const state = l.state || "draft";
      st.saveBtn = button({ label: t("actions.save"), icon: "save", variant: "success", size: "lg", onClick: () => save() });
      st.discardBtn = button({ label: t("actions.discard"), variant: "ghost", size: "lg", onClick: () => discard() });
      const open = h(
        "a",
        { class: "btn btn-secondary btn-lg ld-open", href: d.etsy_url, target: "_blank", rel: "noopener noreferrer", "data-external": "", title: t("actions.open"), "aria-label": t("actions.open") },
        icon("link", { size: 18 }),
        h("span", { class: "btn-label" }, t("actions.open")),
      );
      st.publishBtn = state === "draft" ? button({ label: t("actions.publish"), icon: "upload", variant: "primary", size: "lg", onClick: () => publish() }) : null;
      const note = state === "active" ? t("actions.note_active") : t("actions.note");
      st.noteEl = h("p", { class: "ld-note", title: note }, icon("lock", { size: 13 }), h("span", null, note));
      // The note takes the free space; the buttons stay together on the right.
      const row = h("div", { class: "ld-actions" }, st.noteEl, st.discardBtn, st.saveBtn, open, st.publishBtn);
      st.actionsEl = row;
      queueMicrotask(syncDirty);
      return row;
    }

    function syncDirty() {
      const dirty = isDirty();
      ctx.setDirty(dirty);
      if (st.actionsEl) st.actionsEl.classList.toggle("is-dirty", dirty);
      if (st.saveBtn) {
        st.saveBtn.hidden = !dirty;
        st.discardBtn.hidden = !dirty;
        st.saveBtn.setDisabled(st.busy || !st.titleOk || st.draft.tags.length > st.data.limits.tags);
      }
      if (st.publishBtn) st.publishBtn.setDisabled(st.busy || (dirty && !st.titleOk));
      if (st.descBtn) {
        // A dot on the button while the description differs from the saved one.
        const changed = st.draft.description.trim() !== String(st.data.listing.description || "").trim();
        const name = changed ? `${t("desc.label")} · ${t("desc.unsaved")}` : t("desc.label");
        st.descBtn.classList.toggle("has-change", changed);
        st.descBtn.title = name;
        st.descBtn.setAttribute("aria-label", name);
      }
    }

    function discard() {
      st.draft = { title: st.data.listing.title, tags: [...st.data.listing.tags], description: st.data.listing.description };
      st.lastTag = null;
      render();
    }

    function changes() {
      const l = st.data.listing;
      const body = {};
      if (squash(st.draft.title) !== squash(l.title)) body.title = squash(st.draft.title);
      if (JSON.stringify(st.draft.tags) !== JSON.stringify(l.tags)) body.tags = st.draft.tags;
      if (st.draft.description.trim() !== String(l.description || "").trim()) body.description = st.draft.description.trim();
      return body;
    }

    async function save({ quiet = false, confirmed = false } = {}) {
      const body = changes();
      if (!Object.keys(body).length) return true;
      if (st.data.listing.state === "active" && !confirmed) {
        const ok = await ctx.confirm({ title: t("save.live_title"), message: t("save.live_msg"), confirmLabel: t("save.live_confirm"), icon: "save" });
        if (!ok) return false;
      }
      body.confirm = st.data.listing.state === "active" ? true : undefined;
      st.busy = true;
      st.saveBtn.setLoading(true);
      syncDirty();
      try {
        const d = await ctx.api.patch(`/api/listings/${id}`, body);
        st.busy = false;
        st.lastTag = null;
        setData(d);
        if (!quiet) ctx.toast({ tone: "success", title: t("save.done") });
        return true;
      } catch (err) {
        st.busy = false;
        if (st.saveBtn) st.saveBtn.setLoading(false);
        syncDirty();
        ctx.toast({ tone: "danger", title: t("save.failed"), message: ctx.api.errorText(err, t) });
        return false;
      }
    }

    async function publish() {
      const dirty = isDirty();
      const ok = await ctx.confirm({
        title: t("publish.title"),
        message: [t("publish.msg"), dirty ? ` ${t("publish.msg_save")}` : ""].join(""),
        confirmLabel: t("publish.confirm"),
        icon: "upload",
      });
      if (!ok) return;
      if (dirty && !(await save({ quiet: true, confirmed: true }))) return;
      st.busy = true;
      st.publishBtn.setLoading(true);
      try {
        const d = await ctx.api.post(`/api/listings/${id}/publish`, { confirm: true });
        st.busy = false;
        setData(d);
        ctx.toast({ tone: "success", title: t("publish.done"), message: t("publish.done_msg") });
      } catch (err) {
        st.busy = false;
        if (st.publishBtn) st.publishBtn.setLoading(false);
        syncDirty();
        ctx.toast({ tone: "danger", title: t("publish.failed"), message: ctx.api.errorText(err, t), timeout: 12000 });
      }
    }
  },
};

