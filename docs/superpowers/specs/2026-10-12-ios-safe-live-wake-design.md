# iOS-Safe Speech Playback and Live Wake Detection Design

## Goal

Make the first warm “Hey Dimon” activation reliable and normally complete within two seconds, while ensuring every spoken Dimon response remains usable on iOS Safari when autoplay is blocked.

This design preserves Faster-Whisper as the authoritative recognizer for complete user requests. Nemotron provides live partial text only for wake detection and captions; a Nemotron partial must never execute or populate a financial action.

## Confirmed causes

### iOS greeting playback

The hands-free path currently creates a separate programmatic `Audio` object. If `play()` rejects, the rejection is caught as though playback completed. The visible reply player is bypassed, the greeting disappears, and microphone capture resumes even though the user never heard Dimon.

iOS Safari may reject programmatic playback without a valid user activation. This is expected browser policy behavior, but silently treating the rejection as successful playback is an application defect.

### First wake after warming

The red D currently warms backend models, but it does not make the browser microphone pipeline ready. On iOS, the audio context may still be suspended and the microphone worklet or hands-free WebSocket may still be starting. The green state can therefore appear before the browser is capable of sending audio. A rolling audio buffer cannot preserve speech produced before the browser audio context begins producing frames.

### Nemotron “unavailable” and long wake latency

Nemotron is installed, listening, model-loaded, and able to transcribe both normal and short test utterances directly. The observed unavailable label combines several different outcomes—such as empty noisy audio, timeout, stream closure, and protocol errors—under one broad code.

The larger wake-path defect is architectural: the server currently waits until the browser utterance has ended, then opens the dual recognizer and replays the stored recording into it. Nemotron is therefore not receiving audio live and cannot wake from a partial while the user speaks.

The partial matcher also expects either a small exact phrase set or two identical consecutive partial strings. Nemotron emits progressively longer partial strings and can transcribe the product name as “Diamond,” so the matcher frequently misses a valid evolving partial. The server then falls back to Faster-Whisper. Production timing confirms successful wakes have been using that final fallback, commonly taking about 3.2–4.5 seconds after speech ends. Mobile noise has also kept some wake recordings open for 7–15 seconds, explaining the reported 7.98-second result; network distance alone does not explain it.

## Scope

This change includes:

- iOS-safe Dimon speech playback using the existing visible reply player;
- explicit browser and backend readiness states;
- live streaming of wake audio to the dual recognition service;
- bounded, progressive Nemotron wake matching;
- a 2.5-second wake-only capture cap;
- retained one-second microphone pre-buffering;
- Faster-Whisper authoritative final transcripts and outage fallback;
- existing registered-speaker enforcement;
- precise, privacy-preserving recognition telemetry.

It does not change financial approval rules, lower the Faster-Whisper model or beam quality, introduce browser-native speech recognition or synthesis, or use Nemotron partial text as a transaction instruction.

## Speech playback state machine

The application owns one speech player: the existing visible Dimon reply player. Hands-free speech calls into that player instead of creating an unrelated `Audio` object.

1. Set the player source and prepare the UI before attempting playback.
2. Attempt normal autoplay.
3. If autoplay succeeds, continue without requiring a tap. The player may show the active playback state but does not interrupt the normal flow.
4. If `play()` is rejected because user activation is required, leave the player visible and paused. Show “Tap play to hear Dimon.”
5. While speech is paused or playing, mark Dimon as speaking and suppress microphone buffering and transmission.
6. Resolve the speech operation only on the media element’s `ended` event. After the full clip ends, apply the existing echo-suppression tail and then resume listening automatically.
7. A deliberate user stop, navigation, or hands-free shutdown cancels playback and listening together; it must not masquerade as a successfully heard reply.

The manual player is therefore a fallback shown when autoplay is blocked, not a mandatory interaction on browsers that permit autoplay.

## Readiness state machine

The warm control reports separate states:

- `Warming services`: backend preparation is not complete.
- `Starting microphone`: services are warm, but the browser audio context, microphone worklet, or hands-free socket is not ready.
- `Ready — say “Hey Dimon”`: backend preparation is ready, the audio context is running, microphone frames can be produced, and the hands-free WebSocket has acknowledged `ready`.
- `Needs attention`: a required component failed or permission was denied, with a specific recovery action.

The D logo’s user gesture synchronously resumes or unlocks the browser audio context before its click handler awaits network preparation. It then starts or verifies the microphone and socket while warming backend services. The green badge means full readiness only; model warmth by itself is not green-ready.

The existing one-second browser pre-buffer remains. It retains the newest second of frames while the socket becomes ready and flushes them in chronological order after `ready`. It is cleared on every turn, reconnect, stop, and socket replacement. It cannot contain Dimon’s own playback or the echo-suppression tail.

## Live wake recognition

### Utterance session

When asleep-mode voice activity starts, the server creates one utterance-scoped live recognition session immediately. Initial VAD pre-roll and all following microphone chunks are queued in order and streamed to the existing dual recognition gateway as they arrive. A short bounded queue absorbs connection startup without waiting for utterance completion.

The dual stream serves two roles:

- Nemotron emits interim partials for wake matching and live captions.
- Faster-Whisper returns the authoritative final transcript after the utterance ends.

If the live stream cannot open or fails, the server retains the bounded utterance audio and runs the existing Faster-Whisper fallback after endpointing. It emits at most one wake event.

### Wake matcher

Wake normalization accepts the deliberately supported spoken-name forms “Dimon,” “Diamon,” and the common recognizer rendering “Diamond,” preceded by “Hey” or “Hi.” It remains bounded so unrelated occurrences of “demon” or arbitrary mentions do not activate Dimon.

The matcher evaluates progressive cumulative partials. It does not require the entire partial string to repeat unchanged. A wake prefix becomes stable when:

- an accepted standalone wake phrase is complete; or
- the accepted wake prefix remains present across two successive forward-progressing partials; or
- a final transcript confirms the accepted wake phrase.

Text after the wake prefix is not trusted as a command until Faster-Whisper returns the authoritative final transcript. Early Nemotron output can only open the Ask experience.

### Endpoint and duration bounds

Asleep-mode capture keeps the existing silence endpoint but caps a wake-only attempt at approximately 2.5 seconds. On reaching the cap, the session is finalized and evaluated instead of accumulating 7–15 seconds of mobile background audio. Full request capture after activation retains its separately configured duration and endpoint behavior.

The 2.5-second cap is not applied while Dimon is speaking or while playback is awaiting a user tap, because the microphone is suppressed in those states.

## Voice identity and safety

If no voice identity is registered, any speaker may activate “Hey Dimon,” matching current product behavior.

If a voice identity is registered, early activation is emitted only after the existing speaker verification returns a positive match. `unknown`, insufficient audio, timeout, and mismatch never count as approval. Speaker verification runs concurrently with live wake decoding so it does not unnecessarily serialize the fast path.

Wake recognition never grants transaction authority. All transaction confirmation and approval controls remain unchanged.

## Duplicate and race handling

Each utterance has one stable identifier and one wake-emitted flag. A Nemotron partial, Nemotron final, Faster-Whisper final, reconnect, or fallback may not emit a second wake for the same utterance. Starting a new turn, sleeping, explicit cancellation, or socket replacement clears the utterance session and its queues.

Greeting playback owns microphone suppression until `ended`, preventing the cached greeting from becoming the next request. Audio received from a stale socket or a cancelled session is ignored.

## Failure classification and telemetry

User-facing recovery stays simple, but internal telemetry distinguishes at least:

- Nemotron connection failure;
- readiness timeout;
- stream closed before final;
- protocol or malformed-event failure;
- empty or no-speech final;
- wake phrase not matched;
- speaker verification mismatch, unknown, or timeout;
- Faster-Whisper fallback and fallback failure;
- autoplay rejection and later user-started playback.

Record durations for browser readiness, first partial, wake match, speaker verification, endpointing, authoritative final, and Ask transition. Record the selected path and whether the 2.5-second cap was reached. Do not log raw audio, transcript text, or voice embeddings.

## Interfaces

The browser hands-free controller exposes explicit operations or state notifications for audio unlock, microphone readiness, socket readiness, and speech playback. The application combines these with preparation status to render the D badge.

The server recognizer gains an utterance-scoped streaming interface: open, enqueue audio, receive partials, finish, and cancel. The existing batch transcription API remains available as the fallback and for compatibility. Public WebSocket messages remain compatible; additional readiness and timing metadata must be optional so older clients can ignore it.

## Tests

### Browser

- Successful autoplay requires no tap and listening resumes only after `ended`.
- Rejected autoplay shows the existing paused player and “Tap play to hear Dimon.”
- No audio is buffered or transmitted while the blocked player is paused, while it plays, or during the echo tail.
- Pressing play and reaching `ended` automatically resumes listening.
- The D click resumes the audio context within the user gesture and reports each readiness phase.
- Green-ready is impossible before services, audio context, microphone/worklet, and socket are all ready.
- The one-second pre-buffer remains bounded, flushes in order, and clears on lifecycle boundaries.

### Server and gateway

- Live Nemotron receives chunks before the utterance endpoint.
- Progressive partials such as “Hey” → “Hey Diamond” → “Hey Diamond send” wake once without identical-string repetition.
- Unsupported or incidental phrases do not wake.
- A Nemotron partial cannot populate or execute a request.
- Registered-speaker mismatch, unknown, and timeout cannot early-wake.
- An unregistered account permits any speaker to wake.
- A 2.5-second noisy asleep attempt is finalized rather than growing indefinitely.
- Live-stream failure falls back to Faster-Whisper using the retained audio.
- Early wake and later authoritative final cannot emit duplicate wake events.
- Distinct Nemotron failure categories are observable without transcript or audio logging.

## Deployment acceptance

After unit and integration tests pass, validate on desktop Safari-compatible behavior and a physical iPhone Safari session:

- blocked autoplay leaves a usable paused player;
- microphone capture resumes only after the clip finishes;
- the first wake after D warm-up works once the UI says ready;
- at least five warm unregistered-speaker trials have a target median phrase-end-to-Ask transition of 2.0 seconds or less, with outliers explained by precise telemetry;
- registered-speaker trials never activate for a non-matching voice;
- Faster-Whisper remains authoritative for complete requests;
- all public app modules load and all required backend services report ready.

Physical-device latency results must be reported as measured rather than inferred. If the target is missed, phase timing identifies whether the remaining delay is browser readiness, endpointing, speaker verification, Nemotron matching, or network transport.
