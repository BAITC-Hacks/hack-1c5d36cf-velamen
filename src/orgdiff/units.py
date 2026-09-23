"""Реестр подразделений и должностей из раздела «Структура» и сравнение реестров.

Это детерминированная часть: списки подразделений и подчинённости разбираются
кодом, без LLM, поэтому «создано / сохранено / упразднено» всегда воспроизводимо.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Document, Finding, Source
from .textutil import norm, ratio, stems

UNIT_ITEM = re.compile(r"^[а-яa-z][.)]\s*(.+?)\s*\(([А-ЯЁA-Z]{2,}[А-ЯЁA-Z\-]*)\)\.?\s*$")
GENERIC = {"депар", "управ", "отдел", "служб", "центр"}


@dataclass
class Registry:
    units: dict[str, str] = field(default_factory=dict)  # аббревиатура → полное название
    unit_src: dict[str, str] = field(default_factory=dict)  # аббревиатура → пункт
    positions: dict[str, list[str]] = field(default_factory=dict)  # руководитель → должности
    pos_src: dict[str, str] = field(default_factory=dict)


def build_registry(d: Document) -> Registry:
    r = Registry()
    for c in d.chunks:
        h = c.header.lower()
        items = [ln for ln in c.lines[1:] if re.match(r"^[а-яa-z][.)]\s", ln, re.I)]
        if "состоит из" in h:
            for ln in items:
                m = UNIT_ITEM.match(ln)
                if m:
                    r.units[m.group(2)] = m.group(1)
                    r.unit_src[m.group(2)] = c.id
        elif "подчиня" in h:
            head = re.split(r"\s+подчиня", c.header, maxsplit=1)[0].strip()
            r.positions[head] = [re.sub(r"^[а-яa-z][.)]\s*", "", ln, flags=re.I).rstrip(". ") for ln in items]
            r.pos_src[head] = c.id
    return r


def resolve_units(text: str, reg: Registry) -> list[str]:
    """Какие подразделения упомянуты в тексте: по аббревиатуре или по названию.
    «Директоры департаментов» без уточнения — это все подразделения."""
    found = []
    if re.fullmatch(r"(?i)директоры департаментов", text.strip()):
        return list(reg.units)
    words = set(re.findall(r"[А-ЯЁA-Z]{2,}", text))
    st = stems(text)
    for abbr, name in reg.units.items():
        key = stems(name) - GENERIC
        if abbr in words or (key and key <= st):
            found.append(abbr)
    return found


def assign_owners(d: Document, reg: Registry) -> None:
    """Проставляет функциям владельца-подразделение, если его можно определить."""
    for c in d.chunks:
        if c.owner:
            c.owner_units = resolve_units(c.owner, reg)
    for f in d.functions:
        c = d.chunk(f.chunk_id)
        if c and c.owner:
            f.owner_units = list(c.owner_units)
            f.owner = ", ".join(c.owner_units) if c.owner_units else c.owner


def compare_registries(a: Document, ra: Registry, b: Document, rb: Registry) -> list[Finding]:
    out: list[Finding] = []

    def add(type_, title, desc, sources, severity="info"):
        out.append(Finding(id="", type=type_, title=title, description=desc, severity=severity, sources=sources))

    renamed_a, renamed_b = set(), set()
    for x in set(ra.units) - set(rb.units):
        for y in set(rb.units) - set(ra.units):
            if ratio(ra.units[x], rb.units[y]) > 0.8:
                renamed_a.add(x)
                renamed_b.add(y)
                add("unit_renamed", f"{x} → {y}", f"Подразделение переименовано: «{ra.units[x]}» → «{rb.units[y]}».",
                    [Source(a.doc, ra.unit_src[x], ra.units[x]), Source(b.doc, rb.unit_src[y], rb.units[y])], "medium")

    for abbr in rb.units:
        if abbr in ra.units:
            add("unit_retained", abbr, f"Подразделение сохранено: {rb.units[abbr]} ({abbr}).",
                [Source(a.doc, ra.unit_src[abbr], ra.units[abbr]), Source(b.doc, rb.unit_src[abbr], rb.units[abbr])])
        elif abbr not in renamed_b:
            add("unit_created", abbr, f"Создано подразделение: {rb.units[abbr]} ({abbr}).",
                [Source(b.doc, rb.unit_src[abbr], rb.units[abbr])], "medium")
    for abbr in ra.units:
        if abbr not in rb.units and abbr not in renamed_a:
            add("unit_abolished", abbr, f"Подразделение упразднено: {ra.units[abbr]} ({abbr}).",
                [Source(a.doc, ra.unit_src[abbr], ra.units[abbr])], "high")

    # Должности: сравниваем по руководителю (группе подчинения).
    # «Директор проектов ДНМ» у Директора ДНМ = «Директор проектов»: аббревиатуру убираем.
    abbrs = "|".join(map(re.escape, set(ra.units) | set(rb.units))) or "$^"

    def pn(p: str) -> str:
        return norm(re.sub(rf"\b(?:{abbrs})\b", " ", p))

    heads_b = {norm(h): h for h in rb.positions}
    for ha, pos_a in ra.positions.items():
        hb = heads_b.get(norm(ha))
        if hb is None:
            add("role_abolished", ha, f"Упразднена линия подчинения «{ha}» (должности: {', '.join(pos_a)}).",
                [Source(a.doc, ra.pos_src[ha], ha)], "high")
            continue
        pos_b = rb.positions[hb]
        removed = [p for p in pos_a if pn(p) not in {pn(x) for x in pos_b}]
        added = [p for p in pos_b if pn(p) not in {pn(x) for x in pos_a}]
        if removed or added:
            desc = f"Изменён состав должностей у «{hb}»."
            if removed:
                desc += f" Исключены: {', '.join(removed)}."
            if added:
                desc += f" Добавлены: {', '.join(added)}."
            add("positions_changed", hb, desc,
                [Source(a.doc, ra.pos_src[ha], ha), Source(b.doc, rb.pos_src[hb], hb)],
                "high" if removed else "medium")
    heads_a = {norm(h) for h in ra.positions}
    for hb, pos_b in rb.positions.items():
        if norm(hb) not in heads_a:
            add("role_created", hb, f"Новая линия подчинения «{hb}» (должности: {', '.join(pos_b)}).",
                [Source(b.doc, rb.pos_src[hb], hb)], "medium")
    return out
