// Main screen: the game table, animated. Players sit around a pressure gauge,
// cards fly from hands to the pile, knockouts shake, the winner is announced.

import { avatar, cardBack, cardFace, playerName, SEAT_COLORS } from "./cards.js";
import { describeStep } from "./words.js";

const NS = "http://www.w3.org/2000/svg";
const GAUGE_MAX = 16;
const RADIUS = 118;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;
const LAYOUTS = {
  2: [[50, 80], [50, 20]],
  3: [[50, 80], [17, 26], [83, 26]],
  4: [[50, 80], [13, 50], [50, 20], [87, 50]],
};
const FLIP_BELOW = 60; // seats low on the table show their hand above the portrait

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function svgNode(tag, attrs) {
  const node = document.createElementNS(NS, tag);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
  return node;
}

function buildGauge() {
  const node = el("div", "gauge");
  const svg = svgNode("svg", { viewBox: "0 0 280 280", "aria-hidden": "true" });
  svg.append(svgNode("circle", { cx: 140, cy: 140, r: RADIUS, class: "gauge-track" }));
  Array.from({ length: GAUGE_MAX }, (_, index) => {
    const angle = (index / GAUGE_MAX) * 2 * Math.PI - Math.PI / 2;
    const [inner, outer] = [RADIUS - 22, RADIUS - 14];
    svg.append(svgNode("line", {
      x1: 140 + inner * Math.cos(angle), y1: 140 + inner * Math.sin(angle),
      x2: 140 + outer * Math.cos(angle), y2: 140 + outer * Math.sin(angle), class: "gauge-tick",
    }));
  });
  const ring = svgNode("circle", {
    cx: 140, cy: 140, r: RADIUS, class: "gauge-ring", transform: "rotate(-90 140 140)",
    "stroke-dasharray": CIRCUMFERENCE, "stroke-dashoffset": CIRCUMFERENCE,
  });
  svg.append(ring);
  const readout = el("div", "gauge-readout");
  const number = el("div", "gauge-number", "0");
  readout.append(el("div", "gauge-label", "PRESSURE"), number);
  node.append(svg, readout);
  return { node, ring, number };
}

export class Arena {
  constructor(root) {
    this.tempo = 1;
    this.view = null;
    this.seats = [];
    this.stage = el("div", "arena");
    this.gauge = buildGauge();
    this.deck = el("div", "pile deck");
    this.deckCount = el("div", "pile-count", "");
    this.deck.append(cardBack("md"), cardBack("md"), cardBack("md"), this.deckCount);
    this.discard = el("div", "pile discard");
    this.fx = el("div", "center-fx");
    this.overlay = el("div", "overlay");
    this.stage.append(this.deck, this.gauge.node, this.discard, this.fx, this.overlay);
    this.caption = el("div", "caption", "Press Start to watch the AI study a game it has never seen.");
    this.meta = el("div", "caption-meta", "");
    const bar = el("div", "caption-bar");
    bar.append(this.caption, this.meta);
    root.replaceChildren(this.stage, bar);
  }

  setTempo(tempo) {
    this.tempo = tempo;
  }

  ms(base) {
    return base * this.tempo;
  }

  center(node) {
    const box = node.getBoundingClientRect();
    const stage = this.stage.getBoundingClientRect();
    return [box.left - stage.left + box.width / 2, box.top - stage.top + box.height / 2];
  }

  async fly(from, to, flyer, duration) {
    const [[fx, fy], [tx, ty]] = [this.center(from), this.center(to)];
    flyer.classList.add("flyer");
    this.stage.append(flyer);
    const frames = [
      { transform: `translate(${fx}px, ${fy}px) translate(-50%, -50%) scale(0.7) rotate(-8deg)` },
      { transform: `translate(${tx}px, ${ty}px) translate(-50%, -50%) scale(1) rotate(0deg)` },
    ];
    await flyer.animate(frames, { duration, easing: "cubic-bezier(.2,.7,.2,1)", fill: "forwards" }).finished;
    flyer.remove();
  }

  float(anchor, text, kind = "") {
    const [x, y] = this.center(anchor);
    const tag = el("div", `float ${kind}`, text);
    tag.style.left = `${x}px`;
    tag.style.top = `${y}px`;
    this.stage.append(tag);
    tag.animate([{ opacity: 0, transform: "translate(-50%, -20%)" }, { opacity: 1, transform: "translate(-50%, -90%)", offset: 0.25 },
      { opacity: 0, transform: "translate(-50%, -210%)" }], { duration: this.ms(1300), easing: "ease-out" }).finished.then(() => tag.remove());
  }

  setPressure(value) {
    const shown = Math.min(value, GAUGE_MAX);
    this.gauge.ring.style.strokeDashoffset = String(CIRCUMFERENCE * (1 - shown / GAUGE_MAX));
    this.gauge.node.dataset.heat = value >= 10 ? "high" : value >= 6 ? "warm" : "cool";
    this.gauge.number.textContent = String(value);
    this.gauge.number.animate([{ transform: "scale(1.18)" }, { transform: "scale(1)" }], { duration: this.ms(380) });
  }

  buildSeats(players) {
    this.seats.forEach((seat) => seat.node.remove());
    this.seats = LAYOUTS[players].map(([x, y], seat) => {
      const node = el("div", y > FLIP_BELOW ? "seat flip" : "seat");
      node.style.left = `${x}%`;
      node.style.top = `${y}%`;
      node.style.setProperty("--seat", SEAT_COLORS[seat]);
      const figure = el("div", "figure");
      figure.append(avatar(seat));
      const name = el("div", "seat-name", playerName(seat));
      const status = el("div", "seat-status", "");
      const hand = el("div", "hand");
      node.append(figure, name, status, hand);
      this.stage.append(node);
      return { node, figure, status, hand };
    });
  }

  render(view) {
    this.view = view;
    this.seats.forEach((seat, index) => {
      const current = index === view.current_seat && !view.over;
      seat.node.classList.toggle("current", current);
      seat.node.classList.toggle("out", !view.alive[index]);
      seat.status.textContent = !view.alive[index] ? "Knocked out" : current ? `${view.actions_left} move${view.actions_left > 1 ? "s" : ""}` : "";
      seat.hand.replaceChildren(...view.hands[index].map((card) => cardFace(card, "xs")));
    });
    this.deckCount.textContent = `${view.draw_pile_size} left`;
    this.deck.classList.toggle("empty", view.draw_pile_size === 0);
    const top = view.discard[view.discard.length - 1];
    this.discard.replaceChildren(...(top ? [cardFace(top, "md")] : [el("div", "pile-empty", "discard")]));
    this.setPressure(view.pressure);
  }

  async startGame(game) {
    this.overlay.classList.remove("show");
    this.buildSeats(game.players);
    const dealt = game.view.hands.map(() => []);
    this.render({ ...game.view, hands: dealt, discard: [] });
    this.caption.textContent = `New game: ${game.players} players. The AI only sees what happens on this table.`;
    this.meta.textContent = game.label.replace(":", " · ") + (game.split === "heldout" ? " · held out for testing" : "");
    for (let round = 0; round < game.view.hands[0].length; round += 1) {
      await Promise.all(this.seats.map((seat, index) => wait(index * this.ms(60)).then(() => this.fly(this.deck, seat.hand, cardBack("md"), this.ms(320)))));
    }
    this.render(game.view);
    await wait(this.ms(300));
  }

  async step(trace) {
    const words = describeStep(trace);
    const seat = this.seats[trace.seat];
    const before = this.view.pressure;
    this.caption.textContent = words.text;
    const pressure = trace.events.filter((event) => event.type === "pressure");
    if (trace.action.type === "play") {
      const source = seat.hand.querySelector(`[data-card="${trace.action.card}"]`) || seat.figure;
      await this.fly(source, this.discard, cardFace(trace.action.card, "md"), this.ms(480));
      this.discard.replaceChildren(cardFace(trace.action.card, "md"));
      if (words.knockout) {
        this.setPressure(pressure[0].value);
        this.gauge.node.classList.add("boom");
        seat.node.animate([{ transform: "translate(-50%,-50%) translateX(-6px)" }, { transform: "translate(-50%,-50%) translateX(6px)" }],
          { duration: 70, iterations: 6 });
        this.float(seat.figure, "OUT", "out");
        await wait(this.ms(900));
        this.gauge.node.classList.remove("boom");
      } else if (pressure.length) {
        const delta = pressure[0].value - before;
        this.float(this.gauge.node, `${delta >= 0 ? "+" : "−"}${Math.abs(delta)}`, delta >= 0 ? "heat" : "cool");
      } else if (words.quiet) {
        this.fx.replaceChildren(cardFace(trace.action.card, "lg"));
        await this.fx.animate([{ opacity: 0, transform: "translate(-50%,-50%) scale(.6)" }, { opacity: 1, offset: 0.3 },
          { opacity: 0, transform: "translate(-50%,-50%) scale(1.5)" }], { duration: this.ms(800) }).finished;
      }
      await this.peek(trace);
    } else if (trace.action.type === "draw") {
      const drew = trace.events.some((event) => event.type === "drew");
      if (drew) await this.fly(this.deck, seat.hand, cardBack("md"), this.ms(420));
      else this.float(seat.figure, "nothing", "cool");
    } else {
      this.float(seat.figure, "pass");
    }
    this.render(trace.view);
    const passed = trace.events.find((event) => event.type === "turn_passed");
    if (passed && passed.actions > 1) this.float(this.seats[passed.to].figure, `${passed.actions} moves`, "heat");
    const over = trace.events.find((event) => event.type === "game_over");
    if (over) await this.announce(over.winner);
    return words;
  }

  async peek(trace) {
    const seen = trace.events.find((event) => event.type === "revealed");
    if (!seen) return;
    const row = el("div", "peek");
    row.append(...seen.cards.map((card) => cardFace(card, "md")));
    this.deck.append(row);
    await wait(this.ms(1100));
    row.remove();
  }

  async announce(winner) {
    this.overlay.replaceChildren(
      el("div", "overlay-kicker", "GAME OVER"),
      el("div", "overlay-title", `${playerName(winner)} wins`),
    );
    this.overlay.style.setProperty("--seat", SEAT_COLORS[winner]);
    this.overlay.classList.add("show");
    await wait(this.ms(1600));
  }
}
