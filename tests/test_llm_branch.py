"""LLM-ветка с подменённой моделью: проверяем агентный цикл и отбраковку выдуманных ссылок."""

from pathlib import Path
from types import SimpleNamespace

import orgdiff.llm as L
from orgdiff.pipeline import run

SAMPLE = Path(__file__).parent.parent / "data" / "sample"


class FakeClient:
    """Имитирует chat.completions: на проверку потерь сначала зовёт инструмент, потом отвечает."""

    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, model, messages, **kw):
        user = messages[1]["content"]
        last = messages[-1]
        if "search_new_edition" in user and last["role"] == "user":
            call = SimpleNamespace(id="c1", type="function",
                                   function=SimpleNamespace(name="search_new_edition", arguments='{"query": "гарантий"}'))
            msg = SimpleNamespace(content=None, tool_calls=[call],
                                  model_dump=lambda **k: {"role": "assistant", "content": None,
                                                          "tool_calls": [{"id": "c1", "type": "function",
                                                                          "function": {"name": "search_new_edition",
                                                                                       "arguments": '{"query": "гарантий"}'}}]})
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])
        if "search_new_edition" in user:
            body = '{"verdict": "lost", "covered_by": [], "explanation": "не найдено"}'
        elif "duplicates" in user:
            body = ('{"duplicates": [{"title": "Выдуманный дубль", "explanation": "x", '
                    '"sources": [{"doc": "new", "clause": "77.7", "quote": "несуществующая цитата о чём-то"}]}], '
                    '"conflicts": [{"title": "ДИТААД и автоматизация", "explanation": "аудит собственной автоматизации", '
                    '"severity": "high", "sources": [{"doc": "new", "clause": "5.3.2.а", '
                    '"quote": "создание дашбордов, автоматизация процессов ВА"}]}]}')
        elif "pair_id" in user:
            body = '{"items": []}'
        else:
            body = '{"key_points": ["Итог."], "recommendations": [{"text": "Проверить ДНМ", "refs": ["F1", "F999"]}]}'
        msg = SimpleNamespace(content=body, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def test_llm_branch_validates_sources(monkeypatch):
    def fake_init(self, model=None, api_key=None):
        self.client, self.model, self.calls = FakeClient(), "fake", 0

    monkeypatch.setattr(L.LLM, "__init__", fake_init)
    res = run(SAMPLE / "r8.docx", SAMPLE / "r9.docx", use_llm=True, max_lost=3)

    types = [f.type for f in res.findings]
    assert types.count("function_lost") == 3  # агент отработал с вызовом инструмента
    assert "duplicate" not in types  # выдуманная ссылка отброшена
    assert any("Выдуманный дубль" in w for w in res.warnings)
    conflict = next(f for f in res.findings if f.type == "conflict")
    assert conflict.sources[0].verified and conflict.sources[0].clause == "5.3.2.а"
    assert res.recommendations[0]["refs"] == ["F1"]  # несуществующая находка вычищена

    from orgdiff import views as V

    sections = dict(V.conclusion(res))
    assert sections["Главное"] == ["Итог."]
    assert any("Конфликт интересов" in s for s in sections["Требует внимания"])
    assert any(r["Что произошло"] == V.FUNC_STATUS["lost"] for r in V.function_rows(res))
