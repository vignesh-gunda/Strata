// Panel 01: the black box as an observer sees it (no hidden state).

import { isGear } from "./observed.js";

const SCALE_STEPS = 16;
const $ = (selector) => document.querySelector(selector);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderScale(pressure, overflow) {
  const scale = $("#scale");
  scale.replaceChildren(
    ...Array.from({ length: SCALE_STEPS }, (_, index) => {
      const tick = el("i");
      const value = index + 1;
      if (value <= pressure) tick.className = overflow && value >= overflow - 2 ? "hot" : "on";
      if (overflow && value === overflow) tick.classList.add("mark");
      return tick;
    }),
  );
  $("#limit-note").textContent = overflow
    ? `Limit: not given · eliminations observed at ${overflow}`
    : "Limit: not given · no elimination seen yet";
}

function renderSeats(view) {
  const seats = view.hands.map((hand, seat) => {
    const row = el("div", "seat");
    row.classList.toggle("current", seat === view.current_seat && !view.over);
    row.classList.toggle("out", !view.alive[seat]);
    const chips = el("div", "hand");
    chips.replaceChildren(...hand.map((card) => el("span", `chip${isGear(card) ? " gear" : ""}`, card)));
    const acts = seat === view.current_seat && !view.over ? `${view.actions_left} act` : view.alive[seat] ? "" : "out";
    row.append(el("div", "seat-name", `SEAT ${seat}`), chips, el("div", "acts", acts));
    return row;
  });
  $("#seats").replaceChildren(...seats);
}

function describe(trace) {
  const line = el("span");
  const act = trace.action.type === "play" ? `plays ${trace.action.card}` : trace.action.type === "draw" ? "draws" : "passes";
  line.append(el("span", "", `Seat ${trace.seat} ${act}`));
  const effects = [];
  trace.events.forEach((event) => {
    if (event.type === "pressure") effects.push(["pressure " + event.value, "em"]);
    if (event.type === "eliminated") effects.push([`seat ${event.seat} is out`, "em"]);
    if (event.type === "drew") effects.push([`gets ${event.card}`, "span"]);
    if (event.type === "revealed") effects.push([`sees ${event.cards.join(", ")}`, "span"]);
    if (event.type === "game_over") effects.push([event.winner === null ? "game over · no winner" : `seat ${event.winner} wins`, "em"]);
  });
  if (!effects.length && trace.action.type === "play") effects.push(["nothing visible changed", "quiet"]);
  effects.forEach(([text, kind]) => {
    line.append(" → ");
    line.append(kind === "quiet" ? el("span", "quiet", text) : el(kind === "em" ? "em" : "span", "", text));
  });
  return line;
}

export function renderGameStart(game, overflow) {
  $("#game-label").textContent = game.label;
  const split = $("#split");
  split.textContent = game.split === "heldout" ? "held out" : "";
  split.className = game.split === "heldout" ? "tag heldout" : "tag";
  $("#last").replaceChildren(el("span", "quiet", `New game ${game.game_id} · ${game.players} players`));
  renderView(game.view, overflow);
}

export function renderView(view, overflow) {
  const num = $("#pressure");
  const changed = num.textContent !== String(view.pressure);
  num.textContent = String(view.pressure);
  num.classList.toggle("bump", changed && view.pressure > 0);
  renderScale(view.pressure, overflow);
  renderSeats(view);
  $("#pile").textContent = String(view.draw_pile_size);
  $("#discard").textContent = String(view.discard.length);
  $("#turn").textContent = String(view.turn);
}

export function renderStep(trace, overflow) {
  $("#last").replaceChildren(describe(trace));
  renderView(trace.view, overflow);
}
