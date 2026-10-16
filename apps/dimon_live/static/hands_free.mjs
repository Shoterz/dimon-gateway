// Hands-free Dimon: say "Hey Dimon, ..." and it does the task by voice.
//
// The browser streams the microphone to /api/voice/hands-free. The server
// ignores everything that does not start with "Hey Dimon" (and, once a
// voiceprint is registered, anything not in the account holder's voice).
// After a request, Dimon speaks the result and asks any follow-up out loud:
// which account, which payee, a name.
//
// How a transaction is confirmed (the server enforces every rule):
//   - Transfers under S$200 with a registered voice: say "Yes Confirm". The
//     voiceprint must match, and this browser's voice key signs on its own.
//   - Everything else falls back to the face on screen: S$200 and up, new
//     payees, or when the voice doesn't match. Identity is verified once with
//     Singpass (Myinfo) before the first approval.
//   - Every investment: Singpass and the face, every time.
// While a confirmation is open, "Hey Dimon, cancel" closes it and "Hey Dimon,
// continue" presses its main button; any other request waits its turn.
//
// On the Ask page "Hey Dimon" is a wake word for the Speak button: it is
// listened for in the background, and when heard the page greets the user
// and opens Speak (hooks.onWake). "Hey Dimon, <request>" in one breath goes
// straight to the page as a spoken request (hooks.onCommand). The wake
// listener pauses while Speak or a reply is using the microphone.

import { createVoiceAudioContext, VoiceFrameResampler } from "./voice_capture.mjs";
import {IDENTITY_VERIFICATION_FAILURE_MESSAGE, isIdentityVerificationFailure} from "./identity_feedback.mjs";
import {RollingAudioPrebuffer} from "./audio_prebuffer.mjs";

const REPLY_TIMEOUT_MS = 12_000;
const CONFIRM_PHRASE = "Yes Confirm";
const WAKE_GREETING_AUDIO_URL = "/static/audio/wake-greeting-v1.wav";
const VOICE_ENROLLMENT_PROMPT_AUDIO_URL = "/static/audio/voice-enrollment-confirmation-v1.wav";
// Each on-screen confirmation, with its cancel and main buttons.
const DIALOGS = [
  ["#approval-dialog", "#approval-cancel", "#approval-confirm"],
  ["#singpass-dialog", "#singpass-cancel", "#singpass-open"],
  ["#kyc-dialog", "#kyc-cancel", "#kyc-next"],
  ["#profile-dialog", "#profile-cancel", "#profile-save"],
];

const RECIPIENT_MATCH_REASONS = new Set([
  "CLOSE_RECIPIENT_MATCH",
  "AMBIGUOUS_RECIPIENT",
  "PHONETIC_RECIPIENT_MATCH",
]);

export function beginWarmupFromGesture(prepareVoice, warmBackend) {
  const voice = prepareVoice();
  const backend = warmBackend();
  return Promise.all([voice, backend]).then(([voiceResult, backendResult]) => ({
    voice: voiceResult,
    backend: backendResult,
  }));
}

export async function beginResetWarmupFromGesture(unlockVoice, resetBackend, prepareVoice) {
  const unlocked = Promise.resolve(unlockVoice());
  const backend = await resetBackend();
  const unlockedVoice = await unlocked;
  if (backend?.state !== "READY") return {voice: unlockedVoice, backend};
  return {voice: await prepareVoice(), backend};
}

export function createSingleFlight(operation) {
  let current = null;
  return function runSingleFlight(...args) {
    if (current) return current;
    current = Promise.resolve().then(() => operation(...args));
    current = current.finally(() => { current = null; });
    return current;
  };
}

const ACTIVE_VOICE_PRESENTATIONS = {
  listening: {
    state: "ready",
    label: "Dimon is listening",
    status: "Listening",
    active: true,
  },
  understanding: {
    state: "ready",
    label: "Dimon is processing your request",
    status: "Processing request",
    active: true,
  },
  speaking: {
    state: "ready",
    label: "Dimon is speaking",
    status: "Speaking",
    active: true,
  },
};

export function combinedWarmPresentation(backend = {}, voice = {}, activeVoiceState = "idle") {
  const backendState = backend.state || "IDLE";
  if (backendState === "FAILED" || backendState === "DEGRADED" || voice.state === "FAILED") {
    return {state: "degraded", label: "Dimon needs attention", status: voice.error || "Services need attention"};
  }
  if (ACTIVE_VOICE_PRESENTATIONS[activeVoiceState]) {
    return ACTIVE_VOICE_PRESENTATIONS[activeVoiceState];
  }
  if (backendState === "PREPARING") {
    return {state: "preparing", label: "Dimon is warming up", status: "Warming services"};
  }
  if (backendState === "READY" && voice.audio && voice.microphone && voice.socket) {
    return {state: "ready", label: "Dimon is ready", status: "Ready — say “Hey Dimon”"};
  }
  if (backendState === "READY" && voice.needsGesture) {
    return {state: "cold", label: "Start Dimon’s microphone", status: "Tap D to start microphone"};
  }
  if (backendState === "READY" && voice.state !== "IDLE") {
    return {state: "preparing", label: "Dimon is starting its microphone", status: "Starting microphone"};
  }
  return {state: "cold", label: "Warm Dimon services", status: ""};
}

export function blockedResultSpeech(result = {}) {
  if (result.reason_code === "PROMPT_INJECTION_BLOCKED") {
    return {
      promptKey: "prompt_injection_blocked",
      text: "I couldn’t do that because prompt injection was blocked. Nothing was sent.",
    };
  }
  return {promptKey: "check_screen", text: null};
}

export function recipientClarificationPlan(result) {
  if (result?.outcome !== "CLARIFICATION_REQUEST"
      || !RECIPIENT_MATCH_REASONS.has(result.reason_code)) return null;
  const names = (result.receipt_choices || [])
    .map(choice => choice?.recipient_display_name)
    .filter(name => typeof name === "string" && name.trim());
  if (!names.length || typeof result.question !== "string" || !result.question.trim()) return null;
  return {
    mode: names.length === 1 ? "confirm" : "choice",
    question: result.question.trim(),
    names,
  };
}

const RECIPIENT_SOCIAL = new Set([
  "thank you", "thanks", "thanks dimon", "youre welcome", "welcome",
  "how are you", "how was your day", "hello", "hi",
]);
const RECIPIENT_CANCEL = new Set(["cancel", "never mind", "nevermind", "stop", "forget it"]);

export function classifyRecipientReply(text) {
  const value = typeof text === "string" ? text.trim() : "";
  const normalized = value.toLowerCase().replace(/[’']/g, "").replace(/[^a-z0-9]+/g, " ").trim();
  if (RECIPIENT_CANCEL.has(normalized)) return {kind: "cancel"};
  if (RECIPIENT_SOCIAL.has(normalized)
      || /^(thank|thanks|hello|hi|hey|welcome|youre welcome|how are|how was|good (morning|afternoon|evening))\b/.test(normalized)) {
    return {kind: "social"};
  }
  const words = value.match(/[A-Za-z][A-Za-z'’-]*/g) || [];
  if (words.length >= 2 && words.length <= 4 && words.join(" ").length === value.length) {
    return {kind: "recipient", value};
  }
  return {kind: "unclear"};
}

export function isExplicitBillRequest(text) {
  const normalized = typeof text === "string" ? text.toLowerCase() : "";
  return /\b(?:pay|settle)\b/.test(normalized)
    && /\b(?:bills?|electricity|utilities?|phone|mobile|internet|insurance|premium|credit\s+card|tax|tuition)\b/.test(normalized);
}

export function shouldAppendTranscriptTurn(lastRole, lastText, role, text) {
  if (role !== "assistant" || lastRole !== "assistant") return true;
  if (lastText === text) return false;
  const openingMessages = new Set([
    "I can help with your accounts, payments, and investments. What would you like to do?",
    "Hi, I'm Dimon. How can I help you?",
  ]);
  return !(openingMessages.has(lastText) && openingMessages.has(text));
}

export function voiceCaptionPresentation(type, text) {
  const value = typeof text === "string" ? text.trim() : "";
  if (type === "transcript_partial") {
    return {text: value ? `Hearing: “${value}”` : "Hearing…", provisional: true, ariaLive: "off"};
  }
  return {text: value, provisional: false, ariaLive: "polite"};
}

export function demoExchangeDisclosure(evidence) {
  if (!evidence?.demo_override) return null;
  const state = String(evidence.actual_market_state || "unavailable").toLowerCase();
  return `Simulated fill using the latest timestamped quote · Nasdaq was ${state} · no real money`;
}

export function returnToWakeListening({wanted, setPaused, sendSleep, setStatus, idleStatus}) {
  // A completed command was paused when its action result arrived. Reopen the
  // local microphone gate as well as putting the server back into wake mode.
  setPaused(false);
  sendSleep();
  if (wanted) setStatus(idleStatus);
}

export function conversationContextMessage(conversation) {
  if (conversation?.status === "WAITING"
      && typeof conversation.conversation_id === "string"
      && Number.isInteger(conversation.version)
      && conversation.version >= 1) {
    return {
      type: "conversation_context",
      conversation_id: conversation.conversation_id,
      expected_version: conversation.version,
    };
  }
  return {type: "conversation_context", conversation_id: null, expected_version: null};
}

export function wakeConfirmationTransition(pending, message = {}) {
  const type = message.type;
  const path = message.path;
  if (type === "wake" && path === "nemotron_partial") {
    return {pending: true, recover: false, resume: false};
  }
  if (type === "wake_complete" || type === "action_result"
      || (type === "wake" && path !== "nemotron_partial")) {
    return {pending: false, recover: false, resume: false};
  }
  if (pending && ["error", "not_woken", "ignored", "closed"].includes(type)) {
    return {pending: false, recover: true, resume: true};
  }
  return {pending: Boolean(pending), recover: false, resume: false};
}

export function initHandsFree(hooks) {
  const panel = document.querySelector("#hands-free-panel");
  if (!panel) return;
  const toggle = panel.querySelector("#hands-free-button");
  const status = panel.querySelector("#hands-free-status");
  const log = panel.querySelector("#hands-free-log");
  const registerButton = panel.querySelector("#voice-register-button");
  const voiceNote = panel.querySelector("#voice-register-note");

  let wanted = false;            // the user turned hands-free on
  let socket = null;
  let socketReady = false;
  let boundConversation = null;
  const startupAudio = new RollingAudioPrebuffer(16_000 * 4);
  let retries = 0;
  let context = null;
  let stream = null;
  let node = null;
  let resampler = null;
  let speaking = false;          // Dimon is talking: the microphone is not sent
  let spokenAbortController = null;
  let wakeGreetingBlobPromise = null;
  let voiceEnrollmentPromptBlobPromise = null;
  let pushToTalkYielded = false;
  let voiceprint = false;
  let voiceKeyId = null;
  let voiceMaxMinor = 20_000;
  let waiter = null;             // {types, resolve}
  let busy = false;
  let paused = false;            // Speak or a reply owns the microphone
  let needsTap = false;          // the browser won't start audio before a tap
  let startPromise = null;
  let readinessFailure = null;
  const readiness = {audio: false, microphone: false, socket: false};
  let releaseLeadership = null;
  let leadershipTask = null;
  const waiting = [];            // requests heard while busy, handled in order
  let activeTrace = null;        // IDs and monotonic timings only; never voice content
  let activeAttemptId = null;
  let pendingWakeConfirmation = false;
  const wakeReceivedAttempts = new Set();

  const formatSgd = hooks.formatSgd;
  const deviceApprover = hooks.deviceApprover;

  function traceElapsed() {
    return activeTrace ? Math.max(0, performance.now() - activeTrace.startedAt) : 0;
  }

  function flushTraceEvents() {
    if (!activeTrace?.bound || socket?.readyState !== WebSocket.OPEN) return;
    for (const event of activeTrace.events.splice(0)) {
      socket.send(JSON.stringify({
        type: "trace_event",
        trace_id: activeTrace.id,
        ...event,
      }));
    }
  }

  function recordTrace(event, attemptId = null) {
    if (!activeTrace) return;
    const item = {event, elapsed_ms: Math.round(traceElapsed() * 1000) / 1000};
    if (attemptId) item.attempt_id = attemptId;
    activeTrace.events.push(item);
    if (activeTrace.events.length > 32) activeTrace.events.shift();
    flushTraceEvents();
  }

  function bindTrace() {
    if (!activeTrace || !socketReady || socket?.readyState !== WebSocket.OPEN) return;
    activeTrace.bound = false;
    socket.send(JSON.stringify({type: "trace_start", trace_id: activeTrace.id}));
  }

  function beginTrace(traceId = crypto.randomUUID()) {
    activeTrace = {
      id: traceId,
      startedAt: performance.now(),
      bound: false,
      backendReady: false,
      events: [],
    };
    activeAttemptId = null;
    wakeReceivedAttempts.clear();
    recordTrace("d_click");
    if (readiness.audio) recordTrace("audio_ready");
    if (readiness.microphone) recordTrace("microphone_ready");
    if (socket?.readyState === WebSocket.OPEN) recordTrace("socket_ready");
    bindTrace();
    return traceId;
  }

  function markBackendReady(traceId = null) {
    if (!activeTrace || (traceId && activeTrace.id !== traceId) || activeTrace.backendReady) return;
    activeTrace.backendReady = true;
    recordTrace("backend_ready");
  }

  function readinessSnapshot() {
    const ready = readiness.audio && readiness.microphone && readiness.socket;
    return {
      ...readiness,
      state: readinessFailure ? "FAILED" : ready ? "READY" : wanted ? "STARTING" : "IDLE",
      error: readinessFailure,
      needsGesture: needsTap,
    };
  }

  function publishReadiness() {
    const snapshot = readinessSnapshot();
    hooks.onReadiness?.(snapshot);
    return snapshot;
  }

  async function acquireLeadership() {
    if (!navigator.locks?.request) return true;
    let report;
    const acquired = new Promise(resolve => { report = resolve; });
    leadershipTask = navigator.locks.request("dimon-hands-free-listener", {ifAvailable: true}, async lock => {
      report(Boolean(lock));
      if (!lock) return;
      await new Promise(resolve => { releaseLeadership = resolve; });
    });
    return acquired;
  }

  let statusTimer = null;
  function setStatus(text) {
    clearTimeout(statusTimer);
    status.textContent = text;
    if (busy) hooks.onStatus?.(text);
  }
  const idleText = () => (needsTap ? `Tap anywhere to start listening for "Hey Dimon"`
    : `Listening for "Hey Dimon"${voiceprint ? " · voice match on" : ""}`);
  // A short notice, then back to the idle prompt unless something else happened.
  function flashStatus(text, ms = 2500) {
    setStatus(text);
    statusTimer = setTimeout(() => { if (!busy && !waiter) status.textContent = paused ? "Paused while Dimon is busy" : idleText(); }, ms);
  }

  function note(who, text) {
    // The page may show the conversation itself (the Ask thread).
    if (hooks.note) { hooks.note(who, text); return; }
    const line = document.createElement("p");
    line.className = `assistant-line ${who === "Dimon" ? "dimon" : "you"}`;
    const label = document.createElement("span");
    label.textContent = who;
    line.append(label, document.createTextNode(text));
    log.append(line);
    log.hidden = false;
    while (log.childElementCount > 40) log.firstElementChild.remove();
    log.scrollTop = log.scrollHeight;
  }

  const reason = code => String(code || "").replaceAll("_", " ").toLowerCase();

  // ------------------------------------------------------------ speaking
  function stopSpeaking() {
    spokenAbortController?.abort();
    spokenAbortController = null;
  }

  async function fetchSpeechArtifact(artifactId) {
    for (let attempt = 0; attempt < 60; attempt += 1) {
      const response = await fetch(`/api/audio/${encodeURIComponent(artifactId)}`, {cache: "no-store"});
      if (response.status === 202) {
        await new Promise(resolve => setTimeout(resolve, 250));
        continue;
      }
      if (!response.ok) return null;
      return response.blob();
    }
    return null;
  }

  function preloadWakeGreeting() {
    if (!wakeGreetingBlobPromise) {
      wakeGreetingBlobPromise = fetch(WAKE_GREETING_AUDIO_URL, {cache: "force-cache"})
        .then(response => response.ok ? response.blob() : null)
        .catch(() => null);
    }
    return wakeGreetingBlobPromise;
  }

  function preloadVoiceEnrollmentPrompt() {
    if (!voiceEnrollmentPromptBlobPromise) {
      voiceEnrollmentPromptBlobPromise = fetch(VOICE_ENROLLMENT_PROMPT_AUDIO_URL, {cache: "force-cache"})
        .then(response => response.ok ? response.blob() : null)
        .catch(() => null);
    }
    return voiceEnrollmentPromptBlobPromise;
  }

  async function playSpeechBlob(blob) {
    stopSpeaking();
    const controller = new AbortController();
    spokenAbortController = controller;
    speaking = true;
    hooks.onMode?.("speaking", "Dimon is speaking");
    let result = "failed";
    const attemptId = activeAttemptId;
    const traceId = activeTrace?.id || null;
    const recordPlayback = event => {
      if (traceId && activeTrace?.id === traceId) recordTrace(event, attemptId);
    };
    try {
      result = await hooks.playSpeechBlob?.(blob, {
        signal: controller.signal,
        onReady: () => recordPlayback("reply_audio_ready"),
        onStarted: () => recordPlayback("reply_playback_started"),
      }) || "failed";
    } finally {
      if (spokenAbortController === controller) spokenAbortController = null;
      speaking = false;
    }
    if (result === "ended") await new Promise(resolve => setTimeout(resolve, 250));
    return result;
  }

  async function say(promptKey, values = {}, textForDisplay = "", audioArtifactId = null) {
    const display = textForDisplay || "Please check the screen.";
    note("Dimon", display);
    let artifactId = audioArtifactId;
    try {
      if (promptKey === "wake_greeting") {
        const cachedGreeting = await preloadWakeGreeting();
        if (cachedGreeting) {
          await playSpeechBlob(cachedGreeting);
          return;
        }
      }
      if (promptKey === "enrollment_prompt" && values.index === 0) {
        const cachedEnrollmentPrompt = await preloadVoiceEnrollmentPrompt();
        if (cachedEnrollmentPrompt) {
          await playSpeechBlob(cachedEnrollmentPrompt);
          return;
        }
      }
      if (!artifactId) {
        const response = await fetch("/api/speech/prompts", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({prompt_key: promptKey, values}),
        });
        if (!response.ok) return;
        artifactId = (await response.json()).audio_artifact_id;
      }
      if (!artifactId) return;
      const blob = await fetchSpeechArtifact(artifactId);
      if (!blob) return;
      await playSpeechBlob(blob);
    } catch (_error) {
      speaking = false;
    }
  }

  function sayResult(result, fallbackKey = "check_screen", textForDisplay = "Please check the screen.") {
    const artifactId = result ? result.audio_artifact_id : null;
    return say(fallbackKey, {}, textForDisplay, artifactId);
  }

  // ------------------------------------------------------------ socket
  function send(message) {
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
  }

  function setConversationContext(conversation) {
    boundConversation = conversation?.status === "WAITING" ? {...conversation} : null;
    if (socketReady) send(conversationContextMessage(boundConversation));
  }

  function waitFor(types, timeoutMs = REPLY_TIMEOUT_MS) {
    return new Promise(resolve => {
      const timer = setTimeout(() => { waiter = null; resolve(null); }, timeoutMs);
      waiter = {types, resolve: value => { clearTimeout(timer); waiter = null; resolve(value); }};
    });
  }

  // A short tone: "your turn". The microphone ignores it like Dimon's voice.
  async function chime() {
    if (!context || context.state !== "running") return;
    speaking = true;
    try {
      const tone = context.createOscillator();
      const gain = context.createGain();
      tone.frequency.value = 880;
      gain.gain.setValueAtTime(0.0001, context.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.12, context.currentTime + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, context.currentTime + 0.16);
      tone.connect(gain).connect(context.destination);
      tone.start();
      tone.stop(context.currentTime + 0.18);
      await new Promise(resolve => setTimeout(resolve, 230));
    } finally {
      speaking = false;
    }
  }

  async function listen(expect, extra = {}) {
    await chime();
    send({type: "listen", expect, ...extra});
    const prompt = expect === "approval" ? `Say “${CONFIRM_PHRASE}” to approve` : "Listening for your answer…";
    setStatus(prompt);
    // Same listening screen as Speak: state, waveform, button and caption.
    hooks.onMode?.("listening", prompt);
    const message = await waitFor(["reply", "voice_verified", "voice_rejected", "enroll_sample",
                                   "enroll_rejected", "action_result", "error", "cancelled"]);
    hooks.onMode?.("understanding", expect === "approval" ? "Checking your voice…" : "I heard you · working on it");
    setStatus(busy ? "Working on it…" : idleText());
    return message;
  }

  // Tapping the button while Dimon waits for an answer stops waiting.
  function cancelListen() {
    if (waiter) waiter.resolve({type: "cancelled"});
  }

  function openDialog() {
    for (const [dialog, cancel, primary] of DIALOGS) {
      const element = document.querySelector(dialog);
      if (element?.open) return {element, cancel: document.querySelector(cancel), primary: document.querySelector(primary)};
    }
    return null;
  }

  // "Hey Dimon, cancel" / "Hey Dimon, continue": steer whatever is open.
  function steer(action) {
    const dialog = openDialog();
    if (dialog) {
      const button = action === "cancel" ? dialog.cancel : dialog.primary;
      if (button && !button.disabled) {
        note("Dimon", action === "cancel" ? "Cancelled." : "Okay.");
        button.click();
      } else {
        note("Dimon", "That isn't ready yet. Finish the step on screen.");
      }
      return;
    }
    if (waiter && action === "cancel") { waiter.resolve({type: "cancelled"}); return; }
    void say(
      action === "cancel" ? "nothing_to_cancel" : "nothing_waiting",
      {},
      action === "cancel" ? "There's nothing to cancel." : "There's nothing waiting to be confirmed.",
    );
  }

  function onMessage(event) {
    const message = JSON.parse(event.data);
    if (message.type === "trace_bound") {
      if (activeTrace?.id === message.trace_id) {
        activeTrace.bound = true;
        flushTraceEvents();
      }
      return;
    }
    if (message.type === "ready") {
      socketReady = true;
      readiness.socket = true;
      readinessFailure = null;
      publishReadiness();
      send(conversationContextMessage(boundConversation));
      bindTrace();
      flushStartupAudio(socket);
      voiceprint = Boolean(message.voiceprint);
      voiceMaxMinor = message.voice_max_minor || voiceMaxMinor;
      setStatus(busy ? "Working on it…" : idleText());
      if (waiter?.types.includes("ready")) waiter.resolve(message);
      return;
    }
    if (message.type === "speech_started") {
      activeAttemptId = message.attempt_id || null;
    }
    if ((message.type === "wake" || message.type === "wake_complete") && message.attempt_id) {
      activeAttemptId = message.attempt_id;
      if (!wakeReceivedAttempts.has(message.attempt_id)) {
        wakeReceivedAttempts.add(message.attempt_id);
        recordTrace("browser_wake_received", message.attempt_id);
      }
    }
    const wakeTransition = wakeConfirmationTransition(pendingWakeConfirmation, message);
    pendingWakeConfirmation = wakeTransition.pending;
    if (wakeTransition.recover) {
      if (wakeTransition.resume) paused = false;
      startupAudio.clear();
      send({type: "sleep"});
      hooks.onWakeFailed?.(message);
      flashStatus("Wake check ended. Say Hey Dimon again or tap Speak.", 4000);
      return;
    }
    if (!busy && !waiter) {
      // Content-free progress, so it never looks dead while it listens.
      if (message.type === "speech_started") { setStatus("Hearing you…"); return; }
      if (message.type === "processing") { setStatus("Checking…"); return; }
      if (message.type === "not_woken") { flashStatus(`Didn't catch "Hey Dimon". Start with it, like "Hey Dimon, send…"`); return; }
    }
    if (message.type === "ignored") { flashStatus("That didn't sound like your registered voice, so it was ignored.", 4000); return; }
    if (message.type === "transcript_partial") {
      setStatus(message.text || "Listening…");
      hooks.onCaption?.(voiceCaptionPresentation(message.type, message.text));
      return;
    }
    if (message.type === "wake" && message.path === "nemotron_partial" && !busy) {
      // Open Ask immediately, but keep capturing until the authoritative final
      // distinguishes a bare wake from "Hey Dimon, <request>".
      setStatus("Heard Hey Dimon…");
      hooks.onWakeDetected?.();
      return;
    }
    if (message.type === "wake_complete" && hooks.onWake && !busy) {
      send({type: "sleep"});
      pause();
      void hooks.onWake();
      return;
    }
    if (message.type === "wake" && hooks.onWake && !busy) {
      if (message.command) {
        // The server runs the request next and sends action_result.
        setStatus("Working on it…");
        return;
      }
      send({type: "sleep"});
      pause();
      void hooks.onWake();
      return;
    }
    if (message.type === "wake") {
      setStatus("Listening…");
      if (!message.command && !busy) void say("wake_ack", {}, "Yes?");
      return;
    }
    if (message.type === "control") { note("You", message.text); steer(message.action); return; }
    if (message.type === "heard") note("You", message.text);
    if (message.type === "reply") note("You", message.text);
    if (waiter && waiter.types.includes(message.type)) { waiter.resolve(message); return; }
    if (message.type === "action_result" && hooks.onCommand && !busy) {
      pause();
      void hooks.onCommand(message.result, message.text);
      return;
    }
    if (message.type === "action_result") {
      if (busy) {
        waiting.push(message);
        note("Dimon", "Got it. I'll do that once this one is finished.");
      } else {
        void converse(message.result, message.text);
      }
      return;
    }
    if (message.type === "error") flashStatus(`Voice problem: ${reason(message.reason_code)}. Try again.`, 5000);
  }

  // ------------------------------------------------------------ microphone
  async function startAudio() {
    context ||= createVoiceAudioContext(window);
    if (context.state === "suspended") {
      if (navigator.userActivation && !navigator.userActivation.hasBeenActive) {
        // Started on page load: browsers keep audio off until the first tap.
        needsTap = true;
        const current = context;
        const wake = () => {
          window.removeEventListener("pointerdown", wake, true);
          window.removeEventListener("keydown", wake, true);
          current.resume().then(() => {
            needsTap = false;
            readiness.audio = current.state === "running";
            readinessFailure = null;
            publishReadiness();
            if (wanted && !busy && !paused) setStatus(idleText());
          }).catch(error => {
            readinessFailure = error?.name === "NotAllowedError" ? "Microphone permission was denied" : "Audio could not start";
            publishReadiness();
          });
        };
        window.addEventListener("pointerdown", wake, true);
        window.addEventListener("keydown", wake, true);
      } else {
        await context.resume();
      }
    }
    readiness.audio = context.state === "running";
    if (readiness.audio) recordTrace("audio_ready");
    publishReadiness();
    // Browsers may pause audio (another tab, a device change); pick it back up.
    context.onstatechange = () => {
      readiness.audio = context?.state === "running";
      publishReadiness();
      if (wanted && context?.state === "suspended") context.resume().catch(() => {});
    };
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true},
    });
    await context.audioWorklet.addModule("/static/voice_capture_worklet.js");
    resampler = new VoiceFrameResampler(context.sampleRate);
    const source = context.createMediaStreamSource(stream);
    node = new AudioWorkletNode(context, "dimon-voice-capture", {numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1]});
    const muted = context.createGain();
    muted.gain.value = 0;
    source.connect(node).connect(muted).connect(context.destination);
    readiness.microphone = true;
    recordTrace("microphone_ready");
    readinessFailure = null;
    publishReadiness();
    let audibleUntil = 0;
    node.port.onmessage = event => {
      // Never send Dimon's own voice (spoken replies and read-backs) as the
      // user's: it would be heard as a request, an answer,
      // or a voice-registration sample. A short tail covers the echo.
      const now = Date.now();
      if (speaking || hooks.isDimonAudible?.()) audibleUntil = now + 350;
      const dimonAudible = now < audibleUntil;
      if (waiter && !dimonAudible) hooks.onLevel?.(event.data);
      if (dimonAudible || (paused && !busy)) {
        startupAudio.clear();
        return;
      }
      for (const frame of resampler.push(event.data)) {
        if (!socketReady || socket?.readyState !== WebSocket.OPEN) startupAudio.push(frame.buffer);
        else socket.send(frame.buffer);
      }
    };
    // A headset unplugged or the microphone switched: restart on the new one.
    for (const track of stream.getAudioTracks()) {
      track.onended = () => {
        if (!wanted) return;
        readiness.microphone = false;
        publishReadiness();
        flashStatus("Microphone changed. Reconnecting…", 4000);
        stopAudio();
        setTimeout(() => { if (wanted) startAudio().catch(() => stop("The microphone isn't available.")); }, 800);
      };
    }
  }

  function stopAudio() {
    startupAudio.clear();
    node?.disconnect();
    stream?.getTracks().forEach(track => { track.onended = null; track.stop(); });
    if (context) context.onstatechange = null;
    context?.close().catch(() => {});
    node = stream = context = resampler = null;
    readiness.audio = false;
    readiness.microphone = false;
    publishReadiness();
  }

  function connect() {
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    const current = new WebSocket(`${scheme}//${location.host}/api/voice/hands-free`);
    socket = current;
    socketReady = false;
    startupAudio.clear();
    current.binaryType = "arraybuffer";
    current.onopen = () => {
      retries = 0;
      recordTrace("socket_ready");
    };
    current.onmessage = event => {
      const message = JSON.parse(event.data);
      if (message.type === "error" && message.reason_code === "APP_LOCKED") { stop("Dimon is locked."); return; }
      onMessage(event);
    };
    current.onclose = () => {
      if (socket !== current) return;
      const wakeTransition = wakeConfirmationTransition(
        pendingWakeConfirmation, {type: "closed"},
      );
      pendingWakeConfirmation = wakeTransition.pending;
      if (wakeTransition.recover) {
        if (wakeTransition.resume) paused = false;
        hooks.onWakeFailed?.({type: "closed"});
      }
      socket = null;
      socketReady = false;
      readiness.socket = false;
      publishReadiness();
      startupAudio.clear();
      waiter?.resolve(null);
      if (!wanted) return;
      // Dropped (sleep, network, the 30-minute limit): reconnect quietly.
      const delay = Math.min(5000, 400 * 2 ** retries);
      retries += 1;
      setStatus(retries > 3 ? "Can't reach Dimon. Still trying…" : "Reconnecting…");
      setTimeout(() => { if (wanted && !socket) connect(); }, delay);
    };
  }

  function flushStartupAudio(current) {
    if (current !== socket || !socketReady || current?.readyState !== WebSocket.OPEN) return;
    const durationMs = Math.min(1000, Math.round(startupAudio.byteLength / 64));
    const frames = startupAudio.drain();
    if (!frames.length) return;
    current.send(JSON.stringify({type: "prebuffer_flushed", duration_ms: durationMs}));
    for (const frame of frames) current.send(frame);
  }

  async function startInternal() {
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
      setStatus("This browser can't listen hands-free. Use typing or Speak request.");
      return;
    }
    try {
      if (!await acquireLeadership()) {
        setStatus("Hey Dimon is already listening in another tab.");
        return;
      }
      wanted = true;
      readinessFailure = null;
      publishReadiness();
      await startAudio();
      connect();
      toggle.setAttribute("aria-pressed", "true");
      if (!hooks.onWake) document.body.dataset.handsFree = "on";
      toggle.classList.add("recording");
      toggle.lastElementChild.textContent = "Turn off \"Hey Dimon\"";
      setStatus("Connecting…");
    } catch (error) {
      readinessFailure = error?.name === "NotAllowedError"
        ? "Microphone permission was denied"
        : "Couldn’t start the microphone";
      publishReadiness();
      stop(error?.name === "NotAllowedError"
        ? "Microphone permission was denied."
        : "Couldn't start hands-free listening.");
    }
  }

  function start() {
    if (startPromise) return startPromise;
    startPromise = startInternal().finally(() => { startPromise = null; });
    return startPromise;
  }

  function unlockAudioFromGesture() {
    context ||= createVoiceAudioContext(window);
    const current = context;
    const resumePromise = current.state === "suspended" ? current.resume() : Promise.resolve();
    readinessFailure = null;
    publishReadiness();
    return Promise.resolve(resumePromise).then(() => {
      if (context === current) {
        needsTap = false;
        readiness.audio = current.state === "running";
        if (readiness.audio) recordTrace("audio_ready");
        publishReadiness();
      }
      return readinessSnapshot();
    }).catch(error => {
      readinessFailure = error?.name === "NotAllowedError"
        ? "Microphone permission was denied"
        : "Audio could not start";
      publishReadiness();
      return readinessSnapshot();
    });
  }

  function prepareFromGesture() {
    wanted = true;
    readinessFailure = null;
    publishReadiness();
    return unlockAudioFromGesture().then(async () => {
      if (startPromise) await startPromise;
      else if (!node) await start();
      else if (!socket) connect();
      return readinessSnapshot();
    }).catch(error => {
      readinessFailure = error?.name === "NotAllowedError"
        ? "Microphone permission was denied"
        : "Audio could not start";
      publishReadiness();
      return readinessSnapshot();
    });
  }

  function stop(message = "Hands-free off") {
    wanted = false;
    pushToTalkYielded = false;
    toggle.setAttribute("aria-pressed", "false");
    delete document.body.dataset.handsFree;
    toggle.classList.remove("recording");
    toggle.lastElementChild.textContent = "Turn on \"Hey Dimon\"";
    send({type: "stop"});
    const closing = socket;
    socket = null;
    socketReady = false;
    readiness.socket = false;
    startupAudio.clear();
    closing?.close();
    stopAudio();
    stopSpeaking();
    waiting.length = 0;
    waiter?.resolve(null);
    releaseLeadership?.();
    releaseLeadership = null;
    leadershipTask = null;
    publishReadiness();
    setStatus(message);
  }

  function pause() {
    if (paused) return;
    paused = true;
    if (wanted && !busy) setStatus("Paused while Dimon is busy");
  }

  function resume() {
    if (!paused) return;
    paused = false;
    // Mid-conversation the server may be waiting for an answer or the
    // confirmation phrase; only an idle listener goes back to sleep.
    if (busy) return;
    send({type: "sleep"});
    if (wanted) setStatus(idleText());
  }

  async function pauseForPushToTalk() {
    if (pushToTalkYielded) return;
    pushToTalkYielded = true;
    paused = true;
    stopAudio();
    if (wanted && !busy) setStatus("Paused while Speak uses the microphone");
  }

  async function resumeAfterPushToTalk() {
    if (!pushToTalkYielded) return;
    pushToTalkYielded = false;
    paused = false;
    if (wanted && !context) {
      try {
        await startAudio();
      } catch (_error) {
        stop("Couldn't restore hands-free listening.");
        return;
      }
    }
    if (busy) return;
    send({type: "sleep"});
    if (wanted) setStatus(idleText());
  }

  // ------------------------------------------------------------ conversation
  function describeChoice(choice) {
    const from = choice.source_label ? ` from ${choice.source_label}` : "";
    return `${formatSgd(choice.amount_minor)} to ${choice.recipient_display_name}${from}`;
  }

  async function followUp(text, extra) {
    // The user's spoken answer is added to their own request, which is
    // checked again from the start; Dimon never fills it in by itself.
    const result = await hooks.runRequest(`${text.replace(/[.!\s]+$/, "")} ${extra}`);
    if (!result) return;
    hooks.renderResult(result);
    await handle(result, null);
  }

  async function converse(result, text) {
    if (!result) return;
    if (busy) { waiting.push({result, text}); return; }
    busy = true;
    // The conversation speaks for itself: the page's reply audio and its
    // automatic re-listening stand aside until it ends.
    document.body.dataset.handsFree = "on";
    try {
      hooks.renderResult(result);
      await handle(result, text);
    } catch (error) {
      console.error("hands-free conversation failed", error);
      await say("generic_error", {}, "Something went wrong there. Please check the screen.");
    } finally {
      busy = false;
      if (hooks.onWake) delete document.body.dataset.handsFree;
      returnToWakeListening({
        wanted,
        setPaused(value) { paused = value; },
        sendSleep() { send({type: "sleep"}); },
        setStatus,
        idleStatus: idleText(),
      });
    }
    const next = waiting.shift();
    if (next) void converse(next.result, next.text);
  }

  async function handle(result, text) {
    const outcome = result.outcome;
    if (outcome === "CLARIFICATION_REQUEST" && result.reason_code === "TRANSCRIPTION_DISAGREEMENT") {
      const liveText = result.transcription_disagreement?.live_text?.trim?.() || "";
      if (!liveText) {
        await say("did_not_catch", {}, "I didn't catch that.");
        return;
      }
      await say("transcription_disagreement", {live_text: liveText}, result.question);
      const reply = await listen("confirmation");
      const answer = String(reply?.text ?? reply?.value ?? "").toLowerCase().replace(/[^a-z ]/g, " ").trim();
      if (/^(yes|correct|right|that is right|use that)\b/.test(answer)) {
        const corrected = await hooks.runRequest(liveText);
        if (corrected) {
          hooks.renderResult(corrected);
          await handle(corrected, liveText);
        }
      } else {
        await say("did_not_catch", {}, "Please say the request again.");
      }
      return;
    }
    if (outcome === "MULTI_REQUEST") {
      await say("multi_requests", {count: result.steps.length}, `I heard ${result.steps.length} requests. Let's go through them.`);
      for (const [index, step] of result.steps.entries()) {
        // Long requests are on screen; reading them back only delays the answer.
        const label = step.text && step.text.length <= 60 ? `: ${step.text.replace(/[.!?\s]+$/, "")}` : ` of ${result.steps.length}`;
        await say("request_number", {index: index + 1, total: result.steps.length}, `Request ${index + 1}${label}.`);
        hooks.renderResult(step.result);
        await handle(step.result, step.text);
      }
      await say("all_done", {}, "That's everything.");
      return;
    }
    // Mixed requests the server planned as one group: each item keeps its
    // own receipt and approval, taken one at a time like a split request.
    if (outcome === "MULTI_ACTION_CONFIRMATION_REQUEST") {
      const items = (result.items || []).filter(item => item.result);
      await say("multi_actions", {count: items.length}, `I've prepared ${items.length} actions. Let's go through them.`);
      for (const [index, item] of items.entries()) {
        await say("action_number", {index: index + 1, total: items.length}, `Action ${index + 1} of ${items.length}.`);
        hooks.renderResult(item.result);
        await handle(item.result, item.evidence || null);
      }
      await say("all_done", {}, "That's everything.");
      return;
    }
    // A share purchase or sale asks a few questions before it is ready.
    if (result.conversation?.status === "WAITING" && hooks.conversationTurn) {
      await stockConversation(result, text);
      return;
    }
    if (outcome === "ORDER_CONFIRMATION_REQUEST" && result.paper_order) {
      await approveShares(result);
      return;
    }
    if (outcome === "ANSWERED") { await sayResult(result, "done", result.answer || "Done."); return; }
    if (outcome === "BLOCKED" || outcome === "FAILED") {
      const blockedSpeech = blockedResultSpeech(result);
      await sayResult(
        result,
        blockedSpeech.promptKey,
        blockedSpeech.text || result.question || result.warning || `I couldn't do that: ${reason(result.reason_code)}.`,
      );
      return;
    }
    if (outcome === "CLARIFICATION_REQUEST" && result.reason_code === "MISSING_SOURCE_ACCOUNT") {
      const labels = (result.source_options || []).map(option => option.label);
      await sayResult(result, "check_screen", result.question);
      const reply = await listen("account", {options: labels});
      if (reply?.value && text) await followUp(text, `from ${reply.value}`);
      else await say("leave_for_now", {}, "I didn't catch which account, so I've left it.");
      return;
    }
    if (outcome === "CLARIFICATION_REQUEST" && result.reason_code === "PAYEE_NICKNAME_REQUIRED") {
      await sayResult(result, "check_screen", result.question);
      const reply = await listen("name");
      if (reply?.value && text) await followUp(text, `as ${reply.value}`);
      else await say("leave_for_now", {}, "I didn't catch a name, so nothing was saved.");
      return;
    }
    if (outcome === "CLARIFICATION_REQUEST" && result.reason_code === "MISSING_AMOUNT") {
      await sayResult(result, "missing_amount", `${result.question || "How much?"} Please say the whole request again with the amount.`);
      return;
    }
    if (outcome === "CLARIFICATION_REQUEST" && result.reason_code === "INVESTOR_PROFILE_REQUIRED") {
      await say("investor_profile", {}, "Before your first investment, please answer the four questions on screen.");
      return;
    }
    if (outcome === "CLARIFICATION_REQUEST" && result.reason_code === "MISSING_RECIPIENT") {
      await sayResult(result, "missing_recipient", result.question || "Who would you like to pay? Tell me the recipient's name.");
      async function closeRecipient(promptKey, answer) {
        hooks.renderResult({outcome: "ANSWERED", reason_code: "CONVERSATIONAL", answer});
        await say(promptKey, {}, answer);
      }
      for (let attempt = 0; attempt < 2; attempt += 1) {
        const reply = await listen("name");
        if (reply?.type === "cancelled") {
          await closeRecipient("recipient_cancelled", "I've cancelled that payment request. Nothing was sent.");
          return;
        }
        const classified = classifyRecipientReply(reply?.text ?? reply?.value);
        const replyText = String(reply?.text ?? "").trim();
        if (isExplicitBillRequest(replyText)) {
          const corrected = await hooks.runRequest(replyText);
          if (corrected) {
            hooks.renderResult(corrected);
            await handle(corrected, replyText);
          }
          return;
        }
        if (classified.kind === "cancel") {
          await closeRecipient("recipient_cancelled", "I've cancelled that payment request. Nothing was sent.");
          return;
        }
        if (classified.kind === "social") {
          await closeRecipient("recipient_social_close", "You're welcome. I've cancelled that payment request. Nothing was sent.");
          return;
        }
        if (classified.kind === "recipient" && text) {
          await followUp(text, `to ${classified.value}`);
          return;
        }
        if (attempt === 0) {
          await say("recipient_again", {}, "I still need the recipient's name. For example, Alicia Powell. Nothing has been sent.");
        }
      }
      await closeRecipient("recipient_stopped", "I couldn't confirm a recipient, so I've cancelled that payment request. Nothing was sent.");
      return;
    }
    if (outcome === "CLARIFICATION_REQUEST" && result.reason_code === "BILLER_REQUIRED") {
      await sayResult(result, "check_screen", result.question || "Which bill would you like to pay?");
      const reply = await listen("answer");
      const replyText = String(reply?.text ?? "").trim();
      if (!replyText) {
        await say("leave_for_now", {}, "I didn't catch which bill, so nothing was paid.");
        return;
      }
      const request = isExplicitBillRequest(replyText) ? replyText : `Pay my ${replyText} bill`;
      const corrected = await hooks.runRequest(request);
      if (corrected) {
        hooks.renderResult(corrected);
        await handle(corrected, request);
      }
      return;
    }
    const recipientPlan = recipientClarificationPlan(result);
    if (recipientPlan) {
      let choices = result.receipt_choices;
      if (recipientPlan.mode === "confirm") {
        await sayResult(result, "check_screen", recipientPlan.question);
        const reply = await listen("confirm");
        if (reply?.value !== true) {
          await say("leave_for_now", {}, "I'll leave that one for now.");
          return;
        }
      } else {
        await sayResult(result, "choose_option", recipientPlan.question);
        let reply = await listen("choice", {options: recipientPlan.names});
        if ((reply?.value === null || reply?.value === undefined) && reply?.type !== "cancelled") {
          await say("choose_option_again", {}, `Sorry, which one: ${recipientPlan.names.join(" or ")}?`);
          reply = await listen("choice", {options: recipientPlan.names});
        }
        if (reply?.value === null || reply?.value === undefined) {
          await say("leave_for_now", {}, "I'll leave that one for now.");
          return;
        }
        choices = [choices[reply.value]];
      }
      const choice = choices[0];
      if (result.unusual_amount) {
        await faceApproval("receipts", choice, `${result.question} That's unusual for this payee.`);
        return;
      }
      await approve("receipts", choice, choice.amount_minor, `Send ${describeChoice(choice)}?`);
      return;
    }
    if (outcome === "CLARIFICATION_REQUEST" && (result.receipt_choices || []).length) {
      let choices = result.receipt_choices;
      if (choices.length > 1) {
        const names = choices.map(choice => choice.recipient_display_name);
        // The details (account endings, last paid) are on screen; spoken, a
        // short question gets a faster answer.
        await sayResult(result, "choose_option", names.length <= 3 ? `Did you mean ${names.join(" or ")}?` : (result.question || "Which one?"));
        let reply = await listen("choice", {options: names});
        if ((reply?.value === null || reply?.value === undefined) && reply?.type !== "cancelled") {
          await say("choose_option_again", {}, `Sorry, which one: ${names.join(" or ")}?`);
          reply = await listen("choice", {options: names});
        }
        if (reply?.value === null || reply?.value === undefined) { await say("leave_for_now", {}, "I'll leave that one for now."); return; }
        choices = [choices[reply.value]];
      }
      const choice = choices[0];
      if (result.unusual_amount) {
        await faceApproval("receipts", choice, `${result.question} That's unusual for this payee.`);
        return;
      }
      await approve("receipts", choice, choice.amount_minor, `Send ${describeChoice(choice)}?`);
      return;
    }
    if (outcome === "BATCH_CONFIRMATION_REQUEST") {
      const receipt = result.batch_receipt;
      const list = receipt.items.map(item => `${formatSgd(item.amount_minor)} to ${item.recipient_display_name}`).join(", ");
      await approve("batch-receipts", receipt, receipt.total_minor,
        `Send ${list}, ${formatSgd(receipt.total_minor)} in total from ${receipt.source_label}?`);
      return;
    }
    if (outcome === "ORDER_CONFIRMATION_REQUEST") {
      await sayResult(result, "investment_review", `${result.order.title}. Investments always need Singpass and your face. `
        + "Approve it in the Singpass app, then confirm with your face on screen. Say \"Hey Dimon, cancel\" to stop.");
      await announce(await hooks.approveOrder(result.order));
      return;
    }
    if (outcome === "PAYEE_ADDITION_REQUEST") {
      await faceApproval("payee-additions", result.payee_addition,
        `${result.question.replace("Confirm with your face.", "")} Adding a payee needs your face.`);
      return;
    }
    if (outcome?.startsWith("EXECUTED")) { await sayResult(result, "done", "Done."); return; }
    await sayResult(result, "check_screen", result.question || "Please check the screen.");
  }

  // ------------------------------------------------------------ shares
  // Buying or selling shares is a short conversation: how many, market or a
  // price limit, and for how long. Dimon asks, listens without the wake word,
  // and sends each answer to the same conversation; the server decides what
  // the words mean and when the order is ready. Nothing is bought or sold
  // until Singpass and the face approve the exact order.
  function withQuestion(answer, question) {
    if (!question) return answer || "";
    if (!answer) return question;
    return answer.includes(question) ? answer : `${answer} ${question}`;
  }

  // In "buy Tesla and buy 2 Apple shares" the second purchase replaces the
  // first one's open conversation. When its turn comes, an outdated one is
  // asked again from the user's own words.
  async function currentConversation(result, text) {
    const id = result.conversation?.conversation_id;
    try {
      const response = await fetch(`/api/conversations/${encodeURIComponent(id)}`, {cache: "no-store"});
      if (response.ok) {
        const body = await response.json();
        const latest = body.conversation || {};
        if (latest.status === "WAITING" && latest.version === result.conversation.version) return result;
      }
    } catch (_error) { /* asked again below */ }
    if (!text) return result;
    const again = await hooks.runRequest(text);
    if (again) hooks.renderResult(again);
    return again;
  }

  async function stockConversation(result, text = null) {
    let current = await currentConversation(result, text);
    if (current && current !== result && current.conversation?.status !== "WAITING") {
      await handle(current, null);
      return;
    }
    let misses = 0;
    for (let turn = 0; turn < 12; turn += 1) {
      if (current?.conversation?.status !== "WAITING") break;
      const question = current.question || current.conversation.question || "";
      await sayResult(current, "check_screen", misses ? `Sorry, I didn't catch that. ${question}` : withQuestion(current.answer, question));
      const reply = await listen("answer");
      if (reply?.type === "cancelled") {
        current = await hooks.conversationTurn({kind: "CHOICE", choice: "CANCEL"});
        if (current) hooks.renderResult(current);
        break;
      }
      const text = String(reply?.text || "").trim();
      if (!text) {
        misses += 1;
        if (misses >= 2) {
          await say("leave_for_now", {}, "I'll leave it on screen. Tap an option, or say Hey Dimon when you're ready.");
          return;
        }
        continue;
      }
      misses = 0;
      note("You", text);
      const next = await hooks.conversationTurn({kind: "TEXT", text});
      if (!next) {
        await say("connection_lost", {}, "I lost the connection. Please check the screen before trying again.");
        return;
      }
      hooks.renderResult(next);
      current = next;
    }
    if (!current) { await say("order_cancelled", {}, "Okay, nothing was ordered."); return; }
    if (current.outcome === "ORDER_CONFIRMATION_REQUEST" && current.paper_order) {
      await approveShares(current);
      return;
    }
    if (current.conversation?.status === "WAITING") {
      await say("leave_for_now", {}, "Let's leave that on screen for now.");
      return;
    }
    await sayResult(current, "check_screen", current.answer || current.question || "Okay.");
  }

  function quoteMoney(currency, minor) {
    const value = (Number(minor || 0) / 100).toLocaleString("en-SG", {minimumFractionDigits: 2, maximumFractionDigits: 2});
    return currency === "SGD" ? `S$${value}` : currency === "USD" ? `US$${value}` : `${currency} ${value}`;
  }

  async function approveShares(result) {
    const order = result.paper_order;
    const selling = order.side === "SELL";
    const name = order.instrument?.name || order.instrument?.symbol || "the shares";
    const count = `${order.quantity} ${Number(order.quantity) === 1 ? "share" : "shares"}`;
    await sayResult(result, "investment_review", `${selling ? "Sell" : "Buy"} ${count} of ${name}. Approve it in the Singpass app, `
      + "then confirm with your face on screen. Say \"Hey Dimon, cancel\" to stop.");
    hooks.onMode?.("review", "Approve in Singpass, then confirm with your face");
    const outcome = await hooks.approvePaperOrder?.(order);
    if (!outcome) { await say("order_cancelled", {}, `Okay, nothing was ${selling ? "sold" : "bought"}.`); return; }
    const trade = outcome.trade || {};
    const currency = trade.quote_currency || order.instrument?.quote_currency || "USD";
    const market = String(order.instrument?.exchange || "").toLowerCase().includes("nasdaq") ? "Nasdaq"
      : String(order.instrument?.exchange || "").toLowerCase().includes("nyse") ? "the New York exchange" : "the market";
    if (outcome.status === "FILLED") {
      const price = quoteMoney(currency, trade.fill_price_minor);
      await sayResult(outcome, "done", selling
        ? `Done. Sold ${count} of ${name} at ${price} each. ${quoteMoney(trade.funding_currency || "SGD", trade.funding_credit_minor)} is in your savings.`
        : `Done. Bought ${count} of ${name} at ${price} each.`);
    } else if (outcome.status === "QUEUED") {
      await sayResult(outcome, "check_screen", `Approved. ${market} is closed now, so it will ${selling ? "sell" : "buy"} when it opens. You can cancel it in Wealth until then.`);
    } else if (outcome.status === "OPEN") {
      await sayResult(outcome, "check_screen", `Approved. It's waiting for your price of ${quoteMoney(currency, order.limit_price_minor)}. You can cancel it in Wealth.`);
    } else if (outcome.status === "CANCELLED") {
      await say("order_cancelled", {}, `Okay, nothing was ${selling ? "sold" : "bought"}.`);
    } else {
      const reasonCode = outcome.reason_code || outcome.status;
      if (isIdentityVerificationFailure(reasonCode)) {
        await say("identity_verification_failed", {}, IDENTITY_VERIFICATION_FAILURE_MESSAGE);
      } else {
        await sayResult(outcome, "order_failed", `It didn't go through: ${reason(reasonCode)}. Nothing was ${selling ? "sold" : "bought"}.`);
      }
    }
  }

  async function announce(result) {
    if (!result) { await say("nothing_sent", {}, "Nothing was sent."); return; }
    if (result.outcome?.startsWith("EXECUTED")) { await sayResult(result, "done", "Done."); return; }
    if (result.reason_code === "APPROVAL_CANCELLED") { await say("cancelled_not_sent", {}, "Okay, cancelled. Nothing was sent."); return; }
    await sayResult(result, "check_screen", result.question || `It didn't go through: ${reason(result.reason_code)}.`);
  }

  // ------------------------------------------------------------ approval
  // The face (and, the first time, the one-off Singpass identity check) on
  // screen. Hands-free keeps listening: "Hey Dimon, cancel" closes it.
  async function faceApproval(collection, receipt, lead) {
    const kyc = await fetch("/api/kyc", {cache: "no-store"}).then(r => r.json()).catch(() => ({}));
    const verified = kyc.kyc_status === "VERIFIED" && kyc.face_registered;
    await say(
      verified ? "face_confirm" : "identity_then_face",
      {},
      verified
        ? `${lead} Please confirm with your face on screen.`
        : `${lead} First, verify your identity once with Singpass, then confirm with your face on screen.`,
    );
    hooks.onMode?.("review", verified ? "Confirm with your face on screen" : "Verify your identity, then confirm with your face");
    await announce(await hooks.screenApproval(collection, receipt, {autoPlayReadBack: true}));
  }

  function isNo(text) {
    return /\b(no|nope|cancel|stop|don'?t|wait|never\s*mind)\b/i.test(text || "");
  }

  async function approve(collection, receipt, amountMinor, question) {
    if (!voiceprint) {
      await faceApproval(collection, receipt, question);
      return;
    }
    if (amountMinor >= voiceMaxMinor) {
      await faceApproval(collection, receipt, `${question} Payments of 200 dollars or more need your face.`);
      return;
    }
    let challenge;
    try {
      await hooks.ensureSigningCredential();
      const issued = await deviceApprover.requestChallenge(collection, receipt);
      if (issued.kind === "BLOCKED") {
        hooks.renderResult(issued.result);
        await sayResult(issued.result, "check_screen", issued.result.question || "That can't be approved.");
        return;
      }
      challenge = issued.challenge;
    } catch (_error) {
      await faceApproval(collection, receipt, question);
      return;
    }
    const review = challenge.review || {};
    if (review.requires_out_of_band || review.requires_unusual_confirmation) {
      await faceApproval(collection, receipt, `${question} This one needs an extra check.`);
      return;
    }
    // The server asks for the face (a new payee, or Singpass for larger
    // amounts): a spoken "Yes Confirm" would only be refused.
    if (review.requires_face || review.requires_singpass) {
      const why = (review.items || []).some(item => item.new_payee) ? "It's a new payee, so it needs your face."
        : "This one needs your face.";
      await faceApproval(collection, receipt, `${question} ${why}`);
      return;
    }
    const phraseResponse = await fetch("/api/voice/phrase", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({challenge_id: challenge.challenge_id}),
    });
    if (!phraseResponse.ok) {
      await faceApproval(collection, receipt, question);
      return;
    }
    const {phrase} = await phraseResponse.json();
    let verified = null;
    for (let attempt = 0; attempt < 2 && !verified; attempt += 1) {
      if (attempt === 0 && review.read_back?.audio_artifact_id) {
        await say("check_screen", {}, question, review.read_back.audio_artifact_id);
      }
      await say(attempt === 0 ? "voice_confirmation" : "voice_confirmation_retry", {},
        attempt === 0 ? `${question} Say: ${phrase}.` : `Please say: ${phrase}.`);
      // Only now, with Dimon silent and the microphone open, ask for the
      // phrase on screen; an answer said over Dimon's voice is never heard.
      hooks.showPhrase(phrase, question.replace(/\?$/, ""));
      const reply = await listen("approval", {challenge_id: challenge.challenge_id});
      if (reply?.type === "voice_verified") { verified = reply; break; }
      if (reply?.type === "cancelled" || (reply?.type === "voice_rejected" && isNo(reply.heard))) {
        hooks.showPhrase(null);
        await say("cancelled_not_sent", {}, "Okay, not sent.");
        return;
      }
      if (reply?.reason_code === "VOICE_LOCKED") break;
      if (attempt === 0) {
        const promptKey = reply?.reason_code === "VOICE_NO_MATCH" ? "voice_mismatch" : reply ? "did_not_catch" : "heard_nothing";
        await say(promptKey, {}, reply?.reason_code === "VOICE_NO_MATCH" ? "That didn't sound like your registered voice."
          : reply ? "I didn't catch that." : "I didn't hear anything.");
      }
    }
    hooks.showPhrase(null);
    if (!verified) {
      await faceApproval(collection, receipt, "Voice confirmation didn't work.");
      return;
    }
    try {
      // This browser's voice key signs on its own; without one, the
      // registered device does (Windows Hello may ask).
      const silent = await deviceApprover.hasKey(voiceKeyId);
      if (!silent) setStatus("Confirm on your device…");
      const assertion = silent
        ? await deviceApprover.signWithKey(challenge, voiceKeyId)
        : await deviceApprover.signChallenge(challenge);
      const approval = await hooks.submitApproval(collection, receipt, {
        assertion: hooks.wireAssertion(assertion),
        read_back_played: Boolean(review.read_back?.text),
      });
      if (approval.kind !== "RESULT") { await say("connection_lost", {}, "The connection dropped. Check the screen before trying again."); return; }
      hooks.renderResult(approval.result);
      await sayResult(approval.result, approval.result.outcome?.startsWith("EXECUTED") ? "done" : "check_screen",
        approval.result.outcome?.startsWith("EXECUTED")
        ? "Done. Confirmed by your voice."
        : `It wasn't sent: ${reason(approval.result.reason_code)}.`);
    } catch (_error) {
      await say("device_signature_failed", {}, "Your device didn't sign it, so nothing was sent.");
    }
  }

  // ------------------------------------------------------------ registration
  async function registerVoiceOnce() {
    // Check first: the microphone only turns on once registration can go ahead.
    let kyc = await fetch("/api/kyc", {cache: "no-store"}).then(r => r.json()).catch(() => ({}));
    if ((kyc.kyc_status !== "VERIFIED" || !kyc.face_registered) && hooks.verifyIdentity) {
      // Run the one-time identity check right here instead of sending the
      // user off to start a payment.
      voiceNote.textContent = "First, a one-time identity check: Singpass Myinfo and your face.";
      try {
        await hooks.verifyIdentity();
      } catch (_error) {
        voiceNote.textContent = "Identity check not finished, so your voice wasn't registered.";
        return;
      }
      kyc = await fetch("/api/kyc", {cache: "no-store"}).then(r => r.json()).catch(() => ({}));
    }
    if (kyc.kyc_status !== "VERIFIED" || !kyc.face_registered) {
      voiceNote.textContent = "Finish the one-time identity verification (Singpass Myinfo and your face) first.";
      return;
    }
    const body = {};
    try {
      // This browser's key for signing voice approvals without a pop-up.
      const registration = await deviceApprover.post("/api/signing/registration-challenge");
      body.voice_key = await deviceApprover.createVoiceKey(registration);
    } catch (_error) {
      // Without it, voice approvals are still signed by the registered device.
    }
    const response = await fetch("/api/voiceprint/enrollments", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    });
    const enrollment = await response.json().catch(() => ({}));
    if (!response.ok) {
      const code = String(enrollment.detail || "");
      const reasons = {
        FACE_NOT_REGISTERED: "Register your face during identity verification first.",
        KYC_EXPIRED: "Your identity verification expired. Verify again, then register your voice.",
        VOICE_ENGINE_UNAVAILABLE: "Voice identity unavailable · the speaker model is not ready on this server.",
      };
      voiceNote.textContent = reasons[code]
        || (code.startsWith("KYC_") ? "Finish the one-time identity verification (Singpass Myinfo and your face) first."
          : `Voice registration isn't available right now (${reason(code) || response.status}).`);
      return;
    }
    if (toggle.getAttribute("aria-pressed") !== "true") {
      await start();
      if (toggle.getAttribute("aria-pressed") !== "true") return;
      await waitFor(["ready"], 5000);
    }
    busy = true;
    try {
      let result = null;
      for (const [index, prompt] of enrollment.prompts.entries()) {
        let accepted = false;
        for (let attempt = 0; attempt < 3 && !accepted; attempt += 1) {
          voiceNote.textContent = `Recording ${index + 1} of ${enrollment.prompts.length}: say "${prompt}"`;
          await say("enrollment_prompt", {index}, `Please say: ${prompt}`);
          const reply = await listen("enroll", {enrollment_id: enrollment.enrollment_id});
          if (reply?.type === "enroll_sample") { accepted = true; result = reply; }
          else {
            const promptKey = reply?.reason_code === "VOICE_SAMPLES_INCONSISTENT"
              ? "voice_sample_inconsistent"
              : reply?.reason_code === "VOICE_PROMPT_NOT_HEARD" ? "voice_prompt_not_heard" : "voice_try_again";
            await say(promptKey, {}, reply?.reason_code === "VOICE_SAMPLES_INCONSISTENT"
              ? "That sounded different from before. Please try again."
              : reply?.reason_code === "VOICE_PROMPT_NOT_HEARD"
                ? "I didn't hear that phrase. Say it once more, on its own." : "I didn't get that. Once more.");
          }
        }
        if (!accepted) { voiceNote.textContent = "Voice registration stopped. Try again in a quiet place."; return; }
      }
      voiceNote.textContent = "Last step: confirm with your face and device.";
      await say("voice_registration_face", {}, "Thanks. Now confirm with your face on screen to turn on voice approvals.");
      await hooks.screenApproval("voice-enrollments", {receipt_id: result.receipt_id, receipt_digest: result.receipt_digest});
      await refreshVoiceStatus();
      if (voiceprint) await say("voice_registered", {}, `Voice approvals are on. For payments under 200 dollars, just say: ${CONFIRM_PHRASE}.`);
    } finally {
      busy = false;
      send({type: "sleep"});
      setStatus(socket ? idleText() : status.textContent);
    }
  }

  const registerVoice = createSingleFlight(registerVoiceOnce);

  async function refreshVoiceStatus() {
    voiceNote.textContent = "Checking voice identity…";
    try {
      const current = await fetch("/api/voiceprint", {cache: "no-store"}).then(r => r.json());
      voiceprint = Boolean(current.voice_registered);
      voiceKeyId = current.voice_key_credential_id || null;
      voiceMaxMinor = current.voice_max_minor || voiceMaxMinor;
      registerButton.lastElementChild.textContent = voiceprint ? "Re-register voice" : "Register my voice";
      const silent = voiceprint && await deviceApprover.hasKey(voiceKeyId);
      voiceNote.textContent = voiceprint
        ? `Voice identity ready · say "${CONFIRM_PHRASE}" to approve payments under S$200${silent ? "" : " (re-register on this browser to skip the device prompt)"}.`
        : current.voice_engine_ready
          ? "Voice identity ready · register your voice to approve everyday payments hands-free."
          : "Voice identity unavailable · the speaker model is not ready on this server.";
    } catch (_error) {
      voiceNote.textContent = "Voice identity unavailable · check the server connection and try again.";
    }
  }

  toggle.addEventListener("click", () => (toggle.getAttribute("aria-pressed") === "true" ? stop() : start()));
  registerButton.addEventListener("click", async () => {
    registerButton.disabled = true;
    try {
      await registerVoice();
    } finally {
      registerButton.disabled = false;
    }
  });
  void refreshVoiceStatus();
  preloadWakeGreeting();
  preloadVoiceEnrollmentPrompt();
  if (hooks.autoStart) void start();
  return {
    pause, resume, stop, pauseForPushToTalk, resumeAfterPushToTalk, say, converse, cancelListen,
    announceResult: sayResult,
    prepareFromGesture, unlockAudioFromGesture, setConversationContext, beginTrace, markBackendReady,
    isBusy: () => busy,
    isListening: () => Boolean(waiter),
    isSpeaking: () => speaking,
    stopSpeaking,
  };
}
