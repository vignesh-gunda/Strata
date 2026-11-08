// Plain-language sentences for one step, used by both screens.

import { cardName, playerName } from "./cards.js";

function find(trace, type) {
  return trace.events.filter((event) => event.type === type);
}

export function describeStep(trace) {
  const who = playerName(trace.seat);
  const pressure = find(trace, "pressure");
  const out = find(trace, "eliminated");
  const drew = find(trace, "drew")[0];
  const seen = find(trace, "revealed")[0];
  const over = find(trace, "game_over")[0];
  const parts = [];

  if (trace.action.type === "play") parts.push(`${who} plays ${cardName(trace.action.card)}.`);
  if (find(trace, "recycled").length) parts.push("The deck ran out, so the discard pile becomes the new deck.");
  if (trace.action.type === "draw") parts.push(drew ? `${who} draws a card.` : `${who} tries to draw, but gets nothing.`);
  if (trace.action.type === "pass") parts.push(`${who} has nothing to do and passes.`);

  if (out.length) {
    parts.push(`Pressure hits ${pressure[0].value}. ${playerName(out[0].seat)} is knocked out!`);
  } else if (pressure.length) {
    parts.push(`Pressure is now ${pressure[0].value}.`);
  }
  if (seen) parts.push(`They peek at the top ${seen.cards.length === 1 ? "card" : `${seen.cards.length} cards`} of the deck.`);
  const quiet = trace.action.type === "play" && !pressure.length && !seen;
  if (quiet) parts.push("Nothing visible happens.");
  if (over) parts.push(`${playerName(over.winner)} wins the game!`);
  return { text: parts.join(" "), quiet, knockout: out.length > 0 };
}
