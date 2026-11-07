// "Who won": results grouped in rounds of 3 games, plus a running tally.

import { avatar, playerName, SEAT_COLORS } from "./cards.js";

const ROUND = 3;
const ROUNDS_SHOWN = 3;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function describeSetup(label) {
  const [kind, name = ""] = label.split(":");
  const words = name.replaceAll("_", " ");
  return kind === "scenario" ? `test: ${words}` : `${words} players`;
}

function chunk(items, size) {
  return Array.from({ length: Math.ceil(items.length / size) }, (_, i) => items.slice(i * size, i * size + size));
}

function face(winner) {
  if (winner === null) return el("span", "result-face draw", "=");
  const holder = el("span", "result-face");
  holder.style.setProperty("--seat", SEAT_COLORS[winner % SEAT_COLORS.length]);
  holder.append(avatar(winner));
  return holder;
}

function gameRow(result) {
  const row = el("li", "result-row");
  const text = el("div", "result-text");
  text.append(
    el("b", "", result.winner === null ? "Draw" : `${playerName(result.winner)} wins`),
    el("span", "", `Game ${result.index + 1} · ${result.players} players · ${result.steps} moves · ${describeSetup(result.label)}`),
  );
  row.append(face(result.winner), text);
  return row;
}

function roundCard(games, number, fresh) {
  const complete = games.length === ROUND;
  const card = el("div", `round${complete ? "" : " open"}${fresh ? " fresh" : ""}`);
  const first = games[0].index + 1;
  const head = el("div", "round-head");
  head.append(
    el("span", "round-name", `Round ${number}`),
    el("span", "round-range", `games ${first}–${first + ROUND - 1}`),
    el("span", "round-state", complete ? "complete" : `${games.length} of ${ROUND} played`),
  );
  const list = el("ol", "round-list");
  list.append(...games.map(gameRow));
  card.append(head, list);
  return card;
}

function tally(results) {
  const wins = results.reduce((counts, { winner }) => {
    const key = winner === null ? "draw" : String(winner);
    return { ...counts, [key]: (counts[key] || 0) + 1 };
  }, {});
  const seats = Object.keys(wins).filter((key) => key !== "draw").map(Number).sort((a, b) => wins[b] - wins[a] || a - b);
  const chips = seats.map((seat) => {
    const chip = el("span", "tally-chip");
    chip.append(face(seat), el("b", "", String(wins[seat])), el("span", "", playerName(seat)));
    return chip;
  });
  if (wins.draw) {
    const chip = el("span", "tally-chip");
    chip.append(face(null), el("b", "", String(wins.draw)), el("span", "", "Draws"));
    chips.push(chip);
  }
  return chips;
}

export class Results {
  constructor() {
    this.node = el("section", "results");
    const head = el("div", "results-head");
    const titles = el("div");
    titles.append(el("h3", "", "Who won · every 3 games"), el("p", "results-hint", "Each round is 3 games the AI watched. The tally shows every win so far."));
    this.tally = el("div", "tally");
    head.append(titles, this.tally);
    this.rounds = el("div", "rounds");
    this.node.append(head, this.rounds);
    this.results = [];
    this.render(false);
  }

  add(result) {
    this.results = [...this.results, result];
    this.render(this.results.length % ROUND === 0);
  }

  render(roundJustClosed) {
    const rounds = chunk(this.results, ROUND);
    const shown = rounds.map((games, i) => ({ games, number: i + 1 })).slice(-ROUNDS_SHOWN).reverse();
    const cards = shown.map(({ games, number }, i) => roundCard(games, number, i === 0 && roundJustClosed));
    this.rounds.replaceChildren(...(cards.length ? cards : [el("div", "column-empty", "The first round of 3 games is being played…")]));
    this.tally.replaceChildren(...tally(this.results));
  }
}
