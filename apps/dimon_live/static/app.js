import { requestReceiptApproval } from "./receipt_approval.mjs";
import { requestBatchReceiptApproval } from "./batch_receipt_approval.mjs";
import { requestClarificationResponse } from "./clarification_response.mjs";
import { requestAction } from "./action_request.mjs";
import { accountDisplay } from "./account_display.mjs";
import { DeviceApprover, wireAssertion } from "./device_approval.mjs";
import { captureHeadTurn, faceMessages, openCamera, postFrames } from "./face_capture.mjs";
import { authoriseWithSingpass, digiWealthInvestedMinor, openProfileDialog, renderDigiWealthPage, renderInvestingAnswer, renderOrderCard, requestOrderApproval } from "./invest_ui.mjs";
import {
  createVoiceAudioContext,
  VoiceDeadline,
  VoiceFrameResampler,
  voiceButtonPresentation,
  voiceLocksManualSubmit,
} from "./voice_capture.mjs";
import {describeVoiceFailure, reportVoiceFailure} from "./voice_failure.mjs";
import {autoplayResultAudio, clearResultAudio, playAudioToCompletion, waitForAudioArtifact} from "./result_audio.mjs";
import {cancelPaperEquityOrder, renderPaperEquityOrder, requestPaperEquityApproval} from "./paper_equity_ui.mjs";
import {multiActionClarificationPresentation, renderMultiActionReview} from "./multi_action_review.mjs";
import {formatFundingSource, formatTransactionalMoney} from "./transaction_display.mjs";
import {activateView, classifyActionResult, openEvidence, renderHomeSummary, resolveStarterIntent, submitFromComposer, viewNameFromHash} from "./banking_shell.mjs";
import * as multiRequest from "./multi_request.mjs";
import {beginResetWarmupFromGesture, combinedWarmPresentation, demoExchangeDisclosure, initHandsFree, shouldAppendTranscriptTurn, voiceCaptionPresentation} from "./hands_free.mjs";
import {IDENTITY_VERIFICATION_FAILURE_MESSAGE} from "./identity_feedback.mjs";
import {initVoiceDiagnostics} from "./voice_diagnostics.mjs";

const ui = {
  prepareDimonButton: document.querySelector("#prepare-dimon-button"),
  prepareDimonStatus: document.querySelector("#prepare-dimon-status"),
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
  replyAudioPlayer: document.querySelector("#reply-audio-player"),
  replyAudioToggle: document.querySelector("#reply-audio-toggle"),
  replyAudioSeek: document.querySelector("#reply-audio-seek"),
  replyAudioTime: document.querySelector("#reply-audio-time"),
  replyAudioState: document.querySelector("#reply-audio-state"),
  candidateList: document.querySelector("#candidate-list"),
  planBar: document.querySelector("#plan-bar"),
  traceList: document.querySelector("#trace-list"),
  elapsedLabel: document.querySelector("#elapsed-label"),
  ledgerBody: document.querySelector("#ledger-body"),
  commitSummary: document.querySelector("#commit-summary"),
  commitNote: document.querySelector("#commit-note"),
  commitStatus: document.querySelector("#commit-status"),
  evidenceValidation: document.querySelector("#evidence-validation"),
  evidenceValidationStatus: document.querySelector("#evidence-validation-status"),
  evidenceReceipt: document.querySelector("#evidence-receipt"),
  evidenceReceiptStatus: document.querySelector("#evidence-receipt-status"),
  evidenceReceiptDetails: document.querySelector("#evidence-receipt-details"),
  exactlyOnceStatus: document.querySelector("#exactly-once-status"),
  exactlyOnceState: document.querySelector("#exactly-once-state"),
  outcomeEvidenceAction: document.querySelector("#outcome-evidence-action"),
  healthIndicator: document.querySelector("#health-indicator"),
  healthText: document.querySelector("#health-text"),
  inferenceModeBadge: document.querySelector("#inference-mode-badge"),
  modelLabel: document.querySelector("#model-label"),
  evidenceDiagnosticList: document.querySelector("#evidence-diagnostic-list"),
  voiceButton: document.querySelector("#voice-button"),
  voiceStage: document.querySelector("#ask-view"),
  askView: document.querySelector("#ask-view"),
  voiceStageStatus: document.querySelector("#voice-stage-status"),
  voiceLiveCaption: document.querySelector("#voice-live-caption"),
  voiceWaveform: document.querySelector("#voice-waveform"),
  voiceTextToggle: document.querySelector("#voice-text-toggle"),
  voiceFallbackGuidance: document.querySelector("#voice-fallback-guidance"),
  voiceStatus: document.querySelector("#voice-status"),
  conversationStatus: document.querySelector("#conversation-status"),
  agentAvailability: document.querySelector("#agent-availability"),
  suggestionList: document.querySelector("#suggestion-list"),
  intentHistory: document.querySelector("#intent-history"),
  intentHistoryCount: document.querySelector("#intent-history-count"),
  intentHistoryList: document.querySelector("#intent-history-list"),
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
  approvalVoice: document.querySelector("#approval-voice-confirmation"),
  approvalVoiceCheck: document.querySelector("#approval-voice-confirmation-check"),
  homeTotalBalance: document.querySelector("#home-total-balance"),
  homeWalletBalance: document.querySelector("#home-wallet-balance"),
  homeWealthBalance: document.querySelector("#home-wealth-balance"),
  homeAccountList: document.querySelector("#home-account-list"),
  homeActivityList: document.querySelector("#home-activity-list"),
  recentActivityList: document.querySelector("#recent-activity-list"),
  homeForm: document.querySelector("#home-command-form"),
  homeInput: document.querySelector("#home-command-input"),
  homeVoiceButton: document.querySelector("#home-voice-button"),
  homeTypeToggle: document.querySelector("#home-type-toggle"),
  homeTypingRow: document.querySelector("#home-typing-row"),
  payeeList: document.querySelector("#payee-list"),
  assistantTranscript: document.querySelector("#assistant-transcript"),
  assistantCategory: document.querySelector("#assistant-category"),
};

const voiceDiagnostics = initVoiceDiagnostics();
let pendingVoiceTraceId = null;
let preparationPollTimer = 0;
let preparationStatusClearTimer = 0;
let lastPreparationSnapshot = {state: "IDLE", stages: []};
let handsFreeReadiness = {state: "IDLE", audio: false, microphone: false, socket: false};

function renderPreparation(snapshot = {}, measuredTraceId = null) {
  if (!ui.prepareDimonButton) return;
  lastPreparationSnapshot = snapshot;
  const state = snapshot.state || "IDLE";
  const failed = Array.isArray(snapshot.stages)
    ? snapshot.stages.filter(stage => stage?.state === "FAILED").map(stage => stage.name)
    : [];
  const activeVoiceState = ui.voiceStage?.dataset.voiceState || "idle";
  const presentation = combinedWarmPresentation(snapshot, handsFreeReadiness, activeVoiceState);
  const statusText = failed.length ? `Unavailable: ${failed.join(", ")}` : presentation.status;
  ui.prepareDimonButton.dataset.state = presentation.state;
  ui.prepareDimonButton.setAttribute("aria-label", presentation.label);
  ui.prepareDimonButton.disabled = state === "PREPARING" || presentation.state === "preparing" || Boolean(presentation.active);
  if (ui.prepareDimonStatus) ui.prepareDimonStatus.textContent = statusText;
  if (state === "READY" && measuredTraceId) {
    wakeWord?.markBackendReady?.(measuredTraceId);
  }
  window.clearTimeout(preparationPollTimer);
  window.clearTimeout(preparationStatusClearTimer);
  if (state === "PREPARING") {
    preparationPollTimer = window.setTimeout(
      () => void refreshPreparation(false, measuredTraceId), 1000,
    );
  } else if (state === "READY" || state === "DEGRADED") {
    // The gateway actively probes Nemotron while warm. Keep the badge honest
    // if a later probe fails, recovers, or the ready window expires.
    preparationPollTimer = window.setTimeout(
      () => void refreshPreparation(false, measuredTraceId), 15_000,
    );
  }
  if (statusText && presentation.state !== "preparing" && !presentation.active) {
    preparationStatusClearTimer = window.setTimeout(() => {
      if (ui.prepareDimonStatus) ui.prepareDimonStatus.textContent = "";
    }, 3500);
  }
}

async function refreshPreparation(start = false, measuredTraceId = null) {
  if (!ui.prepareDimonButton) return {state: "DEGRADED", stages: []};
  try {
    const response = await fetch("/api/demo/prepare", {
      method: start ? "POST" : "GET",
      headers: start ? {"Content-Type": "application/json"} : undefined,
      body: start ? "{}" : undefined,
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`HTTP${response.status}`);
    const snapshot = await response.json();
    renderPreparation(snapshot, measuredTraceId);
    return snapshot;
  } catch (_error) {
    const snapshot = {state: "DEGRADED", stages: []};
    renderPreparation(snapshot);
    if (ui.prepareDimonStatus) ui.prepareDimonStatus.textContent = "Services could not be warmed · try again";
    return snapshot;
  }
}

async function resetAndWarmPreparation(traceId) {
  let snapshot = await refreshPreparation(true, traceId);
  const deadline = Date.now() + 150_000;
  while (snapshot.state === "PREPARING" && Date.now() < deadline) {
    await new Promise(resolve => window.setTimeout(resolve, 750));
    snapshot = await refreshPreparation(false, traceId);
  }
  if (snapshot.state === "PREPARING") {
    snapshot = {
      state: "DEGRADED",
      stages: [{name: "capacity", state: "FAILED", reason_code: "GPU_RESET_TIMEOUT"}],
    };
    renderPreparation(snapshot, traceId);
  }
  return snapshot;
}

ui.prepareDimonButton?.addEventListener("click", () => {
  const traceId = crypto.randomUUID();
  pendingVoiceTraceId = traceId;
  if (wakeWord?.beginTrace) {
    wakeWord.beginTrace(traceId);
    pendingVoiceTraceId = null;
  }
  voiceDiagnostics.markRunStarted();
  void beginResetWarmupFromGesture(
    () => wakeWord?.unlockAudioFromGesture() || Promise.resolve(handsFreeReadiness),
    () => resetAndWarmPreparation(traceId),
    () => wakeWord?.prepareFromGesture() || Promise.resolve(handsFreeReadiness),
  );
});
void refreshPreparation();

let previousBalances = new Map();
let activeClarification = null;
let pendingClarificationResponse = null;
let pendingActionRequest = null;
let unknownActionRetryInFlight = false;
let evidenceDiagnostics = [];
let activeConversation = null;
let activeMultiActionResponse = null;
let pendingConversationTurn = null;
let voiceSocket = null;
let voiceContext = null;
let voiceStream = null;
let voiceNode = null;
let voiceSource = null;
let voiceMuted = null;
let voiceResampler = null;

function recordEvidenceDiagnostic(stage, error = {}) {
  const diagnostic = {
    stage: String(stage || "unknown").slice(0, 64),
    errorName: String(error.name || "Error").slice(0, 64),
    reasonCode: String(error.reasonCode || error.reason_code || "").slice(0, 96),
    httpStatus: Number(error.httpStatus || error.status || 0) || null,
  };
  evidenceDiagnostics = [...evidenceDiagnostics.slice(-9), diagnostic];
  if (ui.evidenceDiagnosticList) {
    ui.evidenceDiagnosticList.innerHTML = evidenceDiagnostics.map(item => `<li>
      <strong>${escapeHtml(item.stage)}</strong>
      <span>${escapeHtml(item.errorName)}</span>
      ${item.reasonCode ? `<code>${escapeHtml(item.reasonCode)}</code>` : ""}
      ${item.httpStatus ? `<span>HTTP ${item.httpStatus}</span>` : ""}
    </li>`).join("");
  }
  return diagnostic;
}
let voiceWorkletContext = null;
let voiceReady = false;
let voiceTerminal = false;
let voicePhase = "idle";
let voiceFinishing = false;
let voiceStage = "audio_context";
let voiceResumeRequested = false;
let lastRequestWasSpoken = false;
let pendingOnScreenReview = false;
let activeEvidenceContext = null;
let replyAudioGeneration = 0;
const spokenIntentHistory = [];
const sessionActivity = [];
const sessionActivityIds = new Set();
const voiceDeadline = new VoiceDeadline();
const escapeHtml = value => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");
const formatSgd = minor => formatTransactionalMoney("SGD", minor);
const formatPaperMoney = (currency, minor) => formatTransactionalMoney(currency, minor);

function setAssistantMode(mode, status = null) {
  if (!ui.voiceStage) return;
  ui.voiceStage.dataset.voiceState = mode;
  if (mode === "speaking") {
    ui.voiceWaveform?.querySelectorAll("span").forEach(bar => bar.style.removeProperty("height"));
  }
  const copy = {
    idle: {status: "Ready when you are", button: "Start listening"},
    starting: {status: "Connecting to the microphone · tap Cancel to stop", button: "Cancel microphone startup"},
    listening: {status: "Listening · tap Finish when done", button: "Finish recording and send"},
    understanding: {status: "Dimon is working on your request", button: "Dimon is processing your request"},
    speaking: {status: "Dimon is speaking · tap Stop reply to interrupt", button: "Stop Dimon’s reply"},
    error: {status: "Voice input unavailable · try again or type", button: "Try voice input again"},
    review: {status: "Review the details below", button: "Start another request"},
  }[mode] || {status: "Ready when you are", button: "Start listening"};
  if (ui.voiceStageStatus) ui.voiceStageStatus.textContent = status || copy.status;
  if (ui.voiceButton) {
    const presentation = voiceButtonPresentation(mode);
    ui.voiceButton.setAttribute("aria-label", presentation.ariaLabel || copy.button);
    ui.voiceButton.setAttribute("aria-pressed", String(mode === "listening"));
    ui.voiceButton.dataset.action = presentation.action;
    ui.voiceButton.disabled = presentation.disabled;
    const label = ui.voiceButton.querySelector("span");
    if (label) label.textContent = presentation.label;
  }
  if (ui.agentAvailability) ui.agentAvailability.textContent = mode === "idle" ? "Online"
    : mode === "review" ? "Review ready" : mode === "listening" ? "Listening"
      : mode === "speaking" ? "Speaking" : "Working";
  renderPreparation(lastPreparationSnapshot);
}

function initializeVoiceWaveform() {
  if (!ui.voiceWaveform) return;
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < 27; index += 1) {
    const bar = document.createElement("span");
    bar.style.setProperty("--bar-index", String(index));
    fragment.append(bar);
  }
  ui.voiceWaveform.append(fragment);
}

function setVoiceLevel(samples) {
  if (!ui.voiceWaveform) return;
  let energy = 0;
  for (let index = 0; index < samples.length; index += 1) energy += samples[index] * samples[index];
  const rms = samples.length ? Math.sqrt(energy / samples.length) : 0;
  const level = Math.min(1, Math.max(0, (rms - 0.006) * 10));
  ui.voiceWaveform.querySelectorAll("span").forEach((bar, index) => {
    const peak = index % 4 === 0 ? 19 : index % 3 === 0 ? 14 : 10;
    bar.style.height = `${4 + level * peak}px`;
  });
}

initializeVoiceWaveform();

function stopReplyAudio({keepReview = pendingOnScreenReview} = {}) {
  replyAudioGeneration += 1;
  ui.resultAudio?.pause();
  setAssistantMode(keepReview ? "review" : "idle");
}

function formatReplyTime(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

function syncReplyAudioPlayer() {
  const audio = ui.resultAudio;
  if (!audio || !ui.replyAudioPlayer) return;
  const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
  const current = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
  const playing = !audio.paused && !audio.ended;
  const progress = duration > 0 ? Math.min(1000, Math.round(current / duration * 1000)) : 0;
  ui.replyAudioPlayer.dataset.playbackState = playing ? "playing" : audio.ended ? "ended" : "ready";
  ui.replyAudioPlayer.style.setProperty("--reply-progress", `${duration > 0 ? progress / 10 : 0}%`);
  if (ui.replyAudioToggle) {
    ui.replyAudioToggle.setAttribute("aria-label", playing ? "Pause Dimon’s reply" : audio.ended ? "Replay Dimon’s reply" : "Play Dimon’s reply");
    ui.replyAudioToggle.setAttribute("aria-pressed", String(playing));
  }
  if (ui.replyAudioSeek) {
    ui.replyAudioSeek.value = String(progress);
    ui.replyAudioSeek.disabled = duration <= 0;
    ui.replyAudioSeek.setAttribute("aria-valuetext", `${formatReplyTime(current)} of ${formatReplyTime(duration)}`);
  }
  if (ui.replyAudioTime) ui.replyAudioTime.textContent = `${formatReplyTime(current)} / ${formatReplyTime(duration)}`;
  if (ui.replyAudioState) {
    ui.replyAudioState.textContent = playing ? "Speaking now"
      : audio.ended ? "Reply complete · replay anytime"
        : current > 0 ? "Paused · resume anytime" : "Voice reply · tap to listen";
  }
}

ui.resultAudio?.addEventListener("loadedmetadata", syncReplyAudioPlayer);
ui.resultAudio?.addEventListener("durationchange", syncReplyAudioPlayer);
ui.resultAudio?.addEventListener("timeupdate", syncReplyAudioPlayer);
ui.resultAudio?.addEventListener("play", syncReplyAudioPlayer);
ui.resultAudio?.addEventListener("pause", syncReplyAudioPlayer);
ui.resultAudio?.addEventListener("ended", syncReplyAudioPlayer);
ui.resultAudio?.addEventListener("emptied", syncReplyAudioPlayer);
for (const event of ["play", "pause", "ended", "emptied"]) ui.resultAudio?.addEventListener(event, () => syncWakeWord());
ui.resultAudio?.addEventListener("ended", () => {
  setAssistantMode(pendingOnScreenReview ? "review" : "idle", "Ready for your next request");
});

ui.replyAudioToggle?.addEventListener("click", async () => {
  if (!ui.resultAudio) return;
  if (!ui.resultAudio.paused) {
    ui.resultAudio.pause();
    setAssistantMode(pendingOnScreenReview ? "review" : "idle", "Reply paused · tap play to continue");
    return;
  }
  if (ui.resultAudio.ended) ui.resultAudio.currentTime = 0;
  setAssistantMode("speaking", "Dimon is speaking");
  try {
    await ui.resultAudio.play();
  } catch (_error) {
    void reportVoiceFailure({
      stage: "reply_audio_playback",
      errorName: _error?.name || "PlaybackError",
    });
    setAssistantMode(pendingOnScreenReview ? "review" : "idle", "Tap play to hear Dimon’s reply");
  }
  syncReplyAudioPlayer();
});

ui.replyAudioSeek?.addEventListener("input", () => {
  if (!ui.resultAudio || !Number.isFinite(ui.resultAudio.duration) || ui.resultAudio.duration <= 0) return;
  ui.resultAudio.currentTime = ui.resultAudio.duration * Number(ui.replyAudioSeek.value) / 1000;
  syncReplyAudioPlayer();
});

function resumeVoiceCapture() {
  if (!ui.askView || ui.askView.hidden) return;
  if (voiceSocket && voiceSocket.readyState !== WebSocket.CLOSED) {
    voiceResumeRequested = true;
    return;
  }
  if (voicePhase !== "idle") {
    voiceResumeRequested = true;
    return;
  }
  voiceResumeRequested = false;
  void startAudioWorkletVoice();
}

function showReplyAudioUnavailable(autoListen, status = "I couldn’t play Dimon’s last reply · continue by voice or type") {
  setAssistantMode(pendingOnScreenReview ? "review" : "idle", status);
  if (autoListen) resumeVoiceCapture();
}

async function speakReply(result, {autoListen = false} = {}) {
  const generation = ++replyAudioGeneration;
  if (document.body.dataset.handsFree === "on") return;
  const artifactId = result?.audio_artifact_id;
  if (!artifactId && ui.resultAudio) {
    // Some safe screen-only results do not carry a server audio artifact.
    // Keep them visible instead of sending arbitrary result prose for speech.
    setAssistantMode(pendingOnScreenReview ? "review" : "idle");
    if (autoListen) resumeVoiceCapture();
    return;
  }
  if (!artifactId || !ui.resultAudio) {
    void reportVoiceFailure({
      stage: "reply_audio_fetch",
      errorName: artifactId ? "AudioElementUnavailable" : "NoArtifact",
      requestId: result?.request_id,
    });
    showReplyAudioUnavailable(
      autoListen,
      autoListen ? "I couldn’t play Dimon’s last reply · reopening the microphone" : undefined,
    );
    return;
  }
  setAssistantMode("speaking", "Dimon is speaking");
  const audioUrl = await waitForAudioArtifact(ui.resultAudio, `/api/audio/${encodeURIComponent(artifactId)}`, {
    isCurrent: () => generation === replyAudioGeneration,
    onFailure: errorName => reportVoiceFailure({
      stage: "reply_audio_fetch",
      errorName,
      requestId: result?.request_id,
    }),
  });
  if (generation !== replyAudioGeneration) return;
  if (!audioUrl) {
    showReplyAudioUnavailable(
      autoListen,
      autoListen ? "I couldn’t play Dimon’s last reply · reopening the microphone" : undefined,
    );
    return;
  }
  if (ui.replyAudioPlayer) ui.replyAudioPlayer.hidden = false;
  syncReplyAudioPlayer();
  const played = await autoplayResultAudio(ui.resultAudio, {
    onEnded: () => {
      if (generation !== replyAudioGeneration) return;
      setAssistantMode(pendingOnScreenReview ? "review" : "idle", "Ready for your next request");
      if (autoListen) resumeVoiceCapture();
    },
    onFailure: errorName => reportVoiceFailure({
      stage: "reply_audio_playback",
      errorName,
      requestId: result?.request_id,
    }),
  });
  if (generation !== replyAudioGeneration) return;
  if (!played) {
    showReplyAudioUnavailable(
      autoListen,
      autoListen ? "Tap play to hear Dimon’s reply · reopening the microphone" : "Tap play to hear Dimon’s reply",
    );
  }
}

async function playHandsFreeSpeechBlob(blob, {
  signal = null,
  onReady = () => {},
  onStarted = () => {},
} = {}) {
  if (!ui.resultAudio || !ui.replyAudioPlayer || !(blob instanceof Blob)) return "failed";
  clearResultAudio(ui.resultAudio);
  const objectUrl = URL.createObjectURL(blob);
  ui.resultAudio.dataset.resultAudioObjectUrl = objectUrl;
  ui.resultAudio.src = objectUrl;
  ui.resultAudio.preload = "metadata";
  ui.resultAudio.hidden = false;
  ui.resultAudio.load();
  onReady();
  ui.replyAudioPlayer.hidden = false;
  syncReplyAudioPlayer();
  const pauseOnAbort = () => ui.resultAudio?.pause();
  signal?.addEventListener("abort", pauseOnAbort, {once: true});
  try {
    return await playAudioToCompletion(ui.resultAudio, {
      signal,
      onStarted,
      onBlocked: errorName => {
        void reportVoiceFailure({stage: "hands_free_audio_playback", errorName});
        setAssistantMode("speaking", "Tap play to hear Dimon");
        syncReplyAudioPlayer();
      },
      onFailure: errorName => {
        void reportVoiceFailure({stage: "hands_free_audio_playback", errorName});
        setAssistantMode(pendingOnScreenReview ? "review" : "idle", "I couldn’t play Dimon’s reply");
      },
    });
  } finally {
    signal?.removeEventListener("abort", pauseOnAbort);
  }
}

function appendAssistantMessage(role, text) {
  if (!ui.assistantTranscript || !text) return;
  const last = ui.assistantTranscript.lastElementChild;
  const lastRole = last?.classList.contains("user-message") ? "user"
    : last?.classList.contains("assistant-message") ? "assistant" : null;
  const lastText = last?.querySelector("p")?.textContent || "";
  if (!shouldAppendTranscriptTurn(lastRole, lastText, role, text)) return;
  const item = document.createElement("li");
  item.className = `transcript-turn ${role === "user" ? "user-message" : "assistant-message"}`;
  item.setAttribute("aria-label", `${role === "user" ? "You said" : "Dimon said"}: ${text}`);
  const header = document.createElement("div");
  header.className = "transcript-turn-header";
  const avatar = document.createElement("span");
  avatar.className = "transcript-turn-avatar";
  avatar.setAttribute("aria-hidden", "true");
  avatar.textContent = role === "user" ? "Y" : "D";
  const label = document.createElement("span");
  label.className = "transcript-turn-speaker";
  label.textContent = role === "user" ? "You" : "Dimon";
  const message = document.createElement("p");
  message.textContent = text;
  header.append(avatar, label);
  item.append(header, message);
  ui.assistantTranscript.append(item);
  ui.askView?.classList.add("conversation-started");
  scrollConversationToLatest(item);
}

function resetChatTranscript() {
  if (!ui.assistantTranscript || !ui.outcomePanel) return;
  const welcome = document.createElement("li");
  welcome.className = "assistant-message welcome-message";
  const speaker = document.createElement("span");
  speaker.textContent = "Dimon";
  const message = document.createElement("p");
  message.textContent = "I can help with your accounts, payments, and investments. What would you like to do?";
  welcome.append(speaker, message);
  ui.outcomePanel.hidden = true;
  if (ui.candidateList) {
    ui.candidateList.hidden = true;
    ui.candidateList.innerHTML = "";
  }
  if (ui.planBar) {
    ui.planBar.hidden = true;
    ui.planBar.innerHTML = "";
  }
  ui.assistantTranscript.replaceChildren(welcome, ui.outcomePanel);
  ui.askView?.classList.remove("conversation-started");
}

function scrollConversationToLatest(element = ui.assistantTranscript?.lastElementChild) {
  if (!element || !ui.assistantTranscript) return;
  const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth";
  element.scrollIntoView({block: "nearest", behavior});
}

function autoGrowComposer() {
  if (!ui.input) return;
  ui.input.style.height = "auto";
  ui.input.style.height = `${Math.min(ui.input.scrollHeight, 128)}px`;
}

function showTypedFallback({focus = true} = {}) {
  ui.form.hidden = false;
  ui.voiceTextToggle.setAttribute("aria-expanded", "true");
  ui.voiceTextToggle.textContent = "Close typing";
  if (focus) ui.input.focus();
}

function hideTypedFallback() {
  ui.form.hidden = true;
  ui.voiceTextToggle.setAttribute("aria-expanded", "false");
  ui.voiceTextToggle.textContent = "Type instead";
}

function beginAssistantTurn() {
  document.querySelector("#thinking-message")?.remove();
  if (ui.conversationStatus) ui.conversationStatus.textContent = "Dimon is thinking.";
  setAssistantMode("understanding");
}

function endAssistantTurn(message = "Dimon replied.") {
  document.querySelector("#thinking-message")?.remove();
  if (ui.conversationStatus) ui.conversationStatus.textContent = message;
  if (ui.agentAvailability) ui.agentAvailability.textContent = "Online";
}

function setComposerDisabled(disabled) {
  ui.input.disabled = disabled;
  if (ui.homeInput) ui.homeInput.disabled = disabled;
  if (ui.homeVoiceButton) ui.homeVoiceButton.disabled = disabled;
}

function renderIntentHistory() {
  if (!ui.intentHistory || !ui.intentHistoryCount || !ui.intentHistoryList) return;
  ui.intentHistoryCount.textContent = String(spokenIntentHistory.length);
  ui.intentHistoryList.innerHTML = spokenIntentHistory.length
    ? spokenIntentHistory.map(item => `<li><span>You said</span><p>${escapeHtml(item)}</p></li>`).join("")
    : '<li class="intent-history-empty">Voice requests will stay here for this session.</li>';
}

function recordSpokenIntent(text) {
  const normalized = typeof text === "string" ? text.trim() : "";
  if (!normalized) return;
  spokenIntentHistory.unshift(normalized);
  spokenIntentHistory.splice(8);
  renderIntentHistory();
}

function setVoicePhase(phase) {
  voicePhase = phase;
  ui.runButton.disabled = voiceLocksManualSubmit(phase);
  syncWakeWord();
}

// The "Hey Dimon" listener waits while Speak or a spoken reply has the
// microphone, and comes back once both are done.
let wakeWord = null;
let wakeWordTimer = null;

function setActiveConversation(conversation) {
  activeConversation = conversation?.status === "WAITING" ? conversation : null;
  wakeWord?.setConversationContext(activeConversation);
  return activeConversation;
}
const VOICE_SILENCE_MS = 12000;
let lastHeardText = "";
let voiceSilenceTimer = null;
const speakIsBusy = () => voicePhase !== "idle" || voiceFinishing || voiceResumeRequested
  || Boolean(ui.resultAudio && !ui.resultAudio.paused && !ui.resultAudio.ended);

function syncWakeWord() {
  if (!wakeWord) return;
  clearTimeout(wakeWordTimer);
  if (speakIsBusy()) {
    wakeWord.pause();
    return;
  }
  // A reply may reopen the microphone right after it ends; wait a moment.
  wakeWordTimer = setTimeout(() => {
    if (speakIsBusy()) syncWakeWord();
    else wakeWord.resume();
  }, 1200);
}

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
  ui.ledgerBody.innerHTML = transactions.map(transaction => `<tr data-evidence-receipt="${escapeHtml(transaction.transaction_id || "")}" tabindex="-1">
    <td>${escapeHtml(transaction.source_label)} → ${escapeHtml(transaction.destination_label)}</td>
    <td>${formatSgd(transaction.amount_minor)}</td>
    <td><span class="state-pill">Committed</span></td>
    <td class="transaction-id">${escapeHtml(String(transaction.transaction_id || "").slice(0, 14))}…</td>
  </tr>`).join("");
}

const investHelpers = {
  escapeHtml: value => escapeHtml(value),
  formatSgd: minor => formatSgd(minor),
  formatTransactionalMoney,
  formatFundingSource,
};
let latestBankingState = null;
let latestInvestmentOverview = null;
let lastCommandText = "";

function showWealthExploreUnavailable() {
  const results = document.querySelector("#wealth-explore-results");
  const count = document.querySelector("#wealth-explore-count");
  if (results) results.innerHTML = '<div class="wealth-explore-empty" role="status"><strong>Explore is temporarily unavailable</strong><p>Your portfolio overview and plans remain available. Try Explore again shortly.</p></div>';
  if (count) count.textContent = "Unavailable";
}

function runCommand(text) {
  resetCommandMode();
  ui.input.value = text;
  ui.form.requestSubmit();
}

let pendingOrdersTimer = null;

function announceSettledOrders(settled) {
  for (const order of settled) {
    const name = order.instrument?.name || order.instrument?.symbol || "Your shares";
    const shares = `${order.quantity} ${Number(order.quantity) === 1 ? "share" : "shares"}`;
    const selling = order.side === "SELL";
    const trade = order.trade || {};
    const text = order.status === "FILLED"
      ? `${selling ? "Sold" : "Bought"} ${shares} of ${name} at ${formatPaperMoney(trade.quote_currency || order.instrument?.quote_currency || "USD", trade.fill_price_minor)} each. Your waiting order has filled.`
      : `Your order to ${selling ? "sell" : "buy"} ${shares} of ${name} didn't fill (${reasonLabel(order.reason_code || order.status)}). Nothing moved.`;
    appendAssistantMessage("assistant", text);
    recordExecutedActions({paper_order_result: {...order, status: order.status, trade: order.trade}});
    void loadState().catch(() => null);
  }
}

document.addEventListener("click", async event => {
  const button = event.target.closest?.("[data-cancel-stock-order]");
  if (!button) return;
  button.disabled = true;
  button.textContent = "Cancelling…";
  try {
    const result = await cancelPaperEquityOrder({order_id: button.dataset.cancelStockOrder, digest: button.dataset.orderDigest});
    button.textContent = result.status === "CANCELLED" ? "Cancelled" : "Already filled";
    appendAssistantMessage("assistant", result.status === "CANCELLED"
      ? "Cancelled. That order won't fill, and nothing moved."
      : "That order had already completed, so it couldn't be cancelled.");
  } catch (_error) {
    button.disabled = false;
    button.textContent = "Cancel";
  }
  void loadInvestments();
});

async function loadInvestments() {
  try {
    const response = await fetch("/api/invest", {cache: "no-store"});
    if (!response.ok) {
      showWealthExploreUnavailable();
      return null;
    }
    const overview = await response.json();
    latestInvestmentOverview = overview;
    renderDigiWealthPage(document.querySelector("#wealth-view"), overview, investHelpers);
    renderHomeSummaryFromLatestState();
    announceSettledOrders(overview.settled_orders || []);
    // Orders waiting for the market are checked again every minute.
    window.clearTimeout(pendingOrdersTimer);
    if ((overview.paper_equity?.pending_orders || []).length) {
      pendingOrdersTimer = window.setTimeout(() => { void loadInvestments(); }, 60_000);
    }
    return overview;
  } catch (_error) {
    showWealthExploreUnavailable();
    return null;
  }
}

async function cancelPlan(planId) {
  await fetch(`/api/invest/plans/${encodeURIComponent(planId)}/cancel`, {method: "POST"});
  await loadInvestments();
}

function renderHomeSummaryFromLatestState() {
  if (!latestBankingState) return;
  const home = renderHomeSummary(
    latestBankingState,
    {escapeHtml, formatSgd},
    digiWealthInvestedMinor(latestInvestmentOverview || {}),
  );
  if (ui.homeTotalBalance) ui.homeTotalBalance.textContent = formatSgd(home.totalMinor);
  if (ui.homeWalletBalance) ui.homeWalletBalance.textContent = formatSgd(home.walletMinor);
  if (ui.homeWealthBalance) ui.homeWealthBalance.textContent = formatSgd(home.wealthMinor);
  if (ui.homeAccountList) ui.homeAccountList.innerHTML = home.accountsHtml;
  if (ui.homeActivityList) ui.homeActivityList.innerHTML = home.activityHtml;
}

function renderSessionActivity() {
  if (!ui.recentActivityList) return;
  if (!sessionActivity.length) {
    ui.recentActivityList.innerHTML = '<div class="bank-empty-state"><strong>No completed actions yet</strong><span>Completed activity will appear here as you use Dimon.</span></div>';
    return;
  }
  ui.recentActivityList.innerHTML = sessionActivity.map(item => `
    <article class="activity-row">
      <span class="activity-icon activity-icon-${item.kind}" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none">${item.icon}</svg></span>
      <div>
        <strong>${escapeHtml(item.title)}</strong>
        <span>${escapeHtml([item.category, item.detail, item.time].filter(Boolean).join(" · "))}</span>
      </div>
      <strong class="activity-amount ${item.direction === "in" ? "activity-credit" : ""}">${item.direction === "in" ? "+" : "−"}${escapeHtml(item.amount)}</strong>
    </article>`).join("");
}

function addSessionActivity({id, category, kind, title, detail = "", amount, direction = "out", icon}) {
  if (id && sessionActivityIds.has(id)) return;
  if (id) sessionActivityIds.add(id);
  sessionActivity.unshift({
    category,
    kind,
    title: title || category,
    detail,
    amount: amount || "",
    direction,
    icon,
    time: new Date().toLocaleString("en-SG", {dateStyle: "medium", timeStyle: "short"}),
  });
  renderSessionActivity();
}

function recordExecutedActions(result = {}) {
  const outcome = result.outcome;
  if (outcome === "EXECUTED_SYNTHETIC_BILL_PAYMENT") {
    const action = result.resolved_action || {};
    addSessionActivity({
      id: result.transaction_id,
      category: "Bills",
      kind: "bill",
      title: action.biller_display_name || action.recipient_display_name || "Bill payment",
      detail: action.payment_reference ? `Ref ${action.payment_reference}` : "Bill paid",
      amount: formatSgd(action.amount_minor || result.amount_minor || 0),
      icon: '<path d="M6 3h9l3 3v15H6zM9 11h6M9 15h6M15 3v4h4"/>',
    });
    return;
  }
  if (outcome === "EXECUTED_SYNTHETIC_TRANSFER") {
    const action = result.resolved_action || {};
    const payNow = action.destination_kind === "PAYNOW_MOBILE";
    addSessionActivity({
      id: result.transaction_id,
      category: payNow ? "PayNow" : "Payment",
      kind: payNow ? "paynow" : "payment",
      title: action.recipient_display_name || "Transfer",
      detail: payNow ? ["PayNow", action.masked_destination].filter(Boolean).join(" · ") : "Transfer sent",
      amount: formatSgd(action.amount_minor || result.amount_minor || 0),
      icon: payNow
        ? '<path d="M5 12h14M13 6l6 6-6 6"/>'
        : '<path d="M7 17 17 7M9 7h8v8"/>',
    });
    return;
  }
  if (outcome === "EXECUTED_SYNTHETIC_BATCH") {
    (result.resolved_actions || []).forEach((action, index) => {
      const payNow = action.destination_kind === "PAYNOW_MOBILE";
      addSessionActivity({
        id: `${result.batch_transaction_id || result.request_id || "batch"}:${index}`,
        category: payNow ? "PayNow" : "Payment",
        kind: payNow ? "paynow" : "payment",
        title: action.recipient_display_name || "Transfer",
        detail: payNow ? ["PayNow", action.masked_destination].filter(Boolean).join(" · ") : "Transfer sent",
        amount: formatSgd(action.amount_minor || 0),
        icon: payNow
          ? '<path d="M5 12h14M13 6l6 6-6 6"/>'
          : '<path d="M7 17 17 7M9 7h8v8"/>',
      });
    });
    return;
  }
  const paper = result.paper_order_result;
  if (paper?.status === "FILLED") {
    const trade = paper.trade || {};
    const symbol = trade.symbol || paper.instrument?.symbol || "";
    const shares = Number(trade.quantity || 0);
    addSessionActivity({
      id: trade.trade_id || paper.order_id || result.request_id,
      category: "Stocks",
      kind: "stock",
      title: `${paper.instrument?.name || symbol || "Stock"}${symbol ? ` (${symbol})` : ""}`,
      detail: `${shares} ${shares === 1 ? "share" : "shares"} ${trade.side === "SELL" ? "sold" : "bought"}`,
      amount: formatPaperMoney(trade.funding_currency || "SGD", trade.side === "SELL" ? trade.funding_credit_minor || 0 : trade.funding_debit_minor || 0),
      direction: trade.side === "SELL" ? "in" : "out",
      icon: '<path d="M4 18V6M4 18h16M7 14l4-4 3 2 5-6"/>',
    });
    return;
  }
  if (outcome === "EXECUTED_SYNTHETIC_TRADE" || outcome === "EXECUTED_INVESTMENT_PLAN") {
    (result.trades || []).forEach((trade, index) => {
      const shares = Number(trade.quantity || 0);
      const side = String(trade.side || "BUY").toUpperCase();
      const cashMinor = Number(trade.cash_minor ?? (Number(trade.price_minor || 0) * shares));
      addSessionActivity({
        id: `${result.order_id || result.request_id || "trade"}:${index}`,
        category: "Stocks",
        kind: "stock",
        title: `${trade.name || trade.symbol || "Stock"}${trade.symbol ? ` (${trade.symbol})` : ""}`,
        detail: `${shares} ${shares === 1 ? "share" : "shares"} ${side === "SELL" ? "sold" : "bought"}`,
        amount: formatSgd(Math.abs(cashMinor)),
        direction: side === "SELL" ? "in" : "out",
        icon: '<path d="M4 18V6M4 18h16M7 14l4-4 3 2 5-6"/>',
      });
    });
  }
}

function renderState(state) {
  latestBankingState = state;
  renderAccounts(state.accounts || []);
  renderLedger(state.transactions || []);
  renderHomeSummaryFromLatestState();
  return state;
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
  BILL_PAYMENT_INTENT_EXTRACTED: ["Bill request understood", "Extracted from your words"],
  BILL_PAYMENT_PROPOSAL_GROUNDED: ["Bill details checked", "Amount and evidence matched"],
  BILLER_DIRECTORY_MATCHED: ["Biller matched", "Synthetic demo directory"],
  NO_APPROVAL_NEEDED: ["No approval needed", "Nothing leaves your account"],
  INVESTMENT_REQUEST_UNDERSTOOD: ["Investing request", "Fixed rules · no AI"],
  INVESTMENT_PORTFOLIO: ["Portfolio", "Live simulated prices"],
  INVESTMENT_TRENDING: ["Trending picks", "Rule-based, with risks"],
  INVESTMENT_PLANS: ["Long-term plans", "Matched to your profile"],
  INVESTMENT_QUOTE: ["Quote", "Simulated market"],
  REAL_STOCK_REQUEST_UNDERSTOOD: ["Stock request", "Allocation and company label preserved"],
  REAL_MARKET_SNAPSHOT_VERIFIED: ["Market snapshot", "Verified provider facts · no model-generated figures"],
  REAL_STOCK_BRIEF: ["Stock brief", "Research only · no order created"],
  ORDER_DRAFTED: ["Order drafted", "Nothing bought or sold yet"],
  ORDER_APPROVAL_RECEIVED: ["Order approval", "Stored order received"],
  SINGPASS_AUTHORISED: ["Singpass authorised", "Signed approval for this order"],
  SINGPASS_AUTHORISATION_REQUIRED: ["Singpass approval needed", "Nothing moved"],
  SINGPASS_REJECTED: ["Rejected in Singpass", "Nothing bought or sold"],
  SINGPASS_CANCELLED: ["Singpass cancelled", "Nothing bought or sold"],
  SINGPASS_EXPIRED: ["Singpass request expired", "Start the order again"],
  SINGPASS_IDENTITY_REQUIRED: ["Singpass identity needed", "Verify through Myinfo first"],
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
  VOICE_CONFIRMATION_REQUIRED: ["Confirmation phrase needed", "Say “yes, confirm” and check the mock affirmation"],
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
  MULTIPLE_REQUESTS_FOUND: ["Several requests found", "Each part is checked and approved on its own"],
  MULTIPLE_REQUESTS: ["Requests drafted separately", "No part can approve another"],
  RULE_PARSER_MATCHED: ["Plain transfer understood", "Fixed rules · the AI was not needed"],
  PAYEE_ADDITION_UNDERSTOOD: ["Add PayNow payee", "Fixed rules · the AI took no part"],
  PAYEE_ADDITION_DRAFTED: ["Payee draft stored", "Saving needs your face and device"],
  PAYEE_NICKNAME_REQUIRED: ["Name needed", "Choose what to call this payee"],
  VOICE_MATCH_CONSUMED: ["Voice confirmed", "Matched your voiceprint and the spoken phrase"],
  VOICE_REGISTRATION_APPROVAL_RECEIVED: ["Voice registration", "Approved with face and device"],
  VOICEPRINT_REGISTERED: ["Voiceprint registered", "Voice can confirm payments under S$200"],
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

function evidenceReceiptFrom(result = {}) {
  const candidates = [
    result.receipt,
    ...(Array.isArray(result.receipt_choices) ? result.receipt_choices : []),
    result.batch_receipt,
    result.order,
    result.paper_order,
    result.paper_order_result,
  ].filter(Boolean);
  const source = candidates.find(candidate => candidate.receipt_id || candidate.order_id || candidate.digest || candidate.order_digest) || {};
  const receiptId = source.receipt_id || source.order_id || result.receipt_id || result.order_id
    || result.transaction_id || result.batch_transaction_id || null;
  const digest = source.receipt_digest || source.digest || source.order_digest
    || result.receipt_digest || result.order_digest || null;
  return {receiptId, digest};
}

function evidenceValidationFrom(result = {}) {
  return result.validation
    || result.receipt_choices?.find(choice => choice.validation)?.validation
    || result.batch_receipt?.validation
    || result.order?.validation
    || result.paper_order?.validation
    || null;
}

function evidenceLifecycleStatus(result, receiptId) {
  const outcome = String(result.outcome || "");
  const reason = String(result.reason_code || "");
  if (reason === "IDEMPOTENT_REPLAY" || reason.includes("ALREADY_EXECUTED")) return "Already settled";
  if (outcome.startsWith("EXECUTED_") || result.paper_order_result?.status === "FILLED") return "Settled";
  if (outcome === "BLOCKED" || outcome === "FAILED") return "Blocked";
  if (reason.includes("APPROVAL_RECEIVED") || reason.includes("AUTHORISED") || reason.includes("APPROVED")) return "Approved";
  if (receiptId || outcome.includes("CONFIRMATION_REQUEST") || reason === "SIGNED_APPROVAL_REQUIRED") return "Proposed";
  return "Blocked";
}

function renderEvidenceLifecycle(result = {}) {
  const {receiptId, digest} = evidenceReceiptFrom(result);
  const validation = evidenceValidationFrom(result);
  const status = evidenceLifecycleStatus(result, receiptId);
  if (ui.evidenceValidation) {
    ui.evidenceValidation.innerHTML = validation
      ? `<strong>${escapeHtml(validation.verdict || "Recorded")}</strong><span>${escapeHtml(validation.note || "Independent validation was recorded for this action.")}</span>`
      : `<strong>${status === "Blocked" ? "No validation record" : "Validation pending"}</strong><span>${!receiptId && !(result.trace || []).length ? "No receipt or trace is available; this blocked result is still recorded safely." : "No independent validation detail was returned for this result."}</span>`;
  }
  if (ui.evidenceValidationStatus) ui.evidenceValidationStatus.textContent = validation?.verdict || (status === "Blocked" ? "Not available" : "Pending");
  if (ui.evidenceReceipt) {
    ui.evidenceReceipt.dataset.evidenceReceipt = receiptId || "";
    ui.evidenceReceiptStatus.textContent = receiptId ? status : "Not created";
  }
  if (ui.evidenceReceiptDetails) {
    ui.evidenceReceiptDetails.innerHTML = receiptId
      ? `<div><dt>Receipt</dt><dd>${escapeHtml(receiptId)}</dd></div><div><dt>Digest</dt><dd>${escapeHtml(digest ? `${String(digest).slice(0, 18)}…` : "Not returned")}</dd></div><div><dt>Approval state</dt><dd>${escapeHtml(status)}</dd></div>`
      : `<div><dt>Receipt</dt><dd>No stored receipt</dd></div><div><dt>Digest</dt><dd>—</dd></div><div><dt>Approval state</dt><dd>${escapeHtml(status)}</dd></div>`;
  }
  if (ui.exactlyOnceState && ui.exactlyOnceStatus) {
    const reason = String(result.reason_code || "");
    const outcome = String(result.outcome || "");
    const alreadySettled = reason === "IDEMPOTENT_REPLAY" || reason.includes("ALREADY_EXECUTED");
    const settled = outcome.startsWith("EXECUTED_") || result.paper_order_result?.status === "FILLED";
    const blocked = status === "Blocked";
    ui.exactlyOnceState.textContent = alreadySettled ? "Already settled" : settled ? "Settled" : blocked ? "Blocked" : "Proposed";
    ui.exactlyOnceStatus.textContent = alreadySettled
      ? "Already settled. This response returns the stored result; no second execution occurred."
      : settled
        ? "Settled exactly once. Retrying the same request returns the stored result and cannot create a second execution."
        : blocked
          ? "Blocked before settlement. No ledger mutation occurred and a retry cannot create a second execution."
          : "Proposed. No settlement has occurred yet; Awaiting approval is still required.";
  }
}

function evidenceContextFromResult(result = {}) {
  const {receiptId} = evidenceReceiptFrom(result);
  const validation = evidenceValidationFrom(result);
  if (!receiptId && !validation && !(result.trace || []).length) {
    return {receiptId, outcome: String(result.outcome || result.reason_code || "BLOCKED"), focusTarget: "heading"};
  }
  return {
    receiptId,
    outcome: String(result.outcome || result.reason_code || "BLOCKED"),
    focusTarget: receiptId ? "" : validation ? "validation" : "trace",
  };
}

function renderOutcomeEvidenceAction() {
  if (!ui.outcomeEvidenceAction) return;
  if (!activeEvidenceContext) {
    ui.outcomeEvidenceAction.innerHTML = "";
    return;
  }
  const receiptAttribute = activeEvidenceContext.receiptId
    ? ` data-evidence-receipt-id="${escapeHtml(activeEvidenceContext.receiptId)}"`
    : "";
  ui.outcomeEvidenceAction.innerHTML = `<button type="button" class="evidence-link" data-view-execution-evidence${receiptAttribute} data-evidence-outcome="${escapeHtml(activeEvidenceContext.outcome)}" data-evidence-focus="${escapeHtml(activeEvidenceContext.focusTarget)}">View execution evidence</button>`;
  ui.outcomeEvidenceAction.querySelector("[data-view-execution-evidence]")?.addEventListener("click", () => {
    openEvidence(document, activeEvidenceContext);
  });
}

function setOutcome(kind, index, kicker, title, detail) {
  ui.outcomeTitle.classList.remove("voice-phrase");
  ui.outcomePanel.hidden = false;
  ui.outcomePanel.className = `outcome-panel ${kind}`;
  ui.outcomePanel.dataset.category = ui.assistantCategory?.textContent?.toLowerCase() || "response";
  ui.outcomeIndex.textContent = "";
  ui.outcomeKicker.textContent = kicker;
  ui.outcomeTitle.textContent = title;
  ui.outcomeDetail.textContent = detail;
  renderOutcomeEvidenceAction();
  ui.candidateList.hidden = true;
  ui.candidateList.innerHTML = "";
  if (ui.assistantTranscript) {
    ui.assistantTranscript.append(ui.outcomePanel);
    scrollConversationToLatest(ui.outcomePanel);
  }
  endAssistantTurn();
}

function resetCommandMode({clearInput = true} = {}) {
  activeClarification = null;
  pendingClarificationResponse = null;
  pendingActionRequest = null;
  setActiveConversation(null);
  pendingConversationTurn = null;
  activeEvidenceContext = null;
  ui.commandLabel.textContent = "Type a request for Dimon";
  ui.input.maxLength = 500;
  ui.input.readOnly = false;
  ui.input.placeholder = "What would you like to do?";
  autoGrowComposer();
  ui.runButton.setAttribute("aria-label", "Send request to Dimon");
  ui.commandHint.textContent = "Use a full saved name. Partial or misspelled names never auto-execute.";
  ui.newRequestButton.hidden = true;
  if (clearInput) ui.input.value = "";
  autoGrowComposer();
}

async function retryPendingActionRequest() {
  if (unknownActionRetryInFlight || !pendingActionRequest) return;
  unknownActionRetryInFlight = true;
  const controls = [...ui.candidateList.querySelectorAll("[data-unknown-recovery-action]")];
  controls.forEach(control => { control.disabled = true; });
  const started = performance.now();
  setBusy(true);
  beginAssistantTurn();
  try {
    const actionResponse = await requestAction(pendingActionRequest);
    if (actionResponse.kind === "UNKNOWN") {
      renderUnknownActionRecovery(actionResponse, started);
      return;
    }
    pendingActionRequest = null;
    ui.input.readOnly = false;
    renderResult(actionResponse.result, Math.round(performance.now() - started));
  } catch (_error) {
    renderUnknownActionRecovery({message: "Connection interrupted"}, started);
  } finally {
    unknownActionRetryInFlight = false;
    setBusy(false);
  }
}

function renderUnknownActionRecovery(actionResponse, started) {
  endAssistantTurn("The reply is delayed. Retrying will send the same message safely.");
  ui.input.readOnly = true;
  ui.newRequestButton.hidden = true;
  ui.outcomePanel.hidden = false;
  ui.outcomePanel.className = "outcome-panel clarification";
  ui.outcomeIndex.textContent = "?";
  ui.outcomeKicker.textContent = "Outcome unknown · safe retry available";
  ui.outcomeTitle.textContent = "We couldn’t confirm the result.";
  ui.outcomeDetail.textContent = "Your balances may already reflect this request. Retry the same request or check settlement evidence before starting over.";
  setSettlement("idle", "Unknown", [["Connection", actionResponse.message || "Reply unavailable"]],
    "The reply was lost. Retrying sends the identical request, so nothing can happen twice.");
  ui.elapsedLabel.textContent = `${Math.round(performance.now() - started)} ms · response unknown`;
  ui.candidateList.hidden = false;
  ui.candidateList.innerHTML = `<div class="unknown-recovery-actions" role="group" aria-label="Recover interrupted request">
    <button type="button" class="candidate-choice unknown-recovery-primary" data-unknown-recovery-action data-retry-same-request>Retry same request</button>
    <button type="button" class="candidate-choice" data-unknown-recovery-action data-check-settlement-evidence>Check settlement evidence</button>
    <button type="button" class="text-button unknown-recovery-discard" data-unknown-recovery-action data-discard-unknown-request>Discard and start over</button>
  </div>`;
  ui.candidateList.querySelector("[data-retry-same-request]")?.addEventListener("click", () => {
    void retryPendingActionRequest();
  });
  ui.candidateList.querySelector("[data-check-settlement-evidence]")?.addEventListener("click", () => {
    activeEvidenceContext = {
      outcome: "UNKNOWN",
      focusTarget: "heading",
      requestId: pendingActionRequest?.requestId || null,
    };
    openEvidence(document, activeEvidenceContext);
  });
  ui.candidateList.querySelector("[data-discard-unknown-request]")?.addEventListener("click", () => {
    if (!window.confirm("Discard this request and start over? The previous outcome is still unknown, so check your evidence before sending a similar request.")) return;
    resetCommandMode();
    ui.outcomePanel.hidden = true;
    ui.candidateList.hidden = true;
    ui.suggestionList.hidden = false;
    setAssistantMode("idle", "Ready for a new request");
    ui.voiceButton.focus();
  });
}

function editCurrentRequest() {
  resetCommandMode({clearInput: false});
  ui.outcomePanel.hidden = true;
  ui.input.value = lastCommandText;
  autoGrowComposer();
  showTypedFallback();
  ui.input.setSelectionRange(ui.input.value.length, ui.input.value.length);
  if (ui.conversationStatus) ui.conversationStatus.textContent = "Edit your message, then send it again.";
}

const conversationChoiceLabels = {
  BUY_NOW: "Buy now",
  SELL_ALL: "Sell all",
  SET_LIMIT: "Set a maximum price",
  USE_AVAILABLE_FUNDS: "Use available balance",
  CHANGE_FUNDING_ACCOUNT: "Choose another account",
  DAY: "DAY order",
  GTC: "Good till cancelled",
  CANCEL: "Cancel",
};

function choiceLabel(choice, result) {
  const selling = (result?.public_view || result?.conversation?.public_view)?.side === "SELL";
  if (selling && choice === "BUY_NOW") return "Sell now";
  if (selling && choice === "SET_LIMIT") return "Set a minimum price";
  return conversationChoiceLabels[choice] || choice;
}

// One turn of a stock conversation for hands-free voice: the same request
// as a tapped choice, returned instead of rendered. Retries reuse the
// request id, so a turn is never applied twice.
async function conversationTurn(input) {
  const conversation = activeConversation;
  if (!conversation || conversation.status !== "WAITING") return null;
  const body = JSON.stringify({request_id: crypto.randomUUID(), expected_version: conversation.version, input});
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const response = await fetch(`/api/conversations/${encodeURIComponent(conversation.conversation_id)}/turns`, {
        method: "POST", headers: {"Content-Type": "application/json"}, body,
      });
      if (response.ok) return await response.json();
      if (![502, 503, 504].includes(response.status)) {
        const detail = await response.json().catch(() => ({}));
        setActiveConversation(null);
        return {
          outcome: "BLOCKED",
          reason_code: detail.detail || `HTTP_${response.status}`,
          answer: "That request is no longer current. Please ask again.",
        };
      }
    } catch (_error) { /* retried below */ }
    await new Promise(resolve => window.setTimeout(resolve, 400 * (attempt + 1)));
  }
  return null;
}

function syncConversation(result) {
  if (!result?.conversation) return null;
  return setActiveConversation({
    ...result.conversation,
    question: result.question ?? result.conversation.question,
    allowed_choices: result.allowed_choices || result.conversation.allowed_choices || [],
  });
}

function renderConversationControls(result) {
  const conversation = syncConversation(result);
  if (!conversation) return;
  const choices = conversation.allowed_choices || [];
  const question = result.question || conversation.question;
  ui.newRequestButton.hidden = false;
  if (!question && !choices.length) return;
  ui.candidateList.hidden = false;
  const controls = `<article class="invest-card conversation-card">
    ${question ? `<p class="invest-why">${escapeHtml(question)}</p>` : ""}
    ${choices.length ? `<div class="invest-card-actions">${choices.map(choice => `<button type="button" class="candidate-choice" data-conversation-choice="${escapeHtml(choice)}">${escapeHtml(choiceLabel(choice, result))}</button>`).join("")}</div>` : ""}
  </article>`;
  ui.candidateList.insertAdjacentHTML("beforeend", controls);
  ui.candidateList.querySelectorAll("[data-conversation-choice]").forEach(button => {
    button.addEventListener("click", () => submitConversationTurn({
      kind: "CHOICE",
      choice: button.dataset.conversationChoice,
    }));
  });
}

async function submitConversationTurn(input, retryTurn = null) {
  const conversation = retryTurn
    ? {conversation_id: retryTurn.conversationId}
    : activeConversation;
  if (!conversation || (!retryTurn && conversation.status !== "WAITING")) return;
  const turn = retryTurn || {
    conversationId: conversation.conversation_id,
    expectedVersion: conversation.version,
    requestId: crypto.randomUUID(),
    input,
  };
  pendingConversationTurn = turn;
  ui.candidateList.querySelectorAll(".candidate-choice").forEach(button => {
    button.disabled = true;
  });
  const started = performance.now();
  setBusy(true);
  ui.outcomePanel.hidden = true;
  beginAssistantTurn();
  ui.elapsedLabel.textContent = "Follow-up in flight";
  try {
    let response = null;
    let networkError = null;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        response = await fetch(`/api/conversations/${encodeURIComponent(turn.conversationId)}/turns`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({
            request_id: turn.requestId,
            expected_version: turn.expectedVersion,
            input: turn.input,
          }),
        });
        networkError = null;
        const retryableHttpFailure = response.status === 502
          || response.status === 503 || response.status === 504;
        const conflict = response.status === 409
          ? await response.clone().json().catch(() => ({}))
          : null;
        const retryableConflict = conflict?.detail === "CONVERSATION_VERSION_CONFLICT";
        if (attempt < 2 && (retryableHttpFailure || retryableConflict)) {
          await new Promise(resolve => window.setTimeout(resolve, 400 * (attempt + 1)));
          continue;
        }
        break;
      } catch (error) {
        networkError = error;
        if (attempt < 2) {
          await new Promise(resolve => window.setTimeout(resolve, 400 * (attempt + 1)));
        }
      }
    }
    if (networkError) throw networkError;
    if (!response) throw new Error("No response was received");
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      pendingConversationTurn = null;
      setActiveConversation(null);
      endAssistantTurn("The investment request needs attention.");
      recordEvidenceDiagnostic("conversation_follow_up", {name: "HttpError", status: response.status, reasonCode: detail.detail});
      setOutcome("blocked", "!", "Follow-up stopped", "This request is no longer current.", "Start a new request to continue.");
      ui.newRequestButton.hidden = false;
      return;
    }
    const result = await response.json();
    pendingConversationTurn = null;
    renderResult(result, Math.round(performance.now() - started));
  } catch (error) {
    endAssistantTurn("The investment request could not be updated.");
    setOutcome(
      "failed",
      "×",
      "Connection interrupted",
      "I couldn’t retrieve the result for that choice.",
      "The server may already have processed it. Retry the same choice to recover the result safely.",
    );
    ui.newRequestButton.hidden = true;
    ui.candidateList.hidden = false;
    ui.candidateList.innerHTML = '<button type="button" class="candidate-choice" data-retry-conversation-turn>Retry the same choice</button>';
    ui.candidateList.querySelector("[data-retry-conversation-turn]")?.addEventListener("click", () => {
      if (pendingConversationTurn) {
        void submitConversationTurn(pendingConversationTurn.input, pendingConversationTurn);
      }
    }, {once: true});
  } finally {
    setBusy(false);
    ui.voiceButton.focus();
  }
}

function enterClarificationMode(result) {
  const clarification = result.clarification;
  activeClarification = clarification;
  pendingClarificationResponse = null;
  if (clarification.kind === "MULTI_ACTION") {
    ui.commandLabel.textContent = "Clarify request";
    ui.input.value = "";
    ui.input.maxLength = 160;
    ui.input.readOnly = false;
    ui.input.placeholder = "John Tan, and yes, NVIDIA";
    ui.runButton.setAttribute("aria-label", "Clarify grouped request");
    ui.commandHint.textContent = "Answer only the missing details. Nothing is prepared until all requests validate.";
    ui.newRequestButton.hidden = false;
    ui.voiceButton.focus();
    return;
  }
  ui.commandLabel.textContent = "How much?";
  ui.input.value = "";
  ui.input.maxLength = 100;
  ui.input.readOnly = false;
  ui.input.placeholder = "ten dollars";
  ui.runButton.setAttribute("aria-label", "Continue exact request");
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
      <span><small>From</small><strong>${escapeHtml(formatFundingSource(facts))}</strong></span>
      ${facts.currency ? `<span><small>Locked amount currency</small><strong>${escapeHtml(String(facts.currency))}</strong></span>` : ""}
      <span><small>To</small><strong>${escapeHtml((facts.recipient_display_names || []).join(", ") || "—")}</strong></span>
      ${facts.destination_reference ? `<span><small>PayNow</small><strong>${escapeHtml(facts.destination_reference)}</strong></span>` : ""}
      <span><small>Expires</small><strong>${escapeHtml(expiryLabel)}</strong></span>
    </div>
  </div>`;
  ui.voiceButton.focus();
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
  const authenticationMethod = result.approval?.authentication_method;
  const face = result.approval?.face_similarity != null
    ? `Your face (${Math.round(result.approval.face_similarity * 100)}% match) + `
    : "";
  const singpass = result.approval?.singpass_auth_id ? "Singpass + " : "";
  if (singpass) return `${singpass}${face}${kind === "WEBAUTHN_PLATFORM" ? "Windows Hello" : "this device's key"}`;
  if (authenticationMethod === "VOICEPRINT_AND_DEVICE_SIGNATURE") {
    const similarity = result.approval?.voice_similarity;
    return `Your voice${similarity != null ? ` (${Math.round(similarity * 100)}% match)` : ""} + ${kind === "WEBAUTHN_PLATFORM" ? "Windows Hello" : "this device's key"}`;
  }
  if (authenticationMethod === "MOCK_VOICE_AFFIRMATION_AND_DEVICE_SIGNATURE") {
    return `“Yes, confirm” mock affirmation + ${kind === "WEBAUTHN_PLATFORM" ? "Windows Hello" : "this device's key"}`;
  }
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
  if (result.outcome === "EXECUTED_SYNTHETIC_BILL_PAYMENT") {
    const action = result.resolved_action || {};
    setSettlement("committed", "Bill paid", [
      ["Biller", action.biller_display_name || action.recipient_display_name],
      ["Paid", formatSgd(action.amount_minor)],
      ["From", accountLabel(result, action.source_account_id)],
      ["Savings balance", `${formatSgd(action.balance_before_minor)} → ${formatSgd(action.balance_after_minor)}`],
      ["Approved with", approvalLabel(result)],
      ["Reference", reference(result.transaction_id)],
    ], "Recorded in the synthetic demo ledger. No external biller or bank was contacted.");
    return;
  }
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
        recordEvidenceDiagnostic("device_enrollment", error);
        const message = faceMessages[error.reasonCode] || "Verification could not finish.";
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

// The device key signs every transfer. High-value transfers and investment
// transactions also require a registered-face match on this receipt.
function describeSigner(kind, review) {
  if (!review.requires_face) {
    return {
      label: `Mock “yes, confirm” affirmation + ${kind === "WEBAUTHN_PLATFORM" ? "Windows Hello / Touch ID" : "this device's key"}`,
      action: "Confirm “yes, confirm”",
    };
  }
  return kind === "WEBAUTHN_PLATFORM"
    ? {label: "Your face + Windows Hello / Touch ID", action: "Confirm with your face"}
    : {label: "Your face + this device's key", action: "Confirm with your face"};
}

// The deterministic confirmation overlay. Every fact shown comes from the
// server-stored receipt returned with the challenge, never from model text.
// Plays the server-generated read-back from the primary Kokoro pipeline (or
// its outage backup). If audio is unavailable, the safe text remains visible.
// Returns a function that stops playback.
let readBackAudio = null;

// Any of Dimon's own audio playing: the hands-free microphone stays muted.
function dimonAudible() {
  const playing = audio => Boolean(audio && !audio.paused && !audio.ended);
  return playing(ui.resultAudio) || playing(readBackAudio) || Boolean(wakeWord?.isSpeaking?.());
}

function speakReadBack(readBack, onDone) {
  let stopped = false;
  let audio = null;
  const finish = () => { if (!stopped) onDone(); };
  (async () => {
    if (!readBack?.audio_artifact_id) return;
    const url = `/api/audio/${encodeURIComponent(readBack.audio_artifact_id)}`;
    for (let attempt = 0; attempt < 60 && !stopped; attempt += 1) {
      try {
        const response = await fetch(url, {cache: "no-store"});
        if (response.status === 202) { await new Promise(r => setTimeout(r, 250)); continue; }
        if (!response.ok) break;
        audio = new Audio(URL.createObjectURL(await response.blob()));
        readBackAudio = audio;
        audio.onended = finish;
        audio.onerror = () => {};
        await autoplayResultAudio(audio, {onEnded: finish});
        return;
      } catch (_error) {
        break;
      }
    }
  })();
  return () => {
    stopped = true;
    if (audio) audio.pause();
  };
}

function resetApprovalDialog({totalLabel = "Total debit"} = {}) {
  const debitLabel = ui.approvalTotal.closest("div")?.querySelector("dt");
  if (debitLabel) debitLabel.textContent = totalLabel;
  ui.approvalReadBack.hidden = true;
  ui.approvalReadBackText.textContent = "";
  ui.approvalReadBackPlay.onclick = null;
  ui.approvalOob.hidden = true;
  ui.approvalOobCode.value = "";
  ui.approvalOobCode.oninput = null;
  ui.approvalUnusual.hidden = true;
  ui.approvalUnusualCheck.checked = false;
  ui.approvalUnusualCheck.onchange = null;
  ui.approvalUnusualText.textContent = "";
  ui.approvalVoice.hidden = true;
  ui.approvalVoiceCheck.checked = false;
  ui.approvalVoiceCheck.onchange = null;
  const facePanel = document.querySelector("#approval-face");
  if (facePanel) facePanel.hidden = true;
}

function confirmOnDevice(challenge, signerKind, options = {}) {
  resetApprovalDialog();
  const review = challenge.review;
  const needsFace = Boolean(review.requires_face);
  const needsVoiceConfirmation = Boolean(review.requires_voice_confirmation);
  const signer = describeSigner(signerKind, review);
  ui.approvalItems.innerHTML = review.items.map(item => `<div class="approval-item">
      <span><small>${item.destination_kind === "UTILITY_BILL" ? "Biller" : "To"}</small><strong>${escapeHtml(item.recipient_display_name)}</strong>${item.masked_destination && item.destination_kind !== "UTILITY_BILL" ? `<em>${escapeHtml(item.masked_destination)}</em>` : ""}${item.payment_reference ? `<em>Reference ${escapeHtml(item.payment_reference)}</em>` : ""}${item.new_payee ? '<b class="payee-flag">New payee</b>' : ""}</span>
      <span><small>Amount</small><strong>${review.kind === "PAYEE" || review.kind === "VOICE" ? "No money moves" : escapeHtml(formatTransactionalMoney(item.currency || review.currency || "SGD", item.amount_minor))}</strong>${item.unusual_amount ? '<b class="payee-flag">Unusual amount</b>' : ""}</span>
    </div>`).join("");
  ui.approvalSource.textContent = formatFundingSource(review);
  ui.approvalTotal.textContent = formatTransactionalMoney(review.currency || "SGD", review.total_minor);
  ui.approvalDigest.textContent = `${review.receipt_digest.slice(0, 16)}…`;
  ui.approvalMethod.textContent = `${review.requires_singpass ? "Singpass + " : ""}${signer.label}`;
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
  const facePanel = document.querySelector("#approval-face");
  facePanel.hidden = !needsFace;
  facePrompt.className = "face-prompt";
  facePrompt.textContent = needsFace ? "Starting the camera…" : "";
  let closeCamera = () => {};
  let scanning = false;
  const cameraReady = needsFace
    ? openCamera(faceVideo).then(close => {
        closeCamera = close;
        facePrompt.textContent = "When you are ready, press Confirm and follow the prompts.";
        return true;
      }).catch(error => {
        recordEvidenceDiagnostic("approval_camera", error);
        facePrompt.className = "face-prompt bad";
        facePrompt.textContent = faceMessages[error.reasonCode] || "The camera could not start. Check its permission and try again.";
        return false;
      })
    : Promise.resolve(true);
  ui.approvalVoice.hidden = !needsVoiceConfirmation;
  ui.approvalVoiceCheck.checked = false;
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
    ? `Yes, ${formatTransactionalMoney(unusualItem.currency || review.currency || "SGD", unusualItem.amount_minor)} to ${unusualItem.recipient_display_name} is correct. You usually send ${formatTransactionalMoney(unusualItem.currency || review.currency || "SGD", unusualItem.unusual_amount.typical_min_minor)}–${formatTransactionalMoney(unusualItem.currency || review.currency || "SGD", unusualItem.unusual_amount.typical_max_minor)}.`
    : "";
  const codeReady = () => !needsCode || /^\d{6}$/.test(ui.approvalOobCode.value);
  const allReady = () => codeReady()
    && (!needsReadBack || readBackPlayed)
    && (!needsVoiceConfirmation || ui.approvalVoiceCheck.checked)
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
  ui.approvalVoiceCheck.onchange = syncConfirm;
  syncConfirm();
  if (readBack && (needsReadBack || options.autoPlayReadBack || lastRequestWasSpoken)) playReadBack();
  return new Promise(resolve => {
    const cleanup = confirmed => {
      const result = confirmed
        ? {
            confirmed: true,
            outOfBandCode: needsCode ? ui.approvalOobCode.value : undefined,
            readBackPlayed,
            unusualConfirmed: needsUnusual && ui.approvalUnusualCheck.checked,
            voiceConfirmation: needsVoiceConfirmation ? "yes, confirm" : undefined,
          }
        : {confirmed: false};
      stopReadBack();
      closeCamera();
      cameraReady.then(() => closeCamera());
      ui.approvalReadBackPlay.onclick = null;
      ui.approvalUnusualCheck.onchange = null;
      ui.approvalVoiceCheck.onchange = null;
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
      if (!needsFace) {
        cleanup(true);
        return;
      }
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
        recordEvidenceDiagnostic("approval_face_match", error);
        facePrompt.className = "face-prompt bad";
        facePrompt.textContent = faceMessages[error.reasonCode] || "Face verification could not finish. Try the scan again.";
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
    (needsCode ? ui.approvalOobCode : needsVoiceConfirmation ? ui.approvalVoiceCheck : ui.approvalCancel).focus();
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
      await Promise.all([loadState(), loadPayees()]);
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

async function signedApproval(collection, receipt, submit, interruptedDetail, options = {}) {
  const started = performance.now();
  setChoiceBusy(true);
  try {
    let signed = signedAssertions.get(receipt.receipt_id);
    if (!signed) {
      ui.elapsedLabel.textContent = "Requesting a one-time signing challenge";
      await ensureSigningCredential();
      let singpassApproved = false;
      let singpassRefusal = null;
      const isTransferReceipt = collection === "receipts" || collection === "batch-receipts";
      const amountMinor = Number(receipt.total_minor ?? receipt.amount_minor ?? 0);
      const requestTransferSingpass = async () => {
        ui.elapsedLabel.textContent = "Requesting approval in Singpass";
        const path = collection === "batch-receipts" ? "batch-receipts" : "receipts";
        const singpassResult = await authoriseWithSingpass(
          {order_id: receipt.receipt_id, order_digest: receipt.receipt_digest},
          investHelpers,
          {
            requestUrl: `/api/${path}/${encodeURIComponent(receipt.receipt_id)}/singpass`,
            requestBody: {receipt_digest: receipt.receipt_digest},
          },
        );
        if (!singpassResult.approved) {
          renderApprovalRefusal(singpassResult.reason, "Singpass did not authorise this transfer. Nothing moved.");
          ui.elapsedLabel.textContent = "Singpass approval not completed";
          singpassRefusal = singpassResult.reason;
          return false;
        }
        singpassApproved = true;
        return true;
      };
      // Request Singpass before issuing the short lived device challenge. The
      // stored receipt fixes this amount, and the server independently checks
      // the threshold before creating the Singpass request.
      const singpassBlocked = () => ({outcome: "BLOCKED", reason_code: singpassRefusal || "SINGPASS_NOT_APPROVED"});
      if (isTransferReceipt && amountMinor >= 20000) {
        const approved = await requestTransferSingpass();
        if (!approved) return singpassBlocked();
      }
      const issued = await deviceApprover.requestChallenge(collection, receipt);
      if (issued.kind === "BLOCKED") {
        renderResult(issued.result, Math.round(performance.now() - started));
        return issued.result;
      }
      if (isTransferReceipt && issued.challenge.review?.requires_singpass && !singpassApproved) {
        const approved = await requestTransferSingpass();
        if (!approved) return singpassBlocked();
      }
      const credential = await deviceApprover.usableCredential(issued.challenge.allow_credentials);
      const decision = await confirmOnDevice(issued.challenge, credential?.kind, options);
      if (!decision.confirmed) {
        renderApprovalRefusal("APPROVAL_CANCELLED", "You closed the confirmation. Nothing was signed and balances are unchanged.");
        ui.elapsedLabel.textContent = "Approval cancelled";
        return {outcome: "BLOCKED", reason_code: "APPROVAL_CANCELLED"};
      }
      ui.elapsedLabel.textContent = "Waiting for the device signature";
      signed = {
        assertion: wireAssertion(await deviceApprover.signChallenge(issued.challenge)),
        out_of_band_code: decision.outOfBandCode,
        read_back_played: Boolean(decision.readBackPlayed),
        unusual_amount_confirmed: Boolean(decision.unusualConfirmed),
        voice_confirmation: decision.voiceConfirmation,
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
      return null;
    }
    signedAssertions.delete(receipt.receipt_id);
    renderResult(approval.result, Math.round(performance.now() - started));
    return approval.result;
  } catch (error) {
    console.error("signed approval failed", error);
    signedAssertions.delete(receipt.receipt_id);
    const reason = error?.reasonCode || (error?.name === "NotAllowedError" ? "BIOMETRIC_DECLINED" : "SIGNING_FAILED");
    renderApprovalRefusal(reason, "The device did not produce a valid signature. Nothing was executed.");
    ui.elapsedLabel.textContent = `${Math.round(performance.now() - started)} ms · not signed`;
    return {outcome: "BLOCKED", reason_code: reason};
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
  // Stock and equity transactions: Singpass first, then face and device.
  try {
    await ensureSigningCredential();
  } catch (error) {
    renderApprovalRefusal(error.reasonCode || "KYC_NOT_COMPLETED", IDENTITY_VERIFICATION_FAILURE_MESSAGE);
    return {outcome: "BLOCKED", reason_code: error.reasonCode || "KYC_NOT_COMPLETED"};
  }
  const singpassResult = await authoriseWithSingpass(order, investHelpers);
  if (!singpassResult.approved) {
    renderApprovalRefusal(singpassResult.reason, "Singpass did not authorise this order. Nothing was bought or sold.");
    return {outcome: "BLOCKED", reason_code: singpassResult.reason};
  }
  const result = await signedApproval(
    "orders",
    order,
    requestOrderApproval,
    "Retry this exact order to retrieve its stored result; it can fill at most once.",
  );
  await loadInvestments();
  return result;
}

function approveBatchReceipt(receipt) {
  return signedApproval(
    "batch-receipts",
    receipt,
    requestBatchReceiptApproval,
    "Retry this exact batch receipt to retrieve its stored result.",
  );
}

async function paperSingpass(order) {
  return authoriseWithSingpass(order, investHelpers, {
    requestUrl: `/api/paper-equity/orders/${encodeURIComponent(order.order_id)}/singpass`,
    requestBody: {receipt_digest: order.digest},
    pollIntervalMs: 1000,
  });
}

// Paper-equity orders use a dedicated visible confirmation step. The camera
// must not run against a hidden video element: users need to see themselves,
// the stored order facts, and the head-turn prompt before the face scan.
function confirmPaperFace(order, challenge) {
  const dialog = ui.approvalDialog;
  const faceVideo = document.querySelector("#approval-face-video");
  const facePrompt = document.querySelector("#approval-face-prompt");
  const instrument = order.instrument || {};
  const quote = order.quote_snapshot || {};
  const quoteCurrency = instrument.quote_currency || "USD";
  const fundingCurrency = order.funding_currency || "SGD";
  const digest = order.digest || order.order_digest || "";

  const selling = order.side === "SELL";
  resetApprovalDialog({totalLabel: selling ? "Estimated proceeds" : "Maximum debit"});
  const shareCount = `${order.quantity || 0} ${Number(order.quantity) === 1 ? "share" : "shares"}`;
  ui.approvalItems.innerHTML = `<div class="approval-item">
      <span><small>${selling ? "Sell" : "Buy"}</small><strong>${escapeHtml(`${shareCount} of ${instrument.name || instrument.symbol || "equity"}`)}</strong><em>${escapeHtml(`${instrument.symbol || ""} · ${instrument.exchange || ""}`)}</em></span>
      <span><small>Quote</small><strong>${escapeHtml(formatPaperMoney(quoteCurrency, quote.price_minor))}</strong><em>${escapeHtml(`${order.order_type || "MARKET"} · ${order.time_in_force || "DAY"}`)}</em></span>
    </div>`;
  ui.approvalSource.textContent = formatFundingSource(order);
  ui.approvalTotal.textContent = formatPaperMoney(fundingCurrency, selling
    ? Math.max(0, Number(order.estimated_funding_minor || 0) - Number(order.fee_funding_minor || 0))
    : order.max_funding_debit_minor);
  ui.approvalDigest.textContent = `${digest.slice(0, 16)}…`;
  ui.approvalMethod.textContent = "Device + live face";
  ui.approvalValidation.className = "approval-validation pass";
  ui.approvalValidation.innerHTML = selling
    ? "<strong>The shares are sold at the live price when the order fills, and the proceeds after the fee are paid into your savings. No live broker order will be sent.</strong>"
    : order.order_type === "MARKET"
    ? "<strong>Market orders fill at the live price. The final debit may differ from this estimate and must fit your available balance. No live broker order will be sent.</strong>"
    : "<strong>Review the stored paper order. No live broker order will be sent.</strong>";
  ui.approvalReadBack.hidden = true;
  ui.approvalOob.hidden = true;
  ui.approvalUnusual.hidden = true;
  ui.approvalVoice.hidden = true;
  ui.approvalVoiceCheck.checked = false;
  ui.approvalConfirm.textContent = "Confirm with face";
  ui.approvalConfirm.disabled = true;
  facePrompt.className = "face-prompt";
  facePrompt.textContent = "Starting the camera…";
  document.querySelector("#approval-face").hidden = false;

  return new Promise(resolve => {
    let closeCamera = () => {};
    let cameraReady = false;
    let scanning = false;
    let finished = false;
    const finish = confirmed => {
      if (finished) return;
      finished = true;
      closeCamera();
      ui.approvalConfirm.onclick = null;
      ui.approvalCancel.onclick = null;
      ui.approvalDialog.oncancel = null;
      ui.approvalConfirm.disabled = false;
      if (dialog.open) dialog.close();
      resolve(confirmed);
    };
    const camera = openCamera(faceVideo).then(close => {
      closeCamera = close;
      if (finished) {
        closeCamera();
        return;
      }
      cameraReady = true;
      facePrompt.textContent = "When ready, confirm and slowly turn your head to one side, then back to centre.";
      ui.approvalConfirm.disabled = false;
    }).catch(error => {
      recordEvidenceDiagnostic("paper_approval_camera", error);
      facePrompt.className = "face-prompt bad";
      facePrompt.textContent = faceMessages[error.reasonCode] || "The camera could not start. Check its permission and try again.";
    });
    ui.approvalConfirm.onclick = async () => {
      if (!cameraReady || scanning || finished) return;
      scanning = true;
      ui.approvalConfirm.disabled = true;
      try {
        const frames = await captureHeadTurn(faceVideo, {
          onStep: (_index, prompt) => { facePrompt.textContent = prompt; },
        });
        facePrompt.textContent = "Checking your face…";
        const match = await postFrames("/api/face/verify", {
          challenge_id: challenge.challenge_id,
          frames,
        });
        facePrompt.className = "face-prompt ok";
        facePrompt.textContent = `Face matched (${Math.round((match.similarity || 0) * 100)}%). Signing…`;
        finish(true);
      } catch (error) {
        recordEvidenceDiagnostic("paper_approval_face_match", error);
        facePrompt.className = "face-prompt bad";
        facePrompt.textContent = faceMessages[error.reasonCode] || "Face verification could not finish. Try the scan again.";
        ui.approvalConfirm.textContent = error.reasonCode === "FACE_LOCKED" ? "Locked" : "Scan again";
        scanning = error.reasonCode === "FACE_LOCKED";
        ui.approvalConfirm.disabled = scanning;
      }
    };
    ui.approvalCancel.onclick = () => finish(false);
    ui.approvalDialog.oncancel = event => {
      event.preventDefault();
      finish(false);
    };
    dialog.showModal();
    void camera;
  });
}

async function approvePaperOrder(order, {announcementOwner = "screen"} = {}) {
  const started = performance.now();
  let payload;
  try {
    await ensureSigningCredential();
    const singpassResult = await paperSingpass(order);
    if (!singpassResult.approved) {
      renderApprovalRefusal(singpassResult.reason, "Singpass did not authorise this order. Nothing moved.");
      return {status: "CANCELLED", reason_code: singpassResult.reason};
    }
    const challengeResponse = await fetch(`/api/paper-equity/orders/${encodeURIComponent(order.order_id)}/challenge`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({receipt_digest: order.digest}),
    });
    const challenge = await challengeResponse.json();
    if (!challengeResponse.ok) throw Object.assign(new Error(challenge.detail || "Challenge unavailable"), {reasonCode: challenge.detail});
    const assertion = wireAssertion(await deviceApprover.signChallenge(challenge));
    const faceConfirmed = await confirmPaperFace(order, challenge);
    if (!faceConfirmed) {
      renderApprovalRefusal("PAPER_APPROVAL_CANCELLED", "Face confirmation was cancelled. Nothing moved.");
      return {status: "CANCELLED", reason_code: "PAPER_APPROVAL_CANCELLED"};
    }
    const approval = await requestPaperEquityApproval(order, {assertion});
    if (approval.kind === "UNKNOWN") throw new Error(approval.message);
    payload = approval.result;
  } catch (error) {
    recordEvidenceDiagnostic("paper_order_approval", error);
    renderApprovalRefusal(error.reasonCode || "PAPER_APPROVAL_FAILED", "The approval could not be completed. Nothing moved.");
    return {status: "REJECTED", reason_code: error.reasonCode || "PAPER_APPROVAL_FAILED"};
  }

  const result = payload.paper_order_result || payload;
  const [state, investmentOverview] = await Promise.all([
    loadState().catch(() => null),
    loadInvestments().catch(() => null),
  ]);
  const renderedResult = {
    ...payload,
    outcome: result.status === "FILLED" ? "PAPER_ORDER_FILLED" : "PAPER_ORDER_RESULT",
    reason_code: result.reason_code,
    paper_order_result: {
      ...result,
      side: order.side || "BUY",
      order_id: order.order_id,
      digest: order.digest,
      instrument: order.instrument,
      funding_account_id: order.funding_account_id,
    },
    paper_equity: investmentOverview?.paper_equity,
    state,
  };
  renderResult(renderedResult, Math.round(performance.now() - started));
  // A screen click completes outside the hands-free conversation coroutine.
  // Hand the server-owned fill artifact to that player explicitly; otherwise
  // hands-free mode suppresses renderResult's normal audio playback and the
  // completed purchase is only visible. Voice-owned approvals announce the
  // same result in approveShares, so they must not enter this branch.
  if (announcementOwner === "screen" && result.status === "FILLED") {
    await wakeWord?.announceResult(renderedResult, "done", "Purchase complete. Your portfolio is updated.");
  }
  return {...result, instrument: order.instrument, audio_artifact_id: payload.audio_artifact_id};
}

function finishVoiceResponse(result, speakAutomatically) {
  pendingOnScreenReview = Boolean(
    result.receipt_choices?.length || result.batch_receipt || result.outcome === "BATCH_CONFIRMATION_REQUEST"
    || result.outcome === "ORDER_CONFIRMATION_REQUEST" || result.order || result.paper_order
    || result.bill_payment || result.outcome === "EXECUTED_SYNTHETIC_TRANSFER",
  );
  // A prepared payment or investment must be reviewed on screen before we
  // reopen the microphone for another intent.
  ui.newRequestButton.hidden = false;
  speakReply(result, {autoListen: speakAutomatically && !pendingOnScreenReview});
}

function renderResult(result, elapsedMs) {
  const spokenOrigin = lastRequestWasSpoken;
  lastRequestWasSpoken = false;
  document.querySelector("#thinking-message")?.remove();
  ui.input.value = "";
  autoGrowComposer();
  const category = classifyActionResult(result);
  activeEvidenceContext = evidenceContextFromResult(result);
  renderEvidenceLifecycle(result);
  multiRequest.observe(result);
  renderPlanBar();
  if (result.outcome === "MULTI_ACTION_CONFIRMATION_REQUEST") {
    if (result.conversation) syncConversation(result);
    const tier = result.payment_authentication?.authentication_tier === "SINGPASS_AND_FACE"
      ? "The combined bill and transfer total requires Singpass and face approval for each payment."
      : "Each payment uses its combined total for the required approval tier.";
    setOutcome("clarification", "⇄", "Multiple actions · review each", result.answer || "I’ve prepared your requested actions.", `${tier} Nothing moves until you approve each payment; the stock order keeps its own approval.`);
    activeMultiActionResponse = result;
    renderMultiActionCards(result);
    if (result.state) renderState(result.state);
    renderTrace(result.trace || [], result.outcome);
    clearResultAudio(ui.resultAudio);
    if (ui.replyAudioPlayer) ui.replyAudioPlayer.hidden = true;
    syncReplyAudioPlayer();
    ui.elapsedLabel.textContent = `${elapsedMs} ms end to end`;
    finishVoiceResponse(result, spokenOrigin);
    return;
  }
  if (result.conversation) {
    const conversation = syncConversation(result);
    if (conversation?.status !== "WAITING") setActiveConversation(null);
  } else if (String(result.reason_code || "").startsWith("CONVERSATION_")) {
    setActiveConversation(null);
  }
  const categoryLabels = {payments: "Payments", investing: "Investing", account: "Accounts", request: "Requests", unknown: "Needs review"};
  if (ui.assistantCategory) ui.assistantCategory.textContent = categoryLabels[category];
  ui.outcomePanel.dataset.category = category;
  const paperFill = result.paper_order_result?.status === "FILLED" ? result.paper_order_result : null;
  const fillTrade = paperFill?.trade;
  const demoDisclosure = demoExchangeDisclosure(paperFill?.execution_evidence);
  const priceMoved = result.paper_order_result?.reason_code === "PRICE_MOVED_BEYOND_LIMIT"
    || result.paper_order_result?.reason_code === "FUNDING_MOVED_BEYOND_LIMIT";
  const customerResponse = priceMoved
    ? "The price moved above the limit you approved, so I didn’t place the order. Nothing was bought and your balance is unchanged. Would you like a fresh quote?"
    : fillTrade?.side === "SELL"
    ? `Sold ${fillTrade.quantity} ${paperFill.instrument?.name || fillTrade.symbol} ${Number(fillTrade.quantity) === 1 ? "share" : "shares"} at ${formatPaperMoney(fillTrade.quote_currency || "USD", fillTrade.fill_price_minor)} each. ${formatPaperMoney(fillTrade.funding_currency || "SGD", fillTrade.funding_credit_minor)} was paid into your savings.${demoDisclosure ? ` ${demoDisclosure}.` : ""}`
    : fillTrade
    ? `Bought ${fillTrade.quantity} ${paperFill.instrument?.name || fillTrade.symbol} ${Number(fillTrade.quantity) === 1 ? "share" : "shares"} at ${formatPaperMoney(fillTrade.quote_currency || "USD", fillTrade.fill_price_minor)} each. Your savings and portfolio are updated.${demoDisclosure ? ` ${demoDisclosure}.` : ""}`
    : result.paper_order_result?.status === "QUEUED"
    ? `Approved. ${result.paper_order_result.instrument?.name || "The order"} will ${result.paper_order_result.side === "SELL" ? "sell" : "buy"} when the market opens. You can cancel it in Wealth until then.`
    : result.paper_order_result?.status === "OPEN"
    ? `Approved. The order is waiting for your price. You can cancel it in Wealth until it fills.`
    : result.outcome === "EXECUTED_SYNTHETIC_BILL_PAYMENT"
      ? `${result.resolved_action?.biller_display_name || "Your biller"} was paid ${formatSgd(result.resolved_action?.amount_minor || result.amount_minor)} from ${accountLabel(result, result.resolved_action?.source_account_id)}. Your balance is updated.`
    : result.reason_code === "MISSING_RECIPIENT"
      ? "Who would you like to pay? Tell me the recipient’s name, and I’ll check it with you."
      : result.answer || result.question || result.detail || reasonLabel(result.reason_code || result.outcome);
  appendAssistantMessage("assistant", customerResponse);
  if (ui.voiceLiveCaption) ui.voiceLiveCaption.textContent = customerResponse;
  if (ui.suggestionList) ui.suggestionList.hidden = true;
  if (result.state) renderState(result.state);
  recordExecutedActions(result);
  renderTrace(result.trace || [], result.outcome);
  renderCommit(result);
  clearResultAudio(ui.resultAudio);
  if (ui.replyAudioPlayer) ui.replyAudioPlayer.hidden = true;
  syncReplyAudioPlayer();
  ui.elapsedLabel.textContent = `${elapsedMs} ms end to end`;
  if (renderVoiceAndPayeeResult(result)) {
    finishVoiceResponse(result, spokenOrigin);
    return;
  }
  if (result.outcome === "ANSWERED" && category === "unknown" && !result.conversation) {
    setOutcome("blocked", "!", "Request needs attention", reasonLabel(result.reason_code || "UNKNOWN_RESULT"), result.detail || "No action was assumed and no balance changed.");
  } else if (result.paper_order_result) {
    const paper = result.paper_order_result;
    if (paper.status === "FILLED" && paper.trade?.side === "SELL") {
      const trade = paper.trade;
      const symbol = trade.symbol || paper.instrument?.symbol || "";
      const quantity = Number(trade.quantity || 0);
      void loadInvestments();
      const holding = (result.paper_equity?.holdings || []).find(item => item.symbol === symbol);
      const stillHeld = Number(holding?.quantity || 0);
      const fillPrice = formatPaperMoney(trade.quote_currency || paper.instrument?.quote_currency || "USD", trade.fill_price_minor || 0);
      const fundingAccount = (result.state?.accounts || []).find(account => account.account_id === paper.funding_account_id);
      const fee = Number(trade.fee_funding_minor || 0);
      const instrumentName = paper.instrument?.name || symbol || "Stock";
      setOutcome("success", "✓", `${instrumentName} · sale complete`,
        `${quantity} ${instrumentName} ${quantity === 1 ? "share" : "shares"} sold`,
        `Filled at ${fillPrice} per share. The proceeds are in your savings.`);
      ui.outcomeIndex.textContent = "✓";
      ui.outcomePanel.classList.add("trade-filled");
      ui.candidateList.hidden = false;
      ui.candidateList.innerHTML = `<div class="trade-update-grid">
        <div class="trade-update-primary"><span>Still in your portfolio</span><strong>${escapeHtml(`${stillHeld} ${stillHeld === 1 ? "share" : "shares"}`)}</strong><small>${escapeHtml(symbol)}</small></div>
        <div><span>Sold at</span><strong>${escapeHtml(`${fillPrice} each`)}</strong><small>Filled at market price</small></div>
        <div><span>Paid in</span><strong>${escapeHtml(formatPaperMoney(trade.funding_currency || "SGD", trade.funding_credit_minor || 0))}</strong><small>${fee ? `After a ${escapeHtml(formatPaperMoney(trade.funding_currency || "SGD", fee))} fee` : "Into your funding account"}</small></div>
        <div><span>${escapeHtml(fundingAccount?.label || "Savings")} now</span><strong>${escapeHtml(fundingAccount ? formatPaperMoney(fundingAccount.currency, fundingAccount.balance_minor) : "Updated")}</strong><small>Available now</small></div>
      </div>`;
    } else if (paper.status === "FILLED") {
      const trade = paper.trade || {};
      const symbol = trade.symbol || paper.instrument?.symbol || "";
      const quantity = Number(trade.quantity || 0);
      void loadInvestments();
      const holding = (result.paper_equity?.holdings || []).find(item => item.symbol === symbol);
      const sharesOwned = Number(holding?.quantity ?? quantity);
      const fillPrice = formatPaperMoney(trade.quote_currency || paper.instrument?.quote_currency || "USD", trade.fill_price_minor || 0);
      const averageEntry = holding?.quantity
        ? formatPaperMoney(holding.quote_currency, Math.round(holding.cost_quote_minor / holding.quantity))
        : fillPrice;
      const fundingAccount = (result.state?.accounts || []).find(account => account.account_id === paper.funding_account_id);
      const cashDebited = formatPaperMoney(trade.funding_currency || "SGD", trade.funding_debit_minor || 0);
      const accountBalance = fundingAccount
        ? formatPaperMoney(fundingAccount.currency, fundingAccount.balance_minor)
        : "Updated";
      const fee = Number(trade.fee_funding_minor || 0);
      const instrumentName = paper.instrument?.name || symbol || "Stock";
      const disclosure = demoExchangeDisclosure(paper.execution_evidence);
      setOutcome(
        "success",
        "✓",
        `${instrumentName} · purchase complete`,
        `You’re in — ${quantity} ${instrumentName} ${quantity === 1 ? "share" : "shares"} bought`,
        disclosure ? `${disclosure}. Filled at ${fillPrice} per share.` : `Filled at ${fillPrice} per share. Your portfolio is updated.`,
      );
      ui.outcomeIndex.textContent = "✓";
      ui.outcomePanel.classList.add("trade-filled");
      ui.candidateList.hidden = false;
      ui.candidateList.innerHTML = `${disclosure ? `<p class="demo-exchange-disclosure">${escapeHtml(disclosure)}</p>` : ""}<div class="trade-update-grid">
        <div class="trade-update-primary"><span>In your portfolio</span><strong>${escapeHtml(`${sharesOwned} ${sharesOwned === 1 ? "share" : "shares"}`)}</strong><small>${escapeHtml(symbol)} · avg entry ${escapeHtml(averageEntry)}</small></div>
        <div><span>Price paid</span><strong>${escapeHtml(`${fillPrice} each`)}</strong><small>Filled at market price</small></div>
        <div><span>Total paid</span><strong>${escapeHtml(cashDebited)}</strong><small>${fee ? `Includes ${escapeHtml(formatPaperMoney(trade.funding_currency || "SGD", fee))} fee` : "Taken from your funding account"}</small></div>
        <div><span>${escapeHtml(fundingAccount?.label || "Savings")} now</span><strong>${escapeHtml(accountBalance)}</strong><small>Available after purchase</small></div>
      </div>`;
    } else if (paper.status === "QUEUED" || paper.status === "OPEN") {
      const name = paper.instrument?.name || "Your shares";
      const selling = paper.side === "SELL";
      setOutcome("clarification", "…", "Order waiting for the market",
        `${name} hasn’t been ${selling ? "sold" : "bought"} yet`,
        paper.status === "QUEUED"
          ? `The market is closed. The approved order ${selling ? "sells" : "buys"} when it opens. Your balance and holdings haven’t changed; they change only if it fills. Cancel it any time before.`
          : "The approved order is waiting for your price. Your balance and holdings haven’t changed; they change only if it fills. Cancel it any time before.");
      if (paper.order_id && paper.digest) {
        ui.candidateList.hidden = false;
        ui.candidateList.innerHTML = `<button type="button" class="candidate-choice" data-cancel-stock-order="${escapeHtml(paper.order_id)}" data-order-digest="${escapeHtml(paper.digest)}">Cancel this order</button>`;
      }
    } else if (paper.status === "REQUOTE_REQUIRED" && priceMoved) {
      const name = paper.instrument?.name || "Your stock order";
      setOutcome(
        "clarification",
        "↻",
        "Price changed · order not filled",
        `${name} wasn’t bought at the approved limit`,
        "Nothing was bought and your balance is unchanged. Get a fresh quote to review a new order; you’ll approve the updated price before anything is purchased.",
      );
      const originalRequest = (spokenOrigin ? spokenIntentHistory[0] : lastCommandText)
        || lastCommandText || spokenIntentHistory[0] || "";
      if (originalRequest) {
        ui.candidateList.hidden = false;
        ui.candidateList.innerHTML = '<button type="button" class="candidate-choice" data-refresh-stock-quote>Get a fresh quote</button>';
        ui.candidateList.querySelector("[data-refresh-stock-quote]")?.addEventListener("click", () => {
          runCommand(originalRequest);
        }, {once: true});
      }
    } else {
      const name = paper.instrument?.name || "The shares";
      const selling = paper.side === "SELL";
      setOutcome("blocked", "!", "Order not completed",
        selling ? `${name} wasn’t sold` : `${name} wasn’t added to your portfolio`,
        selling ? "No shares were sold and your holdings are unchanged. You can try again."
          : "No shares were bought and your available balance is unchanged. You can review the request and try again.");
    }
  } else if (result.outcome === "EXECUTED_SYNTHETIC_TRANSFER") {
    setOutcome("success", "✓", "Synthetic transfer settled", "The ledger moved exactly once.", `${formatSgd(result.amount_minor)} moved to ${result.resolved_action.recipient_display_name}. The database recorded two balanced entries.`);
    if (result.receipt?.receipt_id) appendEvidenceLink(result.receipt.receipt_id);
    if (result.resolved_action.destination_kind === "PAYNOW_MOBILE" && result.receipt?.receipt_id) {
      offerSavePayee(result.receipt.receipt_id, result.resolved_action);
    }
  } else if (result.outcome === "EXECUTED_SYNTHETIC_BILL_PAYMENT") {
    const action = result.resolved_action || {};
    setOutcome("success", "✓", "Bill payment complete", `${action.biller_display_name || "Your bill"} is paid`, `${formatSgd(action.amount_minor || result.amount_minor)} was paid from ${accountLabel(result, action.source_account_id)}. Your available balance is now ${formatSgd(action.balance_after_minor)}.`);
    ui.outcomePanel.classList.add("trade-filled");
    if (result.receipt?.receipt_id) appendEvidenceLink(result.receipt.receipt_id);
  } else if (result.outcome === "ORDER_CONFIRMATION_REQUEST" && result.paper_order) {
    const purchaseName = result.paper_order.instrument?.name || "stock";
    if (result.paper_order.side === "SELL") {
      setOutcome("clarification", "⇄", "Stock sale · review", `Review your ${purchaseName} sale`, "Check the share count and price. Approve with Singpass, then your face.");
    } else {
      setOutcome("clarification", "⇄", "Stock purchase · review", `Review your ${purchaseName} purchase`, "Check the share count, price protection, and funding limit. Final approval happens on screen.");
    }
    renderPaperEquityOrder(ui.candidateList, result.paper_order, {escapeHtml}, approvePaperOrder);
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
    if (result.portfolio) void loadInvestments();
  } else if (result.outcome === "ANSWERED" && result.bill_payment) {
    const bill = result.bill_payment;
    const billAuth = bill.required_authentication || {};
    const authLabel = billAuth.requires_singpass
      ? "Singpass + facial recognition"
      : "Mock voice affirmation: say “yes, confirm” (no speaker identity check)";
    const amount = formatSgd(Number(bill.amount_minor || 0));
    const source = bill.source_account_label || "your selected account";
    setOutcome("success", "i", "Bill payment · proposal ready", result.answer, "Matched to the synthetic bill directory. No payment has been created.");
    ui.candidateList.hidden = false;
    ui.candidateList.innerHTML = `<article class="invest-card conversation-card"><div class="receipt-facts"><span><small>Biller</small><strong>${escapeHtml(bill.biller_display_name || bill.biller_label)}</strong></span><span><small>Amount</small><strong>${escapeHtml(amount)}</strong></span><span><small>From</small><strong>${escapeHtml(source)}</strong></span></div><p class="proposal-status">Read only · no receipt or payment yet</p><p class="proposal-status">Required if bill payments are enabled: ${escapeHtml(authLabel)}.</p></article>`;
  } else if (result.outcome === "ANSWERED" && result.investing) {
    const titles = {TRENDING: "Trending investments", PLANS: "Long-term growth plans", PORTFOLIO: "Your portfolio", QUOTE: "Price and outlook", REAL_STOCK_BRIEF: "Verified stock brief"};
    setOutcome("success", "i", titles[result.investing.view] || "Investing", result.answer,
      result.investing.view === "REAL_STOCK_BRIEF"
        ? "Timestamped Yahoo Finance research data. No order has been created. Not financial advice."
        : "Rule-based on simulated market data and your risk profile; the AI is not used. Not financial advice.");
    renderInvestingAnswer(ui.candidateList, result.investing, investHelpers, runCommand, cancelPlan);
    if (result.investing.view === "REAL_STOCK_BRIEF") renderConversationControls(result);
  } else if (result.outcome === "ANSWERED" && result.conversation) {
    syncConversation(result);
    const status = result.conversation.status;
    if (status === "READY_FOR_DRAFT") {
      setOutcome("success", "✓", "Investment details ready", result.answer, "Review the details above. No order has been created yet.");
    } else if (status === "CANCELLED") {
      setOutcome("waiting", "·", "Investment request cancelled", result.answer, "No order was created.");
    } else {
      setOutcome("clarification", "?", "Investment follow-up", result.answer, result.question || "Choose an option or reply by voice or text.");
    }
    renderConversationControls(result);
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
          <span><small>From</small><strong>${escapeHtml(formatFundingSource(receipt))}</strong></span>
          <span><small>To</small><strong>${escapeHtml(item.recipient_display_name)}</strong>${item.masked_destination ? ` <em class="masked-account">${escapeHtml(item.masked_destination)}</em>` : ""}</span>
          <span><small>Amount</small><strong>${escapeHtml(formatTransactionalMoney(item.currency || receipt.currency || "SGD", item.amount_minor))}</strong></span>
        </div>`).join("")}
      </div>
      <div class="batch-total"><span>Total debit</span><strong>${escapeHtml(formatTransactionalMoney(receipt.currency || "SGD", receipt.total_minor))}</strong></div>
      <p class="proposal-status">Validated · approval required</p>
      <p class="batch-expiry">Expires ${escapeHtml(expiryLabel)}</p>
      <div class="proposal-actions"><button type="button" class="candidate-edit" aria-label="Edit request">Edit</button><button type="button" class="candidate-choice">Review batch</button></div>
    </article>`;
    ui.candidateList.querySelector(".candidate-edit").addEventListener("click", editCurrentRequest);
    ui.candidateList.querySelector(".candidate-choice").addEventListener("click", async () => {
      await approveBatchReceipt(receipt);
    });
  } else if (result.outcome === "CLARIFICATION_REQUEST") {
    if (result.reason_code === "TRANSCRIPTION_DISAGREEMENT") {
      const disagreement = result.transcription_disagreement || {};
      const liveText = typeof disagreement.live_text === "string" ? disagreement.live_text.trim() : "";
      setOutcome(
        "clarification",
        "?",
        "Confirm what you said",
        result.question || "I heard two different versions. Which one did you mean?",
        "Nothing has been prepared or sent.",
      );
      ui.candidateList.hidden = false;
      ui.candidateList.innerHTML = liveText
        ? `<button type="button" class="candidate-choice" data-use-live-transcript>Yes, use “${escapeHtml(liveText)}”</button><button type="button" class="candidate-edit" data-retry-transcript>No, try again</button>`
        : '<button type="button" class="candidate-edit" data-retry-transcript>Try again</button>';
      ui.candidateList.querySelector("[data-use-live-transcript]")?.addEventListener("click", () => runCommand(liveText), {once: true});
      ui.candidateList.querySelector("[data-retry-transcript]")?.addEventListener("click", () => void startVoice(), {once: true});
      finishVoiceResponse(result, spokenOrigin);
      return;
    }
    const multiActionClarification = multiActionClarificationPresentation(result);
    if (multiActionClarification) {
      setOutcome(
        "clarification",
        "?",
        multiActionClarification.title,
        multiActionClarification.question,
        multiActionClarification.detail,
      );
      const checklist = multiActionClarification.checklist || [];
      ui.candidateList.hidden = checklist.length === 0;
      ui.candidateList.innerHTML = checklist.length
        ? `<article class="multi-action-clarification-list" aria-label="Grouped request status">${checklist.map(item => `<div class="multi-action-clarification-row"><span>${escapeHtml(item.label)}</span><strong>${escapeHtml(item.status)}</strong></div>`).join("")}</article>`
        : "";
      if (result.clarification?.kind === "MULTI_ACTION") enterClarificationMode(result);
    } else if (result.bill_payment_resolution) {
      const resolution = result.bill_payment_resolution;
      const ambiguous = resolution.status === "BILLER_AMBIGUOUS";
      setOutcome(
        "clarification",
        "?",
        ambiguous ? "Choose a biller" : "Biller not supported yet",
        result.question || result.answer || "Which biller should I use?",
        ambiguous
          ? "Choose the matching supported demo biller. This only clarifies the request; no payment has been created."
          : "Edit the biller name and try again. No payment has been created.",
      );
      const candidates = resolution.candidates || [];
      ui.candidateList.hidden = false;
      ui.candidateList.innerHTML = candidates.length
        ? `<article class="invest-card conversation-card"><div class="invest-card-actions">${candidates.map((candidate, index) => `<button type="button" class="candidate-choice" data-biller-index="${index}">${escapeHtml(candidate.display_name)}</button>`).join("")}</div></article>`
        : '<button type="button" class="candidate-edit">Edit request</button>';
      ui.candidateList.querySelectorAll("[data-biller-index]").forEach(button => {
        button.addEventListener("click", () => {
          const candidate = candidates[Number(button.dataset.billerIndex)];
          const evidence = resolution.biller_evidence;
          const originalText = lastCommandText;
          if (!candidate || !evidence || !originalText.includes(evidence)) {
            setOutcome("blocked", "!", "Request needs editing", "I couldn't safely replace the biller name.", "Edit the biller name in your request, then send it again. No payment has been created.");
            return;
          }
          runCommand(originalText.replace(evidence, candidate.display_name));
        });
      });
      ui.candidateList.querySelector(".candidate-edit")?.addEventListener("click", editCurrentRequest);
    } else if (result.reason_code === "BILLER_REQUIRED" || result.reason_code === "AMOUNT_REQUIRED" || result.reason_code === "CURRENCY_REQUIRED") {
      setOutcome("clarification", "?", "Bill payment detail needed", result.question || result.answer, "No payment has been created. Add the missing detail to your request and send it again.");
    } else if (result.reason_code === "SCHEDULED_BILL_PAYMENT_UNSUPPORTED") {
      setOutcome("clarification", "?", "Bill payment timing needed", result.question || "Would you like to pay now?", "This demo only supports payment now. No bill receipt has been created.");
    } else if (result.reason_code === "MISSING_RECIPIENT") {
      setOutcome("clarification", "?", "One detail needed", "Who would you like to pay?", "Tell me the recipient’s name. Nothing has been sent.");
    } else {
    if (result.reason_code === "MISSING_AMOUNT" && result.clarification) {
      setOutcome("clarification", "?", "One detail left", "How much would you like to send?", "Your recipient and account are noted. Nothing has moved; tell me the amount when you’re ready.");
      enterClarificationMode(result);
      finishVoiceResponse(result, spokenOrigin);
      return;
    }
    const choices = result.receipt_choices || [];
    const readOnlyBill = result.read_only_bill_payment;
    const billReceipt = choices.length === 1 && choices[0].destination_kind === "UTILITY_BILL";
    const combinedNote = readOnlyBill && choices[0]
      ? ` A read-only ${readOnlyBill.biller_display_name || "utility bill"} proposal for ${formatSgd(readOnlyBill.amount_minor)} is also included in the intent total (${formatSgd(readOnlyBill.amount_minor + choices[0].amount_minor)}). The bill itself will not be paid.`
      : "";
    setOutcome("clarification", billReceipt ? "▤" : "?", billReceipt ? "Review bill payment" : "Please review before sending", result.question || "Which recipient did you mean?", `Check the recipient, amount and source account below. Nothing will move until you approve this payment on your device.${combinedNote}`);
    ui.candidateList.hidden = false;
    ui.candidateList.innerHTML = choices.map((choice, index) => {
      const expiry = new Date(choice.expires_at);
      const expiryLabel = Number.isNaN(expiry.getTime())
        ? choice.expires_at
        : expiry.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
      const payNow = choice.destination_kind === "PAYNOW_MOBILE";
      const billPayment = choice.destination_kind === "UTILITY_BILL";
      const matchLabel = result.reason_code === "PHONETIC_RECIPIENT_MATCH"
        ? '<p class="match-label">Possible phonetic match</p>'
        : payNow ? '<p class="match-label">Via PayNow · directory verified</p>' : "";
      const validationLabel = choice.validation?.verdict === "PASS"
        ? "Validated · approval required"
        : "Review required";
      return `<article class="receipt-choice">
        <p class="proposal-status">${escapeHtml(validationLabel)}</p>
        ${matchLabel}
        <div class="receipt-facts">
          <span><small>From</small><strong>${escapeHtml(formatFundingSource(choice))}</strong></span>
          <span><small>${billPayment ? "Biller" : "To"}</small><strong>${escapeHtml(choice.recipient_display_name)}</strong>${choice.masked_destination && !billPayment ? ` <em class="masked-account">${escapeHtml(choice.masked_destination)}</em>` : ""}</span>
          <span><small>Amount</small><strong>${escapeHtml(formatTransactionalMoney(choice.currency || result.currency || "SGD", choice.amount_minor))}</strong></span>
          ${payNow ? `<span><small>Proxy</small><strong>${escapeHtml(choice.destination_reference)}</strong></span>` : ""}
          ${choice.payment_reference ? `<span><small>Reference</small><strong>${escapeHtml(choice.payment_reference)}</strong></span>` : ""}
          ${choice.payee ? `<span><small>History</small><strong>${escapeHtml(choice.payee.new_payee ? "New payee" : [choice.payee.last_payment, choice.payee.typical].filter(Boolean).join(" · ") || "—")}</strong></span>` : ""}
          <span><small>Expires</small><strong>${escapeHtml(expiryLabel)}</strong></span>
        </div>
        <div class="proposal-actions">
          <button type="button" class="candidate-edit" aria-label="Edit request">Edit</button>
          <button type="button" class="candidate-choice" data-index="${index}">${billPayment ? "Review bill" : "Review"}</button>
        </div>
      </article>`;
    }).join("");
    ui.candidateList.querySelectorAll(".candidate-edit").forEach(button => {
      button.addEventListener("click", editCurrentRequest);
    });
    ui.candidateList.querySelectorAll(".candidate-choice").forEach(button => {
      button.addEventListener("click", async () => {
        const choice = choices[Number(button.dataset.index)];
        await approveReceipt(choice);
      });
    });
    }
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
  } else if (category === "unknown") {
    setOutcome("blocked", "!", "Request needs attention", reasonLabel(result.reason_code || "UNKNOWN_RESULT"), result.detail || "No action was assumed and no balance changed.");
  } else {
    setOutcome("failed", "×", "Execution failed safely", (result.reason_code || "UNKNOWN_ERROR").replaceAll("_", " ").toLowerCase(), result.detail || "No ledger mutation occurred.");
  }
  // Approval actions happen through a button, but their outcome is still part
  // of the voice conversation. Read it aloud, then reopen the microphone.
  finishVoiceResponse(
    result,
    spokenOrigin || Boolean(result.paper_order_result)
      || result.outcome === "EXECUTED_SYNTHETIC_BILL_PAYMENT",
  );
  if (activeMultiActionResponse && (
      String(result.outcome || "").startsWith("EXECUTED_") || result.paper_order_result || result.paper_order
  )) void refreshMultiActionCards(activeMultiActionResponse);
}

function renderMultiActionCards(result) {
  ui.candidateList.hidden = false;
  ui.candidateList.innerHTML = renderMultiActionReview(result.items, escapeHtml, formatSgd);
  ui.candidateList.querySelectorAll(".multi-action-approve").forEach(button => {
    button.addEventListener("click", async () => {
      const item = result.items[Number(button.dataset.multiIndex)];
      if (!item) return;
      if (button.dataset.childType === "batch_receipt") {
        await approveBatchReceipt(item.result.batch_receipt);
      } else if (button.dataset.childType === "paper_order") {
        await approvePaperOrder(item.result.paper_order);
      } else {
        const choice = item.result.receipt_choices?.[0];
        if (choice) await approveReceipt(choice);
      }
      await refreshMultiActionCards(result);
    });
  });
}

async function refreshMultiActionCards(result) {
  const groupId = result?.multi_action_group?.group_id;
  if (!groupId) return;
  const latest = await fetch(`/api/multi-actions/${encodeURIComponent(groupId)}`, {cache: "no-store"})
    .then(response => response.ok ? response.json() : null).catch(() => null);
  if (!latest?.items) return;
  result.items = result.items.map((existing, index) => ({
    ...existing,
    status: latest.items[index]?.status || existing.status,
    result: latest.items[index]?.result || existing.result,
  }));
  activeMultiActionResponse = result;
  renderMultiActionCards(result);
}

function setBusy(busy) {
  setComposerDisabled(busy);
  ui.runButton.disabled = busy;
  ui.newRequestButton.disabled = busy;
  if (ui.agentAvailability) ui.agentAvailability.textContent = busy ? "Working" : "Online";
  const idleLabel = activeClarification
    ? pendingClarificationResponse ? "Retry exact response" : "Continue exact request"
    : pendingActionRequest ? "Retry exact request" : "Run intention";
  ui.runButton.setAttribute("aria-label", busy ? "Sending request to Dimon"
    : idleLabel === "Run intention" ? "Send request to Dimon" : idleLabel);
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

async function loadPayees() {
  if (!ui.payeeList) return;
  let usingDemoFallback = false;
  try {
    let payees;
    try {
      const response = await fetch("/api/payees", {cache: "no-store"});
      if (!response.ok) throw new Error(`Gateway returned HTTP ${response.status}`);
      payees = await response.json();
    } catch (_error) {
      // Keep the seeded synthetic demo directory usable if the backend is an
      // older process that has not picked up the payees endpoint yet.
      usingDemoFallback = true;
      payees = [
        {display_name: "John Smith", paynow_proxy: "82352935"},
        {display_name: "Alicia Powell", paynow_proxy: "81234567"},
        {display_name: "John Tan", paynow_proxy: "87654321"},
      ];
    }
    ui.payeeList.replaceChildren();
    if (!Array.isArray(payees) || payees.length === 0) {
      const empty = document.createElement("p");
      empty.className = "payee-list-message";
      empty.textContent = "No saved contacts yet.";
      ui.payeeList.append(empty);
      return;
    }

    const list = document.createElement("ul");
    list.className = "payee-contact-list";
    for (const payee of payees) {
      if (typeof payee?.display_name !== "string" || !payee.display_name.trim()) continue;
      const name = payee.display_name.trim();
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "payee-contact";
      button.dataset.payeeName = name;
      button.setAttribute("aria-label", `Choose ${name}`);
      const avatar = document.createElement("span");
      avatar.className = "payee-avatar";
      avatar.setAttribute("aria-hidden", "true");
      avatar.textContent = name.split(/\s+/).slice(0, 2).map(part => part[0]).join("").toUpperCase();
      const label = document.createElement("span");
      label.className = "payee-contact-name";
      const nameLine = document.createElement("span");
      nameLine.textContent = name;
      label.append(nameLine);
      if (payee.is_new) {
        const flag = document.createElement("span");
        flag.className = "payee-new-flag";
        flag.textContent = "New";
        nameLine.append(flag);
      }
      if (typeof payee.paynow_proxy === "string" && payee.paynow_proxy.trim()) {
        const proxyLine = document.createElement("small");
        proxyLine.className = "payee-contact-proxy";
        proxyLine.textContent = `PayNow · ${payee.paynow_proxy}`;
        label.append(proxyLine);
      }
      // Nicknames that resolve to this payee ("mum", "Uncle Tan") and the
      // last payment, so a spoken nickname is never a surprise.
      const nicknames = (Array.isArray(payee.aliases) ? payee.aliases : [])
        .filter(alias => typeof alias === "string" && !alias.startsWith("my ")
          && alias.toLowerCase() !== name.toLowerCase());
      const meta = [
        nicknames.length ? `“${nicknames.slice(0, 3).join("”, “")}”` : "",
        typeof payee.last_payment === "string" ? payee.last_payment : "",
      ].filter(Boolean).join(" · ");
      if (meta) {
        const metaLine = document.createElement("small");
        metaLine.className = "payee-contact-meta";
        metaLine.textContent = meta;
        label.append(metaLine);
      }
      const action = document.createElement("span");
      action.className = "payee-contact-action";
      action.setAttribute("aria-hidden", "true");
      action.textContent = "›";
      button.append(avatar, label, action);
      item.append(button);
      list.append(item);
    }
    if (!list.children.length) {
      const empty = document.createElement("p");
      empty.className = "payee-list-message";
      empty.textContent = "No saved contacts yet.";
      ui.payeeList.append(empty);
      return;
    }
    ui.payeeList.append(list);
    if (usingDemoFallback) {
      const note = document.createElement("p");
      note.className = "payee-list-message";
      note.textContent = "Showing demo contacts. Restart the app server to sync saved contacts.";
      ui.payeeList.append(note);
    }
  } catch (_error) {
    const unavailable = document.createElement("p");
    unavailable.className = "payee-list-message";
    unavailable.textContent = "Contacts could not be loaded. Reload the page to try again.";
    ui.payeeList.replaceChildren(unavailable);
  }
}

async function loadSession() {
  const response = await fetch("/api/session");
  if (!response.ok) throw new Error(`Session returned HTTP ${response.status}`);
  const session = await response.json();
  const fixtureMode = session.inference_mode === "fixture";
  ui.inferenceModeBadge.hidden = !fixtureMode;
  return session;
}

function voiceUrl() {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${scheme}//${window.location.host}/api/voice/session`;
  if (activeClarification?.clarification_id) {
    const params = new URLSearchParams({clarification_id: activeClarification.clarification_id});
    return `${url}?${params}`;
  }
  if (!activeConversation || activeConversation.status !== "WAITING") return url;
  const params = new URLSearchParams({
    conversation_id: activeConversation.conversation_id,
    expected_version: String(activeConversation.version),
  });
  return `${url}?${params}`;
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
  ui.voiceWaveform?.querySelectorAll("span").forEach(bar => bar.style.removeProperty("height"));
  ui.voiceButton.classList.remove("recording");
  ui.voiceButton.setAttribute("aria-pressed", "false");
  ui.voiceButton.removeAttribute("aria-busy");
}

function armVoiceDeadline(delayMs, message) {
  voiceDeadline.arm(delayMs, () => {
    voiceTerminal = true;
    setVoicePhase("error");
    setAssistantMode("error", message);
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
    setVoicePhase("transcribing");
    setAssistantMode("understanding", "Audio captured · processing your request");
    voiceFinishing = true;
    armVoiceDeadline(20000, "Transcription timed out · try again or type the request");
  } else if (sendStop && voicePhase === "starting") {
    voiceDeadline.clear();
    voiceTerminal = true;
    setVoicePhase("idle");
    setAssistantMode("idle", "Voice request cancelled · ready when you are");
    ui.voiceStatus.textContent = "Voice request cancelled · typed fallback ready";
    if (voiceSocket && voiceSocket.readyState < WebSocket.CLOSING) voiceSocket.close();
  } else if (!sendStop) {
    setVoicePhase("transcribing");
    setAssistantMode("understanding", "Audio captured · processing your request");
    voiceFinishing = true;
    armVoiceDeadline(20000, "Transcription timed out · try again or type the request");
  }
  await releaseVoiceCapture();
}

async function startVoice() {
  ui.suggestionList.hidden = true;
  hideTypedFallback();
  stopReplyAudio({keepReview: false});
  await wakeWord?.pauseForPushToTalk?.();
  handsFreePausedForPushToTalk = Boolean(wakeWord);
  await startAudioWorkletVoice();
}

let handsFreePausedForPushToTalk = false;

function resumeHandsFreeAfterPushToTalk() {
  if (!handsFreePausedForPushToTalk) return;
  handsFreePausedForPushToTalk = false;
  void wakeWord?.resumeAfterPushToTalk?.();
}

async function startAudioWorkletVoice() {
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
    resumeHandsFreeAfterPushToTalk();
    setComposerDisabled(false);
    ui.voiceStatus.textContent = "Microphone unavailable · use typed input";
    setAssistantMode("idle", "Microphone unavailable · type a request or check microphone permissions");
    return;
  }
  try {
    voiceTerminal = false;
    setVoicePhase("starting");
    setAssistantMode("starting", "Connecting to Dimon’s voice service · tap Cancel to stop");
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
        setVoiceLevel(event.data);
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
      // Speech has started: the silence timeout no longer applies.
      if (message.type === "pause" || message.type === "endpoint"
        || (message.type === "transcript_partial" && message.text)) clearTimeout(voiceSilenceTimer);
      if (message.type === "ready") {
        voiceDeadline.clear();
        voiceReady = true;
        // Nothing said: close the microphone and go back to waiting for
        // "Hey Dimon" instead of listening forever.
        clearTimeout(voiceSilenceTimer);
        voiceSilenceTimer = setTimeout(() => {
          if (voicePhase !== "listening") return;
          voiceTerminal = true;
          setAssistantMode(pendingOnScreenReview ? "review" : "idle", "Say “Hey Dimon” or tap Speak when you need me");
          ui.voiceStatus.textContent = "Stopped listening · say Hey Dimon or tap Speak";
          if (voiceSocket && voiceSocket.readyState < WebSocket.CLOSING) voiceSocket.close();
        }, VOICE_SILENCE_MS);
        setVoicePhase("listening");
        setAssistantMode("listening");
        ui.voiceButton.removeAttribute("aria-busy");
        ui.voiceStatus.textContent = "Listening… pause to send";
        if (ui.voiceLiveCaption) {
          ui.voiceLiveCaption.textContent = "I’m listening. Go ahead.";
        }
      } else if (message.type === "pause") {
        ui.voiceStatus.textContent = "Pause detected… finishing request";
      } else if (message.type === "endpoint") {
        setAssistantMode("understanding", "I heard you · working on it");
        ui.voiceStatus.textContent = "Transcribing…";
        void stopVoice({sendStop: false});
      } else if (message.type === "transcript_partial") {
        const caption = voiceCaptionPresentation(message.type, message.text);
        if (ui.voiceLiveCaption) {
          ui.voiceLiveCaption.textContent = caption.text;
          ui.voiceLiveCaption.classList.toggle("provisional", caption.provisional);
          ui.voiceLiveCaption.setAttribute("aria-live", caption.ariaLive);
        }
        ui.voiceStatus.textContent = message.text;
      } else if (message.type === "transcript_final") {
        ui.input.value = message.text;
        lastHeardText = message.text;
        recordSpokenIntent(message.text);
        appendAssistantMessage("user", message.text);
        if (ui.voiceLiveCaption) {
          const caption = voiceCaptionPresentation(message.type, message.text);
          ui.voiceLiveCaption.textContent = caption.text;
          ui.voiceLiveCaption.classList.remove("provisional");
          ui.voiceLiveCaption.setAttribute("aria-live", caption.ariaLive);
        }
        ui.voiceStatus.textContent = `Heard: ${message.text}`;
      } else if (message.type === "transcript_conflict") {
        lastHeardText = message.live_text || "";
        if (ui.voiceLiveCaption) {
          ui.voiceLiveCaption.textContent = "I heard two different versions · please confirm";
          ui.voiceLiveCaption.classList.remove("provisional");
          ui.voiceLiveCaption.setAttribute("aria-live", "polite");
        }
        ui.voiceStatus.textContent = "Transcription needs confirmation";
      } else if (message.type === "action_result") {
        resumeHandsFreeAfterPushToTalk();
        lastRequestWasSpoken = true;
        if (activeClarification) {
          activeClarification = null;
          pendingClarificationResponse = null;
        }
        ui.voiceButton.disabled = false;
        voiceDeadline.clear();
        voiceTerminal = true;
        setVoicePhase("complete");
        if (wakeWord) {
          // Spoken request: Dimon answers aloud, asks any follow-up by voice,
          // and takes the approval by voice (voiceprint + this device's key).
          void wakeWord.converse(message.result, lastHeardText).finally(afterConversation);
        } else {
          renderResult(message.result, 0);
        }
        ui.voiceStatus.textContent = "Request completed · typed fallback ready";
        voiceFinishing = false;
      } else if (message.type === "error") {
        resumeHandsFreeAfterPushToTalk();
        voiceDeadline.clear();
        voiceTerminal = true;
        setVoicePhase("error");
        voiceFinishing = false;
        setComposerDisabled(false);
        recordEvidenceDiagnostic("voice_session", {reason_code: message.reason_code});
        ui.voiceStatus.textContent = "Voice input is unavailable · use typed input";
        setAssistantMode("error", "Voice input is unavailable · type your request or try again");
        if (ui.voiceLiveCaption) {
          ui.voiceLiveCaption.textContent = "Voice input is unavailable right now. Type your request or try again.";
        }
        void releaseVoiceCapture();
      }
    };
    voiceSocket.onclose = () => {
      resumeHandsFreeAfterPushToTalk();
      clearTimeout(voiceSilenceTimer);
      const closedPhase = voicePhase;
      const resumeAfterClose = voiceResumeRequested && !ui.askView.hidden;
      voiceResumeRequested = false;
      voiceDeadline.clear();
      voiceSocket = null;
      voiceFinishing = false;
      if (!voiceTerminal && closedPhase !== "idle") {
        ui.voiceStatus.textContent = "Voice connection closed · try again or type the request";
      }
      setVoicePhase("idle");
      setComposerDisabled(false);
      void releaseVoiceCapture();
      if (resumeAfterClose) {
        window.setTimeout(() => void startAudioWorkletVoice(), 0);
      }
    };
    await new Promise((resolve, reject) => {
      voiceSocket.addEventListener("open", resolve, {once: true});
      voiceSocket.addEventListener("error", () => reject(new Error("voice socket unavailable")), {once: true});
    });
    if (voicePhase === "starting" && !voiceReady) {
      armVoiceDeadline(125000, "Speech model did not start · try again or type the request");
    }
  } catch (error) {
    resumeHandsFreeAfterPushToTalk();
    voiceDeadline.clear();
    voiceTerminal = true;
    setVoicePhase("idle");
    setComposerDisabled(false);
    voiceFinishing = false;
    const failure = describeVoiceFailure(voiceStage, error);
    ui.voiceStatus.textContent = `${failure.status} (${failure.stage}/${failure.errorName})`;
    setAssistantMode("error", `${failure.status} · type a request or try the microphone again`);
    void reportVoiceFailure(failure);
    if (voiceSocket && voiceSocket.readyState < WebSocket.CLOSING) voiceSocket.close();
    await releaseVoiceCapture();
  }
}

ui.input.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    if (voiceLocksManualSubmit(voicePhase)) return;
    ui.form.requestSubmit();
  }
});
ui.input.addEventListener("input", autoGrowComposer);

ui.form.addEventListener("submit", async event => {
  event.preventDefault();
  const text = ui.input.value.trim();
  if (!text) {
    if (ui.voiceFallbackGuidance) ui.voiceFallbackGuidance.textContent = "Type a request before sending.";
    ui.input.focus();
    return;
  }
  if (ui.voiceFallbackGuidance) ui.voiceFallbackGuidance.textContent = "";
  ui.suggestionList.hidden = true;
  hideTypedFallback({focus: false});
  stopReplyAudio({keepReview: false});
  appendAssistantMessage("user", text);
  ui.outcomePanel.hidden = true;
  beginAssistantTurn();
  if (activeConversation?.status === "WAITING") {
    lastRequestWasSpoken = false;
    await submitConversationTurn({kind: "TEXT", text});
    return;
  }
  // A completed/expired conversation cannot absorb a new intention.
  setActiveConversation(null);
  if (!activeClarification) {
    lastCommandText = text;
    multiRequest.noteSubmit();
  }
  lastRequestWasSpoken = false;
  const started = performance.now();
  setBusy(true);
  if (ui.conversationStatus) ui.conversationStatus.textContent = activeClarification
    ? "Dimon is checking your reply against the saved request."
    : "Dimon is checking your message.";
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
        endAssistantTurn("The reply is delayed. Retrying will send the same response safely.");
        ui.input.readOnly = true;
        ui.outcomePanel.hidden = false;
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
      renderUnknownActionRecovery(actionResponse, started);
      return;
    }
    pendingActionRequest = null;
    ui.input.readOnly = false;
    renderResult(actionResponse.result, Math.round(performance.now() - started));
  } catch (error) {
    recordEvidenceDiagnostic("action_request", error);
    if (pendingActionRequest) {
      renderUnknownActionRecovery({message: "Connection interrupted"}, started);
    } else {
      endAssistantTurn("Dimon could not complete that request.");
      setOutcome("failed", "×", "Request unavailable", "The request could not be completed.", "Check your connection and try again.");
      setSettlement("blocked", "Not sent", [], "No request was sent. Your balances are unchanged.");
    }
  } finally {
    setBusy(false);
    ui.voiceButton.focus();
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
    recordEvidenceDiagnostic("demo_reset", error);
    setOutcome("failed", "×", "Reset could not finish", "Your current demo balances are unchanged.", "Check your connection and try again.");
  } finally {
    ui.resetButton.disabled = false;
  }
});

ui.newRequestButton.addEventListener("click", () => {
  wakeWord?.cancelListen();
  wakeWord?.stopSpeaking();
  stopReplyAudio({keepReview: false});
  resetCommandMode();
  activeMultiActionResponse = null;
  multiRequest.resetPlan();
  lastCommandText = "";
  lastRequestWasSpoken = false;
  spokenIntentHistory.splice(0);
  renderIntentHistory();
  resetChatTranscript();
  ui.suggestionList.hidden = false;
  hideTypedFallback({focus: false});
  ui.voiceButton.disabled = false;
  pendingOnScreenReview = false;
  setAssistantMode("idle");
  if (ui.conversationStatus) ui.conversationStatus.textContent = "New chat started. Previous messages are no longer shown.";
  if (ui.voiceLiveCaption) ui.voiceLiveCaption.textContent = "Ask naturally. Your words and Dimon’s replies will appear here.";
  ui.voiceButton.focus();
});

ui.voiceButton.addEventListener("click", () => {
  // A "Hey Dimon" conversation uses the same button: Stop while Dimon talks,
  // Finish while it waits for an answer.
  if (wakeWord?.isSpeaking()) {
    wakeWord.stopSpeaking();
    return;
  }
  if (wakeWord?.isListening()) {
    wakeWord.cancelListen();
    return;
  }
  if (ui.resultAudio && !ui.resultAudio.paused) {
    stopReplyAudio({keepReview: pendingOnScreenReview});
    setAssistantMode("idle", "Reply stopped · tap the microphone to speak");
    return;
  }
  if (voicePhase !== "idle") {
    void stopVoice();
    return;
  }
  startVoice();
});

ui.voiceTextToggle.addEventListener("click", () => {
  const opening = ui.form.hidden;
  ui.form.hidden = !opening;
  ui.voiceTextToggle.setAttribute("aria-expanded", String(opening));
  ui.voiceTextToggle.textContent = opening ? "Close typing" : "Type instead";
  if (opening) ui.input.focus();
});

ui.homeForm?.addEventListener("submit", event => {
  event.preventDefault();
  submitFromComposer(ui.homeForm, ui.homeInput, {
    targetInput: ui.input,
    targetForm: ui.form,
    activate: () => activateView(document, "ask"),
    canSubmit: () => !ui.input.readOnly,
    onBlocked: () => {
      activateView(document, "ask");
      showTypedFallback();
      ui.voiceStatus.textContent = "Finish or clear the pending exact retry before starting a new request";
    },
  });
});

ui.homeVoiceButton?.addEventListener("click", () => {
  activateView(document, "ask");
  ui.voiceButton.click();
});

ui.homeTypeToggle?.addEventListener("click", () => {
  const opening = ui.homeTypingRow.hidden;
  ui.homeTypingRow.hidden = !opening;
  ui.homeTypeToggle.setAttribute("aria-expanded", String(opening));
  ui.homeTypeToggle.textContent = opening ? "Close typing" : "Type instead";
  if (opening) ui.homeInput.focus();
});

function syncViewToLocation({focus = true} = {}) {
  const viewName = viewNameFromHash(window.location.hash) || "home";
  activateView(document, viewName, {focus});
}

function navigateToView(viewName) {
  if (viewNameFromHash(window.location.hash) === viewName) {
    activateView(document, viewName);
    return;
  }
  if (window.history?.pushState) {
    window.history.pushState({view: viewName}, "", `#${viewName}`);
    activateView(document, viewName);
    return;
  }
  window.location.hash = viewName;
}

function bindWealthNavigation() {
  const wealthRoot = document.querySelector("#wealth-view");
  if (!wealthRoot || wealthRoot.dataset.bound === "true") return;
  const tabs = [...wealthRoot.querySelectorAll("[role=tab][data-wealth-section]")];
  const panels = [...wealthRoot.querySelectorAll("[role=tabpanel]")];
  const activateWealthSection = (wealthSection, {focus = false} = {}) => {
    const selectedTab = tabs.find(tab => tab.dataset.wealthSection === wealthSection) || tabs[0];
    if (!selectedTab) return;
    tabs.forEach(tab => {
      const selected = tab === selectedTab;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    });
    panels.forEach(panel => {
      panel.hidden = panel.id !== selectedTab.getAttribute("aria-controls");
    });
    if (focus) selectedTab.focus();
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => activateWealthSection(tab.dataset.wealthSection));
    tab.addEventListener("keydown", event => {
      let nextIndex = null;
      if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
      if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
      if (event.key === "Home") nextIndex = 0;
      if (event.key === "End") nextIndex = tabs.length - 1;
      if (nextIndex == null) return;
      event.preventDefault();
      activateWealthSection(tabs[nextIndex].dataset.wealthSection, {focus: true});
    });
  });
  wealthRoot.dataset.bound = "true";
  activateWealthSection(tabs.find(tab => tab.getAttribute("aria-selected") === "true")?.dataset.wealthSection || "overview");
}

syncViewToLocation({focus: false});
window.addEventListener("hashchange", () => syncViewToLocation());
window.addEventListener("popstate", () => syncViewToLocation());

document.querySelectorAll("[data-view-target]").forEach(button => {
  button.addEventListener("click", () => {
    navigateToView(button.dataset.viewTarget);
  });
});

document.querySelectorAll("[data-view-link]").forEach(control => {
  control.addEventListener("click", event => {
    if (control.tagName === "A") return;
    event.preventDefault();
    navigateToView(control.dataset.viewLink);
  });
});

bindWealthNavigation();

document.querySelectorAll(".scenario-load, [data-prompt]").forEach(button => {
  button.addEventListener("click", () => {
    resetCommandMode();
    ui.input.value = button.dataset.prompt || "";
    autoGrowComposer();
    activateView(document, "ask");
    showTypedFallback();
    ui.input.setSelectionRange(ui.input.value.length, ui.input.value.length);
    ui.voiceStatus.textContent = "Prompt loaded · review it before sending";
  });
});

ui.payeeList?.addEventListener("click", event => {
  const contact = event.target.closest?.(".payee-contact[data-payee-name]");
  if (!contact) return;
  activateView(document, "ask");
  runCommand(`Send ${contact.dataset.payeeName}`);
});

document.querySelectorAll("[data-suggestion-intent]").forEach(button => {
  button.addEventListener("click", () => {
    ui.suggestionList.hidden = true;
    stopReplyAudio({keepReview: false});
    const intent = resolveStarterIntent(button.dataset.suggestionIntent);
    if (intent) runCommand(button.dataset.suggestionPrompt || button.textContent.trim());
  });
});

document.addEventListener("click", event => {
  const button = event.target.closest?.(".invest-action[data-say], .wealth-ask-button[data-say]");
  if (!button) return;
  activateView(document, "ask");
  runCommand(button.dataset.say);
});
// ---------- Several requests in one sentence ----------
function renderPlanBar() {
  if (ui.planBar) multiRequest.renderBar(ui.planBar, escapeHtml, openPlanStep, showPlan);
}

async function openPlanStep(index) {
  const result = multiRequest.openStep(index);
  if (!result) return;
  // Another purchase in the same sentence may have replaced this one's open
  // conversation; ask it again from the user's own words.
  const conversation = result.conversation;
  if (conversation?.status === "WAITING") {
    const latest = await fetch(`/api/conversations/${encodeURIComponent(conversation.conversation_id)}`, {cache: "no-store"})
      .then(response => (response.ok ? response.json() : null)).catch(() => null);
    const current = latest?.conversation;
    if (!current || current.status !== "WAITING" || current.version !== conversation.version) {
      multiRequest.markFollowUp();
      runCommand(multiRequest.stepText(index));
      return;
    }
  }
  renderResult(result, 0);
}

function showPlan() {
  multiRequest.closeStep();
  if (ui.planBar) ui.planBar.hidden = true;
  setOutcome("clarification", "≡", "Several requests · each approved on its own",
    "Review each request.", "Nothing has moved. Each request has its own receipt or question, and approving one never approves another.");
  multiRequest.renderList(ui.candidateList, escapeHtml, openPlanStep);
}

// Follow-ups add the user's own words to the request they belong to and
// re-run it through the full pipeline ("... from everyday", "... as Uncle Tan").
function followUpCommand(extra) {
  const base = multiRequest.activeStepText() || lastCommandText;
  multiRequest.markFollowUp();
  runCommand(`${base.replace(/[.!\s]+$/, "")} ${extra}`);
}

function requestSignedJson(path, receipt, signed) {
  return fetch(path, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({receipt_digest: receipt.receipt_digest, ...signed}),
  }).then(async response => response.ok
    ? {kind: "RESULT", result: await response.json()}
    : {kind: "UNKNOWN", message: `Gateway returned HTTP ${response.status}`})
    .catch(error => ({kind: "UNKNOWN", message: error instanceof Error ? error.message : String(error)}));
}

const requestPayeeAdditionApproval = (addition, signed) =>
  requestSignedJson(`/api/payee-additions/${encodeURIComponent(addition.receipt_id)}/approve`, addition, signed);

function approvePayeeAddition(addition) {
  return signedApproval(
    "payee-additions",
    addition,
    requestPayeeAdditionApproval,
    "Retry this exact payee draft to retrieve its stored result; it can be saved at most once.",
  );
}

// Outcomes from multi-request, PayNow payee additions and voice registration.
// Returns true when it rendered the result.
function renderVoiceAndPayeeResult(result) {
  const setCategory = label => { if (ui.assistantCategory) ui.assistantCategory.textContent = label; };
  if (result.outcome === "MULTI_REQUEST") {
    setCategory("Requests");
    multiRequest.startPlan(result);
    showPlan();
    ui.outcomeDetail.textContent = `${result.question} Nothing has moved; approving one request never approves another.`;
    return true;
  }
  if (result.outcome === "PAYEE_ADDITION_REQUEST") {
    setCategory("Payments");
    const addition = result.payee_addition;
    setOutcome("clarification", "+", "New PayNow payee · confirm with your face", result.question,
      "No money moves. After saving, payments to this payee are capped and need a one-time code for 24 hours.");
    ui.candidateList.hidden = false;
    ui.candidateList.innerHTML = `<article class="receipt-choice">
      <p class="proposal-status">PayNow · directory verified</p>
      <div class="receipt-facts">
        <span><small>Save as</small><strong>${escapeHtml(addition.nickname)}</strong></span>
        <span><small>Registered name</small><strong>${escapeHtml(addition.masked_name)}</strong></span>
        <span><small>Number</small><strong>${escapeHtml(addition.masked_proxy)}</strong></span>
      </div>
      <div class="proposal-actions">
        <button type="button" class="candidate-edit" aria-label="Edit request">Edit</button>
        <button type="button" class="candidate-choice">Review &amp; save payee</button>
      </div>
    </article>`;
    ui.candidateList.querySelector(".candidate-edit").addEventListener("click", editCurrentRequest);
    ui.candidateList.querySelector(".candidate-choice").addEventListener("click", async () => {
      await approvePayeeAddition(addition);
    });
    return true;
  }
  if (result.outcome === "EXECUTED_PAYEE_ADDED") {
    setCategory("Payments");
    setOutcome("success", "✓", "Payee saved", `${result.payee.display_name} (${result.payee.masked_destination}) is now a payee.`,
      "Confirmed with your face and device. For 24 hours, payments to this new payee need a one-time code and are capped.");
    void loadPayees();
    return true;
  }
  if (result.outcome === "EXECUTED_VOICE_REGISTERED") {
    setCategory("Accounts");
    setOutcome("success", "✓", "Voice registered", "Your voice can now confirm payments under S$200.",
      "Approved with your face and device. With \"Hey Dimon\" on, say \"Yes, confirm\" in your own voice to approve. S$200 and above still needs Singpass and your face.");
    return true;
  }
  if (result.outcome === "CLARIFICATION_REQUEST" && result.reason_code === "PAYEE_NICKNAME_REQUIRED") {
    setCategory("Payments");
    setOutcome("clarification", "?", "Name this payee", result.question,
      "The number was checked in the PayNow directory. Nothing is saved until you confirm with your face.");
    ui.candidateList.hidden = false;
    ui.candidateList.innerHTML = `<form class="nickname-form">
      <input type="text" maxlength="40" minlength="3" required placeholder="e.g. Uncle Tan" aria-label="Payee name">
      <button type="submit" class="candidate-choice">Continue</button>
    </form>`;
    ui.candidateList.querySelector("form").addEventListener("submit", event => {
      event.preventDefault();
      const name = event.target.querySelector("input").value.trim();
      if (name) followUpCommand(`as ${name}`);
    });
    return true;
  }
  if (result.outcome === "CLARIFICATION_REQUEST" && result.reason_code === "MISSING_SOURCE_ACCOUNT"
      && Array.isArray(result.source_options) && result.source_options.length) {
    setCategory("Payments");
    setOutcome("clarification", "?", "Which account pays?", result.question,
      "You have more than one account, so the source is never guessed. Nothing has moved.");
    ui.candidateList.hidden = false;
    ui.candidateList.innerHTML = `<div class="source-choices">${result.source_options.map(option =>
      `<button type="button" class="candidate-choice" data-account="${escapeHtml(option.label)}">From ${escapeHtml(option.label)} · ${escapeHtml(formatSgd(option.balance_minor))}</button>`).join("")}</div>`;
    ui.candidateList.querySelectorAll("[data-account]").forEach(button => {
      button.addEventListener("click", () => followUpCommand(`from ${button.dataset.account}`));
    });
    return true;
  }
  return false;
}

// ---------- Topbar: Singpass authorisations waiting ----------
const topbarBell = document.querySelector("#topbar-bell");
const topbarBadge = document.querySelector("#topbar-bell-badge");

async function loadTopbar() {
  if (!topbarBell) return;
  const queue = await fetch("/api/singpass/pending", {cache: "no-store"})
    .then(r => r.ok ? r.json() : {pending: []}).catch(() => ({pending: []}));
  const waiting = queue.pending?.length || 0;
  topbarBadge.hidden = waiting === 0;
  topbarBadge.textContent = String(waiting);
  topbarBell.setAttribute("aria-label", waiting
    ? `${waiting} Singpass authorisation${waiting === 1 ? "" : "s"} waiting`
    : "No Singpass authorisations waiting");
}

topbarBell?.addEventListener("click", () => {
  window.open("/mock-singpass", "dimon-mock-singpass", "noopener");
});

// ---------- Log out = lock. The server refuses every account route until
// the face registered at KYC unlocks it, so a reload cannot skip this. ----------
const lockScreen = document.querySelector("#lock-screen");
const lockVideo = document.querySelector("#lock-face-video");
const lockPrompt = document.querySelector("#lock-prompt");
const unlockButton = document.querySelector("#unlock-button");
let closeLockCamera = null;

function showLockScreen() {
  wakeWord?.stop?.("Dimon is locked.");
  wakeWord = null;
  stopReplyAudio({keepReview: false});
  if (voiceSocket && voiceSocket.readyState < WebSocket.CLOSING) voiceSocket.close();
  voiceResumeRequested = false;
  voiceTerminal = true;
  voiceFinishing = false;
  voiceDeadline.clear();
  setVoicePhase("idle");
  void releaseVoiceCapture();
  lockScreen.hidden = false;
  lockPrompt.className = "face-prompt";
  lockPrompt.textContent = "";
  unlockButton.disabled = false;
  unlockButton.focus();
}

document.querySelector("#logout-button")?.addEventListener("click", async () => {
  const response = await fetch("/api/session/lock", {method: "POST"});
  if (response.ok) {
    showLockScreen();
    return;
  }
  const payload = await response.json().catch(() => ({}));
  activateView(document, "ask");
  if (payload.detail === "FACE_NOT_REGISTERED") {
    setOutcome("clarification", "!", "Log out", "Verify your identity first",
      "Logging out locks Dimon until your registered face unlocks it. Complete the one-time identity check to register your face, then you can log out.");
  } else {
    setOutcome("failed", "×", "Log out", "Could not log out", payload.detail || `HTTP ${response.status}`);
  }
});

unlockButton?.addEventListener("click", async () => {
  unlockButton.disabled = true;
  lockPrompt.className = "face-prompt";
  try {
    if (!closeLockCamera) {
      lockVideo.hidden = false;
      lockPrompt.textContent = "Starting the camera…";
      closeLockCamera = await openCamera(lockVideo);
    }
    const challenge = await postFrames("/api/session/unlock-challenge", {});
    const frames = await captureHeadTurn(lockVideo, {onStep: (_index, prompt) => { lockPrompt.textContent = prompt; }});
    lockPrompt.textContent = "Checking your face…";
    await postFrames("/api/session/unlock", {challenge_id: challenge.challenge_id, frames});
    closeLockCamera();
    closeLockCamera = null;
    lockPrompt.className = "face-prompt ok";
    lockPrompt.textContent = "Unlocked.";
    window.location.reload();
  } catch (error) {
    lockPrompt.className = "face-prompt bad";
    lockPrompt.textContent = faceMessages[error.reasonCode] || error.message;
    unlockButton.textContent = "Try again";
    unlockButton.disabled = false;
  }
});

// ---------- Hands-free "Hey Dimon" ----------
// Hands-free reuses the same rendering, signing and approval paths as the
// screen, so a spoken approval is checked exactly like a tapped one.
const approvalSubmitters = {
  "receipts": requestReceiptApproval,
  "batch-receipts": requestBatchReceiptApproval,
  "payee-additions": requestPayeeAdditionApproval,
  "voice-enrollments": (receipt, signed) => requestSignedJson(
    `/api/voice-enrollments/${encodeURIComponent(receipt.receipt_id)}/approve`, receipt, signed),
};

function showVoicePhrase(phrase, summary) {
  if (!phrase) return;
  activateView(document, "ask", {focus: false});
  setOutcome("clarification", "♪", "Confirm by voice", `“${phrase}”`,
    `Say “${phrase}” to confirm: ${summary}. Your voice must match your registered voiceprint, and this device signs the approval. Say “no” to cancel.`);
  ui.outcomeTitle.classList.add("voice-phrase");
}

// ---------- One-time identity check, on its own ----------
// Mock Singpass Myinfo, face registration and this device's key, without
// starting a payment. Payments run the same check when it is missing.
const verifyIdentityButton = document.querySelector("#verify-identity-button");
const verifyIdentityNote = document.querySelector("#verify-identity-note");
const identityStatusLabel = document.querySelector("#identity-status-label");

async function refreshIdentityStatus() {
  if (!verifyIdentityButton) return;
  const kyc = await fetch("/api/kyc", {cache: "no-store"}).then(r => r.ok ? r.json() : {}).catch(() => ({}));
  const verified = kyc.kyc_status === "VERIFIED" && kyc.face_registered;
  verifyIdentityButton.dataset.verified = String(verified);
  verifyIdentityButton.disabled = verified;
  verifyIdentityButton.lastElementChild.textContent = verified ? "Verified" : "Verify";
  identityStatusLabel.textContent = verified
    ? `${kyc.kyc_name ? `${kyc.kyc_name} · ` : ""}Identity verified`
    : "Identity not verified";
  verifyIdentityNote.hidden = true;
}

async function verifyIdentity() {
  await ensureSigningCredential();
  await refreshIdentityStatus();
}

verifyIdentityButton?.addEventListener("click", async () => {
  verifyIdentityButton.disabled = true;
  try {
    await verifyIdentity();
  } catch (error) {
    verifyIdentityNote.hidden = false;
    verifyIdentityNote.textContent = error?.reasonCode === "KYC_NOT_COMPLETED"
      ? "Verification cancelled. Nothing was saved."
      : `Verification didn't finish: ${error?.message || "try again"}.`;
  } finally {
    verifyIdentityButton.disabled = verifyIdentityButton.dataset.verified === "true";
  }
});
const WAKE_GREETING = "Hi, I'm Dimon. How can I help you?";

function nextAnimationFrame() {
  return new Promise(resolve => requestAnimationFrame(() => resolve()));
}

function afterConversation() {
  if (!wakeWord.isBusy()) setAssistantMode(pendingOnScreenReview ? "review" : "idle", "Say “Hey Dimon” or tap Speak when you need me");
  syncWakeWord();
}

function startHandsFree() {
  if (wakeWord) return;
  wakeWord = initHandsFree({
    autoStart: true,
  playSpeechBlob: playHandsFreeSpeechBlob,
  onReadiness: readiness => {
    handsFreeReadiness = readiness;
    renderPreparation(lastPreparationSnapshot);
  },
  // During a voice conversation, the Ask screen shows what Dimon is doing,
  // exactly as it does after a tap on Speak.
  onStatus: text => { if (ui.voiceStageStatus) ui.voiceStageStatus.textContent = text; },
  onMode: (mode, text) => {
    activateView(document, "ask", {focus: false});
    if (mode !== "listening") ui.voiceWaveform?.querySelectorAll("span").forEach(bar => bar.style.removeProperty("height"));
    setAssistantMode(mode, text);
    if (ui.voiceButton) ui.voiceButton.classList.toggle("recording", mode === "listening");
    if (mode === "listening" && ui.voiceLiveCaption) {
      ui.voiceLiveCaption.textContent = "I’m listening. Go ahead.";
      ui.voiceLiveCaption.classList.remove("provisional");
      ui.voiceLiveCaption.setAttribute("aria-live", "polite");
    }
  },
  onLevel: setVoiceLevel,
  onCaption: caption => {
    if (ui.voiceLiveCaption && caption?.text) {
      ui.voiceLiveCaption.textContent = caption.text;
      ui.voiceLiveCaption.classList.toggle("provisional", Boolean(caption.provisional));
      ui.voiceLiveCaption.setAttribute("aria-live", caption.ariaLive || "polite");
    }
  },
  isDimonAudible: dimonAudible,
  onWakeDetected() {
    activateView(document, "ask", {focus: false});
    setAssistantMode("understanding", "Heard “Hey Dimon” · listening for your request");
  },
  onWakeFailed() {
    activateView(document, "ask", {focus: false});
    setAssistantMode("idle", "Wake check ended · say “Hey Dimon” again or tap Speak");
  },
  // "Hey Dimon" opens Speak, after a short greeting.
  async onWake() {
    activateView(document, "ask", {focus: false});
    if (ui.resultAudio && !ui.resultAudio.paused) stopReplyAudio({keepReview: pendingOnScreenReview});
    const welcomeText = ui.assistantTranscript?.querySelector(".welcome-message p")?.textContent || "";
    if (shouldAppendTranscriptTurn("assistant", welcomeText, "assistant", WAKE_GREETING)) {
      appendAssistantMessage("assistant", WAKE_GREETING);
    }
    // Let the browser visibly open Ask before synthesis/playback starts.
    await nextAnimationFrame();
    await wakeWord.say("wake_greeting", {}, WAKE_GREETING);
    if (voicePhase === "idle" && !voiceFinishing) await startVoice();
    syncWakeWord();
  },
  // "Hey Dimon, <request>" in one breath: answered like a Speak request.
  async onCommand(result, text) {
    lastRequestWasSpoken = true;
    await wakeWord.converse(result, text);
    afterConversation();
  },
  formatSgd,
  verifyIdentity,
  renderResult: result => {
    activateView(document, "ask", {focus: false});
    renderResult(result, 0);
  },
  // What the user said joins the Ask conversation; Dimon's spoken prompts
  // show in the live caption, since the results themselves are in the thread.
  note: (who, text) => {
    if (who === "Dimon") {
      if (ui.voiceLiveCaption) ui.voiceLiveCaption.textContent = text;
    } else {
      appendAssistantMessage("user", text);
      if (ui.voiceLiveCaption) {
        ui.voiceLiveCaption.textContent = text;
        ui.voiceLiveCaption.classList.remove("provisional");
        ui.voiceLiveCaption.setAttribute("aria-live", "polite");
      }
    }
  },
  async runRequest(text) {
    const response = await requestAction({requestId: crypto.randomUUID(), text});
    return response.kind === "RESULT" ? response.result : null;
  },
  approveOrder,
  approvePaperOrder: order => approvePaperOrder(order, {announcementOwner: "hands_free"}),
  conversationTurn,
  approvePayeeAddition,
  screenApproval: (collection, receipt, options = {}) => signedApproval(
    collection, receipt, approvalSubmitters[collection],
    "Retry this exact approval to retrieve its stored result.",
    options,
  ),
  submitApproval: (collection, receipt, signed) => approvalSubmitters[collection](receipt, signed),
  ensureSigningCredential,
  deviceApprover,
  wireAssertion,
  showPhrase: showVoicePhrase,
  });
  wakeWord.setConversationContext(activeConversation);
  if (pendingVoiceTraceId) {
    wakeWord.beginTrace(pendingVoiceTraceId);
    pendingVoiceTraceId = null;
  }
}

document.querySelector("#invest-profile-button")?.addEventListener("click", async () => {
  const overview = await fetch("/api/invest", {cache: "no-store"}).then(r => r.json());
  if (await openProfileDialog(overview.questions)) await loadInvestments();
});
function startUnlockedShell() {
  startHandsFree();
  void refreshIdentityStatus();
  void loadInvestments();
  void loadTopbar();
}

loadSession().then(session => {
  // A locked account answers nothing else until the face unlocks it.
  if (session?.locked) {
    showLockScreen();
    return;
  }
  startUnlockedShell();
  return Promise.all([loadState(), loadPayees()]);
}).then(() => {
  document.dispatchEvent(new Event("dimon:boot-complete"));
}).catch(error => {
  recordEvidenceDiagnostic("application_startup", error);
  ui.healthText.textContent = "Connection unavailable";
  setOutcome("failed", "×", "Dimon could not start", "Account details could not be loaded.", "Reload this page to try again.");
});
