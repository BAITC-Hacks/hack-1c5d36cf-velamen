"""Сквозной сценарий: два файла → нарезка → реестры → выравнивание → функции → LLM → находки."""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .align import Pair, align
from .functions import FunctionMatch, match_functions
from .models import Document, Finding, Source
from .parser import parse_file
from .textutil import strip_marker
from .units import Registry, assign_owners, build_registry, compare_registries
from .validate import check_source


@dataclass
class Result:
    a: Document
    b: Document
    reg_a: Registry
    reg_b: Registry
    pairs: list[Pair]
    matches: list[FunctionMatch]
    findings: list[Finding] = field(default_factory=list)
    change_notes: dict[str, dict] = field(default_factory=dict)  # pair_id → {kind, summary}
    lost_checks: dict[str, dict] = field(default_factory=dict)  # clause r8 → вердикт агента
    summary: str = ""  # одна строка с цифрами — для командной строки
    key_points: list[str] = field(default_factory=list)  # главное от LLM, списком
    recommendations: list[dict] = field(default_factory=list)
    llm_used: bool = False
    llm_calls: int = 0
    model: str = ""
    warnings: list[str] = field(default_factory=list)


def run(path_a: str | Path, path_b: str | Path, use_llm: bool | None = None, max_lost: int = 30,
        progress: Callable[[str], None] = lambda s: None) -> Result:
    progress("Разбор документов")
    a, b = parse_file(path_a, "old"), parse_file(path_b, "new")
    ra, rb = build_registry(a), build_registry(b)
    assign_owners(a, ra)
    assign_owners(b, rb)

    progress("Сравнение структуры и пунктов")
    res = Result(a, b, ra, rb, align(a, b), match_functions(a, b))
    if not ra.units and not rb.units:
        res.warnings.append("В документах не найден перечень подразделений («… состоит из …»).")
    findings = compare_registries(a, ra, b, rb)
    findings += _function_findings(res)

    if use_llm is None:
        use_llm = bool(os.environ.get("OPENAI_API_KEY"))
    llm = None
    if use_llm:
        from .llm import LLM
        llm = LLM()
        res.llm_used, res.model = True, llm.model
        findings += _llm_findings(res, llm, max_lost, progress)
    else:
        findings += _offline_lost(res)
        res.warnings.append("LLM не использовалась: смысловая классификация, проверка потерь, "
                            "дубли и конфликты интересов не выполнены.")

    order = {"high": 0, "medium": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f.severity, 3))
    for n, f in enumerate(findings, 1):
        f.id = f"F{n}"
    res.findings = findings

    progress("Итоговый вывод")
    if llm:
        from .llm import conclusion

        brief = [{"id": f.id, "type": f.type, "severity": f.severity, "description": f.description}
                 for f in findings if f.type != "unit_retained"]
        concl = conclusion(llm, brief)
        ids = {f.id for f in findings}
        res.key_points = [str(p) for p in concl.get("key_points", []) if p]
        res.recommendations = [
            {"text": r.get("text", ""), "refs": [x for x in r.get("refs", []) if x in ids]}
            for r in concl.get("recommendations", []) if r.get("text")
        ]
        res.llm_calls = llm.calls
    res.summary = _template_summary(res)
    return res


def _src(doc: Document, clause: str, text: str) -> Source:
    return Source(doc.doc, clause, text[:160])


def _function_findings(res: Result) -> list[Finding]:
    out = []
    moved: dict[tuple[str, str], list[FunctionMatch]] = defaultdict(list)
    new: dict[str, list[FunctionMatch]] = defaultdict(list)
    for m in res.matches:
        if m.status == "moved":
            moved[(m.a.owner, m.b.owner)].append(m)
        elif m.status == "new":
            new[m.b.owner].append(m)
    for (oa, ob), ms in moved.items():
        srcs = []
        for m in ms:
            srcs += [_src(res.a, m.a.id, m.a.text), _src(res.b, m.b.id, m.b.text)]
        out.append(Finding("", "function_moved", f"{oa} → {ob}",
                           f"{len(ms)} функц. перешли от «{oa}» к «{ob}».", "medium", srcs))
    for owner, ms in new.items():
        out.append(Finding("", "function_new", f"Новые функции: {owner}",
                           f"У «{owner}» появилось {len(ms)} новых функц.: "
                           + "; ".join(m.b.text[:80] for m in ms[:5]), "info",
                           [_src(res.b, m.b.id, m.b.text) for m in ms]))
    return out


def _offline_lost(res: Result) -> list[Finding]:
    return [Finding("", "function_lost_candidate", _quote(m.a.text),
                    f"Была у: {m.a.owner}. Похожей функции в новой редакции не найдено — проверьте вручную "
                    "или включите LLM.", "medium", [_src(res.a, m.a.id, m.a.text)])
            for m in res.matches if m.status == "lost_candidate"]


def _quote(text: str, n: int = 160) -> str:
    t =strip_marker(text).rstrip(";:.")
    return "«" + (t if len(t) <= n else t[: n - 1].rsplit(" ", 1)[0] + "…") + "»"


def _llm_findings(res: Result, llm, max_lost: int, progress) -> list[Finding]:
    from . import llm as L

    docs = {res.a.doc: res.a, res.b.doc: res.b}
    out: list[Finding] = []

    progress("LLM: смысл изменений пунктов")
    res.change_notes = L.classify_changes(llm, res.pairs)
    for p in res.pairs:
        note = res.change_notes.get(p.id)
        if note and note.get("kind") == "substantive":
            out.append(Finding("", "clause_substantive", f"Изменён пункт {p.a.id} → {p.b.id}",
                               note.get("summary", ""), "medium",
                               [_src(res.a, p.a.id, p.a.header), _src(res.b, p.b.id, p.b.header)], "llm"))

    progress("Агент: проверка потерь функций")
    lost = [m for m in res.matches if m.status == "lost_candidate"]
    if len(lost) > max_lost:
        res.warnings.append(f"Проверено {max_lost} из {len(lost)} кандидатов в потерю (лимит).")
    res.lost_checks = L.verify_lost(llm, lost, res.b, limit=max_lost)
    for m in lost:
        v = res.lost_checks.get(m.a.id)
        if not v:
            continue
        if v.get("verdict") == "lost":
            out.append(Finding("", "function_lost", _quote(m.a.text),
                               f"Была у: {m.a.owner}. {v.get('explanation', '')}", "high",
                               [_src(res.a, m.a.id, m.a.text)], "llm"))
        else:
            cov = [check_source(docs, res.b.doc, c.get("clause", ""), c.get("quote", ""))
                   for c in v.get("covered_by", [])]
            v["covered_sources"] = cov
            if not any(s.verified for s in cov):
                out.append(Finding("", "function_lost", _quote(m.a.text),
                                   f"Была у: {m.a.owner}. Модель считает функцию сохранённой, но указанные ею "
                                   "пункты это не подтверждают.", "medium", [_src(res.a, m.a.id, m.a.text)] + cov,
                                   "llm", verified=False))

    progress("LLM: дублирование и конфликты интересов")
    dc = L.duplicates_and_conflicts(llm, res.b)
    for kind, key in (("duplicate", "duplicates"), ("conflict", "conflicts")):
        for it in dc.get(key, []):
            srcs = [check_source(docs, res.b.doc, s.get("clause", ""), s.get("quote", ""))
                    for s in it.get("sources", [])]
            ok = [s for s in srcs if s.verified]
            if not ok:
                res.warnings.append(f"Отброшена неподтверждённая находка ({kind}): {it.get('title', '')}")
                continue
            out.append(Finding("", kind, it.get("title", ""), it.get("explanation", ""),
                               it.get("severity", "medium"), srcs, "llm", verified=len(ok) == len(srcs)))
    return out


def _template_summary(res: Result) -> str:
    c = defaultdict(int)
    for f in res.findings:
        c[f.type] += 1
    ms = defaultdict(int)
    for m in res.matches:
        ms[m.status] += 1
    return (
        f"Создано подразделений: {c['unit_created']}, сохранено: {c['unit_retained']}, "
        f"упразднено: {c['unit_abolished']}, упразднено линий подчинения: {c['role_abolished']}. "
        f"Функций сохранено: {ms['retained'] + ms['modified']}, перенесено к другому владельцу: {ms['moved']}, "
        f"кандидатов в потерю: {ms['lost_candidate']}, новых: {ms['new']}. "
        f"Изменено пунктов: {sum(p.status == 'changed' for p in res.pairs)}."
    )
