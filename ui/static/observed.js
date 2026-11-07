// Observed facts: what the traces show directly, with no interpretation.
// Each fact counts how often it was seen and cites the latest trace ID.

const GEARS = new Set(["gear1", "gear2", "gear3"]);

function eventsOf(trace, type) {
  return trace.events.filter((event) => event.type === type);
}

function factsFromTrace(trace, before) {
  const facts = [];
  const action = trace.action;
  const pressure = eventsOf(trace, "pressure");
  const out = eventsOf(trace, "eliminated");
  const turn = eventsOf(trace, "turn_passed")[0];
  const quiet = trace.events.every((event) => ["played", "turn_passed", "game_over"].includes(event.type));

  if (action.type === "play") {
    if (pressure.length && !out.length) {
      const delta = pressure[0].value - before.pressure;
      facts.push(`play ${action.card} → pressure ${delta >= 0 ? "+" : "−"}${Math.abs(delta)}`);
    }
    if (quiet) facts.push(`play ${action.card} → nothing visible changed`);
    eventsOf(trace, "revealed").forEach((event) => facts.push(`play ${action.card} → reveals top ${event.cards.length}`));
    if (turn && turn.actions > 1) facts.push(`after ${action.card}, next seat got ${turn.actions} actions`);
  }
  if (out.length && pressure.length >= 2) {
    facts.push(`seat eliminated when pressure hit ${pressure[0].value}`);
    facts.push(`pressure after an elimination = ${pressure[pressure.length - 1].value}`);
  }
  if (action.type === "draw" && !eventsOf(trace, "drew").length) facts.push("draw → no card taken");
  const handSize = (before.hands[trace.seat] || []).length;
  const drawOffered = trace.legal.some((legal) => legal.type === "draw");
  if (!drawOffered && before.draw_pile_size > 0) facts.push(`draw not offered with ${handSize} cards in hand`);
  return facts;
}

export class ObservedFacts {
  constructor() {
    this.facts = new Map();
    this.overflowValues = new Map();
  }

  // Feed one trace plus the view just before it; returns the keys that changed.
  ingest(trace, before) {
    const keys = factsFromTrace(trace, before);
    keys.forEach((key) => {
      const known = this.facts.get(key) || { text: key, count: 0, first: trace.trace_id };
      this.facts.set(key, { ...known, count: known.count + 1, last: trace.trace_id });
    });
    eventsOf(trace, "eliminated").forEach(() => {
      const value = eventsOf(trace, "pressure")[0]?.value;
      if (value !== undefined) this.overflowValues.set(value, (this.overflowValues.get(value) || 0) + 1);
    });
    return keys;
  }

  list() {
    return [...this.facts.values()].sort((a, b) => b.count - a.count);
  }

  lowestOverflow() {
    const values = [...this.overflowValues.keys()];
    return values.length ? Math.min(...values) : null;
  }
}

export function isGear(card) {
  return GEARS.has(card);
}
