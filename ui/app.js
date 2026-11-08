// MCD console. One function per tab; each fetches what it needs and re-renders.
// No framework: `html` builds strings, `esc` escapes anything from the data.

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x) => (x == null ? "–" : (x * 100).toFixed(1) + "%");
const chip = (passed) => passed == null ? '<span class="chip na">n/a</span>'
  : passed ? '<span class="chip ok">✓ followed</span>' : '<span class="chip bad">✗ broken</span>';

async function api(path, method = "GET", body) {
  const res = await fetch(path, { method, headers: { "Content-Type": "application/json" },
                                  body: body ? JSON.stringify(body) : undefined });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
const attempt = async (fn) => { try { return await fn(); } catch (e) { alert(e.message); } };

// ---------------------------------------------------------------- tabs
const TABS = { pipeline, conversations, rulebook, run, results, transcripts };
const LABELS = { pipeline: "Pipeline", conversations: "Conversations", rulebook: "Rulebook",
                 run: "Run", results: "Results", transcripts: "Transcripts" };
let current = location.hash.slice(1) || "pipeline";
let pollTimer = null;

function show(tab, params = {}) {
  current = tab; location.hash = tab; clearInterval(pollTimer);
  $("#tabs").innerHTML = Object.keys(TABS).map((t) =>
    `<button data-tab="${t}" class="${t === tab ? "active" : ""}">${LABELS[t]}</button>`).join("");
  document.querySelectorAll("#tabs button").forEach((b) => b.onclick = () => show(b.dataset.tab));
  TABS[tab](params);
}

// ---------------------------------------------------------------- pipeline
async function pipeline() {
  const s = await api("/api/state");
  $("#view").innerHTML = `
    <div class="card"><h2>Pipeline state</h2>
      <div class="grid">
        ${[["Conversations", s.conversations], ["Variants", s.variants],
           ["Model runs", s.responses], ["Graded rows", s.grades]]
          .map(([k, v]) => `<div><div class="stat">${v}</div><div class="muted">${k}</div></div>`).join("")}
      </div>
      <p class="muted">Workspace: ${esc(s.workspace)} · Models run: ${esc(s.models.join(", ") || "none")}</p>
    </div>
    <div class="card"><h2>1 · Ingest the dataset</h2>
      <p class="muted">Reads the spreadsheet, checks both versions align, and seeds the rulebook if empty.</p>
      <div class="row"><label style="flex:1">Path to .xlsx
        <input id="xlsx" value="tests/assets/Lost in the Switch — Simple Conversations (1).xlsx"></label>
        <button class="btn" id="ingest">Ingest</button></div>
      <div id="ingest-out" class="muted"></div></div>
    <div class="card"><h2>2 · Remix into conditions</h2>
      <p class="muted">Builds the runnable conversations for each condition from the aligned English/Hindi turns.</p>
      <ul>${Object.entries(s.conditions).map(([k, v]) => `<li><b>${k}</b> — ${esc(v)}</li>`).join("")}</ul>
      <button class="btn secondary" id="remix">Rebuild variants</button> <span id="remix-out" class="muted"></span></div>
    <div class="card"><h2>3 · Edit rules → 4 · Run → 5 · Grade → 6 · Results</h2>
      <p class="muted">Use the Rulebook, Run and Results tabs.</p></div>`;
  $("#ingest").onclick = () => attempt(async () => {
    const r = await api("/api/ingest", "POST", { path: $("#xlsx").value });
    $("#ingest-out").textContent = `${r.conversations} conversations. Warnings: ${r.warnings.length ? r.warnings.join("; ") : "none"}`;
    pipeline();
  });
  $("#remix").onclick = () => attempt(async () => {
    const r = await api("/api/remix", "POST", {}); $("#remix-out").textContent = `${r.variants} variants`;
  });
}

// ---------------------------------------------------------------- conversations
async function conversations() {
  const convs = await api("/api/conversations");
  if (!convs.length) { $("#view").innerHTML = '<div class="card">No data yet. Ingest the dataset first.</div>'; return; }
  const rb = await api("/api/rulebook");
  const conds = Object.keys(convs[0].variants);
  $("#view").innerHTML = `
    <div class="card"><div class="row">
      <label>Conversation <select id="conv">${convs.map((c) => `<option>${esc(c.id)}</option>`).join("")}</select></label>
      <label>Left <select id="left">${conds.map((c) => `<option>${c}</option>`).join("")}</select></label>
      <label>Right <select id="right">${conds.map((c, i) => `<option ${c === "MIX" ? "selected" : ""}>${c}</option>`).join("")}</select></label>
    </div></div><div id="conv-body"></div>`;
  const draw = () => {
    const c = convs.find((x) => x.id === $("#conv").value);
    const side = (cond) => `<div><h3>${cond}</h3>${c.variants[cond].map((t) => `
      <div class="turn ${t.lang === "Hindi" ? "hi" : ""} ${t.distractor ? "distractor" : ""}">
        <div class="meta">Turn ${t.n} · ${t.lang}${t.distractor ? " · distractor" : ""}</div>${esc(t.text)}</div>`).join("")}</div>`;
    const rules = rb.rules.filter((r) => r.conv_id === c.id);
    $("#conv-body").innerHTML = `<div class="card"><h2>Rules for ${esc(c.id)}</h2>${rules.length ? rules.map((r) =>
      `<span class="chip tag">${esc(r.name)} · turns ${r.from_turn}–${r.to_turn ?? "end"}</span>`).join("") : "No rules"}</div>
      <div class="cols">${side($("#left").value)}${side($("#right").value)}</div>`;
  };
  ["conv", "left", "right"].forEach((id) => $("#" + id).onchange = draw);
  draw();
}

// ---------------------------------------------------------------- rulebook
async function rulebook() {
  const rb = await api("/api/rulebook");
  const convIds = [...new Set(rb.rules.map((r) => r.conv_id))];
  const filter = sessionStorage.getItem("rbFilter") || "";
  const rules = rb.rules.filter((r) => !filter || r.conv_id === filter);
  $("#view").innerHTML = `
    <div class="card"><div class="row">
      <div><b>Rulebook v${rb.version}</b> <span class="muted">· ${rb.rules.length} rules · updated ${esc(rb.updated_at)}</span>
        ${rb.stale.length ? `<div><span class="chip warn">${rb.stale.length} rule(s) edited since last grading — re-grade on the Run tab</span></div>` : ""}</div>
      <label>Conversation <select id="rb-filter"><option value="">All</option>
        ${convIds.map((c) => `<option ${c === filter ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></label>
      <button class="btn" id="add">+ New rule</button>
      <button class="btn secondary" id="reset">Reset to seed</button></div></div>
    <div class="card" style="overflow-x:auto"><table>
      <tr><th>Rule</th><th>Type</th><th>Turns</th><th>Last run (final turn)</th><th></th></tr>
      ${rules.map((r) => `<tr>
        <td><b>${esc(r.name)}</b>${r.enabled ? "" : ' <span class="chip na">disabled</span>'}
            ${rb.stale.includes(r.id) ? ' <span class="chip warn">stale</span>' : ""}
            ${r.language_scope === "english_only" ? ' <span class="chip tag">English-only</span>' : ""}
            <div class="muted">${esc(r.conv_id)} · ${esc(r.id)} · ${r.polarity === "must" ? "must" : "must not"}</div>
            <div class="muted">${esc(r.description)}</div></td>
        <td>${esc(r.check.type)}<div class="muted">${esc(r.check.question || JSON.stringify(
            Object.fromEntries(Object.entries(r.check).filter(([k]) => k !== "type"))))}</div></td>
        <td>${r.from_turn}–${r.to_turn ?? "end"}</td>
        <td>${Object.entries(rb.last_run[r.id] || {}).map(([k, v]) =>
            `<div>${esc(k.replace("|", " · "))} ${chip(v.passed)} <span class="muted">t${v.turn}</span></div>`).join("") || '<span class="muted">not graded</span>'}</td>
        <td><button class="btn secondary" data-edit="${esc(r.id)}">Edit</button></td></tr>`).join("")}
    </table></div>
    <dialog id="dlg"><form id="rule-form" method="dialog"></form></dialog>`;
  $("#rb-filter").onchange = (e) => { sessionStorage.setItem("rbFilter", e.target.value); rulebook(); };
  $("#add").onclick = () => editRule({ conv_id: filter || convIds[0], polarity: "must", from_turn: 1, to_turn: null,
    check: { type: "judge", question: "" }, enabled: true, name: "", description: "", id: "" }, true);
  $("#reset").onclick = () => confirm("Replace all rules with the shipped seed? Your edits will be lost.") &&
    attempt(async () => { await api("/api/rulebook/reset", "POST", {}); rulebook(); });
  document.querySelectorAll("[data-edit]").forEach((b) =>
    b.onclick = () => editRule(rb.rules.find((r) => r.id === b.dataset.edit), false));
}

const CHECK_TYPES = ["judge", "contains", "regex", "regex_count", "word_count", "sentence_count",
                     "paragraph_count", "max_words_per_paragraph", "lowercase_only", "ends_with", "reply_language"];

function editRule(rule, isNew) {
  const { type, ...params } = rule.check;
  $("#rule-form").innerHTML = `
    <h2>${isNew ? "New rule" : "Edit rule"}</h2>
    <div class="row"><label style="flex:1">Rule ID <input id="f-id" value="${esc(rule.id)}" ${isNew ? "" : "readonly"}></label>
      <label style="flex:1">Conversation <input id="f-conv" value="${esc(rule.conv_id)}"></label></div>
    <label>Name <input id="f-name" value="${esc(rule.name)}"></label>
    <label>Description <input id="f-desc" value="${esc(rule.description)}"></label>
    <div class="row">
      <label>Polarity <select id="f-pol"><option value="must" ${rule.polarity === "must" ? "selected" : ""}>must</option>
        <option value="must_not" ${rule.polarity === "must_not" ? "selected" : ""}>must not</option></select></label>
      <label>From turn <input id="f-from" type="number" min="1" value="${rule.from_turn}" style="width:80px"></label>
      <label>To turn (blank = end) <input id="f-to" type="number" min="1" value="${rule.to_turn ?? ""}" style="width:80px"></label>
      <label>Language scope <select id="f-scope"><option value="any" ${rule.language_scope !== "english_only" ? "selected" : ""}>any language</option>
        <option value="english_only" ${rule.language_scope === "english_only" ? "selected" : ""}>English-only</option></select></label>
      <label>Enabled <select id="f-en"><option value="1" ${rule.enabled ? "selected" : ""}>yes</option><option value="0" ${rule.enabled ? "" : "selected"}>no</option></select></label></div>
    <label>Check type <select id="f-type">${CHECK_TYPES.map((t) => `<option ${t === type ? "selected" : ""}>${t}</option>`).join("")}</select></label>
    <label>Check parameters (JSON) <textarea id="f-params">${esc(JSON.stringify(params, null, 1))}</textarea></label>
    <div class="muted"><b>English-only</b> rules (e.g. "no letter c") are not graded on Hindi replies, and the matching pair is dropped from the switch cost.
      Passing a check must mean the rule was <b>followed</b>. Judge: <code>{"question": "...", "context": "reply"|"full"}</code>.
      Examples: contains <code>{"terms": ["x"], "mode": "all"}</code> · regex <code>{"pattern": "...", "expect": false}</code> · word_count <code>{"min": 250}</code></div>
    <div class="row"><button class="btn" value="save" id="save" type="button">Save</button>
      <button class="btn secondary" type="button" id="cancel">Cancel</button>
      ${isNew ? "" : '<button class="btn danger" type="button" id="del">Delete</button>'}</div>`;
  const dlg = $("#dlg"); dlg.showModal();
  $("#cancel").onclick = () => dlg.close();
  if (!isNew) $("#del").onclick = () => confirm("Delete this rule?") && attempt(async () => {
    await api("/api/rulebook/rule?id=" + encodeURIComponent(rule.id), "DELETE"); dlg.close(); rulebook(); });
  $("#save").onclick = () => attempt(async () => {
    const to = $("#f-to").value;
    await api("/api/rulebook/rule", "PUT", {
      id: $("#f-id").value.trim(), conv_id: $("#f-conv").value.trim(), name: $("#f-name").value,
      description: $("#f-desc").value, polarity: $("#f-pol").value, from_turn: Number($("#f-from").value),
      to_turn: to === "" ? null : Number(to), enabled: $("#f-en").value === "1", language_scope: $("#f-scope").value, source: rule.source || "user",
      check: { type: $("#f-type").value, ...JSON.parse($("#f-params").value || "{}") } });
    dlg.close(); rulebook();
  });
}

// ---------------------------------------------------------------- run
async function run() {
  const [s, convs] = await Promise.all([api("/api/state"), api("/api/conversations")]);
  $("#view").innerHTML = `
    <div class="card"><h2>Run a model</h2>
      <p class="muted">Any OpenAI-compatible endpoint (Voyager API, vLLM). Use model name <code>mock</code> for a dry run with no network.
        The API key is sent to the local server for this call only and is never saved.</p>
      <div class="row">
        <label>Model <input id="r-model" value="mock"></label>
        <label style="flex:1">Base URL <input id="r-url" placeholder="https://…/v1 (or set MCD_BASE_URL)"></label>
        <label>API key <input id="r-key" type="password" placeholder="(or set MCD_API_KEY)"></label>
        <label>Max tokens <input id="r-max" type="number" value="4096" style="width:90px"></label>
        <label style="flex:1">Extra request fields (JSON, optional) <input id="r-extra" placeholder='{"chat_template_kwargs": {"enable_thinking": false}}'></label>
        <label>Parallel calls <input id="r-workers" type="number" min="1" max="32" value="4" style="width:90px"></label></div>
      <h3>Conditions</h3>
      <div class="row">${Object.entries(s.conditions).map(([k, v]) =>
        `<label style="flex-direction:row;align-items:center;gap:6px"><input type="checkbox" class="cond" value="${k}" ${["EN", "MIX", "MIX_CLEAN"].includes(k) ? "checked" : ""}> ${k} <span class="muted">${esc(v)}</span></label>`).join("")}</div>
      <h3>Conversations</h3>
      <div class="row"><label style="flex-direction:row;align-items:center;gap:6px"><input type="checkbox" id="all-convs" checked> All ${convs.length}</label>
        <select id="r-convs" multiple size="5" disabled>${convs.map((c) => `<option>${esc(c.id)}</option>`).join("")}</select></div>
      <p><button class="btn" id="start-run">Start run</button> <span class="muted">Resumable: finished conversations are skipped.</span></p></div>
    <div class="card"><h2>Grade the replies</h2>
      <p class="muted">Applies the current rulebook to every reply. Code checks are free; judge rules call the judge model.
        Re-run this after editing rules — only changed judge questions are re-asked.</p>
      <div class="row"><label>Judge model <input id="g-model" value="mock"></label>
        <label style="flex:1">Base URL <input id="g-url" placeholder="same as above if blank"></label>
        <label>API key <input id="g-key" type="password"></label>
        <button class="btn" id="start-grade">Grade</button></div></div>
    <div class="card"><h2>Job</h2><div id="job">idle</div>
      <p><button class="btn secondary" id="stop">Stop</button></p></div>`;
  $("#all-convs").onchange = (e) => $("#r-convs").disabled = e.target.checked;
  $("#start-run").onclick = () => attempt(async () => {
    await api("/api/run", "POST", {
      model: $("#r-model").value, base_url: $("#r-url").value || null, api_key: $("#r-key").value || null,
      max_tokens: $("#r-max").value, workers: $("#r-workers").value, extra_body: $("#r-extra").value || null,
      conditions: [...document.querySelectorAll(".cond:checked")].map((c) => c.value),
      conv_ids: $("#all-convs").checked ? null : [...$("#r-convs").selectedOptions].map((o) => o.value) });
    poll();
  });
  $("#start-grade").onclick = () => attempt(async () => {
    await api("/api/grade", "POST", { judge_model: $("#g-model").value,
      base_url: $("#g-url").value || $("#r-url").value || null, api_key: $("#g-key").value || $("#r-key").value || null, workers: $("#r-workers").value, extra_body: $("#r-extra").value || null });
    poll();
  });
  $("#stop").onclick = () => api("/api/job/stop", "POST", {});
  poll();
}

function poll() {
  clearInterval(pollTimer);
  const tick = async () => {
    const j = await api("/api/job");
    const el = $("#job"); if (!el) return clearInterval(pollTimer);
    const share = j.total ? (100 * j.done) / j.total : 0;
    el.innerHTML = j.state === "idle" ? "idle" : `
      <b>${esc(j.kind)}</b> · ${esc(j.state)} · ${j.done}/${j.total}${j.failed ? ` · <span class="error">${j.failed} FAILED (not saved)</span>` : ""} · ${esc(j.message)}
      <div class="bar"><div style="width:${share}%"></div></div>
      ${j.error_count ? `<div class="error">${j.error_count} error(s): ${esc(j.errors.join(" | "))}</div>` : ""}`;
    if (j.state !== "running") clearInterval(pollTimer);
  };
  tick(); pollTimer = setInterval(tick, 1000);
}

// ---------------------------------------------------------------- results
async function results() {
  const group = sessionStorage.getItem("group") || "all";
  const [{ summary, grid, headroom, quality }, rb] = await Promise.all([api("/api/results?group=" + group), api("/api/rulebook")]);
  const models = Object.keys(summary);
  if (!models.length) { $("#view").innerHTML = '<div class="card">No graded results yet. Run a model, then grade.</div>'; return; }
  $("#banner").hidden = !models.includes("mock");
  $("#banner").textContent = "Showing MOCK model results. These are not evidence about any real model.";
  const cols = models.flatMap((m) => Object.keys(summary[m].conditions).map((c) => `${m}|${c}`));
  const byRule = Object.fromEntries(rb.rules.map((r) => [r.id, r]));
  const convOf = (id) => byRule[id]?.conv_id || "";

  const picker = `<div class="card"><label>Conversations <select id="group">
    ${[["all", "All"], ["S", "Original dataset (LIS-S, 20)"], ["X", "Curated matched pairs (LIS-X, 20)"]].map(([v, t]) =>
      `<option value="${v}" ${v === group ? "selected" : ""}>${t}</option>`).join("")}</select></label></div>`;
  const labels = { headroom: ["ok", "usable: room to see forgetting"], ceiling: ["warn", "ceiling: almost always follows the rule in English, a drop cannot show"],
                   floor: ["bad", "floor: rarely follows the rule even in English"] };
  const headroomCard = `<div class="card"><h2>English baseline headroom</h2>
    <p class="muted">Pass rate on the all-English runs only (independent of any switch effect). Use it to decide which models and groups can show forgetting.</p>
    <table><tr><th>Model</th><th>Group</th><th>Rules</th><th>English pass</th><th>Verdict</th></tr>
    ${Object.entries(headroom || {}).flatMap(([m, groups]) => Object.entries(groups).map(([g, h]) => `<tr><td>${esc(m)}</td>
      <td>${g === "X" ? "Curated pairs (X)" : "Original (S)"}</td><td>${h.rules}</td><td>${pct(h.pass)}</td>
      <td><span class="chip ${labels[h.status][0]}">${labels[h.status][1]}</span></td></tr>`)).join("") || '<tr><td colspan="5" class="muted">Run and grade the EN condition first.</td></tr>'}
    </table></div>`;
  const verdicts = { ok: ["ok", "OK"], warning: ["warn", "Warning"], do_not_interpret: ["bad", "Do not interpret"] };
  const qrows = Object.entries(quality || {}).flatMap(([m, conds]) => Object.entries(conds).map(([c, q]) => ({ m, c, q })));
  const blocked = qrows.some((r) => r.q.verdict === "do_not_interpret");
  const qualityCard = `<div class="card" style="${blocked ? "border-color:var(--bad)" : ""}"><h2>Data quality gate</h2>
    <p class="muted">Share of rule checks that could not be graded for avoidable reasons (empty reply, cut-off reply, judge failure).
      Over 5% = warning; over 15% = results should not be interpreted. Fix the cause and re-run.</p>
    <table><tr><th>Model</th><th>Condition</th><th>Rules</th><th>Ungraded</th><th>Reasons</th><th>Verdict</th></tr>
    ${qrows.map(({ m, c, q }) => `<tr><td>${esc(m)}</td><td>${esc(c)}</td><td>${q.rules}</td><td>${pct(q.share)}</td>
      <td class="muted">${esc(Object.entries(q.reasons).map(([k, v]) => k + ": " + v).join(", ") || "none")}</td>
      <td><span class="chip ${verdicts[q.verdict][0]}">${verdicts[q.verdict][1]}</span></td></tr>`).join("")}</table></div>`;
  $("#view").innerHTML = picker + qualityCard + headroomCard + models.map((m) => {
    const costs = summary[m].switch_costs, conds = summary[m].conditions;
    return `<div class="card"><h2>${esc(m)}</h2>
      ${Object.keys(costs).length ? `<div class="grid">${Object.entries(costs).map(([c, sc]) => sc ? `<div>
        <div class="stat">${(sc.diff * 100).toFixed(1)} pts</div>
        <div class="muted">Switch cost EN − ${esc(c)} (final pass)<br>95% CI ${(sc.lo * 100).toFixed(1)} to ${(sc.hi * 100).toFixed(1)} · ${sc.pairs} rule pairs · ${sc.conversations} conversations</div>
        ${sc.too_few ? `<div><span class="chip warn">Only ${sc.conversations} conversations: the interval is not trustworthy. Treat this as a pipeline check, not a result.</span></div>` : ""}
        ${sc.dropped_pairs ? `<div class="muted">${sc.dropped_pairs} pair(s) dropped (ungradable). Sensitivity: ${(sc.if_dropped_pass * 100).toFixed(1)} to ${(sc.if_dropped_fail * 100).toFixed(1)} pts if counted as pass / fail.</div>` : ""}</div>` : "").join("")}</div>
        <div class="muted">Positive = the model keeps fewer rules than in English. If the interval includes 0, there is no clear effect. MIX_CLEAN keeps rule-introducing turns in English, so it is the cleaner memory test.</div>`
        : '<div class="muted">Switch cost needs an EN run plus at least one other condition.</div>'}
      <table style="margin-top:12px"><tr><th>Condition</th><th>Rules</th><th>Final pass</th><th>First-turn pass</th>
        <th>Forgetting</th><th>Must</th><th>Must not</th><th title="Replies that could not be graded: empty, cut off, or an English-only rule on a Hindi reply">Dropped</th><th>Hindi replies</th><th>Cut off</th></tr>
        ${Object.entries(conds).map(([c, s]) => `<tr><td><b>${c}</b></td><td>${s.rules}</td>
          <td><div class="bar"><div style="width:${(s.final_pass || 0) * 100}%"></div></div>${pct(s.final_pass)}</td>
          <td>${pct(s.first_pass)}</td><td>${pct(s.forgetting)} <span class="muted">of ${s.forgetting_n}</span></td>
          <td>${pct(s.must_final)}</td><td>${pct(s.must_not_final)}</td><td>${s.dropped_skipped + s.dropped_error}</td><td>${pct(s.hindi_reply_share)}</td><td>${pct(s.truncated)}</td></tr>`).join("")}
      </table></div>`;
  }).join("") + `
    <div class="card" style="overflow-x:auto"><h2>Per-rule results</h2>
      <p class="muted"><b>Final pass</b>: followed on the last turn the rule applies. Click a cell to read the transcript. Hover for every turn.</p>
      <table class="heat"><tr><th>Rule</th>${cols.map((c) => `<th>${esc(c.replace("|", " · "))}</th>`).join("")}</tr>
      ${Object.keys(grid).sort().map((id) => `<tr><td><b>${esc(byRule[id]?.name || id)}</b><div class="muted">${esc(id)}</div></td>
        ${cols.map((c) => {
          const cells = grid[id][c]; if (!cells) return '<td class="cell">–</td>';
          const last = cells[cells.length - 1], trail = cells.map((x) => `t${x.turn}:${x.passed == null ? "?" : x.passed ? "✓" : "✗"}`).join(" ");
          const [m, cond] = c.split("|");
          return `<td class="cell" title="${esc(trail)}" data-conv="${esc(convOf(id))}" data-cond="${esc(cond)}" data-model="${esc(m)}">${last.passed == null ? "?" : last.passed ? '<span class="chip ok">✓</span>' : '<span class="chip bad">✗</span>'}</td>`;
        }).join("")}</tr>`).join("")}</table></div>`;
  $("#group").onchange = (e) => { sessionStorage.setItem("group", e.target.value); results(); };
  document.querySelectorAll("td.cell[data-conv]").forEach((td) => td.onclick = () =>
    show("transcripts", { conv: td.dataset.conv, condition: td.dataset.cond, model: td.dataset.model }));
}

// ---------------------------------------------------------------- transcripts
async function transcripts(params = {}) {
  const [s, convs, rb] = await Promise.all([api("/api/state"), api("/api/conversations"), api("/api/rulebook")]);
  const conds = Object.keys(convs[0]?.variants || {});
  const pick = (id, options, value) => `<select id="${id}">${options.map((o) =>
    `<option ${o === value ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  $("#view").innerHTML = `<div class="card"><div class="row">
    <label>Conversation ${pick("t-conv", convs.map((c) => c.id), params.conv)}</label>
    <label>Condition ${pick("t-cond", conds, params.condition)}</label>
    <label>Model ${pick("t-model", s.models.length ? s.models : ["(none)"], params.model)}</label></div></div><div id="t-body"></div>`;
  const byRule = Object.fromEntries(rb.rules.map((r) => [r.id, r]));
  const draw = async () => {
    const t = await attempt(() => api(`/api/transcript?conv=${$("#t-conv").value}&condition=${$("#t-cond").value}&model=${encodeURIComponent($("#t-model").value)}`));
    if (!t) return;
    $("#t-body").innerHTML = t.turns.map((x) => `
      <div class="turn ${x.lang === "Hindi" ? "hi" : ""} ${x.distractor ? "distractor" : ""}">
        <div class="meta">Turn ${x.n} · ${x.lang}${x.distractor ? " · distractor (not graded)" : ""}</div>${esc(x.text)}
        ${x.reply == null ? "" : `<div class="reply"><div class="meta">Model reply${x.truncated ? " · CUT OFF by length limit" : ""}</div>${esc(x.reply)}</div>`}
        ${x.grades.length ? `<div style="margin-top:6px">${x.grades.map((g) =>
          `${chip(g.passed)} <span class="muted">${esc(byRule[g.rule_id]?.name || g.rule_id)}</span><br>`).join("")}</div>` : ""}</div>`).join("");
  };
  ["t-conv", "t-cond", "t-model"].forEach((id) => $("#" + id).onchange = draw);
  draw();
}

show(TABS[current] ? current : "pipeline");
