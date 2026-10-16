# Dimon Dual Streaming STT Design

Dimon will send each microphone PCM16 chunk to two recognizers during the same utterance. Nemotron is the caption engine: its interim results are shown as disposable live text, and its final result is never used for intent. CPU Faster-Whisper is the authority: only its final transcript is sent to the financial agent.

Nemotron is best-effort. Failure to open, receive audio, or finish disables captions for that utterance without failing it. Faster-Whisper failure remains fatal because the system must not execute financial intent from provisional captions. Both streams must be closed on success, failure, or client disconnect. Readiness and the public transcription revision represent the authoritative Faster-Whisper service; Nemotron continues to use its on-demand lifecycle controller.

The selectable backend name is `dual`. Existing `nemotron` and `whisper_cpu` modes remain available for diagnostics and rollback. Kokoro remains the only speech-output engine.
