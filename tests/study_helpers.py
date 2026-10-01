"""Helpers shared by the study-logging tests."""

import csv
import json
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from talk_with_reachy import study_log


SR = 16000


def wait_for_writes() -> None:
    """Block until every study job submitted so far has run."""
    future = study_log.submit(lambda: None)
    if future is not None:
        future.result(timeout=10)


def records(data_dir: Path) -> list[dict[str, object]]:
    """All JSONL records of the single session in ``data_dir``."""
    (path,) = study_log.transcripts_dir(data_dir).glob("*.jsonl")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def csv_rows(data_dir: Path) -> list[dict[str, str]]:
    """All CSV rows of the single session in ``data_dir``."""
    (path,) = study_log.transcripts_dir(data_dir).glob("*.csv")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def tone(key: int, seconds: float) -> NDArray[np.int16]:
    """Audio whose first sample tells ``FakeEmbedder`` which voice it is."""
    return np.full(int(seconds * SR), key, dtype=np.int16)


class FakeEmbedder:
    """Returns a fixed embedding per voice key (the clip's first sample)."""

    def __init__(self, vectors: dict[int, list[float]]) -> None:
        """Map voice keys to embeddings."""
        self.vectors = {k: np.asarray(v, dtype=np.float32) / np.linalg.norm(v) for k, v in vectors.items()}
        self.calls = 0

    def embed(self, samples: NDArray[np.int16], sample_rate: int) -> NDArray[np.float32]:
        """Look up the voice by its key."""
        self.calls += 1
        return self.vectors[int(samples[0])]


def basis(i: int, dim: int = 8) -> list[float]:
    """Return the unit vector along axis ``i``."""
    v = [0.0] * dim
    v[i] = 1.0
    return v
