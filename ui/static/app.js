// Wires the live stream to the two screens: the animated table (main) and
// the plain-language "behind the scenes" view.

import { Arena } from "./arena.js";
import { AskPanel } from "./ask.js";
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
const ask = new AskPanel($("#ask"));
const facts = new ObservedFacts();

// "simulator" = the built-in games; "agent" = the probe games the agent itself plays (T16.3)
let session = { source: null, running: false, speed: "normal", nextIndex: 0, before: null, mode: "simulator", agentNext: 0 };
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
    session = { ...session, agentNext: session.mode === "agent" ? message.data.index + 1 : session.agentNext };
    totals = { ...totals, games: totals.games + 1 };
    const { winner, steps } = message.data;
    if (session.game && Number.isInteger(winner)) backstage.addResult({ ...session.game, winner, steps });
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
  const url = session.mode === "agent"
    ? `/api/agent-traces?delay=${delay}&since=${session.agentNext}`
    : `/api/stream?seed=${SEED}&mutations=${MUTATIONS}&delay=${delay}&start=${session.nextIndex}`;
  const source = new EventSource(url);
  source.addEventListener("run", (event) => {
    $("#run").textContent = JSON.parse(event.data).run_id;
  });
  ["game", "trace", "end"].forEach((kind) => source.addEventListener(kind, enqueue(kind)));
  source.onerror = () => stop(session.mode === "agent" ? "NO AGENT GAMES" : "OFFLINE");
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
$("#source").addEventListener("click", () => {
  const wasRunning = session.running;
  if (wasRunning) stop();
  const mode = session.mode === "agent" ? "simulator" : "agent";
  queue = [];
  session = { ...session, mode, before: null };
  $("#source").textContent = mode === "agent" ? "Table: agent's games" : "Table: simulator";
  if (wasRunning) start();
});
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

// The agent's live action feed. It runs on its own, so it is not tied to Start/Pause.
const FEED_RETRY_MS = 10000;
function connectFeed() {
  const source = new EventSource("/api/feed");
  source.addEventListener("status", (event) => {
    const { connected, run } = JSON.parse(event.data);
    backstage.setAgentState(connected, run);
    ask.setAgentState(connected, run);
    if (!connected) {
      source.close(); // no agent run attached to this server; look again later
      setTimeout(connectFeed, FEED_RETRY_MS);
    }
  });
  source.addEventListener("feed", (event) => {
    const action = JSON.parse(event.data);
    backstage.pushAction(action);
    ask.pushAction(action);
  });
  source.onerror = () => {
    backstage.setAgentState(false);
    ask.setAgentState(false);
  }; // EventSource reconnects by itself and resumes by loop id
}

setInterval(pollLedger, LEDGER_POLL_MS);
pollLedger();
connectFeed();
showTab("game");
