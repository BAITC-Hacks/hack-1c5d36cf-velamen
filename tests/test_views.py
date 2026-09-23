"""Человекочитаемый вывод: без служебных полей, с понятными формулировками."""

from pathlib import Path

import pytest

from orgdiff import views as V
from orgdiff.pipeline import run
from orgdiff.report import sheets, write_html, write_xlsx

SAMPLE = Path(__file__).parent.parent / "data" / "sample"


@pytest.fixture(scope="module")
def res():
    return run(SAMPLE / "r8.docx", SAMPLE / "r9.docx", use_llm=False)


def test_plural_and_person():
    assert V.plural(1, "функция", "функции", "функций") == "1 функция"
    assert V.plural(3, "функция", "функции", "функций") == "3 функции"
    assert V.plural(12, "функция", "функции", "функций") == "12 функций"
    assert V.plural(21, "функция", "функции", "функций") == "21 функция"
    assert V.person("Директору ДНМ") == "Директор ДНМ"
    assert V.person("Главному аудитору") == "Главный аудитор"


def test_conclusion_is_a_list_of_sections(res):
    sections = dict(V.conclusion(res))
    assert list(sections) == ["Структура подразделений", "Функции", "Требует внимания", "Изменения текста"]
    structure = "\n".join(sections["Структура подразделений"])
    assert "— ДИТААД — Департамент ИТ-аудита и анализа данных" in structure
    assert "Сохранены: ДНМ, ДККМ" in structure
    assert "«Директор направления внутреннего аудита»" in structure
    assert any("Директор направления внутреннего аудита → ДИТААД, ДОА" in s for s in sections["Функции"])


def test_tables_have_no_technical_columns(res):
    for title, _, rows in sheets(res):
        for r in V.public(rows):
            assert not {"ID", "Похожесть", "Источник вывода", "_status"} & set(r), title


def test_units_table(res):
    rows = {r["Подразделение"]: r["Что произошло"] for r in V.unit_rows(res)}
    assert rows == {"ДИТААД": "🆕 Создано", "ДОА": "🆕 Создано", "ДНМ": "✅ Сохранено", "ДККМ": "✅ Сохранено"}


def test_changes_show_only_changed_fragments(res):
    row = next(r for r in V.change_rows(res) if r["Пункт"] == "9.42")
    assert "[процедурами и правилами.]" in row["Было"]
    assert "[ВНД.]" in row["Стало"]


def test_reports_are_written(res, tmp_path):
    assert write_xlsx(res, tmp_path / "r.xlsx").stat().st_size > 0
    html = write_html(res, tmp_path / "r.html").read_text(encoding="utf-8")
    assert V.APP_NAME in html and "Структура подразделений" in html
