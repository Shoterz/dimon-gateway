// Hands-free Dimon: say "Hey Dimon, ..." and it does the task by voice.
//
// The browser streams the microphone to /api/voice/hands-free. The server
// ignores everything that does not start with "Hey Dimon" (and, once a
// voiceprint is registered, anything not in the account holder's voice).
// After a request, Dimon speaks the result and asks any follow-up out loud:
// which account, which payee, a name, "shall I go ahead?". Payments under
// S$200 are confirmed by saying a one-time four-digit phrase, which must match
// the registered voiceprint; the device key still signs every approval.
// Anything that needs more (S$200 and up, a new payee, investments, a token
// code) is handed to the normal on-screen confirmation with the face.

import { createVoiceAudioContext, VoiceFrameResampler } from "./voice_capture.mjs";

const REPLY_TIMEOUT_MS = 12_000;

export function initHandsFree(hooks) {
  const panel = document.querySelector("#hands-free-panel");
  if (!panel) return;
  const toggle = panel.querySelector("#hands-free-button");
  const status = panel.querySelector("#hands-free-status");
  const log = panel.querySelector("#hands-free-log");
  const registerButton = panel.querySelector("#voice-register-button");
  const voiceNote = panel.querySelector("#voice-register-note");

  let socket = null;
  let context = null;
  let stream = null;
  let node = null;
  let resampler = null;
  let speaking = false;          // Dimon is talking: the microphone is not sent
  let voiceprint = false;
  let voiceMaxMinor = 20_000;
  let waiter = null;             // {types, resolve, timer}
  let busy = false;

  const formatSgd = hooks.formatSgd;

  function setStatus(text) { status.textContent = text; }

  function note(who, text) {
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

  // ------------------------------------------------------------ speaking
  function say(text) {
    note("Dimon", text);
    return new Promise(resolve => {
      const synth = window.speechSynthesis;
      if (!synth || !window.SpeechSynthesisUtterance) { resolve(); return; }
      speaking = true;
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = "en-SG";
      utterance.rate = 1.02;
      const done = () => { speaking = false; setTimeout(resolve, 250); };
      utterance.onend = done;
      utterance.onerror = done;
      synth.cancel();
      synth.speak(utterance);
    });
  }

  // ------------------------------------------------------------ socket
  function send(message) {
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
  }

  function waitFor(types, timeoutMs = REPLY_TIMEOUT_MS) {
    return new Promise(resolve => {
      const timer = setTimeout(() => { waiter = null; resolve(null); }, timeoutMs);
      waiter = {types, resolve: value => { clearTimeout(timer); waiter = null; resolve(value); }, timer};
    });
  }

  async function listen(expect, extra = {}) {
    send({type: "listen", expect, ...extra});
    setStatus(expect === "approval" ? "Say the four digits…" : "Listening for your answer…");
    const message = await waitFor(["reply", "voice_verified", "voice_rejected", "enroll_sample",
                                   "enroll_rejected", "action_result", "error"]);
    setStatus("Say \"Hey Dimon\" to start");
    return message;
  }

  function onMessage(event) {
    const message = JSON.parse(event.data);
    if (message.type === "ready") {
      voiceprint = Boolean(message.voiceprint);
      voiceMaxMinor = message.voice_max_minor || voiceMaxMinor;
      setStatus(`Say "Hey Dimon" to start${voiceprint ? " · voice match on" : ""}`);
      return;
    }
    if (message.type === "wake") {
      setStatus("Listening…");
      if (!message.command) void say("Yes?");
      return;
    }
    if (message.type === "heard") note("You", message.text);
    if (message.type === "reply") note("You", message.text);
    if (message.type === "processing") setStatus("Working on it…");
    if (waiter && waiter.types.includes(message.type)) { waiter.resolve(message); return; }
    if (message.type === "action_result" && !busy) void converse(message.result, message.text);
    if (message.type === "error") setStatus(`Voice problem: ${String(message.reason_code).replaceAll("_", " ").toLowerCase()}`);
  }

  async function start() {
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
      setStatus("This browser can't listen hands-free. Use typing or Speak request.");
      return;
    }
    try {
      context = createVoiceAudioContext(window);
      if (context.state === "suspended") await context.resume();
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
      const scheme = location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(`${scheme}//${location.host}/api/voice/hands-free`);
      socket.binaryType = "arraybuffer";
      socket.onmessage = onMessage;
      socket.onclose = () => { if (toggle.getAttribute("aria-pressed") === "true") stop("Hands-free stopped."); };
      node.port.onmessage = event => {
        if (speaking || socket?.readyState !== WebSocket.OPEN) return;
        for (const frame of resampler.push(event.data)) socket.send(frame.buffer);
      };
      toggle.setAttribute("aria-pressed", "true");
      toggle.classList.add("recording");
      toggle.lastElementChild.textContent = "Stop hands-free";
      setStatus("Connecting…");
    } catch (error) {
      stop(error?.name === "NotAllowedError"
        ? "Microphone permission was denied."
        : "Couldn't start hands-free listening.");
    }
  }

  function stop(message = "Hands-free off") {
    toggle.setAttribute("aria-pressed", "false");
    toggle.classList.remove("recording");
    toggle.lastElementChild.textContent = "Turn on \"Hey Dimon\"";
    send({type: "stop"});
    socket?.close();
    socket = null;
    node?.disconnect();
    stream?.getTracks().forEach(track => track.stop());
    context?.close().catch(() => {});
    node = stream = context = resampler = null;
    window.speechSynthesis?.cancel();
    waiter?.resolve(null);
    setStatus(message);
  }

  // ------------------------------------------------------------ conversation
  function describeChoice(choice) {
    const from = choice.source_label ? ` from ${choice.source_label}` : "";
    return `${formatSgd(choice.amount_minor)} to ${choice.recipient_display_name}${from}`;
  }

  async function askConfirm(question) {
    await say(question);
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const reply = await listen("confirm");
      if (reply?.type === "reply" && reply.value !== null) return reply.value;
      if (attempt === 0) await say("Please say yes or no.");
    }
    return false;
  }

  async function followUp(text, extra) {
    // The user's spoken answer is added to their own request, which is
    // checked again from the start; Dimon never fills it in by itself.
    const result = await hooks.runRequest(`${text.replace(/[.!\s]+$/, "")} ${extra}`);
    if (result) await converse(result, null);
  }

  async function converse(result, text) {
    if (!result) return;
    busy = true;
    try {
      hooks.renderResult(result);
      await handle(result, text);
    } finally {
      busy = false;
      send({type: "sleep"});
      setStatus(`Say "Hey Dimon" to start${voiceprint ? " · voice match on" : ""}`);
    }
  }

  async function handle(result, text) {
    const outcome = result.outcome;
    if (outcome === "MULTI_REQUEST") {
      await say(`I heard ${result.steps.length} requests. Let's go through them.`);
      for (const [index, step] of result.steps.entries()) {
        await say(`Request ${index + 1}: ${step.text}.`);
        hooks.renderResult(step.result);
        await handle(step.result, step.text);
      }
      await say("That's everything.");
      return;
    }
    if (outcome === "ANSWERED") { await say(result.answer || "Done."); return; }
    if (outcome === "BLOCKED" || outcome === "FAILED") {
      await say(result.question || result.warning
        || `I couldn't do that: ${String(result.reason_code || "").replaceAll("_", " ").toLowerCase()}.`);
      return;
    }
    if (outcome === "CLARIFICATION_REQUEST" && result.reason_code === "MISSING_SOURCE_ACCOUNT") {
      const labels = (result.source_options || []).map(option => option.label);
      await say(result.question);
      const reply = await listen("account", {options: labels});
      if (reply?.value && text) await followUp(text, `from ${reply.value}`);
      else await say("I didn't catch which account, so I've left it.");
      return;
    }
    if (outcome === "CLARIFICATION_REQUEST" && result.reason_code === "PAYEE_NICKNAME_REQUIRED") {
      await say(result.question);
      const reply = await listen("name");
      if (reply?.value && text) await followUp(text, `as ${reply.value}`);
      else await say("I didn't catch a name, so nothing was saved.");
      return;
    }
    if (outcome === "CLARIFICATION_REQUEST" && result.reason_code === "MISSING_AMOUNT") {
      await say(`${result.question || "How much?"} Please say the whole request again with the amount.`);
      return;
    }
    if (outcome === "CLARIFICATION_REQUEST" && result.reason_code === "INVESTOR_PROFILE_REQUIRED") {
      await say("Before your first investment, please answer the four questions on screen.");
      return;
    }
    if (outcome === "CLARIFICATION_REQUEST" && (result.receipt_choices || []).length) {
      let choices = result.receipt_choices;
      if (choices.length > 1) {
        const names = choices.map(choice => choice.recipient_display_name);
        await say(`${result.question || "Which one?"}`);
        const reply = await listen("choice", {options: names});
        if (reply?.value === null || reply?.value === undefined) { await say("I'll leave that one for now."); return; }
        choices = [choices[reply.value]];
      }
      const choice = choices[0];
      if (result.unusual_amount) { await say(`${result.question} Please check this one on screen.`); return; }
      if (!await askConfirm(`Send ${describeChoice(choice)}?`)) { await say("Okay, not sent."); return; }
      await approve("receipts", choice, choice.amount_minor, describeChoice(choice));
      return;
    }
    if (outcome === "BATCH_CONFIRMATION_REQUEST") {
      const receipt = result.batch_receipt;
      const list = receipt.items.map(item => `${formatSgd(item.amount_minor)} to ${item.recipient_display_name}`).join(", ");
      if (!await askConfirm(`Send ${list}, ${formatSgd(receipt.total_minor)} in total from ${receipt.source_label}?`)) {
        await say("Okay, not sent.");
        return;
      }
      await approve("batch-receipts", receipt, receipt.total_minor, `${formatSgd(receipt.total_minor)} in total`);
      return;
    }
    if (outcome === "ORDER_CONFIRMATION_REQUEST") {
      await say(`${result.order.title}. Investments need your Singpass app and your face, so please confirm on screen.`);
      await hooks.approveOrder(result.order);
      return;
    }
    if (outcome === "PAYEE_ADDITION_REQUEST") {
      await say(`${result.question.replace("Confirm with your face.", "")} Adding a payee needs your face, so please confirm on screen.`);
      await hooks.approvePayeeAddition(result.payee_addition);
      return;
    }
    if (outcome?.startsWith("EXECUTED")) { await say("Done."); return; }
    await say(result.question || "Please check the screen.");
  }

  // ------------------------------------------------------------ approval
  async function approve(collection, receipt, amountMinor, summary) {
    const screen = async reason => {
      await say(`${reason} Please confirm on screen with your face.`);
      await hooks.screenApproval(collection, receipt);
    };
    if (!voiceprint) return screen("You haven't registered your voice yet.");
    if (amountMinor >= voiceMaxMinor) return screen("Payments of 200 dollars or more need your face.");
    let challenge;
    try {
      await hooks.ensureSigningCredential();
      const issued = await hooks.deviceApprover.requestChallenge(collection, receipt);
      if (issued.kind === "BLOCKED") { hooks.renderResult(issued.result); await say(issued.result.question || "That can't be approved."); return; }
      challenge = issued.challenge;
    } catch (_error) {
      return screen("I couldn't start the confirmation.");
    }
    const review = challenge.review || {};
    if (review.requires_out_of_band || review.requires_unusual_confirmation) {
      return screen("This one needs an extra check.");
    }
    const phraseResponse = await fetch("/api/voice/phrase", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({challenge_id: challenge.challenge_id}),
    });
    if (!phraseResponse.ok) return screen("Voice confirmation isn't available for this one.");
    const {phrase} = await phraseResponse.json();
    hooks.showPhrase(phrase, summary);
    if (review.read_back?.text) await say(review.read_back.text);
    let verified = null;
    for (let attempt = 0; attempt < 2 && !verified; attempt += 1) {
      await say(`To confirm, say: ${phrase.split(" ").join(", ")}.`);
      const reply = await listen("approval", {challenge_id: challenge.challenge_id}, REPLY_TIMEOUT_MS);
      if (reply?.type === "voice_verified") verified = reply;
      else if (reply?.reason_code === "VOICE_LOCKED") break;
      else if (attempt === 0) await say(reply?.reason_code === "VOICE_NO_MATCH"
        ? "That didn't sound like your registered voice." : "I didn't hear those digits.");
    }
    hooks.showPhrase(null);
    if (!verified) return screen("Voice confirmation didn't work.");
    try {
      const signed = {
        assertion: hooks.wireAssertion(await hooks.deviceApprover.signChallenge(challenge)),
        read_back_played: Boolean(review.read_back?.text),
      };
      const approval = await hooks.submitApproval(collection, receipt, signed);
      if (approval.kind !== "RESULT") { await say("The connection dropped. Check the screen before trying again."); return; }
      hooks.renderResult(approval.result);
      await say(approval.result.outcome?.startsWith("EXECUTED")
        ? `Done. ${summary} confirmed by your voice.`
        : `It wasn't sent: ${String(approval.result.reason_code || "").replaceAll("_", " ").toLowerCase()}.`);
    } catch (_error) {
      await say("Your device didn't sign it, so nothing was sent.");
    }
  }

  // ------------------------------------------------------------ registration
  async function registerVoice() {
    if (toggle.getAttribute("aria-pressed") !== "true") await start();
    const response = await fetch("/api/voiceprint/enrollments", {method: "POST"});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const reasons = {
        KYC_PENDING: "Finish identity verification first.",
        FACE_NOT_REGISTERED: "Register your face during verification first.",
        VOICE_ENGINE_UNAVAILABLE: "The voice model isn't installed. Run install.cmd again.",
      };
      voiceNote.textContent = reasons[body.detail] || "Voice registration isn't available right now.";
      return;
    }
    busy = true;
    try {
      let result = null;
      for (const [index, prompt] of body.prompts.entries()) {
        let accepted = false;
        for (let attempt = 0; attempt < 3 && !accepted; attempt += 1) {
          voiceNote.textContent = `Recording ${index + 1} of ${body.prompts.length}: say "${prompt}"`;
          await say(`Please say: ${prompt}`);
          const reply = await listen("enroll", {enrollment_id: body.enrollment_id});
          if (reply?.type === "enroll_sample") { accepted = true; result = reply; }
          else await say(reply?.reason_code === "VOICE_SAMPLES_INCONSISTENT"
            ? "That sounded different from before. Please try again." : "I didn't get that. Once more.");
        }
        if (!accepted) { voiceNote.textContent = "Voice registration stopped. Try again in a quiet place."; return; }
      }
      voiceNote.textContent = "Last step: confirm with your face and device.";
      await say("Thanks. Now confirm with your face on screen to turn on voice approvals.");
      await hooks.screenApproval("voice-enrollments", {receipt_id: result.receipt_id, receipt_digest: result.receipt_digest});
      await refreshVoiceStatus();
    } finally {
      busy = false;
      send({type: "sleep"});
    }
  }

  async function refreshVoiceStatus() {
    try {
      const status = await fetch("/api/voiceprint").then(r => r.json());
      voiceprint = Boolean(status.voice_registered);
      voiceMaxMinor = status.voice_max_minor || voiceMaxMinor;
      registerButton.lastElementChild.textContent = voiceprint ? "Re-register voice" : "Register my voice";
      voiceNote.textContent = voiceprint
        ? "Voice registered: say the digits Dimon gives you to approve payments under S$200."
        : status.voice_engine_ready
          ? "Register your voice to approve everyday payments hands-free."
          : "Voice approvals need the voice model (install.cmd downloads it).";
    } catch (_error) { /* keep the last text */ }
  }

  toggle.addEventListener("click", () => (toggle.getAttribute("aria-pressed") === "true" ? stop() : start()));
  registerButton.addEventListener("click", () => { void registerVoice(); });
  void refreshVoiceStatus();
}
