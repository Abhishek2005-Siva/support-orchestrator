"use strict";
const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
const API = (window.API_BASE || "").replace(/\/$/, "");
const esc = t => String(t ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const S = { accounts: [], cur: null, tokens: {}, threads: {}, history: {}, busy: false, polls: {}, filter: "pending", items: [], ran: new Set() };
const toast = (m, ms = 2600) => { const t = $("#toast"); t.textContent = m; t.hidden = false; clearTimeout(toast.t); toast.t = setTimeout(() => t.hidden = true, ms); };
const wait = ms => new Promise(r => setTimeout(r, ms));

async function req(path, opts = {}, tok) {
  const r = await fetch(API + path, { ...opts, headers: { "content-type": "application/json", ...(tok ? { Authorization: "Bearer " + tok } : {}) } });
  if (!r.ok) { let d = r.statusText; try { const j = await r.json(); d = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {} const e = new Error(d); e.status = r.status; e.retry = r.headers.get("Retry-After"); throw e; }
  return r.json();
}
const tok = () => S.tokens[S.cur.client_id];

/* ---------- accounts ---------- */
async function login(a) {
  if (S.tokens[a.client_id]) return;
  const t = await req("/auth/token", { method: "POST", body: JSON.stringify({ client_id: a.client_id, client_secret: a.secret }) });
  S.tokens[a.client_id] = t.access_token; setTimeout(() => delete S.tokens[a.client_id], (t.expires_in - 60) * 1000);
}
async function useAccount(a) {
  S.cur = a; $("#acct").value = a.client_id;
  try { await login(a); } catch (e) { return toast("Sign-in failed: " + e.message); }
  const staff = a.role !== "customer";
  $("#nav-staff").hidden = !staff; $$('#nav button[data-v="chat"]').forEach(b => b.hidden = staff);
  if (staff) { view("staff"); loadQueue(); } else { view("chat"); renderThread(); }
}
function view(v) { ["chat", "staff", "login"].forEach(x => $("#v-" + x).hidden = x !== v); $$("#nav button").forEach(b => b.classList.toggle("on", b.dataset.v === v)); }
function renderWho() {
  const a = S.cur; if (!a) return;
  $("#who").innerHTML = a.role === "customer" ? `Chatting as <b>${esc(a.label)}</b> <span>(${esc(a.client_id)}): ${esc(a.hint)}</span>` : "";
}

/* ---------- test cases ---------- */
function buildCases() {
  $("#cases").innerHTML = CASES.map((g, gi) => `<div class="grp">${esc(g.group)}</div>` + g.items.map((c, ci) =>
    `<button class="case" data-k="${gi}-${ci}"><b>${esc(c.title)}</b><span>“${esc(c.message.length > 70 ? c.message.slice(0, 70) + "…" : c.message)}”</span></button>`).join("")).join("");
  $$(".case").forEach(b => b.onclick = () => { const [g, i] = b.dataset.k.split("-"); runCase(CASES[g].items[i], b); });
}
async function runCase(c, btn) {
  if (S.busy) return toast("One moment, still answering…");
  if (c.account !== "any") { const a = S.accounts.find(x => x.key === c.account); if (a && (!S.cur || S.cur.client_id !== a.client_id || S.cur.role !== "customer")) await useAccount(a); }
  else if (!S.cur || S.cur.role !== "customer") await useAccount(S.accounts.find(x => x.role === "customer"));
  $("#v-chat").scrollIntoView?.({ block: "nearest" });
  await send(c.message, c); btn?.classList.add("done");
}

/* ---------- chat ---------- */
const thread = () => S.threads[S.cur.client_id] ||= [];
function renderThread() {
  renderWho(); const el = $("#thread"); el.innerHTML = "";
  if (!thread().length) el.innerHTML = `<div class="empty"><h3>Ask anything about billing or technical problems</h3><p>Pick a test case on the left to see what the system can do, or type your own question.</p></div>`;
  else thread().forEach(m => el.appendChild(m.node));
  el.scrollTop = el.scrollHeight;
}
function bubble(cls, html) { const d = document.createElement("div"); d.className = "msg " + cls; d.innerHTML = html; thread().push({ node: d }); $(".empty", $("#thread"))?.remove(); $("#thread").appendChild(d); $("#thread").scrollTop = 1e9; return d; }
const STAGES = [["input", "checked"], ["dispatch", "routed"], ["specialist_outputs", "answered"], ["merged", "drafted"], ["validation", "verified"]];
const stepsHtml = done => `<div class="steps">${STAGES.map(([k, l]) => `<i class="${done.includes(k) ? "d" : ""}">${done.includes(k) ? "✓ " : ""}${l}</i>`).join("")}</div>`;

async function send(text, kase) {
  text = (text || "").trim(); if (!text || S.busy || !S.cur || S.cur.role !== "customer") return;
  S.busy = true; $("#send").disabled = true; $("#msg").value = ""; grow();
  bubble("me", esc(text));
  const pend = bubble("bot", `<div class="dots"><span></span><span></span><span></span></div>${stepsHtml([])}`);
  const cid = S.cur.client_id;
  try {
    const res = await stream(text, done => pend.innerHTML = `<div class="dots"><span></span><span></span><span></span></div>${stepsHtml(done)}`);
    pend.innerHTML = answerHtml(res, kase);
    const h = (S.history[cid] ||= []); h.push({ role: "user", content: text }, { role: "assistant", content: res.reply || "" }); S.history[cid] = h.slice(-8);
    if (res.status === "human_review" && res.query_id) watch(res.query_id, cid);
    pend.querySelector("[data-staff]")?.addEventListener("click", () => { const a = S.accounts.find(x => x.role !== "customer"); if (a) useAccount(a); });
  } catch (e) {
    pend.innerHTML = `<div class="txt">${e.status === 429 ? `You're sending too fast. Try again in ${esc(e.retry || 60)} s.` : "Something went wrong: " + esc(e.message)}</div>`;
  } finally { S.busy = false; $("#send").disabled = false; if (S.cur?.role === "customer") $("#msg").focus(); }
}

async function stream(message, onProgress) {
  const r = await fetch(API + "/v1/query/stream", { method: "POST", headers: { "content-type": "application/json", Authorization: "Bearer " + tok() }, body: JSON.stringify({ message, channel: "web", history: S.history[S.cur.client_id] || [] }) });
  if (!r.ok) { const e = new Error(r.status === 422 ? "Message is empty or over 2000 characters." : r.statusText); e.status = r.status; e.retry = r.headers.get("Retry-After"); throw e; }
  const rd = r.body.getReader(), dec = new TextDecoder(); let buf = "", result = null, err = null;
  for (;;) {
    const { done, value } = await rd.read(); if (done) break; buf += dec.decode(value, { stream: true });
    let i; while ((i = buf.search(/\r?\n\r?\n/)) >= 0) {
      const block = buf.slice(0, i); buf = buf.slice(i).replace(/^\r?\n\r?\n/, ""); let ev = "message", data = "";
      block.split(/\r?\n/).forEach(l => { if (l.startsWith("event:")) ev = l.slice(6).trim(); else if (l.startsWith("data:")) data += l.slice(5).trim(); });
      if (!data) continue; const j = JSON.parse(data); if (ev === "progress") onProgress(j.stages || []); else if (ev === "result") result = j; else if (ev === "error") err = j;
    }
  }
  if (err) { const e = new Error(err.detail || "error"); e.status = err.status_code; throw e; }
  if (!result) throw new Error("No result received"); return result;
}

function answerHtml(r, kase) {
  const lab = { delivered: ["Answered", "ok"], human_review: ["Handed to a human", "warn"], rejected: ["Blocked", "bad"] }[r.status] || [r.status, ""];
  const v = r.validation, f = r.flags || {}, c = [`<span class="chip ${lab[1]}">${lab[0]}</span>`];
  (r.intents || []).forEach(i => c.push(`<span class="chip">${esc(i)}</span>`));
  if (v) c.push(`<span class="chip ${v.verdict === "approve" ? "ok" : "warn"}">checked: ${esc(v.verdict)}${v.confidence != null ? " " + Math.round(v.confidence * 100) + "%" : ""}</span>`);
  if (f.requires_human_approval) c.push(`<span class="chip warn">refund awaiting approval</span>`);
  if (f.cache_hit) c.push(`<span class="chip ok">cached</span>`);
  if (r.review_id) c.push(`<span class="chip">ref ${esc(r.review_id)}</span>`);
  c.push(`<span class="chip">${(r.latency_ms / 1000).toFixed(r.latency_ms < 1000 ? 2 : 1)} s</span>`);
  let note = "";
  if (kase) {
    const ok = kase.expect.status === "any" || kase.expect.status === r.status;
    note = `<div class="note"><b>What to look for</b>${esc(kase.look)}<div class="res ${ok ? "ok" : "warn"}">${ok ? "✓ Outcome as expected" : "≠ Outcome differs from the expected (" + kase.expect.status + "): the model is non-deterministic, try again"}</div>${kase.staff && r.status === "human_review" ? `<button class="btn" data-staff>Open the review queue as staff →</button>` : ""}</div>`;
  }
  const detail = { agents: r.agents, dispatch: r.dispatch, validation: r.validation, flags: r.flags };
  return `<div class="txt">${esc(r.reply || "(no reply)")}</div><div class="chips">${c.join("")}</div>${note}<details><summary>Technical details</summary><pre>${esc(JSON.stringify(detail, null, 2))}</pre></details>`;
}
function watch(qid, cid) {
  if (S.polls[qid]) return; let n = 0;
  S.polls[qid] = setInterval(async () => {
    try {
      await login(S.accounts.find(a => a.client_id === cid)); const q = await req("/v1/query/" + qid, {}, S.tokens[cid]); n++;
      if (q.status === "delivered" || q.status === "rejected") {
        clearInterval(S.polls[qid]); const d = document.createElement("div"); d.className = "msg bot"; d.innerHTML = `<div class="chips"><span class="chip ok">Update from our team</span></div><div class="txt">${esc(q.reply)}</div>`;
        (S.threads[cid] ||= []).push({ node: d }); if (S.cur?.client_id === cid) { $("#thread").appendChild(d); $("#thread").scrollTop = 1e9; } toast("A specialist replied to the case");
      }
    } catch {} if (n > 150) clearInterval(S.polls[qid]);
  }, 5000);
}
function grow() { const m = $("#msg"); m.style.height = "auto"; m.style.height = Math.min(m.scrollHeight, 140) + "px"; }

/* ---------- staff ---------- */
async function loadQueue() {
  if (S.cur?.role === "customer") return;
  try {
    const q = await req(`/v1/review-queue?status=${S.filter}&limit=100`, {}, tok()); S.items = q.items; const p = S.items.filter(i => i.status === "pending").length;
    $("#qn").textContent = p; $("#qn").hidden = !p;
    $("#queue").innerHTML = S.items.length ? S.items.map(i => `<button class="qi" data-id="${i.review_id}"><span class="pr ${i.priority}">${i.priority}</span><b>${esc(i.review_id)}</b> <small>${esc(i.status)} · ${esc((i.customer_message || "").slice(0, 70))}</small></button>`).join("") : `<p class="muted">Nothing here.</p>`;
    $$(".qi").forEach(b => b.onclick = () => openCase(b.dataset.id));
  } catch (e) { toast("Could not load queue: " + e.message); }
}
function openCase(id) {
  $$(".qi").forEach(b => b.classList.toggle("sel", b.dataset.id === id)); const i = S.items.find(x => x.review_id === id); if (!i) return; const open = i.status === "pending";
  $("#detail").innerHTML = `<h2><span class="pr ${i.priority}">${i.priority}</span>${esc(i.review_id)}</h2><p class="muted">${esc(i.reason)}</p>
   <h4>Customer wrote</h4><div class="box">${esc(i.customer_message)}</div><h4>AI summary</h4><div class="box">${esc(i.summary)}</div>
   <h4>Draft reply (not sent yet)</h4><div class="box">${esc(i.draft_reply || "No draft: please write a reply.")}</div>
   ${open ? `<h4>Your reply</h4><textarea id="edit" rows="4">${esc(i.draft_reply || "")}</textarea><div class="row"><button class="btn good" data-a="approve" ${i.draft_reply ? "" : "disabled"}>Approve draft</button><button class="btn" data-a="edit">Edit &amp; send</button><button class="btn bad" data-a="reject">Reject</button></div>` : `<p class="muted">Resolved by ${esc(i.reviewer || "-")}.</p>`}`;
  $$("#detail [data-a]").forEach(b => b.onclick = async () => {
    const a = b.dataset.a, reply = $("#edit")?.value.trim(); if (a === "edit" && (!reply || reply.length < 3)) return toast("Write a reply first");
    try { await req(`/v1/review-queue/${id}/resolve`, { method: "POST", body: JSON.stringify({ action: a, ...(a === "edit" ? { reply } : {}) }) }, tok()); toast("Resolved: " + a + ". The customer's chat updates within a few seconds."); await loadQueue(); $("#detail").innerHTML = `<p class="empty">Case ${esc(id)} resolved.</p>`; }
    catch (e) { toast(e.status === 409 ? "Already resolved" : "Failed: " + e.message); loadQueue(); }
  });
}

/* ---------- boot ---------- */
async function boot() {
  buildCases();
  $("#nav").onclick = e => { const b = e.target.closest("button"); if (!b) return; view(b.dataset.v); if (b.dataset.v === "staff") loadQueue(); };
  $("#qf").onclick = e => { const b = e.target.closest("button"); if (!b) return; S.filter = b.dataset.s; $$("#qf button").forEach(x => x.classList.toggle("on", x === b)); loadQueue(); };
  $("#composer").onsubmit = e => { e.preventDefault(); send($("#msg").value); };
  $("#msg").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send($("#msg").value); } });
  $("#msg").addEventListener("input", grow); $("#how").onclick = () => $("#dlg").showModal(); $("#dclose").onclick = () => $("#dlg").close();
  $("#acct").onchange = e => useAccount(S.accounts.find(a => a.client_id === e.target.value));
  setInterval(() => { if (S.cur?.role !== "customer" && !$("#v-staff").hidden) loadQueue(); }, 15000);
  const msg = $("#thread"); msg.innerHTML = `<div class="empty"><h3>Waking the demo server…</h3><p>The free server sleeps when idle; this can take up to a minute.</p></div>`; view("chat");
  for (let i = 0; i < 8; i++) {
    try { S.accounts = await req("/demo/accounts"); break; }
    catch (e) { if (e.status === 404) { view("login"); $("#lf").onsubmit = async ev => { ev.preventDefault(); const a = { client_id: $("#cid").value.trim(), secret: $("#sec").value, label: $("#cid").value.trim(), hint: "", role: "customer" }; S.accounts = [a]; try { await login(a); $("#acct-wrap").hidden = $("#nav").hidden = false; $("#acct").innerHTML = `<option value="${esc(a.client_id)}">${esc(a.client_id)}</option>`; useAccount(a); } catch (er) { $("#lerr").textContent = er.message; } }; return; } await wait(5000); }
  }
  if (!S.accounts.length) { $("#thread").innerHTML = `<div class="empty"><h3>Server unavailable</h3><p>Please retry in a minute.</p></div>`; return; }
  $("#acct").innerHTML = S.accounts.map(a => `<option value="${esc(a.client_id)}">${esc(a.label)}${a.role !== "customer" ? " (staff)" : ""}</option>`).join("");
  $("#acct-wrap").hidden = $("#nav").hidden = false;
  await useAccount(S.accounts.find(a => a.key === "failed_payment") || S.accounts[0]);
}
boot();
