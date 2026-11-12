"""SimHash de 64 bits para detección temprana de duplicados (Plan V3.0 §5.8).

Rasgos: 5-gramas de caracteres sobre texto normalizado (minúsculas, sin acentos ni puntuación).
Bandas por distancia de Hamming:
  ≤ DUPLICATE_MAX  → reproducción idéntica: se marca duplicado y se enlaza a la canónica (no se borra)
  ≤ RELATED_MAX    → mismo hecho con otra redacción: se enlaza como relacionada
  >                → distintas
duplicate_similarity = 1 - distancia/64 es una distancia normalizada, no una probabilidad.
Ponderación de rasgos: frecuencia simple. La ponderación TF-IDF con IDF versionado queda pendiente
(requiere corpus histórico); la versión del algoritmo se guarda en cada noticia.
"""
import hashlib
import re
import unicodedata

SIMHASH_VERSION = "simhash64-char5-v1"
DUPLICATE_MAX = 3
RELATED_MAX = 8


def normalize_for_hash(text: str) -> str:
    t = unicodedata.normalize("NFD", text.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def simhash64(text: str) -> int:
    norm = normalize_for_hash(text)
    if len(norm) < 5:
        return 0
    weights: dict[str, int] = {}
    for i in range(len(norm) - 4):
        g = norm[i:i + 5]
        weights[g] = weights.get(g, 0) + 1
    v = [0] * 64
    for gram, w in weights.items():
        h = int.from_bytes(hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(64):
            v[bit] += w if (h >> bit) & 1 else -w
    out = 0
    for bit in range(64):
        if v[bit] > 0:
            out |= 1 << bit
    return out


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def to_hex(h: int) -> str:
    return f"{h:016x}"


def from_hex(s: str) -> int:
    return int(s, 16)
