// Printer illustrations by machine style. Original artwork; colours come from CSS
// custom properties (--pi-*) so they follow the light and dark themes.

export const PRINTER_TYPES = [
  ["bedslinger", "Bed slinger"],
  ["cantilever", "Cantilever"],
  ["corexy_open", "Open CoreXY"],
  ["corexy_enclosed", "Enclosed CoreXY"],
  ["delta", "Delta"],
  ["generic", "Generic"],
];

const C = {
  frame: "var(--pi-frame)",
  frame2: "var(--pi-frame2)",
  rail: "var(--pi-rail)",
  top: "var(--pi-top)",
  face: "var(--pi-face)",
  side: "var(--pi-side)",
  glass: "var(--pi-glass)",
  plate: "var(--pi-plate)",
  plateEdge: "var(--pi-plate-edge)",
  plateSide: "var(--pi-plate-side)",
  screen: "var(--pi-screen)",
  spool: "var(--pi-spool)",
  nozzle: "var(--pi-nozzle)",
  bg: "var(--pi-bg)",
};

const shadow = (rx) => `<ellipse cx="80" cy="121" rx="${rx}" ry="4" fill="#000" opacity=".08"/>`;
const head = (x, y, w = 24, h = 24) => {
  const cx = x + w / 2, cy = y + h / 2 - 1;
  return `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="4" fill="${C.frame}"/>` +
    `<circle cx="${cx}" cy="${cy}" r="${Math.min(w, h) * 0.29}" fill="${C.frame2}"/><circle cx="${cx}" cy="${cy}" r="${Math.min(w, h) * 0.09}" fill="${C.rail}"/>` +
    `<path d="M${cx - 3} ${y + h} L${cx + 3} ${y + h} L${cx} ${y + h + 5} Z" fill="${C.nozzle}"/>`;
};
const plate = (x1, x2, y, dx, dy) =>
  `<path d="M${x1} ${y} L${x2} ${y} L${x2 + dx} ${y - dy} L${x1 + dx} ${y - dy} Z" fill="${C.plate}"/>` +
  `<rect x="${x1}" y="${y}" width="${x2 - x1}" height="4" fill="${C.plateEdge}"/>` +
  `<path d="M${x2} ${y} L${x2 + dx} ${y - dy} L${x2 + dx} ${y - dy + 4} L${x2} ${y + 4} Z" fill="${C.plateSide}"/>`;
const base = (x, w, screenX) =>
  `<rect x="${x}" y="104" width="${w}" height="15" rx="4" fill="${C.frame}"/><rect x="${screenX}" y="108" width="17" height="7" rx="1.5" fill="${C.screen}"/>`;

const DRAW = {
  bedslinger: () =>
    shadow(58) +
    `<rect x="34" y="26" width="8" height="80" rx="2" fill="${C.frame2}"/><rect x="118" y="26" width="8" height="80" rx="2" fill="${C.frame2}"/>` +
    `<rect x="31" y="20" width="98" height="9" rx="3" fill="${C.frame}"/><rect x="72" y="14" width="16" height="8" rx="2" fill="${C.frame2}"/>` +
    `<circle cx="80" cy="10" r="10" fill="${C.spool}"/><circle cx="80" cy="10" r="4" fill="${C.bg}"/>` +
    `<rect x="38" y="56" width="84" height="5" rx="2" fill="${C.rail}"/>` +
    head(68, 47) + base(22, 116, 30) + plate(30, 124, 98, 10, 9),
  cantilever: () =>
    shadow(56) +
    `<rect x="28" y="14" width="13" height="92" rx="3" fill="${C.frame2}"/><rect x="28" y="38" width="98" height="10" rx="3" fill="${C.frame}"/>` +
    head(100, 44) + base(22, 116, 114) + plate(54, 126, 98, 8, 7),
  corexy_open: () =>
    shadow(60) +
    `<rect x="46" y="10" width="6" height="96" fill="${C.rail}"/><rect x="130" y="10" width="6" height="96" fill="${C.rail}"/>` +
    `<rect x="46" y="10" width="90" height="6" fill="${C.rail}"/><rect x="46" y="100" width="90" height="6" fill="${C.rail}"/>` +
    `<line x1="44" y1="82" x2="44" y2="106" stroke="${C.rail}" stroke-width="2"/><line x1="122" y1="82" x2="122" y2="106" stroke="${C.rail}" stroke-width="2"/>` +
    plate(36, 112, 86, 16, 11) +
    `<rect x="38" y="32" width="84" height="4" rx="1.5" fill="${C.rail}"/>` + head(64, 28, 22, 20) +
    `<path d="M26 22 L32 22 L52 10 L46 10 Z" fill="${C.frame2}"/><path d="M110 22 L116 22 L136 10 L130 10 Z" fill="${C.frame2}"/><path d="M110 112 L116 112 L136 100 L130 100 Z" fill="${C.frame2}"/>` +
    `<rect x="26" y="22" width="6" height="96" fill="${C.frame}"/><rect x="110" y="22" width="6" height="96" fill="${C.frame}"/>` +
    `<rect x="26" y="22" width="90" height="6" fill="${C.frame}"/><rect x="26" y="112" width="90" height="6" fill="${C.frame}"/>`,
  corexy_enclosed: () =>
    shadow(60) +
    `<path d="M116 22 L138 9 L138 106 L116 119 Z" fill="${C.side}"/><path d="M22 22 L44 9 L138 9 L116 22 Z" fill="${C.top}"/>` +
    `<rect x="22" y="22" width="94" height="97" rx="3" fill="${C.face}"/><rect x="29" y="29" width="80" height="74" rx="4" fill="${C.frame2}"/>` +
    `<rect x="38" y="36" width="62" height="3" rx="1" fill="${C.rail}"/>` +
    `<rect x="58" y="33" width="20" height="17" rx="3" fill="${C.frame}"/><path d="M65 50 L71 50 L68 54 Z" fill="${C.nozzle}"/>` +
    `<path d="M37 88 L99 88 L101 84 L39 84 Z" fill="${C.plate}"/><rect x="37" y="88" width="62" height="3" fill="${C.plateEdge}"/>` +
    `<rect x="29" y="29" width="80" height="74" rx="4" fill="${C.glass}" opacity=".38"/><path d="M33 34 L52 34 L36 64 L33 64 Z" fill="#fff" opacity=".2"/>` +
    `<rect x="104" y="56" width="3" height="18" rx="1.5" fill="${C.frame}"/>` +
    `<rect x="22" y="106" width="94" height="13" rx="3" fill="${C.frame}"/><rect x="28" y="109" width="16" height="6" rx="1.5" fill="${C.screen}"/>` +
    `<rect x="126" y="40" width="6" height="22" rx="2" fill="${C.rail}"/>`,
  delta: () =>
    shadow(50) +
    `<rect x="76" y="6" width="8" height="98" rx="2" fill="${C.rail}"/><rect x="74" y="24" width="12" height="9" rx="2" fill="${C.frame2}"/>` +
    `<path d="M80 33 L77 70 M80 33 L83 70" stroke="${C.rail}" stroke-width="1.6" stroke-linecap="round"/>` +
    `<ellipse cx="80" cy="102" rx="38" ry="9" fill="${C.plateSide}"/><ellipse cx="80" cy="99" rx="38" ry="9" fill="${C.plate}"/>` +
    `<path d="M44 38 L76 72 M48 36 L78 70 M116 38 L84 72 M112 36 L82 70" stroke="${C.rail}" stroke-width="1.6" stroke-linecap="round"/>` +
    `<circle cx="80" cy="73" r="8" fill="${C.frame}"/><circle cx="80" cy="73" r="3" fill="${C.frame2}"/><path d="M77 80 L83 80 L80 85 Z" fill="${C.nozzle}"/>` +
    `<rect x="36" y="12" width="9" height="98" rx="2" fill="${C.frame2}"/><rect x="115" y="12" width="9" height="98" rx="2" fill="${C.frame2}"/>` +
    `<rect x="34" y="32" width="13" height="9" rx="2" fill="${C.frame}"/><rect x="113" y="32" width="13" height="9" rx="2" fill="${C.frame}"/>` +
    `<path d="M32 14 L80 3 L128 14 L80 20 Z" fill="${C.frame}"/><path d="M30 108 L130 108 L126 119 L34 119 Z" fill="${C.frame}"/>` +
    `<rect x="72" y="111" width="16" height="5" rx="1.5" fill="${C.screen}"/>`,
  generic: () =>
    shadow(56) +
    `<path d="M112 22 L130 12 L130 108 L112 119 Z" fill="${C.side}"/><path d="M30 22 L48 12 L130 12 L112 22 Z" fill="${C.top}"/>` +
    `<rect x="30" y="22" width="82" height="97" rx="4" fill="${C.face}"/><rect x="38" y="32" width="66" height="64" rx="4" fill="${C.frame2}"/>` +
    `<rect x="60" y="38" width="22" height="18" rx="3" fill="${C.frame}"/><path d="M68 56 L74 56 L71 61 Z" fill="${C.nozzle}"/>` +
    `<rect x="46" y="84" width="50" height="4" rx="1" fill="${C.plate}"/>` +
    `<rect x="30" y="104" width="82" height="15" rx="4" fill="${C.frame}"/><circle cx="71" cy="111" r="2.5" fill="${C.screen}"/>`,
};

export function printerIcon(type, { size = null, label = "" } = {}) {
  const draw = DRAW[type] || DRAW.generic;
  const dims = size ? ` width="${size}" height="${Math.round((size * 124) / 160)}"` : "";
  const a11y = label ? ` role="img" aria-label="${label.replace(/"/g, "&quot;")}"` : ` aria-hidden="true"`;
  return `<svg class="printer-icon" viewBox="0 0 160 124"${dims}${a11y}>${draw()}</svg>`;
}

export function typeLabel(type) {
  return (PRINTER_TYPES.find(([t]) => t === type) || PRINTER_TYPES[5])[1];
}
