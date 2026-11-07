// Panel 02: evidence as layers. Bedrock is raw traces; each layer above is
// more refined: observed facts, inferred rules, confirmed rules.

const MAX_TRACE_LINES = 14;
const MAX_FACTS = 7;

const LAYERS = [
  { key: "confirmed", name: "CONFIRMED", sub: "won a separating experiment and predicts held-out games" },
  { key: "inferred", name: "INFERRED", sub: "fits the evidence, alternatives not yet ruled out" },
  { key: "observed", name: "OBSERVED", sub: "seen directly in traces, no interpretation" },
  { key: "traces", name: "TRACES", sub: "every step, as recorded" },
];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function factRow(fact, fresh) {
  const row = el("div", `fact${fresh ? " fresh" : ""}${fact.status === "refuted" ? " refuted" : ""}`);
  row.append(el("span", "text", fact.text), el("span", "count", fact.countLabel), el("span", "cite", fact.cite || ""));
  return row;
}

function traceLine(trace) {
  const line = el("div", `trace-line${trace.surprise ? " surprise" : ""}`);
  const act = trace.action.card ? `${trace.action.type} ${trace.action.card}` : trace.action.type;
  line.append(el("span", "id", `${trace.trace_id}  `), `${trace.game_id} s${trace.seat}  `, el("b", "", act));
  trace.events.forEach((event) => {
    if (event.type === "pressure") line.append("  ", el("span", "heat", `p=${event.value}`));
    if (event.type === "eliminated") line.append("  ", el("span", "out", `s${event.seat} out`));
    if (event.type === "drew") line.append(`  drew ${event.card}`);
    if (event.type === "revealed") line.append(`  saw ${event.cards.length}`);
  });
  return line;
}

export class Strata {
  constructor(root) {
    this.nodes = {};
    root.replaceChildren(
      ...LAYERS.map((layer) => {
        const section = el("div", "layer");
        section.dataset.layer = layer.key;
        const head = el("div", "layer-head");
        const count = el("div", "layer-count", "0");
        head.append(el("div", "layer-name", layer.name), count, el("div", "layer-sub", layer.sub));
        const body = el("div", `layer-body${layer.key === "traces" ? " trace-log" : ""}`);
        section.append(head, body);
        this.nodes[layer.key] = { count, body };
        return section;
      }),
    );
    this.traceCount = 0;
    this.setLedger({ connected: false, claims: [] });
  }

  pushTrace(trace) {
    this.traceCount += 1;
    const { count, body } = this.nodes.traces;
    count.textContent = this.traceCount.toLocaleString();
    body.prepend(traceLine(trace));
    while (body.children.length > MAX_TRACE_LINES) body.lastChild.remove();
  }

  setObserved(facts, freshKeys) {
    const { count, body } = this.nodes.observed;
    count.textContent = String(facts.length);
    const rows = facts.slice(0, MAX_FACTS).map((fact) =>
      factRow({ text: fact.text, countLabel: `×${fact.count}`, cite: fact.last }, freshKeys.includes(fact.text)),
    );
    body.replaceChildren(...(rows.length ? rows : [el("div", "empty", "Nothing observed yet.")]));
  }

  setLedger(ledger) {
    ["confirmed", "inferred"].forEach((status) => {
      const claims = ledger.claims.filter((claim) => claim.status === status || (status === "confirmed" && claim.status === "refuted"));
      const { count, body } = this.nodes[status];
      count.textContent = String(claims.filter((claim) => claim.status === status).length);
      const rows = claims.slice(0, 5).map((claim) =>
        factRow({
          text: claim.claim,
          status: claim.status,
          countLabel: claim.status === "refuted" ? "refuted" : claim.experiment || `${(claim.evidence || []).length} traces`,
          cite: (claim.evidence || [])[0],
        }),
      );
      const waiting = ledger.connected ? "No claims at this level yet." : "Waiting for the Hypothesizer.";
      body.replaceChildren(...(rows.length ? rows : [el("div", "empty", waiting)]));
    });
  }
}
