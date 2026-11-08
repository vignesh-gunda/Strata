// "Ask the agent": one text box, the agent's one-line updates, and the folder it is coding into.
// Everything the server sends is shown with textContent, never as HTML.

const POLL_MS = 2000;
const EXAMPLES = ["Rebuild it in Go", "Rebuild it in JavaScript", "Explain the rules of the game", "What did you find wrong in the manual?"];
const FILE_RETRY_MS = 10000;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function sizeLabel(bytes) {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;
}

export class AskPanel {
  constructor(root) {
    this.root = root;
    this.files = new Map();
    this.selected = null;
    this.follow = true;
    this.pending = false;
    this.build();
    this.pollStatus();
    setInterval(() => this.pollStatus(), POLL_MS);
    this.connectFiles();
  }

  build() {
    const intro = el("div", "ask-intro");
    intro.append(
      el("h2", "", "Tell the agent what you want."),
      el("p", "", "Type one line. Ask a question about the game, or ask it to rebuild the game in a language. It answers in plain words and shows its work below."),
    );

    this.form = el("form", "ask-form");
    this.input = el("input", "ask-input");
    this.input.type = "text";
    this.input.maxLength = 400;
    this.input.placeholder = "e.g. Rebuild it in Go";
    this.input.setAttribute("aria-label", "Ask the agent");
    this.send = el("button", "primary", "Send");
    this.send.type = "submit";
    this.stop = el("button", "ghost", "Stop the run");
    this.stop.type = "button";
    this.stop.hidden = true;
    this.form.append(this.input, this.send, this.stop);

    const chips = el("div", "ask-chips");
    EXAMPLES.forEach((text) => {
      const chip = el("button", "chip", text);
      chip.type = "button";
      chip.addEventListener("click", () => {
        this.input.value = text;
        this.input.focus();
      });
      chips.append(chip);
    });

    this.state = el("div", "ask-state", "Checking the agent…");
    this.log = el("ol", "ask-log");
    this.log.setAttribute("aria-live", "polite");

    this.actionLog = new ActionLog();
    const actionsBox = el("section", "ask-section");
    actionsBox.append(this.actionLog.node);

    const filesBox = el("section", "ask-section");
    const filesHead = el("div", "actions-head");
    this.filesState = el("span", "actions-state", "No code yet");
    filesHead.append(el("h3", "", "The code it is writing"), this.filesState);
    const followLabel = el("label", "follow");
    this.followBox = el("input");
    this.followBox.type = "checkbox";
    this.followBox.checked = true;
    followLabel.append(this.followBox, document.createTextNode(" Follow the newest change"));
    this.followBox.addEventListener("change", () => {
      this.follow = this.followBox.checked;
    });
    const split = el("div", "files");
    this.fileList = el("ul", "file-list");
    this.fileEmpty = el("li", "column-empty", "Files appear here as soon as the agent starts writing them.");
    this.fileList.append(this.fileEmpty);
    const viewer = el("div", "file-viewer");
    this.fileName = el("div", "file-name", "");
    this.fileText = el("pre", "file-text", "");
    this.fileText.tabIndex = 0;
    viewer.append(this.fileName, this.fileText);
    split.append(this.fileList, viewer);
    filesBox.append(filesHead, followLabel, split);

    this.root.replaceChildren(intro, this.form, chips, this.state, this.log, actionsBox, filesBox);

    this.form.addEventListener("submit", (event) => {
      event.preventDefault();
      this.submit();
    });
    this.stop.addEventListener("click", () => this.cancel());
  }

  // ---------- ask ----------
  addLine(who, text, kind = "") {
    const row = el("li", `ask-line ${who} ${kind}`.trim());
    row.append(el("span", "ask-who", who === "you" ? "You" : "Agent"), el("span", "ask-text", text));
    this.log.append(row);
    row.scrollIntoView({ block: "nearest" });
    return row;
  }

  async post(path, body) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return response.json();
  }

  async submit() {
    const text = this.input.value.trim();
    if (!text || this.pending) return;
    this.pending = true;
    this.send.disabled = true;
    this.addLine("you", text);
    const waiting = this.addLine("agent", "Thinking…", "waiting");
    this.input.value = "";
    try {
      const result = await this.post("/api/ask", { text });
      waiting.querySelector(".ask-text").textContent = result.reply || "No answer.";
      waiting.classList.remove("waiting");
      if (!result.ok) waiting.classList.add("refused");
    } catch {
      waiting.querySelector(".ask-text").textContent = "I could not reach the agent. Is the server still running?";
      waiting.classList.remove("waiting");
      waiting.classList.add("refused");
    } finally {
      this.pending = false;
      this.send.disabled = false;
      this.pollStatus();
    }
  }

  async cancel() {
    this.stop.disabled = true;
    try {
      const result = await this.post("/api/cancel", {});
      this.addLine("agent", result.reply || "Asked the run to stop.");
    } catch {
      this.addLine("agent", "I could not reach the agent to stop the run.", "refused");
    } finally {
      this.stop.disabled = false;
      this.pollStatus();
    }
  }

  async pollStatus() {
    try {
      const response = await fetch("/api/agent", { cache: "no-store" });
      this.renderStatus(await response.json());
    } catch {
      this.state.textContent = "The server is not answering. Retrying…";
    }
  }

  renderStatus(info) {
    if (!info.enabled) {
      this.state.textContent = "The agent is not attached to this server. Start it with --run-dir agent/runs/<run_id> to use the ask box.";
      this.input.disabled = true;
      this.send.disabled = true;
      return;
    }
    this.input.disabled = false;
    if (!this.pending) this.send.disabled = false;
    const models = info.models || {};
    const parts = [info.status];
    if (models.started) parts.push(`Models on: ${["muse", "subq"].filter((m) => models[m]).join(" + ") || "none (keyword mode)"}`);
    const budget = info.budget || {};
    const used = Object.entries(budget).map(([name, b]) => `${name} ${(b.used || 0).toLocaleString()} tokens`);
    if (used.length) parts.push(`Ask box used: ${used.join(", ")}`);
    this.state.textContent = parts.join(" · ");
    this.stop.hidden = !info.running;
  }

  // ---------- the agent's one-line updates ----------
  pushAction(action) {
    this.actionLog.push(action);
  }

  setAgentState(connected, run) {
    this.actionLog.setState(connected, run);
  }

  // ---------- the folder it is coding into ----------
  connectFiles() {
    const source = new EventSource("/api/files/stream");
    source.addEventListener("status", (event) => {
      if (!JSON.parse(event.data).connected) {
        source.close();
        setTimeout(() => this.connectFiles(), FILE_RETRY_MS);
      }
    });
    source.addEventListener("files", (event) => this.renderFiles(JSON.parse(event.data)));
  }

  renderFiles({ files, changes }) {
    const fresh = new Set((changes || []).filter((c) => c.kind !== "deleted").map((c) => c.path));
    this.files = new Map(files.map((file) => [file.path, file]));
    this.fileList.replaceChildren();
    if (!files.length) {
      this.fileList.append(this.fileEmpty);
      this.filesState.textContent = "No code yet";
      return;
    }
    files.forEach((file) => {
      const row = el("li", fresh.has(file.path) ? "file fresh" : "file");
      const button = el("button", "file-button", file.path);
      button.type = "button";
      button.setAttribute("aria-current", String(file.path === this.selected));
      button.addEventListener("click", () => {
        this.follow = false;
        this.followBox.checked = false;
        this.open(file.path);
      });
      row.append(button, el("span", "file-size", sizeLabel(file.size)));
      this.fileList.append(row);
    });
    this.filesState.textContent = `${files.length} file${files.length === 1 ? "" : "s"}${fresh.size ? " · just changed: " + [...fresh][0] : ""}`;
    if (this.follow && fresh.size) this.open([...fresh][fresh.size - 1]);
    else if (this.selected && fresh.has(this.selected)) this.open(this.selected);
    else if (!this.selected) this.open(files[0].path);
  }

  async open(path) {
    this.selected = path;
    this.fileList.querySelectorAll(".file-button").forEach((b) => b.setAttribute("aria-current", String(b.textContent === path)));
    this.fileName.textContent = path;
    try {
      const response = await fetch(`/api/files/read?path=${encodeURIComponent(path)}`, { cache: "no-store" });
      const file = await response.json();
      if (file.error) this.fileText.textContent = "This file cannot be shown.";
      else if (file.hidden) this.fileText.textContent = "This file was hidden because it touches restricted material.";
      else if (file.binary) this.fileText.textContent = `Binary file, ${sizeLabel(file.size)}.`;
      else this.fileText.textContent = file.truncated ? `${file.text}\n… (showing the first part)` : file.text;
      if (this.follow) this.fileText.scrollTop = this.fileText.scrollHeight;
    } catch {
      this.fileText.textContent = "Could not load this file.";
    }
  }
}

// ---------- the agent's loop lines: every one of them, kept for the whole run ----------
// Oldest at the top, newest at the bottom, so the list reads like a story. While you are at the
// bottom it follows new lines; scroll up to read back and it stays put (a button jumps to the newest).
const TYPE_NAMES = {
  survey: "Reading", audit: "Checking", probe: "Playing", hypothesize: "Guessing rules", score: "Scoring",
  rebuild: "Rebuilding", verify: "Verifying", repair: "Repairing",
};
const NEAR_BOTTOM_PX = 40;

export class ActionLog {
  constructor(title = "What the agent is doing") {
    this.items = new Map();          // loop number -> action, so a reconnect never shows a line twice
    this.filter = "all";
    this.run = null;
    this.unseen = 0;

    this.node = el("section", "actions-box");
    const head = el("div", "actions-head");
    this.state = el("span", "actions-state", "Agent not connected");
    head.append(el("h3", "", title), this.state);

    const tools = el("div", "actions-tools");
    this.count = el("span", "actions-count", "0 lines");
    this.select = el("select", "actions-filter");
    this.select.setAttribute("aria-label", "Show only one kind of step");
    this.select.append(new Option("Every step", "all"));
    this.select.addEventListener("change", () => {
      this.filter = this.select.value;
      this.render();
    });
    this.jump = el("button", "ghost", "Jump to newest");
    this.jump.type = "button";
    this.jump.addEventListener("click", () => this.toBottom());
    const top = el("button", "ghost", "Back to the first line");
    top.type = "button";
    top.addEventListener("click", () => {
      this.list.scrollTop = 0;
    });
    tools.append(this.count, this.select, top, this.jump);

    this.list = el("ol", "actions actions-scroll");
    this.list.tabIndex = 0;
    this.list.setAttribute("aria-label", "Every step the agent took, oldest first");
    this.empty = el("li", "column-empty", "Nothing yet. Each thing the agent does shows up here, one line at a time.");
    this.list.append(this.empty);
    this.list.addEventListener("scroll", () => {
      if (this.atBottom()) this.markSeen();
    });
    this.node.append(head, tools, this.list);
  }

  atBottom() {
    return this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight < NEAR_BOTTOM_PX;
  }

  toBottom() {
    this.list.scrollTop = this.list.scrollHeight;
    this.markSeen();
  }

  markSeen() {
    this.unseen = 0;
    this.jump.textContent = "Jump to newest";
    this.jump.classList.remove("has-new");
  }

  row(action) {
    const row = el("li", `action ${action.type}`);
    row.dataset.loop = String(action.loop);
    const proof = (action.trace_ids || []).length ? `trace ${action.trace_ids[0]}` : "";
    const text = el("span", "action-text", action.sentence);
    if (action.next) text.append(el("span", "action-next", action.next));
    row.append(el("span", "action-loop", `loop ${action.loop}`), text, el("span", "action-proof", proof));
    if (action.at) row.title = new Date(action.at).toLocaleTimeString();
    return row;
  }

  shown(action) {
    return this.filter === "all" || action.type === this.filter;
  }

  updateCount() {
    const total = this.items.size;
    const visible = [...this.items.values()].filter((a) => this.shown(a)).length;
    const lines = `${total} line${total === 1 ? "" : "s"}`;
    this.count.textContent = this.filter === "all" ? lines : `${visible} of ${lines}`;
  }

  addType(type) {
    if (!type || [...this.select.options].some((o) => o.value === type)) return;
    this.select.append(new Option(TYPE_NAMES[type] || type, type));
  }

  markNewest() {
    this.list.querySelectorAll(".action.newest").forEach((r) => r.classList.remove("newest"));
    const rows = this.list.querySelectorAll(".action");
    if (rows.length) rows[rows.length - 1].classList.add("newest");
  }

  push(action) {
    if (!action || !Number.isInteger(action.loop) || this.items.has(action.loop)) return;
    const follow = this.atBottom();
    this.items.set(action.loop, action);
    this.addType(action.type);
    this.updateCount();
    if (!this.shown(action)) return;
    this.empty.remove();
    const row = this.row(action);
    // lines normally arrive in order; if one is late, slot it in by loop number
    const later = [...this.list.querySelectorAll(".action")].find((r) => Number(r.dataset.loop) > action.loop);
    this.list.insertBefore(row, later || null);
    this.markNewest();
    if (follow) this.toBottom();
    else {
      this.unseen += 1;
      this.jump.textContent = `Jump to newest (${this.unseen} new)`;
      this.jump.classList.add("has-new");
    }
  }

  render() {
    const rows = [...this.items.values()].filter((a) => this.shown(a)).sort((a, b) => a.loop - b.loop);
    this.list.replaceChildren(...(rows.length ? rows.map((a) => this.row(a)) : [this.empty]));
    this.markNewest();
    this.updateCount();
    this.toBottom();
  }

  setState(connected, run) {
    if (connected && run && this.run && run !== this.run) {   // a different run: start a fresh list
      this.items.clear();
      this.render();
    }
    if (connected && run) this.run = run;
    this.state.textContent = connected ? `Watching run ${run}` : "Agent not connected";
    this.state.dataset.on = String(connected);
  }
}
