"""Выравнивание пунктов двух редакций по содержанию, а не по номеру.

Нумерация между редакциями сдвигается (удалили 5.3 — все следующие съехали),
поэтому номер хранится только как метаданные.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from .models import Chunk, Document
from .textutil import norm, ratio, word_diff

PAIR_THRESHOLD = 0.45


@dataclass
class Pair:
    id: str
    status: str  # same | changed | added | deleted
    a: Chunk | None
    b: Chunk | None
    similarity: float = 1.0
    diff: str = ""

    @property
    def renumbered(self) -> bool:
        return bool(self.a and self.b and self.a.id != self.b.id)


def align(a: Document, b: Document) -> list[Pair]:
    ka = [norm(c.text) for c in a.chunks]
    kb = [norm(c.text) for c in b.chunks]
    pairs: list[Pair] = []
    sm = SequenceMatcher(None, ka, kb, autojunk=False)
    for op, a1, a2, b1, b2 in sm.get_opcodes():
        if op == "equal":
            for i, j in zip(range(a1, a2), range(b1, b2)):
                pairs.append(Pair("", "same", a.chunks[i], b.chunks[j]))
            continue
        pairs.extend(_pair_block(a.chunks[a1:a2], b.chunks[b1:b2]))
    for n, p in enumerate(pairs, 1):
        p.id = f"P{n}"
    return pairs


def _pair_block(ca: list[Chunk], cb: list[Chunk]) -> list[Pair]:
    """Внутри изменённого блока жадно сопоставляем самые похожие пункты."""
    scores = sorted(((ratio(x.text, y.text), i, j) for i, x in enumerate(ca) for j, y in enumerate(cb)), reverse=True)
    used_a, used_b, matched = set(), set(), {}
    for s, i, j in scores:
        if s < PAIR_THRESHOLD:
            break
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        matched[i] = (j, s)
    out: list[Pair] = []
    for i, x in enumerate(ca):
        if i in matched:
            j, s = matched[i]
            y = cb[j]
            status = "same" if norm(x.text) == norm(y.text) else "changed"
            out.append(Pair("", status, x, y, s, "" if status == "same" else word_diff(x.text, y.text)))
        else:
            out.append(Pair("", "deleted", x, None, 0.0))
    for j, y in enumerate(cb):
        if j not in used_b:
            out.append(Pair("", "added", None, y, 0.0))
    return out
