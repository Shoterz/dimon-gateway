"""Voiceprint registration and spoken confirmation of everyday approvals.

The account holder registers a voiceprint (a 256-number speaker embedding, not
a recording) after KYC. Registering it is itself approved with the registered
face and a device signature, so nobody holding an unlocked session can enrol
their own voice.

For transfers under S$200 the voice can stand in for the face scan: Dimon
asks the user to say "Yes Confirm", and the utterance must (1) say it, (2)
match the registered voiceprint, (3) be spoken after this approval's phrase
was issued and within five minutes, and (4) never have been heard before
byte for byte. The match is bound to that approval's one-time challenge and
the exact receipt digest and is spent once, inside the step that also
verifies the device signature. The device signature is always required;
S$200 and above, new payees and every investment always need the face.

A voice match is evidence, not proof: cloned and replayed voices can fool
speaker verification, and a fixed phrase is easier to replay from a
recording than one-time digits, which is why it is limited to small
transfers, paired with the device key, and never used alone.

What is stored: the template, and per utterance its verdict, similarity, the
bounded confirmation words heard, and the SHA-256 of the audio. Audio is
analysed in memory and discarded.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import secrets
import sqlite3
import struct
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, Sequence

import numpy as np

SAMPLE_RATE = 16_000
SPEAKER_MODEL_BYTES = 26_530_309
SPEAKER_MODEL_SHA256 = "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068"
MATCH_THRESHOLD = 0.55          # cosine similarity to the registered voiceprint
# "Yes Confirm" is too short (~0.7 s) for a reliable speaker match on its own.
# When the request it confirms was spoken moments earlier in the same
# hands-free session, that longer utterance must clear MATCH_THRESHOLD and the
# short phrase this lower bar: two voice checks instead of one.
# Measured on the speaker model: the account holder's "Yes, confirm" scores
# 0.41-0.53, other voices 0.00-0.20; the request itself 0.82 vs 0.00-0.20.
CONFIRM_MATCH_THRESHOLD = 0.30
PRIOR_REQUEST_TTL_SECONDS = 180
ENROLL_CONSISTENCY = 0.50       # every registration sample must match the others
ENROLL_SAMPLES = 3
MIN_SECONDS = 1.0               # each registration recording
MIN_CONFIRM_SECONDS = 0.5       # "Yes Confirm" takes well under a second to say
MAX_SECONDS = 15.0
MAX_FAILED = 5
LOCKOUT_WINDOW = timedelta(minutes=10)
PHRASE_TTL = timedelta(minutes=5)
VOICE_MAX_MINOR = 20_000        # below S$200 the voice may replace the face
ENROLLMENT_TTL = timedelta(minutes=10)

VOICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_templates (
  user_id TEXT PRIMARY KEY,
  template BLOB NOT NULL,
  template_sha256 TEXT NOT NULL,
  engine_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PENDING', 'ACTIVE')),
  enrollment_id TEXT NOT NULL,
  enrolled_at TEXT NOT NULL,
  activated_at TEXT
);
CREATE TABLE IF NOT EXISTS voice_enrollments (
  enrollment_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  template BLOB,
  template_sha256 TEXT,
  enrollment_digest TEXT,
  sample_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK(status IN ('RECORDING', 'AWAITING_CONFIRMATION', 'ACTIVE', 'EXPIRED')),
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_enroll_vectors (
  enrollment_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  vector BLOB NOT NULL,
  PRIMARY KEY (enrollment_id, position)
);
CREATE TABLE IF NOT EXISTS voice_samples (
  sample_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  purpose TEXT NOT NULL CHECK(purpose IN ('ENROLL', 'VERIFY', 'WAKE')),
  enrollment_id TEXT,
  challenge_id TEXT,
  receipt_digest TEXT,
  phrase_expected TEXT,
  phrase_heard TEXT,
  similarity REAL,
  seconds REAL NOT NULL,
  verdict TEXT NOT NULL CHECK(verdict IN ('MATCH', 'RECORDED', 'REJECTED')),
  reason_code TEXT,
  audio_sha256 TEXT NOT NULL,
  engine_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  consumed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS voice_samples_one_match_per_challenge
  ON voice_samples(challenge_id) WHERE verdict = 'MATCH' AND purpose = 'VERIFY';
CREATE INDEX IF NOT EXISTS voice_samples_by_user_time ON voice_samples(user_id, created_at);
CREATE TABLE IF NOT EXISTS voice_audio_seen (
  audio_sha256 TEXT PRIMARY KEY,
  sample_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_phrases (
  challenge_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  receipt_digest TEXT NOT NULL,
  phrase TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

_DIGIT_WORDS = {
    "zero": "0", "oh": "0", "o": "0", "one": "1", "won": "1", "two": "2", "to": "2", "too": "2",
    "three": "3", "tree": "3", "four": "4", "for": "4", "fore": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "ate": "8", "nine": "9", "niner": "9",
}
ENROLL_PROMPTS = (
    "My voice is my confirmation for Dimon",
    "Send money only when I say so",
    "Every payment needs my own voice",
)


class VoiceRejected(ValueError):
    def __init__(self, reason_code: str, sample: dict[str, Any] | None = None):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.sample = sample


class SpeakerEngine(Protocol):
    version: str

    def embed(self, pcm: np.ndarray) -> np.ndarray: ...


# --------------------------------------------------------------- features
def _mel(frequency: np.ndarray | float) -> np.ndarray | float:
    return 1127.0 * np.log(1.0 + np.asarray(frequency) / 700.0)


@lru_cache(maxsize=1)
def _mel_banks(num_bins: int = 80, fft_size: int = 512, sample_rate: int = SAMPLE_RATE,
               low: float = 20.0) -> np.ndarray:
    """Kaldi's triangular mel filters over the FFT bins (no DC excluded)."""

    high = sample_rate / 2
    mel_low, mel_high = _mel(low), _mel(high)
    delta = (mel_high - mel_low) / (num_bins + 1)
    fft_mels = _mel(np.arange(fft_size // 2) * sample_rate / fft_size)
    banks = np.zeros((num_bins, fft_size // 2 + 1), dtype=np.float64)
    for index in range(num_bins):
        left, center, right = (mel_low + (index + k) * delta for k in range(3))
        up = (fft_mels - left) / (center - left)
        down = (right - fft_mels) / (right - center)
        banks[index, : fft_size // 2] = np.maximum(0.0, np.minimum(up, down))
    return banks


def fbank(pcm: np.ndarray, num_bins: int = 80) -> np.ndarray:
    """80-band log mel filterbank, Kaldi style (25 ms frames, 10 ms hop,
    Povey window, pre-emphasis 0.97), with per-utterance mean removal, which
    is what WeSpeaker models are trained on."""

    waveform = np.asarray(pcm, dtype=np.float64) * 32768.0
    frame_length, frame_shift, fft_size = 400, 160, 512
    if waveform.size < frame_length:
        raise VoiceRejected("VOICE_TOO_SHORT")
    count = 1 + (waveform.size - frame_length) // frame_shift
    index = np.arange(frame_length)[None, :] + frame_shift * np.arange(count)[:, None]
    frames = waveform[index]
    frames = frames - frames.mean(axis=1, keepdims=True)
    emphasized = frames.copy()
    emphasized[:, 1:] -= 0.97 * frames[:, :-1]
    emphasized[:, 0] -= 0.97 * frames[:, 0]
    window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(frame_length) / (frame_length - 1))) ** 0.85
    spectrum = np.abs(np.fft.rfft(emphasized * window, n=fft_size)) ** 2
    energies = spectrum @ _mel_banks(num_bins, fft_size).T
    features = np.log(np.maximum(energies, np.finfo(np.float32).eps))
    features -= features.mean(axis=0, keepdims=True)
    return features.astype(np.float32)


def float32le_to_array(audio: bytes) -> np.ndarray:
    if len(audio) % 4:
        raise VoiceRejected("MALFORMED_AUDIO")
    samples = np.frombuffer(audio, dtype="<f4")
    if not np.all(np.isfinite(samples)):
        raise VoiceRejected("MALFORMED_AUDIO")
    return np.clip(samples, -1.0, 1.0)


def trim_silence(pcm: np.ndarray, threshold: float = 0.01) -> np.ndarray:
    """Drop leading and trailing silence so a long pause cannot dilute the voice."""

    frame = 160
    usable = pcm[: pcm.size - pcm.size % frame]
    if usable.size == 0:
        return pcm
    energy = np.sqrt((usable.reshape(-1, frame) ** 2).mean(axis=1))
    voiced = np.flatnonzero(energy >= threshold)
    if voiced.size == 0:
        return pcm[:0]
    return pcm[voiced[0] * frame: (voiced[-1] + 1) * frame]


class OnnxSpeakerEngine:
    """WeSpeaker ResNet34 (VoxCeleb), run on the CPU with onnxruntime."""

    def __init__(self, model_path: Path):
        import onnxruntime

        self.model_path = Path(model_path)
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
        self.session = onnxruntime.InferenceSession(
            str(self.model_path), options, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name
        digest = hashlib.sha256(self.model_path.read_bytes()).hexdigest()[:12]
        self.version = f"wespeaker-resnet34-lm:{digest}"

    def embed(self, pcm: np.ndarray) -> np.ndarray:
        features = fbank(pcm)[None, :, :]
        (embedding,) = self.session.run(None, {self.input_name: features})
        vector = np.asarray(embedding[0], dtype=np.float64)
        return vector / (np.linalg.norm(vector) or 1.0)


def model_path(env: dict[str, str] | os._Environ = os.environ) -> Path:
    configured = env.get("DIMON_SPEAKER_MODEL_PATH", "").strip()
    if configured:
        return Path(configured)
    project_root = Path(__file__).resolve().parents[2]
    return project_root / "models" / "voice" / "voxceleb_resnet34_LM.onnx"


def speaker_model_health(env: dict[str, str] | os._Environ = os.environ) -> dict[str, str]:
    path = model_path(env)
    if not path.is_file():
        return {"state": "unavailable", "path": str(path), "reason": "model_missing"}
    if path.stat().st_size != SPEAKER_MODEL_BYTES:
        return {"state": "unavailable", "path": str(path), "reason": "model_size_mismatch"}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != SPEAKER_MODEL_SHA256:
        return {"state": "unavailable", "path": str(path), "reason": "model_checksum_mismatch"}
    return {"state": "ready", "path": str(path), "reason": "verified"}


@lru_cache(maxsize=1)
def _load_default_engine(path: Path) -> OnnxSpeakerEngine:
    return OnnxSpeakerEngine(path)


def default_engine() -> OnnxSpeakerEngine | None:
    path = model_path()
    if not path.is_file():
        return None
    try:
        return _load_default_engine(path)
    except Exception:  # noqa: BLE001 - retry transient and broken-model failures
        return None


default_engine.cache_clear = _load_default_engine.cache_clear  # type: ignore[attr-defined]


# --------------------------------------------------------------- helpers
def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b / denominator) if denominator else 0.0


def _pack(vector: Sequence[float]) -> bytes:
    values = [float(value) for value in vector]
    return struct.pack(f"<{len(values)}d", *values)


def _unpack(blob: bytes) -> np.ndarray:
    return np.asarray(struct.unpack(f"<{len(blob) // 8}d", blob), dtype=np.float64)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


CONFIRM_PHRASE = "Yes Confirm"
# "Confirm" as recognisers hear it: "conform", "come firm", "can firm".
_SAID_CONFIRM = re.compile(
    r"\b(?:yes|yeah|yea|yep|yup|ya|yah)\b[\s,.!]*(?:i\s+)?(?:c[aou]n|come|kon)\s*f[aeiou]r+m(?:ed|s)?\b",
    re.IGNORECASE,
)
_SAID_NO = re.compile(r"\b(?:no|not|don'?t|do\s+not|cancel|stop|wait)\b", re.IGNORECASE)


def new_phrase() -> str:
    """What to say to confirm by voice: always "Yes Confirm"."""

    return CONFIRM_PHRASE


def digits_heard(transcript: str) -> str:
    """The digits in a transcript, from numerals or number words."""

    heard: list[str] = []
    for token in re.findall(r"[a-z]+|\d", transcript.casefold()):
        if token.isdigit():
            heard.append(token)
        elif token in _DIGIT_WORDS:
            heard.append(_DIGIT_WORDS[token])
    return "".join(heard)


def phrase_spoken(phrase: str, transcript: str) -> bool:
    if phrase == CONFIRM_PHRASE:
        return bool(_SAID_CONFIRM.search(transcript)) and not _SAID_NO.search(transcript)
    # Phrases issued before "Yes Confirm": four one-time digits.
    expected = phrase.replace(" ", "")
    return expected in digits_heard(transcript)


def phrase_heard(phrase: str, transcript: str) -> str:
    """What is recorded about the spoken confirmation (never the whole audio)."""

    if phrase == CONFIRM_PHRASE:
        return " ".join(re.findall(r"[a-z']+", transcript.casefold()))[:60]
    return digits_heard(transcript)


# --------------------------------------------------------------- store
class VoiceStore:
    """The separate voice database, beside the ledger, like the face one."""

    def __init__(self, path: Path, engine: Callable[[], SpeakerEngine | None]):
        self.path = Path(path)
        self._engine = engine
        self._ready = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 3000")
            if not self._ready:
                connection.executescript(VOICE_SCHEMA)
                self._ready = True
            yield connection
        finally:
            connection.close()

    def engine(self) -> SpeakerEngine:
        engine = self._engine()
        if engine is None:
            raise VoiceRejected("VOICE_ENGINE_UNAVAILABLE")
        return engine

    def status(self, user_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, enrolled_at, activated_at FROM voice_templates WHERE user_id = ?", (user_id,)
            ).fetchone()
        engine = self._engine()
        return {
            "voice_registered": bool(row and row["status"] == "ACTIVE"),
            "voice_status": row["status"] if row else "NONE",
            "voice_enrolled_at": row["activated_at"] if row else None,
            "voice_engine_ready": engine is not None,
            "voice_max_minor": VOICE_MAX_MINOR,
        }

    def active_template(self, connection: sqlite3.Connection, user_id: str) -> np.ndarray | None:
        row = connection.execute(
            "SELECT template FROM voice_templates WHERE user_id = ? AND status = 'ACTIVE'", (user_id,)
        ).fetchone()
        return _unpack(row["template"]) if row else None

    def _locked(self, connection: sqlite3.Connection, user_id: str, now: datetime) -> bool:
        since = _utc(now - LOCKOUT_WINDOW)
        failures = connection.execute(
            """
            SELECT COUNT(*) FROM voice_samples
            WHERE user_id = ? AND verdict = 'REJECTED' AND purpose = 'VERIFY' AND created_at >= ?
            """,
            (user_id, since),
        ).fetchone()[0]
        return failures >= MAX_FAILED

    def _prepare(self, audio: bytes, min_seconds: float = MIN_SECONDS) -> tuple[np.ndarray, str, float]:
        pcm = trim_silence(float32le_to_array(audio))
        seconds = pcm.size / SAMPLE_RATE
        if seconds < min_seconds:
            raise VoiceRejected("VOICE_TOO_SHORT")
        if seconds > MAX_SECONDS:
            raise VoiceRejected("VOICE_TOO_LONG")
        return pcm, hashlib.sha256(audio).hexdigest(), seconds

    def _record(self, connection: sqlite3.Connection, **fields: Any) -> dict[str, Any]:
        sample = {
            "sample_id": f"voice_{secrets.token_hex(12)}",
            "enrollment_id": None, "challenge_id": None, "receipt_digest": None,
            "phrase_expected": None, "phrase_heard": None, "similarity": None, "reason_code": None,
            **fields,
        }
        connection.execute(
            """
            INSERT INTO voice_samples (
              sample_id, user_id, purpose, enrollment_id, challenge_id, receipt_digest,
              phrase_expected, phrase_heard, similarity, seconds, verdict, reason_code,
              audio_sha256, engine_version, created_at
            ) VALUES (:sample_id, :user_id, :purpose, :enrollment_id, :challenge_id, :receipt_digest,
              :phrase_expected, :phrase_heard, :similarity, :seconds, :verdict, :reason_code,
              :audio_sha256, :engine_version, :created_at)
            """,
            sample,
        )
        if sample["verdict"] != "REJECTED" or sample["reason_code"] != "VOICE_REPLAYED":
            connection.execute(
                "INSERT OR IGNORE INTO voice_audio_seen (audio_sha256, sample_id) VALUES (?, ?)",
                (sample["audio_sha256"], sample["sample_id"]),
            )
        return sample

    def _seen(self, connection: sqlite3.Connection, audio_sha256: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM voice_audio_seen WHERE audio_sha256 = ?", (audio_sha256,)
        ).fetchone() is not None

    # ---------------------------------------------------------- enrolment
    def start_enrollment(self, user_id: str, now: datetime) -> dict[str, Any]:
        enrollment_id = f"venroll_{secrets.token_hex(16)}"
        with self._connect() as connection:
            connection.execute(
                "UPDATE voice_enrollments SET status = 'EXPIRED' WHERE user_id = ? AND status IN ('RECORDING', 'AWAITING_CONFIRMATION')",
                (user_id,),
            )
            connection.execute(
                """
                INSERT INTO voice_enrollments (enrollment_id, user_id, status, expires_at, created_at)
                VALUES (?, ?, 'RECORDING', ?, ?)
                """,
                (enrollment_id, user_id, _utc(now + ENROLLMENT_TTL), _utc(now)),
            )
        return {"enrollment_id": enrollment_id, "prompts": list(ENROLL_PROMPTS), "samples_needed": ENROLL_SAMPLES}

    def _enrollment(self, connection: sqlite3.Connection, enrollment_id: str, user_id: str, now: datetime) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM voice_enrollments WHERE enrollment_id = ?", (enrollment_id,)
        ).fetchone()
        if row is None or row["user_id"] != user_id:
            raise VoiceRejected("VOICE_ENROLLMENT_NOT_FOUND")
        if now.astimezone(timezone.utc) >= datetime.fromisoformat(row["expires_at"]):
            raise VoiceRejected("VOICE_ENROLLMENT_EXPIRED")
        return row

    def add_enrollment_sample(
        self, *, user_id: str, enrollment_id: str, audio: bytes, now: datetime
    ) -> dict[str, Any]:
        engine = self.engine()
        pcm, audio_sha256, seconds = self._prepare(audio)
        embedding = engine.embed(pcm)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._enrollment(connection, enrollment_id, user_id, now)
                if row["status"] != "RECORDING":
                    raise VoiceRejected("VOICE_ENROLLMENT_CLOSED")
                if self._seen(connection, audio_sha256):
                    raise VoiceRejected("VOICE_REPLAYED")
                previous = [
                    _unpack(vector["vector"]) for vector in connection.execute(
                        "SELECT vector FROM voice_enroll_vectors WHERE enrollment_id = ? ORDER BY position",
                        (enrollment_id,),
                    )
                ]
                for other in previous:
                    if cosine(embedding, other) < ENROLL_CONSISTENCY:
                        raise VoiceRejected("VOICE_SAMPLES_INCONSISTENT")
                sample = self._record(
                    connection, user_id=user_id, purpose="ENROLL", enrollment_id=enrollment_id,
                    seconds=round(seconds, 2), verdict="RECORDED", audio_sha256=audio_sha256,
                    engine_version=engine.version, created_at=_utc(now),
                )
                connection.execute(
                    "INSERT INTO voice_enroll_vectors (enrollment_id, position, vector) VALUES (?, ?, ?)",
                    (enrollment_id, len(previous), _pack(embedding)),
                )
                count = len(previous) + 1
                result: dict[str, Any] = {"sample_id": sample["sample_id"], "samples": count,
                                          "samples_needed": ENROLL_SAMPLES}
                if count >= ENROLL_SAMPLES:
                    vectors = [*previous, embedding]
                    template = np.mean(vectors, axis=0)
                    template /= np.linalg.norm(template) or 1.0
                    blob = _pack(template)
                    template_sha256 = hashlib.sha256(blob).hexdigest()
                    digest = hashlib.sha256(
                        f"voice-enrollment:{enrollment_id}:{user_id}:{template_sha256}:{row['expires_at']}".encode()
                    ).hexdigest()
                    connection.execute(
                        """
                        UPDATE voice_enrollments SET template = ?, template_sha256 = ?, enrollment_digest = ?,
                          sample_count = ?, status = 'AWAITING_CONFIRMATION' WHERE enrollment_id = ?
                        """,
                        (blob, template_sha256, digest, count, enrollment_id),
                    )
                    result.update({"complete": True, "receipt_id": enrollment_id, "receipt_digest": digest})
                else:
                    connection.execute(
                        "UPDATE voice_enrollments SET sample_count = ? WHERE enrollment_id = ?", (count, enrollment_id)
                    )
                    result["complete"] = False
                connection.commit()
                return result
            except VoiceRejected:
                connection.rollback()
                raise

    def awaiting_enrollment(self, enrollment_id: str, digest: str, user_id: str, now: datetime) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._enrollment(connection, enrollment_id, user_id, now)
            if row["status"] == "ACTIVE":
                raise VoiceRejected("RECEIPT_ALREADY_EXECUTED")
            if row["status"] != "AWAITING_CONFIRMATION":
                raise VoiceRejected("VOICE_ENROLLMENT_INCOMPLETE")
            if row["enrollment_digest"] != digest:
                raise VoiceRejected("RECEIPT_DIGEST_MISMATCH")
            return dict(row)

    def activate(self, *, enrollment_id: str, digest: str, user_id: str, now: datetime) -> dict[str, Any]:
        engine = self.engine()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._enrollment(connection, enrollment_id, user_id, now)
            if row["status"] != "AWAITING_CONFIRMATION" or row["enrollment_digest"] != digest:
                connection.rollback()
                raise VoiceRejected("VOICE_ENROLLMENT_INCOMPLETE")
            connection.execute(
                """
                INSERT INTO voice_templates (user_id, template, template_sha256, engine_version, status,
                  enrollment_id, enrolled_at, activated_at)
                VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET template = excluded.template,
                  template_sha256 = excluded.template_sha256, engine_version = excluded.engine_version,
                  status = 'ACTIVE', enrollment_id = excluded.enrollment_id,
                  enrolled_at = excluded.enrolled_at, activated_at = excluded.activated_at
                """,
                (user_id, row["template"], row["template_sha256"], engine.version, enrollment_id,
                 row["created_at"], _utc(now)),
            )
            connection.execute(
                "UPDATE voice_enrollments SET status = 'ACTIVE' WHERE enrollment_id = ?", (enrollment_id,)
            )
            connection.commit()
        return {"voice_registered": True, "template_sha256": row["template_sha256"]}

    # ---------------------------------------------------------- confirmation
    def issue_phrase(self, *, user_id: str, challenge_id: str, receipt_digest: str, now: datetime) -> str:
        with self._connect() as connection:
            if self.active_template(connection, user_id) is None:
                raise VoiceRejected("VOICE_NOT_REGISTERED")
            existing = connection.execute(
                "SELECT phrase FROM voice_phrases WHERE challenge_id = ? AND user_id = ?", (challenge_id, user_id)
            ).fetchone()
            if existing:
                return existing["phrase"]
            phrase = new_phrase()
            connection.execute(
                """
                INSERT INTO voice_phrases (challenge_id, user_id, receipt_digest, phrase, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (challenge_id, user_id, receipt_digest, phrase, _utc(now + PHRASE_TTL), _utc(now)),
            )
            return phrase

    def verify(
        self, *, user_id: str, challenge_id: str, receipt_digest: str, audio: bytes, transcript: str, now: datetime,
        prior_request_audio: bytes | None = None,
    ) -> dict[str, Any]:
        """Match one spoken confirmation against the voiceprint and the phrase.

        prior_request_audio is the request this confirms, as spoken moments
        earlier in the same hands-free session (see CONFIRM_MATCH_THRESHOLD)."""

        engine = self.engine()
        pcm, audio_sha256, seconds = self._prepare(audio, MIN_CONFIRM_SECONDS)
        embedding = engine.embed(pcm)
        prior_similarity = None
        if prior_request_audio is not None:
            try:
                prior_pcm, _, _ = self._prepare(prior_request_audio)
            except VoiceRejected:
                prior_pcm = None
            if prior_pcm is not None:
                prior_embedding = engine.embed(prior_pcm)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            base = dict(user_id=user_id, purpose="VERIFY", challenge_id=challenge_id,
                        receipt_digest=receipt_digest, seconds=round(seconds, 2),
                        audio_sha256=audio_sha256, engine_version=engine.version, created_at=_utc(now))

            def reject(reason: str, **extra: Any) -> VoiceRejected:
                sample = self._record(connection, verdict="REJECTED", reason_code=reason, **base, **extra)
                connection.commit()
                return VoiceRejected(reason, sample)

            if self._locked(connection, user_id, now):
                connection.rollback()
                raise VoiceRejected("VOICE_LOCKED")
            template = self.active_template(connection, user_id)
            if template is None:
                connection.rollback()
                raise VoiceRejected("VOICE_NOT_REGISTERED")
            phrase = connection.execute(
                "SELECT * FROM voice_phrases WHERE challenge_id = ? AND user_id = ?", (challenge_id, user_id)
            ).fetchone()
            if phrase is None or phrase["receipt_digest"] != receipt_digest:
                connection.rollback()
                raise VoiceRejected("VOICE_PHRASE_NOT_ISSUED")
            if now.astimezone(timezone.utc) >= datetime.fromisoformat(phrase["expires_at"]):
                connection.rollback()
                raise VoiceRejected("VOICE_PHRASE_EXPIRED")
            if connection.execute(
                "SELECT 1 FROM voice_samples WHERE challenge_id = ? AND verdict = 'MATCH' AND purpose = 'VERIFY'",
                (challenge_id,),
            ).fetchone():
                connection.rollback()
                raise VoiceRejected("VOICE_ALREADY_CONFIRMED")
            heard = phrase_heard(phrase["phrase"], transcript)
            expected = phrase["phrase"] if phrase["phrase"] == CONFIRM_PHRASE else phrase["phrase"].replace(" ", "")
            extra = {"phrase_expected": expected, "phrase_heard": heard}
            if self._seen(connection, audio_sha256):
                raise reject("VOICE_REPLAYED", **extra)
            if not phrase_spoken(phrase["phrase"], transcript):
                raise reject("VOICE_PHRASE_MISMATCH", **extra)
            similarity = round(cosine(template, embedding), 4)
            if prior_request_audio is not None and prior_pcm is not None:
                prior_similarity = round(cosine(template, prior_embedding), 4)
            if prior_similarity is not None and prior_similarity >= MATCH_THRESHOLD:
                matched = similarity >= CONFIRM_MATCH_THRESHOLD
                # Recorded as the weaker of the two checks.
                similarity = min(similarity, prior_similarity)
            else:
                matched = similarity >= MATCH_THRESHOLD
            if not matched:
                raise reject("VOICE_NO_MATCH", similarity=similarity, **extra)
            sample = self._record(connection, verdict="MATCH", similarity=similarity, **base, **extra)
            connection.commit()
            return sample

    def has_match(self, *, user_id: str, challenge_id: str, receipt_digest: str) -> bool:
        """Whether an unspent voice match exists for this exact challenge and
        receipt. Read-only: the match is spent later by consume()."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM voice_samples
                WHERE challenge_id = ? AND user_id = ? AND verdict = 'MATCH'
                  AND purpose = 'VERIFY' AND receipt_digest = ? AND consumed_at IS NULL
                """,
                (challenge_id, user_id, receipt_digest),
            ).fetchone()
        return row is not None

    def consume(self, *, user_id: str, challenge_id: str, receipt_digest: str, now: datetime) -> dict[str, Any]:
        """Spend the voice match for this exact challenge and receipt, once."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM voice_samples
                WHERE challenge_id = ? AND user_id = ? AND verdict = 'MATCH' AND purpose = 'VERIFY'
                """,
                (challenge_id, user_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise VoiceRejected("VOICE_NOT_VERIFIED")
            if row["receipt_digest"] != receipt_digest:
                connection.rollback()
                raise VoiceRejected("VOICE_RECEIPT_MISMATCH")
            if row["consumed_at"] is not None:
                connection.rollback()
                raise VoiceRejected("VOICE_ALREADY_USED")
            connection.execute(
                "UPDATE voice_samples SET consumed_at = ? WHERE sample_id = ? AND consumed_at IS NULL",
                (_utc(now), row["sample_id"]),
            )
            connection.commit()
            return dict(row)

    def speaker_similarity(self, user_id: str, audio: bytes) -> float | None:
        """For the wake word: how closely this utterance matches the voiceprint.
        None when no voiceprint is registered or no engine is installed."""

        engine = self._engine()
        if engine is None:
            return None
        with self._connect() as connection:
            template = self.active_template(connection, user_id)
        if template is None:
            return None
        try:
            pcm, _, _ = self._prepare(audio)
        except VoiceRejected:
            return None
        return round(cosine(template, engine.embed(pcm)), 4)


def voice_database_path(ledger_path: Path) -> Path:
    ledger_path = Path(ledger_path)
    return ledger_path.with_name(f"{ledger_path.stem}.voice.db")
