"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// Exercise the real llmprobe module, not a separate copy of its metric mapping.
const html = fs.readFileSync(path.join(__dirname, "../benchmarks/index.html"), "utf8");
const start = html.indexOf("    window.LlmProbe = (function () {");
const end = html.indexOf("\n    window.ModelOverview =", start);
assert.ok(start > 0 && end > start);
const source = html.slice(start, end);

function run(id = "A", overrides = {}) {
  return {
    folder: `rtx5090-${id}-Q4`, model: `rtx5090:${id}-Q4`, host: "test-host",
    run: id, ts: "2026-10-19T12:00:00Z", path: `${id}/llmprobe/run`,
    meta: { run_name: `rtx5090-${id}-Q4` }, status: "conformance-failed",
    depth: "default", mode: "probe", complete: true, rankable: true,
    completion_reasons: [], artifacts: {}, returncode: 1,
    report: {
      run: { phases: Object.fromEntries(["coverage", "conformance", "capability", "agentic", "fidelity", "performance"]
        .map(name => [name, { status: "measured" }])) },
      durationMs: 2500, usage: { inputTokens: 100, outputTokens: 20 },
      coverage: { byTier: ["core", "extended", "frontier"].map(tier => ({ tier, pct: 50, supported: 1, total: 2, missing: [], unprobed: [] })), entries: [], credits: [] },
      conformance: { pct: 80, passed: 8, total: 10, results: [], bySurface: [] },
      capability: { pct: 75, verdict: "capable", categories: [{ category: "tools", passed: 3, total: 4, pct: 75 }], evals: [], weakCategories: [] },
      agentic: { pct: 60, passed: 3, total: 5, tasks: [] },
      fidelity: { pct: 90, items: 1, slices: [{ measured: true, score: 0.9, weight: 1, label: "correctness", detail: "one item" }], unmeasured: [], measurements: { items: [] } },
      bench: { decodeTokPerSec: { median: 40 }, prefillTokPerSec: { median: 800 }, ttftMs: { median: 100 } },
    },
    ...overrides,
  };
}

class Element {
  constructor(id) {
    this.id = id; this.value = ""; this.checked = false; this.disabled = false;
    this.textContent = ""; this.innerHTML = ""; this.hidden = false;
    this.listeners = new Map(); this.classes = new Set();
    this.classList = {
      contains: key => this.classes.has(key),
      remove: key => this.classes.delete(key),
      toggle: (key, enabled) => enabled ? this.classes.add(key) : this.classes.delete(key),
    };
  }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  emit(name) { return this.listeners.get(name)?.({ target: this }); }
  querySelector() { return this.pre ||= new Element("pre"); }
  getContext() { return { canvas: this }; }
}

function dashboard(records = [run()], options = {}) {
  const elements = new Map(), charts = [], requests = [], selectedTabs = [];
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element(id));
    return elements.get(id);
  };
  const state = { records, fail: false, gpu: null, bits: null, text: null, dim: null };
  const document = { getElementById: get };
  function setTab(id) {
    get("panel-llmp-results").classList.toggle("active", id === "llmp-results");
    get("panel-llmp-scatter").classList.toggle("active", id === "llmp-scatter");
  }
  class Chart {
    constructor(ctx, config) { this.ctx = ctx; this.config = config; this.data = config.data; this.destroyed = false; charts.push(this); }
    destroy() { assert.equal(this.destroyed, false, "destroy each chart only once"); this.destroyed = true; }
  }
  const context = {
    document, window: {}, Chart: options.noChart ? undefined : Chart,
    loadDashboardJson: async (url, force) => {
      requests.push({ url, force });
      if (state.fail) throw new Error("manifest.json: 404 Not Found");
      return { schema_version: 1, generated_at: "today", runs: state.records };
    },
    gpuFromModel: name => String(name).startsWith("rtx5090") ? "rtx5090" : String(name).startsWith("gfx1151") ? "gfx1151" : "other",
    gpuFromHost: () => "other", bitsFromName: name => Number(String(name).match(/Q(\d+)/)?.[1]) || null,
    gpuMatches: gpu => state.gpu == null || gpu === state.gpu,
    bitsMatch: bits => state.bits == null || bits === state.bits,
    textMatches: (...names) => state.text == null || names.some(name => String(name).includes(state.text)),
    dimmed: (_gpu, _bits, ...names) => state.dim != null && names.some(name => String(name).includes(state.dim)),
    dimC: (color, dim) => dim ? "dim" : color, colorFor: gpu => `color:${gpu}`, hlSuffix: n => ` · ${n} highlighted`,
    populateGpuFilter() {}, populateGpuHighlight() {},
    selectTab: id => { selectedTabs.push(id); setTab(id); return context.window.LlmProbe.onTabActivated(); },
  };
  vm.runInNewContext(source, context);
  return {
    get, charts, state, requests, selectedTabs, api: context.window.LlmProbe,
    async activate(id = "llmp-scatter") { setTab(id); await context.window.LlmProbe.onTabActivated(); },
    chart: () => charts.at(-1),
    points: () => charts.at(-1).data.datasets.flatMap(d => d.data),
    async reload() { await get("llmpSpReload").emit("click"); },
    setAxis(axis, value) { get(`llmpSp${axis}`).value = value; get(`llmpSp${axis}`).emit("change"); },
  };
}

test("the new tab is connected to llmprobe activation and exposes all numeric axes", async () => {
  assert.match(html, /data-tab="llmp-scatter">X–Y plot/);
  assert.match(html, /id\.startsWith\("llmp-"\).*LlmProbe\.onTabActivated/);
  const d = dashboard();
  await d.activate();
  assert.equal(d.chart().config.type, "scatter");
  assert.equal(d.get("llmpSpX").value, "duration");
  assert.equal(d.get("llmpSpY").value, "capability");
  for (const key of ["conformance", "capability", "agentic", "fidelity", "core", "extended", "frontier", "decodeTokPerSec", "prefillTokPerSec", "ttftMs", "input", "output", "tokens", "duration"]) {
    assert.ok(d.get("llmpSpX").innerHTML.includes(`value="${key}"`), key);
    assert.ok(d.get("llmpSpY").innerHTML.includes(`value="${key}"`), key);
  }
});

test("axis changes use native percentages, token counts, and duration in seconds", async () => {
  const d = dashboard();
  await d.activate();
  assert.equal(d.points()[0].x, 2.5);
  assert.equal(d.points()[0].y, 75);
  assert.equal(d.chart().config.options.scales.x.title.text, "duration s");
  assert.equal(d.chart().config.options.scales.y.title.text, "capability %");
  const previous = d.chart();
  d.setAxis("X", "agentic"); d.setAxis("Y", "tokens");
  assert.equal(previous.destroyed, true);
  assert.equal(d.points()[0].x, 60); assert.equal(d.points()[0].y, 120);
  d.setAxis("X", "decodeTokPerSec"); d.setAxis("Y", "ttftMs");
  assert.equal(d.points()[0].x, 40); assert.equal(d.points()[0].y, 100);
  assert.equal(d.requests.length, 1, "axis changes reuse the loaded manifest");
});

test("missing, unavailable, unknown, empty, and non-finite measurements are omitted, not zero", async () => {
  const records = [run("zero"), run("missing"), run("unavailable"), run("empty"), run("nan"), run("unknown")];
  records[0].report.agentic.pct = 0;
  delete records[1].report.agentic;
  records[2].report.run.phases.agentic.status = "not-run";
  records[3].report.agentic.total = 0;
  records[4].report.agentic.pct = NaN;
  delete records[5].report.run.phases.agentic;
  const d = dashboard(records); await d.activate(); d.setAxis("Y", "agentic");
  assert.equal(d.points().length, 1);
  assert.equal(d.points()[0].y, 0);
  assert.match(d.get("llmpSpStatus").textContent, /5 missing an axis measurement/);
  d.setAxis("Y", "fidelity");
  assert.equal(d.points().length, 6);
});

test("coverage respects its phase and keeps intentional partial coverage", async () => {
  const unknown = run("unknown"), partial = run("partial");
  delete unknown.report.run.phases.coverage;
  partial.report.run.phases.coverage = { status: "partial", reason: "one item not probed" };
  const d = dashboard([unknown, partial]); await d.activate(); d.setAxis("Y", "core");
  assert.equal(d.points().length, 1);
  assert.equal(d.points()[0].r.run, "partial");
  assert.equal(d.points()[0].y, 50);
  const labels = d.chart().config.options.plugins.tooltip.callbacks.label({ raw: d.points()[0] });
  assert.ok(labels.some(label => label.includes("one item not probed")));
});

test("unmeasured performance and null duration do not fabricate coordinates", async () => {
  const r = run(); r.report.bench.ttftMs = null;
  const d = dashboard([r]); await d.activate(); d.setAxis("X", "ttftMs");
  assert.equal(d.points().length, 0);
  assert.match(d.get("llmpSpMessage").textContent, /No runs have measurements for both axes/);
  r.report.durationMs = null; d.setAxis("X", "duration");
  assert.equal(d.points().length, 0);
});

test("global GPU, quantization and text filters apply, highlights fade rather than hide", async () => {
  const d = dashboard([run("A"), run("B", { model: "gfx1151:B-Q8", folder: "gfx1151-B-Q8", meta: { run_name: "gfx1151-B-Q8" } })]);
  await d.activate(); assert.equal(d.points().length, 2);
  d.state.dim = "B-Q8"; d.api.refresh();
  assert.equal(d.points().length, 2);
  assert.deepEqual(Array.from(d.chart().data.datasets.find(g => g.label === "gfx1151").backgroundColor), ["dim"]);
  d.state.gpu = "rtx5090"; d.api.refresh(); assert.equal(d.points().length, 1);
  d.state.gpu = null; d.state.bits = 8; d.api.refresh(); assert.equal(d.points()[0].r.run, "B");
  d.state.bits = null; d.state.text = "A-Q4"; d.api.refresh(); assert.equal(d.points()[0].r.run, "A");
});

test("comparable-scope filters stay synchronized and limited runs have triangle markers", async () => {
  const d = dashboard([run(), run("quick", { rankable: false, depth: "quick" })]);
  await d.activate();
  assert.deepEqual(Array.from(d.chart().data.datasets[0].pointStyle), ["circle", "triangle"]);
  d.get("llmpSpRankable").checked = true; d.get("llmpSpRankable").emit("change");
  assert.equal(d.get("llmpRankable").checked, true); assert.equal(d.points().length, 1);
  d.get("llmpRankable").checked = false; d.get("llmpRankable").emit("change");
  assert.equal(d.get("llmpSpRankable").checked, false); assert.equal(d.points().length, 2);
});

test("clicking nearby points uses nearest hit selection and opens the chosen run's details", async () => {
  const first = run("A"), second = run("B");
  second.report.durationMs = 2501;
  second.report.capability.pct = 75.01;
  const d = dashboard([first, second]); await d.activate();
  const interaction = d.chart().config.options.interaction;
  // Chart.js's default "point" mode returns all overlapping hits in data
  // order. Request nearest-center hits so points[0] is the clicked run.
  assert.equal(interaction.mode, "nearest");
  assert.equal(interaction.axis, "xy");
  assert.equal(interaction.intersect, true);
  const point = d.points()[1];
  const labels = d.chart().config.options.plugins.tooltip.callbacks.label({ raw: point });
  assert.ok(labels.includes("rtx5090-B-Q4"));
  assert.ok(labels.some(label => label.startsWith("duration s: ")));
  assert.ok(labels.some(label => label.includes("conformance-failed")));
  d.chart().config.options.onClick({}, [{ datasetIndex: 0, index: 1 }]);
  await Promise.resolve();
  assert.deepEqual(d.selectedTabs, ["llmp-results"]);
  assert.ok(d.get("llmpDetail").innerHTML.includes("rtx5090-B-Q4"));
  assert.ok(d.get("llmpTbl").innerHTML.includes('<tr class="" aria-selected="true"><td>2026-10-19T12:00:00Z</td><td><span title="rtx5090:B-Q4">'));
});

test("hidden canvases are not rendered; tab activation renders current filters", async () => {
  const d = dashboard(); await d.activate("llmp-results");
  assert.equal(d.charts.length, 0); d.api.refresh(); assert.equal(d.charts.length, 0);
  await d.activate(); assert.equal(d.charts.length, 1);
  await d.activate("llmp-results"); d.api.refresh(); assert.equal(d.charts.length, 1);
  d.state.gpu = "gfx1151"; await d.activate(); assert.equal(d.points().length, 0);
  assert.match(d.get("llmpSpMessage").textContent, /No runs match/);
});

test("reload replaces points, failure clears stale chart, and retry restores it", async () => {
  const d = dashboard(); await d.activate(); const original = d.chart();
  d.state.records = [run("new")]; await d.reload();
  assert.equal(original.destroyed, true); assert.equal(d.points()[0].r.run, "new");
  const current = d.chart(); d.state.fail = true; await d.reload();
  assert.equal(current.destroyed, true);
  assert.match(d.get("llmpSpMessage").textContent, /404 Not Found/);
  assert.equal(d.get("llmpSpReload").disabled, false);
  d.state.fail = false; await d.reload(); assert.equal(d.chart().destroyed, false);
});

test("empty histories and a missing chart library have actionable messages", async () => {
  const empty = dashboard([]); await empty.activate();
  assert.equal(empty.points().length, 0); assert.match(empty.get("llmpSpMessage").textContent, /No llmprobe reports/);
  const missing = dashboard([run()], { noChart: true }); await missing.activate();
  assert.equal(missing.charts.length, 0);
  assert.match(missing.get("llmpSpMessage").textContent, /Chart.js is unavailable/);
  assert.ok(missing.get("llmpTbl").innerHTML.includes("rtx5090-A-Q4"));
});
