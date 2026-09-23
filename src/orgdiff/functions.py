"""Сопоставление функций двух редакций: сохранена / изменена / перенесена / кандидат в потерю / новая."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .models import Document, Function
from .textutil import norm, similarity, strip_marker

FUZZY_THRESHOLD = 0.72


@dataclass
class FunctionMatch:
    status: str  # retained | modified | moved | lost_candidate | new
    a: Function | None
    b: Function | None
    similarity: float = 1.0


def same_owner(x: Function, y: Function) -> bool:
    """Владелец тот же — или функция закреплена за более широкой группой, куда он входит."""
    if x.owner_units and y.owner_units:
        return set(x.owner_units) <= set(y.owner_units)
    ox, oy = norm(x.owner), norm(y.owner)  # «Куратор проверки» ≈ «Куратор»
    return ox == oy or ox.startswith(oy + " ") or oy.startswith(ox + " ")


def _key(f: Function) -> str:
    return norm(strip_marker(f.text))


def match_functions(a: Document, b: Document) -> list[FunctionMatch]:
    """Четыре прохода, от самого надёжного к менее надёжному:
    дословно у того же владельца → дословно у другого → похоже у того же → похоже у другого.
    Так общие формулировки («выполняет прочие поручения») не «перетекают» между владельцами.
    """
    matched: dict[int, tuple[Function, float]] = {}  # id(a) → (b, similarity)
    used_b: set[int] = set()

    def run(cond: Callable[[Function, Function], bool], fuzzy: bool) -> None:
        rest_a = [f for f in a.functions if id(f) not in matched]
        rest_b = [g for g in b.functions if id(g) not in used_b]
        if not fuzzy:
            for f in rest_a:
                g = next((g for g in rest_b if id(g) not in used_b and _key(g) == _key(f) and cond(f, g)), None)
                if g:
                    matched[id(f)] = (g, 1.0)
                    used_b.add(id(g))
            return
        scores = sorted(
            ((similarity(strip_marker(f.text), strip_marker(g.text)), i, j)
             for i, f in enumerate(rest_a) for j, g in enumerate(rest_b) if cond(f, g)),
            key=lambda t: t[0], reverse=True,
        )
        for s, i, j in scores:
            if s < FUZZY_THRESHOLD:
                break
            f, g = rest_a[i], rest_b[j]
            if id(f) in matched or id(g) in used_b:
                continue
            matched[id(f)] = (g, s)
            used_b.add(id(g))

    run(same_owner, fuzzy=False)
    run(lambda f, g: True, fuzzy=False)
    run(same_owner, fuzzy=True)
    run(lambda f, g: True, fuzzy=True)

    out: list[FunctionMatch] = []
    for f in a.functions:
        if id(f) not in matched:
            out.append(FunctionMatch("lost_candidate", f, None, 0.0))
            continue
        g, s = matched[id(f)]
        if not same_owner(f, g):
            status = "moved"
        else:
            status = "retained" if s == 1.0 else "modified"
        out.append(FunctionMatch(status, f, g, s))
    out.extend(FunctionMatch("new", None, g, 0.0) for g in b.functions if id(g) not in used_b)
    return out


def search_functions(doc: Document, query: str, k: int = 5) -> list[tuple[float, Function]]:
    """Поиск функций по смыслу (грубо, по словам) — инструмент агента."""
    scored = [(similarity(query, strip_marker(f.text)), f) for f in doc.functions]
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored[:k]
