"use strict";
/* =============== Data & knowledge: policies, database, knowledge graph, verifications ===============
   Everything here is read from the live API (/v1/data/*) with the signed-in customer's token, so it shows exactly the data the agents can see. */
const DS = { tab: "policies", table: "transactions", q: "", layers: { knowledge: true, operational: true }, focus: "", hl: null, graph: null, sel: null, ready: false };
const TCOL = { customer: "#38e1b0", account: "#6d8dff", card: "#c792ea", transaction: "#7f8ea8", merchant: "#ffbd4a", dispute: "#ff6b6b", transfer: "#4fd1c5", issue: "#ff9f43", check: "#9aa7ff", policy: "#3ddc97", regulation: "#f4d35e", action: "#ff7ab6", kb: "#7fd6ff" };
const dtok = () => S.tokens[S.cur?.client_id];
const dget = p => req(p, {}, dtok());
const ER = { customers: [450, 26], accounts: [70, 118], cards: [190, 118], transfers: [310, 118], disputes: [430, 118], fraud_alerts: [550, 118], fee_waivers: [670, 118], verifications: [790, 118],
  tickets: [810, 26], transactions: [250, 214], merchants: [450, 214], policies: [590, 214], kg_nodes: [700, 214], kg_edges: [810, 214], service_components: [90, 214] };
const GROUPCOL = { bank: "#6d8dff", knowledge: "#3ddc97", reference: "#ffbd4a" };
const idLink = v => typeof v === "string" && /^(TXN-\d{8}|TRF-\d{8}|CARD-\d{7}|DSP-\d{6})$/.test(v);
const nodeKey = id => ({ TXN: "txn:", TRF: "trf:", CARD: "card:", DSP: "dsp:" })[id.split("-")[0]] + id;

function dataTabs() {
  const tabs = [["policies", "Policies"], ["database", "Database"], ["graph", "Knowledge graph"], ["verifs", "Verifications"]];
  $("#datatabs").innerHTML = tabs.map(([k, l]) => `<button data-k="${k}" class="${DS.tab === k ? "on" : ""}">${l}</button>`).join("");
}
async function renderData() {
  dataTabs(); const el = $("#databody");
  if (!dtok()) { el.innerHTML = `<p class="empty">Sign in as a customer first.</p>`; return; }
  el.innerHTML = `<p class="empty">Loading…</p>`;
  try { await ({ policies: renderPolicies, database: renderDatabase, graph: renderGraph, verifs: renderVerifs })[DS.tab](el); } catch (e) { el.innerHTML = `<p class="err">Could not load: ${esc(e.message)}</p>`; }
}

/* ---------- policies ---------- */
async function renderPolicies(el) {
  const { policies } = await dget("/v1/data/policies");
  el.innerHTML = `<p class="muted dnote">Each policy is a <b>row in the database</b> (table <code>policies</code>), linked to the regulation it implements and the help article that explains it. The verifier reads these rows at decision time, so changing a value changes what the agents are allowed to do.</p>
  <div class="cardg pols">${policies.map(p => `<div class="card pol"><div class="row"><b class="pid">${esc(p.id)}</b><span class="chip w">v${esc(p.version)}</span></div><h4>${esc(p.title)}</h4><p>${esc(p.rule)}</p>
    <div class="chips">${Object.entries(p.params).map(([k, v]) => `<span class="chip ok">${esc(k.replace(/_/g, " "))}: <b>${esc(Array.isArray(v) ? v.join(", ") : v)}</b></span>`).join("")}</div>
    <div class="meta"><span class="chip reg">⚖ ${esc(p.regulation)}</span>${p.kb_article ? `<span class="chip kbc">📖 ${esc(p.kb_article)}</span>` : ""}</div>
    ${p.governs.length ? `<div class="meta">governs: ${p.governs.map(g => `<a href="#" class="chip lnk" data-focus="issue:${esc(g)}">${esc(g.replace(/_/g, " "))}</a>`).join("")}</div>` : ""}
    ${p.gates_actions.length ? `<div class="meta">gates actions: ${p.gates_actions.map(g => `<span class="chip act">${esc(g)}</span>`).join("")}</div>` : ""}</div>`).join("")}</div>`;
  $$("[data-focus]", el).forEach(a => a.onclick = e => { e.preventDefault(); openGraph(a.dataset.focus); });
}

/* ---------- database ---------- */
async function renderDatabase(el) {
  const ov = await dget("/v1/data/overview"); DS.ov = ov; const by = Object.fromEntries(ov.tables.map(t => [t.name, t]));
  const edges = []; ov.tables.forEach(t => t.columns.forEach(c => { if (c.fk) { const tt = c.fk.split(".")[0]; if (by[tt] && tt !== t.name) edges.push([t.name, tt]); } }));
  edges.push(["policies", "kg_nodes"]);
  const box = t => { const [x, y] = ER[t.name] || [450, 300], sel = t.name === DS.table;
    return `<g class="erb ${sel ? "sel" : ""}" data-t="${t.name}" transform="translate(${x - 52},${y})"><rect width="104" height="46" rx="9" stroke="${GROUPCOL[t.group]}"/><text x="52" y="19" text-anchor="middle" class="ert" ${t.name.length > 14 ? 'style="font-size:9.5px"' : ""}>${t.name}</text><text x="52" y="36" text-anchor="middle" class="erc">${t.scoped ? t.rows + " of " + (t.bank_total >= 1000 ? (t.bank_total / 1000).toFixed(1) + "k" : t.bank_total) : t.rows.toLocaleString() + " rows"}</text></g>`; };
  const lines = edges.map(([a, b]) => { const [x1, y1] = ER[a] || [450, 300], [x2, y2] = ER[b] || [450, 300]; return `<line x1="${x1}" y1="${y1 + 23}" x2="${x2}" y2="${y2 + 23}" class="ere"/>`; }).join("");
  el.innerHTML = `<p class="muted dnote">The simulated bank: <b>${by.transactions.bank_total.toLocaleString()}</b> ledger rows across <b>${by.customers.bank_total}</b> customers. You see <b>your</b> rows (the same rows the agents can read); reference and knowledge tables are shared. Click a table. Internal columns (risk flag, fraud score) are never exposed.</p>
  <svg id="er" viewBox="0 0 900 290" class="er">${lines}${ov.tables.map(box).join("")}</svg>
  <div class="legend"><span><i style="background:${GROUPCOL.bank}"></i>bank data</span><span><i style="background:${GROUPCOL.reference}"></i>reference</span><span><i style="background:${GROUPCOL.knowledge}"></i>knowledge</span><span class="muted">lines = foreign keys</span></div>
  <div id="tdetail"></div>`;
  $$(".erb", el).forEach(g => g.onclick = () => { DS.table = g.dataset.t; DS.q = ""; $$(".erb", el).forEach(x => x.classList.toggle("sel", x === g)); renderTable(); });
  renderTable();
}
async function renderTable() {
  const el = $("#tdetail"), t = DS.ov.tables.find(x => x.name === DS.table); if (!t || !el) return;
  const d = await dget(`/v1/data/table/${t.name}?limit=25${DS.q ? "&q=" + encodeURIComponent(DS.q) : ""}`);
  const cell = (c, v) => v == null ? `<span class="muted">null</span>` : idLink(v) ? `<a href="#" class="idl" data-id="${esc(v)}">${esc(v)}</a>` : typeof v === "object" ? `<code>${esc(short(v, 60))}</code>` : esc(String(v).length > 70 ? String(v).slice(0, 70) + "…" : v);
  el.innerHTML = `<div class="tdh"><h3>${esc(t.name)} <span class="chip" style="border-color:${GROUPCOL[t.group]}">${t.group}</span></h3><p class="muted">${esc(t.description)}</p>
    <div class="chips">${t.columns.map(c => `<span class="chip ${c.pk ? "ok" : ""}" title="${esc(c.type)}">${esc(c.name)}${c.fk ? ` → <a href="#" class="fkl" data-t="${esc(c.fk.split(".")[0])}">${esc(c.fk)}</a>` : ""}<small> ${esc(c.type.toLowerCase())}</small></span>`).join("")}</div>
    <div class="row"><input id="tq" placeholder="filter rows…" value="${esc(DS.q)}"><span class="muted">${d.count} of ${t.rows.toLocaleString()} shown</span></div></div>
    <div class="tw"><table class="dt"><thead><tr>${d.columns.map(c => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${d.rows.map(r => `<tr>${d.columns.map(c => `<td>${cell(c, r[c])}</td>`).join("")}</tr>`).join("") || `<tr><td colspan="${d.columns.length}" class="muted">No rows.</td></tr>`}</tbody></table></div>`;
  $("#tq").onchange = e => { DS.q = e.target.value.trim(); renderTable(); };
  $$(".idl", el).forEach(a => a.onclick = e => { e.preventDefault(); openGraph(nodeKey(a.dataset.id)); });
  $$(".fkl", el).forEach(a => a.onclick = e => { e.preventDefault(); DS.table = a.dataset.t; DS.q = ""; $$(".erb").forEach(x => x.classList.toggle("sel", x.dataset.t === DS.table)); renderTable(); });
}

/* ---------- knowledge graph ---------- */
function openGraph(focus, hl) { DS.tab = "graph"; DS.focus = focus || ""; if (hl !== undefined) DS.hl = hl; view("data"); }
function hlSet(v) {  // node ids touched by a verification report
  const s = new Set(); if (!v) return s; const c = v.consulted || {};
  s.add("issue:" + v.issue_type); (v.checks || []).forEach(x => s.add("chk:" + x.check)); (c.policies || []).forEach(p => s.add("pol:" + p)); (c.regulations || []).forEach(r => s.add("reg:" + r));
  (c.knowledge_base || []).forEach(k => s.add("kb:" + k)); (v.allowed_actions || []).forEach(a => s.add("act:" + a)); if (v.primary_action) s.add("act:" + v.primary_action);
  const sid = v.subject_id || ""; if (sid) s.add(nodeKey(sid)); return s;
}
async function renderGraph(el) {
  const issues = ["duplicate_charge", "unrecognised_payment", "lost_stolen_card", "transfer_trace", "transfer_cancel", "fee_dispute", "declined_payment"];
  const lastV = DS.forceV || run?.verifs?.[run.verifs.length - 1]; const q = DS.focus ? `&focus=${encodeURIComponent(DS.focus)}&depth=2` : "";
  const g = await dget(`/v1/data/graph?txns=14${q}`); DS.graph = g;
  const hl = DS.hl === "last" ? hlSet(lastV) : new Set();
  el.innerHTML = `<p class="muted dnote">Two layers. The <b>knowledge layer</b> says which checks, policies, regulations and actions apply to each kind of issue; the verifier <b>reads it to decide what to check</b>. The <b>operational layer</b> is built live from your accounts, cards and ledger. Drag nodes, click one for details, or focus on an issue type.</p>
  <div class="gctl"><label>Focus <select id="gfocus"><option value="">everything</option>${issues.map(i => `<option value="issue:${i}" ${DS.focus === "issue:" + i ? "selected" : ""}>${i.replace(/_/g, " ")}</option>`).join("")}</select></label>
    <label><input type="checkbox" id="gk" ${DS.layers.knowledge ? "checked" : ""}> knowledge</label><label><input type="checkbox" id="go" ${DS.layers.operational ? "checked" : ""}> my data</label>
    <button class="btn alt" id="ghl" ${lastV ? "" : "disabled"}>${DS.hl === "last" ? "Clear highlight" : "Highlight what the last verification consulted"}</button>${DS.focus && !DS.focus.startsWith("issue:") ? `<span class="chip">focus: ${esc(DS.focus)}</span>` : ""}</div>
  <div class="gwrap"><svg id="kg" viewBox="0 0 940 560" class="kg"></svg><aside id="gdet" class="gdet"><p class="muted">Click a node.</p></aside></div>
  <div class="legend">${Object.entries(TCOL).map(([k, c]) => `<span><i style="background:${c}"></i>${k}</span>`).join("")}</div>`;
  $("#gfocus").onchange = e => { DS.focus = e.target.value; renderData(); }; $("#gk").onchange = e => { DS.layers.knowledge = e.target.checked; drawGraph(hl); }; $("#go").onchange = e => { DS.layers.operational = e.target.checked; drawGraph(hl); };
  $("#ghl").onclick = () => { DS.hl = DS.hl === "last" ? null : "last"; if (!DS.hl) DS.forceV = null; renderData(); };
  drawGraph(hl);
}
function layout(nodes, edges) {
  const W = 940, H = 560, byId = {}; nodes.forEach((n, i) => { const k = n.layer === "knowledge"; byId[n.id] = n; n.x = (k ? W * 0.3 : W * 0.75) + Math.cos(i * 2.399) * (40 + (i % 9) * 14); n.y = H / 2 + Math.sin(i * 2.399) * (40 + (i % 11) * 14); n.vx = n.vy = 0; n.deg = 0; });
  const E = edges.map(e => [byId[e.src], byId[e.dst], e.rel]).filter(e => e[0] && e[1]); E.forEach(([a, b]) => { a.deg++; b.deg++; });
  const gx = n => n.layer === "knowledge" ? W * 0.3 : W * 0.75, N = nodes.length, iters = N > 120 ? 160 : 240;
  for (let it = 0; it < iters; it++) {
    const cool = 1 - it / iters;
    for (let i = 0; i < N; i++) for (let j = i + 1; j < N; j++) { const a = nodes[i], b = nodes[j]; let dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy + 0.01; if (d2 > 40000) continue; const f = 2200 / d2, d = Math.sqrt(d2); dx /= d; dy /= d; a.vx += dx * f; a.vy += dy * f; b.vx -= dx * f; b.vy -= dy * f; }
    E.forEach(([a, b]) => { const dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) + 0.01, f = (d - 58) * 0.035; a.vx += dx / d * f; a.vy += dy / d * f; b.vx -= dx / d * f; b.vy -= dy / d * f; });
    nodes.forEach(n => { n.vx += (gx(n) - n.x) * 0.006; n.vy += (H / 2 - n.y) * 0.006; n.x += Math.max(-14, Math.min(14, n.vx)) * cool; n.y += Math.max(-14, Math.min(14, n.vy)) * cool; n.vx *= 0.55; n.vy *= 0.55; n.x = Math.max(24, Math.min(W - 24, n.x)); n.y = Math.max(20, Math.min(H - 20, n.y)); });
  }
  return E;
}
function drawGraph(hl) {
  const g = DS.graph, svg = $("#kg"); if (!g || !svg) return;
  const nodes = g.nodes.filter(n => DS.layers[n.layer]).map(n => ({ ...n })), ids = new Set(nodes.map(n => n.id)), edges = g.edges.filter(e => ids.has(e.src) && ids.has(e.dst));
  const E = layout(nodes, edges), on = hl && hl.size; const nb = id => new Set(E.filter(e => e[0].id === id || e[1].id === id).flatMap(e => [e[0].id, e[1].id]));
  const r = n => Math.min(15, 5 + Math.sqrt(n.deg) * 1.9) + (n.type === "customer" ? 4 : 0), lab = n => n.label.length > 26 ? n.label.slice(0, 25) + "…" : n.label;
  svg.innerHTML = `<g>${E.map(([a, b, rel]) => `<line class="ge ${on && hl.has(a.id) && hl.has(b.id) ? "hl" : ""}" data-a="${esc(a.id)}" data-b="${esc(b.id)}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"><title>${esc(a.label)} ${esc(rel)} ${esc(b.label)}</title></line>`).join("")}</g>
    <g>${nodes.map(n => `<g class="gn ${on ? (hl.has(n.id) ? "hl" : "dim") : ""}" data-id="${esc(n.id)}" transform="translate(${n.x},${n.y})"><circle r="${r(n)}" fill="${TCOL[n.type] || "#888"}"/>${n.deg > 5 || on && hl.has(n.id) || n.type === "issue" || n.type === "customer" ? `<text y="${-r(n) - 4}" text-anchor="middle">${esc(lab(n))}</text>` : ""}<title>${esc(n.type)}: ${esc(n.label)}</title></g>`).join("")}</g>`;
  $$(".gn", svg).forEach(gn => {
    const id = gn.dataset.id, n = nodes.find(x => x.id === id);
    gn.onmouseenter = () => { const s = nb(id); $$(".gn", svg).forEach(x => x.classList.toggle("dim", !s.has(x.dataset.id))); $$(".ge", svg).forEach(x => x.classList.toggle("hl", x.dataset.a === id || x.dataset.b === id)); };
    gn.onmouseleave = () => { $$(".gn", svg).forEach(x => { x.classList.remove("dim"); if (on) { x.classList.toggle("dim", !hl.has(x.dataset.id)); } }); $$(".ge", svg).forEach(x => x.classList.toggle("hl", !!(on && hl.has(x.dataset.a) && hl.has(x.dataset.b)))); };
    gn.onclick = () => { DS.sel = id; const rel = E.filter(e => e[0].id === id || e[1].id === id);
      $("#gdet").innerHTML = `<h4 style="color:${TCOL[n.type]}">${esc(n.type)}</h4><b>${esc(n.label)}</b><p class="muted"><code>${esc(id)}</code> · ${esc(n.layer)} layer</p>${Object.keys(n.props || {}).length ? `<pre>${esc(JSON.stringify(n.props, null, 1))}</pre>` : ""}
        <h4>Relations (${rel.length})</h4><ul>${rel.slice(0, 24).map(e => e[0].id === id ? `<li>${esc(e[2].toLowerCase().replace(/_/g, " "))} → <a href="#" data-go="${esc(e[1].id)}">${esc(e[1].label)}</a></li>` : `<li><a href="#" data-go="${esc(e[0].id)}">${esc(e[0].label)}</a> → ${esc(e[2].toLowerCase().replace(/_/g, " "))}</li>`).join("")}</ul>${n.type === "issue" ? `<button class="btn alt" data-focus="${esc(id)}">Focus on this issue</button>` : ""}`;
      $$("#gdet [data-go]").forEach(a => a.onclick = e => { e.preventDefault(); $(`.gn[data-id="${CSS.escape(a.dataset.go)}"]`, svg)?.dispatchEvent(new Event("click")); });
      $$("#gdet [data-focus]").forEach(b => b.onclick = () => { DS.focus = b.dataset.focus; renderData(); }); };
  });
  let dragN = null, moved = false;   // drag a node: update its position and the ends of its lines
  svg.onmousedown = e => { const gn = e.target.closest(".gn"); if (gn) { dragN = nodes.find(x => x.id === gn.dataset.id); moved = false; e.preventDefault(); } };
  svg.onmousemove = e => { if (!dragN) return; moved = true; const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY; const p = pt.matrixTransform(svg.getScreenCTM().inverse()); dragN.x = p.x; dragN.y = p.y;
    $(`.gn[data-id="${CSS.escape(dragN.id)}"]`, svg).setAttribute("transform", `translate(${p.x},${p.y})`);
    $$(".ge", svg).forEach(l => { if (l.dataset.a === dragN.id) { l.setAttribute("x1", p.x); l.setAttribute("y1", p.y); } if (l.dataset.b === dragN.id) { l.setAttribute("x2", p.x); l.setAttribute("y2", p.y); } }); };
  svg.onmouseup = svg.onmouseleave = () => { dragN = null; };
}

/* ---------- verifications ---------- */
async function renderVerifs(el) {
  const { verifications } = await dget("/v1/data/verifications?limit=12");
  const dc = { act: "ok", wait: "w", deny: "bad", no_action: "w", human: "warn" };
  el.innerHTML = `<p class="muted dnote">Every time an agent investigates, it writes a <b>verification</b>: which tables, policies, regulations, knowledge-graph paths and help articles it consulted, the checks it ran, and the decision. An action tool refuses to run without a matching verification. Run a scenario on the Console, then come back.</p>
  ${verifications.length ? verifications.map(v => { const r = v.report, c = r.consulted || {};
    return `<div class="card vf"><div class="row"><b>${esc(v.verification_id)}</b><span class="chip ${dc[v.decision] || ""}">${esc(v.decision)}</span><span class="chip">${esc(v.issue_type.replace(/_/g, " "))}</span><span class="muted">${esc(v.subject_id)} · ${esc(v.created_at)}</span><button class="ghost" data-hl="${esc(v.verification_id)}">Show in graph</button></div>
      <p>${esc(r.explanation || "")}</p>
      <table class="dt"><thead><tr><th>check</th><th>result</th><th>detail</th></tr></thead><tbody>${(r.checks || []).map(k => `<tr><td>${esc(k.check.replace(/_/g, " "))}</td><td><span class="chip ${k.result === "pass" ? "ok" : k.result === "flag" ? "warn" : k.result === "fail" ? "bad" : "w"}">${esc(k.result)}</span></td><td>${esc(k.detail)}</td></tr>`).join("")}</tbody></table>
      <div class="meta">tables: ${(c.database_tables || []).map(x => `<span class="chip">${esc(x)}</span>`).join("")}</div><div class="meta">policies: ${(c.policies || []).map(x => `<span class="chip ok">${esc(x)}</span>`).join("")}${(c.regulations || []).map(x => `<span class="chip reg">${esc(x)}</span>`).join("")}</div>
      <div class="meta">help articles: ${(c.knowledge_base || []).map(x => `<span class="chip kbc">${esc(x)}</span>`).join("")} · graph paths read: <b>${(c.knowledge_graph_paths || []).length}</b></div></div>`; }).join("") : `<p class="empty">No verifications yet. Try the “Charged twice” scenario.</p>`}`;
  $$("[data-hl]", el).forEach(b => b.onclick = () => { const v = verifications.find(x => x.verification_id === b.dataset.hl); DS.forceV = v.report; openGraph("issue:" + v.issue_type, "last"); });
}

function initData() {
  $("#datatabs").onclick = e => { const b = e.target.closest("button"); if (!b) return; DS.tab = b.dataset.k; renderData(); };
  dataTabs();
}

async function fillIntroNums() {
  try { const ov = await dget("/v1/data/overview"), t = Object.fromEntries(ov.tables.map(x => [x.name, x]));
    $("#intro-nums").innerHTML = `<div><b>${t.transactions.bank_total.toLocaleString()}</b>ledger transactions</div><div><b>${t.customers.bank_total}</b>simulated customers</div><div><b>${t.policies.rows}</b>policies as database rows</div><div><b>${t.kg_nodes.rows}</b>knowledge-graph nodes</div>`; } catch (e) { }
}
