// DealCheck 前端：档案（存浏览器本地）→ 合作信息 → 评估报告。
// 报告先显示规则结果，再通过 SSE 实时显示 Agent 的真实步骤；SSE 失败时回退为普通请求。
// 创作者姓名只存在本地，用来替换邮件里的 [Your name]，从不发送到服务器。
"use strict";

// ---------- 常量 ----------

const PLATFORMS = [["instagram", "Instagram"], ["tiktok", "TikTok"], ["youtube", "YouTube"]];

const NICHES = [
  ["beauty", "Beauty"], ["fashion", "Fashion"], ["fitness_wellness", "Fitness & Wellness"], ["food", "Food"],
  ["tech", "Tech"], ["gaming", "Gaming"], ["travel_lifestyle", "Travel & Lifestyle"], ["personal_finance", "Personal Finance"],
];

const CATEGORIES = [
  ["alcohol", "Alcohol"], ["tobacco_vaping", "Tobacco & vaping"], ["cannabis", "Cannabis"], ["gambling", "Gambling"],
  ["crypto", "Crypto"], ["weight_loss_supplements", "Weight-loss supplements"], ["adult_content", "Adult content"],
  ["firearms", "Firearms"], ["fast_fashion", "Fast fashion"], ["mlm", "MLM"], ["supplements_pharma", "Supplements & pharma"],
  ["political_content", "Political content"], ["payday_loans", "Payday loans"],
];

const DELIVERABLES = [
  ["instagram_reel", "Instagram Reel", "instagram"], ["instagram_feed_post", "Instagram feed post", "instagram"],
  ["instagram_story", "Instagram Story", "instagram"], ["tiktok_video", "TikTok video", "tiktok"],
  ["youtube_integration", "YouTube integration", "youtube"], ["youtube_dedicated", "YouTube dedicated video", "youtube"],
];

const TERM_NAMES = { exclusivity: "Exclusivity", usage_rights: "Usage rights", kill_fee: "Kill fee", revisions: "Revisions" };
const ADJUSTMENT_NAMES = { brand_reputation: "Brand reputation", values_fit: "Values fit", peer_rate: "Peer rates" };
const FLAG_NAMES = { scam: "Scam allegations", regulatory: "Regulatory action", nonpayment: "Non-payment", safety: "Product safety" };
const POSITION = { below_p25: "Below market", p25_to_p50: "Lower half", p50_to_p75: "Upper half", above_p75: "Above market" };
const SIGNATURE_PLACEHOLDER = "[Your name]";

const EMPTY_PROFILE = {
  name: "", followers: { instagram: "", tiktok: "", youtube: "" }, niche: "", values: [], blocked: [], custom: [], rateFloor: "",
};
const EMPTY_DEAL = {
  brandName: "", website: "", brandCategories: [], deliverables: [{ type: "instagram_reel", quantity: 1 }], offer: "",
  upfront: false, exDays: 0, exScope: "none", usageType: "organic", usageDays: "", perpetual: false, billedSeparately: false,
  killFee: "", killFeeMissing: false, revisions: 2, revisionsUnlimited: false,
};
const SAMPLE_PROFILE = {
  name: "Maya", followers: { instagram: "28000", tiktok: "", youtube: "" }, niche: "beauty",
  values: ["cruelty-free", "skin positivity"], blocked: ["alcohol"], custom: [], rateFloor: "250",
};
const SAMPLE_DEAL = {
  ...EMPTY_DEAL, brandName: "Glow Theory Skincare", website: "https://glowtheory.example", offer: "450",
  deliverables: [{ type: "instagram_reel", quantity: 1 }], killFeeMissing: true, revisions: 3,
};

// ---------- 工具函数 ----------

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const money = (n) => "$" + Math.round(Number(n)).toLocaleString("en-US");
const clone = (o) => JSON.parse(JSON.stringify(o));
const label = (list, value) => (list.find((x) => x[0] === value) || [value, value])[1];

const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem("dealcheck." + key); return v ? JSON.parse(v) : fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem("dealcheck." + key, JSON.stringify(value)); } catch { /* 隐私模式等情况下忽略 */ }
  },
};

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 2400);
}

function domainOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return url; }
}

// 把正文里的来源编号渲染成角标：先转义，再把 (s1, s2) 换成 [1][2]，"unverified" 换成标记。
function cite(text, idMap) {
  const link = (sid) => (idMap && idMap[sid] ? `<sup><a href="#src-${idMap[sid]}" data-src="${idMap[sid]}">[${idMap[sid]}]</a></sup>` : "");
  const mark = '<span class="unverified" title="This citation could not be verified and was removed">unverified</span>';
  const token = (t) => (t === "unverified" ? mark : link(t));
  let html = esc(text);
  html = html.replace(/\s?\(((?:\s*(?:s\d+|unverified)\s*,?)+)\)/g, (_, group) =>
    group.split(",").map((t) => token(t.trim())).join(""));
  return html.replace(/\b(s\d+)\b/g, (m) => link(m) || m);
}

// ---------- 状态 ----------

const state = {
  profile: { ...clone(EMPTY_PROFILE), ...store.get("profile", {}) },
  deal: { ...clone(EMPTY_DEAL), ...store.get("deal", {}) },
  run: store.get("lastReport", null),
  status: null,
  formError: null,
  controller: null,
};

// ---------- 转换为接口格式 ----------

function apiProfile(p) {
  const followers = {};
  for (const [key] of PLATFORMS) {
    const n = parseInt(p.followers[key], 10);
    if (n > 0) followers[key] = n;
  }
  return {
    followers, niche: p.niche, values: p.values, blocked_categories: p.blocked, blocked_custom: p.custom,
    rate_floor_usd: p.rateFloor === "" ? null : Number(p.rateFloor),
  };
}

function apiDeal(d) {
  let website = d.website.trim();
  if (website && !/^https?:\/\//i.test(website)) website = "https://" + website;
  const paid = d.usageType !== "organic";
  return {
    brand_name: d.brandName.trim(),
    brand_website: website || null,
    brand_categories: d.brandCategories,
    deliverables: d.deliverables.map((x) => ({ type: x.type, quantity: Math.max(1, parseInt(x.quantity, 10) || 1) })),
    offer_usd: Number(d.offer),
    requires_upfront_payment: d.upfront,
    contract: {
      exclusivity: { days: d.exScope === "none" ? 0 : Math.max(0, parseInt(d.exDays, 10) || 0), scope: d.exScope },
      usage_rights: {
        type: d.usageType,
        duration_days: paid && !d.perpetual && d.usageDays !== "" ? Math.max(0, parseInt(d.usageDays, 10) || 0) : null,
        perpetual: d.perpetual,
        paid_usage_billed_separately: paid && d.billedSeparately,
      },
      kill_fee_percent: d.killFeeMissing || d.killFee === "" ? null : Number(d.killFee),
      revisions: d.revisionsUnlimited ? "unlimited" : Math.max(0, parseInt(d.revisions, 10) || 0),
    },
  };
}

function validate() {
  const p = apiProfile(state.profile);
  if (!state.profile.niche || !Object.keys(p.followers).length) {
    return 'Set up your <a href="#/profile">profile</a> first: niche and at least one platform.';
  }
  const d = state.deal;
  if (!d.brandName.trim()) return "Enter the brand name.";
  if (d.offer === "" || Number(d.offer) < 0) return "Enter the cash offer in USD (0 if the brand only sends product).";
  if (!d.deliverables.length) return "Add at least one deliverable.";
  for (const x of d.deliverables) {
    const platform = DELIVERABLES.find((t) => t[0] === x.type)[2];
    if (!p.followers[platform]) {
      return `Add your ${label(PLATFORMS, platform)} follower count in your <a href="#/profile">profile</a>.`;
    }
  }
  if (!d.killFeeMissing && d.killFee !== "" && (Number(d.killFee) < 0 || Number(d.killFee) > 100)) return "Kill fee must be between 0 and 100%.";
  return null;
}

// ---------- 网络请求 ----------

async function postJSON(url, body, signal) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const data = await res.json();
      if (typeof data.detail === "string") detail = data.detail;
      else if (Array.isArray(data.detail)) detail = data.detail.map((d) => d.msg).join("; ");
    } catch { /* 保留默认信息 */ }
    throw new Error(detail);
  }
  return res.json();
}

async function streamEvaluation(body, signal, onProgress) {
  const res = await fetch("/api/evaluate/stream", {
    method: "POST", headers: { "Content-Type": "application/json", Accept: "text/event-stream" }, body: JSON.stringify(body), signal,
  });
  if (!res.ok || !res.body) throw new Error(`Stream failed (${res.status})`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (!data) continue;
      const payload = JSON.parse(data);
      if (event === "progress") onProgress(payload);
      else if (event === "result") return payload;
      else if (event === "error") throw new Error(payload.message || "Stream error");
    }
  }
  throw new Error("Stream ended without a result");
}

async function loadStatus() {
  try {
    const res = await fetch("/api/status");
    state.status = res.ok ? await res.json() : null;
  } catch { state.status = null; }
  if (currentPage() === "deal") render();
}

// ---------- 评估流程 ----------

async function analyse() {
  const error = validate();
  if (error) { state.formError = error; render(); window.scrollTo({ top: 0, behavior: "smooth" }); return; }
  state.formError = null;
  store.set("profile", state.profile);
  store.set("deal", state.deal);

  if (state.controller) state.controller.abort();
  const controller = new AbortController();
  state.controller = controller;

  const body = { profile: apiProfile(state.profile), deal: apiDeal(state.deal) };
  state.run = { phase: "rules", request: body, rules: null, full: null, steps: [], error: null, email: null };
  location.hash = "#/report";
  render();

  try {
    state.run.rules = await postJSON("/api/rules/evaluate", body, controller.signal);
  } catch (e) {
    if (controller.signal.aborted) return;
    state.run.phase = "error";
    state.run.error = e.message;
    render();
    return;
  }
  state.run.phase = "streaming";
  render();

  let full = null;
  try {
    full = await streamEvaluation(body, controller.signal, (event) => {
      state.run.steps.push(event);
      if (currentPage() === "report") renderProgressOnly();
    });
  } catch (e) {
    if (controller.signal.aborted) return;
    state.run.phase = "loading";  // SSE 失败：回退为普通请求
    state.run.steps = [];
    render();
    try {
      full = await postJSON("/api/evaluate", body, controller.signal);
    } catch (e2) {
      if (controller.signal.aborted) return;
      state.run.phase = "done";
      state.run.error = "AI research could not be completed: " + e2.message + " Showing rules-only results.";
      render();
      return;
    }
  }
  finish(full);
}

function finish(full) {
  const name = state.profile.name.trim();
  const fill = (text) => (name ? text.split(SIGNATURE_PLACEHOLDER).join(name) : text);
  state.run.full = full;
  state.run.phase = "done";
  state.run.email = { subject: fill(full.email.subject), body: fill(full.email.body) };
  store.set("lastReport", { ...state.run, request: state.run.request });
  render();
  loadStatus();
}

// ---------- 路由 ----------

function currentPage() {
  const page = location.hash.replace(/^#\/?/, "").split("?")[0];
  return ["profile", "deal", "report"].includes(page) ? page : null;
}

function render() {
  let page = currentPage();
  if (!page) {
    page = state.profile.niche ? "deal" : "profile";
    history.replaceState(null, "", "#/" + page);
  }
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.page === page));
  const app = $("#app");
  if (page === "profile") app.innerHTML = profilePage();
  else if (page === "deal") app.innerHTML = dealPage();
  else app.innerHTML = reportPage();
}

// ---------- 页面：档案 ----------

function chips(list, selected, group) {
  return list.map(([value, text]) =>
    `<button type="button" class="chip ${selected.includes(value) ? "on" : ""}" data-toggle="${group}" data-value="${esc(value)}">${esc(text)}</button>`).join("");
}

function tagList(items, group) {
  return items.map((t, i) =>
    `<button type="button" class="chip on" data-remove="${group}" data-index="${i}">${esc(t)}<span class="x" aria-label="Remove">×</span></button>`).join("");
}

function profilePage() {
  const p = state.profile;
  return `
  <div class="page-head">
    <h1>Your <em>profile</em></h1>
    <p class="lede">Saved only in this browser. Used to benchmark offers and check deals against your boundaries.</p>
  </div>
  <form class="stack" id="profile-form" autocomplete="off">
    <section class="card">
      <div class="card-head"><span class="label">Audience</span></div>
      <div class="grid">
        ${PLATFORMS.map(([key, name]) => `
          <label class="field"><span>${name} followers</span>
            <input type="number" min="0" step="1" inputmode="numeric" data-follower="${key}" value="${esc(p.followers[key])}" placeholder="—"></label>`).join("")}
      </div>
      <p class="hint" style="margin:10px 0 14px">Supported range: 1,000 to 499,999 followers per platform.</p>
      <label class="field"><span>Niche</span>
        <select data-bind="niche">
          <option value="">Choose your niche</option>
          ${NICHES.map(([v, t]) => `<option value="${v}" ${p.niche === v ? "selected" : ""}>${t}</option>`).join("")}
        </select></label>
    </section>

    <section class="card">
      <div class="card-head"><span class="label">Values</span></div>
      <div class="field">
        <span>What you stand for (press Enter to add)</span>
        <input type="text" data-tag-input="values" maxlength="50" placeholder="e.g. cruelty-free, sustainability">
      </div>
      <div class="chips">${tagList(p.values, "values")}</div>
    </section>

    <section class="card">
      <div class="card-head"><span class="label">Boundaries</span></div>
      <div class="field"><span>Categories you never work with — matching brands are declined automatically</span>
        <div class="chips">${chips(CATEGORIES, p.blocked, "blocked")}</div></div>
      <div class="field">
        <span>Anything else you avoid (checked by AI research; you'll be asked to verify matches)</span>
        <input type="text" data-tag-input="custom" maxlength="100" placeholder="e.g. caffeinated energy drinks">
        <div class="chips">${tagList(p.custom, "custom")}</div>
      </div>
      <label class="field"><span>Minimum rate per deal (USD, optional)</span>
        <input type="number" min="0" step="1" data-bind="rateFloor" value="${esc(p.rateFloor)}" placeholder="—"></label>
    </section>

    <section class="card">
      <div class="card-head"><span class="label">Email signature</span></div>
      <label class="field"><span>Your name — stays in this browser and is never sent to our server</span>
        <input type="text" data-bind="name" maxlength="100" value="${esc(p.name)}" placeholder="How you sign your emails"></label>
    </section>

    <div class="actions">
      <button type="button" class="btn btn-ghost" data-action="sample">Use sample profile</button>
      <button type="submit" class="btn btn-primary">Save profile</button>
    </div>
  </form>`;
}

// ---------- 页面：合作信息 ----------

function dealPage() {
  const d = state.deal;
  const paid = d.usageType !== "organic";
  const s = state.status;
  let statusBanner = "";
  if (s && !s.agent_available) {
    statusBanner = `<div class="banner warn"><div><strong>Rules-only mode.</strong> AI brand research is currently off, so you'll get rule-based results without brand research.</div></div>`;
  } else if (s && (s.remaining_today === 0 || !s.site_budget_available)) {
    statusBanner = `<div class="banner warn"><div><strong>AI research limit reached for today.</strong> You'll get rules-only results until tomorrow.</div></div>`;
  } else if (s) {
    statusBanner = `<p class="hint">${s.remaining_today} of ${s.daily_limit} AI-researched reports left today.</p>`;
  }
  return `
  <div class="page-head">
    <h1>Analyse a <em>deal</em></h1>
    <p class="lede">Enter the offer as the brand sent it. You'll get a recommendation, a fair-rate check and a reply draft.</p>
  </div>
  <form class="stack" id="deal-form" autocomplete="off">
    ${state.formError ? `<div class="banner error"><div>${state.formError}</div></div>` : ""}
    ${statusBanner}

    <section class="card">
      <div class="card-head"><span class="label">Brand</span></div>
      <div class="grid">
        <label class="field"><span>Brand name</span><input type="text" data-bind="brandName" value="${esc(d.brandName)}" placeholder="e.g. Acme Skincare"></label>
        <label class="field"><span>Website (helps confirm the right brand)</span><input type="text" data-bind="website" value="${esc(d.website)}" placeholder="acme.com"></label>
      </div>
      <div class="field" style="margin-top:14px"><span>Does the brand sell any of these? (leave empty if unsure — research will check)</span>
        <div class="chips">${chips(CATEGORIES, d.brandCategories, "brandCategories")}</div></div>
    </section>

    <section class="card">
      <div class="card-head"><span class="label">Deliverables &amp; offer</span></div>
      ${d.deliverables.map((x, i) => `
        <div class="row">
          <select data-deliverable="${i}" aria-label="Deliverable type">
            ${DELIVERABLES.map(([v, t]) => `<option value="${v}" ${x.type === v ? "selected" : ""}>${t}</option>`).join("")}
          </select>
          <input type="number" min="1" step="1" data-quantity="${i}" value="${esc(x.quantity)}" aria-label="Quantity">
          <button type="button" class="btn-icon" data-remove-deliverable="${i}" aria-label="Remove deliverable" ${d.deliverables.length === 1 ? "disabled" : ""}>×</button>
        </div>`).join("")}
      <button type="button" class="btn btn-ghost btn-small" data-action="add-deliverable">+ Add deliverable</button>
      <label class="field" style="margin-top:16px"><span>Cash offer for everything above (USD)</span>
        <input type="number" min="0" step="1" data-bind="offer" value="${esc(d.offer)}" placeholder="e.g. 450"></label>
    </section>

    <section class="card">
      <div class="card-head"><span class="label">Contract terms</span></div>
      <div class="grid">
        <label class="field"><span>Exclusivity</span>
          <select data-bind="exScope">
            <option value="none" ${d.exScope === "none" ? "selected" : ""}>None</option>
            <option value="category" ${d.exScope === "category" ? "selected" : ""}>Competitors only</option>
            <option value="industry" ${d.exScope === "industry" ? "selected" : ""}>Industry-wide</option>
          </select></label>
        <label class="field"><span>Exclusivity length (days)</span>
          <input type="number" min="0" data-bind="exDays" value="${esc(d.exDays)}" ${d.exScope === "none" ? "disabled" : ""}></label>
      </div>
      <div class="grid" style="margin-top:14px">
        <label class="field"><span>Usage rights</span>
          <select data-bind="usageType">
            <option value="organic" ${d.usageType === "organic" ? "selected" : ""}>Organic posting only</option>
            <option value="paid_ads" ${d.usageType === "paid_ads" ? "selected" : ""}>Paid ads</option>
            <option value="whitelisting" ${d.usageType === "whitelisting" ? "selected" : ""}>Whitelisting</option>
          </select></label>
        <label class="field"><span>Paid usage length (days)</span>
          <input type="number" min="0" data-bind="usageDays" value="${esc(d.usageDays)}" placeholder="Not stated" ${!paid || d.perpetual ? "disabled" : ""}></label>
      </div>
      <div class="checks">
        <label class="check"><input type="checkbox" data-bind="perpetual" ${d.perpetual ? "checked" : ""}> Perpetual usage</label>
        <label class="check"><input type="checkbox" data-bind="billedSeparately" ${d.billedSeparately ? "checked" : ""} ${!paid ? "disabled" : ""}> Paid usage billed separately</label>
      </div>
      <div class="grid" style="margin-top:16px">
        <label class="field"><span>Kill fee (%)</span>
          <input type="number" min="0" max="100" data-bind="killFee" value="${esc(d.killFee)}" ${d.killFeeMissing ? "disabled" : ""}>
          <label class="check"><input type="checkbox" data-bind="killFeeMissing" ${d.killFeeMissing ? "checked" : ""}> Not mentioned</label></label>
        <label class="field"><span>Revision rounds</span>
          <input type="number" min="0" data-bind="revisions" value="${esc(d.revisions)}" ${d.revisionsUnlimited ? "disabled" : ""}>
          <label class="check"><input type="checkbox" data-bind="revisionsUnlimited" ${d.revisionsUnlimited ? "checked" : ""}> Unlimited</label></label>
      </div>
    </section>

    <section class="card">
      <div class="card-head"><span class="label">Red flags</span></div>
      <label class="check"><input type="checkbox" data-bind="upfront" ${d.upfront ? "checked" : ""}> The brand asks me to pay something upfront (product, deposit, "training" fee)</label>
    </section>

    <div class="actions">
      <button type="button" class="btn btn-ghost" data-action="sample">Try a sample deal</button>
      <button type="submit" class="btn btn-primary">Analyse deal</button>
    </div>
  </form>`;
}

// ---------- 页面：评估报告 ----------

const STEP_TEXT = {
  thinking: (e, i) => (e.final ? "Wrapping up within the research budget" : i === 0 ? "Planning the research" : "Reviewing what was found"),
  search: (e) => `Searching: <span class="q">${esc(e.query)}</span>`,
  read: (e) => (e.source ? `Reading source: <span class="q">${esc(e.source.domain || e.source.title)}</span>` : "Reading a source"),
  tool: (e) => ({ get_market_benchmark: "Checking the market benchmark", check_contract_terms: "Checking contract terms",
    get_creator_profile: "Reading your profile" })[e.name] || "Checking details",
  submitting: () => "Compiling findings with sources",
  scoring: () => "Scoring with the rule engine",
  email: () => "Drafting your reply email",
  agent_skipped: () => "Brand research skipped — a hard rule already decides this deal",
  agent_unavailable: (e) => esc(e.message || "AI research unavailable — using rules only"),
};

function progressCard() {
  const run = state.run;
  if (!run || !["rules", "streaming", "loading"].includes(run.phase)) return "";
  let body;
  if (run.phase === "loading") {
    body = `<ul class="steps"><li class="step current"><span class="icon"><span class="spinner"></span></span>Researching the brand (live progress unavailable)…</li></ul>`;
  } else {
    const steps = run.phase === "rules" ? [] : run.steps;
    let thinkingCount = 0;
    const items = steps.map((e, idx) => {
      const n = e.type === "thinking" ? thinkingCount++ : 0;
      const text = (STEP_TEXT[e.type] || (() => esc(e.type)))(e, n);
      const current = idx === steps.length - 1;
      return `<li class="step ${current ? "current" : ""}"><span class="icon">${current ? '<span class="spinner"></span>' : "✓"}</span><span>${text}</span></li>`;
    });
    if (!items.length) items.push(`<li class="step current"><span class="icon"><span class="spinner"></span></span>${run.phase === "rules" ? "Running the rule engine" : "Starting brand research"}</li>`);
    body = `<ul class="steps">${items.join("")}</ul>`;
  }
  return `<section class="card" id="progress-card"><div class="card-head"><span class="label">AI research — live</span><span class="tag tag-sourced">In progress</span></div>${body}</section>`;
}

function renderProgressOnly() {
  const card = $("#progress-card");
  if (card) card.outerHTML = progressCard();
  else render();
}

function modeBanner(run) {
  if (run.error && run.phase === "done") return `<div class="banner warn"><div><strong>Rules-only mode.</strong> ${esc(run.error)}</div></div>`;
  const agent = run.full && run.full.agent;
  if (!agent) return "";
  const msg = {
    unavailable: ["Rules-only mode.", agent.error || "AI brand research is unavailable."],
    limited: ["Rules-only mode.", agent.error],
    fallback: ["Rules-only mode.", "AI brand research couldn't be completed, so brand and fit scores are neutral."],
    skipped: ["Brand research skipped.", "A hard rule already decides this deal, so we didn't spend time researching the brand."],
  }[agent.status];
  return msg ? `<div class="banner warn"><div><strong>${esc(msg[0])}</strong> ${esc(msg[1])}</div></div>` : "";
}

// 结论是否已经确定：调查完成；或预检已命中拒绝类规则（Agent 只会增加、不会撤销拒绝）；或已确认是仅规则模式。
function isSettled(run, ev) {
  if (run.phase === "done") return true;
  if (ev.hard_rules.some((h) => h.kind === "reject")) return true;
  return run.steps.some((e) => e.type === "agent_unavailable" || e.type === "agent_skipped");
}

function pendingCard(ev) {
  const p = ev.pricing;
  const c = ev.contract;
  const caps = ev.hard_rules.filter((h) => h.kind === "cap");
  const contractText = c.red_count || c.yellow_count
    ? `${c.red_count} high-risk and ${c.yellow_count} medium-risk term${c.red_count + c.yellow_count > 1 ? "s" : ""}` : "All terms within typical ranges";
  return `
  <section class="card decision">
    <div class="card-head"><span class="label">Recommendation</span><span class="tag tag-sourced">Waiting for brand research</span></div>
    <p class="decision-word pending">Checking the brand…</p>
    <p class="muted" style="margin:12px 0 0">The recommendation depends on brand research, which usually takes 10–20 seconds. Already settled by the rules:</p>
    <ul class="settled">
      <li><span class="k">Pricing</span><span>Offer is ${Math.round(p.offer_ratio * 100)}% of the typical rate</span><span class="mono">${ev.score.pricing.final}/${ev.score.pricing.max}</span></li>
      <li><span class="k">Contract</span><span>${contractText}</span><span class="mono">${ev.score.contract.final}/${ev.score.contract.max}</span></li>
    </ul>
    ${caps.length ? `<div class="rule-box cap"><div class="label">Fix before accepting</div>${caps.map((h) => `<p class="rule-item"><span class="rule-id">${esc(h.rule_id)}</span>${esc(h.message)}</p>`).join("")}</div>` : ""}
  </section>`;
}

function decisionCard(ev, idMap) {
  const rejects = ev.hard_rules.filter((h) => h.kind === "reject");
  const caps = ev.hard_rules.filter((h) => h.kind === "cap");
  const overridden = rejects.length > 0;
  const words = { accept: "Accept", negotiate: "Negotiate", decline: "Decline" };
  const capNote = !overridden && caps.length && ev.decision === "negotiate" && ev.total_score >= 75
    ? `Score alone suggests Accept — capped at Negotiate by ${caps.map((h) => h.rule_id).join(", ")}` : "";
  const ruleItems = (hits) => hits.map((h) => `<p class="rule-item"><span class="rule-id">${esc(h.rule_id)}</span>${cite(h.message, idMap)}</p>`).join("");
  const capMessages = new Set(caps.map((h) => h.message));
  const reasons = overridden ? [] : ev.reasons.filter((r) => !capMessages.has(r));
  return `
  <section class="card decision">
    <div class="card-head"><span class="label">Recommendation</span>
      <span class="tags"><span class="tag tag-rule">Rule-based</span>${ev.brand_research_available ? '<span class="tag tag-sourced">Uses sourced research</span>' : ""}</span></div>
    <div class="decision-top">
      <div>
        <p class="decision-word ${ev.decision}">${words[ev.decision]}</p>
      </div>
      <div class="score ${overridden ? "overridden" : ""}">
        <div class="score-num">${ev.total_score}<small>/100</small></div>
        <div class="score-note">${overridden ? "Score overridden by a hard rule" : capNote ? esc(capNote) : "Overall score"}</div>
      </div>
    </div>
    ${rejects.length ? `<div class="rule-box reject"><div class="label">Hard rule — this decides the deal</div>${ruleItems(rejects)}</div>` : ""}
    ${caps.length ? `<div class="rule-box cap"><div class="label">Fix before accepting</div>${ruleItems(caps)}</div>` : ""}
    ${reasons.length ? `<ul class="reasons">${reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
  </section>`;
}

function pct(value, max) { return Math.max(0, Math.min(100, (value / max) * 100)); }

function priceCard(ev, request) {
  const p = ev.pricing;
  const b = p.adjusted_benchmark;
  const counter = p.suggested_counter_range ? Math.round(p.suggested_counter_range.high / 10) * 10 : null;
  const floor = request && request.profile.rate_floor_usd;
  const max = Math.max(b.p75, p.offer_usd, counter || 0, floor || 0) * 1.15 || 1;
  const edge = (x) => (x < 8 ? "edge-left" : x > 92 ? "edge-right" : "");
  const at = (v) => pct(v, max).toFixed(2);
  const a = p.adjustments;
  const parts = [`niche ×${a.niche_coefficient}`];
  if (a.usage_rights_uplift) parts.push(`usage rights +${Math.round(a.usage_rights_uplift * 100)}%`);
  if (a.exclusivity_uplift) parts.push(`exclusivity +${Math.round(a.exclusivity_uplift * 100)}%`);
  return `
  <section class="card">
    <div class="card-head"><span class="label">Offer vs market</span><span class="tags"><span class="tag tag-rule">Rule-based</span></span></div>
    <div class="pricebar" role="img" aria-label="Market range ${money(b.p25)} to ${money(b.p75)}, brand offer ${money(p.offer_usd)}${counter ? ", suggested counter " + money(counter) : ""}">
      <div class="pb-track"></div>
      <div class="pb-band" style="left:${at(b.p25)}%;width:${(pct(b.p75, max) - pct(b.p25, max)).toFixed(2)}%"></div>
      <div class="pb-mark p50" style="left:${at(b.p50)}%"></div>
      ${floor ? `<div class="pb-mark floor" style="left:${at(floor)}%"></div>` : ""}
      <div class="pb-mark" style="left:${at(p.offer_usd)}%"></div>
      <div class="pb-label above ${edge(pct(p.offer_usd, max))}" style="left:${at(p.offer_usd)}%">Offer ${money(p.offer_usd)}</div>
      ${counter ? `<div class="pb-mark counter" style="left:${at(counter)}%"></div>
      <div class="pb-label below ${edge(pct(counter, max))}" style="left:${at(counter)}%">Counter ${money(counter)}</div>` : ""}
    </div>
    <div class="legend">
      <span><i class="l-band"></i>Market range ${money(b.p25)}–${money(b.p75)}</span>
      <span><i class="l-offer"></i>Brand offer</span>
      ${counter ? '<span><i class="l-counter"></i>Suggested counter</span>' : ""}
      ${floor ? `<span><i class="l-floor"></i>Your minimum ${money(floor)}</span>` : ""}
    </div>
    <div class="stats">
      <div class="stat"><div class="label">Market median</div><div class="v">${money(b.p50)}</div></div>
      <div class="stat"><div class="label">Offer ratio</div><div class="v">${p.offer_ratio.toFixed(2)}×</div></div>
      <div class="stat"><div class="label">Position</div><div class="v" style="font-family:var(--sans);font-size:14px">${POSITION[p.position]}</div></div>
    </div>
    ${p.confidence === "low" ? `<div class="banner warn" style="margin-top:14px"><div><strong>Low confidence.</strong> ${esc(p.confidence_message)}</div></div>` : ""}
    <details style="margin-top:12px"><summary class="hint" style="cursor:pointer">How this was calculated</summary>
      <ul class="sublist">
        ${p.line_items.map((li) => `<li><span class="k">${esc(label(DELIVERABLES, li.type))} × ${li.quantity}</span> — ${esc(li.tier.replace("_", "+"))} tier, median <span class="mono">${money(li.unit_price.p50)}</span> each</li>`).join("")}
        <li>Adjustments: ${esc(parts.join(", "))}. Benchmarks from public industry reports (data as of ${esc(p.data_date)}).</li>
        ${p.confidence_notes.map((n) => `<li>${esc(n)}</li>`).join("")}
      </ul>
    </details>
  </section>`;
}

function scoreCard(ev) {
  const s = ev.score;
  const brandTag = ev.brand_research_available && s.brand_fit.adjustment !== 0
    ? '<span class="tag tag-sourced">Sourced</span>' : '<span class="tag tag-insufficient">Insufficient info</span>';
  const row = (name, dim, tag, sub) => `
    <div class="dim"><span>${name}</span><div class="dim-bar"><span style="width:${pct(dim.final, dim.max)}%"></span></div>
      <span class="dim-val">${dim.final}/${dim.max}</span>
      <div class="dim-sub">${tag} ${sub}</div></div>`;
  const adj = (dim) => (dim.adjustment ? ` · base ${dim.base}, research ${dim.adjustment > 0 ? "+" : ""}${dim.adjustment}` : "");
  return `
  <section class="card">
    <div class="card-head"><span class="label">Score breakdown</span><span class="mono muted">${s.total}/100</span></div>
    ${row("Pricing", s.pricing, '<span class="tag tag-rule">Rule-based</span>', adj(s.pricing))}
    ${row("Contract", s.contract, '<span class="tag tag-rule">Rule-based</span>', "")}
    ${row("Brand &amp; fit", s.brand_fit, brandTag, ev.brand_research_available ? adj(s.brand_fit) : " · neutral score without research")}
    ${s.total_cap_correction ? `<p class="hint" style="margin:10px 0 0">Research adjustments were capped at ±15 points (${s.total_cap_correction > 0 ? "+" : ""}${s.total_cap_correction}).</p>` : ""}
  </section>`;
}

function contractCard(ev) {
  return `
  <section class="card">
    <div class="card-head"><span class="label">Contract terms</span><span class="tags"><span class="tag tag-rule">Rule-based</span></span></div>
    ${ev.contract.terms.map((t) => `
      <div class="term ${t.light}">
        <div class="term-head"><span>${TERM_NAMES[t.term]}</span><span class="light">${{ green: "OK", yellow: "Caution", red: "High risk" }[t.light]}</span></div>
        ${t.risk ? `<p>${esc(t.risk)}</p>` : ""}
        ${t.suggestion ? `<p class="fix">→ ${esc(t.suggestion)}</p>` : ""}
      </div>`).join("")}
  </section>`;
}

function overviewSections(f) {
  const o = f.brand_overview || { who_they_are: f.brand_summary || "" };  // 兼容旧版本保存在本地的报告
  return [["Who they are", o.who_they_are], ["Working with creators", o.working_with_creators],
    ["Regulatory &amp; controversies", o.regulatory_and_controversies]];
}

function researchCard(run, ev, idMap) {
  if (run.phase !== "done") {
    return `<section class="card"><div class="card-head"><span class="label">Brand research</span><span class="tag tag-sourced">In progress</span></div><p class="muted" style="margin:0">Results appear here when the research finishes.</p></section>`;
  }
  const f = run.full && run.full.findings;
  if (!f) {
    return `<section class="card"><div class="card-head"><span class="label">Brand research</span><span class="tag tag-insufficient">Insufficient info</span></div>
      <p class="muted" style="margin:0">No brand research in this report. Brand and fit use a neutral score — check the brand yourself before agreeing.</p></section>`;
  }
  const sourced = f.sources.length > 0;
  const level = (v) => (v === "insufficient_info" ? '<span class="tag tag-insufficient">Insufficient info</span>' : esc(v.replace("_", " ")));
  const adjustments = ev.score.adjustments;
  return `
  <section class="card research">
    <div class="card-head"><span class="label">Brand research</span>
      <span class="tags">${sourced ? '<span class="tag tag-sourced">Sourced</span>' : '<span class="tag tag-insufficient">Insufficient info</span>'}</span></div>
    <div class="overview">${overviewSections(f).map(([title, text]) => `
      <div class="ov"><div class="label">${title}</div><p>${text ? cite(text, idMap) : '<span class="muted">Nothing reliable found.</span>'}</p></div>`).join("")}</div>
    <div class="facts">
      <div class="fact"><div class="label">Brand identity</div><div class="v">${f.brand_entity_confirmed ? "Confirmed" : '<span class="tag tag-insufficient">Not confirmed</span>'}</div></div>
      <div class="fact"><div class="label">Risk level</div><div class="v">${level(f.risk_level)}</div></div>
      <div class="fact"><div class="label">Values fit</div><div class="v">${level(f.values_fit.level)}</div></div>
    </div>
    ${f.values_fit.summary ? `<p class="muted">${cite(f.values_fit.summary, idMap)}</p>` : ""}
    ${f.severe_flags.length ? `<div class="label" style="margin-top:14px">Serious issues reported</div><ul class="sublist">${f.severe_flags.map((x) => `<li><span class="k">${esc(FLAG_NAMES[x.type] || x.type)}</span> — ${cite(x.description, idMap)}</li>`).join("")}</ul>` : ""}
    ${f.peer_rates.length ? `<div class="label" style="margin-top:14px">What comparable creators report</div><ul class="sublist">${f.peer_rates.map((x) => `<li>${cite(x.description, idMap)}</li>`).join("")}</ul>` : ""}
    ${adjustments.length ? `<div class="label" style="margin-top:14px">Score adjustments from research</div><ul class="sublist">${adjustments.map((a) => `
      <li><span class="k">${esc(ADJUSTMENT_NAMES[a.item] || a.item)}</span> <span class="delta ${a.applied > 0 ? "pos" : a.applied < 0 ? "neg" : ""}">${a.applied > 0 ? "+" : ""}${a.applied}</span>
      ${a.status === "voided" ? ' <span class="status-voided">Not counted — no valid source</span>' : a.status === "clamped" ? ` <span class="hint">(requested ${a.requested}, capped)</span>` : ""}
      <br>${cite(a.reason, idMap)}</li>`).join("")}</ul>` : ""}
    ${f.unverified_citations.length ? `<p class="hint" style="margin-top:12px">${f.unverified_citations.length} citation${f.unverified_citations.length > 1 ? "s" : ""} could not be matched to a source and ${f.unverified_citations.length > 1 ? "were" : "was"} removed (marked <span class="unverified">unverified</span>).</p>` : ""}
  </section>`;
}

function emailCard(run) {
  if (run.phase !== "done" || !run.email) return "";
  const e = run.full.email;
  return `
  <section class="card">
    <div class="card-head"><span class="label">Reply draft</span><span class="hint">${e.generated_by === "llm" ? "AI draft · numbers from the rule engine" : "Template draft"}</span></div>
    <input type="text" class="email-subject" id="email-subject" value="${esc(run.email.subject)}" aria-label="Email subject">
    <textarea id="email-body" aria-label="Email body">${esc(run.email.body)}</textarea>
    <div class="email-foot">
      <span class="hint">DealCheck never sends emails. Review, edit, then send it yourself.${state.profile.name ? "" : ' Add your name in your <a href="#/profile">profile</a> to fill the sign-off.'}</span>
      <button type="button" class="btn btn-primary btn-small" data-action="copy-email">Copy email</button>
    </div>
  </section>`;
}

function sourcesCard(sources) {
  if (!sources.length) return "";
  return `
  <section class="card">
    <div class="card-head"><span class="label">Sources</span><span class="tag tag-sourced">${sources.length} cited</span></div>
    <ol class="sources">${sources.map((s, i) => `
      <li id="src-${i + 1}"><span class="n">[${i + 1}]</span><span><a class="t" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title || domainOf(s.url))}</a><br><span class="d">${esc(domainOf(s.url))}</span></span></li>`).join("")}</ol>
  </section>`;
}

function disclaimer(ev, request) {
  const highValue = request && (request.deal.offer_usd >= 5000 || ev.contract.red_count >= 2);
  return `
  <p class="disclaimer">
    ${highValue ? "<strong>This deal is high-value or has several high-risk terms — consider having a lawyer review the contract before signing.</strong><br>" : ""}
    <strong>DealCheck provides informational analysis only and does not constitute legal or financial advice.</strong>
    Recommendations come from rule-based benchmarks and publicly available information, and may contain errors. You are responsible for your decisions.
    Remember to disclose sponsored content as required by the platform and the FTC (e.g. #ad or Paid Partnership).
  </p>`;
}

function reportPage() {
  const run = state.run;
  if (!run) {
    return `<div class="page-head"><h1>Deal <em>report</em></h1></div>
      <section class="card empty"><p>No report yet.</p><a class="btn btn-primary" href="#/deal">Analyse a deal</a></section>`;
  }
  const head = `<div class="page-head"><h1>Deal <em>report</em></h1><p class="lede">${esc(run.request.deal.brand_name)} · ${run.request.deal.deliverables.map((d) => `${d.quantity || 1}× ${label(DELIVERABLES, d.type)}`).join(", ")} · ${money(run.request.deal.offer_usd)}</p></div>`;
  if (run.phase === "error") {
    return head + `<div class="banner error"><div><strong>Couldn't analyse this deal.</strong> ${esc(run.error)} <a href="#/deal">Edit the deal</a></div></div>`;
  }
  if (!run.rules && !run.full) return head + progressCard();

  const ev = (run.full && run.full.evaluation) || run.rules;
  const sources = (run.full && run.full.sources) || [];
  const idMap = Object.fromEntries(sources.map((s, i) => [s.id, i + 1]));
  return head + `<div class="stack">
    ${modeBanner(run)}
    ${isSettled(run, ev) ? decisionCard(ev, idMap) : pendingCard(ev)}
    ${progressCard()}
    ${priceCard(ev, run.request)}
    ${scoreCard(ev)}
    ${contractCard(ev)}
    ${researchCard(run, ev, idMap)}
    ${emailCard(run)}
    ${sourcesCard(sources)}
    ${disclaimer(ev, run.request)}
  </div>`;
}

// ---------- 交互事件 ----------

function updateField(target) {
  const page = currentPage();
  const model = page === "profile" ? state.profile : state.deal;
  if (target.dataset.bind) {
    model[target.dataset.bind] = target.type === "checkbox" ? target.checked : target.value;
    return target.type === "checkbox" || target.tagName === "SELECT";  // 这些改动会影响其他输入框是否可用，需要重新渲染
  }
  if (target.dataset.follower) state.profile.followers[target.dataset.follower] = target.value;
  if (target.dataset.deliverable) state.deal.deliverables[+target.dataset.deliverable].type = target.value;
  if (target.dataset.quantity) state.deal.deliverables[+target.dataset.quantity].quantity = target.value;
  return false;
}

function persist() {
  store.set("profile", state.profile);
  store.set("deal", state.deal);
}

document.addEventListener("input", (e) => {
  if (e.target.id === "email-subject" && state.run && state.run.email) state.run.email.subject = e.target.value;
  else if (e.target.id === "email-body" && state.run && state.run.email) state.run.email.body = e.target.value;
  else if (e.target.dataset.tagInput) return;
  else { updateField(e.target); persist(); }
});

document.addEventListener("change", (e) => {
  if (e.target.dataset.tagInput || e.target.id === "email-subject" || e.target.id === "email-body") return;
  if (updateField(e.target)) render();
  persist();
});

document.addEventListener("keydown", (e) => {
  const group = e.target.dataset && e.target.dataset.tagInput;
  if (!group || e.key !== "Enter") return;
  e.preventDefault();
  const value = e.target.value.trim();
  const list = state.profile[group];
  if (value && !list.includes(value) && list.length < 10) list.push(value);
  persist();
  render();
  const input = document.querySelector(`[data-tag-input="${group}"]`);
  if (input) input.focus();
});

document.addEventListener("click", async (e) => {
  const el = e.target.closest("button, a");
  if (!el) return;
  const d = el.dataset;
  if (d.toggle) {
    const model = d.toggle === "brandCategories" ? state.deal : state.profile;
    const list = model[d.toggle];
    const i = list.indexOf(d.value);
    if (i >= 0) list.splice(i, 1); else list.push(d.value);
    persist(); render();
  } else if (d.remove) {
    state.profile[d.remove].splice(+d.index, 1);
    persist(); render();
  } else if (d.removeDeliverable !== undefined) {
    state.deal.deliverables.splice(+d.removeDeliverable, 1);
    persist(); render();
  } else if (d.action === "add-deliverable") {
    state.deal.deliverables.push({ type: "instagram_reel", quantity: 1 });
    persist(); render();
  } else if (d.action === "sample") {
    if (currentPage() === "profile") { state.profile = clone(SAMPLE_PROFILE); toast("Sample profile loaded — review and save"); }
    else {
      if (!state.profile.niche) state.profile = clone(SAMPLE_PROFILE);
      state.deal = clone(SAMPLE_DEAL);
      toast("Sample deal loaded (fictional brand, so research will find little)");
    }
    state.formError = null;
    persist(); render();
  } else if (d.action === "copy-email") {
    const text = `Subject: ${state.run.email.subject}\n\n${state.run.email.body}`;
    try { await navigator.clipboard.writeText(text); toast("Email copied"); } catch {
      const area = $("#email-body"); area.select(); document.execCommand("copy"); toast("Email copied");
    }
  }
});

document.addEventListener("submit", (e) => {
  e.preventDefault();
  if (e.target.id === "profile-form") {
    persist();
    state.formError = null;
    toast("Profile saved on this device");
    location.hash = "#/deal";
  } else if (e.target.id === "deal-form") {
    analyse();
  }
});

window.addEventListener("hashchange", () => { render(); window.scrollTo(0, 0); });

// ---------- 启动 ----------

if (state.run && state.run.phase !== "done" && state.run.phase !== "error") state.run = null;  // 刷新时丢弃未完成的评估
if (/[?&]sample=1/.test(location.hash)) {
  state.profile = clone(SAMPLE_PROFILE);
  state.deal = clone(SAMPLE_DEAL);
  persist();
  if (currentPage() === "report") analyse();
}
render();
loadStatus();
