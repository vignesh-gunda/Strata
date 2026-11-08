// Playing cards and player portraits.
// Card art: OpenMoji 15.1 (CC BY-SA 4.0). Portraits: DiceBear "Notionists" (CC0).
// Both are bundled under /static/assets so the demo works without internet.

const ART_BASE = "/static/assets/cards/";
const FACE_BASE = "/static/assets/faces/";

export const CARD_INFO = {
  gear1: { name: "Gear", index: "1", art: "2699", kind: "gear" },
  gear2: { name: "Gear", index: "2", art: "2699", kind: "gear" },
  gear3: { name: "Gear", index: "3", art: "2699", kind: "gear" },
  vent: { name: "Vent", index: "V", art: "1F4A8", kind: "vent" },
  reverse: { name: "Reverse", index: "R", art: "1F504", kind: "reverse" },
  double: { name: "Double", index: "×2", art: "23E9", kind: "double" },
  jam: { name: "Jam", index: "J", art: "1F512", kind: "jam" },
  scan: { name: "Scan", index: "S", art: "1F50D", kind: "scan" },
};

export const SEAT_COLORS = ["#e8743b", "#4b8fd6", "#9b7bd4", "#3fae7c"];
const SEAT_BACKDROPS = ["f6d2bf", "cfe1f6", "e2d8f4", "cdeedd"];

export function cardName(card) {
  const info = CARD_INFO[card];
  if (!info) return card;
  return info.kind === "gear" ? `Gear ${info.index}` : info.name;
}

export function playerName(seat) {
  return `Player ${seat + 1}`;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function cardArt(card) {
  const info = CARD_INFO[card];
  const img = el("img", "art");
  img.alt = "";
  img.decoding = "async";
  img.draggable = false;
  if (info) img.src = `${ART_BASE}${info.art}.svg`;
  img.addEventListener("error", () => img.replaceWith(el("span", "art-fallback", info?.index ?? "?")), { once: true });
  return img;
}

export function cardFace(card, size = "sm") {
  const info = CARD_INFO[card] ?? { name: card, index: "?", kind: "unknown" };
  const face = el("div", `card face ${size} ${info.kind}`);
  face.dataset.card = card;
  face.append(el("span", "corner top", info.index), cardArt(card), el("span", "card-name", info.name), el("span", "corner bottom", info.index));
  return face;
}

export function cardBack(size = "sm") {
  const back = el("div", `card back ${size}`);
  back.append(el("span", "monogram", "S"));
  return back;
}

export function avatar(seat) {
  const img = el("img", "avatar");
  img.alt = "";
  img.draggable = false;
  img.src = `${FACE_BASE}player-${(seat % SEAT_BACKDROPS.length) + 1}.svg`;
  img.addEventListener("error", () => img.replaceWith(el("span", "avatar-fallback", String(seat + 1))), { once: true });
  return img;
}
