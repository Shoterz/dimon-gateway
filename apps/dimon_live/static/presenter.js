// Presenter view: the customer's phone on the left, and on the right a plain
// reading of this session's hash-chained audit log — what the AI proposed,
// what the independent checker said, and what the gateway accepted or refused.
const list = document.getElementById("events");
const chainLabel = document.getElementById("chain");
const phone = document.getElementById("phone");
let lastSequence = 0;
const seenReceipts = new Set();
const heldValidations = new Map();
const lastVerdict = new Map();

const money = minor => `S$${(minor / 100).toLocaleString("en-SG", {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
const words = code => String(code || "").replaceAll("_", " ").toLowerCase();
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[ch]);

function add(kind, who, what, detail, {turn = false} = {}) {
  list.querySelector(".empty")?.remove();
  const item = document.createElement("li");
  item.className = `${kind}${turn ? " turn" : ""}`;
  item.innerHTML = `<span class="who">${escapeHtml(who)}</span><div class="what"><strong>${escapeHtml(what)}</strong>${detail ? `<small>${escapeHtml(detail)}</small>` : ""}</div>`;
  list.append(item);
  list.scrollTop = list.scrollHeight;
}

function describeProposal(payload) {
  const intent = payload.validated_intent;
  if (!intent) {
    if (!payload.raw_model_output) return null;
    return ["Proposal did not match the contract", "Rejected by schema validation"];
  }
  if (intent.kind === "BENEFICIARY_TRANSFER_BATCH") {
    return [`${(intent.items || []).length} payments: ${(intent.items || []).map(item => item.recipient_label).join(", ")}`,
      "Names only. Amounts are never taken from the AI; the gateway reads them from your words."];
  }
  if (intent.kind === "PAYNOW_MOBILE_TRANSFER") {
    return [`PayNow to ${intent.mobile_proxy || "a mobile number"}`, "The gateway looks up the registered name itself."];
  }
  return [`Pay ${intent.recipient_label || "someone"}${intent.payment_reference ? ` for ${intent.payment_reference}` : ""}`,
    "Names only. Amounts are never taken from the AI; the gateway reads them from your words."];
}

function showValidation(event) {
  const report = event.payload;
  const problems = (report.fields || []).filter(field => field.verdict !== "MATCH");
  const stage = event.event_type.replace("VALIDATION_", "").toLowerCase();
  const verdict = {PASS: "Everything matches what you said", ATTENTION: "Needs your confirmation", FROZEN: "Frozen: the draft contradicts your words"}[report.verdict] || report.verdict;
  add(report.verdict === "FROZEN" ? "refused" : "check", `Checker · ${stage}`, verdict,
    problems.map(field => `${field.field.replace(/^items\[\d+\]\./, "")}: ${field.note} (draft ${field.draft_value}, heard ${field.observed})`).join(" · "));
}

function showRequest(event) {
  const p = event.payload;
  add("user", p.source === "voice" ? "You said" : "You typed", `“${p.text}”`, null, {turn: true});
  if (p.reason_code === "PROMPT_INJECTION_BLOCKED") {
    add("refused", "Firewall", "Blocked before the AI saw it", `Detected: ${(p.firewall?.categories || []).map(words).join(", ")}`);
    return;
  }
  if (p.reason_code === "SCAM_SUSPECTED") {
    add("refused", "Scam check", "Held: this looks like a scam", "Nothing drafted. The AI can only make rules stricter, never looser.");
    return;
  }
  if (p.outcome === "ANSWERED") {
    const answers = {
      BALANCE_INQUIRY: "Balance read, no approval needed",
      MONEY_REQUEST_SENT: "Payment request recorded, no approval needed",
      INVESTMENT_PORTFOLIO: "Portfolio shown",
      INVESTMENT_TRENDING: "Trending investments suggested by fixed rules, with risks",
      INVESTMENT_PLANS: "Long-term plans matched to the risk profile",
      INVESTMENT_QUOTE: "Price quote shown",
    };
    add("gateway", "Gateway", answers[p.reason_code] || words(p.reason_code),
      "Nothing leaves the account, so the AI is not used and no signature is asked for.");
    return;
  }
  if (p.outcome === "ORDER_CONFIRMATION_REQUEST") {
    add("gateway", "Gateway drafted", "Investment order built from your words", "Fixed rules, no AI. Waiting for the face and device confirmation.");
    return;
  }
  const proposal = describeProposal(p);
  if (proposal) add("ai", "AI proposed", proposal[0], proposal[1]);
  const receipts = [...(p.receipts || []), ...(p.batch_receipt ? [p.batch_receipt] : [])];
  for (const receipt of receipts) {
    seenReceipts.add(receipt.receipt_id);
    for (const validation of heldValidations.get(receipt.receipt_id) || []) showValidation(validation);
    heldValidations.delete(receipt.receipt_id);
  }
  if ((p.receipts || []).length || p.batch_receipt) {
    const drafted = p.batch_receipt
      ? `Batch receipt for ${money(p.batch_receipt.total_minor)} drafted`
      : (p.receipts || []).map(r => `${r.recipient_display_name} ${money(r.amount_minor)}`).join(" or ");
    if (p.outcome === "BLOCKED") {
      add("refused", "Gateway refused", words(p.reason_code), "The receipt can never be signed or executed.");
    } else {
      add("gateway", "Gateway drafted", p.batch_receipt ? drafted : `Receipt: ${drafted}`, "Waiting for the owner's device signature. Nothing has moved.");
    }
  } else if (p.outcome === "BLOCKED" || p.outcome === "FAILED") {
    add("refused", "Gateway refused", words(p.reason_code), "Nothing drafted, nothing moved.");
  } else if (p.reason_code) {
    add("gateway", "Gateway asked", words(p.reason_code), "It asks instead of guessing.");
  }
}

function show(event) {
  const p = event.payload || {};
  switch (event.event_type) {
    case "REQUEST_DRAFTED":
      showRequest(event);
      break;
    case "VALIDATION_DRAFT":
    case "VALIDATION_CHALLENGE":
    case "VALIDATION_EXECUTION":
      // The checker re-reads the receipt before signing and again before
      // execution; repeat it only when its verdict changes.
      if (event.event_type !== "VALIDATION_DRAFT" && lastVerdict.get(event.receipt_id) === p.verdict) break;
      lastVerdict.set(event.receipt_id, p.verdict);
      if (seenReceipts.has(event.receipt_id) || event.event_type !== "VALIDATION_DRAFT") showValidation(event);
      else heldValidations.set(event.receipt_id, [...(heldValidations.get(event.receipt_id) || []), event]);
      break;
    case "CHALLENGE_ISSUED":
      if (!event.receipt_id) {
        add("gateway", "Gateway", "Asked this device to create its approval key", "Registered during identity onboarding.");
      } else {
        add("gateway", "Gateway", "Asked the registered device to sign this exact receipt", "The signature covers the receipt's digest, so it cannot be moved to another payment.");
      }
      break;
    case "FACE_REGISTERED":
      add("gateway", "Face ID", "Face registered to the account", "One face per account. Stored as a template in its own database, never as a photo.");
      break;
    case "FACE_REGISTRATION_REJECTED":
    case "FACE_SCAN_REJECTED":
      add("refused", "Face ID", `Face scan refused: ${words(p.reason_code)}`, p.similarity != null ? `Similarity ${Math.round(p.similarity * 100)}%` : "Nothing can be signed without the registered face.");
      break;
    case "FACE_SCAN_MATCHED":
      add("gateway", "Face ID", `Face matched (${Math.round((p.similarity || 0) * 100)}%)`, "Live head turn, bound to this exact receipt's challenge.");
      break;
    case "FACE_MATCH_CONSUMED":
      add("gateway", "Gateway", "Face match spent inside execution", "It cannot be reused for another payment.");
      break;
    case "DEVICE_SIGNATURE_VERIFIED":
      add("gateway", "Gateway", "Device signature verified", p.credential_kind ? `Signed with ${words(p.credential_kind)}` : null);
      break;
    case "APPROVAL_REFUSED":
      add("refused", "Gateway refused", words(p.reason_code || p.reason), "No money moved.");
      break;
    case "LEDGER_COMMITTED":
      add("gateway", "Gateway accepted", p.total_minor != null ? `Batch settled: ${money(p.total_minor)}` : `Settled: ${money(p.amount_minor || 0)}`, "Written once, balanced, and chained into the audit log.");
      break;
    case "MONEY_REQUESTED":
      break;
    case "SINGPASS_AUTHORISATION_REQUESTED":
      add("gateway", "Singpass", "Authorisation sent to the Singpass app", "For this exact order only; valid 5 minutes, usable once.");
      break;
    case "SINGPASS_APPROVED":
      add("gateway", "Singpass", "Approved in the Singpass app", "Signed by Singpass's own key.");
      break;
    case "SINGPASS_REJECTED":
      add("refused", "Singpass", "Rejected in the Singpass app", "Nothing can be bought or sold.");
      break;
    case "SINGPASS_AUTHORISATION_CONSUMED":
      add("gateway", "Gateway", "Singpass approval verified and spent inside execution", `Identity ${p.uinfin_masked}.`);
      break;
    case "INVESTOR_PROFILE_SAVED":
      add("gateway", "Gateway", `Investor profile saved: risk ${p.risk_score} of 5`, `${p.horizon_years}-year horizon decides which suggestions fit.`);
      break;
    case "TRADE_EXECUTED":
      add("gateway", "Gateway accepted", `${p.side === "BUY" ? "Bought" : "Sold"} ${p.symbol} at ${money(p.price_minor)}`, "Filled within the signed price limit; cash and holdings changed together.");
      break;
    case "INVESTMENT_PLAN_STARTED":
      add("gateway", "Gateway accepted", `Monthly plan started: ${p.name}`, `${money(p.monthly_minor)} a month; first instalment invested now.`);
      break;
    case "PAYEE_SAVED":
      add("gateway", "Gateway", "New payee saved", "S$100 cap and one-time code for the first 24 hours.");
      break;
    case "MYINFO_RETRIEVED":
      add("gateway", "Singpass Myinfo", "Identity details retrieved with consent", null, {turn: true});
      break;
    case "KYC_VERIFIED_DEVICE_BOUND":
      add("gateway", "Gateway", "Identity verified; this device is now the only one that can approve", null);
      break;
    case "DEMO_RESET":
      add("user", "Reset", "Ledger restored to its starting balances", null, {turn: true});
      break;
    default:
      break;
  }
}

async function poll() {
  try {
    const response = await fetch("/api/audit?limit=200", {cache: "no-store"});
    if (response.ok) {
      const body = await response.json();
      const fresh = (body.events || []).filter(event => event.sequence > lastSequence).sort((a, b) => a.sequence - b.sequence);
      for (const event of fresh) {
        show(event);
        lastSequence = event.sequence;
      }
      const intact = body.chain?.valid;
      chainLabel.className = `chain ${intact === false ? "broken" : "ok"}`;
      chainLabel.textContent = intact === false ? "Audit chain broken" : `Audit chain intact · ${body.chain?.events ?? lastSequence} entries`;
    }
  } catch {
    chainLabel.className = "chain broken";
    chainLabel.textContent = "Gateway unreachable";
  }
  setTimeout(poll, 1000);
}

document.getElementById("tools").addEventListener("click", event => {
  const button = event.target.closest("button[data-say]");
  if (!button) return;
  const doc = phone.contentDocument;
  if (!doc) return;
  const restart = doc.getElementById("new-request-button");
  if (restart && !restart.hidden) restart.click();
  const input = doc.getElementById("command-input");
  const form = doc.getElementById("command-form");
  if (!input || !form || input.disabled) return;
  input.value = button.dataset.say;
  input.dispatchEvent(new Event("input", {bubbles: true}));
  form.requestSubmit();
});

poll();
