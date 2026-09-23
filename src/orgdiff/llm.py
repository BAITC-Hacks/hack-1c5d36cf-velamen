"""LLM-слой: семантика изменений, проверка потерь (агент с инструментом поиска),
дублирование и конфликты интересов, итоговый вывод.

LLM никогда не видит документ целиком — только выровненные фрагменты с номерами
пунктов — и отвечает строго JSON со ссылками на эти номера. Ссылки потом проверяет
validate.py.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from .align import Pair
from .functions import FunctionMatch, search_functions
from .models import Document, Function

SYSTEM = (
    "Ты аналитик организационных изменений. Работаешь только с переданными фрагментами документов. "
    "Не делай утверждений, которых нет во фрагментах. Каждое утверждение подкрепляй номером пункта "
    "(поле clause) и дословной короткой цитатой (поле quote, до 20 слов, копируй без изменений). "
    "Отвечай на русском, строго одним JSON-объектом."
)


class LLM:
    def __init__(self, model: str | None = None, api_key: str | None = None):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
        self.calls = 0

    def ask(self, user: str, tools: list[dict] | None = None,
            tool_impl: dict[str, Callable[..., Any]] | None = None, max_steps: int = 4) -> dict:
        """Один запрос к модели; если даны инструменты — агентный цикл с вызовами инструментов."""
        messages: list[dict] = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
        for step in range(max_steps + 1):
            kwargs: dict[str, Any] = {"model": self.model, "messages": messages,
                                      "response_format": {"type": "json_object"}}
            if tools and step < max_steps:
                kwargs["tools"] = tools
            resp = self.client.chat.completions.create(**kwargs)
            self.calls += 1
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return _parse_json(msg.content or "{}")
            messages.append(msg.model_dump(exclude_none=True))
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                result = tool_impl[tc.function.name](**args) if tool_impl else {"error": "no tools"}
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(result, ensure_ascii=False)})
        return {}


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        return json.loads(text[start: end + 1]) if start >= 0 < end else {}


def _fn(f: Function) -> dict:
    return {"doc": f.doc, "clause": f.id, "owner": f.owner, "text": f.text}


# ---------- 1. Классификация изменённых пунктов ----------

def classify_changes(llm: LLM, pairs: list[Pair], batch: int = 8) -> dict[str, dict]:
    changed = [p for p in pairs if p.status == "changed"]
    out: dict[str, dict] = {}
    for i in range(0, len(changed), batch):
        items = [{"pair_id": p.id, "old_clause": p.a.id, "new_clause": p.b.id, "diff": p.diff[:3000]}
                 for p in changed[i: i + batch]]
        prompt = (
            "Ниже пары пунктов старой (old) и новой (new) редакции положения. diff: [-удалено-] {+добавлено+}.\n"
            "Для каждой пары определи kind: \"editorial\" (стилистика, сокращения, смысл не изменился) или "
            "\"substantive\" (изменились функции, полномочия, подчинённость, сроки, требования). "
            "summary — одно предложение, что изменилось по смыслу. units — затронутые подразделения/роли.\n"
            "Формат JSON: {\"items\": [{\"pair_id\": ..., \"kind\": ..., \"summary\": ..., \"units\": [...]}]}\n\n"
            + json.dumps(items, ensure_ascii=False)
        )
        for it in llm.ask(prompt).get("items", []):
            out[str(it.get("pair_id"))] = it
    return out


# ---------- 2. Агент: проверка кандидатов в потерю функций ----------

SEARCH_TOOL = [{
    "type": "function",
    "function": {
        "name": "search_new_edition",
        "description": "Поиск похожих функций в новой редакции документа по смысловому запросу.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Формулировка функции или ключевые слова"}},
            "required": ["query"],
        },
    },
}]


def verify_lost(llm: LLM, candidates: list[FunctionMatch], b: Document, limit: int = 30) -> dict[str, dict]:
    def tool(query: str) -> list[dict]:
        return [{**_fn(f), "score": round(s, 2)} for s, f in search_functions(b, query, k=6)]

    out: dict[str, dict] = {}
    for m in candidates[:limit]:
        f = m.a
        prompt = (
            "Функция из старой редакции (old) не нашлась в новой (new) при автоматическом сопоставлении.\n"
            f"Функция: {json.dumps(_fn(f), ensure_ascii=False)}\n"
            f"Похожие функции в new (предварительный поиск): {json.dumps(tool(f.text), ensure_ascii=False)}\n"
            "Если нужно — вызови search_new_edition с другими формулировками (синонимы, суть функции). "
            "Затем реши: verdict = \"covered\" (функция есть в new, возможно переформулирована или у другого "
            "владельца) или \"lost\" (в new её нет ни у кого). Для covered укажи covered_by — номера пунктов new "
            "и quote из new. Формат JSON: {\"verdict\": ..., \"covered_by\": [{\"clause\": ..., \"quote\": ...}], "
            "\"new_owner\": ..., \"explanation\": ...}"
        )
        out[f.id] = llm.ask(prompt, tools=SEARCH_TOOL, tool_impl={"search_new_edition": tool})
    return out


# ---------- 3. Дублирование и конфликты интересов ----------

def duplicates_and_conflicts(llm: LLM, b: Document) -> dict:
    owned = [_fn(f) for f in b.functions if f.owner != "БВА (общие положения)"]
    rules = [{"clause": c.id, "text": c.text[:2500]} for c in b.chunks
             if any(k in c.text.lower() for k in ("не имеют права", "конфликт", "совмещ"))]
    prompt = (
        "Функции подразделений и ролей новой редакции (new) с владельцами (owner):\n"
        + json.dumps(owned, ensure_ascii=False)
        + "\n\nПравила о запретах и конфликтах интересов из той же редакции:\n"
        + json.dumps(rules, ensure_ascii=False)
        + "\n\nНайди:\n1) duplicates — одна и та же по сути функция закреплена за РАЗНЫМИ владельцами так, что "
        "возможен параллелизм (общие для всех руководителей формулировки вроде «выполняет поручения» не считай).\n"
        "2) conflicts — потенциальный конфликт интересов: подразделение проверяет то, что само создаёт/внедряет/"
        "сопровождает, либо сочетание функций противоречит правилам выше.\n"
        "Для каждой находки: title, explanation, severity (medium|high), sources — список {doc: \"new\", clause, quote}.\n"
        "Формат JSON: {\"duplicates\": [...], \"conflicts\": [...]}"
    )
    return llm.ask(prompt)


# ---------- 4. Итоговый вывод ----------

def conclusion(llm: LLM, findings: list[dict]) -> dict:
    prompt = (
        "Ниже проверенные находки сравнения двух редакций положения (id, тип, описание). "
        "Составь вывод для сотрудника, анализирующего реорганизацию. key_points — 3–6 главных выводов, каждый "
        "одним коротким предложением простым языком (без канцелярита, без id находок в тексте). "
        "recommendations — 3–6 конкретных действий: что перераспределить, уточнить или закрепить в документе; "
        "каждое — одно предложение, refs — id находок, на которых оно основано. Используй только эти находки.\n"
        "Формат JSON: {\"key_points\": [...], \"recommendations\": [{\"text\": ..., \"refs\": [...]}]}\n\n"
        + json.dumps(findings, ensure_ascii=False)
    )
    return llm.ask(prompt)
