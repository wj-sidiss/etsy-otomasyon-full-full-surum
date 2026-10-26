// Server-sent events client: one stream on /api/events per browser, shared by every
// etsyotomasyon tab.
//
//   const off = events.on("job", (job) => ...);   // -> unsubscribe()
//
// Why shared: a browser opens at most 6 connections to one host (HTTP/1.1), for all
// its tabs together, and an EventSource keeps one of them for good. With a stream per
// tab, six open tabs took every connection and all of them hung on spinners. So the
// tabs elect a leader - the tab holding the Web Lock "etsyotomasyon-events" - and only the
// leader opens the stream; it relays every event to the other tabs over the
// BroadcastChannel "etsyotomasyon-events". When the leader goes (tab closed, reloaded), the
// next tab in line gets the lock and opens the stream. A browser may freeze a tab that
// has been hidden for a while, and a frozen leader relays nothing, so a visible tab
// takes the lead from a leader that has been hidden for STEAL_AFTER (or that does not
// answer at all). The old leader keeps its stream until the new one is connected;
// every event carries the server's id, which drops the doubles of that overlap. A
// browser without Web Locks or BroadcastChannel keeps one stream per tab, as before.
//
// Server topics: hello, status, shop, job, job-event, notification (and any other topic
// a page subscribes to). Client-side topics:
//   "connection" {connected: bool}  - stream opened / lost (the leader's, for the others)
//   "reconnect"  {}                 - stream is back after a gap (a drop, or a new leader
//                                     that may have missed events): re-fetch what you show
//   "oauth-done" {service}          - a consent tab reached /oauth-done (app.js relays it)
// On every reconnect /api/status is fetched again and delivered on "status".

import { api } from "./api.js";

const BASE_TOPICS = ["hello", "status", "shop", "job", "job-event", "notification"];
const LOCAL_TOPICS = new Set(["connection", "reconnect", "oauth-done"]);
const LOCK_NAME = "etsyotomasyon-events";
const CHANNEL_NAME = "etsyotomasyon-events";
const STEAL_AFTER = 60000; // a leader hidden this long hands over to a visible tab
const REPLY_WAIT = 2500; // a leader that does not answer "hi" in this time may be frozen
const HANDOVER_WAIT = 5000; // an old leader keeps its stream at most this long
const SEEN_KEEP = 500; // event ids remembered for dropping doubles

function canShare() {
  try {
    return typeof BroadcastChannel === "function" && !!navigator.locks && typeof navigator.locks.request === "function";
  } catch {
    return false;
  }
}

function tabId() {
  try {
    return crypto.randomUUID();
  } catch {
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  }
}

function parse(raw) {
  try {
    return raw ? JSON.parse(raw) : null;
  } catch {
    return raw;
  }
}

class EventClient {
  constructor() {
    this.handlers = new Map(); // topic -> Set<cb>
    this.topics = new Set(BASE_TOPICS);
    this.source = null;
    this.attempt = 0;
    this.timer = null;
    this.closed = true;
    this.connected = false;
    this.everConnected = false;
    this.instance = null;
    this.lifecycleBound = false;
    // Sharing one stream between the tabs.
    this.tab = tabId();
    this.role = "none"; // "leader" | "follower" | "solo" (a stream of its own) | "none"
    this.channel = null;
    this.release = null; // lets go of the lock while leading
    this.lease = 0; // the lock grant this tab leads under
    this.grants = 0;
    this.queued = null; // AbortController of this tab's waiting lock request
    this.leader = null; // what the leader said last: {tab, hiddenSince}
    this.hiddenSince = null;
    this.handover = null; // {source, timer}: the stream kept while a new leader connects
    this.replyTimer = null;
    this.stealTimer = null;
    this.seen = new Set();
    this.seenOrder = [];
  }

  /** Subscribe; returns an unsubscribe function. */
  on(topic, cb) {
    let set = this.handlers.get(topic);
    if (!set) {
      set = new Set();
      this.handlers.set(topic, set);
    }
    set.add(cb);
    if (this._addTopic(topic)) this._post({ k: "topic", topic });
    return () => {
      set.delete(cb);
    };
  }

  /** Subscribe for one delivery only. */
  once(topic, cb) {
    const off = this.on(topic, (data) => {
      off();
      cb(data);
    });
    return off;
  }

  /** Deliver locally (also used for client-side topics). */
  emit(topic, data) {
    const set = this.handlers.get(topic);
    if (!set) return;
    for (const cb of [...set]) {
      try {
        cb(data);
      } catch (err) {
        console.error(`[events] handler for "${topic}" failed`, err);
      }
    }
  }

  /** This tab holds the stream (for the others too, when shared). */
  get leading() {
    return this.role === "leader" || this.role === "solo";
  }

  connect() {
    if (!this.closed) return;
    this.closed = false;
    if (!this.lifecycleBound) {
      this.lifecycleBound = true;
      this.hiddenSince = document.hidden ? Date.now() : null;
      // Leaving the page (or entering the back/forward cache) lets go of the stream and
      // of the lead at once, so the next tab takes over without waiting.
      window.addEventListener("pagehide", () => this._leave());
      window.addEventListener("pageshow", (e) => {
        if (e.persisted && !this.closed && this.role === "none") this._start();
      });
      document.addEventListener("visibilitychange", () => this._onVisibility());
    }
    this._start();
  }

  /** Stop for good (e.g. after quitting the app). */
  close() {
    this.closed = true;
    this._leave();
  }

  // ---------------------------------------------------------------- joining, leading

  _start() {
    if (this.closed) return;
    if (!canShare()) {
      this.role = "solo";
      this._open();
      return;
    }
    try {
      this.channel = new BroadcastChannel(CHANNEL_NAME);
    } catch {
      this.channel = null;
      this.role = "solo";
      this._open();
      return;
    }
    this.channel.onmessage = (e) => this._onMessage(e && e.data);
    this.role = "follower";
    this.leader = null;
    // Whoever leads introduces itself (and learns the topics this tab listens to).
    this._post({ k: "hi", tab: this.tab, topics: [...this.topics] });
    clearTimeout(this.replyTimer);
    this.replyTimer = setTimeout(() => this._noReply(), REPLY_WAIT);
    this._queue();
  }

  _leave() {
    clearTimeout(this.timer);
    clearTimeout(this.replyTimer);
    clearTimeout(this.stealTimer);
    this._endHandover();
    if (this.source) this.source.close();
    this.source = null;
    const wasLeader = this.role === "leader";
    this.role = "none";
    this.lease = 0;
    if (this.queued) {
      this.queued.abort();
      this.queued = null;
    }
    if (this.release) {
      const release = this.release;
      this.release = null;
      release();
    }
    if (this.channel) {
      if (wasLeader) this._post({ k: "bye", tab: this.tab });
      this.channel.close();
      this.channel = null;
    }
    this._setConnected(false);
  }

  /** Wait in line for the lead. */
  _queue() {
    if (this.closed || this.role !== "follower" || this.queued) return;
    const ctl = new AbortController();
    this.queued = ctl;
    this._request({ signal: ctl.signal }, ctl);
  }

  /** Take the lead from a leader that is hidden for long (maybe frozen) or silent. */
  _steal() {
    if (this.closed || this.role !== "follower") return;
    if (this.queued) {
      this.queued.abort();
      this.queued = null;
    }
    this._request({ steal: true }, null);
  }

  _request(options, ctl) {
    const grant = ++this.grants;
    let held = false;
    let request;
    try {
      request = navigator.locks.request(LOCK_NAME, options, () => {
        if (ctl && this.queued === ctl) this.queued = null;
        // Not wanted any more (left, or a newer request replaced this one): let go at once.
        if (this.closed || this.role !== "follower" || grant !== this.grants) return undefined;
        held = true;
        return new Promise((resolve) => {
          this.release = resolve;
          this.lease = grant;
          this._lead();
        });
      });
    } catch (err) {
      // Locks refused (an odd context): a stream of this tab's own still works.
      console.warn("[events] no shared stream", err);
      if (ctl && this.queued === ctl) this.queued = null;
      this._leave();
      if (this.closed) return;
      this.role = "solo";
      this._open();
      return;
    }
    request
      .catch(() => {}) // aborted while waiting, or taken by another tab
      .then(() => {
        if (ctl && this.queued === ctl) this.queued = null;
        if (held && this.lease === grant) this._lostLead();
        // Back in line - unless a newer request (a steal under way) replaced this one:
        // queueing again would make that steal let go of the lock at once.
        if (grant === this.grants && !this.closed && this.role === "follower") this._queue();
      });
  }

  _lead() {
    this.role = "leader";
    clearTimeout(this.replyTimer);
    clearTimeout(this.stealTimer);
    this.leader = { tab: this.tab, hiddenSince: this.hiddenSince };
    this._announce();
    this.attempt = 0;
    this._open();
  }

  /** Another tab took the lock: keep relaying until its stream is open, then stop. */
  _lostLead() {
    this.lease = 0;
    this.release = null;
    if (this.role !== "leader") return;
    this.role = "follower";
    this.leader = null;
    clearTimeout(this.timer);
    const source = this.source;
    this.source = null;
    this._endHandover();
    if (source) this.handover = { source, timer: setTimeout(() => this._endHandover(), HANDOVER_WAIT) };
  }

  _endHandover() {
    const handover = this.handover;
    if (!handover) return;
    this.handover = null;
    clearTimeout(handover.timer);
    handover.source.close();
  }

  _announce() {
    this._post({ k: "leader", tab: this.tab, hiddenSince: this.hiddenSince, connected: this.connected });
  }

  _noReply() {
    if (this.closed || this.role !== "follower" || this.leader) return;
    // Nobody answered: the lock is held by a tab that does not run (frozen). Take over
    // now if this tab is on screen, else as soon as it is.
    this.leader = { tab: null, hiddenSince: Date.now() - STEAL_AFTER };
    this._maybeSteal();
  }

  _maybeSteal() {
    clearTimeout(this.stealTimer);
    if (this.closed || this.role !== "follower" || document.hidden || !this.leader) return;
    const since = this.leader.hiddenSince;
    if (typeof since !== "number") return; // the leader is on screen: all is well
    const wait = Math.max(0, since + STEAL_AFTER - Date.now()) + Math.round(Math.random() * 300);
    this.stealTimer = setTimeout(() => {
      const leader = this.leader;
      if (this.closed || this.role !== "follower" || document.hidden || !leader || typeof leader.hiddenSince !== "number") return;
      if (Date.now() - leader.hiddenSince < STEAL_AFTER) {
        this._maybeSteal();
        return;
      }
      this._steal();
    }, wait);
  }

  _onVisibility() {
    if (document.hidden) {
      if (this.hiddenSince === null) this.hiddenSince = Date.now();
    } else {
      this.hiddenSince = null;
    }
    if (this.closed) return;
    if (this.role === "leader") {
      this.leader = { tab: this.tab, hiddenSince: this.hiddenSince };
      this._announce();
    } else if (this.role === "follower") {
      this._maybeSteal();
    }
  }

  // ---------------------------------------------------------------- the other tabs

  _post(msg) {
    if (!this.channel) return;
    try {
      this.channel.postMessage(msg);
    } catch (err) {
      console.warn("[events] could not tell the other tabs", err);
    }
  }

  _onMessage(msg) {
    if (!msg || typeof msg !== "object" || this.closed || !this.channel) return;
    switch (msg.k) {
      case "hi":
        for (const topic of Array.isArray(msg.topics) ? msg.topics : []) this._addTopic(topic);
        if (this.role === "leader") this._announce();
        break;
      case "topic":
        this._addTopic(msg.topic);
        break;
      case "leader":
        if (msg.tab === this.tab || this.role !== "follower") break;
        clearTimeout(this.replyTimer);
        this.leader = { tab: msg.tab, hiddenSince: typeof msg.hiddenSince === "number" ? msg.hiddenSince : null };
        if (typeof msg.connected === "boolean") this._setConnected(msg.connected);
        this._maybeSteal();
        break;
      case "bye":
        if (this.role === "follower" && this.leader && this.leader.tab === msg.tab) this.leader = null;
        break;
      case "conn":
        if (msg.tab === this.tab || this.role !== "follower") break;
        if (msg.connected && this.handover) this._endHandover(); // the new leader is on
        this._setConnected(!!msg.connected);
        break;
      case "reconnect":
        if (this.role === "follower") this.emit("reconnect", {});
        break;
      case "ev":
        if (typeof msg.topic !== "string" || !this._fresh(msg.id)) break;
        this._deliver(msg.topic, msg.data);
        break;
      case "no_session":
        // The leader found the session gone (the app restarted): find out for this tab.
        if (this.role === "follower") api.get("/api/session").catch(() => {});
        break;
      default:
        break;
    }
  }

  // ---------------------------------------------------------------- the stream

  /** Add a topic to listen to; true when it is new. */
  _addTopic(topic) {
    if (typeof topic !== "string" || !topic || this.topics.has(topic) || LOCAL_TOPICS.has(topic)) return false;
    this.topics.add(topic);
    if (this.source) this._listen(this.source, topic);
    if (this.handover) this._listen(this.handover.source, topic);
    return true;
  }

  _listen(source, topic) {
    source.addEventListener(topic, (e) => this._fromServer(source, topic, e));
  }

  _fromServer(source, topic, e) {
    const handingOver = !!this.handover && this.handover.source === source;
    if (source !== this.source && !handingOver) return; // a stream this tab has let go
    const id = e.lastEventId || "";
    if (!this._fresh(id)) return;
    const data = parse(e.data);
    if (this.role === "leader" || handingOver) this._post({ k: "ev", topic, data, id });
    this._deliver(topic, data);
  }

  /** Deliver here and, when leading, to the other tabs (no id: never a double). */
  _share(topic, data) {
    if (this.role === "leader") this._post({ k: "ev", topic, data, id: "" });
    this._deliver(topic, data);
  }

  _deliver(topic, data) {
    if (topic === "hello" && data && data.instance) this.instance = data.instance;
    this.emit(topic, data);
  }

  /** False for an event id already delivered (the overlap of two leaders' streams). */
  _fresh(id) {
    if (!id) return true;
    const key = String(id);
    if (this.seen.has(key)) return false;
    this.seen.add(key);
    this.seenOrder.push(key);
    if (this.seenOrder.length > SEEN_KEEP) this.seen.delete(this.seenOrder.shift());
    return true;
  }

  _open() {
    if (this.closed || !this.leading || this.source) return;
    let source;
    try {
      source = new EventSource("/api/events");
    } catch (err) {
      console.warn("[events] EventSource failed", err);
      this._scheduleReconnect();
      return;
    }
    this.source = source;
    for (const topic of this.topics) this._listen(source, topic);
    source.onopen = () => {
      if (source !== this.source) return;
      // A gap before this stream (a drop, or the lead passing from a closed tab): the
      // pages re-fetch what they show.
      const wasReconnect = this.everConnected;
      this.attempt = 0;
      this._setConnected(true);
      if (wasReconnect) {
        if (this.role === "leader") this._post({ k: "reconnect" });
        this.emit("reconnect", {});
      }
    };
    source.onerror = () => {
      source.close();
      if (this.handover && this.handover.source === source) {
        this._endHandover();
        return;
      }
      if (source !== this.source) return;
      // Take reconnecting into our own hands (backoff + session check).
      this.source = null;
      this._setConnected(false);
      this._scheduleReconnect();
    };
  }

  _setConnected(value) {
    if (this.role === "leader") this._post({ k: "conn", tab: this.tab, connected: value });
    if (value) this.everConnected = true;
    if (this.connected === value) return;
    this.connected = value;
    this.emit("connection", { connected: value });
  }

  _scheduleReconnect() {
    if (this.closed || !this.leading) return;
    clearTimeout(this.timer);
    const delay = Math.min(15000, 1000 * 2 ** this.attempt) + Math.round(Math.random() * 400);
    this.attempt = Math.min(this.attempt + 1, 6);
    this.timer = setTimeout(() => this._probe(), delay);
  }

  async _probe() {
    if (this.closed || !this.leading) return;
    // A status fetch tells us whether the server is back and our session still valid
    // (401 no_session is handled globally by api.onNoSession).
    try {
      const status = await api.get("/api/status");
      if (this.closed || !this.leading) return;
      this._share("status", status);
      this._open();
    } catch (err) {
      if (err && err.code === "no_session") {
        this._post({ k: "no_session" });
        this.close();
        return;
      }
      this._scheduleReconnect();
    }
  }
}

export const events = new EventClient();
export default events;
