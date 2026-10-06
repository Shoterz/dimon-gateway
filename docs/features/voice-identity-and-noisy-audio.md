# Voice identity and noisy audio

Status: partly implemented (October 2026). Personalised wake word, speaker
verification and a replay check are in `apps/dimon_live/voiceprint.py` and
`hands_free.py`. Speaker verification stands in for the face only on approvals
under S$200, together with a fresh spoken one-time phrase and the device
signature, never alone; S$200 and above, new payees, investments and voiceprint
registration still need the face. Presentation-attack (deepfake) detection and
target-speaker extraction are not implemented.

## Product question

Can Dimon Gateway hear the enrolled customer in a noisy environment, avoid accidental wake-ups from other people or media, and make the user experience feel personal?

The answer should affect whether the assistant starts listening or asks for typed input. It must not independently authorize a payment.

## Four separate capabilities

| Capability | Question answered | Example outcome |
| --- | --- | --- |
| Personalized wake-word detection | Did the enrolled user say “Hey Dimon”? | Ignore a nearby stranger saying the wake phrase |
| Speaker verification | Does this audio match the enrolled user's voice? | “Voice match accepted” or “voice match insufficient” |
| Diarization / target-speaker extraction | Which person is speaking, or can we isolate the target speaker? | Suppress a television or nearby conversation |
| Presentation-attack detection | Is this likely live human speech rather than a replay or synthetic clone? | Escalate a replayed recording to typed approval |

These are not interchangeable. A wake word detects a phrase; speaker verification compares a voice to an enrolled voiceprint; separation handles competing audio; anti-spoofing looks for replay and synthetic-audio attacks.

## Inspiration from music and audio recognition

Music stem separation is useful inspiration. It is a source-separation problem: estimate the desired source, such as vocals, while suppressing drums or other instruments. The speech equivalent is target-speaker extraction, where an enrolled speaker embedding can help a model isolate that person's speech from a mixture.

Shazam-style audio fingerprinting solves a different problem: it identifies a known recording from robust acoustic landmarks. It can inspire exact-replay detection when a recording is already known, but it does not establish that an unknown live voice belongs to the customer. Speaker verification needs a speaker embedding and calibrated comparison threshold instead.

Do not claim a new breakthrough in convolutional filtering or that Dimon Gateway outperforms consumer assistants. The credible hackathon contribution is a transparent composition of existing speech capabilities with an enforced transaction-approval boundary.

## Candidate interaction flow

```text
microphone
  → voice activity detection
  → personalized “Hey Dimon” check
  → speaker-match and audio-quality result
  → anti-spoof result
  → target-speaker extraction or diarization, if needed
  → ASR transcript
  → untrusted payment-planning input
  → receipt review and explicit approval
```

The transcript remains untrusted content even after a successful voice match. An enrolled user can speak a malicious instruction, make a mistake, or be socially engineered. A successful voice match only permits conversation; it does not grant transaction authority.

## First-demo scope

Start with an experimental “voice access signal,” not a claim of biometric banking authentication:

1. Obtain informed consent and enrol a small voice sample for each test participant.
2. Compare a command against the enrolled voice embedding.
3. Display an explainable result: match, non-match, or uncertain.
4. On non-match or uncertainty, refuse hands-free interaction and offer typed input.
5. Keep explicit receipt approval mandatory for every payment, regardless of result.

The event brief permits mock biometrics. Label this feature as an experimental, mock-biometric workflow and state that it does not prove liveness, legal consent, or bank-grade identity assurance.

## Evaluation matrix

| Test | Desired system behaviour | Ledger result |
| --- | --- | --- |
| Enrolled user in a quiet room | Wake, match, transcribe request | No debit until receipt approval |
| Another person says “Hey Dimon” | Do not accept as the customer | No debit |
| Background television or nearby conversation | Do not produce a payment request | No debit |
| Enrolled user in moderate noise | Mark uncertain or recover safely | No debit without review |
| Replay of an enrolled recording | Flag as suspicious where possible; require typed flow | No debit |
| Generated voice clone | Treat as untrusted; do not claim reliable detection without measurement | No debit |
| Enrolled user says an injected instruction | Treat transcript as untrusted; planner may not gain execution authority | No debit without valid receipt approval |

Report false accepts, false rejects, uncertainty rate, speech-recognition errors on payees and amounts, and end-to-end unauthorized debits separately. A low false-accept rate on a small demo set is evidence about that test set only.

## Privacy and retention

A voice embedding is biometric-related data. Keep audio and embeddings out of Git, obtain recording consent, limit access, define retention, and provide deletion in any pilot. The prototype should use synthetic payment data and avoid presenting the voice signal as a permanent identity credential.

## Research context

NIST runs speaker-recognition evaluations to measure and calibrate text-independent speaker-recognition systems. ASVspoof 5 includes both speech-deepfake detection and spoofing-robust speaker-verification tracks, which illustrates why a speaker match alone is insufficient. [NIST Speaker Recognition Evaluation](https://sre.nist.gov/), [ASVspoof 5](https://www.asvspoof.org/).
