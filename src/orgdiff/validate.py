"""Проверка ссылок, которые вернула LLM: пункт существует и цитата в нём действительно есть."""

from __future__ import annotations

from difflib import SequenceMatcher

from .models import Document, Source
from .textutil import norm


def quote_in(quote: str, text: str) -> bool:
    q, t = norm(quote), norm(text)
    if not q:
        return True
    if q in t:
        return True
    m = SequenceMatcher(None, q, t, autojunk=False).find_longest_match(0, len(q), 0, len(t))
    return m.size >= 0.8 * len(q)


def _locate(doc: Document, quote: str) -> str | None:
    """Если модель ошиблась номером, ищем пункт, где цитата есть на самом деле."""
    if len(norm(quote)) < 15:
        return None
    for f in doc.functions:
        if quote_in(quote, f.text):
            return f.id
    for c in doc.chunks:
        if quote_in(quote, c.text):
            return c.id
    return None


def check_source(docs: dict[str, Document], doc: str, clause: str, quote: str = "") -> Source:
    d = docs.get(doc)
    if d is None:
        return Source(doc, clause, quote, verified=False)
    clause = str(clause).strip().rstrip(".")
    text = d.clause_text(clause)
    if text is not None and quote_in(quote, text):
        return Source(doc, clause, quote, verified=True)
    fixed = _locate(d, quote) if quote else None
    if fixed:
        return Source(doc, fixed, quote, verified=True)
    return Source(doc, clause, quote, verified=False)
