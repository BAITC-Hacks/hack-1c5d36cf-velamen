"""Проверки на тестовом наборе: известные изменения между редакциями 8 и 9 положения о ВА.

LLM не вызывается — проверяется детерминированное ядро.
"""

from pathlib import Path

import pytest

from orgdiff.align import align
from orgdiff.functions import match_functions
from orgdiff.models import Document
from orgdiff.parser import parse_file, parse_lines
from orgdiff.pipeline import run
from orgdiff.units import assign_owners, build_registry, compare_registries
from orgdiff.validate import check_source

SAMPLE = Path(__file__).parent.parent / "data" / "sample"


@pytest.fixture(scope="module")
def docs():
    a, b = parse_file(SAMPLE / "r8.docx", "old"), parse_file(SAMPLE / "r9.docx", "new")
    ra, rb = build_registry(a), build_registry(b)
    assign_owners(a, ra)
    assign_owners(b, rb)
    return a, ra, b, rb


# --- нарезка ---

def test_glued_clauses_are_split(docs):
    a, *_ = docs
    assert a.chunk("3.9").text.startswith("3.9. Рабочие места")
    assert a.chunk("3.10") is not None


def test_dates_and_references_do_not_split_clauses(docs):
    a, *_ = docs
    assert "06.12.2011" in a.chunk("1.10").text
    lines = ["1. Общие положения", "1.1. Работает в соответствии с п. 1.2 настоящего Положения."]
    d = parse_lines(lines, "x")
    assert [c.id for c in d.chunks] == ["0", "1", "1.1"]


def test_all_sections_found_and_toc_skipped(docs):
    a, _, b, _ = docs
    assert list(a.sections) == [str(i) for i in range(1, 15)]
    assert not any("ОГЛАВЛЕНИЕ" in c.header for c in b.chunks)


# --- must have 1: реорганизованные / сохранённые / созданные ---

def test_units_created_retained(docs):
    a, ra, b, rb = docs
    f = compare_registries(a, ra, b, rb)
    created = {x.title for x in f if x.type == "unit_created"}
    retained = {x.title for x in f if x.type == "unit_retained"}
    assert created == {"ДИТААД", "ДОА"}
    assert retained == {"ДНМ", "ДККМ"}
    abolished_roles = [x for x in f if x.type == "role_abolished"]
    assert any("направления внутреннего аудита" in x.title for x in abolished_roles)


def test_owner_resolution(docs):
    a, _, b, _ = docs
    assert a.chunk("5.4").owner_units == ["ДНМ"]
    assert set(b.chunk("5.3").owner_units) == {"ДИТААД", "ДОА"}


# --- выравнивание не зависит от сдвига нумерации ---

def test_renumbered_clause_is_matched(docs):
    a, _, b, _ = docs
    pairs = align(a, b)
    p = next(p for p in pairs if p.a and p.a.id == "5.8")  # «Работники БВА имеют право»
    assert p.b is not None and p.b.id == "5.7"
    assert p.status == "same"


# --- must have 2: перенос/потеря функций ---

def test_guarantee_map_moved_from_dnm(docs):
    a, _, b, _ = docs
    m = next(m for m in match_functions(a, b) if m.a and m.a.id == "5.4.4.б")
    assert m.status == "moved"
    assert m.a.owner == "ДНМ" and m.b.id == "5.3.3.б"


def test_dnm_specific_rights_are_loss_candidates(docs):
    a, _, b, _ = docs
    lost = {m.a.id for m in match_functions(a, b) if m.status == "lost_candidate"}
    assert {"5.7.1", "5.7.2"} <= lost


def test_generic_duties_stay_with_owner(docs):
    a, _, b, _ = docs
    m = next(m for m in match_functions(a, b) if m.a and m.a.id == "5.4.2")  # ДНМ: предложения в план
    assert m.status == "retained" and m.b.owner == "ДНМ"


# --- must have 4: трассировка ---

def test_source_validation(docs):
    a, _, b, _ = docs
    ds = {"old": a, "new": b}
    assert check_source(ds, "new", "3.4", "Департамент операционного аудита").verified
    assert not check_source(ds, "new", "3.4", "Департамент закупок").verified
    fixed = check_source(ds, "new", "9.99", "Департамент ИТ-аудита и анализа данных (ДИТААД)")
    assert fixed.verified and fixed.clause == "3.4"


def test_offline_run_every_finding_has_sources():
    res = run(SAMPLE / "r8.docx", SAMPLE / "r9.docx", use_llm=False)
    assert res.findings
    assert all(f.sources for f in res.findings)
    for f in res.findings:
        for s in f.sources:
            doc: Document = res.a if s.doc == "old" else res.b
            assert doc.clause_text(s.clause) is not None, (f.id, s.clause)
