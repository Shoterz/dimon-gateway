export const AGENT_VOICE_PLAYBACK_RATE = 1.00;

export async function waitForAudioArtifact(audioElement, url, {
  fetchImpl = globalThis.fetch,
  sleep = delayMs => new Promise(resolve => setTimeout(resolve, delayMs)),
  createObjectUrl = blob => URL.createObjectURL(blob),
  isCurrent = () => true,
  onFailure = () => {},
  attempts = 160,
} = {}) {
  for (let attempt = 0; attempt < attempts && isCurrent(); attempt += 1) {
    let response;
    try {
      response = await fetchImpl(url, {cache: "no-store"});
    } catch (_error) {
      if (isCurrent()) onFailure("NetworkError");
      return null;
    }
    if (response.status === 202) {
      await sleep(250);
      continue;
    }
    if (response.ok) {
      let blob;
      try {
        blob = await response.blob();
      } catch (_error) {
        if (isCurrent()) onFailure("BlobReadError");
        return null;
      }
      if (!blob.size || !blob.type.startsWith("audio/")) {
        if (isCurrent()) onFailure("InvalidAudioPayload");
        return null;
      }
      const objectUrl = createObjectUrl(blob);
      audioElement.dataset.resultAudioObjectUrl = objectUrl;
      audioElement.src = objectUrl;
      audioElement.preload = "metadata";
      audioElement.hidden = false;
      audioElement.load();
      return objectUrl;
    }
    if (isCurrent()) onFailure(`HTTP${response.status}`);
    return null;
  }
  if (isCurrent()) onFailure("PendingTimeout");
  return null;
}

export async function autoplayResultAudio(audioElement, {
  onEnded = () => {},
  onFailure = () => {},
  playImpl = () => audioElement.play(),
} = {}) {
  audioElement.autoplay = true;
  audioElement.playbackRate = AGENT_VOICE_PLAYBACK_RATE;
  audioElement.addEventListener("ended", onEnded, {once: true});
  try {
    await playImpl();
    return true;
  } catch (error) {
    // Browser autoplay policy may require the visible player to be pressed.
    // The player remains available and the caller can restore voice readiness.
    const errorName = /^[A-Za-z0-9_.-]{1,64}$/.test(error?.name || "")
      ? error.name
      : "PlaybackError";
    onFailure(errorName);
    return false;
  }
}

export function playAudioToCompletion(audioElement, {
  onBlocked = () => {},
  onFailure = () => {},
  onStarted = () => {},
  playImpl = () => audioElement.play(),
  signal = null,
} = {}) {
  audioElement.playbackRate = AGENT_VOICE_PLAYBACK_RATE;
  return new Promise(resolve => {
    let settled = false;
    const cleanup = () => {
      audioElement.removeEventListener("ended", onEnded);
      audioElement.removeEventListener("error", onError);
      signal?.removeEventListener("abort", onAbort);
    };
    const finish = value => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(value);
    };
    const onEnded = () => finish("ended");
    const onError = () => {
      onFailure("MediaError");
      finish("failed");
    };
    const onAbort = () => finish("cancelled");

    audioElement.addEventListener("ended", onEnded);
    audioElement.addEventListener("error", onError);
    signal?.addEventListener("abort", onAbort, {once: true});
    if (signal?.aborted) {
      onAbort();
      return;
    }
    Promise.resolve()
      .then(playImpl)
      .then(() => {
        try { onStarted(); } catch (_error) { /* Diagnostics cannot stop playback. */ }
      })
      .catch(error => {
        const errorName = /^[A-Za-z0-9_.-]{1,64}$/.test(error?.name || "")
          ? error.name
          : "PlaybackError";
        if (errorName === "NotAllowedError") {
          onBlocked(errorName);
          return;
        }
        onFailure(errorName);
        finish("failed");
      });
  });
}

export function clearResultAudio(audioElement, {
  revokeObjectUrl = url => URL.revokeObjectURL(url),
} = {}) {
  audioElement.pause();
  const objectUrl = audioElement.dataset.resultAudioObjectUrl;
  if (objectUrl) revokeObjectUrl(objectUrl);
  delete audioElement.dataset.resultAudioObjectUrl;
  audioElement.autoplay = false;
  audioElement.removeAttribute("src");
  audioElement.hidden = true;
  audioElement.load();
}
