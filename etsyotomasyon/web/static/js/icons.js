// Inline SVG icons: 24x24 line drawings, stroke 1.8, currentColor. The ones the video's
// Icon.tsx draws (home, upload, list, search, truck, chart, image, file, link, bell,
// arrow-right, zap, clock, box, target, tag, lock, info, alert, refresh, user, mail, coins,
// grid, sparkle) use its path data. "loader" is the video's "sparkle" (its 8-ray burst,
// next to titles and suggestions); "settings" keeps its cog (the video never shows it).
// Usage: icon("upload") / icon("check", { size: 14 }) -> <svg> Node.

const SVG_NS = "http://www.w3.org/2000/svg";

// The video's "sparkle": an 8-ray burst.
const SPARKLE = '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M18 6l-2.5 2.5M8.5 15.5 6 18"/>';

// Filled dots use class "f" (fill currentColor, no stroke).
const PATHS = {
  home: '<path d="M3 11.5 12 4l9 7.5M5.5 9.5V20h13V9.5"/>',
  upload: '<path d="M12 15V4M7.5 8.5 12 4l4.5 4.5"/><path d="M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
  download: '<path d="M12 3.5V15"/><path d="m7 10 5 5 5-5"/><path d="M4 15v3.5A2.5 2.5 0 0 0 6.5 21h11a2.5 2.5 0 0 0 2.5-2.5V15"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  truck: '<path d="M2.5 6.5h11v10h-11zM13.5 10h4l3 3.5v3h-7"/><circle cx="6.5" cy="17.5" r="1.8"/><circle cx="17" cy="17.5" r="1.8"/>',
  chart: '<path d="M4 20V10M10 20V4M16 20v-7M21 20H3"/>',
  image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="10" r="1.8"/><path d="m4 18 5-5 4 4 3-3 4 4"/>',
  file: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  bell: '<path d="M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 2h-15l1.5-2Z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
  check: '<path d="M20 6.5 9.5 17 4.5 12"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  more: '<circle class="f" cx="5.5" cy="12" r="1.5"/><circle class="f" cx="12" cy="12" r="1.5"/><circle class="f" cx="18.5" cy="12" r="1.5"/>',
  "more-vertical": '<circle class="f" cx="12" cy="5.5" r="1.5"/><circle class="f" cx="12" cy="12" r="1.5"/><circle class="f" cx="12" cy="18.5" r="1.5"/>',
  "chevron-left": '<path d="m15 18-6-6 6-6"/>',
  "chevron-right": '<path d="m9 18 6-6-6-6"/>',
  "chevron-down": '<path d="m6 9 6 6 6-6"/>',
  "chevron-up": '<path d="m18 15-6-6-6 6"/>',
  "chevrons-up-down": '<path d="m8 9 4-4 4 4"/><path d="m8 15 4 4 4-4"/>',
  "arrow-right": '<path d="M5 12h14M13 6l6 6-6 6"/>',
  "arrow-left": '<path d="M19.5 12h-15"/><path d="m10.5 6-6 6 6 6"/>',
  "arrow-up": '<path d="M12 19.5v-15"/><path d="m6 10.5 6-6 6 6"/>',
  "arrow-down": '<path d="M12 4.5v15"/><path d="m18 13.5-6 6-6-6"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.7-3.8L4 9"/><path d="M4 4v5h5M4 13a8 8 0 0 0 14.7 3.8L20 15"/><path d="M20 20v-5h-5"/>',
  filter: '<path d="M4 6.5h16M7 12h10M10 17.5h4"/>',
  sort: '<path d="M4 6.5h10M4 12h7M4 17.5h4"/><path d="M18 5v14"/><path d="m15 16 3 3 3-3"/>',
  settings: '<path d="M9.87 5.02 10.53 2.72h2.94l.66 2.3 1.3.53 2.1-1.15 2.07 2.07-1.15 2.1.53 1.3 2.3.66v2.94l-2.3.66-.53 1.3 1.15 2.1-2.07 2.07-2.1-1.15-1.3.53-.66 2.3h-2.94l-.66-2.3-1.3-.53-2.1 1.15-2.07-2.07 1.15-2.1-.53-1.3-2.3-.66v-2.94l2.3-.66.53-1.3-1.15-2.1 2.07-2.07 2.1 1.15z"/><circle cx="12" cy="12" r="3"/>',
  store: '<path d="M3.5 9.5 5 4.5h14l1.5 5"/><path d="M3.5 9.5a2.8 2.8 0 0 0 5.7 0 2.8 2.8 0 0 0 5.6 0 2.8 2.8 0 0 0 5.7 0"/><path d="M5 11.8v8.7h14v-8.7"/><path d="M10 20.5V16h4v4.5"/>',
  lock: '<rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5"/>',
  unlock: '<rect x="4.5" y="10.5" width="15" height="10" rx="2"/><path d="M8 10.5V7.5a4 4 0 0 1 7.7-1.5"/>',
  shield: '<path d="M12 20.8s7.5-3.4 7.5-9.3V5.8L12 3.2 4.5 5.8v5.7c0 5.9 7.5 9.3 7.5 9.3z"/>',
  "shield-check": '<path d="M12 20.8s7.5-3.4 7.5-9.3V5.8L12 3.2 4.5 5.8v5.7c0 5.9 7.5 9.3 7.5 9.3z"/><path d="m9 12 2.2 2.2L15.5 10"/>',
  tag: '<path d="M3.5 12.5V4.5h8l9 9-8 8-9-9Z"/><circle cx="8" cy="9" r="1.4"/>',
  sparkles: '<path d="M10 3.5l1.6 4.9 4.9 1.6-4.9 1.6L10 16.5l-1.6-4.9L3.5 10l4.9-1.6z"/><path d="M18 14.5l.8 2.2 2.2.8-2.2.8L18 20.5l-.8-2.2-2.2-.8 2.2-.8z"/>',
  sparkle: SPARKLE,
  loader: SPARKLE,
  zap: '<path d="M13 2.5 4.5 13.5H12l-1 8 8.5-11H12l1-8Z"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.6v.01"/>',
  alert: '<path d="M12 3.5 2.5 20h19L12 3.5Z"/><path d="M12 10v4.5M12 17.3v.01"/>',
  "alert-circle": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.8v4.7"/><path d="M12 16h.01" stroke-width="2.4"/>',
  "check-circle": '<circle cx="12" cy="12" r="8.5"/><path d="m8.3 12.2 2.5 2.5 5-5"/>',
  "x-circle": '<circle cx="12" cy="12" r="8.5"/><path d="m9 9 6 6M15 9l-6 6"/>',
  help: '<circle cx="12" cy="12" r="8.5"/><path d="M9.6 9.3a2.5 2.5 0 0 1 4.8.9c0 1.7-2.4 2.3-2.4 3.6"/><path d="M12 17h.01" stroke-width="2.4"/>',
  trash: '<path d="M4 7h16"/><path d="M9 7V5a1.5 1.5 0 0 1 1.5-1.5h3A1.5 1.5 0 0 1 15 5v2"/><path d="m6 7 .9 12.1A1.5 1.5 0 0 0 8.4 20.5h7.2a1.5 1.5 0 0 0 1.5-1.4L18 7"/><path d="M10 11v5.5M14 11v5.5"/>',
  edit: '<path d="M16.3 3.8a2.1 2.1 0 0 1 3 3L8 18.1l-4 1 1-4z"/><path d="m14.3 5.8 3 3"/>',
  external: '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
  copy: '<rect x="8.5" y="8.5" width="12" height="12" rx="2"/><path d="M5 15.5h-.5a1.5 1.5 0 0 1-1.5-1.5V5a1.5 1.5 0 0 1 1.5-1.5h9A1.5 1.5 0 0 1 15 5v.5"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
  "eye-off": '<path d="M3.5 3.5l17 17"/><path d="M10.5 5.6a9 9 0 0 1 1.5-.1c6 0 9.5 6.5 9.5 6.5a15.7 15.7 0 0 1-2.6 3.4"/><path d="M6.6 6.7C4 8.4 2.5 12 2.5 12S6 18.5 12 18.5a9 9 0 0 0 4.9-1.4"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
  box: '<path d="M12 3 3.5 7.5v9L12 21l8.5-4.5v-9L12 3Z"/><path d="M3.5 7.5 12 12l8.5-4.5M12 12v9"/>',
  // Two stacked coins (the Şablon card's price row).
  coins: '<ellipse cx="9" cy="7" rx="5.5" ry="2.5"/><path d="M3.5 7v4c0 1.4 2.5 2.5 5.5 2.5s5.5-1.1 5.5-2.5V7"/><path d="M9.5 16.3c.9 1 2.9 1.7 5.1 1.7 3 0 5.5-1.1 5.5-2.5v-4c0-1.2-1.7-2.2-4.1-2.4"/>',
  dollar: '<path d="M12 2.5v19"/><path d="M16.5 7c-.7-1.3-2.3-2.2-4.5-2.2-2.7 0-4.4 1.3-4.4 3.2 0 4.4 9.3 2.4 9.3 7.3 0 2-1.9 3.5-4.9 3.5-2.4 0-4.1-.9-5-2.4"/>',
  banknote: '<rect x="2.5" y="6" width="19" height="12" rx="2"/><circle cx="12" cy="12" r="2.6"/><path d="M6 9.5h.01M18 14.5h.01" stroke-width="2.4"/>',
  wallet: '<path d="M19.5 7.5V6a1.5 1.5 0 0 0-1.5-1.5H5A1.5 1.5 0 0 0 3.5 6v12A1.5 1.5 0 0 0 5 19.5h14a1.5 1.5 0 0 0 1.5-1.5v-9A1.5 1.5 0 0 0 19 7.5H5"/><path d="M16 13.5h.01" stroke-width="2.6"/>',
  calendar: '<rect x="3.5" y="5" width="17" height="15.5" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
  pin: '<path d="M9 3.5h6l-.8 5.2 3.3 3.3v2H6.5v-2l3.3-3.3z"/><path d="M12 14v6.5"/>',
  "map-pin": '<path d="M19 10c0 5.5-7 11-7 11s-7-5.5-7-11a7 7 0 0 1 14 0z"/><circle cx="12" cy="10" r="2.5"/>',
  logout: '<path d="M9.5 20.5H5.5a1 1 0 0 1-1-1v-15a1 1 0 0 1 1-1h4"/><path d="m15.5 16.5 4.5-4.5-4.5-4.5"/><path d="M20 12H9.5"/>',
  power: '<path d="M12 3v8.5"/><path d="M6.4 6.6a7.8 7.8 0 1 0 11.2 0"/>',
  folder: '<path d="M3.5 7a2 2 0 0 1 2-2h3.8l2.2 2.5h7.5a2 2 0 0 1 2 2v8.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
  "folder-open": '<path d="M4.5 19.5 7 11.5h14l-2.4 7a1.5 1.5 0 0 1-1.4 1H5.5a2 2 0 0 1-2-2V6.5a2 2 0 0 1 2-2h3.8L11.5 7H17a2 2 0 0 1 2 2v2.5"/>',
  grip: '<circle class="f" cx="9" cy="6" r="1.4"/><circle class="f" cx="15" cy="6" r="1.4"/><circle class="f" cx="9" cy="12" r="1.4"/><circle class="f" cx="15" cy="12" r="1.4"/><circle class="f" cx="9" cy="18" r="1.4"/><circle class="f" cx="15" cy="18" r="1.4"/>',
  zoom: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/><path d="M11 8v6M8 11h6"/>',
  "zoom-out": '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/><path d="M8 11h6"/>',
  globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5c2.3 2.4 3.5 5.2 3.5 8.5s-1.2 6.1-3.5 8.5c-2.3-2.4-3.5-5.2-3.5-8.5S9.7 5.9 12 3.5z"/>',
  receipt: '<path d="M5.5 3.5h13v17l-2.2-1.4-2.1 1.4-2.2-1.4-2.2 1.4-2.1-1.4-2.2 1.4z"/><path d="M9 8h6M9 11.5h6M9 15h3.5"/>',
  percent: '<path d="M18.5 5.5l-13 13"/><circle cx="7" cy="7" r="2.3"/><circle cx="17" cy="17" r="2.3"/>',
  layers: '<path d="M12 3 3 7.7l9 4.8 9-4.8z"/><path d="m3 12.2 9 4.8 9-4.8"/><path d="m3 16.5 9 4.8 9-4.8"/>',
  play: '<path d="M7.5 5.2v13.6a1 1 0 0 0 1.5.9l10.6-6.8a1 1 0 0 0 0-1.7L9 4.3a1 1 0 0 0-1.5.9z"/>',
  pause: '<rect x="6.5" y="5" width="3.5" height="14" rx="1"/><rect x="14" y="5" width="3.5" height="14" rx="1"/>',
  stop: '<rect x="5.5" y="5.5" width="13" height="13" rx="2"/>',
  target: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1"/>',
  user: '<circle cx="12" cy="8.5" r="3.8"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/>',
  users: '<circle cx="9" cy="8.5" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 5.2a3.5 3.5 0 0 1 0 6.6"/><path d="M18.5 14.3a6.5 6.5 0 0 1 3 5.7"/>',
  key: '<circle cx="7.5" cy="15.5" r="4"/><path d="m10.4 12.6 9.1-9.1"/><path d="m16 7 2.5 2.5"/><path d="m13.8 9.2 2 2"/>',
  menu: '<path d="M4 6.5h16M4 12h16M4 17.5h16"/>',
  grid: '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>',
  send: '<path d="M21 3 10.5 13.5"/><path d="M21 3l-6.5 18-4-7.5L3 9.5z"/>',
  mail: '<rect x="3" y="5.5" width="18" height="13" rx="2"/><path d="m3.5 7 8.5 6 8.5-6"/>',
  shirt: '<path d="M8.5 4 4 6.5l1.5 4 2-.8V20h9V9.7l2 .8 1.5-4L15.5 4a3.5 3.5 0 0 1-7 0z"/>',
  mug: '<path d="M4.5 6.5h11v9a4 4 0 0 1-4 4h-3a4 4 0 0 1-4-4z"/><path d="M15.5 9H17a2.5 2.5 0 0 1 0 5h-1.5"/>',
  frame: '<rect x="4.5" y="3.5" width="15" height="17" rx="1"/><rect x="8" y="7" width="8" height="10"/>',
  phone: '<rect x="6.5" y="2.5" width="11" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
  bag: '<path d="M5.5 8h13l-1 12a1 1 0 0 1-1 .9h-9a1 1 0 0 1-1-.9z"/><path d="M9 10.5V6.5a3 3 0 0 1 6 0v4"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  star: '<path d="m12 3.5 2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z"/>',
  "trending-up": '<path d="m3 17 6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
  "trending-down": '<path d="m3 7 6 6 4-4 8 8"/><path d="M15 17h6v-6"/>',
  move: '<path d="M12 3v18M3 12h18"/><path d="m9 5.5 3-2.5 3 2.5M9 18.5l3 2.5 3-2.5M5.5 9 3 12l2.5 3M18.5 9l2.5 3-2.5 3"/>',
  crop: '<path d="M6 2.5V16a2 2 0 0 0 2 2h13.5"/><path d="M2.5 6H16a2 2 0 0 1 2 2v13.5"/>',
  // The watermark (Filigran): a drop, like the mark pressed into paper.
  droplet: '<path d="M12 3.5c3.2 3.9 6 7.2 6 10.6a6 6 0 0 1-12 0c0-3.4 2.8-6.7 6-10.6Z"/><path d="M9.2 14.6a2.9 2.9 0 0 0 2.4 2.6"/>',
  maximize: '<path d="M8.5 3.5h-5v5M15.5 3.5h5v5M8.5 20.5h-5v-5M15.5 20.5h5v-5"/>',
  undo: '<path d="M9 14.5 4 9.5l5-5"/><path d="M4 9.5h10.5a5.5 5.5 0 0 1 0 11H11"/>',
  history: '<path d="M3.5 12a8.5 8.5 0 1 0 2.5-6"/><path d="M3.5 4v4.5H8"/><path d="M12 8v4l3 2"/>',
  save: '<path d="M5 3.5h11l3.5 3.5v12a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 19V5A1.5 1.5 0 0 1 5 3.5z"/><path d="M7.5 3.5v5h8v-5"/><path d="M7.5 20.5v-6h9v6"/>',
  language: '<path d="M3.5 5.5h9M8 3.5v2M10.5 5.5c-1 4-3.5 7-7 8.5"/><path d="M5.5 9.5c1.2 2 3 3.5 5 4.5"/><path d="m12.5 20.5 4-9 4 9M13.8 17.5h5.4"/>',
  hash: '<path d="M5 9h15M4 15h15M10 3.5 8 20.5M16 3.5l-2 17"/>',
  package: '<path d="M20.5 7.8 12 3.3 3.5 7.8v8.4l8.5 4.5 8.5-4.5z"/><path d="M3.8 8 12 12.4 20.2 8"/><path d="M12 12.4v8.2"/><path d="m7.8 5.6 8.4 4.6"/>',
  circle: '<circle cx="12" cy="12" r="8.5"/>',
  dot: '<circle class="f" cx="12" cy="12" r="4"/>',
};

/** Names of every available icon (for the kitchen sink / checks). */
export const ICON_NAMES = Object.keys(PATHS);

/**
 * icon(name, {size=16, class, strokeWidth=1.8, title}) -> SVGElement.
 * Unknown names render an empty square (and warn once) so a typo never breaks a page.
 */
export function icon(name, opts = {}) {
  const { size = 16, strokeWidth = 1.8, title } = opts;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", String(strokeWidth));
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("class", "icon icon-" + name + (opts.class ? " " + opts.class : ""));
  svg.setAttribute("focusable", "false");
  let markup = PATHS[name];
  if (markup === undefined) {
    warnOnce(name);
    markup = '<rect x="4" y="4" width="16" height="16" rx="3"/>';
  }
  svg.innerHTML = markup;
  if (title) {
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", title);
  } else {
    svg.setAttribute("aria-hidden", "true");
  }
  return svg;
}

const warned = new Set();
function warnOnce(name) {
  if (warned.has(name)) return;
  warned.add(name);
  console.warn("[icons] unknown icon:", name);
}

/**
 * The app mark (the video's Logo): a violet gradient tile (drawn by .logo-tile in
 * base.css) with an outlined card behind a white one, the front one checked.
 * logoMark({size=26}) -> <span class="logo-tile">; the SVG fills the tile.
 */
export function logoMark({ size = 26 } = {}) {
  const tile = document.createElement("span");
  tile.className = "logo-tile";
  tile.style.width = size + "px";
  tile.style.height = size + "px";
  tile.setAttribute("aria-hidden", "true");
  const svg = document.createElementNS(SVG_NS, "svg");
  // The Logo's 32-unit box without the 2-unit margin around its tile.
  svg.setAttribute("viewBox", "2 2 28 28");
  svg.setAttribute("focusable", "false");
  svg.innerHTML =
    '<rect x="8" y="9" width="12" height="15" rx="2.5" fill="none" stroke="#fff" stroke-opacity=".55" stroke-width="2"/>' +
    '<rect x="12" y="7" width="12" height="15" rx="2.5" fill="#fff"/>' +
    '<path d="M15.5 14.5l2.3 2.3 3.9-4.4" fill="none" stroke="#7b6cff" stroke-width="2.2" ' +
    'stroke-linecap="round" stroke-linejoin="round"/>';
  tile.appendChild(svg);
  return tile;
}
