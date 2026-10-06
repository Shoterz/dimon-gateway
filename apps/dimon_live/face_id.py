"""Face registration at KYC and face confirmation for every transaction.

One account, one face. The face is registered once during KYC onboarding and
kept in its own SQLite database, separate from the ledger. Each transaction is
confirmed by a live face scan from the device camera, bound to that
approval's one-time challenge; the execution gateway spends the match inside
the same step that verifies the device signature, so a scan cannot be reused
for another payment or replayed later.

What is stored: a 128-number face template (not a photo) per account, and for
every scan its verdict, scores, and the SHA-256 of each frame. Camera images
are analysed in memory and discarded.

Liveness: the user turns their head during the scan. A real head turns in 3D,
so the nose moves relative to the eyes; a photo or screen held up to the
camera stays flat, and tilting it barely changes that ratio. Every frame must
show exactly one face, the same face throughout, and no frame may ever have
been seen before.
"""

from __future__ import annotations

import hashlib
import math
import os
import secrets
import sqlite3
import struct
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, Sequence

MATCH_THRESHOLD = 0.40        # cosine similarity to the registered face (SFace)
CONTINUITY_THRESHOLD = 0.25   # every frame must be the same person as the rest
LIVENESS_YAW_RANGE = 0.22     # how far the head must turn during the scan
MIN_FRAMES = 4
MAX_FRAMES = 12
MIN_FACE_FRAMES = 3
MAX_FRAME_BYTES = 400_000
MAX_FAILED_SCANS = 5
LOCKOUT_WINDOW = timedelta(minutes=10)

FACE_SCHEMA = """
CREATE TABLE IF NOT EXISTS face_templates (
  user_id TEXT PRIMARY KEY,
  template BLOB NOT NULL,
  template_sha256 TEXT NOT NULL,
  engine_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PENDING', 'ACTIVE')),
  consent_id TEXT NOT NULL,
  enrolled_at TEXT NOT NULL,
  activated_at TEXT
);
CREATE TABLE IF NOT EXISTS face_scans (
  scan_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  purpose TEXT NOT NULL CHECK(purpose IN ('ENROLL', 'VERIFY')),
  consent_id TEXT,
  challenge_id TEXT,
  receipt_digest TEXT,
  frame_count INTEGER NOT NULL,
  face_frames INTEGER NOT NULL,
  yaw_range REAL,
  similarity REAL,
  verdict TEXT NOT NULL CHECK(verdict IN ('MATCH', 'REGISTERED', 'REJECTED')),
  reason_code TEXT,
  engine_version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  consumed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS face_scans_one_match_per_challenge
  ON face_scans(challenge_id) WHERE verdict = 'MATCH';
CREATE TABLE IF NOT EXISTS face_frames_seen (
  frame_sha256 TEXT PRIMARY KEY,
  scan_id TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS face_scans_no_delete
BEFORE DELETE ON face_scans
BEGIN SELECT RAISE(ABORT, 'FACE_RECORDS_APPEND_ONLY'); END;
"""


class FaceRejected(ValueError):
    def __init__(self, reason_code: str, scan: dict[str, Any] | None = None):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.scan = scan


@dataclass(frozen=True)
class FrameAnalysis:
    face_count: int
    embedding: tuple[float, ...] | None = None
    yaw: float | None = None


class FaceEngine(Protocol):
    version: str

    def analyze(self, frame: bytes) -> FrameAnalysis: ...


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(blob: bytes) -> tuple[float, ...]:
    return struct.unpack(f"<{len(blob) // 4}f", blob)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


# ------------------------------------------------------------------ engine
class OpenCvFaceEngine:
    """YuNet face detector + SFace recognizer (OpenCV Zoo ONNX models)."""

    version = "opencv-yunet-2023mar+sface-2021dec"

    def __init__(self, detector_path: Path, recognizer_path: Path):
        import cv2  # imported here so the rest of the app never needs OpenCV

        self._cv2 = cv2
        self._detector = cv2.FaceDetectorYN.create(str(detector_path), "", (320, 320), 0.85)
        self._recognizer = cv2.FaceRecognizerSF.create(str(recognizer_path), "")

    def analyze(self, frame: bytes) -> FrameAnalysis:
        import numpy as np

        cv2 = self._cv2
        image = cv2.imdecode(np.frombuffer(frame, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise FaceRejected("FACE_FRAME_INVALID")
        height, width = image.shape[:2]
        if max(height, width) > 1280:
            scale = 1280 / max(height, width)
            image = cv2.resize(image, (int(width * scale), int(height * scale)))
            height, width = image.shape[:2]
        self._detector.setInputSize((width, height))
        _, faces = self._detector.detect(image)
        faces = [] if faces is None else [face for face in faces if min(face[2], face[3]) >= 60]
        if len(faces) != 1:
            return FrameAnalysis(face_count=len(faces))
        face = faces[0]
        right_eye, left_eye, nose = face[4:6], face[6:8], face[8:10]
        eye_distance = math.hypot(left_eye[0] - right_eye[0], left_eye[1] - right_eye[1])
        yaw = (nose[0] - (right_eye[0] + left_eye[0]) / 2) / eye_distance if eye_distance else 0.0
        feature = self._recognizer.feature(self._recognizer.alignCrop(image, face)).flatten()
        norm = float(np.linalg.norm(feature)) or 1.0
        return FrameAnalysis(
            face_count=1,
            embedding=tuple(float(value) / norm for value in feature),
            yaw=float(yaw),
        )


def model_paths(env: dict[str, str] | os._Environ = os.environ) -> tuple[Path, Path]:
    home = Path(env.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "DimonGateway" / "models" / "face"
    return (
        Path(env.get("DIMON_FACE_DETECTOR_PATH") or home / "face_detection_yunet_2023mar.onnx"),
        Path(env.get("DIMON_FACE_RECOGNIZER_PATH") or home / "face_recognition_sface_2021dec.onnx"),
    )


@lru_cache(maxsize=1)
def default_engine() -> OpenCvFaceEngine | None:
    """The shared engine, loaded once; None when the models are not installed."""

    detector, recognizer = model_paths()
    if not (detector.is_file() and recognizer.is_file()):
        return None
    try:
        return OpenCvFaceEngine(detector, recognizer)
    except Exception:  # OpenCV missing or the model failed to load
        return None


# ------------------------------------------------------------------ store
@dataclass(frozen=True)
class _Assessment:
    frame_hashes: tuple[str, ...]
    face_frames: int
    yaw_range: float | None
    embeddings: tuple[tuple[float, ...], ...]
    yaws: tuple[float, ...]


class FaceStore:
    """The separate face database: templates, scan records, seen frames."""

    def __init__(self, path: Path, engine: Callable[[], FaceEngine | None]):
        self.path = Path(path)
        self._engine = engine
        self._ready = False   # schema created by this process already

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 3000")
            if not self._ready:
                connection.executescript(FACE_SCHEMA)
                self._ready = True
            yield connection
        finally:
            connection.close()

    def engine(self) -> FaceEngine:
        engine = self._engine()
        if engine is None:
            raise FaceRejected("FACE_ENGINE_UNAVAILABLE")
        return engine

    def status(self, user_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, enrolled_at, activated_at, engine_version FROM face_templates WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        engine = self._engine()
        return {
            "face_registered": bool(row and row["status"] == "ACTIVE"),
            "face_status": row["status"] if row else "NONE",
            "enrolled_at": row["enrolled_at"] if row else None,
            "engine_ready": engine is not None,
            "engine_version": engine.version if engine else None,
        }

    def _assess(self, connection: sqlite3.Connection, frames: Sequence[bytes]) -> _Assessment:
        if not MIN_FRAMES <= len(frames) <= MAX_FRAMES:
            raise FaceRejected("FACE_FRAME_COUNT")
        if any(not frame or len(frame) > MAX_FRAME_BYTES for frame in frames):
            raise FaceRejected("FACE_FRAME_INVALID")
        hashes = tuple(hashlib.sha256(frame).hexdigest() for frame in frames)
        if len(set(hashes)) != len(hashes):
            # A live camera never repeats a frame exactly: this is a still image.
            raise FaceRejected("FACE_NOT_LIVE")
        placeholders = ",".join("?" for _ in hashes)
        if connection.execute(
            f"SELECT 1 FROM face_frames_seen WHERE frame_sha256 IN ({placeholders})", hashes
        ).fetchone():
            raise FaceRejected("FACE_REPLAYED")
        engine = self.engine()
        analyses = [engine.analyze(frame) for frame in frames]
        if any(analysis.face_count > 1 for analysis in analyses):
            raise FaceRejected("FACE_MULTIPLE_FACES")
        faces = [a for a in analyses if a.face_count == 1 and a.embedding is not None and a.yaw is not None]
        if len(faces) < MIN_FACE_FRAMES:
            raise FaceRejected("FACE_NOT_FOUND")
        yaws = tuple(a.yaw for a in faces)
        embeddings = tuple(a.embedding for a in faces)
        yaw_range = max(yaws) - min(yaws)
        return _Assessment(hashes, len(faces), yaw_range, embeddings, yaws)

    @staticmethod
    def _check_live_and_consistent(assessment: _Assessment) -> None:
        if assessment.yaw_range is None or assessment.yaw_range < LIVENESS_YAW_RANGE:
            raise FaceRejected("FACE_NOT_LIVE")
        anchor = assessment.embeddings[min(range(len(assessment.yaws)), key=lambda i: abs(assessment.yaws[i]))]
        if any(cosine(anchor, other) < CONTINUITY_THRESHOLD for other in assessment.embeddings):
            raise FaceRejected("FACE_INCONSISTENT")

    @staticmethod
    def _similarity(template: Sequence[float], assessment: _Assessment) -> float:
        scores = sorted((cosine(template, e) for e in assessment.embeddings), reverse=True)
        return sum(scores[:2]) / len(scores[:2])

    def _record(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: str,
        purpose: str,
        frames_hashes: Sequence[str],
        frame_count: int,
        assessment: _Assessment | None,
        similarity: float | None,
        verdict: str,
        reason_code: str | None,
        now: datetime,
        consent_id: str | None = None,
        challenge_id: str | None = None,
        receipt_digest: str | None = None,
    ) -> dict[str, Any]:
        scan = {
            "scan_id": f"face_scan_{secrets.token_hex(12)}",
            "user_id": user_id,
            "purpose": purpose,
            "consent_id": consent_id,
            "challenge_id": challenge_id,
            "receipt_digest": receipt_digest,
            "frame_count": frame_count,
            "face_frames": assessment.face_frames if assessment else 0,
            "yaw_range": round(assessment.yaw_range, 4) if assessment and assessment.yaw_range is not None else None,
            "similarity": round(similarity, 4) if similarity is not None else None,
            "verdict": verdict,
            "reason_code": reason_code,
            "engine_version": (self._engine() or _NoEngine).version,
            "created_at": _utc(now),
        }
        connection.execute(
            f"INSERT INTO face_scans ({', '.join(scan)}) VALUES ({', '.join('?' for _ in scan)})",
            tuple(scan.values()),
        )
        connection.executemany(
            "INSERT OR IGNORE INTO face_frames_seen (frame_sha256, scan_id) VALUES (?, ?)",
            [(frame_hash, scan["scan_id"]) for frame_hash in frames_hashes],
        )
        return scan

    def _scan(
        self,
        *,
        user_id: str,
        purpose: str,
        frames: Sequence[bytes],
        now: datetime,
        decide: Callable[[sqlite3.Connection, _Assessment], tuple[str, float | None]],
        consent_id: str | None = None,
        challenge_id: str | None = None,
        receipt_digest: str | None = None,
    ) -> dict[str, Any]:
        """Assess the frames, record the scan whatever the outcome, and raise
        FaceRejected (carrying the recorded scan) when it did not pass."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            hashes = tuple(hashlib.sha256(frame).hexdigest() for frame in frames if frame)
            assessment = None
            try:
                if purpose == "VERIFY":
                    failures = connection.execute(
                        """
                        SELECT COUNT(*) FROM face_scans
                        WHERE user_id = ? AND purpose = 'VERIFY' AND verdict = 'REJECTED'
                          AND created_at >= ?
                        """,
                        (user_id, _utc(now - LOCKOUT_WINDOW)),
                    ).fetchone()[0]
                    if failures >= MAX_FAILED_SCANS:
                        raise FaceRejected("FACE_LOCKED")
                assessment = self._assess(connection, frames)
                self._check_live_and_consistent(assessment)
                verdict, similarity = decide(connection, assessment)
                scan = self._record(
                    connection, user_id=user_id, purpose=purpose, frames_hashes=hashes,
                    frame_count=len(frames), assessment=assessment, similarity=similarity,
                    verdict=verdict, reason_code=None, now=now, consent_id=consent_id,
                    challenge_id=challenge_id, receipt_digest=receipt_digest,
                )
                connection.commit()
                return scan
            except FaceRejected as exc:
                if exc.reason_code in {"FACE_ENGINE_UNAVAILABLE", "FACE_LOCKED"}:
                    connection.rollback()
                    raise
                scan = self._record(
                    connection, user_id=user_id, purpose=purpose, frames_hashes=hashes,
                    frame_count=len(frames), assessment=assessment,
                    similarity=getattr(exc, "similarity", None), verdict="REJECTED",
                    reason_code=exc.reason_code, now=now, consent_id=consent_id,
                    challenge_id=challenge_id, receipt_digest=receipt_digest,
                )
                connection.commit()
                raise FaceRejected(exc.reason_code, scan) from None

    def enroll(self, *, user_id: str, consent_id: str, frames: Sequence[bytes], now: datetime) -> dict[str, Any]:
        """Register the account's one face. If a face is already active, a new
        KYC run (e.g. a new device) must show that same face; it is kept."""

        def decide(connection: sqlite3.Connection, assessment: _Assessment) -> tuple[str, float | None]:
            existing = connection.execute(
                "SELECT template, status FROM face_templates WHERE user_id = ?", (user_id,)
            ).fetchone()
            if existing is not None and existing["status"] == "ACTIVE":
                similarity = self._similarity(_unpack(existing["template"]), assessment)
                if similarity < MATCH_THRESHOLD:
                    error = FaceRejected("FACE_DOES_NOT_MATCH_ACCOUNT")
                    error.similarity = similarity  # type: ignore[attr-defined]
                    raise error
                connection.execute(
                    "UPDATE face_templates SET consent_id = ? WHERE user_id = ?", (consent_id, user_id)
                )
                return "REGISTERED", similarity
            frontal = sorted(range(len(assessment.yaws)), key=lambda i: abs(assessment.yaws[i]))[:3]
            template = [sum(assessment.embeddings[i][d] for i in frontal) / len(frontal)
                        for d in range(len(assessment.embeddings[0]))]
            norm = math.sqrt(sum(value * value for value in template)) or 1.0
            blob = _pack([value / norm for value in template])
            connection.execute(
                """
                INSERT INTO face_templates (
                  user_id, template, template_sha256, engine_version, status, consent_id, enrolled_at
                ) VALUES (?, ?, ?, ?, 'PENDING', ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                  template = excluded.template, template_sha256 = excluded.template_sha256,
                  engine_version = excluded.engine_version, consent_id = excluded.consent_id,
                  enrolled_at = excluded.enrolled_at
                """,
                (user_id, blob, hashlib.sha256(blob).hexdigest(), self.engine().version, consent_id, _utc(now)),
            )
            return "REGISTERED", None

        return self._scan(user_id=user_id, purpose="ENROLL", frames=frames, now=now,
                          decide=decide, consent_id=consent_id)

    def enrolled_for_consent(self, user_id: str, consent_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM face_templates WHERE user_id = ? AND consent_id = ?", (user_id, consent_id)
            ).fetchone() is not None

    def activate(self, user_id: str, consent_id: str, now: datetime) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE face_templates SET status = 'ACTIVE', activated_at = COALESCE(activated_at, ?)
                WHERE user_id = ? AND consent_id = ?
                """,
                (_utc(now), user_id, consent_id),
            )

    def verify(
        self, *, user_id: str, challenge_id: str, receipt_digest: str, frames: Sequence[bytes], now: datetime
    ) -> dict[str, Any]:
        """Match a live scan against the registered face for one approval challenge."""

        def decide(connection: sqlite3.Connection, assessment: _Assessment) -> tuple[str, float | None]:
            template = connection.execute(
                "SELECT template FROM face_templates WHERE user_id = ? AND status = 'ACTIVE'", (user_id,)
            ).fetchone()
            if template is None:
                raise FaceRejected("FACE_NOT_REGISTERED")
            if connection.execute(
                "SELECT 1 FROM face_scans WHERE challenge_id = ? AND verdict = 'MATCH'", (challenge_id,)
            ).fetchone():
                raise FaceRejected("FACE_ALREADY_CONFIRMED")
            similarity = self._similarity(_unpack(template["template"]), assessment)
            if similarity < MATCH_THRESHOLD:
                error = FaceRejected("FACE_NO_MATCH")
                error.similarity = similarity  # type: ignore[attr-defined]
                raise error
            return "MATCH", similarity

        return self._scan(user_id=user_id, purpose="VERIFY", frames=frames, now=now, decide=decide,
                          challenge_id=challenge_id, receipt_digest=receipt_digest)

    def consume(self, *, user_id: str, challenge_id: str, receipt_digest: str, now: datetime) -> dict[str, Any]:
        """Spend the face match for this exact challenge and receipt, once."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM face_scans
                WHERE challenge_id = ? AND user_id = ? AND verdict = 'MATCH'
                """,
                (challenge_id, user_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise FaceRejected("FACE_NOT_VERIFIED")
            if row["receipt_digest"] != receipt_digest:
                connection.rollback()
                raise FaceRejected("FACE_RECEIPT_MISMATCH")
            if row["consumed_at"] is not None:
                connection.rollback()
                raise FaceRejected("FACE_ALREADY_USED")
            connection.execute(
                "UPDATE face_scans SET consumed_at = ? WHERE scan_id = ? AND consumed_at IS NULL",
                (_utc(now), row["scan_id"]),
            )
            connection.commit()
            return dict(row)

    def scans(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM face_scans WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (user_id, limit),
                )
            ]


class _NoEngine:
    version = "unavailable"


def face_database_path(ledger_path: Path) -> Path:
    """The face database sits beside its ledger in its own file."""

    ledger_path = Path(ledger_path)
    return ledger_path.with_name(f"{ledger_path.stem}.faces.db")
