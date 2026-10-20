// One worker's live tail: its state from `.agent/live.json` and the last lines of its
// `.agent/dispatches/<id>.out` stream. Embedded in each handoff note:
//   await dv.view("harness/obsidian/views/worker", { id: "001-01" })
//
// `.out` is one JSON line per SDK message (`src/agent_cli/dispatch/driver.py:_out_record`) and
// can reach hundreds of KB, so only its tail is read. It redraws on a timer only while the
// worker runs, and each run owns one timer, so a re-run or closing the note stops it.

const id = input && input.id;
const LINES = (input && input.lines) || 12;
const TAIL_BYTES = 64 * 1024;
const REDRAW_MS = 3000;
const root = dv.container;
const { DateTime } = dv.luxon;
const runId = (root.__agentWorkerRun = (root.__agentWorkerRun || 0) + 1);
for (const node of root.__agentWorkerNodes || []) node.remove();
const head = root.createEl("p");
const stateEl = head.createEl("strong");
const detailEl = head.createSpan();
const ul = root.createEl("ul", { cls: "agent-worker-tail" });
const empty = root.createEl("p");
root.__agentWorkerNodes = [head, ul, empty];
const rows = new Map();
let reading = false;
let timer = null;

function setText(el, text) {
  if (el.textContent !== text) el.setText(text);
}

async function tail(path) {
  // desktop: read only the end of the file; elsewhere fall back to the whole file
  try {
    const fs = require("fs");
    const full = app.vault.adapter.getFullPath(path);
    const size = fs.statSync(full).size;
    const fd = fs.openSync(full, "r");
    try {
      const len = Math.min(size, TAIL_BYTES);
      const buf = Buffer.alloc(len);
      fs.readSync(fd, buf, 0, len, size - len);
      return buf.toString("utf8");
    } finally {
      fs.closeSync(fd);
    }
  } catch (e) {
    try {
      return await app.vault.adapter.read(path);
    } catch (e2) {
      return "";
    }
  }
}

function describe(r) {
  if (r.type === "assistant") {
    const text = (r.text || "").replace(/\s+/g, " ").trim();
    const tools = (r.tools || []).join(", ");
    return ["assistant", [text.length > 160 ? text.slice(0, 157) + "…" : text, tools && `→ ${tools}`].filter(Boolean).join(" ")];
  }
  if (r.type === "user") {
    const errors = (r.errors || []).join("; ");
    return ["result", `${r.tool_results || 0} tool result(s)${errors ? ` · error: ${errors}` : ""}`];
  }
  if (r.type === "result") {
    const status = (r.structured_output && r.structured_output.status) || r.subtype;
    return ["return", `${status} · $${Number(r.total_cost_usd || 0).toFixed(2)} · ${r.num_turns} turns`];
  }
  return [r.type, r.subtype || ""];
}

async function draw() {
  if (root.__agentWorkerRun !== runId || root.isConnected === false) return false;
  if (reading) return true;
  reading = true;
  try {
    let worker = null;
    try {
      const snap = JSON.parse(await app.vault.adapter.read(".agent/live.json"));
      worker = (snap.workers || []).find((w) => w.id === id) || null;
    } catch (e) {
      // no snapshot yet: the stream alone still shows
    }
    const running = worker && (worker.state === "queued" || worker.state === "running");
    let detail = "";
    if (worker) {
      setText(stateEl, worker.state);
      if (running) {
        const since = worker.attempt_started || worker.started;
        detail = (
          ` · attempt ${worker.attempt} · ${since ? DateTime.fromISO(since).toRelative() : ""}` +
            (worker.last_tool ? ` · last tool ${worker.last_tool}` : "")
        );
      } else if (worker.ask) {
        detail = ` · ${worker.ask}`;
      }
    } else {
      setText(stateEl, "Not live: finished and acted on, or not started.");
    }
    setText(detailEl, detail);

    const text = await tail(`.agent/dispatches/${id}.out`);
    const records = [];
    for (const line of text.split("\n")) {
      try {
        records.push(JSON.parse(line));
      } catch (e) {
        // the tail's first line is usually cut mid-record
      }
    }
    const shown = records.slice(-LINES);
    if (root.__agentWorkerRun !== runId || root.isConnected === false) return false;
    const wanted = new Set();
    const occurrences = new Map();
    shown.forEach((r, index) => {
      const signature = JSON.stringify(r);
      const n = (occurrences.get(signature) || 0) + 1;
      occurrences.set(signature, n);
      const key = `${signature}:${n}`;
      wanted.add(key);
      let li = rows.get(key);
      if (!li) {
        const [kind, body] = describe(r);
        li = ul.createEl("li");
        li.createEl("code", { text: kind });
        li.appendText(` ${body}`);
        rows.set(key, li);
      }
      if (ul.children[index] !== li) ul.insertBefore(li, ul.children[index] || null);
    });
    for (const [key, li] of rows) {
      if (!wanted.has(key)) {
        li.remove();
        rows.delete(key);
      }
    }
    setText(empty, shown.length ? "" : "No stream on this machine (.agent/dispatches is local).");
    return running;
  } finally { reading = false; }
}

if (!id) {
  dv.paragraph("worker view: pass { id: \"NNN-<tag>-SS\" } (or a legacy \"NNN-SS\")");
} else if (await draw()) {
  timer = window.setInterval(async () => {
    if (!await draw()) window.clearInterval(timer);
  }, REDRAW_MS);
  dv.component.register(() => window.clearInterval(timer));
}
