// Behind the scenes, in plain words: what the AI just watched, and what it
// has learned so far, split into "seen", "guessing" and "proven".

import { ActionLog } from "./ask.js";
import { cardArt } from "./cards.js";
import { Results } from "./results.js";
import { describeStep } from "./words.js";

const MAX_FEED = 9;
const MAX_FACTS = 8;

const COLUMNS = [
  { key: "observed", step: "1", title: "Seen with its own eyes", hint: "Things that happened in front of it. No guessing." },
  { key: "inferred", step: "2", title: "Its guesses", hint: "Rules it thinks are true, but hasn't proven yet." },
  { key: "confirmed", step: "3", title: "Proven", hint: "Rules that held up in games it had never seen." },
];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function item(text, detail, fresh, refuted) {
  const row = el("li", `learned${fresh ? " fresh" : ""}${refuted ? " refuted" : ""}`);
  row.append(el("span", "learned-text", text), el("span", "learned-detail", detail));
  return row;
}

export class Backstage {
  constructor(root) {
    const intro = el("div", "intro");
    intro.append(
      el("h2", "", "The AI never reads the rules. It only watches games."),
      el("p", "", "Every move is written down as evidence. From that evidence it builds up what it knows, step by step."),
    );
    this.stats = el("div", "intro-stats");
    intro.append(this.stats);

    this.actionLog = new ActionLog();
    const actions = this.actionLog.node;

    const feedBox = el("section", "feed-box");
    feedBox.append(el("h3", "", "What it just watched"));
    this.feed = el("ol", "feed");
    feedBox.append(this.feed);

    const learned = el("section", "learned-box");
    this.columns = {};
    COLUMNS.forEach((column) => {
      const box = el("div", `column ${column.key}`);
      const head = el("div", "column-head");
      const count = el("span", "column-count", "0");
      head.append(el("span", "column-step", column.step), el("span", "column-title", column.title), count);
      const list = el("ol", "column-list");
      box.append(head, el("p", "column-hint", column.hint), list);
      learned.append(box);
      this.columns[column.key] = { count, list };
    });

    const body = el("div", "backstage-body");
    body.append(feedBox, learned);
    this.results = new Results();
    root.replaceChildren(intro, actions, this.results.node, body);
    this.watched = 0;
    this.setLedger({ connected: false, claims: [] });
  }

  pushStep(trace) {
    this.watched += 1;
    const row = el("li", "feed-row");
    const mark = trace.action.card ? cardArt(trace.action.card) : el("span", "feed-dot");
    row.append(el("span", "feed-step", `#${this.watched}`), mark, el("span", "", describeStep(trace).text));
    this.feed.prepend(row);
    while (this.feed.children.length > MAX_FEED) this.feed.lastChild.remove();
  }

  // One agent loop. `action` is {loop, sentence, type, trace_ids} from /api/feed. Every line is kept.
  pushAction(action) {
    this.actionLog.push(action);
  }

  setAgentState(connected, run) {
    this.actionLog.setState(connected, run);
  }

  addResult(result) {
    this.results.add(result);
  }

  setStats(totals, factCount) {
    this.stats.textContent = `${totals.traces.toLocaleString()} moves watched · ${totals.games} games · ${factCount} things noticed`;
  }

  setObserved(facts, freshKeys) {
    const { count, list } = this.columns.observed;
    count.textContent = String(facts.length);
    const rows = facts.slice(0, MAX_FACTS).map((fact) =>
      item(fact.text, `seen ${fact.count}×`, freshKeys.includes(fact.text), false));
    list.replaceChildren(...(rows.length ? rows : [el("li", "column-empty", "Watching the first moves…")]));
  }

  setLedger(ledger) {
    ["inferred", "confirmed"].forEach((status) => {
      const claims = ledger.claims.filter((claim) => claim.status === status || (status === "inferred" && claim.status === "refuted"));
      const { count, list } = this.columns[status];
      count.textContent = String(claims.filter((claim) => claim.status === status).length);
      const rows = claims.slice(0, MAX_FACTS).map((claim) => {
        const proof = (claim.evidence || []).length;
        const detail = claim.status === "refuted" ? "proven wrong" : `${proof} moves of evidence`;
        return item(claim.claim, detail, false, claim.status === "refuted");
      });
      const waiting = ledger.connected ? "Nothing here yet." : "The AI hasn't started guessing yet.";
      list.replaceChildren(...(rows.length ? rows : [el("li", "column-empty", waiting)]));
    });
  }
}
