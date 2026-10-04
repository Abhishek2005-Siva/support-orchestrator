"use strict";
const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
const API = (window.API_BASE || "").replace(/\/$/, ""), NS = "http://www.w3.org/2000/svg";
const esc = t => String(t ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const S = { accounts: [], cur: null, tokens: {}, threads: {}, history: {}, busy: false, polls: {}, qfilter: "pending", qitems: [], design: null };
let run = null;
const toast = (m, ms = 2600) => { const t = $("#toast"); t.textContent = m; t.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(() => t.hidden = true, ms); };
const fmt = ms => ms == null ? "" : ms < 1000 ? ms + " ms" : (ms / 1000).toFixed(2) + " s";

async function req(path, opts = {}, tok) {
  const r = await fetch(API + path, { ...opts, headers: { "content-type": "application/json", ...(tok ? { Authorization: "Bearer " + tok } : {}) } });
  if (!r.ok) { let d = r.statusText; try { const j = await r.json(); d = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {} const e = new Error(d); e.status = r.status; e.retry = r.headers.get("Retry-After"); throw e; }
  return r.json();
}
async function login(a) {
  if (S.tokens[a.client_id]) return S.tokens[a.client_id];
  const t = await req("/auth/token", { method: "POST", body: JSON.stringify({ client_id: a.client_id, client_secret: a.secret }) });
  S.tokens[a.client_id] = t.access_token; setTimeout(() => delete S.tokens[a.client_id], (t.expires_in - 60) * 1000); return t.access_token;
}
const staffAcct = () => S.accounts.find(a => a.role !== "customer");
const staffTok = async () => { const a = staffAcct(); return a ? login(a) : null; };

/* =============== agent network =============== */
const NODES = {
  orch: { l: "Orchestrator", i: "⚙", x: 410, y: 46, w: 170, h: 44, c: "orch" },
  guard: { l: "Input guard", i: "🛡", x: 78, y: 165 }, dispatcher: { l: "Dispatcher", i: "🧭", x: 198, y: 165 }, billing: { l: "Billing agent", i: "💳", x: 336, y: 165 },
  technical: { l: "Technical agent", i: "🛠", x: 476, y: 165 }, general: { l: "General agent", i: "📚", x: 614, y: 165 }, escalation: { l: "Escalation agent", i: "🚨", x: 746, y: 165 },
  validator: { l: "Validator", i: "✅", x: 410, y: 285 }, human: { l: "Human desk", i: "🧑‍💼", x: 746, y: 285 },
  invoices: { l: "Billing data", i: "🗄", x: 290, y: 390, t: 1 }, policy: { l: "Refund policy", i: "⚖", x: 386, y: 390, t: 1 }, logs: { l: "Error logs", i: "📈", x: 482, y: 390, t: 1 },
  status: { l: "Service status", i: "🟢", x: 578, y: 390, t: 1 }, kb: { l: "Knowledge base", i: "📖", x: 674, y: 390, t: 1 },
};
const EDGES = [["orch", "guard"], ["orch", "dispatcher"], ["orch", "billing"], ["orch", "technical"], ["orch", "general"], ["orch", "escalation"], ["billing", "validator"], ["technical", "validator"], ["general", "validator"],
  ["validator", "human"], ["escalation", "human"], ["billing", "invoices"], ["billing", "policy"], ["technical", "logs"], ["technical", "status"], ["general", "kb"], ["billing", "kb"], ["technical", "kb"]];
const TOOLNODE = { get_invoices: "invoices", get_payment_status: "invoices", get_customer_profile: "invoices", check_refund_eligibility: "policy", create_refund_request: "policy", get_service_status: "status",
  get_user_logs: "logs", run_diagnostic: "logs", search_knowledge_base: "kb", get_ticket_history: "human", assign_to_human: "human", create_ticket: "human" };
const TOOLAGENT = { get_invoices: "billing", get_payment_status: "billing", check_refund_eligibility: "billing", create_refund_request: "billing", get_service_status: "technical", get_user_logs: "technical", run_diagnostic: "technical",
  get_ticket_history: "escalation", assign_to_human: "escalation", create_ticket: "technical", get_customer_profile: "billing" };
const AGENT_IDS = ["guard", "dispatcher", "billing", "technical", "general", "escalation", "validator"];
const nodeEls = {}, edgeEls = {};
const dims = n => ({ w: n.w || (n.t ? 92 : 118), h: n.h || (n.t ? 34 : 46) });
const anchor = (id, side) => { const n = NODES[id], d = dims(n); return { x: n.x, y: n.y + (side === "out" ? d.h / 2 : -d.h / 2) }; };

function buildNet() {
  const svg = $("#net"); svg.innerHTML = "";
  EDGES.forEach(([a, b]) => {
    const p = document.createElementNS(NS, "path"); const A = NODES[a], B = NODES[b], da = dims(A), db = dims(B);
    const x1 = A.x, y1 = A.y + da.h / 2, x2 = B.x, y2 = B.y - db.h / 2, my = (y1 + y2) / 2;
    p.setAttribute("d", `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`); p.setAttribute("class", "edge"); svg.appendChild(p); edgeEls[a + ">" + b] = p;
  });
  Object.entries(NODES).forEach(([id, n]) => {
    const d = dims(n), g = document.createElementNS(NS, "g"); g.setAttribute("class", "node " + (n.c || "") + (n.t ? " tool" : "")); g.dataset.id = id;
    g.innerHTML = `<rect x="${n.x - d.w / 2}" y="${n.y - d.h / 2}" width="${d.w}" height="${d.h}" rx="11"/><text class="ic" x="${n.x - d.w / 2 + 14}" y="${n.y + 5}" style="text-anchor:middle">${n.i}</text>
      <text x="${n.x + 6}" y="${n.y - (n.t ? 1 : 2)}">${esc(n.l)}</text><text class="st" x="${n.x + 6}" y="${n.y + (n.t ? 11 : 13)}">idle</text>`;
    svg.appendChild(g); nodeEls[id] = g;
  });
}
function setNode(id, state, text) {
  const g = nodeEls[id]; if (!g) return; g.classList.remove("active", "think", "done", "blocked", "waiting"); if (state) g.classList.add(state);
  g.querySelector(".st").textContent = text || state || "idle"; if (state === "active") { g.classList.remove("pop"); void g.getBoundingClientRect(); g.classList.add("pop"); }
  if (run && AGENT_IDS.includes(id)) { const a = run.agents[id] ||= { state: "idle", ms: null, calls: 0 }; a.state = state || "idle"; renderAgents(); }
}
function flow(a, b, warn, keep = 1300) {
  const e = edgeEls[a + ">" + b]; if (!e) return; e.classList.add(warn ? "flowwarn" : "flow"); setTimeout(() => e.classList.remove("flow", "flowwarn"), keep);
  const A = anchor(a, "out"), B = anchor(b, "in"), c = document.createElementNS(NS, "circle"), m = document.createElementNS(NS, "animateMotion");
  c.setAttribute("r", 4.5); c.setAttribute("class", "packet" + (warn ? " w" : "")); m.setAttribute("dur", ".75s"); m.setAttribute("fill", "freeze"); m.setAttribute("path", `M${A.x},${A.y} L${B.x},${B.y}`);
  c.appendChild(m); $("#net").appendChild(c); m.beginElement?.(); setTimeout(() => c.remove(), 850);
}
function resetNet() { Object.keys(NODES).forEach(id => setNode(id, null, "idle")); Object.values(edgeEls).forEach(e => e.classList.remove("flow", "flowwarn")); }

/* =============== run model =============== */
function newRun(text, scen) {
  run = { t0: performance.now(), text, scen, events: [], tools: [], agents: {}, checks: {}, dispatch: null, result: null, trace: [], active: [], lastSpec: null, done: false, toolCount: 0 };
  resetNet(); $("#trace").innerHTML = ""; $("#tools").innerHTML = `<p class="empty">Waiting for tool calls…</p>`; $("#tr-total").textContent = ""; $("#tl-total").textContent = "";
  $("#explain").disabled = true; $("#esc").disabled = true; $("#esc-n").hidden = true; renderSec(); renderState(); renderAgents(); setNode("orch", "active", "routing");
  clearInterval(run.timer); run.timer = setInterval(() => { if (!run || run.done) return clearInterval(run?.timer); renderState(); }, 200);
}
function tline(t, icon, html, cls = "") {
  const el = $("#trace"); const row = document.createElement("div"); row.className = "tr " + cls; row.innerHTML = `<time>${t != null ? (t / 1000).toFixed(2) + "s" : ""}</time><span class="ic">${icon}</span><span>${html}</span>`;
  el.appendChild(row); el.scrollTop = el.scrollHeight; run.trace.push({ t, icon, text: row.textContent, cls }); $("#tr-total").textContent = run.trace.length + " events";
}
const short = (o, n = 70) => { let s = typeof o === "string" ? o : JSON.stringify(o); return s.length > n ? s.slice(0, n) + "…" : s; };
const argSummary = a => { if (!a) return ""; const x = a.args !== undefined ? a.args : a; if (typeof x === "string") return short(x, 60); return Object.entries(x || {}).map(([k, v]) => `${k}=${short(v, 28)}`).join(", "); };
function llmAgent(n) { const k = n.split(".")[1]; return { dispatcher: "dispatcher", billing: "billing", technical: "technical", general: "general", validator: "validator", escalation: "escalation", safety: "guard", merge: "orch" }[k]; }

function onTrace(ev) {
  if (!run) return; run.events.push(ev); const n = ev.name || "", t = ev.t;
  if (ev.phase === "start") {
    if (n === "guard.input") { setNode("guard", "active", "checking"); flow("orch", "guard"); tline(t, "▶", "Input guard started: sanitise, mask secrets, injection checks"); }
    else if (n === "guard.safety_model") { setNode("guard", "active", "safety model"); }
    else if (n.startsWith("agent.")) {
      const id = n.split(".")[1]; setNode(id, "active", "working"); if (id !== "validator") flow("orch", id); run.active.push(id);
      if (["billing", "technical", "general"].includes(id)) run.lastSpec = id;
      if (id === "validator") { run.active.filter(a => ["billing", "technical", "general"].includes(a)).forEach(a => flow(a, "validator")); if (!run.active.some(a => ["billing", "technical", "general"].includes(a))) flow("orch", "validator"); }
      tline(t, "▶", `<b>${esc(NODES[id]?.l || id)}</b> activated`);
    } else if (ev.kind === "tool") {
      const tn = n.slice(5), node = TOOLNODE[tn], ag = TOOLAGENT[tn] === "billing" || TOOLAGENT[tn] === "technical" || TOOLAGENT[tn] === "escalation" ? TOOLAGENT[tn] : (run.lastSpec || "general");
      const owner = tn === "search_knowledge_base" ? (run.lastSpec || "general") : ag;
      run.toolCount++; if (node && node !== "human") { setNode(node, "active", "called"); flow(owner, node, false, 900); } else if (node === "human") setNode("human", "active", "queueing");
      const rec = { id: ev.id, name: tn, args: ev.input?.args ?? ev.input, t, status: "running", node }; run.tools.push(rec); renderTools();
      tline(t, "→", `<code>${esc(tn)}</code>(${esc(argSummary(ev.input))})`); renderState();
    } else if (ev.kind === "llm") { const a = llmAgent(n); if (a && a !== "orch") { setNode(a, "think", "thinking…"); } }
  } else if (ev.phase === "end") {
    const o = ev.output || {};
    if (n === "guard.input") {
      const bad = o.action === "refuse", masked = (o.reasons || []).includes("secret_in_message");
      run.checks.guard = { bad, reasons: o.reasons || [], score: o.score, patterns: o.patterns || [], masked };
      setNode("guard", bad ? "blocked" : "done", bad ? "BLOCKED" : "passed " + fmt(ev.ms));
      tline(t, bad ? "⛔" : "✓", bad ? `Input guard <b>blocked</b> the message: ${esc((o.reasons || []).join(", "))}${o.score ? " (injection score " + o.score + ")" : ""}` : `Input guard passed${masked ? " · card/secret masked" : ""} · injection score ${o.score ?? 0}`, bad ? "bad" : "ok");
      if (bad) { setNode("orch", "done", "stopped"); } renderSec();
    } else if (n === "guard.safety_model") {
      run.checks.safety = { unsafe: !!o.unsafe, err: !!o.error }; if (!run.checks.guard?.bad) setNode("guard", o.unsafe ? "blocked" : "done", o.unsafe ? "UNSAFE" : "passed"); tline(t, o.unsafe ? "⚠" : "✓", `Content-safety model: ${o.unsafe ? "<b>unsafe</b>" : o.error ? "unavailable (skipped)" : "safe"} · ${fmt(ev.ms)}`, o.unsafe ? "warn" : "dim"); renderSec();
    } else if (n === "agent.dispatcher") {
      run.dispatch = o; setNode("dispatcher", "done", fmt(ev.ms));
      tline(t, "✓", `Intent detected: <b>${esc((o.intents || []).join(" + "))}</b> · urgency ${esc(o.urgency)} · sentiment ${esc(o.sentiment)} · confidence ${Math.round((o.confidence || 0) * 100)}%`, "ok");
      if ((o.forced_escalation_reasons || []).length) tline(t, "🔐", `Deterministic escalation triggers: <b>${esc(o.forced_escalation_reasons.join(", "))}</b>`, "pol");
      if (o.refund_requested) tline(t, "🔐", "Dispatcher flagged an explicit <b>refund request</b> (enables the gated write)", "pol");
      run.checks.dispatch = o; renderSec(); renderState();
    } else if (n.startsWith("agent.") && n !== "agent.validator") {
      const id = n.split(".")[1]; setNode(id, ev.status === "ok" ? "done" : "blocked", fmt(ev.ms));
      if (id === "escalation") { run.checks.escalation = o; setNode("human", "waiting", "queued"); flow("escalation", "human", true);
        tline(t, "🚨", `Escalation filed: <b>${esc(o.review_id || "")}</b> · priority <b>${esc(o.priority)}</b> · queue ${esc(o.queue)} · ETA ${esc(o.eta)}`, "warn"); }
      else tline(t, "✓", `${esc(NODES[id]?.l || id)} answered · confidence ${Math.round((o.confidence || 0) * 100)}% · ${o.tool_calls ?? 0} tool calls · ${o.iterations ?? 1} model steps${o.needs_human ? " · <b>needs human</b>" : ""}`, o.needs_human ? "warn" : "ok");
      const a = run.agents[id] ||= {}; a.ms = ev.ms; a.calls = o.tool_calls; renderAgents(); renderState();
    } else if (n === "agent.validator") {
      const v = o.verdict, issues = o.issues || []; run.checks.validator = o; const bad = v !== "approve";
      setNode("validator", v === "approve" ? "done" : v === "human_review" ? "waiting" : "blocked", `${v} ${fmt(ev.ms)}`);
      tline(t, v === "approve" ? "✓" : "⚠", `Validator: <b>${esc(v)}</b> · confidence ${Math.round((o.confidence || 0) * 100)}% · layer ${esc(o.layer)}${issues.length ? "<br><small>" + esc(issues.join(" · ")) + "</small>" : ""}`, bad ? "warn" : "ok");
      if (v === "human_review") { setNode("human", "waiting", "needed"); flow("validator", "human", true); } renderSec(); renderState();
    } else if (ev.kind === "tool") {
      const tn = n.slice(5), rec = run.tools.find(x => x.id === ev.id); const d = o.data;
      if (rec) { rec.status = o.blocked ? "blocked" : o.ok ? "ok" : "error"; rec.ms = ev.ms; rec.out = o; rec.err = o.error; }
      if (rec?.node && rec.node !== "human") setNode(rec.node, o.blocked ? "blocked" : "done", o.blocked ? "BLOCKED" : fmt(ev.ms));
      const sum = o.blocked ? `⛔ blocked: ${esc(o.error)}` : o.ok ? esc(toolSummary(tn, d)) : `✗ ${esc(o.error)}`;
      tline(t, o.blocked ? "⛔" : o.ok ? "✓" : "✗", `<code>${esc(tn)}</code> → ${sum} <span class="muted">${fmt(ev.ms)}</span>`, o.blocked ? "bad" : o.ok ? "ok" : "warn");
      if (tn === "check_refund_eligibility" && o.ok && d) { run.checks.policy = d; tline(t, "🔐", `Policy engine: <b>${d.eligible ? "REFUND_ELIGIBLE" : "REFUND_DENIED"}</b> (${esc(d.reason_code)}) · ${esc(d.refundable_usd || "")}${d.requires_approval ? " · <b>needs human approval</b>" : ""}`, "pol"); }
      if (tn === "create_refund_request") { run.checks.refundWrite = { ok: o.ok, blocked: o.blocked, err: o.error, d }; if (o.ok && d) tline(t, "🔐", `Refund <b>${esc(d.refund_id)}</b> filed: status <b>${esc(d.status)}</b>, ${esc(d.amount)}`, "pol"); }
      if (tn === "assign_to_human" && o.ok && d) { run.checks.assign = d; setNode("human", "waiting", d.priority); }
      if (o.blocked) run.checks.blockedTool = { tn, err: o.error }; renderTools(); renderSec();
    } else if (ev.kind === "llm") {
      const a = llmAgent(n); const nd = a && nodeEls[a]; if (nd && nd.classList.contains("think")) setNode(a, "active", "working");
      tline(t, "·", `model call ${fmt(ev.ms)} · ${((ev.usage?.prompt_tokens || 0) + (ev.usage?.completion_tokens || 0)).toLocaleString()} tokens · <small>${esc((ev.model || "").split("/").pop())}</small>`, "dim");
    }
  } else if (ev.phase === "event") {
    const d = ev.data || {};
    if (n.startsWith("security.")) { run.checks.events = [...(run.checks.events || []), n.slice(9)]; tline(t, "🛡", `Security event <b>${esc(n.slice(9))}</b>${d.detail ? ": " + esc(short(d.detail, 90)) : ""}`, "bad"); renderSec(); }
    else if (n === "graph.revise") tline(t, "↻", `Validator asked for a revision: <small>${esc(short(d.feedback, 120))}</small>`, "warn");
    else if (n === "cache.hit") tline(t, "⚡", "Served from the validated-answer cache (no model calls)", "ok");
    else if (["dispatcher.fallback", "escalation.note_fallback", "graph.fallback", "specialist.failed", "llm.model_unavailable", "validator.judge_unavailable"].includes(n)) tline(t, "⚠", `Failure handled: <b>${esc(n)}</b> ${esc(short(d.error || d.reason || "", 80))}`, "warn");
  }
}
function toolSummary(tn, d) {
  if (!d) return "ok"; if (tn === "get_invoices") return `${d.count} invoices (latest ${d.invoices?.[0]?.invoice_id} ${d.invoices?.[0]?.amount} ${d.invoices?.[0]?.status})`;
  if (tn === "search_knowledge_base") return d.no_relevant_article ? "no relevant article" : `${(d.results || []).length} articles (${d.match_quality || "strong"} match)`;
  if (tn === "get_service_status") return d.all_operational ? "all components operational" : "incident open: " + (d.components || []).filter(c => c.status !== "operational").map(c => c.component + " " + c.status).join(", ");
  if (tn === "get_user_logs") return d.total_events ? `${d.total_events} events (top: ${d.by_code?.[0]?.code})` : "no recent errors";
  if (tn === "check_refund_eligibility") return `${d.eligible ? "eligible" : "not eligible"} · ${d.reason_code}`; if (tn === "create_refund_request") return `${d.refund_id} ${d.status}`;
  if (tn === "assign_to_human") return `${d.review_id} ${d.priority}`; if (tn === "get_customer_profile") return `${d.plan} plan · ${d.tier}`; if (tn === "run_diagnostic") return `${d.check}: ${d.healthy ? "healthy" : "problem"}`;
  return short(d, 70);
}
function renderTools() {
  const el = $("#tools"); if (!run || !run.tools.length) return; $("#tl-total").textContent = run.tools.length + " calls";
  el.innerHTML = run.tools.map((r, i) => `<div class="tc" data-i="${i}"><div class="row"><b>${esc(r.name)}</b><span class="chip ${r.status === "ok" ? "ok" : r.status === "blocked" ? "bad" : r.status === "error" ? "warn" : "w"}">${esc(r.status)}</span><span class="ms">${r.ms != null ? fmt(r.ms) : "…"}</span></div>
    <div class="muted" style="font-size:11.5px">${esc(argSummary(r.args))}</div><pre>${esc(JSON.stringify({ args: r.args, result: r.out }, null, 2))}</pre></div>`).join("");
  $$(".tc", el).forEach(c => c.onclick = () => c.classList.toggle("open"));
}
const SEC_ROWS = [["g1", "Input sanitising & secret masking"], ["g2", "SQL-injection scan"], ["g3", "Prompt-injection check"], ["g4", "Content-safety model"], ["g5", "Topic control & escalation triggers"],
  ["g6", "Identity & tool allow-list"], ["g7", "Write gates (refund / ticket)"], ["g8", "Policy engine (refund rules)"], ["g9", "Output validation"], ["g10", "Human review"]];
function renderSec() {
  const c = run?.checks || {}, g = c.guard, rows = {};
  rows.g1 = !g ? ["wait", "waiting"] : g.masked ? ["info", "card / secret found and masked before any AI saw it"] : ["pass", "text cleaned; nothing sensitive found"];
  rows.g2 = !g ? ["wait", "waiting"] : g.reasons.includes("sql_injection") ? ["block", "SQL-injection pattern: " + g.patterns.slice(0, 2).join(", ")] : ["pass", "no SQL-injection pattern"];
  rows.g3 = !g ? ["wait", "waiting"] : g.reasons.includes("prompt_injection") ? ["block", `injection score ${g.score}: ${g.patterns.slice(0, 3).join(", ")}`] : ["pass", `score ${g.score ?? 0} (blocks at 0.8)`];
  rows.g4 = !c.safety ? (g ? ["wait", "running in parallel / skipped"] : ["wait", "waiting"]) : c.safety.unsafe ? ["warn", "unsafe content: escalated"] : c.safety.err ? ["info", "unavailable (fails open)"] : ["pass", "safe"];
  const d = c.dispatch; rows.g5 = !d ? (g?.bad ? ["info", "not reached"] : ["wait", "waiting"]) : (d.intents || []).includes("off_topic") ? ["block", "off-topic: polite refusal, no tools"] : (d.forced_escalation_reasons || []).length ? ["warn", "forced escalation: " + d.forced_escalation_reasons.join(", ")] : ["pass", "intent " + (d.intents || []).join(" + ")];
  const tools = run?.tools || [], bt = c.blockedTool; rows.g6 = bt ? ["block", `${bt.tn}: ${bt.err}`] : tools.length ? ["pass", `${tools.length} calls, all scoped to the signed-in customer`] : (g?.bad ? ["info", "no tools reached"] : ["wait", "waiting"]);
  const w = c.refundWrite; rows.g7 = w ? (w.blocked ? ["block", w.err] : w.ok ? ["pass", `refund filed through all gates (${w.d?.status})`] : ["warn", w.err || "not filed"]) : tools.some(t => t.name === "create_ticket") ? ["pass", "ticket only on request"] : tools.length || run?.result ? ["info", "no write attempted"] : ["wait", "waiting"];
  const p = c.policy; rows.g8 = p ? [p.eligible ? "pass" : "warn", `${p.eligible ? "eligible" : "denied"}: ${p.reason_code}${p.requires_approval ? ", over limit → human approval" : ""}`] : tools.length || run?.result ? ["info", "not needed for this request"] : ["wait", "waiting"];
  const v = c.validator; rows.g9 = v ? [v.verdict === "approve" ? "pass" : "warn", `${v.verdict} (${Math.round((v.confidence || 0) * 100)}%)${(v.issues || []).length ? ": " + v.issues.slice(0, 2).join(", ") : ""}`] : g?.bad ? ["info", "not needed"] : ["wait", "waiting"];
  const esc2 = c.escalation || c.assign || (run?.result?.status === "human_review"); rows.g10 = run?.result ? (esc2 ? ["warn", "escalated: " + (run.result.review_id || "")] : run.result.flags?.requires_human_approval ? ["warn", "refund awaiting approval"] : ["pass", "not needed"]) : ["wait", "waiting"];
  const ic = { pass: "✓", block: "⛔", info: "ℹ", wait: "○", warn: "⚠" };
  $("#sec").innerHTML = SEC_ROWS.map(([k, l]) => { const [s, t] = rows[k]; return `<div class="sc ${s}"><span class="ic">${ic[s]}</span><div><b>${l}</b><small>${esc(t)}</small></div></div>`; }).join("");
}
function risk() {
  const d = run?.dispatch || {}, r = run?.result; if (r?.status === "rejected") return ["BLOCKED", "attack stopped by the guardrails"];
  const rs = d.forced_escalation_reasons || []; if (rs.some(x => ["legal_threat", "data_breach_security", "threat_or_abuse", "fraud_or_chargeback"].includes(x)) || d.urgency === "critical") return ["HIGH", rs.join(", ") || "critical urgency"];
  if (r?.status === "human_review" || r?.flags?.requires_human_approval || d.urgency === "high" || ["angry", "negative"].includes(d.sentiment)) return ["MEDIUM", r?.flags?.requires_human_approval ? "money above the auto limit" : "frustrated customer / human needed"];
  return ["LOW", "routine request"];
}
function renderState() {
  const d = run?.dispatch, r = run?.result, rk = run ? risk() : ["–", ""], el = run ? (run.done ? run.final : performance.now() - run.t0) : null;
  const esc1 = r ? (r.status === "human_review" || r.flags?.requires_human_approval) : run?.checks?.escalation;
  const kp = (l, v, cls = "", wide) => `<div class="kpi ${wide ? "wide" : ""}"><small>${l}</small><b class="${cls}">${v}</b></div>`;
  $("#state").innerHTML = kp("Intent", d ? esc((d.intents || []).join(" + ")) : "–", "", 1) + kp("Confidence", d ? Math.round((d.confidence || 0) * 100) + "%" : "–") + kp("Urgency", d ? esc(d.urgency) : "–") + kp("Sentiment", d ? esc(d.sentiment) : "–") +
    kp("Agents", run ? Object.keys(run.agents).filter(a => run.agents[a].state !== "idle").length : "–") + kp("Tools", run ? run.toolCount : "–") + kp("Risk", rk[0], "risk-" + rk[0]) + kp("Human escalation", run ? (esc1 ? "YES" : r ? "NO" : "…") : "–", esc1 ? "risk-MEDIUM" : "") +
    kp("Validator", run?.checks?.validator ? esc(run.checks.validator.verdict) : "–") + kp("Latency", el != null ? fmt(Math.round(el)) : "–") + (rk[1] ? `<div class="kpi wide"><small>Why this risk</small><b style="font-weight:500;font-size:12.5px">${esc(rk[1])}</b></div>` : "");
}
function renderAgents() {
  $("#agents").innerHTML = AGENT_IDS.map(id => { const a = run?.agents[id] || {}, s = a.state || "idle"; return `<div class="ag ${s}"><i class="dot"></i>${esc(NODES[id].l)}<span>${s === "idle" ? "idle" : s}${a.ms != null ? " · " + fmt(a.ms) : ""}${a.calls ? " · " + a.calls + " tools" : ""}</span></div>`; }).join("");
}

/* =============== conversation =============== */
const thread = () => S.threads[S.cur.client_id] ||= [];
function renderThread() {
  const el = $("#thread"); el.innerHTML = ""; $("#who").textContent = S.cur ? "· " + S.cur.client_id : "";
  if (!thread().length) el.innerHTML = `<p class="empty">You are signed in as <b>${esc(S.cur?.client_id || "")}</b>.<br>Type any request: the agent works out the problem from your message and checks it against this account.<br><br><small>Test data in this account: <b>${esc(S.cur?.label || "")}</b>, ${esc(S.cur?.hint || "")}. Try a message that does <i>not</i> match it too.</small></p>`; else thread().forEach(m => el.appendChild(m.node)); el.scrollTop = 1e9;
}
function bubble(cls, html) { const d = document.createElement("div"); d.className = "msg " + cls; d.innerHTML = html; thread().push({ node: d }); $(".empty", $("#thread"))?.remove(); $("#thread").appendChild(d); $("#thread").scrollTop = 1e9; return d; }
async function useAccount(a) {
  S.cur = a; $("#acct").value = a.client_id; try { await login(a); } catch (e) { toast("Sign-in failed: " + e.message); return false; } renderThread(); return true;
}
async function stream(message, onEvent) {
  const tok = S.tokens[S.cur.client_id];
  const r = await fetch(API + "/v1/query/stream", { method: "POST", headers: { "content-type": "application/json", Authorization: "Bearer " + tok }, body: JSON.stringify({ message, channel: "web", history: S.history[S.cur.client_id] || [] }) });
  if (!r.ok) { const e = new Error(r.status === 422 ? "Message is empty or over 2000 characters." : r.statusText); e.status = r.status; e.retry = r.headers.get("Retry-After"); throw e; }
  const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "", result = null, err = null;
  for (;;) {
    const { done, value } = await rd.read(); if (done) break; buf += dec.decode(value, { stream: true });
    let i; while ((i = buf.search(/\r?\n\r?\n/)) >= 0) {
      const block = buf.slice(0, i); buf = buf.slice(i).replace(/^\r?\n\r?\n/, ""); let ev = "message", data = "";
      block.split(/\r?\n/).forEach(l => { if (l.startsWith("event:")) ev = l.slice(6).trim(); else if (l.startsWith("data:")) data += l.slice(5).trim(); });
      if (!data) continue; const j = JSON.parse(data); if (ev === "trace") onEvent(j); else if (ev === "result") result = j; else if (ev === "error") err = j;
    }
  }
  if (err) { const e = new Error(err.detail || "error"); e.status = err.status_code; throw e; } if (!result) throw new Error("No result received"); return result;
}
async function send(text, scen) {
  text = (text || "").trim(); if (!text || S.busy || !S.cur) return; S.busy = true; setBusy(true); $("#msg").value = ""; grow(); bubble("me", esc(text)); newRun(text, scen);
  const pend = bubble("bot", `<div class="dots"><span></span><span></span><span></span></div>`); const cid = S.cur.client_id;
  try {
    const res = await stream(text, onTrace); run.result = res; run.done = true; run.final = res.latency_ms; clearInterval(run.timer);
    const lab = { delivered: ["Answered", "ok", ""], human_review: ["Handed to a human", "warn", "hold"], rejected: ["Blocked by guardrails", "bad", "block"] }[res.status] || [res.status, "", ""];
    pend.className = "msg bot " + lab[2]; pend.innerHTML = `<span class="tag ${lab[1]}">${lab[0]}</span><br>${esc(res.reply || "(no reply)")}`;
    const h = (S.history[cid] ||= []); h.push({ role: "user", content: text }, { role: "assistant", content: res.reply || "" }); S.history[cid] = h.slice(-8);
    tline(res.latency_ms, res.status === "delivered" ? "🏁" : res.status === "rejected" ? "⛔" : "🧑‍💼", `Result: <b>${esc(lab[0])}</b> in ${fmt(res.latency_ms)}`, res.status === "delivered" ? "ok" : res.status === "rejected" ? "bad" : "warn");
    if (res.status === "rejected") setNode("orch", "done", "stopped"); else setNode("orch", "done", "complete");
    if (res.status === "human_review") setNode("human", "waiting", "awaiting human");
    run.checks.final = res; $("#explain").disabled = false; const needsHuman = res.status === "human_review" || res.flags?.requires_human_approval; $("#esc").disabled = !needsHuman; $("#esc-n").hidden = !needsHuman;
    renderState(); renderSec(); renderTools(); if (needsHuman) { watch(res, cid); toast("Escalated: open the Human escalation desk"); }
  } catch (e) {
    clearInterval(run?.timer); pend.className = "msg bot block"; pend.innerHTML = e.status === 429 ? `Too many requests. Try again in ${esc(e.retry || 60)} s.` : "Something went wrong: " + esc(e.message); if (run) setNode("orch", "blocked", "error");
  } finally { S.busy = false; setBusy(false); }
}
function setBusy(b) { $("#send").disabled = b; $$(".sbtn").forEach(x => x.disabled = b); }
function watch(res, cid) {
  const qid = res.query_id; if (S.polls[qid]) return; const orig = res.reply; let n = 0; const a = S.accounts.find(x => x.client_id === cid);
  S.polls[qid] = setInterval(async () => {
    try { const q = await req("/v1/query/" + qid, {}, await login(a)); n++;
      if ((res.status === "human_review" && q.status !== "human_review") || (res.status !== "human_review" && q.reply && q.reply !== orig)) {
        clearInterval(S.polls[qid]); const d = document.createElement("div"); d.className = "msg bot upd"; d.innerHTML = `<span class="tag ok">Update from our team</span><br>${esc(q.reply)}`;
        (S.threads[cid] ||= []).push({ node: d }); if (S.cur?.client_id === cid) { $("#thread").appendChild(d); $("#thread").scrollTop = 1e9; } toast("The customer's chat was updated"); }
    } catch {} if (n > 200) clearInterval(S.polls[qid]);
  }, 4000);
}
function grow() { const m = $("#msg"); m.style.height = "auto"; m.style.height = Math.min(m.scrollHeight, 90) + "px"; }

/* =============== scenarios =============== */
function buildScenarios() {
  $("#scen").innerHTML = SCENARIOS.map(s => `<button class="sbtn ${s.danger ? "danger" : ""}" data-id="${s.id}">${esc(s.label)}</button>`).join("");
  $$(".sbtn").forEach(b => b.onclick = () => runScenario(SCENARIOS.find(s => s.id === b.dataset.id), b));
}
async function runScenario(s, btn) {
  if (S.busy) return; $$(".sbtn").forEach(x => x.classList.toggle("sel", x === btn)); $("#scen-hint").textContent = s.hint;
  if (s.account !== "any") { const a = S.accounts.find(x => x.key === s.account); if (a && S.cur?.client_id !== a.client_id) { if (!(await useAccount(a))) return; } }
  await send(s.message, s);
}

/* =============== scheduler =============== */
const SCH = { on: false, ids: [] };
const waitIdle = async () => { while (S.busy) await sleep(300); };
async function schLoop() {
  SCH.on = true; $("#sched").classList.add("on"); $("#sched").textContent = "Scheduled ●";
  do {
    for (const id of SCH.ids) {
      if (!SCH.on) break; const s = SCENARIOS.find(x => x.id === id); await waitIdle(); await runScenario(s, $$(".sbtn").find(b => b.dataset.id === id)); await waitIdle();
      const nxt = SCH.ids[(SCH.ids.indexOf(id) + 1) % SCH.ids.length];
      if (!SCH.repeat && id === SCH.ids[SCH.ids.length - 1]) break;
      for (let t = SCH.every; t > 0 && SCH.on; t--) { $("#scen-hint").textContent = `Scheduled: next “${SCENARIOS.find(x => x.id === nxt).label}” in ${t}s (stop it from the Schedule button)`; await sleep(1000); }
    }
  } while (SCH.on && SCH.repeat);
  schStop();
}
function schStop() { SCH.on = false; $("#sched").classList.remove("on"); $("#sched").textContent = "Schedule ⏱"; if (!S.busy) $("#scen-hint").textContent = "Schedule stopped."; }
function initSchedule() {
  $("#sched").onclick = () => { $("#sch-list").innerHTML = SCENARIOS.map(s => `<label><input type="checkbox" value="${s.id}" ${SCH.ids.includes(s.id) ? "checked" : ""}>${esc(s.label)}</label>`).join(""); $("#dlg-sch").showModal(); };
  $("#sch-start").onclick = () => { const ids = $$("#sch-list input:checked").map(i => i.value); if (!ids.length) return toast("Pick at least one scenario"); SCH.ids = ids; SCH.every = +$("#sch-every").value; SCH.repeat = $("#sch-repeat").checked; $("#dlg-sch").close(); if (!SCH.on) schLoop(); else toast("Schedule updated"); };
  $("#sch-stop").onclick = () => { schStop(); $("#dlg-sch").close(); };
}

/* =============== explain =============== */
function explain() {
  if (!run?.result) return; const r = run.result, c = run.checks, d = run.dispatch, tools = run.tools, S_ = [];
  const sec = (t, items) => S_.push(`<h4>${t}</h4>` + (items.length > 1 ? `<ol>${items.map(i => `<li>${i}</li>`).join("")}</ol>` : `<p>${items[0] || ""}</p>`));
  sec("What was asked", [`The customer ${esc(S.cur?.label || "")} wrote: <i>“${esc(run.text)}”</i>`]);
  const g = c.guard; const gi = [];
  if (g) gi.push(g.bad ? `The <b>input guard</b> stopped it immediately (${esc(g.reasons.join(", "))}). This check is plain code, so no AI model ever saw the message and no tool was touched.` : `The <b>input guard</b> cleaned the text and checked it for injection (score ${g.score ?? 0}) and SQL patterns.${g.masked ? " A card number or secret was found and <b>masked</b> before any model could see it." : ""}`);
  if (c.safety) gi.push(`A separate content-safety model rated it <b>${c.safety.unsafe ? "unsafe" : "safe"}</b>.`);
  sec("Safety checks before any AI", gi);
  if (d) sec("What the system understood", [`The <b>dispatcher</b> classified the request as <b>${esc((d.intents || []).join(" + "))}</b> (urgency ${esc(d.urgency)}, sentiment ${esc(d.sentiment)}, confidence ${Math.round((d.confidence || 0) * 100)}%).${d.reasoning ? " Its reasoning: <i>" + esc(d.reasoning) + "</i>." : ""}${(d.forced_escalation_reasons || []).length ? `<br>Fixed rules (not the AI) added an escalation because of: <b>${esc(d.forced_escalation_reasons.join(", "))}</b>.` : ""}${d.refund_requested ? "<br>It also recognised an explicit <b>request for a refund</b>, which is what allows a refund to be filed." : ""}`]);
  if (tools.length) sec("What the agents did", tools.map(t => `<code>${esc(t.name)}</code>(${esc(argSummary(t.args))}) → ${t.status === "ok" ? esc(toolSummary(t.name, t.out?.data)) : "<b>" + esc(t.status) + "</b>: " + esc(t.err || "")} <small>(${fmt(t.ms)})</small>`));
  const pol = []; if (c.policy) pol.push(`The <b>refund policy engine</b> (code, not AI) decided: <b>${c.policy.eligible ? "eligible" : "not eligible"}</b> (${esc(c.policy.reason_code)}). ${esc(c.policy.explanation || "")}${c.policy.requires_approval ? ` Because the amount exceeds the auto-approval limit, a human must approve it.` : ""}`);
  if (c.refundWrite?.ok) pol.push(`The refund write passed every gate: the customer asked, the dispatcher agreed, eligibility was re-checked inside the tool, and it is idempotent and audited. Status: <b>${esc(c.refundWrite.d?.status)}</b>.`);
  if (c.refundWrite?.blocked) pol.push(`A refund write was <b>blocked</b>: ${esc(c.refundWrite.err)}.`);
  if (c.blockedTool && !c.refundWrite?.blocked) pol.push(`A tool call was <b>blocked</b>: ${esc(c.blockedTool.tn)}: ${esc(c.blockedTool.err)}.`);
  if (tools.length && !pol.length) pol.push("Every tool call was scoped to the signed-in customer; the AI cannot choose whose data to read.");
  if (pol.length) sec("Policy and permissions applied", pol);
  const v = c.validator; if (v) sec("Verification before replying", [`The <b>validator</b> re-checked every amount, date, id and claim in the draft against what the tools returned, then an independent AI judge checked it claim by claim. Verdict: <b>${esc(v.verdict)}</b> (${Math.round((v.confidence || 0) * 100)}%).${(v.issues || []).length ? " Issues: " + esc(v.issues.join(", ")) + "." : ""}`]);
  const why = r.status === "rejected" ? "The message was blocked by the guardrails, so the customer received a safe refusal." : r.status === "human_review" ? "A person has to take over: either fixed rules demanded it (legal, security, anger, repeat contact, explicit request) or the system could not answer safely. The customer got an honest holding message with a reference number." : r.flags?.requires_human_approval ? "The answer was delivered, but the refund is only <b>pending</b> until a human approves it; the reply says so." : "The reply passed all checks and was delivered.";
  sec("Outcome", [`<b>${esc(r.status)}</b> in ${fmt(r.latency_ms)}. ${why}`]);
  const no = []; if (!c.refundWrite && /refund|money back/i.test(run.text)) no.push("No refund was filed: " + (c.policy && !c.policy.eligible ? "the policy denied it." : "the message was a question, not a request, or it was not eligible."));
  if (!tools.some(t => t.name === "create_ticket")) no.push("No support ticket was opened (only done when the customer asks for one).");
  if (r.status !== "rejected") no.push("The AI never invented amounts or dates: anything not found in the tool results would have been revised or sent to a human.");
  if (no.length) sec("What it deliberately did not do", no);
  $("#explain-body").innerHTML = `<h2>Explain this execution</h2><div class="ex">${S_.join("")}</div>`; $("#dlg-explain").showModal();
}

/* =============== human escalation desk + queue =============== */
const isApproval = i => (i.reason || "").startsWith("refund above auto-approval");
function caseHtml(i) {
  const open = i.status === "pending", appr = isApproval(i);
  return `<h2><span class="pr ${i.priority}">${i.priority}</span>${esc(i.review_id)} <small class="muted">${esc(i.status)}${appr ? " · refund approval" : ""}</small></h2><p class="muted">${esc(i.reason)}</p>
  <h4>Customer wrote</h4><div class="box">${esc(i.customer_message)}</div><h4>AI summary for the specialist</h4><div class="box">${esc(i.summary)}</div>
  <h4>${appr ? "What the customer was told (refund pending)" : "Draft reply (NOT sent to the customer)"}</h4><div class="box">${esc(i.draft_reply || "No draft: please write a reply.")}</div>
  ${!appr && i.holding_reply ? `<h4>Holding message the customer already received</h4><div class="box">${esc(i.holding_reply)}</div>` : ""}
  ${open ? (appr ? `<div class="row"><button class="btn good" data-a="approve">Approve refund</button><button class="btn bad" data-a="reject">Reject refund</button></div>`
    : `<h4>Your reply</h4><textarea id="edit" rows="4">${esc(i.draft_reply || "")}</textarea><div class="row"><button class="btn good" data-a="approve" ${i.draft_reply ? "" : "disabled"}>Approve draft</button><button class="btn alt" data-a="edit">Edit &amp; send</button><button class="btn bad" data-a="reject">Reject</button></div>`)
    : `<p class="muted">Resolved by ${esc(i.reviewer || "-")}.</p>`}`;
}
function bindCase(root, i, done) {
  $$("[data-a]", root).forEach(b => b.onclick = async () => {
    const a = b.dataset.a, reply = $("#edit", root)?.value.trim(); if (a === "edit" && (!reply || reply.length < 3)) return toast("Write a reply first");
    try { await req(`/v1/review-queue/${i.review_id}/resolve`, { method: "POST", body: JSON.stringify({ action: a, ...(a === "edit" ? { reply } : {}) }) }, await staffTok()); toast("Resolved: " + a + ". The customer's chat updates shortly."); done?.(); }
    catch (e) { toast(e.status === 409 ? "Already resolved" : "Failed: " + e.message); done?.(); }
  });
}
async function openDesk() {
  const r = run?.result; if (!r) return; const id = r.review_id || r.flags?.approval_review_id; $("#drawer").hidden = false; const body = $("#dbody2");
  const tl = `<h4>What happened so far</h4><div class="box">${esc(run.trace.filter(x => !x.cls.includes("dim")).slice(-9).map(x => x.text).join("\n"))}</div>`;
  const tok = await staffTok(); if (!tok || !id) { body.innerHTML = `<p class="muted">The case was queued${id ? " as " + esc(id) : ""}. Sign in as staff to resolve it.</p>` + tl; return; }
  try { const item = await req("/v1/review-queue/" + id, {}, tok); body.innerHTML = `<p class="muted">This is what the human specialist sees.</p>` + caseHtml(item) + tl; bindCase(body, item, () => { $("#drawer").hidden = true; loadQueue(); }); }
  catch (e) { body.innerHTML = `<p class="err">Could not load the case: ${esc(e.message)}</p>`; }
}
async function loadQueue() {
  const tok = await staffTok(); if (!tok) return; try {
    const q = await req(`/v1/review-queue?status=${S.qfilter}&limit=100`, {}, tok); S.qitems = q.items; const p = q.items.filter(i => i.status === "pending").length; $("#qn").textContent = p; $("#qn").hidden = !p;
    $("#qlist").innerHTML = q.items.length ? q.items.map(i => `<button class="qi" data-id="${i.review_id}"><span class="pr ${i.priority}">${i.priority}</span><b>${esc(i.review_id)}</b><small>${esc(i.status)} · ${esc((i.customer_message || "").slice(0, 60))}</small></button>`).join("") : `<p class="muted">Nothing here.</p>`;
    $$("#qlist .qi").forEach(b => b.onclick = () => { $$("#qlist .qi").forEach(x => x.classList.toggle("sel", x === b)); const i = S.qitems.find(x => x.review_id === b.dataset.id); $("#qdetail").innerHTML = caseHtml(i); bindCase($("#qdetail"), i, () => { $("#qdetail").innerHTML = `<p class="empty">Resolved.</p>`; loadQueue(); }); });
  } catch (e) { toast("Could not load the queue: " + e.message); }
}

/* =============== scope & design pages =============== */
const ACT = { auto: "automatic", auto_policy: "automatic · policy engine decides", human_approval: "human approval", gated_write: "write only if asked", human: "human takes over", refuse: "refused" };
const tbl = (head, rows) => `<table><thead><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
const DTABS = [["scope", "Support scope"], ["flows", "Workflows"], ["tools", "Tools"], ["agents", "Agent boundaries"], ["orch", "Orchestration & state"], ["trust", "Trust & security"], ["fail", "Failure & escalation"], ["gaps", "Known gaps"]];
function designView(k) {
  const D = S.design; if (!D) return "<p class='muted'>Loading…</p>";
  if (k === "scope") return `<p class="muted">What the assistant is responsible for, and how much it may do alone.</p>` + tbl(["", "Area", "Task", "Example", "Handled by", "Autonomy", "Outcome"], D.tasks.map(t => [t.id, t.area, `<b>${esc(t.task)}</b>`, `<i>“${esc(t.example)}”</i>`, esc(t.handled_by), esc(ACT[t.autonomy]), esc(t.outcome)])) + `<h3 style="margin-top:18px">Out of scope</h3><ul>${D.out_of_scope.map(x => `<li>${esc(x)}</li>`).join("")}</ul>`;
  if (k === "flows") return D.workflows.map(w => `<div class="wf"><h3>${esc(w.id)} · ${esc(w.name)} <small class="muted">tasks ${esc(w.tasks.join(", "))}</small></h3>${w.steps.map(s => `<div class="st1"><span class="act ${s.actor}">${s.actor}</span><div>${esc(s.text)}${s.boundary ? `<span class="bd">⛨ trust boundary: ${esc(s.boundary)}</span>` : ""}</div></div>`).join("")}</div>`).join("");
  if (k === "tools") return tbl(["Tool", "Kind", "Agents", "Purpose", "Guards", "On failure"], D.tools.map(t => [`<code>${esc(t.name)}</code>`, `<span class="chip ${t.kind === "write" ? "warn" : "ok"}">${t.kind}</span>`, esc(t.agents), esc(t.purpose), esc(t.guards), esc(t.on_failure)])) + `<p class="muted">${esc(D.tools_common)}</p>`;
  if (k === "agents") return `<div class="cardg">${D.agents.map(a => `<div class="card"><h4>${esc(a.name)} <small class="muted">· ${esc(a.kind)}</small></h4><p><b>Can:</b> ${esc(a.can)}</p><p><b>Cannot:</b> ${esc(a.cannot)}</p><p><b>Tools:</b> ${esc(a.tools)}</p><p><b>If it fails:</b> ${esc(a.failure)}</p></div>`).join("")}</div>`;
  if (k === "orch") return `<h3>Orchestration (LangGraph)</h3><ol>${D.orchestration.nodes.map(n => `<li>${esc(n)}</li>`).join("")}</ol><ul>${D.orchestration.rules.map(n => `<li>${esc(n)}</li>`).join("")}</ul><h3 style="margin-top:18px">State</h3>` + tbl(["Field", "Meaning", "Written by", "Persisted in"], D.state.map(s => [`<code>${esc(s.field)}</code>`, esc(s.meaning), esc(s.written_by), esc(s.persisted)]));
  if (k === "trust") return `<h3>Zones</h3>` + tbl(["Zone", "Trust", "Rule"], D.trust.zones.map(z => [`<b>${esc(z.zone)}</b>`, esc(z.trust), esc(z.rule)])) + `<ul style="margin-top:10px">${D.trust.boundaries.map(b => `<li>${esc(b)}</li>`).join("")}</ul><h3 style="margin-top:18px">Security policies</h3>` + tbl(["Policy", "Rule", "Guardrail ids"], D.policies.map(p => [`<b>${esc(p.policy)}</b>`, esc(p.rule), esc(p.ids)]));
  if (k === "fail") return tbl(["Failure", "What the system does", "Customer sees", "Human sees"], D.failures.map(f => [`<b>${esc(f.failure)}</b>`, esc(f.behaviour), esc(f.customer_sees), esc(f.human_sees)]));
  if (k === "gaps") return `<p class="muted">Where the current system falls short. Written down on purpose.</p><ul>${D.gaps.map(g => `<li>${esc(g)}</li>`).join("")}</ul>`;
}
function buildDesign() {
  $("#dtabs").innerHTML = DTABS.map(([k, l], i) => `<button data-k="${k}" class="${i ? "" : "on"}">${l}</button>`).join(""); const show = k => { $("#dbody").innerHTML = designView(k); };
  $("#dtabs").onclick = e => { const b = e.target.closest("button"); if (!b) return; $$("#dtabs button").forEach(x => x.classList.toggle("on", x === b)); show(b.dataset.k); };
  fetch("design.json").then(r => r.json()).then(j => { S.design = j; show("scope"); }).catch(() => $("#dbody").innerHTML = "<p class='err'>Could not load design.json</p>");
}

/* =============== boot =============== */
function view(v) { ["console", "design", "queue"].forEach(x => $("#v-" + x).hidden = x !== v); $$("#nav button").forEach(b => b.classList.toggle("on", b.dataset.v === v)); if (v === "queue") loadQueue(); }
async function boot() {
  buildNet(); buildScenarios(); buildDesign(); initSchedule(); renderSec(); renderState(); renderAgents();
  $("#nav").onclick = e => { const b = e.target.closest("button"); if (b) view(b.dataset.v); };
  $("#composer").onsubmit = e => { e.preventDefault(); send($("#msg").value); }; $("#msg").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send($("#msg").value); } }); $("#msg").addEventListener("input", grow);
  $("#how").onclick = () => $("#dlg-how").showModal(); $$("[data-close]").forEach(b => b.onclick = () => b.closest("dialog").close()); $("#explain").onclick = explain; $("#esc").onclick = openDesk; $("#dclose2").onclick = () => $("#drawer").hidden = true;
  $("#qrefresh").onclick = loadQueue; $("#qf").onclick = e => { const b = e.target.closest("button"); if (!b) return; S.qfilter = b.dataset.s; $$("#qf button").forEach(x => x.classList.toggle("on", x === b)); loadQueue(); };
  $("#acct").onchange = e => useAccount(S.accounts.find(a => a.client_id === e.target.value));
  setInterval(() => { if (!$("#v-queue").hidden) loadQueue(); }, 12000);
  $("#thread").innerHTML = `<p class="empty">Waking the demo server…<br><small>The free server sleeps when idle; this can take up to a minute.</small></p>`;
  for (let i = 0; i < 10 && !S.accounts.length; i++) { try { S.accounts = await req("/demo/accounts"); } catch (e) { if (e.status === 404) break; await sleep(5000); } }
  if (!S.accounts.length) { $("#live").className = "live off"; $("#live-t").textContent = "demo accounts unavailable"; $("#thread").innerHTML = `<p class="empty">Demo accounts are disabled or the server is unreachable.</p>`; return; }
  const custs = S.accounts.filter(a => a.role === "customer"); $("#acct").innerHTML = custs.map(a => `<option value="${esc(a.client_id)}">${esc(a.client_id)} · test data: ${esc(a.label.toLowerCase())}</option>`).join(""); $("#acct-wrap").hidden = false;
  if (staffAcct()) $("#nav-queue").hidden = false; $("#live").className = "live on"; $("#live-t").textContent = "LIVE";
  await useAccount(custs.find(a => a.key === "failed_payment") || custs[0]); if (staffAcct()) { loadQueue(); }
}
boot();
