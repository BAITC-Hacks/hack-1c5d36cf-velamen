"""Нормализация текста и меры похожести."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

CLAUSE_NUM = re.compile(r"(?<![\d.])\d{1,2}(?:\.\d{1,2}){1,2}\.?")
MARKER = re.compile(r"^\s*(?:\d{1,2}(?:\.\d{1,2})*\.?|[а-яa-z]\.|[–\-•])\s*", re.I)
STOP = {"общества", "общество", "обществе", "настоящего", "положения", "положением", "также",
        "которые", "который", "соответствии", "рамках", "числе", "работников", "работники"}


def norm(text: str) -> str:
    """Текст без номеров пунктов, регистра и пунктуации — для сравнения редакций."""
    t = CLAUSE_NUM.sub(" ", text.lower().replace("ё", "е").replace("№", "no"))
    t = re.sub(r"[^\w]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def strip_marker(text: str) -> str:
    return MARKER.sub("", text, count=1).strip()


def stems(text: str) -> set[str]:
    """Грубые «основы» слов (первые 5 букв) — для русского этого хватает для поиска."""
    return {w[:5] for w in norm(text).split() if len(w) > 3 and w not in STOP and not w.isdigit()}


def ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b), autojunk=False).ratio()


def jaccard(a: str, b: str) -> float:
    sa, sb = stems(a), stems(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def similarity(a: str, b: str) -> float:
    return max(ratio(a, b), jaccard(a, b))


def change_fragments(a: str, b: str, ctx: int = 3) -> list[tuple[str, str]]:
    """Только изменённые места с парой слов контекста: [(«…было…», «…стало…»)]."""
    wa, wb = a.split(), b.split()
    out = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        left_a, left_b = wa[max(0, i1 - ctx):i1], wb[max(0, j1 - ctx):j1]
        right_a, right_b = wa[i2:i2 + ctx], wb[j2:j2 + ctx]
        # Квадратные скобки, а не «ёлочки»: ёлочки часто встречаются в самих документах.
        old = " ".join(left_a + (["[" + " ".join(wa[i1:i2]) + "]"] if i2 > i1 else ["[—]"]) + right_a)
        new = " ".join(left_b + (["[" + " ".join(wb[j1:j2]) + "]"] if j2 > j1 else ["[—]"]) + right_b)
        out.append(("…" + old + "…", "…" + new + "…"))
    return out


def word_diff(a: str, b: str) -> str:
    """Пословный diff в виде «[-удалено-] {+добавлено+}»."""
    wa, wb = a.split(), b.split()
    out = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        if op == "equal":
            seg = wa[i1:i2]
            out.append(" ".join(seg) if len(seg) <= 8 else " ".join(seg[:3] + ["…"] + seg[-3:]))
            continue
        if i2 > i1:
            out.append("[-" + " ".join(wa[i1:i2]) + "-]")
        if j2 > j1:
            out.append("{+" + " ".join(wb[j1:j2]) + "+}")
    return " ".join(out)
