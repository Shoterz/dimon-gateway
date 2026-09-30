import { requestReceiptApproval } from "./receipt_approval.mjs";
import { requestBatchReceiptApproval } from "./batch_receipt_approval.mjs";
import { requestClarificationResponse } from "./clarification_response.mjs";
import { requestAction } from "./action_request.mjs";
import { accountDisplay } from "./account_display.mjs";
import { DeviceApprover, wireAssertion } from "./device_approval.mjs";
import { captureHeadTurn, faceMessages, openCamera, postFrames } from "./face_capture.mjs";
import { openProfileDialog, renderInvestPanel, renderInvestingAnswer, renderOrderCard, requestOrderApproval } from "./invest_ui.mjs";
import {
  createVoiceAudioContext,
  VoiceDeadline,
  VoiceFrameResampler,
} from "./voice_capture.mjs";
import {describeVoiceFailure, reportVoiceFailure} from "./voice_failure.mjs";

const ui = {
  accountList: document.querySelector("#account-list"),
  form: document.querySelector("#command-form"),
  input: document.querySelector("#command-input"),
  runButton: document.querySelector("#run-button"),
  commandLabel: document.querySelector("#command-label"),
  commandHint: document.querySelector("#command-hint"),
  newRequestButton: document.querySelector("#new-request-button"),
  resetButton: document.querySelector("#reset-button"),
  outcomePanel: document.querySelector("#outcome-panel"),
  outcomeIndex: document.querySelector("#outcome-index"),
  outcomeKicker: document.querySelector("#outcome-kicker"),
  outcomeTitle: document.querySelector("#outcome-title"),
  outcomeDetail: document.querySelector("#outcome-detail"),
  resultAudio: document.querySelector("#result-audio"),
  candidateList: document.querySelector("#candidate-list"),
  traceList: document.querySelector("#trace-list"),
  elapsedLabel: document.querySelector("#elapsed-label"),
  ledgerBody: document.querySelector("#ledger-body"),
  commitSummary: document.querySelector("#commit-summary"),
  commitNote: document.querySelector("#commit-note"),
  commitStatus: document.querySelector("#commit-status"),
  healthIndicator: document.querySelector("#health-indicator"),
  healthText: document.querySelector("#health-text"),
  inferenceModeBadge: document.querySelector("#inference-mode-badge"),
  modelLabel: document.querySelector("#model-label"),
  voiceButton: document.querySelector("#voice-button"),
  voiceStatus: document.querySelector("#voice-status"),
  approvalDialog: document.querySelector("#approval-dialog"),
  approvalItems: document.querySelector("#approval-items"),
  approvalSource: document.querySelector("#approval-source"),
  approvalTotal: document.querySelector("#approval-total"),
  approvalDigest: document.querySelector("#approval-digest"),
  approvalMethod: document.querySelector("#approval-method"),
  approvalConfirm: document.querySelector("#approval-confirm"),
  approvalCancel: document.querySelector("#approval-cancel"),
  approvalValidation: document.querySelector("#approval-validation"),
  approvalOob: document.querySelector("#approval-oob"),
  approvalOobCode: document.querySelector("#approval-oob-code"),
  approvalReadBack: document.querySelector("#approval-readback"),
  approvalReadBackText: document.querySelector("#approval-readback-text"),
  approvalReadBackPlay: document.querySelector("#approval-readback-play"),
  approvalUnusual: document.querySelector("#approval-unusual"),
  approvalUnusualCheck: document.querySelector("#approval-unusual-check"),
  approvalUnusualText: document.querySelector("#approval-unusual-text"),
};

let previousBalances = new Map();
let activeClarification = null;
let pendingClarificationResponse = null;
let pendingActionRequest = null;
let voiceSocket = null;
let voiceContext = null;
let voiceStream = null;
let voiceNode = null;
let voiceSource = null;
let voiceMuted = null;
let voiceResampler = null;
let voiceWorkletContext = null;
let voiceReady = false;
let voiceTerminal = false;
let voicePhase = "idle";
let voiceFinishing = false;
let voiceStage = "audio_context";
let lastRequestWasSpoken = false;
const voiceDeadline = new VoiceDeadline();
let audioPollGeneration = 0;
const escapeHtml = value => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");
const formatSgd = minor => new Intl.NumberFormat("en-SG", {
  style: "currency", currency: "SGD",
}).format(minor / 100);

function renderAccounts(accounts) {
  ui.accountList.innerHTML = accounts.map(account => {
    const changed = previousBalances.has(account.account_id)
      && previousBalances.get(account.account_id) !== account.balance_minor;
    const classes = ["account-card", account.is_user_source ? "source" : "", changed ? "changed" : ""].filter(Boolean).join(" ");
    const display = accountDisplay(account);
    return `<article class="${classes}">
      <div>
        <p class="account-owner">${escapeHtml(display.ownerLabel)}</p>
        <p class="account-label">${escapeHtml(account.label)}</p>
        ${display.proxyLabel ? `<p class="account-proxy">${escapeHtml(display.proxyLabel)}</p>` : ""}
      </div>
      <p class="account-balance">${formatSgd(account.balance_minor)}</p>
    </article>`;
  }).join("");
  previousBalances = new Map(accounts.map(account => [account.account_id, account.balance_minor]));
}

function renderLedger(transactions) {
  if (!transactions.length) {
    ui.ledgerBody.innerHTML = '<tr class="empty-row"><td colspan="4">No committed transfers yet.</td></tr>';
    return;
  }
  ui.ledgerBody.innerHTML = transactions.map(transaction => `<tr>
    <td>${escapeHtml(transaction.source_label)} → ${escapeHtml(transaction.destination_label)}</td>
    <td>${formatSgd(transaction.amount_minor)}</td>
    <td><span class="state-pill">Committed</span></td>
    <td class="transaction-id">${escapeHtml(transaction.transaction_id.slice(0, 14))}…</td>
  </tr>`).join("");
}

const investHelpers = {escapeHtml: value => escapeHtml(value), formatSgd: minor => formatSgd(minor)};
let lastCommandText = "";

function runCommand(text) {
  resetCommandMode();
  ui.input.value = text;
  ui.form.requestSubmit();
}

async function loadInvestments() {
  try {
    const response = await fetch("/api/invest", {cache: "no-store"});
    if (response.ok) renderInvestPanel(document.querySelector("#invest-board"), await response.json(), investHelpers);
  } catch (_error) {
    // The panel simply keeps its last values.
  }
}

async function cancelPlan(planId) {
  await fetch(`/api/invest/plans/${encodeURIComponent(planId)}/cancel`, {method: "POST"});
  await loadInvestments();
}

function renderState(state) {
  renderAccounts(state.accounts || []);
  renderLedger(state.transactions || []);
}

const stageCopy = {
  MODEL_RESPONSE_RECEIVED: ["Qwen intent", "Structured response received"],
  INTENT_VALIDATED: ["Schema valid", "Six bounded fields"],
  SOURCE_RESOLVED: ["Source resolved", "Server-owned account ID"],
  SOURCE_DEFAULTED: ["Source defaulted", "Only eligible user account"],
  RECIPIENT_RESOLVED: ["Recipient resolved", "Saved beneficiary ID"],
  ELIGIBILITY_PASSED: ["Eligibility passed", "Exact, funded, conflict-free"],
  SQLITE_COMMITTED: ["Ledger settled", "Matching debit and credit"],
  CLOSE_RECIPIENT_MATCH: ["Close recipient match", "Confirmation required"],
  ACTION_CONFIRMATION_REQUIRED: ["Action confirmation", "No ledger mutation"],
  SIGNED_APPROVAL_REQUIRED: ["Draft only", "Awaiting device signature"],
  VALIDATION_PASSED: ["Validator agreed", "Every field matches your words"],
  VALIDATION_ATTENTION: ["Validator flagged", "Some fields need your check"],
  VALIDATION_FROZEN: ["Validator froze draft", "Contradicts your words"],
  RISK_POLICY_PASSED: ["Risk policy passed", "KYC, limits and velocity"],
  OUT_OF_BAND_REQUIRED: ["Token code required", "New payee or over S$2,000"],
  READ_BACK_REQUIRED: ["Read-back required", "S$200 and above is read aloud"],
  UNUSUAL_AMOUNT_FLAGGED: ["Unusual amount", "Not what you usually send"],
  NEW_PAYEE_LIMIT_EXCEEDED: ["New payee limit", "S$100 for the first 24 hours"],
  UNUSUAL_AMOUNT_UNCONFIRMED: ["Amount not confirmed", "Nothing moved"],
  KYC_PENDING: ["KYC incomplete", "No ledger mutation"],
  KYC_NOT_COMPLETED: ["Identity check not finished", "Nothing moved"],
  SCAM_SUSPECTED: ["Held: possible scam", "Stricter-only rule · nothing drafted"],
  INPUT_FIREWALL_PASSED: ["Firewall passed", "No injection patterns"],
  NO_APPROVAL_NEEDED: ["No approval needed", "Nothing leaves your account"],
  INVESTMENT_REQUEST_UNDERSTOOD: ["Investing request", "Fixed rules · no AI"],
  INVESTMENT_PORTFOLIO: ["Portfolio", "Live simulated prices"],
  INVESTMENT_TRENDING: ["Trending picks", "Rule-based, with risks"],
  INVESTMENT_PLANS: ["Long-term plans", "Matched to your profile"],
  INVESTMENT_QUOTE: ["Quote", "Simulated market"],
  ORDER_DRAFTED: ["Order drafted", "Nothing bought or sold yet"],
  ORDER_APPROVAL_RECEIVED: ["Order approval", "Stored order received"],
  ORDER_FILLED: ["Order filled", "Within your signed price limit"],
  PLAN_STARTED: ["Plan started", "First instalment invested"],
  INVESTOR_PROFILE_REQUIRED: ["Investor profile needed", "Four questions first"],
  RISK_ACKNOWLEDGEMENT_REQUIRED: ["Risk not acknowledged", "Nothing bought"],
  PRICE_MOVED_BEYOND_LIMIT: ["Price moved too far", "Order not filled · nothing moved"],
  NOT_HELD: ["Not in your portfolio", "Nothing sold"],
  SELL_MORE_THAN_HELD: ["More than you hold", "Nothing sold"],
  ORDER_LIMIT_EXCEEDED: ["Order limit", "Up to S$5,000 per order"],
  DAILY_TRADING_LIMIT_EXCEEDED: ["Daily trading limit", "Up to S$10,000 a day"],
  BATCH_RECIPIENTS_RECOVERED_FROM_DIRECTORY: ["Several payments found", "Each name matched to one saved payee"],
  BATCH_REFERENCE_NOTED: ["Reference noted", "Applies to the whole batch"],
  BALANCE_INQUIRY: ["Balance read", "Read-only · no model, no signature"],
  MONEY_REQUEST_SENT: ["Payment request sent", "You receive money · no signature"],
  APPROVAL_DEVICE_NOT_BOUND: ["Not your registered device", "Nothing moved"],
  FACE_NOT_VERIFIED: ["Face not confirmed", "The registered face must confirm this payment"],
  FACE_RECEIPT_MISMATCH: ["Face scan was for another payment", "Nothing moved"],
  FACE_ALREADY_USED: ["Face scan already used", "Nothing moved"],
  KYC_FACE_REQUIRED: ["Face registration needed", "Part of identity verification"],
  KYC_EXPIRED: ["KYC expired", "No ledger mutation"],
  KYC_REJECTED: ["KYC rejected", "No ledger mutation"],
  AMOUNT_LIMIT_EXCEEDED: ["Over per-transfer limit", "No ledger mutation"],
  DAILY_LIMIT_EXCEEDED: ["Over daily limit", "No ledger mutation"],
  VELOCITY_LIMIT_EXCEEDED: ["Too many transfers", "Rate limited · no mutation"],
  MICRO_TRANSFER_RATE_LIMITED: ["Small transfers throttled", "Rate limited · no mutation"],
  OUT_OF_BAND_CODE_INVALID: ["Token code rejected", "Sign again with the right code"],
  OUT_OF_BAND_LOCKED: ["Token locked", "Too many wrong codes"],
  FACE_MATCH_VERIFIED: ["Face matched", "Registered face, live, this receipt"],
  DEVICE_SIGNATURE_VERIFIED: ["Device signature verified", "Bound to this receipt digest"],
  SIGNING_CREDENTIAL_REQUIRED: ["Device not enrolled", "No ledger mutation"],
  APPROVAL_SIGNATURE_INVALID: ["Signature rejected", "Does not verify"],
  APPROVAL_RECEIPT_MISMATCH: ["Signature rejected", "Signed a different receipt"],
  APPROVAL_CHALLENGE_REPLAYED: ["Signature rejected", "Challenge already used"],
  APPROVAL_CHALLENGE_EXPIRED: ["Signature rejected", "Challenge expired"],
  APPROVAL_COUNTER_REPLAY: ["Signature rejected", "Stale or cloned authenticator"],
  APPROVAL_ORIGIN_MISMATCH: ["Signature rejected", "Wrong origin"],
  APPROVAL_USER_NOT_VERIFIED: ["Signature rejected", "Biometric not verified"],
  MISSING_SOURCE_ACCOUNT: ["Source required", "Clarification required"],
  NEGATED_ACTION: ["Negated action", "Execution prohibited"],
  AMBIGUOUS_RECIPIENT: ["Clarification required", "No ledger mutation"],
  INSUFFICIENT_FUNDS: ["Insufficient funds", "No ledger mutation"],
  UNKNOWN_RECIPIENT: ["Recipient unresolved", "No ledger mutation"],
  UNKNOWN_SOURCE_ACCOUNT: ["Source unresolved", "No ledger mutation"],
  INVALID_MODEL_OUTPUT: ["Model output rejected", "No ledger mutation"],
  MODEL_UNAVAILABLE: ["Qwen unavailable", "No ledger mutation"],
  UNSUPPORTED_ACTION: ["Action unsupported", "No ledger mutation"],
  DATABASE_ERROR: ["Could not save", "Nothing moved"],
  IDEMPOTENT_REPLAY: ["Existing result returned", "No second debit"],
  RECEIPT_APPROVAL_RECEIVED: ["Receipt approval", "Stored coordinates received"],
  RECEIPT_REVALIDATED: ["Receipt revalidated", "Digest, owner, state and balance"],
  RECEIPT_DIGEST_MISMATCH: ["Receipt rejected", "Digest does not match"],
  RECEIPT_USER_MISMATCH: ["Receipt rejected", "Owner does not match"],
  RECEIPT_EXPIRED: ["Receipt expired", "Authorization window closed"],
  RECEIPT_SUPERSEDED: ["Choice superseded", "Another candidate was selected"],
  RECEIPT_NOT_FOUND: ["Receipt missing", "No stored receipt found"],
  RECEIPT_INVALID_STATUS: ["Receipt invalid", "Stored lifecycle is not approvable"],
  RECEIPT_ALREADY_EXECUTED: ["Receipt already settled", "Authorization path differs"],
  BATCH_RECEIPT_APPROVAL_RECEIVED: ["Batch approval", "Stored batch coordinates received"],
  BATCH_RECEIPT_REVALIDATED: ["Batch revalidated", "Items, mappings, digest and funds"],
  SQLITE_BATCH_COMMITTED: ["Batch settled", "All payments together, all or none"],
  BATCH_CONFIRMATION_REQUIRED: ["Batch confirmation", "No ledger mutation"],
  AMBIGUOUS_BATCH_BINDING: ["Batch binding rejected", "Restate one amount per recipient"],
  BATCH_RECIPIENT_COUNT_MISMATCH: ["Batch count rejected", "One amount per recipient"],
  DUPLICATE_BATCH_RECIPIENT: ["Duplicate recipient", "Each saved recipient appears once"],
  BATCH_RECEIPT_DIGEST_MISMATCH: ["Batch receipt rejected", "Digest does not match"],
  BATCH_RECEIPT_TAMPERED: ["Batch receipt rejected", "Stored facts changed"],
  BATCH_RECEIPT_EXPIRED: ["Batch receipt expired", "Authorization window closed"],
  BATCH_RECEIPT_INVALID_STATUS: ["Batch receipt invalid", "Stored lifecycle is not approvable"],
  BATCH_RECEIPT_USER_MISMATCH: ["Batch receipt rejected", "Owner does not match"],
  BATCH_RECEIPT_NOT_FOUND: ["Batch receipt missing", "No stored batch receipt found"],
  MISSING_AMOUNT: ["Amount required", "Recipient and source are locked"],
  AMOUNT_CLARIFICATION_COMPLETED: ["Amount captured", "Immutable receipt created"],
  CLARIFICATION_RESPONSE_RECEIVED: ["Amount response", "Bounded parser only · no Qwen call"],
  CLARIFICATION_SCOPE_VIOLATION: ["Reply rejected", "Only one amount is allowed"],
  PAYNOW_PROXY_RESOLVED: ["PayNow resolved", "Directory-owned masked identity"],
  PAYNOW_CONFIRMATION_REQUIRED: ["PayNow confirmation", "Lookup-backed receipt required"],
  PAYNOW_LOOKUP_EXPIRED: ["PayNow lookup expired", "Start a new exact lookup"],
  PHONETIC_RECIPIENT_MATCH: ["Possible phonetic match", "Confirmation required"],
  UNGROUNDED_AMOUNT: ["Amount rejected", "No verbatim evidence"],
  AMOUNT_EVIDENCE_MISMATCH: ["Amount rejected", "Evidence and value differ"],
};

function renderTrace(trace, outcome) {
  const failure = outcome === "FAILED";
  const stopped = outcome === "BLOCKED" || outcome === "CLARIFICATION_REQUEST";
  ui.traceList.innerHTML = trace.map((event, index) => {
    const [title, detail] = stageCopy[event.stage] || [event.stage, "Recorded by server"];
    const stateClass = index === trace.length - 1 && failure ? "failed"
      : index === trace.length - 1 && stopped ? "blocked" : "complete";
    const latency = event.latency_ms == null ? "" : ` · ${event.latency_ms} ms`;
    return `<li class="trace-step ${stateClass}"><span></span><div><strong>${escapeHtml(title)}</strong><small>${escapeHtml(detail + latency)}</small></div></li>`;
  }).join("");
}

function setOutcome(kind, index, kicker, title, detail) {
  ui.outcomePanel.className = `outcome-panel ${kind}`;
  ui.outcomeIndex.textContent = index;
  ui.outcomeKicker.textContent = kicker;
  ui.outcomeTitle.textContent = title;
  ui.outcomeDetail.textContent = detail;
  ui.candidateList.hidden = true;
  ui.candidateList.innerHTML = "";
}

function resetCommandMode({clearInput = true} = {}) {
  activeClarification = null;
  pendingClarificationResponse = null;
  pendingActionRequest = null;
  ui.commandLabel.textContent = "What should the ledger do?";
  ui.input.maxLength = 500;
  ui.input.readOnly = false;
  ui.input.placeholder = "Send Alicia Powell S$2.50 from savings.";
  ui.runButton.querySelector("span").textContent = "Run intention";
  ui.commandHint.textContent = "Use a full saved name. Partial or misspelled names never auto-execute.";
  ui.newRequestButton.hidden = true;
  if (clearInput) ui.input.value = "";
}

function enterClarificationMode(result) {
  const clarification = result.clarification;
  activeClarification = clarification;
  pendingClarificationResponse = null;
  ui.commandLabel.textContent = "How much?";
  ui.input.value = "";
  ui.input.maxLength = 100;
  ui.input.readOnly = false;
  ui.input.placeholder = "ten dollars";
  ui.runButton.querySelector("span").textContent = "Continue exact request";
  ui.commandHint.textContent = "Amount only. Recipient and source cannot change in this reply.";
  ui.newRequestButton.hidden = false;

  const facts = clarification.locked_facts || {};
  const expiry = new Date(clarification.expires_at);
  const expiryLabel = Number.isNaN(expiry.getTime())
    ? clarification.expires_at
    : expiry.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
  ui.candidateList.hidden = false;
  ui.candidateList.innerHTML = `<div class="clarification-lock">
    <p class="match-label">Locked by the gateway</p>
    <div class="locked-facts">
      <span><small>From</small><strong>${escapeHtml(facts.source_label || "—")}</strong></span>
      <span><small>To</small><strong>${escapeHtml((facts.recipient_display_names || []).join(", ") || "—")}</strong></span>
      ${facts.destination_reference ? `<span><small>PayNow</small><strong>${escapeHtml(facts.destination_reference)}</strong></span>` : ""}
      <span><small>Expires</small><strong>${escapeHtml(expiryLabel)}</strong></span>
    </div>
  </div>`;
  ui.input.focus();
}

// The settlement panel speaks plainly: what moved, between whom, how it was
// approved, and the reference to quote. No database statements are shown.
function setSettlement(state, statusText, rows, note) {
  ui.commitStatus.className = `commit-status ${state}`;
  ui.commitStatus.textContent = statusText;
  ui.commitSummary.innerHTML = rows
    .map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`)
    .join("");
  ui.commitSummary.hidden = rows.length === 0;
  ui.commitNote.textContent = note || "";
}

function accountLabel(result, accountId) {
  const account = (result.state?.accounts || []).find(item => item.account_id === accountId);
  return account ? account.label : "your account";
}

function approvalLabel(result) {
  const kind = result.approval?.credential_kind;
  const face = result.approval?.face_similarity != null
    ? `Your face (${Math.round(result.approval.face_similarity * 100)}% match) + `
    : "";
  if (kind === "WEBAUTHN_PLATFORM") return `${face}Windows Hello / Touch ID`;
  if (kind === "MOCK_DEVICE_KEY") return `${face}this device's key`;
  return `${face}your device signature`;
}

function reasonLabel(code) {
  const copy = stageCopy[code];
  return copy ? copy[0] : String(code || "Not completed").replaceAll("_", " ").toLowerCase();
}

function reference(id) {
  return id ? `${id.slice(0, 18)}…` : "—";
}

function renderCommit(result) {
  if (result.outcome === "EXECUTED_SYNTHETIC_BATCH") {
    const actions = result.resolved_actions || [];
    const source = accountLabel(result, actions[0]?.source_account_id);
    setSettlement("committed", "Settled", [
      ["From", source],
      ...actions.map(action => [
        "To",
        `${action.recipient_display_name}${action.masked_destination ? ` (${action.masked_destination})` : ""} · ${formatSgd(action.amount_minor)}`,
      ]),
      ["Total debited", formatSgd(result.amount_minor)],
      ["Approved with", approvalLabel(result)],
      ["Reference", reference(result.batch_transaction_id)],
    ], "All payments settled together in one step: either every payment moved or none did. Money out equals money in.");
    return;
  }
  if (result.outcome === "EXECUTED_SYNTHETIC_TRADE" || result.outcome === "EXECUTED_INVESTMENT_PLAN") {
    const trades = result.trades || [];
    const cash = trades.reduce((sum, trade) => sum + trade.cash_minor, 0);
    const selling = trades[0]?.side === "SELL";
    setSettlement("committed", "Settled", [
      ...(result.plan ? [
        ["Plan", `${result.plan.name} · ${formatSgd(result.plan.monthly_minor)} a month`],
        ["Next instalment", new Date(result.plan.next_run_at).toLocaleDateString()],
      ] : []),
      ...trades.map(trade => [trade.side === "BUY" ? "Bought" : "Sold", `${trade.quantity} × ${trade.name} at ${formatSgd(trade.price_minor)}`]),
      [selling ? "Paid into savings" : "Taken from savings", `${formatSgd(cash)}${trades.some(t => t.fee_minor) ? ` (incl. ${formatSgd(trades.reduce((s, t) => s + t.fee_minor, 0))} fee)` : ""}`],
      ["Approved with", approvalLabel(result)],
      ["Reference", reference(result.order_id)],
    ], "Filled at the live price within the limit you signed. Cash and holdings changed together in one step.");
    return;
  }
  if (result.outcome !== "EXECUTED_SYNTHETIC_TRANSFER") {
    setSettlement("blocked", "Nothing moved", [
      ["Result", reasonLabel(result.reason_code || result.outcome)],
    ], "No money moved and every balance is unchanged.");
    return;
  }
  const action = result.resolved_action;
  setSettlement("committed", "Settled", [
    ["From", accountLabel(result, action.source_account_id)],
    ["To", `${action.recipient_display_name}${action.masked_destination ? ` (${action.masked_destination})` : ""}`],
    ["Amount", formatSgd(action.amount_minor)],
    ["Approved with", approvalLabel(result)],
    ["Reference", reference(result.transaction_id)],
  ], "Recorded as a matching debit and credit: money out equals money in, exactly once.");
}

function setChoiceBusy(busy) {
  ui.candidateList.querySelectorAll(".candidate-choice").forEach(button => {
    button.disabled = busy;
  });
}

const deviceApprover = new DeviceApprover();
// Signed assertions per receipt, kept only so an interrupted response can be
// retried with the identical signature (the gateway then returns the stored
// outcome instead of moving money again).
const signedAssertions = new Map();

async function ensureSigningCredential() {
  const response = await fetch("/api/signing/credentials", {cache: "no-store"});
  const listing = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(`Credential lookup returned HTTP ${response.status}`);
    error.reasonCode = typeof listing.detail === "string" ? listing.detail : `HTTP_${response.status}`;
    throw error;
  }
  const kycStatus = await fetch("/api/kyc", {cache: "no-store"}).then(r => r.json()).catch(() => ({}));
  if (kycStatus.kyc_status === "VERIFIED" && kycStatus.face_registered
      && await deviceApprover.usableCredential(listing.credentials)) return;
  ui.elapsedLabel.textContent = "Identity check needed before your first approval";
  await runKycOnboarding();
}

// One-time KYC: retrieve Myinfo (mock Singpass), take consent, register the
// account's face with the camera, then register this device's passkey as the
// only device allowed to approve.
function runKycOnboarding() {
  const dialog = document.querySelector("#kyc-dialog");
  const person = document.querySelector("#kyc-person");
  const consent = document.querySelector("#kyc-consent");
  const consentCheck = document.querySelector("#kyc-consent-check");
  const status = document.querySelector("#kyc-status");
  const next = document.querySelector("#kyc-next");
  const cancel = document.querySelector("#kyc-cancel");
  const facePanel = document.querySelector("#kyc-face");
  const faceVideo = document.querySelector("#kyc-face-video");
  const facePrompt = document.querySelector("#kyc-face-prompt");
  const labels = {
    name: "Name", uinfin_masked: "NRIC/FIN", sex: "Sex", dob: "Date of birth",
    nationality: "Nationality", residential_status: "Residential status",
    mobile: "Mobile", email: "Email", address: "Registered address", source: "Source",
  };
  person.hidden = true;
  consent.hidden = true;
  facePanel.hidden = true;
  consentCheck.checked = false;
  status.textContent = "";
  next.disabled = false;
  next.textContent = "Retrieve Myinfo with Singpass";
  let consentId = null;
  let faceRegistered = false;
  let closeCamera = () => {};
  return new Promise((resolve, reject) => {
    const finish = error => {
      closeCamera();
      next.onclick = null;
      cancel.onclick = null;
      consentCheck.onchange = null;
      dialog.oncancel = null;
      dialog.close();
      if (error) reject(error); else resolve();
    };
    consentCheck.onchange = () => { next.disabled = !consentCheck.checked; };
    next.onclick = async () => {
      next.disabled = true;
      try {
        if (!consentId) {
          status.textContent = "Retrieving your Myinfo…";
          const response = await fetch("/api/kyc/myinfo", {method: "POST"});
          if (!response.ok) throw new Error(`Myinfo returned HTTP ${response.status}`);
          const myinfo = await response.json();
          consentId = myinfo.consent_id;
          person.innerHTML = `<dl>${Object.entries(labels).map(([key, label]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(myinfo.person[key] || "—")}</dd></div>`).join("")}</dl>`;
          person.hidden = false;
          consent.hidden = false;
          status.textContent = "Check your details, then agree to continue.";
          next.textContent = "Agree and register my face";
          next.disabled = !consentCheck.checked;
          return;
        }
        if (!faceRegistered) {
          if (facePanel.hidden) {
            person.hidden = true;
            consent.hidden = true;
            facePanel.hidden = false;
            status.textContent = "Starting the camera…";
            closeCamera = await openCamera(faceVideo);
            status.textContent = "Centre your face in the frame, then start the scan.";
            next.textContent = "Scan my face";
            next.disabled = false;
            return;
          }
          const frames = await captureHeadTurn(faceVideo, {onStep: (_index, prompt) => { facePrompt.textContent = prompt; }});
          facePrompt.textContent = "Checking…";
          await postFrames("/api/face/register", {consent_id: consentId, frames});
          faceRegistered = true;
          closeCamera();
          facePrompt.className = "face-prompt ok";
          facePrompt.textContent = "Face registered to this account.";
        }
        status.textContent = "Registering this device… follow any Windows Hello prompt.";
        await deviceApprover.enroll({consentId});
        status.textContent = "Verified.";
        finish();
      } catch (error) {
        const message = faceMessages[error.reasonCode] || error.reasonCode || error.message;
        status.textContent = `Could not finish: ${message} You can try again.`;
        if (!faceRegistered && !facePanel.hidden) {
          facePrompt.className = "face-prompt bad";
          facePrompt.textContent = message;
          next.textContent = "Scan my face again";
        }
        if (error.reasonCode?.startsWith("KYC_CONSENT")) {
          consentId = null;
          faceRegistered = false;
          closeCamera();
          facePanel.hidden = true;
          next.textContent = "Retrieve Myinfo with Singpass";
        }
        next.disabled = false;
      }
    };
    const refuse = () => {
      const error = new Error("Identity check not completed");
      error.reasonCode = "KYC_NOT_COMPLETED";
      finish(error);
    };
    cancel.onclick = refuse;
    dialog.oncancel = event => { event.preventDefault(); refuse(); };
    dialog.showModal();
    next.focus();
  });
}

// The registered face confirms every transfer; the device key then signs the
// same receipt (Windows Hello adds its own prompt).
function describeSigner(kind) {
  return kind === "WEBAUTHN_PLATFORM"
    ? {label: "Your face + Windows Hello / Touch ID", action: "Confirm with your face"}
    : {label: "Your face + this device's key", action: "Confirm with your face"};
}

// The deterministic confirmation overlay. Every fact shown comes from the
// server-stored receipt returned with the challenge, never from model text.
// Plays the server-generated read-back (Kokoro or Tencent Cloud voice). If
// that audio is unavailable, the browser's own voice reads the same text; if
// there is no voice at all, showing the text counts as presented. Returns a
// function that stops playback.
function speakReadBack(readBack, onDone) {
  let stopped = false;
  let audio = null;
  const finish = () => { if (!stopped) onDone(); };
  const browserVoice = () => {
    if (stopped) return;
    if (!("speechSynthesis" in window)) { finish(); return; }
    const utterance = new SpeechSynthesisUtterance(readBack.text);
    utterance.onend = finish;
    // A blocked or failed utterance was not heard: leave "Play again" to the user.
    utterance.onerror = () => {};
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utterance);
  };
  (async () => {
    if (!readBack?.audio_artifact_id) { browserVoice(); return; }
    const url = `/api/audio/${encodeURIComponent(readBack.audio_artifact_id)}`;
    for (let attempt = 0; attempt < 60 && !stopped; attempt += 1) {
      try {
        const response = await fetch(url, {cache: "no-store"});
        if (response.status === 202) { await new Promise(r => setTimeout(r, 250)); continue; }
        if (!response.ok) break;
        audio = new Audio(URL.createObjectURL(await response.blob()));
        audio.onended = finish;
        audio.onerror = browserVoice;
        await audio.play();
        return;
      } catch (_error) {
        break;
      }
    }
    browserVoice();
  })();
  return () => {
    stopped = true;
    if (audio) audio.pause();
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  };
}

function confirmOnDevice(challenge, signerKind) {
  const review = challenge.review;
  const signer = describeSigner(signerKind);
  ui.approvalItems.innerHTML = review.items.map(item => `<div class="approval-item">
      <span><small>To</small><strong>${escapeHtml(item.recipient_display_name)}</strong>${item.masked_destination ? `<em>${escapeHtml(item.masked_destination)}</em>` : ""}${item.new_payee ? '<b class="payee-flag">New payee</b>' : ""}</span>
      <span><small>Amount</small><strong>${escapeHtml(formatSgd(item.amount_minor))}</strong>${item.unusual_amount ? '<b class="payee-flag">Unusual amount</b>' : ""}</span>
    </div>`).join("");
  ui.approvalSource.textContent = review.source_label || "—";
  ui.approvalTotal.textContent = formatSgd(review.total_minor);
  ui.approvalDigest.textContent = `${review.receipt_digest.slice(0, 16)}…`;
  ui.approvalMethod.textContent = signer.label;
  const unverified = review.kind === "ORDER" ? [] : (review.validation?.fields || []).filter(field => field.verdict !== "MATCH");
  ui.approvalValidation.className = unverified.length ? "approval-validation attention" : "approval-validation pass";
  ui.approvalValidation.innerHTML = unverified.length
    ? `<strong>Independent validator could not confirm ${unverified.length === 1 ? "this field" : "these fields"} from your words — check before signing:</strong>
       <ul>${unverified.map(field => `<li>${escapeHtml(field.field.replace(/^items\[\d+\]\./, ""))}: ${escapeHtml(field.draft_value)} — ${escapeHtml(field.note)}</li>`).join("")}</ul>`
    : review.kind === "ORDER"
      ? `<strong>${escapeHtml(review.validation.note)}</strong>`
      : "<strong>Independent validator: every field matches your words.</strong>";
  ui.approvalConfirm.textContent = signer.action;
  const facePrompt = document.querySelector("#approval-face-prompt");
  const faceVideo = document.querySelector("#approval-face-video");
  facePrompt.className = "face-prompt";
  facePrompt.textContent = "Starting the camera…";
  let closeCamera = () => {};
  let scanning = false;
  const cameraReady = openCamera(faceVideo).then(close => {
    closeCamera = close;
    facePrompt.textContent = "When you are ready, press Confirm and follow the prompts.";
    return true;
  }).catch(error => {
    facePrompt.className = "face-prompt bad";
    facePrompt.textContent = faceMessages[error.reasonCode] || error.message;
    return false;
  });
  const needsCode = Boolean(review.requires_out_of_band);
  ui.approvalOob.hidden = !needsCode;
  ui.approvalOobCode.value = "";
  // Read-back: required for the S$200+ tier and unusual amounts; also played
  // whenever the request was spoken, so voice in means voice out.
  const readBack = review.read_back;
  const needsReadBack = Boolean(review.requires_read_back);
  let readBackPlayed = false;
  let stopReadBack = () => {};
  ui.approvalReadBack.hidden = !readBack;
  ui.approvalReadBackText.textContent = readBack?.text || "";
  // Unusual amount: an explicit "yes, that amount" before signing.
  const needsUnusual = Boolean(review.requires_unusual_confirmation);
  const unusualItem = review.items.find(item => item.unusual_amount);
  ui.approvalUnusual.hidden = !needsUnusual;
  ui.approvalUnusualCheck.checked = false;
  ui.approvalUnusualText.textContent = review.confirmation_text ? review.confirmation_text : unusualItem
    ? `Yes, ${formatSgd(unusualItem.amount_minor)} to ${unusualItem.recipient_display_name} is correct. You usually send ${formatSgd(unusualItem.unusual_amount.typical_min_minor)}–${formatSgd(unusualItem.unusual_amount.typical_max_minor)}.`
    : "";
  const codeReady = () => !needsCode || /^\d{6}$/.test(ui.approvalOobCode.value);
  const allReady = () => codeReady()
    && (!needsReadBack || readBackPlayed)
    && (!needsUnusual || ui.approvalUnusualCheck.checked);
  const syncConfirm = () => {
    ui.approvalConfirm.disabled = !allReady() || scanning;
    ui.approvalConfirm.title = needsReadBack && !readBackPlayed ? "Listen to the read-back first" : "";
  };
  const playReadBack = () => {
    stopReadBack();
    stopReadBack = speakReadBack(readBack, () => { readBackPlayed = true; syncConfirm(); });
  };
  ui.approvalReadBackPlay.onclick = playReadBack;
  ui.approvalOobCode.oninput = syncConfirm;
  ui.approvalUnusualCheck.onchange = syncConfirm;
  syncConfirm();
  if (readBack && (needsReadBack || lastRequestWasSpoken)) playReadBack();
  return new Promise(resolve => {
    const cleanup = confirmed => {
      const result = confirmed
        ? {
            confirmed: true,
            outOfBandCode: needsCode ? ui.approvalOobCode.value : undefined,
            readBackPlayed,
            unusualConfirmed: needsUnusual && ui.approvalUnusualCheck.checked,
          }
        : {confirmed: false};
      stopReadBack();
      closeCamera();
      cameraReady.then(() => closeCamera());
      ui.approvalReadBackPlay.onclick = null;
      ui.approvalUnusualCheck.onchange = null;
      ui.approvalOobCode.oninput = null;
      ui.approvalConfirm.disabled = false;
      ui.approvalConfirm.onclick = null;
      ui.approvalCancel.onclick = null;
      ui.approvalDialog.oncancel = null;
      ui.approvalDialog.close();
      resolve(result);
    };
    // Face confirmation: a live head-turn scan, matched by the gateway against
    // the face registered at KYC and bound to this exact challenge.
    ui.approvalConfirm.onclick = async () => {
      if (!allReady() || scanning) return;
      if (!await cameraReady) return;
      scanning = true;
      syncConfirm();
      facePrompt.className = "face-prompt";
      try {
        const frames = await captureHeadTurn(faceVideo, {onStep: (_index, prompt) => { facePrompt.textContent = prompt; }});
        facePrompt.textContent = "Checking your face…";
        const match = await postFrames("/api/face/verify", {challenge_id: challenge.challenge_id, frames});
        facePrompt.className = "face-prompt ok";
        facePrompt.textContent = `Face matched (${Math.round((match.similarity || 0) * 100)}%). Signing…`;
        cleanup(true);
      } catch (error) {
        facePrompt.className = "face-prompt bad";
        facePrompt.textContent = error.message;
        ui.approvalConfirm.textContent = error.reasonCode === "FACE_LOCKED" ? "Locked" : "Scan again";
        scanning = error.reasonCode === "FACE_LOCKED";
        syncConfirm();
      }
    };
    ui.approvalCancel.onclick = () => cleanup(false);
    ui.approvalDialog.oncancel = event => {
      event.preventDefault();
      cleanup(false);
    };
    ui.approvalDialog.showModal();
    (needsCode ? ui.approvalOobCode : ui.approvalCancel).focus();
  });
}

// Link to the hash-chained audit bundle: stored receipt, the device
// signature re-verified now, and every event from request to ledger commit.
function appendEvidenceLink(receiptId) {
  const link = document.createElement("a");
  link.href = `/api/audit/receipts/${encodeURIComponent(receiptId)}`;
  link.target = "_blank";
  link.rel = "noopener";
  link.className = "evidence-link";
  link.textContent = "View signed audit evidence";
  ui.outcomeDetail.append(" ", link);
}

// After a PayNow transfer settles, the recipient can be saved under a name.
// The server registers it as a new payee: S$100 cap and a one-time code for
// the first 24 hours.
function offerSavePayee(receiptId, action) {
  ui.candidateList.hidden = false;
  ui.candidateList.innerHTML = `<form class="save-payee">
    <p class="match-label">Save ${escapeHtml(action.recipient_display_name)} (${escapeHtml(action.masked_destination || "PayNow")}) as a payee?</p>
    <label>Name to call them <input name="nickname" maxlength="40" minlength="3" required placeholder="e.g. Jason" autocomplete="off"></label>
    <button type="submit" class="candidate-choice">Save payee</button>
    <small class="save-payee-note">New payees are limited to S$100 per transfer and need a one-time code for their first 24 hours.</small>
  </form>`;
  const form = ui.candidateList.querySelector("form");
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const nickname = form.nickname.value.trim();
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      const response = await fetch("/api/payees", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({receipt_id: receiptId, nickname}),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        const reasons = {
          NICKNAME_IN_USE: "That name is already used by another payee.",
          INVALID_NICKNAME: "Use letters and spaces only (3–40 characters).",
          PAYEE_ALREADY_SAVED: "This PayNow recipient is already saved.",
        };
        form.querySelector(".save-payee-note").textContent = reasons[body.detail] || "Could not save this payee.";
        button.disabled = false;
        return;
      }
      form.innerHTML = `<p class="match-label">Saved as ${escapeHtml(body.display_name)}</p>
        <small class="save-payee-note">You can now say “Pay ${escapeHtml(body.display_name)} 20”. Limited to S$100 with a one-time code until ${escapeHtml(new Date(body.new_payee_until).toLocaleString())}.</small>`;
      await loadState();
    } catch {
      form.querySelector(".save-payee-note").textContent = "Could not reach the gateway. Try again.";
      button.disabled = false;
    }
  });
}

function renderApprovalRefusal(reasonCode, detail) {
  setOutcome("blocked", "!", "Approval not signed", reasonCode.replaceAll("_", " ").toLowerCase(), detail);
  setSettlement("blocked", "Nothing moved", [["Result", reasonLabel(reasonCode)]],
    "Without your device signature nothing can move; balances are unchanged.");
}

async function signedApproval(collection, receipt, submit, interruptedDetail) {
  const started = performance.now();
  setChoiceBusy(true);
  try {
    let signed = signedAssertions.get(receipt.receipt_id);
    if (!signed) {
      ui.elapsedLabel.textContent = "Requesting a one-time signing challenge";
      await ensureSigningCredential();
      const issued = await deviceApprover.requestChallenge(collection, receipt);
      if (issued.kind === "BLOCKED") {
        renderResult(issued.result, Math.round(performance.now() - started));
        return;
      }
      const credential = await deviceApprover.usableCredential(issued.challenge.allow_credentials);
      const decision = await confirmOnDevice(issued.challenge, credential?.kind);
      if (!decision.confirmed) {
        renderApprovalRefusal("APPROVAL_CANCELLED", "You closed the confirmation. Nothing was signed and balances are unchanged.");
        ui.elapsedLabel.textContent = "Approval cancelled";
        return;
      }
      ui.elapsedLabel.textContent = "Waiting for the device signature";
      signed = {
        assertion: wireAssertion(await deviceApprover.signChallenge(issued.challenge)),
        out_of_band_code: decision.outOfBandCode,
        read_back_played: Boolean(decision.readBackPlayed),
        unusual_amount_confirmed: Boolean(decision.unusualConfirmed),
      };
      signedAssertions.set(receipt.receipt_id, signed);
    }
    ui.elapsedLabel.textContent = "Signed approval in flight";
    const approval = await submit(receipt, signed);
    if (approval.kind === "UNKNOWN") {
      ui.outcomePanel.className = "outcome-panel clarification";
      ui.outcomeIndex.textContent = "?";
      ui.outcomeKicker.textContent = "Outcome unknown · safe retry available";
      ui.outcomeTitle.textContent = "The gateway response was interrupted.";
      ui.outcomeDetail.textContent = interruptedDetail;
      setSettlement("idle", "Unknown", [["Connection", approval.message]],
        "The reply was lost. Retrying resends the same signed approval, so the transfer can settle at most once.");
      ui.elapsedLabel.textContent = `${Math.round(performance.now() - started)} ms · response unknown`;
      return;
    }
    signedAssertions.delete(receipt.receipt_id);
    renderResult(approval.result, Math.round(performance.now() - started));
  } catch (error) {
    console.error("signed approval failed", error);
    signedAssertions.delete(receipt.receipt_id);
    const reason = error?.reasonCode || (error?.name === "NotAllowedError" ? "BIOMETRIC_DECLINED" : "SIGNING_FAILED");
    renderApprovalRefusal(reason, "The device did not produce a valid signature. Nothing was executed.");
    ui.elapsedLabel.textContent = `${Math.round(performance.now() - started)} ms · not signed`;
  } finally {
    setChoiceBusy(false);
  }
}

function approveReceipt(choice) {
  return signedApproval(
    "receipts",
    choice,
    requestReceiptApproval,
    "Do not create a new transfer. Retry this exact receipt to retrieve the stored result.",
  );
}

async function approveOrder(order) {
  await signedApproval(
    "orders",
    order,
    requestOrderApproval,
    "Retry this exact order to retrieve its stored result; it can fill at most once.",
  );
  await loadInvestments();
}

function approveBatchReceipt(receipt) {
  return signedApproval(
    "batch-receipts",
    receipt,
    requestBatchReceiptApproval,
    "Retry this exact batch receipt to retrieve its stored result.",
  );
}

async function waitForResultAudio(artifactId, generation) {
  const url = `/api/audio/${encodeURIComponent(artifactId)}`;
  for (let attempt = 0; attempt < 60 && generation === audioPollGeneration; attempt += 1) {
    try {
      const response = await fetch(url, {cache: "no-store"});
      if (response.ok) {
        ui.resultAudio.src = url;
        ui.resultAudio.hidden = false;
        return;
      }
      if (response.status !== 202) return;
    } catch (_error) {
      return;
    }
    await new Promise(resolve => setTimeout(resolve, 250));
  }
}

function renderResult(result, elapsedMs) {
  renderState(result.state);
  renderTrace(result.trace || [], result.outcome);
  renderCommit(result);
  audioPollGeneration += 1;
  const currentAudioGeneration = audioPollGeneration;
  if (result.audio_artifact_id) {
    ui.resultAudio.removeAttribute("src");
    ui.resultAudio.hidden = true;
    void waitForResultAudio(result.audio_artifact_id, currentAudioGeneration);
  } else {
    ui.resultAudio.removeAttribute("src");
    ui.resultAudio.hidden = true;
  }
  ui.elapsedLabel.textContent = `${elapsedMs} ms end to end`;
  if (result.outcome === "EXECUTED_SYNTHETIC_TRANSFER") {
    setOutcome("success", "✓", "Synthetic transfer settled", "The ledger moved exactly once.", `${formatSgd(result.amount_minor)} moved to ${result.resolved_action.recipient_display_name}. The database recorded two balanced entries.`);
    if (result.receipt?.receipt_id) appendEvidenceLink(result.receipt.receipt_id);
    if (result.resolved_action.destination_kind === "PAYNOW_MOBILE" && result.receipt?.receipt_id) {
      offerSavePayee(result.receipt.receipt_id, result.resolved_action);
    }
  } else if (result.outcome === "ORDER_CONFIRMATION_REQUEST") {
    setOutcome("clarification", "⇄", "Investment order · confirm with your face", result.order.title,
      result.order.plan
        ? "Nothing is invested until you confirm. The first instalment is invested now, then the same amount each month until you stop the plan."
        : "Nothing is bought or sold until you confirm. Price protection: the order fills within 2% of this quote, or not at all.");
    renderOrderCard(ui.candidateList, result.order, investHelpers, approveOrder);
  } else if (result.outcome === "CLARIFICATION_REQUEST" && result.reason_code === "INVESTOR_PROFILE_REQUIRED") {
    const pendingText = lastCommandText;
    setOutcome("clarification", "?", "Investor profile needed", result.question, "Your answers decide which investments and plans suit you. Nothing has been bought.");
    ui.candidateList.hidden = false;
    ui.candidateList.innerHTML = '<button type="button" class="candidate-choice">Answer 4 questions</button>';
    ui.candidateList.querySelector("button").addEventListener("click", async () => {
      const saved = await openProfileDialog(result.questions);
      if (!saved) return;
      await loadInvestments();
      if (pendingText) runCommand(pendingText);
    });
  } else if (result.outcome === "EXECUTED_SYNTHETIC_TRADE" || result.outcome === "EXECUTED_INVESTMENT_PLAN") {
    setOutcome("success", "✓", result.outcome === "EXECUTED_INVESTMENT_PLAN" ? "Investment plan started" : "Order filled",
      result.title, "Confirmed with your face and device. Your portfolio is updated below the balances.");
    if (result.portfolio) renderInvestPanel(document.querySelector("#invest-board"), result.portfolio, investHelpers);
  } else if (result.outcome === "ANSWERED" && result.investing) {
    const titles = {TRENDING: "Trending investments", PLANS: "Long-term growth plans", PORTFOLIO: "Your portfolio", QUOTE: "Price and outlook"};
    setOutcome("success", "i", titles[result.investing.view] || "Investing", result.answer,
      "Rule-based on simulated market data and your risk profile; the AI is not used. Not financial advice.");
    renderInvestingAnswer(ui.candidateList, result.investing, investHelpers, runCommand, cancelPlan);
  } else if (result.outcome === "ANSWERED") {
    const request = result.money_request;
    setOutcome("success", "i", "No approval needed", result.answer || "Done.",
      request
        ? `${request.recipient_display_name} (${request.masked_destination}) will be asked to pay you ${formatSgd(request.amount_minor)}. Requests never move money out, so no signature is needed.`
        : "Checking a balance is read-only, so no signature is needed.");
  } else if (result.outcome === "EXECUTED_SYNTHETIC_BATCH") {
    const itemCount = (result.resolved_actions || []).length;
    setOutcome("success", "✓", "Synthetic batch settled", "The complete plan moved exactly once.", `${formatSgd(result.amount_minor)} was split across ${itemCount} transfers in one atomic database transaction.`);
  } else if (result.outcome === "BATCH_CONFIRMATION_REQUEST") {
    setOutcome("clarification", "∑", "Exact batch approval required", "Confirm exact batch", "Both stored transfers will settle together. No balance changes occur until your device signs this immutable receipt.");
    const receipt = result.batch_receipt;
    const expiry = new Date(receipt.expires_at);
    const expiryLabel = Number.isNaN(expiry.getTime())
      ? receipt.expires_at
      : expiry.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
    ui.candidateList.hidden = false;
    ui.candidateList.innerHTML = `<article class="batch-receipt-card">
      <div class="batch-items">
        ${receipt.items.map(item => `<div class="batch-item-row">
          <span><small>From</small><strong>${escapeHtml(receipt.source_label)}</strong></span>
          <span><small>To</small><strong>${escapeHtml(item.recipient_display_name)}</strong>${item.masked_destination ? ` <em class="masked-account">${escapeHtml(item.masked_destination)}</em>` : ""}</span>
          <span><small>Amount</small><strong>${escapeHtml(formatSgd(item.amount_minor))}</strong></span>
        </div>`).join("")}
      </div>
      <div class="batch-total"><span>Total debit</span><strong>${escapeHtml(formatSgd(receipt.total_minor))}</strong></div>
      <p class="batch-expiry">Expires ${escapeHtml(expiryLabel)}</p>
      <button type="button" class="candidate-choice">Review &amp; sign batch</button>
    </article>`;
    ui.candidateList.querySelector(".candidate-choice").addEventListener("click", async () => {
      await approveBatchReceipt(receipt);
    });
  } else if (result.outcome === "CLARIFICATION_REQUEST") {
    if (result.reason_code === "MISSING_AMOUNT" && result.clarification) {
      setOutcome("clarification", "?", "One field is missing", "How much would you like to send?", "The gateway stored the exact source and recipient. Your reply can fill only the amount; Qwen will not run again.");
      enterClarificationMode(result);
      return;
    }
    setOutcome("clarification", "?", "Exact receipt approval required", result.question || "Which recipient did you mean?", "The AI only drafted this. Nothing moves until your device signs these exact stored facts; Qwen will not interpret the request again.");
    const choices = result.receipt_choices || [];
    ui.candidateList.hidden = false;
    ui.candidateList.innerHTML = choices.map((choice, index) => {
      const expiry = new Date(choice.expires_at);
      const expiryLabel = Number.isNaN(expiry.getTime())
        ? choice.expires_at
        : expiry.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
      const payNow = choice.destination_kind === "PAYNOW_MOBILE";
      const matchLabel = result.reason_code === "PHONETIC_RECIPIENT_MATCH"
        ? '<p class="match-label">Possible phonetic match</p>'
        : payNow ? '<p class="match-label">Via PayNow · directory verified</p>' : "";
      return `<article class="receipt-choice">
        ${matchLabel}
        <div class="receipt-facts">
          <span><small>From</small><strong>${escapeHtml(choice.source_label)}</strong></span>
          <span><small>To</small><strong>${escapeHtml(choice.recipient_display_name)}</strong>${choice.masked_destination ? ` <em class="masked-account">${escapeHtml(choice.masked_destination)}</em>` : ""}</span>
          <span><small>Amount</small><strong>${escapeHtml(formatSgd(choice.amount_minor))}</strong></span>
          ${payNow ? `<span><small>Proxy</small><strong>${escapeHtml(choice.destination_reference)}</strong></span>` : ""}
          ${choice.payment_reference ? `<span><small>Reference</small><strong>${escapeHtml(choice.payment_reference)}</strong></span>` : ""}
          ${choice.payee ? `<span><small>History</small><strong>${escapeHtml(choice.payee.new_payee ? "New payee" : [choice.payee.last_payment, choice.payee.typical].filter(Boolean).join(" · ") || "—")}</strong></span>` : ""}
          <span><small>Expires</small><strong>${escapeHtml(expiryLabel)}</strong></span>
        </div>
        <button type="button" class="candidate-choice" data-index="${index}">Review &amp; sign</button>
      </article>`;
    }).join("");
    ui.candidateList.querySelectorAll(".candidate-choice").forEach(button => {
      button.addEventListener("click", async () => {
        const choice = choices[Number(button.dataset.index)];
        await approveReceipt(choice);
      });
    });
  } else if (result.outcome === "BLOCKED" && result.reason_code === "SCAM_SUSPECTED") {
    setOutcome("blocked", "!", "Held: this looks like a scam", "Nothing was sent.", result.warning || "");
  } else if (result.outcome === "BLOCKED" && result.reason_code === "VALIDATION_FROZEN") {
    const discrepancies = (result.discrepancies || [])
      .map(field => `${field.field.replace(/^items\[\d+\]\./, "")}: drafted ${field.draft_value}, you said ${field.observed}`)
      .join("; ");
    setOutcome("blocked", "!", "Transaction frozen", "The draft contradicts what you said.", `The independent validator compared the draft with your own words and froze it. ${discrepancies}. Nothing can be signed or executed; please restate the request.`);
  } else if (result.outcome === "BLOCKED") {
    setOutcome("blocked", "!", "Action blocked", result.reason_code.replaceAll("_", " ").toLowerCase(), result.question || "Deterministic policy refused the action. Balances are unchanged.");
    if (result.reason_code === "CLARIFICATION_SCOPE_VIOLATION") {
      ui.newRequestButton.hidden = false;
    }
  } else {
    setOutcome("failed", "×", "Execution failed safely", (result.reason_code || "UNKNOWN_ERROR").replaceAll("_", " ").toLowerCase(), result.detail || "No ledger mutation occurred.");
  }
}

function setBusy(busy) {
  ui.input.disabled = busy;
  ui.runButton.disabled = busy;
  const idleLabel = activeClarification
    ? pendingClarificationResponse ? "Retry exact response" : "Continue exact request"
    : pendingActionRequest ? "Retry exact request" : "Run intention";
  ui.runButton.querySelector("span").textContent = busy
    ? activeClarification ? "Checking amount…" : "Qwen is extracting…"
    : idleLabel;
}

async function loadState() {
  const stateResponse = await fetch("/api/state");
  if (!stateResponse.ok) throw new Error(`Gateway returned HTTP ${stateResponse.status}`);
  const state = await stateResponse.json();
  renderState(state);
  ui.healthIndicator.classList.add("ready");
  ui.healthText.textContent = "Gateway ready";
  ui.modelLabel.textContent = `Model · ${state.model}`;
}

async function loadSession() {
  const response = await fetch("/api/session");
  if (!response.ok) throw new Error(`Session returned HTTP ${response.status}`);
  const session = await response.json();
  const fixtureMode = session.inference_mode === "fixture";
  ui.inferenceModeBadge.hidden = !fixtureMode;
}

function voiceUrl() {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/api/voice/session`;
}

async function releaseVoiceCapture() {
  if (voiceNode) {
    voiceNode.port.onmessage = null;
    voiceNode.disconnect();
  }
  if (voiceSource) voiceSource.disconnect();
  if (voiceMuted) voiceMuted.disconnect();
  if (voiceStream) voiceStream.getTracks().forEach(track => track.stop());
  voiceNode = null;
  voiceSource = null;
  voiceMuted = null;
  voiceStream = null;
  voiceResampler = null;
  voiceReady = false;
  ui.voiceButton.classList.remove("recording");
  ui.voiceButton.setAttribute("aria-pressed", "false");
  ui.voiceButton.removeAttribute("aria-busy");
}

function armVoiceDeadline(delayMs, message) {
  voiceDeadline.arm(delayMs, () => {
    voiceTerminal = true;
    voicePhase = "error";
    voiceFinishing = false;
    ui.voiceStatus.textContent = message;
    if (voiceSocket && voiceSocket.readyState < WebSocket.CLOSING) voiceSocket.close();
    void releaseVoiceCapture();
  });
}

async function stopVoice({sendStop = true} = {}) {
  if (sendStop && voiceReady && voiceSocket?.readyState === WebSocket.OPEN) {
    voiceSocket.send(JSON.stringify({type: "stop"}));
    ui.voiceStatus.textContent = "Transcribing…";
    voicePhase = "transcribing";
    voiceFinishing = true;
    armVoiceDeadline(20000, "Transcription timed out · try again or type the request");
  } else if (sendStop && voicePhase === "starting") {
    voiceDeadline.clear();
    voiceTerminal = true;
    voicePhase = "idle";
    ui.voiceStatus.textContent = "Voice request cancelled · typed fallback ready";
    if (voiceSocket && voiceSocket.readyState < WebSocket.CLOSING) voiceSocket.close();
  } else if (!sendStop) {
    voicePhase = "transcribing";
    voiceFinishing = true;
    armVoiceDeadline(20000, "Transcription timed out · try again or type the request");
  }
  await releaseVoiceCapture();
}

async function startVoice() {
  if (voiceFinishing) {
    ui.voiceStatus.textContent = "Finishing the previous voice request…";
    return;
  }
  if (voicePhase !== "idle") {
    await stopVoice();
    return;
  }
  const AudioContextConstructor = window.AudioContext || window.webkitAudioContext;
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode || !AudioContextConstructor) {
    ui.voiceStatus.textContent = "Microphone unavailable · use typed input";
    return;
  }
  try {
    voiceTerminal = false;
    voicePhase = "starting";
    ui.voiceStatus.textContent = "Starting speech model… wait before speaking";
    ui.voiceButton.classList.add("recording");
    ui.voiceButton.setAttribute("aria-pressed", "true");
    ui.voiceButton.setAttribute("aria-busy", "true");

    voiceStage = "audio_context";
    if (!voiceContext || voiceContext.state === "closed") {
      voiceContext = createVoiceAudioContext(window);
    }
    if (voiceContext.state === "suspended") await voiceContext.resume();
    voiceStage = "microphone";
    voiceStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    if (voiceWorkletContext !== voiceContext) {
      voiceStage = "worklet_module";
      await voiceContext.audioWorklet.addModule("/static/voice_capture_worklet.js");
      voiceWorkletContext = voiceContext;
    }
    voiceResampler = new VoiceFrameResampler(voiceContext.sampleRate);
    voiceSource = voiceContext.createMediaStreamSource(voiceStream);
    voiceStage = "worklet_node";
    voiceNode = new AudioWorkletNode(voiceContext, "dimon-voice-capture", {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    });
    voiceNode.port.onmessage = event => {
      try {
        const frames = voiceResampler?.push(event.data) || [];
        if (!voiceReady || voiceSocket?.readyState !== WebSocket.OPEN) return;
        for (const frame of frames) voiceSocket.send(frame.buffer);
      } catch (_error) {
        voiceTerminal = true;
        ui.voiceStatus.textContent = "Microphone audio unsupported · use typed input";
        if (voiceSocket?.readyState === WebSocket.OPEN) voiceSocket.close();
        void releaseVoiceCapture();
      }
    };
    voiceMuted = voiceContext.createGain();
    voiceMuted.gain.value = 0;
    voiceSource.connect(voiceNode).connect(voiceMuted).connect(voiceContext.destination);

    voiceStage = "socket";
    voiceSocket = new WebSocket(voiceUrl());
    voiceSocket.binaryType = "arraybuffer";
    voiceSocket.onmessage = event => {
      const message = JSON.parse(event.data);
      if (message.type === "ready") {
        voiceDeadline.clear();
        voiceReady = true;
        voicePhase = "listening";
        ui.voiceButton.removeAttribute("aria-busy");
        ui.voiceStatus.textContent = "Listening… pause to send";
      } else if (message.type === "pause") {
        ui.voiceStatus.textContent = "Pause detected… finishing request";
      } else if (message.type === "endpoint") {
        ui.voiceStatus.textContent = "Transcribing…";
        void stopVoice({sendStop: false});
      } else if (message.type === "transcript_partial") {
        ui.voiceStatus.textContent = message.text;
      } else if (message.type === "transcript_final") {
        ui.input.value = message.text;
        ui.voiceStatus.textContent = `Heard: ${message.text}`;
      } else if (message.type === "action_result") {
        lastRequestWasSpoken = true;
        voiceDeadline.clear();
        voiceTerminal = true;
        voicePhase = "complete";
        renderResult(message.result, 0);
        ui.voiceStatus.textContent = "Request completed · typed fallback ready";
        voiceFinishing = false;
      } else if (message.type === "error") {
        voiceDeadline.clear();
        voiceTerminal = true;
        voicePhase = "error";
        voiceFinishing = false;
        ui.voiceStatus.textContent = `${message.reason_code} · use typed input`;
        void releaseVoiceCapture();
      }
    };
    voiceSocket.onclose = () => {
      const closedPhase = voicePhase;
      voiceDeadline.clear();
      voiceSocket = null;
      voiceFinishing = false;
      if (!voiceTerminal && closedPhase !== "idle") {
        ui.voiceStatus.textContent = "Voice connection closed · try again or type the request";
      }
      voicePhase = "idle";
      void releaseVoiceCapture();
    };
    await new Promise((resolve, reject) => {
      voiceSocket.addEventListener("open", resolve, {once: true});
      voiceSocket.addEventListener("error", () => reject(new Error("voice socket unavailable")), {once: true});
    });
    if (voicePhase === "starting" && !voiceReady) {
      armVoiceDeadline(125000, "Speech model did not start · try again or type the request");
    }
  } catch (error) {
    voiceDeadline.clear();
    voiceTerminal = true;
    voicePhase = "idle";
    voiceFinishing = false;
    const failure = describeVoiceFailure(voiceStage, error);
    ui.voiceStatus.textContent = `${failure.status} (${failure.stage}/${failure.errorName})`;
    void reportVoiceFailure(failure);
    if (voiceSocket && voiceSocket.readyState < WebSocket.CLOSING) voiceSocket.close();
    await releaseVoiceCapture();
  }
}

ui.input.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    ui.form.requestSubmit();
  }
});

ui.form.addEventListener("submit", async event => {
  event.preventDefault();
  const text = ui.input.value.trim();
  if (!text) return;
  if (!activeClarification) lastCommandText = text;
  lastRequestWasSpoken = false;
  const started = performance.now();
  setBusy(true);
  setOutcome(
    "running",
    "··",
    activeClarification ? "Bounded reply" : "Inference running",
    activeClarification ? "The gateway is parsing one amount." : "Qwen is extracting a bounded intent.",
    activeClarification ? "The stored recipient and source cannot change." : "No database operation can begin until deterministic checks pass.",
  );
  ui.elapsedLabel.textContent = "Request in flight";
  try {
    if (activeClarification) {
      const coordinates = pendingClarificationResponse || {
        requestId: crypto.randomUUID(),
        text,
      };
      pendingClarificationResponse = coordinates;
      const clarificationResponse = await requestClarificationResponse(
        activeClarification,
        coordinates.requestId,
        coordinates.text,
      );
      if (clarificationResponse.kind === "UNKNOWN") {
        ui.input.readOnly = true;
        ui.outcomePanel.className = "outcome-panel clarification";
        ui.outcomeIndex.textContent = "?";
        ui.outcomeKicker.textContent = "Outcome unknown · safe retry available";
        ui.outcomeTitle.textContent = "The amount response was interrupted.";
        ui.outcomeDetail.textContent = "Retry sends the identical clarification ID, response UUID, and text. Do not start a duplicate transfer.";
        ui.elapsedLabel.textContent = `${Math.round(performance.now() - started)} ms · response unknown`;
        return;
      }
      const result = clarificationResponse.result;
      resetCommandMode({clearInput: result.outcome !== "BLOCKED"});
      renderResult(result, Math.round(performance.now() - started));
      return;
    }
    const coordinates = pendingActionRequest || {
      requestId: crypto.randomUUID(),
      text,
    };
    pendingActionRequest = coordinates;
    const actionResponse = await requestAction(coordinates);
    if (actionResponse.kind === "UNKNOWN") {
      ui.input.readOnly = true;
      ui.newRequestButton.hidden = false;
      ui.outcomePanel.className = "outcome-panel clarification";
      ui.outcomeIndex.textContent = "?";
      ui.outcomeKicker.textContent = "Outcome unknown · safe retry available";
      ui.outcomeTitle.textContent = "The first request was interrupted.";
      ui.outcomeDetail.textContent = "Retry sends the identical request UUID and text. The ledger may already have settled it exactly once.";
      setSettlement("idle", "Unknown", [["Connection", actionResponse.message]],
        "The reply was lost. Retrying sends the identical request, so nothing can happen twice.");
      ui.elapsedLabel.textContent = `${Math.round(performance.now() - started)} ms · response unknown`;
      return;
    }
    pendingActionRequest = null;
    ui.input.readOnly = false;
    renderResult(actionResponse.result, Math.round(performance.now() - started));
  } catch (error) {
    setOutcome("failed", "×", "Gateway unavailable", "The local application did not complete the request.", error.message);
    setSettlement("blocked", "Nothing moved", [], "The request did not complete; balances are unchanged.");
  } finally {
    setBusy(false);
    ui.input.focus();
  }
});

ui.resetButton.addEventListener("click", async () => {
  if (!window.confirm("Reset every synthetic balance and clear ledger history?")) return;
  ui.resetButton.disabled = true;
  try {
    const response = await fetch("/api/reset", {method: "POST"});
    if (!response.ok) throw new Error(`Reset returned HTTP ${response.status}`);
    previousBalances = new Map();
    resetCommandMode();
    renderState(await response.json());
    setOutcome("waiting", "00", "Demo reset", "The seed ledger is restored.", "Enter a new natural-language transfer when ready.");
    ui.traceList.innerHTML = '<li class="trace-step dormant"><span></span><div><strong>Awaiting intention</strong><small>No active run</small></div></li>';
    setSettlement("idle", "Idle", [], "Demo balances restored. No transfer has settled yet.");
    ui.elapsedLabel.textContent = "No active run";
  } catch (error) {
    setOutcome("failed", "×", "Reset failed", "The current ledger was left in place.", error.message);
  } finally {
    ui.resetButton.disabled = false;
  }
});

ui.newRequestButton.addEventListener("click", () => {
  resetCommandMode();
  setOutcome("waiting", "00", "Continuation cleared", "Ready for a new request.", "No server-side balance or receipt was changed.");
  ui.input.focus();
});

ui.voiceButton.addEventListener("click", () => {
  if (voicePhase !== "idle") stopVoice();
  else startVoice();
});

document.querySelectorAll(".scenario-load").forEach(button => {
  button.addEventListener("click", () => {
    resetCommandMode();
    ui.input.value = button.dataset.prompt || "";
    ui.input.focus();
    ui.input.setSelectionRange(ui.input.value.length, ui.input.value.length);
    ui.voiceStatus.textContent = "Prompt loaded · press Run intention to execute";
  });
});

document.querySelectorAll(".invest-action[data-say]").forEach(button => {
  button.addEventListener("click", () => runCommand(button.dataset.say));
});
document.querySelector("#invest-profile-button")?.addEventListener("click", async () => {
  const overview = await fetch("/api/invest", {cache: "no-store"}).then(r => r.json());
  if (await openProfileDialog(overview.questions)) await loadInvestments();
});
void loadInvestments();

Promise.all([loadSession(), loadState()]).catch(error => {
  ui.healthText.textContent = "Gateway unavailable";
  setOutcome("failed", "×", "Local app unavailable", "Start the FastAPI service and reload this page.", error.message);
});
