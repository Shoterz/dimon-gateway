const WORKER_ORDER = ["qwen", "nemotron", "kokoro", "faster_whisper"];

const WORKER_LABELS = {
  qwen: "Qwen",
  nemotron: "Nemotron",
  kokoro: "Kokoro",
  faster_whisper: "Faster-Whisper Medium",
};

const PHASE_LABELS = {
  d_click: "D clicked",
  audio_ready: "Browser audio ready",
  microphone_ready: "Microphone ready",
  socket_ready: "Socket connected",
  backend_ready: "Backend trace ready",
  attempt_started: "Attempt started",
  speech_detected: "Speech detected",
  nemotron_stream_ready: "Nemotron stream ready",
  first_partial: "First partial",
  wake_match: "Wake phrase matched",
  speaker_check: "Speaker check",
  wake_sent: "Wake sent",
  browser_wake_received: "Wake reached browser",
  authoritative_final: "Final recognition ready",
  recognition_complete: "Recognition complete",
  reply_audio_ready: "Reply audio ready",
  reply_playback_started: "Reply playback started",
  attempt_finished: "Attempt finished",
  failure: "Recognition failed",
  cancelled: "Attempt cancelled",
};

const PATH_LABELS = {
  nemotron_partial: "Nemotron partial",
  nemotron_final: "Nemotron final",
  faster_whisper_final: "Faster-Whisper Medium final",
  cancelled: "Cancelled",
  failed: "Failed",
};

export const DIAGNOSTICS_VISIBILITY_KEY = "dimon.voiceDiagnostics.visible";

function finiteNumber(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function readableToken(value) {
  if (typeof value !== "string" || !value) return null;
  return value.split("_").map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(" ");
}

export function diagnosticsVisible(storage) {
  try {
    return storage?.getItem(DIAGNOSTICS_VISIBILITY_KEY) !== "false";
  } catch (_error) {
    return true;
  }
}

export function setDiagnosticsVisible(storage, visible) {
  try {
    storage?.setItem(DIAGNOSTICS_VISIBILITY_KEY, visible ? "true" : "false");
  } catch (_error) {
    // The panel remains usable when private browsing blocks local storage.
  }
}

export function diagnosticsPresentation(payload = {}) {
  const trace = payload?.trace && typeof payload.trace === "object" ? payload.trace : {};
  const rawWorkers = Array.isArray(payload?.workers) ? payload.workers : [];
  const workersByName = new Map(rawWorkers.map(worker => [worker?.name, worker]));
  const workers = WORKER_ORDER.map(name => {
    const raw = workersByName.get(name) || {};
    const detail = [
      Number.isInteger(raw.worker_pid) ? `PID ${raw.worker_pid}` : null,
      typeof raw.profile === "string" ? raw.profile : null,
      Number.isInteger(raw.active_jobs) ? `${raw.active_jobs} active` : null,
      finiteNumber(raw.heartbeat_age_seconds) !== null
        ? `${raw.heartbeat_age_seconds.toFixed(1)} s heartbeat`
        : null,
      typeof raw.device === "string" ? raw.device.toUpperCase() : null,
      typeof raw.compute_type === "string" ? raw.compute_type : null,
      Number.isInteger(raw.context_tokens) ? `${raw.context_tokens.toLocaleString()} context` : null,
      finiteNumber(raw.observed_vram_mb) !== null && finiteNumber(raw.budget_mb) !== null
        ? `${Math.round(raw.observed_vram_mb).toLocaleString()} / ${Math.round(raw.budget_mb).toLocaleString()} MB`
        : finiteNumber(raw.observed_vram_mb) !== null
          ? `${Math.round(raw.observed_vram_mb).toLocaleString()} MB`
          : null,
    ].filter(Boolean).join(" · ");
    return {
      name,
      label: WORKER_LABELS[name],
      loaded: raw.loaded === true,
      state: typeof raw.state === "string" ? raw.state : "MISSING",
      detail,
    };
  });

  const attempts = Array.isArray(trace.attempts) ? trace.attempts : [];
  const attempt = [...attempts].reverse().find(item => item && item.path)
    || [...attempts].reverse().find(Boolean)
    || null;
  const selectedAttemptId = typeof attempt?.attempt_id === "string" ? attempt.attempt_id : null;
  const events = (Array.isArray(trace.events) ? trace.events : [])
    .filter(event => !event?.attempt_id || event.attempt_id === selectedAttemptId)
    .filter(event => event && PHASE_LABELS[event.name] && (event.source === "browser" || event.source === "server"))
    .map(event => ({
      name: event.name,
      label: PHASE_LABELS[event.name],
      source: event.source,
      elapsedMs: finiteNumber(event.elapsed_ms),
    }))
    .filter(event => event.elapsedMs !== null);
  const rawGpu = payload?.gpu && typeof payload.gpu === "object" ? payload.gpu : null;
  const totalMb = finiteNumber(rawGpu?.total_mb);
  const freeMb = finiteNumber(rawGpu?.free_mb);
  const reserveMb = finiteNumber(rawGpu?.safety_reserve_mb);

  return {
    traceId: typeof trace.trace_id === "string" ? trace.trace_id : null,
    workers,
    events,
    path: typeof attempt?.path === "string" ? attempt.path : null,
    pathLabel: PATH_LABELS[attempt?.path] || "Waiting for a recognition attempt",
    failurePhase: typeof attempt?.failure_phase === "string" ? attempt.failure_phase : null,
    failureLabel: readableToken(attempt?.failure_phase),
    recognizerDisagreement: attempt?.recognizer_disagreement === true,
    fasterWhisperConfidence: ["normal", "low"].includes(attempt?.faster_whisper_confidence)
      ? attempt.faster_whisper_confidence : null,
    fasterWhisperAvgLogprob: typeof attempt?.faster_whisper_avg_logprob === "number"
      && Number.isFinite(attempt.faster_whisper_avg_logprob)
      ? attempt.faster_whisper_avg_logprob : null,
    fasterWhisperNoSpeechProb: finiteNumber(attempt?.faster_whisper_no_speech_prob),
    gpu: totalMb === null || freeMb === null || reserveMb === null ? null : {
      totalMb,
      freeMb,
      reserveMb,
      aboveReserve: freeMb >= reserveMb,
    },
  };
}

function element(documentRef, tag, className, content) {
  const node = documentRef.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}

function formatMilliseconds(value) {
  return value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(2)} s`;
}

function renderDiagnostics(documentRef, nodes, payload) {
  const view = diagnosticsPresentation(payload);
  const loadedCount = view.workers.filter(worker => worker.loaded).length;
  nodes.status.textContent = view.traceId
    ? `Run ${view.traceId.slice(0, 8)} · ${loadedCount}/4 loaded`
    : `${loadedCount}/4 workers loaded · waiting for D click`;

  const workerItems = view.workers.map(worker => {
    const item = element(documentRef, "li", "voice-diagnostics-worker");
    item.dataset.loaded = String(worker.loaded);
    const heading = element(documentRef, "span", "voice-diagnostics-worker-heading");
    heading.append(
      element(documentRef, "i", "voice-diagnostics-worker-dot"),
      element(documentRef, "strong", "", worker.label),
      element(documentRef, "span", "", worker.loaded ? "Loaded" : "Not loaded"),
    );
    item.append(heading);
    if (worker.detail) item.append(element(documentRef, "small", "", worker.detail));
    return item;
  });
  nodes.workers.replaceChildren(...workerItems);

  const quality = [
    view.recognizerDisagreement ? "recognizers disagreed" : null,
    view.fasterWhisperConfidence ? `FW confidence ${view.fasterWhisperConfidence}` : null,
  ].filter(Boolean).join(" · ");
  nodes.path.textContent = view.failureLabel
    ? `${view.pathLabel} · Nemotron: ${view.failureLabel}`
    : quality ? `${view.pathLabel} · ${quality}` : view.pathLabel;
  nodes.path.dataset.path = view.path || "waiting";
  nodes.gpu.textContent = view.gpu
    ? `${Math.round(view.gpu.freeMb).toLocaleString()} MB free · ${Math.round(view.gpu.reserveMb).toLocaleString()} MB reserve`
    : "Waiting for GPU evidence";
  nodes.gpu.dataset.reserve = view.gpu ? (view.gpu.aboveReserve ? "ok" : "low") : "unknown";

  if (!view.events.length) {
    nodes.events.replaceChildren(element(documentRef, "li", "voice-diagnostics-empty", "Click the red D, wait five seconds, then say “Hey Dimon”."));
    return;
  }
  nodes.events.replaceChildren(...view.events.map(event => {
    const item = element(documentRef, "li", "voice-diagnostics-event");
    item.dataset.source = event.source;
    item.append(
      element(documentRef, "i", "voice-diagnostics-event-dot"),
      element(documentRef, "span", "", event.label),
      element(
        documentRef,
        "time",
        "",
        `${formatMilliseconds(event.elapsedMs)} ${event.source === "browser" ? "from click" : "backend"}`,
      ),
    );
    return item;
  }));
}

export function initVoiceDiagnostics({
  documentRef = document,
  fetchImpl = fetch,
  storage = localStorage,
  timers = window,
} = {}) {
  const root = documentRef.querySelector("#voice-diagnostics");
  const show = documentRef.querySelector("#voice-diagnostics-show");
  if (!root || !show) return {refresh: async () => {}, markRunStarted() {}, destroy() {}};
  const details = root.querySelector("#voice-diagnostics-details");
  const hide = root.querySelector("#voice-diagnostics-hide");
  const nodes = {
    status: root.querySelector("#voice-diagnostics-status"),
    workers: root.querySelector("#voice-diagnostics-workers"),
    events: root.querySelector("#voice-diagnostics-events"),
    gpu: root.querySelector("#voice-diagnostics-gpu"),
    path: root.querySelector("#voice-diagnostics-path"),
  };

  function applyVisibility(visible, {focus = false} = {}) {
    root.hidden = !visible;
    show.hidden = visible;
    show.setAttribute("aria-expanded", String(visible));
    setDiagnosticsVisible(storage, visible);
    if (focus) (visible ? details.querySelector("summary") : show).focus();
  }

  applyVisibility(diagnosticsVisible(storage));
  if (timers.matchMedia("(max-width: 640px)").matches) details.open = false;
  hide?.addEventListener("click", () => applyVisibility(false, {focus: true}));
  show.addEventListener("click", () => applyVisibility(true, {focus: true}));

  let refreshInFlight = null;
  async function refresh() {
    if (refreshInFlight) return refreshInFlight;
    refreshInFlight = Promise.resolve().then(async () => {
      const response = await fetchImpl("/api/voice/diagnostics/latest", {cache: "no-store"});
      if (!response.ok) throw new Error(`HTTP${response.status}`);
      renderDiagnostics(documentRef, nodes, await response.json());
    }).catch(() => {
      nodes.status.textContent = "Diagnostics temporarily unavailable";
    }).finally(() => {
      refreshInFlight = null;
    });
    return refreshInFlight;
  }

  function markRunStarted() {
    nodes.status.textContent = "Starting a new measured run…";
    void refresh();
  }

  // Visibility never gates this timer: hiding is presentation-only.
  const interval = timers.setInterval(() => void refresh(), 1000);
  void refresh();
  return {
    refresh,
    markRunStarted,
    destroy() { timers.clearInterval(interval); },
  };
}
