// "Ask Dimon": a live voice conversation with an AI guide over TRTC
// Conversational AI. Speech recognition, the LLM, and speech synthesis all run
// in TRTC's cloud; this module only joins the room, sends the microphone, and
// shows subtitles. The guide has no tools, so it cannot move money; banking
// requests still go through "Speak request" and the signed approval flow.

const SDK_URL = "https://cdn.jsdelivr.net/npm/trtc-sdk-v5@5.20.1/trtc.js";
const SUBTITLE_MESSAGE = 10000;

let sdkPromise = null;

function loadSdk() {
  if (window.TRTC) return Promise.resolve(window.TRTC);
  sdkPromise ??= new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = SDK_URL;
    script.async = true;
    script.onload = () => (window.TRTC ? resolve(window.TRTC) : reject(new Error("TRTC_SDK_MISSING")));
    script.onerror = () => {
      sdkPromise = null;
      reject(new Error("TRTC_SDK_UNREACHABLE"));
    };
    document.head.append(script);
  });
  return sdkPromise;
}

async function post(path) {
  const response = await fetch(path, { method: "POST" });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `HTTP_${response.status}`);
  return body;
}

function describeFailure(error) {
  const name = error?.name || "";
  const message = error?.message || "";
  if (name === "NotAllowedError" || /permission/i.test(message)) {
    return "Microphone permission was denied. Allow it in the browser to talk to Dimon.";
  }
  if (message === "TRTC_SDK_UNREACHABLE") return "Couldn't load the TRTC voice SDK. Check the network and try again.";
  if (message === "TRTC_ASSISTANT_DISABLED") return "The live assistant isn't configured on this server.";
  if (message.startsWith("TRTC_")) return `The voice service refused the call (${message}).`;
  return "The live assistant couldn't start. Try again in a moment.";
}

export function initTrtcAssistant(root = document) {
  const panel = root.querySelector("#assistant-panel");
  if (!panel) return;
  const button = panel.querySelector("#assistant-button");
  const status = panel.querySelector("#assistant-status");
  const log = panel.querySelector("#assistant-log");

  let client = null;
  let botUserId = null;
  let phase = "idle"; // idle | connecting | live | stopping
  const lines = new Map();

  function setPhase(next, text) {
    phase = next;
    status.textContent = text;
    button.classList.toggle("recording", next === "live");
    button.setAttribute("aria-pressed", String(next === "live"));
    button.disabled = next === "connecting" || next === "stopping";
    button.lastElementChild.textContent = next === "live" ? "End conversation" : "Talk to Dimon";
  }

  function showSubtitle(message) {
    const { payload, sender } = message;
    if (!payload || typeof payload.text !== "string" || !payload.text.trim()) return;
    const who = sender === botUserId ? "dimon" : "you";
    const key = `${who}:${payload.roundid ?? ""}`;
    let line = lines.get(key);
    if (!line) {
      line = document.createElement("p");
      line.className = `assistant-line ${who}`;
      const label = document.createElement("span");
      label.textContent = who === "dimon" ? "Dimon" : "You";
      line.append(label, document.createTextNode(""));
      lines.set(key, line);
      log.append(line);
      log.hidden = false;
    }
    line.lastChild.textContent = payload.text;
    line.classList.toggle("interim", payload.end !== true);
    log.scrollTop = log.scrollHeight;
  }

  async function teardown() {
    const current = client;
    client = null;
    if (!current) return;
    try { await current.stopLocalAudio(); } catch { /* already stopped */ }
    try { await current.exitRoom(); } catch { /* never entered */ }
    try { current.destroy(); } catch { /* already destroyed */ }
  }

  async function start() {
    setPhase("connecting", "Connecting…");
    lines.clear();
    log.replaceChildren();
    try {
      const TRTC = await loadSdk();
      const room = await post("/api/trtc/assistant/join");
      botUserId = room.bot_user_id;
      client = TRTC.create();
      client.on(TRTC.EVENT.CUSTOM_MESSAGE, event => {
        try {
          const message = JSON.parse(new TextDecoder().decode(event.data));
          if (message.type === SUBTITLE_MESSAGE) showSubtitle(message);
        } catch { /* not a subtitle */ }
      });
      client.on(TRTC.EVENT.REMOTE_USER_EXIT, event => {
        if (event.userId === botUserId && phase === "live") stop("Dimon ended the conversation.");
      });
      await client.enterRoom({
        sdkAppId: room.sdk_app_id,
        userId: room.user_id,
        userSig: room.user_sig,
        strRoomId: room.room_id,
        scene: "rtc",
      });
      await client.startLocalAudio();
      await post("/api/trtc/assistant/start");
      setPhase("live", "Listening. Ask how anything works.");
    } catch (error) {
      await teardown();
      post("/api/trtc/assistant/stop").catch(() => {});
      setPhase("idle", describeFailure(error));
    }
  }

  async function stop(message = "Conversation ended.") {
    if (phase !== "live") return;
    setPhase("stopping", "Ending…");
    // Stop the cloud task first so the agent stops billing, then release the mic.
    await post("/api/trtc/assistant/stop").catch(() => {});
    await teardown();
    setPhase("idle", message);
  }

  button.addEventListener("click", () => (phase === "live" ? stop() : start()));
  window.addEventListener("pagehide", () => {
    if (phase === "live") navigator.sendBeacon?.("/api/trtc/assistant/stop");
  });

  fetch("/api/trtc")
    .then(response => (response.ok ? response.json() : {}))
    .then(features => { panel.hidden = !features.assistant; })
    .catch(() => {});
}
