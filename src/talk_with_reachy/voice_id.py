"""Voice identification for the Talk with Reachy study.

Each person utterance is turned into a voiceprint (a 192-number speaker
embedding from NVIDIA NeMo TitaNet-S, run locally with sherpa-onnx) and
compared with the voices in ``<data dir>/people/library.json``:

- **Enrolled voices** (``P01``, ``R01``...) are registered by a researcher with
  ``talk-with-reachy-voices enroll``. Their voiceprint stays fixed.
- **Auto voices** (``V001``, ``V002``...) are created when a voice that matches
  nobody speaks for at least ``MIN_NEW_VOICE_S`` seconds. They are recognized
  again later, and a researcher can later link one to a participant ID.

An utterance that is too short, or whose best match is ambiguous, is labeled
``unknown``. Every decision carries a cosine ``match_score`` so the data can be
re-thresholded later, and the audio clip is kept so it can be checked by ear.

All library changes happen on the study worker thread (``study_log.submit``),
including the commands that ``voices_cli`` drops into ``people/requests/``.
"""

from __future__ import annotations
import os
import json
import time
import shutil
import hashlib
import logging
import threading
from typing import Any, Protocol
from pathlib import Path
from datetime import datetime
from dataclasses import field, dataclass

import numpy as np
from numpy.typing import NDArray

from talk_with_reachy import memory, study_log
from talk_with_reachy.voice_files import (
    UNKNOWN,
    MODELS_SUBDIR,
    AUTO_ID_PATTERN,
    LIBRARY_FILENAME,
    REQUEST_LOG_FILENAME,
    valid_id,
    people_dir,
    person_dir,
    requests_dir,
)


logger = logging.getLogger(__name__)

VOICE_ID_ENV = "TALK_WITH_REACHY_VOICE_ID"
MATCH_THRESHOLD_ENV = "TALK_WITH_REACHY_VOICE_MATCH_THRESHOLD"

# Cosine similarity thresholds for TitaNet-S embeddings. In our check, clips of 1 s or
# more scored >= 0.47 against their own speaker and <= 0.34 against others.
DEFAULT_MATCH_THRESHOLD = 0.45
NEW_VOICE_THRESHOLD = 0.30
ADAPT_THRESHOLD = 0.55
MIN_MATCH_S = 1.0
MIN_NEW_VOICE_S = 2.0
AUTO_VOICE_MAX_WEIGHT = 20
ENROLL_DEFAULT_SECONDS = 20.0
ENROLL_EXPIRY_S = 15 * 60.0
ENROLL_KEEP_THRESHOLD = 0.5
CURRENT_SPEAKER_MAX_AGE_S = 120.0
MODEL_RETRY_S = 60.0
REQUEST_POLL_S = 2.0

MODEL_NAME = "nemo_en_titanet_small.onnx"
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx"
)
MODEL_SHA256 = "ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e"


def enabled() -> bool:
    """Return whether voice ID is on: it needs study logging and is on unless the env var is 0."""
    return study_log.logging_enabled() and os.getenv(VOICE_ID_ENV, "1").strip() != "0"


def match_threshold() -> float:
    """Return the cosine score needed to accept a match."""
    try:
        return float(os.getenv(MATCH_THRESHOLD_ENV) or DEFAULT_MATCH_THRESHOLD)
    except ValueError:
        return DEFAULT_MATCH_THRESHOLD


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize(vector: NDArray[np.float32]) -> NDArray[np.float32]:
    norm = float(np.linalg.norm(vector))
    return (vector / norm).astype(np.float32) if norm > 0 else vector.astype(np.float32)


class Embedder(Protocol):
    """Turns audio into a speaker embedding."""

    def embed(self, samples: NDArray[np.int16], sample_rate: int) -> NDArray[np.float32]:
        """Return a unit-length speaker embedding for ``samples``."""
        ...


class SherpaEmbedder:
    """TitaNet-S speaker embeddings through sherpa-onnx."""

    def __init__(self, model_path: Path, num_threads: int = 2) -> None:
        """Load the ONNX model."""
        import sherpa_onnx

        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(model_path), num_threads=num_threads, provider="cpu"
        )
        if not config.validate():
            raise RuntimeError(f"Invalid speaker model at {model_path}")
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)

    def embed(self, samples: NDArray[np.int16], sample_rate: int) -> NDArray[np.float32]:
        """Return a unit-length embedding."""
        stream = self._extractor.create_stream()
        stream.accept_waveform(sample_rate, samples.astype(np.float32) / 32768.0)
        stream.input_finished()
        return _normalize(np.asarray(self._extractor.compute(stream), dtype=np.float32))


def ensure_model(models_dir: Path) -> Path:
    """Download the speaker model once, verify its checksum, and return its path."""
    import httpx

    path = models_dir / MODEL_NAME
    if path.exists():
        return path
    models_dir.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    digest = hashlib.sha256()
    with httpx.stream("GET", MODEL_URL, follow_redirects=True, timeout=60.0) as response:
        response.raise_for_status()
        with partial.open("wb") as handle:
            for chunk in response.iter_bytes():
                digest.update(chunk)
                handle.write(chunk)
    if digest.hexdigest() != MODEL_SHA256:
        partial.unlink(missing_ok=True)
        raise RuntimeError("Downloaded speaker model failed its checksum")
    partial.replace(path)
    return path


@dataclass
class Voice:
    """One known voice."""

    id: str
    kind: str  # "enrolled" or "auto"
    embedding: NDArray[np.float32]
    name: str = ""
    count: int = 0
    created: str = field(default_factory=_now_iso)
    last_seen: str = ""
    aliases: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        """Serialize for library.json."""
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "count": self.count,
            "created": self.created,
            "last_seen": self.last_seen,
            "aliases": self.aliases,
            "embedding": [round(float(x), 5) for x in self.embedding],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Voice:
        """Load from library.json."""
        return cls(
            id=str(data["id"]),
            kind=str(data.get("kind", "auto")),
            embedding=_normalize(np.asarray(data["embedding"], dtype=np.float32)),
            name=str(data.get("name", "")),
            count=int(data.get("count", 0)),
            created=str(data.get("created", "")),
            last_seen=str(data.get("last_seen", "")),
            aliases=[str(a) for a in data.get("aliases", [])],
        )


class VoiceLibrary:
    """The known voices, saved in ``people/library.json``."""

    def __init__(self, path: Path) -> None:
        """Load the library if it exists."""
        self.path = path
        self.voices: dict[str, Voice] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data.get("voices", []):
                voice = Voice.from_json(item)
                self.voices[voice.id] = voice
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            logger.exception("Voice library %s is unreadable; starting a new one (the old file is kept)", path)
            path.replace(path.with_suffix(f".unreadable-{int(time.time())}.json"))

    def save(self) -> None:
        """Write the library atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": 1, "model": MODEL_NAME, "voices": [v.to_json() for v in self.voices.values()]}
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def scores(self, embedding: NDArray[np.float32]) -> dict[str, float]:
        """Cosine similarity of ``embedding`` to every known voice."""
        return {vid: float(np.dot(embedding, v.embedding)) for vid, v in self.voices.items()}

    def best_match(
        self, embedding: NDArray[np.float32], kinds: tuple[str, ...] = ("enrolled", "auto")
    ) -> tuple[str | None, float]:
        """Return the closest voice of the given kinds and its score."""
        candidates = {vid: s for vid, s in self.scores(embedding).items() if self.voices[vid].kind in kinds}
        if not candidates:
            return None, -1.0
        best = max(candidates, key=lambda vid: candidates[vid])
        return best, candidates[best]

    def next_auto_id(self) -> str:
        """Return the next unused ``V###`` label, never reusing one that was merged away."""
        used = set(self.voices)
        for voice in self.voices.values():
            used.update(voice.aliases)
        numbers = [int(vid[1:]) for vid in used if AUTO_ID_PATTERN.match(vid)]
        return f"V{(max(numbers) + 1) if numbers else 1:03d}"

    def create_auto(self, embedding: NDArray[np.float32]) -> Voice:
        """Add a new auto voice."""
        voice = Voice(id=self.next_auto_id(), kind="auto", embedding=embedding, count=1, last_seen=_now_iso())
        self.voices[voice.id] = voice
        self.save()
        return voice

    def register(self, speaker_id: str, embedding: NDArray[np.float32], name: str) -> Voice:
        """Add or replace an enrolled voice."""
        existing = self.voices.get(speaker_id)
        voice = Voice(
            id=speaker_id,
            kind="enrolled",
            embedding=embedding,
            name=name or (existing.name if existing else ""),
            count=existing.count if existing else 0,
            created=existing.created if existing else _now_iso(),
            last_seen=existing.last_seen if existing else "",
            aliases=existing.aliases if existing else [],
        )
        self.voices[speaker_id] = voice
        self.save()
        return voice

    def seen(self, speaker_id: str, embedding: NDArray[np.float32] | None = None) -> None:
        """Count one more utterance; let auto voices drift toward confident new samples."""
        voice = self.voices[speaker_id]
        if embedding is not None and voice.kind == "auto":
            weight = min(voice.count, AUTO_VOICE_MAX_WEIGHT)
            voice.embedding = _normalize(voice.embedding * weight + embedding)
        voice.count += 1
        voice.last_seen = _now_iso()
        self.save()


@dataclass(frozen=True)
class Assignment:
    """Who an utterance was attributed to, and how."""

    speaker_id: str = UNKNOWN
    match_score: float | None = None
    status: str = "unavailable"


@dataclass
class _Enrollment:
    speaker_id: str
    name: str
    target_s: float
    expires_mono: float
    parts: list[tuple[NDArray[np.float32], float]] = field(default_factory=list)


class VoiceIdentifier:
    """Assigns speaker IDs and maintains the library. Not thread-safe: use from the study worker."""

    def __init__(self, data_dir: Path, embedder: Embedder | None = None) -> None:
        """Load the library. Without an embedder, utterances are ``unknown`` until ``set_embedder``."""
        self.data_dir = data_dir
        self.library = VoiceLibrary(people_dir(data_dir) / LIBRARY_FILENAME)
        self._embedder = embedder
        self._enrollment: _Enrollment | None = None
        self._current: tuple[str, float] | None = None

    @property
    def ready(self) -> bool:
        """Whether the speaker model is loaded."""
        return self._embedder is not None

    def set_embedder(self, embedder: Embedder) -> None:
        """Install the speaker model once it has loaded."""
        self._embedder = embedder

    def current_speaker(self, max_age_s: float = CURRENT_SPEAKER_MAX_AGE_S) -> str | None:
        """Return the last confidently identified speaker, if heard within ``max_age_s``."""
        current = self._current
        if current is None or time.monotonic() - current[1] > max_age_s:
            return None
        return current[0] if current[0] in self.library.voices or self._enrolling(current[0]) else None

    def _enrolling(self, speaker_id: str) -> bool:
        return self._enrollment is not None and self._enrollment.speaker_id == speaker_id

    def _set_current(self, speaker_id: str) -> None:
        self._current = (speaker_id, time.monotonic())

    def assign(self, samples: NDArray[np.int16], sample_rate: int) -> Assignment:
        """Attribute one person utterance to a voice, updating the library as needed."""
        if self._embedder is None:
            return Assignment(status="model_not_ready")
        duration = len(samples) / sample_rate if sample_rate else 0.0
        if duration < MIN_MATCH_S:
            return Assignment(status="too_short")
        embedding = self._embedder.embed(samples, sample_rate)

        enrollment = self._active_enrollment()
        if enrollment is not None:
            other, other_score = self.library.best_match(embedding, kinds=("enrolled",))
            if other is None or other == enrollment.speaker_id or other_score < ADAPT_THRESHOLD:
                return self._add_enrollment_part(enrollment, embedding, duration)

        threshold = match_threshold()
        best, score = self.library.best_match(embedding)
        if best is not None and score >= threshold:
            confident = score >= ADAPT_THRESHOLD and duration >= 1.5
            self.library.seen(best, embedding if confident else None)
            self._set_current(best)
            return Assignment(best, round(score, 3), "matched")
        if score < NEW_VOICE_THRESHOLD and duration >= MIN_NEW_VOICE_S:
            voice = self.library.create_auto(embedding)
            self._set_current(voice.id)
            study_log.record_event("new_voice", speaker_id=voice.id)
            return Assignment(voice.id, round(score, 3) if best else None, "new_voice")
        return Assignment(UNKNOWN, round(score, 3) if best else None, "uncertain")

    # ── Enrollment ────────────────────────────────────────────────

    def _active_enrollment(self) -> _Enrollment | None:
        enrollment = self._enrollment
        if enrollment is not None and time.monotonic() > enrollment.expires_mono:
            heard = sum(d for _, d in enrollment.parts)
            study_log.record_event(
                "voice_enrollment_expired", speaker_id=enrollment.speaker_id, heard_s=round(heard, 1)
            )
            self._enrollment = None
            return None
        return enrollment

    def _add_enrollment_part(
        self, enrollment: _Enrollment, embedding: NDArray[np.float32], duration: float
    ) -> Assignment:
        enrollment.parts.append((embedding, duration))
        self._set_current(enrollment.speaker_id)
        total = sum(d for _, d in enrollment.parts)
        if total < enrollment.target_s:
            return Assignment(enrollment.speaker_id, None, "enrolling")

        vectors = np.stack([e for e, _ in enrollment.parts])
        durations = np.array([d for _, d in enrollment.parts])
        mean = _normalize((vectors * durations[:, None]).sum(axis=0))
        keep = vectors @ mean >= ENROLL_KEEP_THRESHOLD
        kept_s = float(durations[keep].sum())
        if kept_s < 0.75 * enrollment.target_s:
            # Too many clips disagree (several people talking?); keep listening.
            return Assignment(enrollment.speaker_id, None, "enrolling")
        voiceprint = _normalize((vectors[keep] * durations[keep][:, None]).sum(axis=0))
        self.library.register(enrollment.speaker_id, voiceprint, enrollment.name)
        study_log.record_event(
            "voice_enrolled",
            speaker_id=enrollment.speaker_id,
            name=enrollment.name,
            used_s=round(kept_s, 1),
            dropped_clips=int((~keep).sum()),
        )
        self._enrollment = None
        return Assignment(enrollment.speaker_id, None, "enrolled")

    # ── Commands from talk-with-reachy-voices ─────────────────────

    def process_requests(self) -> int:
        """Apply queued commands in the order they were made; return how many were handled."""
        folder = requests_dir(self.data_dir)
        handled = 0
        for path in sorted(folder.glob("*.json")) if folder.exists() else []:
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
                result = self._apply_request(request)
            except Exception as e:  # a bad file must not block the others
                request, result = {"file": path.name}, f"error: {e}"
            self._log_request(request, result)
            path.unlink(missing_ok=True)
            handled += 1
        return handled

    def _log_request(self, request: dict[str, Any], result: str) -> None:
        log_path = people_dir(self.data_dir) / REQUEST_LOG_FILENAME
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"time": _now_iso(), **request, "result": result}, ensure_ascii=False) + "\n")
        logger.info("Voice command %s: %s", request.get("action"), result)

    def _apply_request(self, request: dict[str, Any]) -> str:
        action = request.get("action")
        if action == "enroll":
            speaker_id = valid_id(request.get("id"), allow_auto=False)
            seconds = float(request.get("seconds") or ENROLL_DEFAULT_SECONDS)
            self._enrollment = _Enrollment(
                speaker_id, str(request.get("name") or ""), seconds, time.monotonic() + ENROLL_EXPIRY_S
            )
            study_log.record_event("voice_enrollment_started", speaker_id=speaker_id, seconds=seconds)
            return f"listening for {seconds:.0f} s of {speaker_id}'s speech"
        if action == "cancel":
            cancelled = self._enrollment.speaker_id if self._enrollment else None
            self._enrollment = None
            return f"cancelled enrollment of {cancelled}" if cancelled else "no enrollment was active"
        if action == "rename":
            voice = self._voice(request.get("id"))
            voice.name = str(request.get("name") or "")
            self.library.save()
            study_log.record_event("voice_renamed", speaker_id=voice.id, name=voice.name)
            return f"{voice.id} is now named {voice.name!r}"
        if action == "link":
            return self._link(request)
        if action == "delete":
            voice = self._voice(request.get("id"))
            del self.library.voices[voice.id]
            self.library.save()
            shutil.rmtree(person_dir(self.data_dir, voice.id), ignore_errors=True)
            if self._enrolling(voice.id):
                self._enrollment = None
            if self._current and self._current[0] == voice.id:
                self._current = None
            study_log.record_event("voice_deleted", speaker_id=voice.id)
            return f"deleted {voice.id} and its memory"
        raise ValueError(f"unknown action {action!r}")

    def _voice(self, speaker_id: Any) -> Voice:
        voice = self.library.voices.get(str(speaker_id))
        if voice is None:
            raise ValueError(f"no voice called {speaker_id!r}")
        return voice

    def _link(self, request: dict[str, Any]) -> str:
        source = self._voice(request.get("source"))
        target_id = valid_id(request.get("target"), allow_auto=True)
        name = str(request.get("name") or "")
        if target_id == source.id:
            raise ValueError("source and target are the same voice")
        target = self.library.voices.get(target_id)
        source_memory = person_dir(self.data_dir, source.id)
        target_memory = person_dir(self.data_dir, target_id)
        if target is None:
            # Give the voice a researcher-assigned ID; it is now a known participant.
            del self.library.voices[source.id]
            source.aliases.append(source.id)
            source.id, source.kind = target_id, "enrolled"
            source.name = name or source.name
            self.library.voices[target_id] = source
            if source_memory.exists() and not target_memory.exists():
                source_memory.rename(target_memory)
        else:
            if source.kind == "auto" and target.kind == "auto":
                target.embedding = _normalize(target.embedding * target.count + source.embedding * source.count)
            target.count += source.count
            target.aliases.extend([source.id, *source.aliases])
            target.name = name or target.name
            del self.library.voices[source.id]
            for fact in reversed(memory.list_memory_facts(source_memory)):
                memory.add_memory_fact(target_memory, fact.text)
            shutil.rmtree(source_memory, ignore_errors=True)
        self.library.save()
        if self._current and self._current[0] == source.id:
            self._current = (target_id, self._current[1])
        study_log.record_event("voice_linked", source=request.get("source"), target=target_id)
        return f"{request.get('source')} is now {target_id}"

    # ── What Reachy is told ───────────────────────────────────────

    def identity_note(self, speaker_id: str) -> str | None:
        """System note telling the model who is speaking and what it remembers about them."""
        voice = self.library.voices.get(speaker_id)
        enrolling = self._enrolling(speaker_id)
        if voice is None and not enrolling:
            return None
        if enrolling or (voice is not None and voice.kind == "enrolled"):
            name = voice.name if voice is not None else (self._enrollment.name if self._enrollment else "")
            who = f"{name} (ID {speaker_id})" if name else f"participant {speaker_id}"
            lines = [f"Voice ID: the person speaking now is probably {who}."]
        elif voice is not None and voice.count > 1:
            lines = [
                f"Voice ID: the person speaking now has a voice you have heard before, labeled {speaker_id}. "
                "You do not know their name unless your memories below say it."
            ]
        else:
            lines = [
                f"Voice ID: the person speaking now has a voice you have not heard before (labeled {speaker_id})."
            ]
        facts = memory.list_memory_facts(person_dir(self.data_dir, speaker_id))
        if facts:
            lines.append("What you remember about this person:")
            lines.extend(f"- {fact.text}" for fact in facts)
        else:
            lines.append("You have no saved memories about this person.")
        return "\n".join(lines)


# ── Module-level lifecycle ────────────────────────────────────────

_identifier: VoiceIdentifier | None = None
_stop_event: threading.Event | None = None
_threads: list[threading.Thread] = []


def identifier() -> VoiceIdentifier | None:
    """Return the running identifier, if voice ID is on."""
    return _identifier


def status_for_header() -> dict[str, Any]:
    """Voice ID settings for the session_start record."""
    if not enabled():
        return {"enabled": False}
    return {"enabled": True, "model": MODEL_NAME, "match_threshold": match_threshold()}


def current_speaker() -> str | None:
    """Return the person Reachy is most likely talking to now, if known."""
    ident = _identifier
    return ident.current_speaker() if ident is not None else None


def current_person_dir() -> Path | None:
    """Memory folder of the current speaker, or None."""
    ident = _identifier
    speaker_id = current_speaker()
    if ident is None or speaker_id is None:
        return None
    return person_dir(ident.data_dir, speaker_id)


def start(data_dir: Path) -> None:
    """Load the library, then load the model and watch for commands in the background."""
    global _identifier, _stop_event, _threads
    if not enabled():
        return
    _identifier = VoiceIdentifier(data_dir)
    _stop_event = threading.Event()
    _threads = [
        threading.Thread(target=_load_model, args=(_identifier, _stop_event), daemon=True, name="voice-model"),
        threading.Thread(target=_watch_requests, args=(_identifier, _stop_event), daemon=True, name="voice-requests"),
    ]
    for thread in _threads:
        thread.start()


def stop() -> None:
    """Stop the background threads. The identifier stays usable for utterances still queued."""
    global _stop_event, _threads
    if _stop_event is not None:
        _stop_event.set()
    for thread in _threads:
        thread.join(timeout=1.0)
    _stop_event, _threads = None, []


def clear() -> None:
    """Drop the identifier once the session's last utterance has been written."""
    global _identifier
    _identifier = None


def _load_model(ident: VoiceIdentifier, stop_event: threading.Event) -> None:
    reported = False
    while not stop_event.is_set():
        try:
            path = ensure_model(ident.data_dir / MODELS_SUBDIR)
            embedder = SherpaEmbedder(path)
        except Exception as e:
            if not reported:
                logger.warning("Voice ID model unavailable, retrying every %.0f s: %s", MODEL_RETRY_S, e)
                study_log.record_event("voice_model_unavailable", error=f"{type(e).__name__}: {e}")
                reported = True
            stop_event.wait(MODEL_RETRY_S)
            continue
        study_log.submit(ident.set_embedder, embedder)
        study_log.record_event("voice_model_ready", model=MODEL_NAME)
        logger.info("Voice ID model ready (%s)", path)
        return


def _watch_requests(ident: VoiceIdentifier, stop_event: threading.Event) -> None:
    folder = requests_dir(ident.data_dir)
    while not stop_event.is_set():
        if folder.exists() and any(folder.glob("*.json")):
            future = study_log.submit(ident.process_requests)
            if future is not None:
                future.result()
        stop_event.wait(REQUEST_POLL_S)
