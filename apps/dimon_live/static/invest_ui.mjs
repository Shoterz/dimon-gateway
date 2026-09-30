// Investing views: portfolio panel, trending picks, long-term plans, quotes,
// order cards and the investor-profile questions. Every figure shown comes
// from the gateway; suggestions are rule-based and always list their risks.

const pct = value => `${value >= 0 ? "+" : ""}${Number(value).toFixed(1)}%`;
const riskDots = level => "●".repeat(level) + "○".repeat(5 - level);

export function requestOrderApproval(order, signed, fetchImpl = globalThis.fetch) {
  return fetchImpl(`/api/orders/${order.order_id}/approve`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({receipt_digest: order.order_digest, ...signed}),
  }).then(async response => response.ok
    ? {kind: "RESULT", result: await response.json()}
    : {kind: "UNKNOWN", message: `Gateway returned HTTP ${response.status}`})
    .catch(error => ({kind: "UNKNOWN", message: error instanceof Error ? error.message : String(error)}));
}

function sparkline(history, width = 260, height = 56) {
  const closes = (history || []).map(point => point.close_minor);
  if (closes.length < 2) return "";
  const min = Math.min(...closes);
  const max = Math.max(...closes);
  const span = max - min || 1;
  const points = closes.map((value, index) =>
    `${(index / (closes.length - 1) * width).toFixed(1)},${(height - (value - min) / span * (height - 4) - 2).toFixed(1)}`).join(" ");
  const up = closes[closes.length - 1] >= closes[0];
  return `<svg class="spark ${up ? "up" : "down"}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="90-day price chart"><polyline points="${points}"/></svg>`;
}

export function renderInvestPanel(root, overview, {escapeHtml, formatSgd}) {
  if (!root || !overview) return;
  const total = root.querySelector("#invest-total");
  const holdings = root.querySelector("#invest-holdings");
  const profile = overview.profile;
  total.textContent = overview.holdings.length
    ? `${formatSgd(overview.total_value_minor)} · ${overview.total_gain_minor >= 0 ? "+" : "−"}${formatSgd(Math.abs(overview.total_gain_minor))}`
    : "No holdings yet";
  holdings.innerHTML = [
    ...overview.holdings.map(h => `<div class="holding-row">
        <span><strong>${escapeHtml(h.name)}</strong><small>${escapeHtml(h.quantity)} × ${escapeHtml(formatSgd(h.price_minor))} · ${escapeHtml(h.symbol)}</small></span>
        <span class="holding-value"><strong>${escapeHtml(formatSgd(h.value_minor))}</strong><small class="${h.gain_minor >= 0 ? "gain" : "loss"}">${escapeHtml(pct(h.gain_pct))}</small></span>
      </div>`),
    ...overview.plans.map(p => `<div class="holding-row plan-row">
        <span><strong>${escapeHtml(p.name)}</strong><small>${escapeHtml(formatSgd(p.monthly_minor))} a month · next ${escapeHtml(new Date(p.next_run_at).toLocaleDateString())}</small></span>
        <span class="holding-value"><small>Monthly plan</small></span>
      </div>`),
  ].join("") || `<p class="invest-empty">Ask “What's trending?” or “Suggest a long-term plan” to get started.</p>`;
  root.querySelector("#invest-profile").textContent = profile
    ? `Risk profile: ${profile.risk_label} (${profile.risk_score} of 5) · ${profile.horizon_years}-year horizon`
    : "No risk profile yet: you'll answer four questions before your first investment.";
}

function instrumentCard(item, {escapeHtml, formatSgd}, actions) {
  return `<article class="invest-card">
    <header>
      <div><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.symbol)} · ${escapeHtml(item.kind.replace("_", " "))} · ${escapeHtml(item.sector)}</small></div>
      <div class="invest-price"><strong>${escapeHtml(formatSgd(item.price_minor))}</strong><small class="${item.change_1d_pct >= 0 ? "gain" : "loss"}">${escapeHtml(pct(item.change_1d_pct))} today</small></div>
    </header>
    ${item.why ? `<p class="invest-why">${escapeHtml(item.why)}</p>` : ""}
    <dl class="invest-stats">
      <div><dt>1 month</dt><dd>${escapeHtml(pct(item.change_1m_pct))}</dd></div>
      <div><dt>3 months</dt><dd>${escapeHtml(pct(item.change_3m_pct))}</dd></div>
      <div><dt>1 year</dt><dd>${escapeHtml(pct(item.change_1y_pct))}</dd></div>
      <div><dt>Dividend</dt><dd>${escapeHtml(item.dividend_yield_pct.toFixed(1))}%</dd></div>
      <div><dt>Risk</dt><dd title="${item.risk_level} of 5">${riskDots(item.risk_level)}</dd></div>
    </dl>
    <div class="invest-points">
      <div><p class="points-label">Strengths</p><ul>${item.strengths.map(s => `<li>${escapeHtml(s)}</li>`).join("")}</ul></div>
      <div><p class="points-label risks">Risks</p><ul>${item.risks.map(r => `<li>${escapeHtml(r)}</li>`).join("")}</ul></div>
    </div>
    ${item.fit_note ? `<p class="invest-fit ${item.fits_profile ? "fits" : "above"}">${escapeHtml(item.fit_note)}</p>` : ""}
    <div class="invest-card-actions">${actions}</div>
  </article>`;
}

function planCard(plan, {escapeHtml, formatSgd}) {
  const p = plan.projection_minor;
  return `<article class="invest-card plan-card${plan.best_fit ? " best" : ""}">
    <header>
      <div><strong>${escapeHtml(plan.name)}</strong>${plan.best_fit ? '<b class="payee-flag">Best fit for you</b>' : ""}${plan.fits_profile ? "" : '<b class="payee-flag warn">Above your risk profile</b>'}<small>Risk ${riskDots(plan.risk_level)}</small></div>
    </header>
    <p class="invest-why">${escapeHtml(plan.idea)}</p>
    <div class="alloc-bar">${plan.allocations.map(a => `<span style="flex:${a.percent}" title="${escapeHtml(a.name)} ${a.percent}%">${a.percent}%</span>`).join("")}</div>
    <ul class="alloc-list">${plan.allocations.map(a => `<li>${a.percent}% ${escapeHtml(a.name)} (${escapeHtml(a.symbol)})</li>`).join("")}</ul>
    <p class="projection">${escapeHtml(formatSgd(plan.monthly_minor))} a month for ${plan.years} years: you pay in ${escapeHtml(formatSgd(plan.contributed_minor))}. At ${plan.assumed_returns_pct[1]}% a year that could become about <strong>${escapeHtml(formatSgd(p.middle))}</strong>; at ${plan.assumed_returns_pct[0]}% about ${escapeHtml(formatSgd(p.cautious))}, at ${plan.assumed_returns_pct[2]}% about ${escapeHtml(formatSgd(p.strong))}.</p>
    <div class="invest-card-actions"><button type="button" class="candidate-choice" data-say="invest S$${(plan.monthly_minor / 100).toFixed(0)} a month in the ${escapeHtml(plan.name)} plan">Start with ${escapeHtml(formatSgd(plan.monthly_minor))} a month</button></div>
  </article>`;
}

export function renderInvestingAnswer(list, investing, helpers, runCommand, cancelPlan) {
  const {escapeHtml, formatSgd} = helpers;
  let html = "";
  if (investing.view === "TRENDING") {
    const buy = item => `<button type="button" class="candidate-choice" data-say="buy S$100 of ${escapeHtml(item.name)}">Buy S$100</button>
      <button type="button" class="text-button" data-say="how is ${escapeHtml(item.name)} doing">Price chart</button>`;
    html = `<p class="match-label">Trending: rising over one and three months</p>
      ${investing.trending.map(item => instrumentCard(item, helpers, buy(item))).join("") || "<p>Nothing is rising right now.</p>"}
      <p class="match-label">Steady income within your risk profile</p>
      ${investing.income.map(item => instrumentCard(item, helpers, buy(item))).join("")}`;
  } else if (investing.view === "PLANS") {
    html = `<form class="plan-inputs">
        <label>Monthly <input name="monthly" type="number" min="50" max="10000" step="50" value="${investing.monthly_minor / 100}"></label>
        <label>Years <input name="years" type="number" min="1" max="40" value="${investing.years}"></label>
        <button type="submit" class="text-button">Recalculate</button>
      </form>
      ${investing.plans.map(plan => planCard(plan, helpers)).join("")}
      <p class="invest-note">${escapeHtml(investing.note)}</p>`;
  } else if (investing.view === "QUOTE") {
    const item = investing.instrument;
    html = instrumentCard(item, helpers, `<button type="button" class="candidate-choice" data-say="buy S$100 of ${escapeHtml(item.name)}">Buy S$100</button>
      <button type="button" class="text-button" data-say="buy 10 shares of ${escapeHtml(item.name)}">Buy 10 units</button>`)
      .replace("</header>", `</header>${sparkline(investing.history)}<small class="spark-label">Last 90 days</small>`);
  } else if (investing.view === "PORTFOLIO") {
    html = `<div class="portfolio-summary">
        <span><small>Invested value</small><strong>${escapeHtml(formatSgd(investing.total_value_minor))}</strong></span>
        <span><small>Gain / loss</small><strong class="${investing.total_gain_minor >= 0 ? "gain" : "loss"}">${escapeHtml(formatSgd(investing.total_gain_minor))}</strong></span>
        <span><small>Cash in savings</small><strong>${escapeHtml(formatSgd(investing.cash_minor))}</strong></span>
      </div>
      ${investing.holdings.map(h => `<div class="holding-row">
        <span><strong>${escapeHtml(h.name)}</strong><small>${escapeHtml(h.quantity)} ${h.quantity === "1" ? "unit" : "units"} · ${h.weight_pct}% of portfolio · paid ${escapeHtml(formatSgd(h.cost_minor))}</small></span>
        <span class="holding-value"><strong>${escapeHtml(formatSgd(h.value_minor))}</strong><small class="${h.gain_minor >= 0 ? "gain" : "loss"}">${escapeHtml(pct(h.gain_pct))}</small>
        <button type="button" class="text-button" data-say="sell all my ${escapeHtml(h.name)}">Sell</button></span>
      </div>`).join("")}
      ${investing.plans.map(p => `<div class="holding-row plan-row">
        <span><strong>${escapeHtml(p.name)}</strong><small>${escapeHtml(formatSgd(p.monthly_minor))} a month · next ${escapeHtml(new Date(p.next_run_at).toLocaleDateString())}</small></span>
        <span class="holding-value"><button type="button" class="text-button" data-cancel-plan="${escapeHtml(p.plan_id)}">Stop plan</button></span>
      </div>`).join("")}`;
  }
  list.hidden = false;
  list.innerHTML = `${html}<p class="invest-disclaimer">${escapeHtml(investing.disclaimer || "")}</p>`;
  list.querySelectorAll("[data-say]").forEach(button => button.addEventListener("click", () => runCommand(button.dataset.say)));
  list.querySelectorAll("[data-cancel-plan]").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    await cancelPlan(button.dataset.cancelPlan);
    button.closest(".plan-row").remove();
  }));
  const form = list.querySelector(".plan-inputs");
  form?.addEventListener("submit", async event => {
    event.preventDefault();
    const monthly = Math.round(Number(form.monthly.value) * 100);
    const years = Number(form.years.value);
    const response = await fetch(`/api/invest/plans?monthly=${monthly}&years=${years}`);
    if (response.ok) renderInvestingAnswer(list, {view: "PLANS", ...(await response.json())}, helpers, runCommand, cancelPlan);
  });
}

export function renderOrderCard(list, order, {escapeHtml, formatSgd}, onApprove) {
  const facts = order.plan
    ? order.plan.allocations.map(a => `<span><small>${a.percent}%</small><strong>${escapeHtml(a.name)}</strong></span>`).join("")
    : `<span><small>${order.kind === "BUY" ? "Buy" : "Sell"}</small><strong>${escapeHtml(order.quantity)} × ${escapeHtml(order.instrument.name)}</strong></span>
       <span><small>Quote</small><strong>${escapeHtml(formatSgd(order.quote_price_minor))}</strong></span>
       <span><small>${order.kind === "BUY" ? "Price limit" : "Lowest price"}</small><strong>${escapeHtml(formatSgd(order.limit_price_minor))}</strong></span>
       <span><small>Fee</small><strong>${escapeHtml(formatSgd(order.fee_minor))}</strong></span>`;
  list.hidden = false;
  list.innerHTML = `<article class="receipt-choice order-card">
    <p class="match-label">${escapeHtml(order.plan ? "Monthly investment plan" : "Investment order")} · built from your words, not by the AI</p>
    <div class="receipt-facts">${facts}
      <span><small>${order.kind === "SELL" ? "Paid into savings" : order.plan ? "Each month" : "At most from savings"}</small><strong>${escapeHtml(formatSgd(order.total_minor))}</strong></span>
    </div>
    ${order.risk_ack_required ? `<p class="invest-fit above">Riskier than your profile (${escapeHtml(order.profile?.risk_label || "")}). You'll be asked to confirm you understand.</p>` : ""}
    <button type="button" class="candidate-choice">Review &amp; confirm with your face</button>
  </article>`;
  list.querySelector(".candidate-choice").addEventListener("click", () => onApprove(order));
}

export function openProfileDialog(questions) {
  const dialog = document.querySelector("#profile-dialog");
  const form = dialog.querySelector("#profile-questions");
  const ack = dialog.querySelector("#profile-ack");
  const status = dialog.querySelector("#profile-status");
  const save = dialog.querySelector("#profile-save");
  const cancel = dialog.querySelector("#profile-cancel");
  form.innerHTML = questions.map(q => `<fieldset><legend>${q.text}</legend>
    ${q.options.map((option, index) => `<label><input type="radio" name="${q.id}" value="${index + 1}"> ${option}</label>`).join("")}
  </fieldset>`).join("");
  ack.checked = false;
  status.textContent = "";
  return new Promise(resolve => {
    const close = saved => { save.onclick = null; cancel.onclick = null; dialog.close(); resolve(saved); };
    cancel.onclick = () => close(null);
    dialog.oncancel = event => { event.preventDefault(); close(null); };
    save.onclick = async () => {
      const answers = Object.fromEntries(questions.map(q => [q.id, Number(form.querySelector(`input[name="${q.id}"]:checked`)?.value || 0)]));
      if (Object.values(answers).some(value => !value)) { status.textContent = "Please answer all four questions."; return; }
      if (!ack.checked) { status.textContent = "Please confirm you understand investment risk."; return; }
      const response = await fetch("/api/invest/profile", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({answers, acknowledged: true}),
      });
      if (!response.ok) { status.textContent = "Could not save your profile. Please try again."; return; }
      close(await response.json());
    };
    dialog.showModal();
  });
}
