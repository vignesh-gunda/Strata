// Wires the live stream to the two screens: the animated table (main) and
// the plain-language "behind the scenes" view.

import { Arena } from "./arena.js";
import { Backstage } from "./backstage.js";
import { ObservedFacts } from "./observed.js";

// server pause per step (s) and animation tempo (1 = normal)
const SPEEDS = { slow: { delay: 1.6, tempo: 1.4 }, normal: { delay: 1.0, tempo: 1 }, fast: { delay: 0.3, tempo: 0.35 } };
const BACKLOG_RUSH = 6;
const LEDGER_POLL_MS = 2000;
const params = new URLSearchParams(location.search);
const SEED = Number.parseInt(params.get("seed") || "42", 10);
const MUTATIONS = Number.parseInt(params.get("mutations") || "2", 10);

const $ = (selector) => document.querySelector(selector);
const arena = new Arena($("#arena"));
const backstage = new Backstage($("#backstage"));
const facts = new ObservedFacts();

let session = { source: null, running: false, speed: "normal", nextIndex: 0, before: null };
let totals = { games: 0, traces: 0 };
let queue = [];
let draining = false;

function setStatus(text, state) {
  const status = $("#status");
  status.textContent = text;
  status.dataset.state = state;
}

async function handle(message) {
  if (message.kind === "game") {
    const { index, label, players } = message.data;
    session = { ...session, before: message.data.view, nextIndex: index + 1, game: { index, label, players } };
    await arena.startGame(message.data);
  } else if (message.kind === "trace") {
    const trace = message.data;
    const fresh = facts.ingest(trace, session.before || trace.view);
    session = { ...session, before: trace.view };
    totals = { ...totals, traces: totals.traces + 1 };
    backstage.pushStep(trace);
    backstage.setObserved(facts.list(), fresh);
    await arena.step(trace);
  } else if (message.kind === "end") {
    totals = { ...totals, games: totals.games + 1 };
    if (session.game) backstage.addResult({ ...session.game, winner: message.data.winner, steps: message.data.steps });
  }
  backstage.setStats(totals, facts.list().length);
}

async function drain() {
  if (draining) return;
  draining = true;
  while (queue.length) {
    const [next, ...rest] = queue;
    queue = rest;
    const tableHidden = $("#arena").hidden; // nobody is watching the table: skip its animations
    arena.setTempo(tableHidden ? 0.01 : queue.length > BACKLOG_RUSH ? 0.15 : SPEEDS[session.speed].tempo);
    await handle(next);
  }
  draining = false;
}

function enqueue(kind) {
  return (event) => {
    queue = [...queue, { kind, data: JSON.parse(event.data) }];
    drain();
  };
}

function start() {
  const { delay } = SPEEDS[session.speed];
  const source = new EventSource(`/api/stream?seed=${SEED}&mutations=${MUTATIONS}&delay=${delay}&start=${session.nextIndex}`);
  source.addEventListener("run", (event) => {
    $("#run").textContent = JSON.parse(event.data).run_id;
  });
  ["game", "trace", "end"].forEach((kind) => source.addEventListener(kind, enqueue(kind)));
  source.onerror = () => stop("OFFLINE");
  session = { ...session, source, running: true };
  setStatus("LIVE", "live");
  $("#toggle").textContent = "Pause";
}

function stop(label = "PAUSED") {
  if (session.source) session.source.close();
  session = { ...session, source: null, running: false };
  queue = queue.filter((message) => message.kind !== "game");
  setStatus(label, "idle");
  $("#toggle").textContent = "Resume";
}

function showTab(name) {
  document.querySelectorAll("[data-tab]").forEach((tab) => tab.setAttribute("aria-selected", String(tab.dataset.tab === name)));
  document.querySelectorAll("[data-view]").forEach((view) => {
    view.hidden = view.dataset.view !== name;
  });
}

$("#toggle").addEventListener("click", () => (session.running ? stop() : start()));
document.querySelectorAll("[data-tab]").forEach((tab) => tab.addEventListener("click", () => showTab(tab.dataset.tab)));
document.querySelectorAll("[data-speed]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-speed]").forEach((other) => other.setAttribute("aria-pressed", String(other === button)));
    session = { ...session, speed: button.dataset.speed };
    if (session.running) {
      stop();
      start();
    }
  });
});

async function pollLedger() {
  try {
    const response = await fetch("/api/ledger", { cache: "no-store" });
    if (response.ok) backstage.setLedger(await response.json());
  } catch {
    // server restarting; try again on the next tick
  }
}

setInterval(pollLedger, LEDGER_POLL_MS);
pollLedger();
showTab("game");
