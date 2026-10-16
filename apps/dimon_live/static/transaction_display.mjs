// Display-only helpers for money that is part of a transaction or approval.
// Callers still own HTML escaping because these helpers return plain text.

function currencyPrefix(currency) {
  const code = String(currency || "ISO").trim().toUpperCase() || "ISO";
  if (code === "SGD") return "S$";
  if (code === "USD") return "US$";
  return `${code} `;
}

export function formatTransactionalMoney(currency, minor) {
  const prefix = currencyPrefix(currency);
  if (minor == null || (typeof minor === "string" && !minor.trim())) return `${prefix}—`;
  const value = typeof minor === "number" ? minor : Number(minor);
  if (!Number.isFinite(value)) return `${prefix}—`;
  return `${prefix}${(value / 100).toLocaleString("en-SG", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function formatFundingSource(source = {}) {
  const rawLabel = source.source_label
    || source.funding_account_label
    || source.source_account_label;
  const identifier = source.funding_account_id || source.source_account_id;
  const fallbackLabel = String(identifier || "").toLowerCase().includes("savings")
    ? "Savings"
    : "Selected account";
  const label = String(rawLabel || fallbackLabel).trim() || "Selected account";
  const last4 = source.source_last4 ?? source.account_last4;
  const digits = last4 == null ? "" : String(last4).trim();
  const displayLabel = label[0] ? `${label[0].toUpperCase()}${label.slice(1)}` : "Selected account";
  return digits ? `${displayLabel} · •••• ${digits}` : displayLabel;
}
