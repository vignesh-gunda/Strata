// Wires the live stream to the two panels.

import { ObservedFacts } from "./observed.js";
import { Strata } from "./strata.js";
import { renderGameStart, renderStep } from "./table.js";

const SPEEDS = { slow: 0.45, normal: 0.15, fast: 0.03 };
const LEDGER_POLL_MS = 2000;
const params = new URLSearchParams(location.search);
const SEED = Number.parseInt(params.get("seed") || "42", 10);
const MUTATIONS = Number.parseInt(params.get("mutations") || "2", 10);

const $ = (selector) => document.querySelector(selector);
const facts = new ObservedFacts();
const strata = new Strata($("#strata"));

let session = { source: null, running: false, speed: "normal", nextIndex: 0, before: null };
let totals = { games: 0, traces: 0, heldout: 0, surprises: 0 };

function setStatus(text, state) {
  const status = $("#status");
  status.textContent = text;
  status.dataset.state = state;
}

function setTotals(next) {
  totals = next;
  Object.entries(totals).forEach(([key, value]) => {
    $(`#${key}`).textContent = value.toLocaleString();
  });
}

function onGame(game) {
  session = { ...session, before: game.view, nextIndex: game.index + 1 };
  renderGameStart(game, facts.lowestOverflow());
  if (game.split === "heldout") setTotals({ ...totals, heldout: totals.heldout + 1 });
}

function onTrace(trace) {
  const fresh = facts.ingest(trace, session.before || trace.view);
  session = { ...session, before: trace.view };
  renderStep(trace, facts.lowestOverflow());
  strata.pushTrace(trace);
  strata.setObserved(facts.list(), fresh);
  setTotals({ ...totals, traces: totals.traces + 1, surprises: totals.surprises + (trace.surprise ? 1 : 0) });
}

function onEnd() {
  setTotals({ ...totals, games: totals.games + 1 });
}

function start() {
  const url = `/api/stream?seed=${SEED}&mutations=${MUTATIONS}&delay=${SPEEDS[session.speed]}&start=${session.nextIndex}`;
  const source = new EventSource(url);
  source.addEventListener("run", (event) => {
    $("#run").textContent = JSON.parse(event.data).run_id;
  });
  source.addEventListener("game", (event) => onGame(JSON.parse(event.data)));
  source.addEventListener("trace", (event) => onTrace(JSON.parse(event.data)));
  source.addEventListener("end", (event) => onEnd(JSON.parse(event.data)));
  source.onerror = () => stop("DISCONNECTED");
  session = { ...session, source, running: true };
  setStatus("LIVE", "live");
  $("#toggle").textContent = "Pause";
}

function stop(label = "PAUSED") {
  if (session.source) session.source.close();
  session = { ...session, source: null, running: false };
  setStatus(label, "idle");
  $("#toggle").textContent = "Resume";
}

$("#toggle").addEventListener("click", () => (session.running ? stop() : start()));

document.querySelectorAll("[data-speed]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-speed]").forEach((other) => other.setAttribute("aria-pressed", String(other === button)));
    session = { ...session, speed: button.dataset.speed };
    if (session.running) {
      stop();
      start(); // speed applies from the next game
    }
  });
});

async function pollLedger() {
  try {
    const response = await fetch("/api/ledger", { cache: "no-store" });
    if (response.ok) strata.setLedger(await response.json());
  } catch {
    // server restarting; try again on the next tick
  }
}

setInterval(pollLedger, LEDGER_POLL_MS);
pollLedger();
