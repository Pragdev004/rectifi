"""Hashed character n-gram vectors. Pure: standard library + numpy, NO langchain, no model, no network.

Two short texts that share most of their letters ("swiggy order" and "swiggy instamart") get a high cosine, so the
classifier can find "the payment that looks like this one" without any embedding model.

    vectorize("swiggy order")            one unit-length float32 vector (or all zeros for an empty text)
    vectorize_many(["a", "b"])           a matrix, one row per text
    top_matches(matrix, vector, k)       the k best rows as [(row index, cosine)], best first
"""

from __future__ import annotations

import zlib

import numpy as np

DIM = 1024               # slots in the hash space; collisions are harmless for short merchant names
NGRAM_MIN, NGRAM_MAX = 3, 5


def ngrams(text: str, lo: int = NGRAM_MIN, hi: int = NGRAM_MAX) -> list[str]:
    """Character n-grams of the text, padded with one space on each side so word starts and ends count.
    'upi' -> ' up', 'upi', 'pi ', ' upi', 'upi ', ' upi '."""
    cleaned = " ".join((text or "").lower().split())
    if not cleaned:
        return []
    padded = f" {cleaned} "
    grams: list[str] = []
    for n in range(lo, hi + 1):
        grams.extend(padded[i:i + n] for i in range(len(padded) - n + 1))
    return grams


def vectorize(text: str, dim: int = DIM) -> np.ndarray:
    """A unit-length vector of hashed n-gram counts. The same text always gives the same vector (crc32, not Python's
    randomised hash())."""
    vec = np.zeros(dim, dtype=np.float32)
    for gram in ngrams(text):
        vec[zlib.crc32(gram.encode("utf-8")) % dim] += 1.0
    norm = float(np.linalg.norm(vec))
    if norm:
        vec /= norm
    return vec


def vectorize_many(texts: list[str], dim: int = DIM) -> np.ndarray:
    if not texts:
        return np.zeros((0, dim), dtype=np.float32)
    return np.vstack([vectorize(t, dim) for t in texts])


def top_matches(matrix: np.ndarray, vector: np.ndarray, k: int = 5) -> list[tuple[int, float]]:
    """The k rows of `matrix` closest to `vector` by cosine (both are unit length, so it is a dot product)."""
    if matrix.shape[0] == 0 or not np.any(vector):
        return []
    scores = matrix @ vector
    k = min(k, scores.shape[0])
    best = np.argpartition(-scores, k - 1)[:k]
    best = best[np.argsort(-scores[best], kind="stable")]
    return [(int(i), float(scores[i])) for i in best]
