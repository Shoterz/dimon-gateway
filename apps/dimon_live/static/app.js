import { requestReceiptApproval } from "./receipt_approval.mjs";
import { requestBatchReceiptApproval } from "./batch_receipt_approval.mjs";
import { requestClarificationResponse } from "./clarification_response.mjs";
import { requestAction } from "./action_request.mjs";
import { accountDisplay } from "./account_display.mjs";
import { DeviceApprover, wireAssertion } from "./device_approval.mjs";
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
  commitCode: document.querySelector("#commit-code code"),
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
  SQLITE_COMMITTED: ["SQLite committed", "Balanced debit + credit"],
  CLOSE_RECIPIENT_MATCH: ["Close recipient match", "Confirmation required"],
  ACTION_CONFIRMATION_REQUIRED: ["Action confirmation", "No ledger mutation"],
  SIGNED_APPROVAL_REQUIRED: ["Draft only", "Awaiting device signature"],
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
  DATABASE_ERROR: ["Database rolled back", "No ledger mutation"],
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
  SQLITE_BATCH_COMMITTED: ["Atomic SQLite commit", "One transaction · zero-sum batch"],
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

function renderCommit(result) {
  if (result.outcome === "EXECUTED_SYNTHETIC_BATCH") {
    const actions = result.resolved_actions || [];
    const source = actions[0] || {};
    const lines = [
      "BEGIN IMMEDIATE;",
      `UPDATE accounts SET balance_minor = balance_minor - ${result.amount_minor}`,
      `  WHERE account_id = '${source.source_account_id}';`,
    ];
    actions.forEach(action => {
      lines.push(`UPDATE accounts SET balance_minor = balance_minor + ${action.amount_minor}`);
      lines.push(`  WHERE account_id = '${action.destination_account_id}';`);
    });
    lines.push(
      "INSERT INTO batch_ledger_entries (..., delta_minor)",
      `  VALUES (..., -${result.amount_minor}), ${actions.map(action => `(..., +${action.amount_minor})`).join(", ")};`,
      "-- invariant: SUM(delta_minor) = 0",
      `COMMIT;  -- ${result.batch_transaction_id}`,
    );
    ui.commitStatus.className = "commit-status committed";
    ui.commitStatus.textContent = "Atomic commit";
    ui.commitCode.textContent = lines.join("\n");
    return;
  }
  if (result.outcome !== "EXECUTED_SYNTHETIC_TRANSFER") {
    ui.commitStatus.className = "commit-status blocked";
    ui.commitStatus.textContent = "No mutation";
    ui.commitCode.textContent = `-- ${result.reason_code || result.outcome}\nROLLBACK;\n-- balances unchanged`;
    return;
  }
  const action = result.resolved_action;
  ui.commitStatus.className = "commit-status committed";
  ui.commitStatus.textContent = "Committed";
  ui.commitCode.textContent = [
    "BEGIN IMMEDIATE;",
    `UPDATE accounts SET balance_minor = balance_minor - ${action.amount_minor}`,
    `  WHERE account_id = '${action.source_account_id}';`,
    `UPDATE accounts SET balance_minor = balance_minor + ${action.amount_minor}`,
    `  WHERE account_id = '${action.destination_account_id}';`,
    "INSERT INTO ledger_entries (..., delta_minor)",
    `  VALUES (..., -${action.amount_minor}), (..., +${action.amount_minor});`,
    "-- invariant: SUM(delta_minor) = 0",
    `COMMIT;  -- ${result.transaction_id}`,
  ].join("\n");
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
  if (await deviceApprover.usableCredential(listing.credentials)) return;
  ui.elapsedLabel.textContent = "Enrolling this device for signed approval";
  await deviceApprover.enroll();
}

function describeSigner(kind) {
  return kind === "WEBAUTHN_PLATFORM"
    ? {label: "Platform biometric · Windows Hello / Touch ID", action: "Approve with biometric", hold: false}
    : {label: "Mock biometric · device-bound key (demo stand-in)", action: "Press and hold to sign", hold: true};
}

// The deterministic confirmation overlay. Every fact shown comes from the
// server-stored receipt returned with the challenge, never from model text.
function confirmOnDevice(challenge, signerKind) {
  const review = challenge.review;
  const signer = describeSigner(signerKind);
  ui.approvalItems.innerHTML = review.items.map(item => `<div class="approval-item">
      <span><small>To</small><strong>${escapeHtml(item.recipient_display_name)}</strong>${item.masked_destination ? `<em>${escapeHtml(item.masked_destination)}</em>` : ""}</span>
      <span><small>Amount</small><strong>${escapeHtml(formatSgd(item.amount_minor))}</strong></span>
    </div>`).join("");
  ui.approvalSource.textContent = review.source_label || "—";
  ui.approvalTotal.textContent = formatSgd(review.total_minor);
  ui.approvalDigest.textContent = `${review.receipt_digest.slice(0, 16)}…`;
  ui.approvalMethod.textContent = signer.label;
  ui.approvalConfirm.textContent = signer.action;
  ui.approvalConfirm.style.setProperty("--hold", "0");
  return new Promise(resolve => {
    let holdTimer = null;
    let holdStarted = 0;
    let frame = null;
    const cleanup = result => {
      clearTimeout(holdTimer);
      cancelAnimationFrame(frame);
      ui.approvalConfirm.onclick = null;
      ui.approvalConfirm.onpointerdown = null;
      ui.approvalConfirm.onpointerup = null;
      ui.approvalConfirm.onpointerleave = null;
      ui.approvalConfirm.onkeydown = null;
      ui.approvalConfirm.onkeyup = null;
      ui.approvalCancel.onclick = null;
      ui.approvalDialog.oncancel = null;
      ui.approvalDialog.close();
      resolve(result);
    };
    const startHold = () => {
      if (holdTimer) return;
      holdStarted = performance.now();
      const paint = () => {
        const progress = Math.min(1, (performance.now() - holdStarted) / 1200);
        ui.approvalConfirm.style.setProperty("--hold", String(progress));
        frame = requestAnimationFrame(paint);
      };
      paint();
      holdTimer = setTimeout(() => cleanup(true), 1200);
    };
    const stopHold = () => {
      clearTimeout(holdTimer);
      cancelAnimationFrame(frame);
      holdTimer = null;
      ui.approvalConfirm.style.setProperty("--hold", "0");
    };
    if (signer.hold) {
      ui.approvalConfirm.onpointerdown = startHold;
      ui.approvalConfirm.onpointerup = stopHold;
      ui.approvalConfirm.onpointerleave = stopHold;
      ui.approvalConfirm.onkeydown = event => {
        if ((event.key === " " || event.key === "Enter") && !event.repeat) {
          event.preventDefault();
          startHold();
        }
      };
      ui.approvalConfirm.onkeyup = stopHold;
    } else {
      ui.approvalConfirm.onclick = () => cleanup(true);
    }
    ui.approvalCancel.onclick = () => cleanup(false);
    ui.approvalDialog.oncancel = event => {
      event.preventDefault();
      cleanup(false);
    };
    ui.approvalDialog.showModal();
    ui.approvalCancel.focus();
  });
}

function renderApprovalRefusal(reasonCode, detail) {
  setOutcome("blocked", "!", "Approval not signed", reasonCode.replaceAll("_", " ").toLowerCase(), detail);
  ui.commitStatus.className = "commit-status blocked";
  ui.commitStatus.textContent = "No mutation";
  ui.commitCode.textContent = `-- ${reasonCode}\n-- no device signature, no ledger write`;
}

async function signedApproval(collection, receipt, submit, interruptedDetail) {
  const started = performance.now();
  setChoiceBusy(true);
  try {
    let assertion = signedAssertions.get(receipt.receipt_id);
    if (!assertion) {
      ui.elapsedLabel.textContent = "Requesting a one-time signing challenge";
      await ensureSigningCredential();
      const issued = await deviceApprover.requestChallenge(collection, receipt);
      if (issued.kind === "BLOCKED") {
        renderResult(issued.result, Math.round(performance.now() - started));
        return;
      }
      const credential = await deviceApprover.usableCredential(issued.challenge.allow_credentials);
      const confirmed = await confirmOnDevice(issued.challenge, credential?.kind);
      if (!confirmed) {
        renderApprovalRefusal("APPROVAL_CANCELLED", "You closed the confirmation. Nothing was signed and balances are unchanged.");
        ui.elapsedLabel.textContent = "Approval cancelled";
        return;
      }
      ui.elapsedLabel.textContent = "Waiting for the device signature";
      assertion = wireAssertion(await deviceApprover.signChallenge(issued.challenge));
      signedAssertions.set(receipt.receipt_id, assertion);
    }
    ui.elapsedLabel.textContent = "Signed approval in flight";
    const approval = await submit(receipt, assertion);
    if (approval.kind === "UNKNOWN") {
      ui.outcomePanel.className = "outcome-panel clarification";
      ui.outcomeIndex.textContent = "?";
      ui.outcomeKicker.textContent = "Outcome unknown · safe retry available";
      ui.outcomeTitle.textContent = "The gateway response was interrupted.";
      ui.outcomeDetail.textContent = interruptedDetail;
      ui.commitStatus.className = "commit-status idle";
      ui.commitStatus.textContent = "Unknown";
      ui.commitCode.textContent = `-- response interrupted: ${approval.message}\n-- ledger state unknown\n-- retry resends the identical signed approval`;
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
          <span><small>To</small><strong>${escapeHtml(item.recipient_display_name)}</strong></span>
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
          <span><small>To</small><strong>${escapeHtml(choice.recipient_display_name)}</strong></span>
          <span><small>Amount</small><strong>${escapeHtml(formatSgd(choice.amount_minor))}</strong></span>
          ${payNow ? `<span><small>Proxy</small><strong>${escapeHtml(choice.destination_reference)}</strong></span>` : ""}
          ${choice.payment_reference ? `<span><small>Reference</small><strong>${escapeHtml(choice.payment_reference)}</strong></span>` : ""}
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
  } else if (result.outcome === "BLOCKED") {
    setOutcome("blocked", "!", "Action blocked", result.reason_code.replaceAll("_", " ").toLowerCase(), "Deterministic policy refused the action. Balances are unchanged.");
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
      ui.commitStatus.className = "commit-status idle";
      ui.commitStatus.textContent = "Unknown";
      ui.commitCode.textContent = `-- response interrupted: ${actionResponse.message}\n-- ledger state unknown\n-- retry the same request UUID + text`;
      ui.elapsedLabel.textContent = `${Math.round(performance.now() - started)} ms · response unknown`;
      return;
    }
    pendingActionRequest = null;
    ui.input.readOnly = false;
    renderResult(actionResponse.result, Math.round(performance.now() - started));
  } catch (error) {
    setOutcome("failed", "×", "Gateway unavailable", "The local application did not complete the request.", error.message);
    ui.commitStatus.className = "commit-status blocked";
    ui.commitStatus.textContent = "No mutation";
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
    ui.commitStatus.className = "commit-status idle";
    ui.commitStatus.textContent = "Idle";
    ui.commitCode.textContent = "-- seed state restored\nSELECT balance_minor FROM accounts;";
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

Promise.all([loadSession(), loadState()]).catch(error => {
  ui.healthText.textContent = "Gateway unavailable";
  setOutcome("failed", "×", "Local app unavailable", "Start the FastAPI service and reload this page.", error.message);
});
