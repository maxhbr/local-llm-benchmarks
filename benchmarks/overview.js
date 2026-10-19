/* Cross-tool overview. Pure buildRows() is also usable from Node's test runner. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.BenchmarkOverview = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const GPUS = ["rtx5090", "gfx1151"];
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const num = value => finite(value) ? value : null;
  // Match the harness's filesystem slug, not a fuzzy model-name match. Keep
  // quantization, backend, case, and alias suffixes: each is a distinct setup.
  const slug = value => String(value || "").replaceAll("/", "_").replaceAll(":", "-")
    .replace(/[^a-zA-Z0-9._-]/g, "-");
  const apiName = value => String(value || "").replace(/^openai\//, "");
  const gpu = value => GPUS.find(g => new RegExp(`^${g}[-:]`, "i").test(apiName(value))) || "other";
  function timestamp(value) {
    if (!value) return -Infinity;
    const text = String(value);
    const directory = text.match(/^(\d{4})-?(\d{2})-?(\d{2})[-T](\d{2})-?(\d{2})-?(\d{2})/);
    const normalized = directory && !/^\d{4}-\d{2}-\d{2}T/.test(text) ?
      `${directory[1]}-${directory[2]}-${directory[3]}T${directory[4]}:${directory[5]}:${directory[6]}Z` :
      /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(text) ? text + "Z" : text;
    const parsed = Date.parse(normalized);
    return Number.isFinite(parsed) ? parsed : -Infinity;
  }
  function newer(candidate, current) {
    return !current || timestamp(candidate.when) > timestamp(current.when)
      || (timestamp(candidate.when) === timestamp(current.when) && candidate.tie > current.tie);
  }
  const array = value => Array.isArray(value) ? value : [];
  const record = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const records = value => array(value).filter(record);
  const cell = (value, tip, extra = {}) => ({ value: num(value), tip, ...extra });
  function safePath(...parts) {
    if (parts.some(p => !p || p === "." || p === ".." || /[/\\:\x00-\x1f]/.test(p))) return null;
    return parts.map(encodeURIComponent).join("/");
  }

  /** Union all source identities, then choose one latest record per tool.
   * Aider uses its last cumulative row; whole/diff stay selectable. Throughput
   * uses exactly the selected depth/concurrency, never the best across settings.
   * Missing/invalid measurements remain null. No input is mutated.
   */
  function buildRows(sources = {}, settings = {}) {
    const depth = settings.depth ?? "d0";
    const concurrency = String(settings.concurrency ?? 1);
    const aiderFormat = settings.aiderFormat ?? "any";
    const rows = new Map();
    function ensure(folder, ...names) {
      const key = slug(folder);
      if (!key || key === "." || key === "..") return null;
      if (!rows.has(key)) rows.set(key, { key, name: String(folder), gpu: "other", names: [], tools: {}, cells: {} });
      const row = rows.get(key);
      row.names = [...new Set([...row.names, ...names.filter(Boolean), String(folder)])];
      for (const name of [folder, ...names]) {
        const detected = gpu(name);
        if (row.gpu === "other" && detected !== "other") row.gpu = detected;
      }
      return row;
    }
    function choose(row, tool, candidate) {
      if (row && newer(candidate, row.tools[tool])) row.tools[tool] = candidate;
    }

    for (const d of records(sources.aider)) {
      const row = ensure(d.folder || apiName(d.model), d.model);
      const last = array(d.rows).reduce((best, r) =>
        !best || (num(r.test_cases) ?? -1) >= (num(best.test_cases) ?? -1) ? r : best, null);
      if (!row || !last) continue;
      const format = d.editFormat || last.edit_format || (d.baseName === "aider-diff" ? "diff" : "whole");
      if (aiderFormat !== "any" && format !== aiderFormat) continue;
      const when = last.dirname || last.date;
      const href = d.path ? safePath(...d.path.split("/")) : null;
      choose(row, "aider", { last, format, when, tie: String(d.path || ""), href });
    }
    for (const r of records(sources.llama)) {
      const row = ensure(r.model, r.model);
      if (!row || r.depth !== depth || String(r.concurrency) !== concurrency) continue;
      if (!["pp2048", "tg128"].includes(r.test)) continue;
      // The CSV already contains the latest artifact per folder. Do not pool
      // or maximize duplicates; its last matching record wins deterministically.
      row.tools.llama = row.tools.llama || { samples: {}, href: safePath(row.key, "llama-benchy.md") };
      row.tools.llama.samples[r.test] = r;
    }
    for (const r of records(sources.agent)) {
      // The runner emits empty scoreboard entries when it skips an existing
      // result. That is not a new measurement and must not erase its scores.
      if (r.schema === "empty") continue;
      const identity = r.folder || r.raw_key || (r.host ? `${r.host}:${r.model}` : r.model);
      if (!r.model && !r.raw_key && !r.folder) continue;
      const row = ensure(identity, r.model, r.run_name, r.host ? `${r.host}:${r.model}` : "");
      choose(row, "agent", { run: r, when: r.ts || r.run, tie: String(r.run || ""),
        href: typeof r.source_path === "string" ? safePath(...r.source_path.split("/"))
          : safePath("_agent-bench-scoreboards", r.run, "scoreboard.json") });
    }
    for (const r of records(sources.toolEval)) {
      const folder = r.folder || r.meta?.run_name || r.model;
      const row = ensure(folder, r.model, r.meta?.run_name);
      choose(row, "toolEval", { run: r, when: r.ts || r.run, tie: String(r.run || ""),
        href: safePath(slug(folder), "tool-eval-bench", r.run, "result.json") });
    }
    for (const r of records(sources.llmprobe)) {
      const folder = r.folder || r.meta?.run_name || r.model;
      const row = ensure(folder, r.model, r.meta?.run_name);
      choose(row, "llmprobe", { run: r, when: r.ts || r.run, tie: String(r.run || ""),
        href: safePath(slug(folder), "llmprobe", r.run, "result.json") });
    }

    for (const row of rows.values()) {
      const c = row.cells, tools = row.tools;
      if (tools.aider) {
        const { last: r, format, when, href } = tools.aider;
        const tip = `Aider ${format} · ${r.test_cases ?? "unknown"} cases · ${when || "date unknown"}`;
        c.aider = cell(r.pass_rate_2, tip, { href });
        c.aiderFirst = cell(r.pass_rate_1, tip, { href });
        c.aiderCases = cell(r.test_cases, tip, { href });
        c.aiderSeconds = cell(r.seconds_per_case, tip, { href });
        c.aiderFormat = { value: format, tip, href };
      }
      if (tools.llama) {
        for (const [test, r] of Object.entries(tools.llama.samples)) {
          const error = finite(r["t/s_err"]) ? ` ± ${r["t/s_err"]}` : "";
          c[test] = cell(r["t/s"], `llama-benchy ${test} · ${depth} · concurrency ${concurrency}${error} tok/s`,
            { href: tools.llama.href });
        }
      }
      if (tools.agent) {
        const { run: r, when, href } = tools.agent;
        const tip = `Agent-bench · ${when || "date unknown"} · ${r.runs_n ?? "unknown"} trials · ${r.error_count ?? 0} errors`;
        c.agent = cell(finite(r.final_success_rate) ? r.final_success_rate * 100 : null, tip, { href });
        c.agentJson = cell(finite(r.json_valid_rate) ? r.json_valid_rate * 100 : null, tip, { href });
      }
      if (tools.toolEval) {
        const { run: r, when, href } = tools.toolEval, p = r.report || {};
        const tip = `TOOL-EVAL · ${when || "date unknown"} · ${p.status || "status unknown"} · `
          + `${p.scenarios_run ?? "?"}/${array(p.scenarios).length || "?"} scenarios · `
          + `comp score = total points / 138 × 100 (fixed full-suite denominator)`;
        c.comp = cell(p.comparable_score, tip, { href, partial: finite(p.completion_rate) && p.completion_rate < 100 });
        c.completion = cell(p.completion_rate, tip, { href });
        c.toolLatency = cell(p.median_turn_ms, tip, { href });
      }
      if (tools.llmprobe) {
        const { run: r, when, href } = tools.llmprobe, p = r.report || {};
        for (const [key, name] of [["probeConformance", "conformance"], ["probeCapability", "capability"],
          ["probeAgentic", "agentic"], ["probeFidelity", "fidelity"]]) {
          const phase = p.run?.phases?.[name] || {}, card = p[name];
          const measured = ["measured", "partial", "interrupted"].includes(phase.status);
          const count = name === "capability" ? array(card?.categories).reduce((n, x) => n + (num(x.total) || 0), 0)
            : name === "fidelity" ? (array(card?.slices).some(x => x.measured) ? card?.items : 0) : card?.total;
          const partial = phase.status !== "measured" || !r.complete || !!p.incomplete;
          const tip = `llmprobe · ${when || "date unknown"} · ${r.depth || "unknown depth"} · ${r.status || "unknown status"}`
            + ` · ${name}: ${phase.status || "scope unknown"}${phase.reason ? " · " + phase.reason : ""}`;
          c[key] = cell(measured && count > 0 ? card?.pct : null, tip, { href, partial });
        }
      }
      row.toolCount = Object.keys(tools).length;
    }
    return [...rows.values()].sort((a, b) => a.name.localeCompare(b.name));
  }

  const COLUMNS = [
    { key: "name", label: "Model / output folder", group: "Model", text: true },
    { key: "gpu", label: "GPU", group: "Model", text: true },
    { key: "comp", label: "Comp score", group: "TOOL-EVAL", percent: true },
    { key: "completion", label: "Completion", group: "TOOL-EVAL", percent: true },
    { key: "toolLatency", label: "Median turn (ms)", group: "TOOL-EVAL", extra: true },
    { key: "pp2048", label: "pp2048 (tok/s)", group: "llama-benchy" },
    { key: "tg128", label: "tg128 (tok/s)", group: "llama-benchy" },
    { key: "aider", label: "Pass rate (final)", group: "Aider", percent: true },
    { key: "aiderCases", label: "Cases", group: "Aider", digits: 0 },
    { key: "aiderFormat", label: "Edit format", group: "Aider", text: true },
    { key: "aiderFirst", label: "First try", group: "Aider", percent: true, extra: true },
    { key: "aiderSeconds", label: "Seconds / case", group: "Aider", extra: true },
    { key: "agent", label: "Success", group: "Agent-bench", percent: true },
    { key: "agentJson", label: "Valid JSON", group: "Agent-bench", percent: true, extra: true },
    { key: "probeConformance", label: "Conformance", group: "llmprobe", percent: true },
    { key: "probeCapability", label: "Capability", group: "llmprobe", percent: true },
    { key: "probeAgentic", label: "Agentic", group: "llmprobe", percent: true, extra: true },
    { key: "probeFidelity", label: "Fidelity", group: "llmprobe", percent: true, extra: true },
  ];

  /** Mount once. Base data and manifests load independently, so a missing tool
   * never blanks the other sources. All global filtering is supplied by the
   * host page; buildRows remains independent of the DOM and network.
   */
  function create({ document, getBase, baseReady, loadJson, matches, dimmed, bitsFromName,
    onSourcesChanged = () => {} }) {
    const el = id => document.getElementById(id);
    const sources = { agent: [], toolEval: [], llmprobe: [] };
    const diagnostics = new Map();
    const jobs = [["agent", "_agent-bench-scoreboards/manifest.json"],
      ["toolEval", "_tool-eval-scoreboards/manifest.json"], ["llmprobe", "_llmprobe-scoreboards/manifest.json"]];
    let rows = [], started = false, loading = false, sort = { key: "name", direction: "asc" };
    const esc = value => String(value ?? "").replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const fmt = (value, digits = 1) => value.toLocaleString(undefined, { maximumFractionDigits: digits });
    const valueOf = (r, col) => col.key === "name" || col.key === "gpu" ? r[col.key] : r.cells[col.key]?.value;
    const enriched = () => rows.map(r => ({ ...r, bits: r.names.map(bitsFromName).find(v => v != null) ?? null }));
    function cellHtml(row, col) {
      if (col.key === "name") return `<span title="${esc(row.names.join("\n"))}">${esc(row.name)}</span>`;
      if (col.key === "gpu") return `<span class="pill">${esc(row.gpu)}</span>`;
      const c = row.cells[col.key];
      if (c?.value == null) return `<span class="overview-missing" title="${esc(c?.tip || "No result for the selected settings")}">—</span>`;
      let text = col.text ? esc(c.value) : fmt(c.value, col.digits ?? 1) + (col.percent ? "%" : "");
      if (c.partial) text += '<span class="overview-partial" aria-label="partial result">*</span>';
      return c.href ? `<a href="./${esc(c.href)}" target="_blank" rel="noopener" title="${esc(c.tip)}">${text}</a>`
        : `<span title="${esc(c.tip)}">${text}</span>`;
    }
    function render() {
      if (!started) return;
      const columns = COLUMNS.filter(c => !c.extra || el("overviewExtra").checked);
      const minimum = Number(el("overviewMinTools").value);
      const visible = enriched().filter(r => matches(r) && r.toolCount >= minimum);
      const col = COLUMNS.find(c => c.key === sort.key) || COLUMNS[0];
      visible.sort((a, b) => {
        const av = valueOf(a, col), bv = valueOf(b, col);
        // Missing is always last, even when sorting ascending. Zero is data.
        if (av == null || bv == null) return av == null ? (bv == null ? a.name.localeCompare(b.name) : 1) : -1;
        const compare = col.text ? String(av).localeCompare(String(bv)) : av - bv;
        return (sort.direction === "asc" ? compare : -compare) || a.name.localeCompare(b.name);
      });
      const groups = [];
      for (const c of columns) {
        const prev = groups[groups.length - 1];
        if (prev?.name === c.group) prev.count++;
        else groups.push({ name: c.group, count: 1 });
      }
      const head = `<thead><tr class="overview-groups">${groups.map(g =>
        `<th colspan="${g.count}" scope="colgroup">${esc(g.name)}</th>`).join("")}</tr><tr>`
        + columns.map(c => `<th scope="col"${c.key === sort.key ? ` aria-sort="${sort.direction === "asc" ? "ascending" : "descending"}" class="sort-${sort.direction}"` : ""}>`
          + `<button type="button" data-sort="${c.key}">${esc(c.label)}</button></th>`).join("") + "</tr></thead>";
      el("overviewTable").innerHTML = head + "<tbody>" + visible.map(r => `<tr class="${dimmed(r) ? "dim" : ""}">`
        + columns.map(c => `<td class="${c.text ? "" : "num"}${c.key === "name" ? " rowkey" : ""}">${cellHtml(r, c)}</td>`).join("")
        + "</tr>").join("") + "</tbody>";
      const matching = visible.filter(r => !dimmed(r));
      const kpis = [["Models", matching.length], ...[["toolEval", "TOOL-EVAL"], ["llama", "llama-benchy"],
        ["aider", "Aider"], ["agent", "Agent-bench"], ["llmprobe", "llmprobe"]]
        .map(([key, label]) => [label, matching.filter(r => r.tools[key]).length])];
      el("overviewKpis").innerHTML = kpis.map(([label, value]) => `<div class="kpi"><div class="label">${label}</div><div class="value">${value}</div></div>`).join("");
      el("overviewStatus").textContent = `${visible.length} / ${rows.length} models${loading ? " · loading sources…" : ""}`;
      el("overviewEmpty").textContent = visible.length ? "" : loading ? "Loading benchmark results…" : rows.length
        ? "No models match the current filters or selected tools."
        : "No benchmark results found. Run benchmarks.update.sh to generate the dashboard data.";
      el("overviewWarnings").hidden = diagnostics.size === 0;
      el("overviewWarnings").querySelector("pre").textContent = [...diagnostics.values()].join("\n");
    }
    function rebuild() {
      rows = buildRows({ ...getBase(), ...sources }, { depth: el("overviewDepth").value,
        concurrency: el("overviewConcurrency").value, aiderFormat: el("overviewAiderFormat").value });
      render();
    }
    function install(key, data) {
      if (!Array.isArray(data?.runs)) throw new Error("missing runs array");
      if (!data.runs.every(record)) throw new Error("invalid run entry");
      // Validate processing before replacing a healthy source with bad data.
      buildRows({ [key]: data.runs });
      sources[key] = data.runs;
      diagnostics.delete(key);
    }
    function updateManifest(url, data) {
      const job = jobs.find(([, path]) => path === url);
      if (!job) return;
      try { install(job[0], data); }
      catch (error) { diagnostics.set(job[0], `${url}: ${error.message || error}`); }
      if (started) rebuild();
      onSourcesChanged();
    }
    async function load(force = false) {
      if (loading || (started && !force)) return;
      started = true;
      loading = true;
      el("overviewReload").disabled = true;
      rebuild();
      await Promise.allSettled([
        Promise.resolve(baseReady).then(result => {
          if (result?.errors?.length) diagnostics.set("base", result.errors.join("\n"));
          rebuild();
        }).catch(error => { diagnostics.set("base", String(error)); rebuild(); }),
        ...jobs.map(async ([key, url]) => {
          try {
            const data = await loadJson(url, force);
            install(key, data);
          } catch (error) { diagnostics.set(key, `${url}: ${error.message || error}`); }
          rebuild();
          onSourcesChanged();
        }),
      ]);
      loading = false;
      el("overviewReload").disabled = false;
      rebuild();
    }
    el("overviewReload").addEventListener("click", () => load(true));
    el("overviewTable").addEventListener("click", event => {
      const button = event.target.closest("button[data-sort]");
      if (!button) return;
      const col = COLUMNS.find(c => c.key === button.dataset.sort);
      sort = { key: col.key, direction: sort.key === col.key ? (sort.direction === "asc" ? "desc" : "asc") : col.text ? "asc" : "desc" };
      render();
    });
    ["overviewDepth", "overviewConcurrency", "overviewAiderFormat", "overviewMinTools", "overviewExtra"]
      .forEach(id => el(id).addEventListener("change", rebuild));
    return { load, refresh: rebuild, updateManifest, gpus: () => rows.map(r => r.gpu) };
  }
  return { buildRows, create };
});
