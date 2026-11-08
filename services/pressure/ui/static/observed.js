// Facts the AI can state just from watching, written in plain words.
// Each fact counts how often it was seen and remembers the latest step that showed it.

import { cardName } from "./cards.js";

function eventsOf(trace, type) {
  return trace.events.filter((event) => event.type === type);
}

function factsFromTrace(trace, before) {
  const facts = [];
  const action = trace.action;
  const pressure = eventsOf(trace, "pressure");
  const out = eventsOf(trace, "eliminated");
  const turn = eventsOf(trace, "turn_passed")[0];
  const card = action.card ? cardName(action.card) : "";
  const quiet = trace.events.every((event) => ["played", "turn_passed", "game_over"].includes(event.type));

  if (action.type === "play") {
    if (pressure.length && !out.length) {
      const delta = pressure[0].value - before.pressure;
      if (delta > 0) facts.push(`${card} raises pressure by ${delta}`);
      if (delta < 0) facts.push(`${card} lowers pressure by ${-delta}`);
      if (delta === 0) facts.push(`${card} leaves pressure unchanged`);
    }
    if (quiet) facts.push(`Sometimes ${card} does nothing you can see`);
    eventsOf(trace, "revealed").forEach((event) =>
      facts.push(`${card} lets you peek at the top ${event.cards.length === 1 ? "card" : `${event.cards.length} cards`}`),
    );
    if (turn && turn.actions > 1) facts.push(`After ${card}, the next player gets ${turn.actions} moves`);
  }
  if (out.length && pressure.length >= 2) {
    facts.push(`A player is knocked out when pressure reaches ${pressure[0].value}`);
    facts.push(`After a knockout, pressure drops to ${pressure[pressure.length - 1].value}`);
  }
  if (action.type === "draw" && !eventsOf(trace, "drew").length) facts.push("Sometimes drawing gives no card");
  const handSize = (before.hands[trace.seat] || []).length;
  const drawOffered = trace.legal.some((legal) => legal.type === "draw");
  if (!drawOffered && before.draw_pile_size > 0) facts.push(`You can't draw while holding ${handSize} cards`);
  return facts;
}

export class ObservedFacts {
  constructor() {
    this.facts = new Map();
  }

  // Feed one step plus the table just before it; returns the facts it supported.
  ingest(trace, before) {
    const keys = factsFromTrace(trace, before);
    keys.forEach((key) => {
      const known = this.facts.get(key) || { text: key, count: 0 };
      this.facts.set(key, { ...known, count: known.count + 1, last: trace.trace_id });
    });
    return keys;
  }

  list() {
    return [...this.facts.values()].sort((a, b) => b.count - a.count);
  }
}
