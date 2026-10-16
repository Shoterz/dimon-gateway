const PAYMENT_REASONS = new Set([
  "ACTION_CONFIRMATION_REQUIRED",
  "AMBIGUOUS_RECIPIENT",
  "AMOUNT_CLARIFICATION_COMPLETED",
  "BATCH_CONFIRMATION_REQUIRED",
  "MISSING_AMOUNT",
  "PAYNOW_CONFIRMATION_REQUIRED",
  "PHONETIC_RECIPIENT_MATCH",
  "SIGNED_APPROVAL_REQUIRED",
]);

const ACCOUNT_REASONS = new Set(["BALANCE_INQUIRY", "ACCOUNT_INFORMATION"]);
const REQUEST_REASONS = new Set(["SERVICE_REQUEST", "REQUEST_ANSWERED", "MONEY_REQUEST_SENT"]);
const INVESTING_VIEWS = new Set(["TRENDING", "PLANS", "PORTFOLIO", "QUOTE", "REAL_STOCK_BRIEF"]);
const STARTER_INTENTS = Object.freeze({
  AFFORDABILITY: "affordability",
  PAYMENT: "payment",
  INVESTMENTS: "investments",
});
const VIEW_NAMES = new Set(["home", "pay", "ask", "activity", "wealth", "safety", "evidence"]);

export function resolveStarterIntent(value) {
  return STARTER_INTENTS[String(value || "").toUpperCase()] || null;
}

export function viewNameFromHash(hash) {
  const value = String(hash || "").replace(/^#/, "").trim().toLowerCase();
  return VIEW_NAMES.has(value) ? value : null;
}

export function renderHomeSummary(state = {}, helpers, investedMinor = 0) {
  const {escapeHtml, formatSgd} = helpers;
  const accounts = Array.isArray(state.accounts)
    ? state.accounts.filter(account => Boolean(account.is_user_source))
    : [];
  const transactions = Array.isArray(state.transactions) ? state.transactions : [];
  const totalMinor = accounts.reduce(
    (total, account) => total + Number(account.balance_minor || 0),
    0,
  );
  const walletMinor = 62;
  const accountsHtml = accounts.length
    ? accounts.map(account => `
      <article class="bank-account-card">
        <div>
          <p class="bank-account-type">${escapeHtml(account.label || "Account")}</p>
          <p class="bank-account-number">Synthetic account ··${escapeHtml(String(account.account_id || "0000").slice(-4))}</p>
        </div>
        <strong>${formatSgd(Number(account.balance_minor || 0))}</strong>
      </article>`).join("")
    : '<div class="bank-empty-state"><strong>No accounts are available</strong><span>Refresh the demo session to try again.</span></div>';
  const activityHtml = transactions.length
    ? transactions.slice(0, 6).map(transaction => `
      <article class="activity-row">
        <span class="activity-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none"><path d="M7 17 17 7M9 7h8v8"/></svg>
        </span>
        <div>
          <strong>${escapeHtml(transaction.destination_label || "Transfer")}</strong>
          <span>From ${escapeHtml(transaction.source_label || "account")}</span>
        </div>
        <strong>−${formatSgd(Number(transaction.amount_minor || 0))}</strong>
      </article>`).join("")
    : '<div class="bank-empty-state"><strong>No recent activity</strong><span>Approved synthetic transactions will appear here.</span></div>';

  const portfolioMinor = Number.isFinite(Number(investedMinor))
    ? Math.max(0, Number(investedMinor))
    : 0;
  return {
    totalMinor,
    walletMinor,
    investedMinor: portfolioMinor,
    wealthMinor: totalMinor + walletMinor + portfolioMinor,
    accountsHtml,
    activityHtml,
  };
}

export function classifyActionResult(result = {}) {
  const reason = String(result.reason_code || "");
  const intentType = String(result.intent_type || result.action?.intent_type || "");
  if (result.bill_payment || result.bill_payment_resolution) return "payments";
  if (result.investing && !INVESTING_VIEWS.has(String(result.investing.view || ""))) return "unknown";
  if (result.investing || result.order || result.paper_order || reason.includes("STOCK") || reason.includes("INVEST")) {
    return "investing";
  }
  if (PAYMENT_REASONS.has(reason) || result.receipt || result.batch_receipt) return "payments";
  if (ACCOUNT_REASONS.has(reason) || intentType === "ACCOUNT_INFORMATION") return "account";
  if (REQUEST_REASONS.has(reason) || intentType === "SERVICE_REQUEST") return "request";
  return "unknown";
}

export function activateView(root, viewName, {focus = true} = {}) {
  const target = root.querySelector(`#${viewName}-view`)
    || root.querySelector(`[data-view="${viewName}"]`);
  if (!target) return false;

  root.querySelectorAll("[data-view]").forEach(view => {
    const inactive = view !== target;
    view.hidden = inactive;
    if (inactive) view.setAttribute?.("hidden", "");
    else view.removeAttribute?.("hidden");
  });
  // Keep both the DOM property and markup attribute in sync. The attribute is
  // what the browser renders from, while the property is what callers inspect.
  target.hidden = false;
  target.removeAttribute?.("hidden");
  root.querySelectorAll("[data-view-target]").forEach(button => {
    if (button.dataset.viewTarget === viewName) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  const topbarTitle = root.querySelector("#topbar-view-title");
  if (topbarTitle) {
    topbarTitle.textContent = viewName === "wealth" ? "digiWealth" : "";
    topbarTitle.hidden = viewName !== "wealth";
  }
  if (focus) target.querySelector("h1, h2, [data-view-heading]")?.focus();
  return true;
}

export function openEvidence(root, {receiptId, outcome = "", focusTarget = ""} = {}) {
  if (!root) return false;
  const evidence = root.querySelector("#evidence-view")
    || root.querySelector('[data-view="evidence"]');
  if (!evidence || !activateView(root, "evidence", {focus: false})) return false;

  const receiptTargets = Array.from(root.querySelectorAll?.("[data-evidence-receipt]") || []);
  const matchingReceipt = receiptId == null ? null : receiptTargets.find(target =>
    String(target.dataset?.evidenceReceipt || "") === String(receiptId));
  const validation = root.querySelector("#evidence-validation")
    || root.querySelector("[data-evidence-validation]");
  const trace = root.querySelector("#trace-list")
    || root.querySelector("[data-evidence-trace]");
  const heading = root.querySelector("#evidence-title")
    || evidence.querySelector?.("h1, h2, [data-view-heading]");
  const requested = {
    validation,
    trace,
    heading,
  }[String(focusTarget || "").toLowerCase()];
  const target = matchingReceipt || requested || validation || trace || heading;
  if (!target) return false;

  root.querySelectorAll?.(".evidence-focus").forEach(node => node.classList?.remove("evidence-focus"));
  target.classList?.add("evidence-focus");
  target.focus?.();
  return true;
}

export function submitFromComposer(form, input, source = {}) {
  const text = typeof input?.value === "string" ? input.value.trim() : "";
  if (!text || !source.targetInput || !source.targetForm) return false;
  if (source.canSubmit && !source.canSubmit()) {
    source.onBlocked?.();
    return false;
  }
  source.targetInput.value = text;
  source.activate?.();
  source.targetForm.requestSubmit();
  form?.reset?.();
  return true;
}
