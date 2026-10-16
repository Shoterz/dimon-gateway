import {formatFundingSource, formatTransactionalMoney} from "./transaction_display.mjs";

const MULTI_ACTION_CLARIFICATION_REASONS = new Set([
  "MULTI_ACTION_EVIDENCE_INVALID",
  "MULTI_ACTION_ITEM_UNRESOLVED",
  "MULTI_ACTION_CHILD_PREPARATION_FAILED",
]);

export function multiActionClarificationPresentation(result) {
  if (result?.outcome !== "CLARIFICATION_REQUEST" || !MULTI_ACTION_CLARIFICATION_REASONS.has(result?.reason_code)) {
    return null;
  }
  const context = result.multi_action_clarification;
  if (context && Array.isArray(context.issues)) {
    const labels = {
      BILL_PAYMENT: "Bill payment",
      BENEFICIARY_TRANSFER: "Payment",
      STOCK_INVESTMENT: "Stock purchase",
    };
    const checklist = [
      ...(context.resolved_items || []).map(item => ({
        label: labels[item.kind] || "Requested action", status: "Understood",
      })),
      ...context.issues.map(issue => ({
        label: issue.field === "recipient" ? "Recipient"
          : issue.field === "instrument" ? "Stock" : "Requested detail",
        status: "Needs clarification",
      })),
    ];
    const understood = (context.resolved_items || []).map(item => {
      const evidence = String(item.evidence || "").toLowerCase();
      if (item.kind === "BILL_PAYMENT" && evidence.includes("water")) return "the water bill";
      return (labels[item.kind] || "one requested action").toLowerCase();
    });
    return {
      title: context.issues.length === 1
        ? "One detail needs clarification"
        : `${context.issues.length === 2 ? "Two" : context.issues.length} details need clarification`,
      question: result.answer || context.issues.map(issue => issue.question).join(" "),
      detail: understood.length
        ? `I understood ${understood.join(" and ")}. No payment or order has been prepared.`
        : "No payment or order has been prepared.",
      checklist,
    };
  }
  const recipientMissing = result.reason_code === "MULTI_ACTION_ITEM_UNRESOLVED"
    && String(result.answer || "").toLowerCase().includes("recipient");
  return {
    title: "One request needs clarification",
    question: recipientMissing ? "Who should receive the repayment?" : "Which part should I clarify?",
    detail: recipientMissing
      ? "Please say the whole request again with the recipient’s saved name. Nothing has been prepared or sent."
      : "Please say the whole request again with the missing detail. Nothing has been prepared or sent.",
  };
}

export function renderMultiActionReview(items, escapeHtml, formatMoney = value => formatTransactionalMoney("SGD", value)) {
  if (!Array.isArray(items) || items.length < 2 || items.length > 3) return "";
  const labels = {
    BILL_PAYMENT: "Bill payment",
    BENEFICIARY_TRANSFER: "Payment to a saved recipient",
    PAYNOW_MOBILE_TRANSFER: "PayNow payment",
    STOCK_INVESTMENT: "Stock purchase",
  };
  return `<section class="multi-action-review" aria-label="Grouped action review">
    ${items.map((item, index) => {
      const result = item.result || {};
      const choices = result.receipt_choices || [];
      const batch = result.batch_receipt;
      const paper = result.paper_order;
      const choice = choices[0];
      const details = choice
        ? `<div class="receipt-facts"><span><small>${choice.destination_kind === "UTILITY_BILL" ? "Biller" : "To"}</small><strong>${escapeHtml(choice.recipient_display_name || "")}</strong></span><span><small>From</small><strong>${escapeHtml(formatFundingSource(choice))}</strong></span><span><small>Amount</small><strong>${escapeHtml(formatTransactionalMoney(choice.currency || "SGD", choice.amount_minor))}</strong></span></div>`
        : batch
          ? `<p>${escapeHtml(String(batch.items?.length || 0))} recipients · ${escapeHtml(formatTransactionalMoney(batch.currency || "SGD", batch.total_minor))}</p>`
          : paper
            ? `<div class="receipt-facts"><span><small>Investment</small><strong>${escapeHtml(paper.instrument?.name || paper.instrument?.symbol || "Stock order")}</strong></span><span><small>Shares</small><strong>${escapeHtml(String(paper.quantity ?? "—"))}</strong></span><span><small>Quote</small><strong>${escapeHtml(formatTransactionalMoney(paper.instrument?.quote_currency || "USD", paper.quote_snapshot?.price_minor))} each</strong></span><span><small>Estimated debit</small><strong>${escapeHtml(formatTransactionalMoney(paper.funding_currency || "SGD", paper.max_funding_debit_minor))}</strong></span></div>`
            : `<p>${escapeHtml(String(result.answer || result.question || item.evidence || ""))}</p>`;
      const status = item.status === "AWAITING_APPROVAL" ? "Review required" : String(item.status || "Prepared").replaceAll("_", " ");
      const canApprove = item.status === "AWAITING_APPROVAL";
      const button = choice && canApprove
        ? `<button type="button" class="candidate-choice multi-action-approve" data-multi-index="${index}" data-child-type="receipt" data-child-id="${escapeHtml(choice.receipt_id || "")}">Review ${item.kind === "BILL_PAYMENT" ? "bill" : "payment"}</button>`
        : batch && canApprove
          ? `<button type="button" class="candidate-choice multi-action-approve" data-multi-index="${index}" data-child-type="batch_receipt" data-child-id="${escapeHtml(batch.receipt_id || "")}">Review payments</button>`
          : paper && canApprove
            ? `<button type="button" class="candidate-choice multi-action-approve" data-multi-index="${index}" data-child-type="paper_order" data-child-id="${escapeHtml(paper.order_id || "")}">Review stock order</button>`
          : "";
      return `<article class="multi-action-item" data-multi-item="${index}">
        <div class="multi-action-item-heading"><span class="multi-action-number">${index + 1}</span><div><strong>${escapeHtml(labels[item.kind] || "Requested action")}</strong><span class="multi-action-status">${escapeHtml(status)}</span></div></div>
        ${details}${button}
      </article>`;
    }).join("")}
  </section>`;
}
