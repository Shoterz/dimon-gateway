# Fast Warm Wake Detection Design

## Goal

Reduce the elapsed time from finishing “Hey Dimon” to opening the Ask experience from roughly four seconds to a target of 1.3–2.0 seconds, without weakening full-request transcription accuracy, introducing browser-native speech recognition, or losing words spoken while a connection becomes ready.

## Current behavior and cause

The browser continuously streams microphone frames to the Dimon hands-free WebSocket after it opens. Frames produced while that socket is connecting are discarded. The server uses energy-based utterance segmentation, waits for about 700 ms of trailing silence, and then sends the completed wake utterance to the CPU Faster-Whisper worker. Faster-Whisper returns only a final result, typically adding about three seconds.

The inference gateway already supports a dual transcription path: Faster-Whisper is authoritative and Nemotron emits partial captions. Nemotron is kept warm by the demo-preparation flow and has sufficient GPU capacity to remain available alongside Faster-Whisper and Kokoro.

## Approved architecture

### Wake detection

Use warm Nemotron partial transcripts as the fast wake signal. For each completed wake-sized utterance, Nemotron decoding and the existing speaker check run concurrently. As soon as a normalized partial confidently matches an accepted wake phrase and any required speaker match has passed, the server sends exactly one `wake` event to the browser. The browser can then switch to Ask and play the cached greeting without waiting for Faster-Whisper’s final transcript.

Accepted variants remain bounded by the existing `voice_dialog.wake` normalization and tests. Arbitrary mentions of words such as “demon” must not wake Dimon. If voice identity is registered, the existing speaker-match requirement still applies before the wake is accepted; fast detection must not bypass it.

Faster-Whisper remains authoritative for any complete request following the wake phrase. Nemotron partial text is never used to execute, approve, or fill a financial action.

### Fallback

If Nemotron is unavailable, times out, disconnects, or produces no accepted partial, the existing Faster-Whisper final wake result remains the fallback. The fallback may retain current latency but preserves availability.

Only one wake event may be emitted for an utterance. A later final transcript must reconcile with the already-emitted wake and must not open a second session or replay the greeting.

### One-second microphone pre-buffer

The browser begins collecting resampled microphone frames while the hands-free WebSocket connects. It retains a rolling maximum of one second of audio. After the server sends `ready`, the browser flushes those frames in chronological order before sending live frames.

The buffer is cleared after it is flushed, whenever listening stops, after each completed turn, and whenever the socket is replaced. It is never persisted.

Audio captured while Dimon is speaking, while the cached greeting is playing, or during the existing echo-suppression tail is neither buffered nor sent. This prevents Dimon from waking or transcribing itself.

## Protocol and state

The existing browser hands-free WebSocket remains the public interface. Its `ready`, binary-audio, `transcript_partial`, and `wake` message shapes remain compatible.

Internally, the wake recognizer gains an early-partial callback capable of marking the current utterance as woken. The utterance state records whether a wake event has already been emitted and whether final Faster-Whisper reconciliation is still pending. Closing, sleeping, cancellation, and reconnecting clear this state.

## Timing and observability

Record bounded metadata only:

- milliseconds from detected speech end to first Nemotron wake match;
- milliseconds from speech end to emitted `wake` event;
- whether the fast path or Faster-Whisper fallback was used;
- whether pre-buffered audio was flushed and its bounded duration.

Do not log transcript contents or raw audio.

The target is a median warm-path wake event within two seconds after the user finishes the wake phrase. The UI transition should begin immediately upon receiving that event; cached greeting playback remains independent of model generation.

## Tests

- Nemotron partial wake emits one early `wake` event.
- A later authoritative final does not emit a duplicate wake.
- Non-wake partials never trigger the UI.
- Registered-speaker mismatch cannot use the fast path.
- Nemotron failure falls back to Faster-Whisper final recognition.
- The browser retains at most one second of frames before `ready` and flushes them in order.
- Buffered audio is cleared between turns, reconnects, and stop/start cycles.
- No frames are buffered or sent while Dimon is audible or during the echo-suppression tail.
- Full requests continue to use Faster-Whisper’s authoritative final text.

## Out of scope

- Replacing Faster-Whisper for complete requests.
- Reducing the full-request model size or beam width.
- Browser-native speech recognition or speech synthesis.
- Changing payment, approval, or conversation intent behavior.
