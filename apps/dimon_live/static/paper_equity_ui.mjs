import {formatFundingSource, formatTransactionalMoney} from "./transaction_display.mjs";

// Renderer and endpoint helpers for real-market paper orders.  It deliberately
// keeps the quote currency and funding currency separate; only the server may
// supply the refreshed price, FX rate, fee, or lifecycle state.

export function renderPaperEquityOrder(list, order, {escapeHtml}, onApprove) {
  const quote = order.quote_snapshot || {};
  const fx = order.fx_snapshot || {};
  const quoteCurrency = order.instrument?.quote_currency || "USD";
  const fundingCurrency = order.funding_currency || "SGD";
  const selling = order.side === "SELL";
  const shares = `${order.quantity} ${Number(order.quantity) === 1 ? "share" : "shares"}`;
  const limit = order.order_type === "LIMIT" && order.limit_price_minor != null
    ? `<span><small>${selling ? "Minimum price" : "Maximum price"}</small><strong>${escapeHtml(formatTransactionalMoney(quoteCurrency, order.limit_price_minor))}</strong></span>`
    : `<span><small>Execution</small><strong>Market price</strong></span>`;
  const cash = selling
    ? `<span><small>Estimated proceeds</small><strong>${escapeHtml(formatTransactionalMoney(fundingCurrency, Math.max(0, Number(order.estimated_funding_minor || 0) - Number(order.fee_funding_minor || 0))))}</strong></span>`
    : `<span><small>${order.order_type === "MARKET" ? "Estimated debit" : "Maximum funding debit"}</small><strong>${escapeHtml(formatTransactionalMoney(fundingCurrency, order.max_funding_debit_minor))}</strong></span>`;
  const note = selling
    ? "Proceeds are paid into your savings after the fee, at the live price when the order fills."
    : order.order_type === "MARKET" ? "Final debit reflects the live fill price and is checked against your available balance." : `FX ${escapeHtml(fx.pair || "—")} ${escapeHtml(fx.rate || "—")}; available balance is checked again before settlement.`;
  list.hidden = false;
  list.innerHTML = `<article class="receipt-choice paper-equity-order-card">
    <p class="match-label">${selling ? "Stock sale" : "Stock purchase"} · review details</p>
    <header><div><strong>${escapeHtml(order.instrument?.name || order.instrument?.symbol || "Equity")}</strong><small>${escapeHtml(order.instrument?.symbol || "")} · ${escapeHtml(order.instrument?.exchange || "")}</small></div>
      <strong>${escapeHtml(formatTransactionalMoney(quoteCurrency, quote.price_minor))}</strong></header>
    <div class="receipt-facts">
      <span><small>${selling ? "Sell" : "Buy"}</small><strong>${escapeHtml(shares)}</strong></span>
      <span><small>Order</small><strong>${escapeHtml(`${order.order_type === "LIMIT" ? "Limit" : "Market"} · ${order.time_in_force === "GTC" ? "until cancelled" : "today"}`)}</strong></span>
      ${limit}
      ${cash}
      <span><small>Funding source</small><strong>${escapeHtml(formatFundingSource(order))}</strong></span>
    </div>
    <p class="invest-note">${note}</p>
    <p class="invest-note">Receipt ${escapeHtml(String(order.digest || "").slice(0, 16))}… · expires ${escapeHtml(order.approval_expires_at || "")}</p>
    <button type="button" class="candidate-choice">Review and approve on screen</button>
  </article>`;
  const button = list.querySelector(".candidate-choice");
  let busy = false;
  button.addEventListener("click", async () => {
    if (busy) return;
    busy = true;
    button.disabled = true;
    button.textContent = "Opening approval…";
    try {
      await onApprove(order);
    } finally {
      if (button.isConnected) {
        busy = false;
        button.disabled = false;
        button.textContent = "Review and approve on screen";
      }
    }
  });
}

export function requestPaperEquityApproval(order, signed, fetchImpl = globalThis.fetch) {
  return fetchImpl(`/api/paper-equity/orders/${encodeURIComponent(order.order_id)}/approve`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({receipt_digest: order.digest, ...signed}),
  }).then(async response => response.ok
    ? {kind: "RESULT", result: await response.json()}
    : {kind: "UNKNOWN", message: `Gateway returned HTTP ${response.status}`})
    .catch(error => ({kind: "UNKNOWN", message: error instanceof Error ? error.message : String(error)}));
}

export function refreshPaperEquityOrder(order, fetchImpl = globalThis.fetch) {
  return fetchImpl(`/api/paper-equity/orders/${encodeURIComponent(order.order_id)}/refresh`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({receipt_digest: order.digest}),
  }).then(response => response.json());
}

export function cancelPaperEquityOrder(order, fetchImpl = globalThis.fetch) {
  return fetchImpl(`/api/paper-equity/orders/${encodeURIComponent(order.order_id)}/cancel`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({receipt_digest: order.digest}),
  }).then(response => response.json());
}
