"""Человекочитаемые представления результата — общие для интерфейса, HTML и Excel.

Здесь нет служебных полей (ID, похожесть, «источник вывода»): только то, что
нужно сотруднику, который разбирает реорганизацию.
"""

from __future__ import annotations

import html
import re
from collections import defaultdict

from .models import Finding
from .pipeline import Result
from .textutil import change_fragments, strip_marker

APP_NAME = "Оргскоп"
APP_TAGLINE = "сравнение оргструктуры и функций до и после реорганизации"
DISCLAIMER = ("Выводы носят рекомендательный характер и должны быть проверены ответственным сотрудником. "
              "У каждой находки указаны пункты исходных документов.")

SEVERITY = {"high": "🔴 Высокая", "medium": "🟡 Средняя", "info": "⚪ Справочно"}
FUNC_STATUS = {
    "lost": "❌ Потеряна",
    "lost_candidate": "❓ Не найдена — проверить",
    "covered": "🔁 Переформулирована",
    "moved": "➡️ Передана другому",
    "new": "🆕 Новая",
    "modified": "✏️ Изменена формулировка",
    "retained": "✅ Без изменений",
}
FUNC_ORDER = list(FUNC_STATUS)
ISSUE = {"function_lost": "Потеря функции", "function_lost_candidate": "Функция не найдена",
         "duplicate": "Дублирование", "conflict": "Конфликт интересов"}
GENERAL_OWNER = "БВА (общие положения)"

_NOMINATIVE = {"главному": "Главный", "аудитору": "аудитор", "директору": "Директор",
               "руководителю": "Руководитель", "начальнику": "Начальник", "менеджеру": "Менеджер",
               "заместителю": "Заместитель"}


def person(head: str) -> str:
    """«Директору ДНМ» → «Директор ДНМ» (заголовки перечней подчинения стоят в дательном падеже)."""
    return " ".join(_NOMINATIVE.get(w.lower(), w) for w in head.split())


def owner(label: str) -> str:
    return "БВА в целом" if label == GENERAL_OWNER else label


def short(text: str, n: int = 220) -> str:
    text = re.sub(r"\s+", " ", strip_marker(text)).strip().rstrip(";")
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def plural(n: int, one: str, few: str, many: str) -> str:
    """plural(16, "функция", "функции", "функций") → «16 функций»."""
    k = n % 100
    form = many if 11 <= k <= 14 else one if k % 10 == 1 else few if 2 <= k % 10 <= 4 else many
    return f"{n} {form}"


def clauses(ids: list[str]) -> str:
    ids = list(dict.fromkeys(i for i in ids if i))
    if not ids:
        return "—"
    return ("п. " if len(ids) == 1 else "пп. ") + ", ".join(ids)


def refs(f: Finding) -> str:
    old = [s.clause for s in f.sources if s.doc == "old"]
    new = [s.clause for s in f.sources if s.doc == "new"]
    parts = []
    if old:
        parts.append("было: " + clauses(old))
    if new:
        parts.append("стало: " + clauses(new))
    return "; ".join(parts)


# ---------- Вывод списком ----------

def conclusion(res: Result) -> list[tuple[str, list[str]]]:
    by = defaultdict(list)
    for f in res.findings:
        by[f.type].append(f)
    ra, rb = res.reg_a, res.reg_b
    sections: list[tuple[str, list[str]]] = []

    if res.key_points:
        sections.append(("Главное", list(res.key_points)))

    # Структура
    s = []
    if by["unit_created"]:
        s.append("Созданы:")
        s += [f"— {f.title} — {rb.units[f.title]} (стало: {clauses([rb.unit_src[f.title]])})" for f in by["unit_created"]]
    if by["unit_retained"]:
        s.append("Сохранены: " + ", ".join(f.title for f in by["unit_retained"]))
    if by["unit_renamed"]:
        s.append("Переименованы: " + ", ".join(f.title for f in by["unit_renamed"]))
    s.append("Упразднены: " + (", ".join(f"{f.title} — {ra.units[f.title]}" for f in by["unit_abolished"]) or "нет"))
    for f in by["role_abolished"]:
        s.append(f"Упразднена должность «{person(f.title)}» вместе с её линией подчинения ({refs(f)})")
    if by["positions_changed"]:
        s.append("Изменён состав подчинённых:")
        s += [f"— {person(f.title)} ({refs(f)})" for f in by["positions_changed"]]
    sections.append(("Структура подразделений", s))

    # Функции
    counts = defaultdict(int)
    for r in function_rows(res):
        counts[r["_status"]] += 1
    s = []
    moved = sorted(by["function_moved"], key=lambda f: -len(f.sources))
    fn = lambda n: plural(n, "функция", "функции", "функций")  # noqa: E731
    if moved:
        s.append(f"Передано другим подразделениям: {fn(counts['moved'])}")
        for f in moved:
            src_from, src_to = f.title.split(" → ", 1)
            s.append(f"— {owner(src_from)} → {owner(src_to)}: {len(f.sources) // 2}")
    if res.llm_used and res.lost_checks:
        s.append(f"Потеряно (подтверждено агентом): {fn(counts['lost'])}")
        s.append(f"Переформулировано, но сохранено: {fn(counts['covered'])}")
    if counts["lost_candidate"]:
        s.append(f"Не найдено в новой редакции, нужна проверка: {fn(counts['lost_candidate'])}")
    if counts["new"]:
        per = defaultdict(int)
        for m in res.matches:
            if m.status == "new":
                per[owner(m.b.owner)] += 1
        s.append(f"Новых: {fn(counts['new'])}")
        s += [f"— {k}: {v}" for k, v in per.items()]
    s.append(f"Без изменений или с правкой формулировки: {fn(counts['retained'] + counts['modified'])}")
    sections.append(("Функции", s))

    # Риски
    s = []
    for t in ("function_lost", "duplicate", "conflict"):
        for f in by[t]:
            s.append(f"{ISSUE[t]}: {f.title} ({refs(f)})")
    if not s and not res.llm_used:
        s.append("Поиск дублей и конфликтов интересов выполняется с LLM — сейчас она отключена")
    elif not s:
        s.append("Подтверждённых потерь, дублей и конфликтов интересов не найдено")
    sections.append(("Требует внимания", s))

    # Текст
    changed = [p for p in res.pairs if p.status != "same"]
    kinds = defaultdict(int)
    for p in changed:
        kinds[res.change_notes.get(p.id, {}).get("kind", "")] += 1
    s = [f"Изменено: {plural(len(changed), 'пункт', 'пункта', 'пунктов')}"
         + (f" (по смыслу — {kinds['substantive']}, редакционно — {kinds['editorial']})" if res.change_notes else "")]
    renum = sum(p.renumbered for p in res.pairs)
    if renum:
        s.append(f"Перенумеровано: {plural(renum, 'пункт', 'пункта', 'пунктов')}. Сравнение идёт по содержанию, "
                 "поэтому сдвиг номеров не мешает")
    sections.append(("Изменения текста", s))

    if res.recommendations:
        sections.append(("Рекомендации", [r["text"] + (f" (см. {', '.join(r['refs'])})" if r["refs"] else "")
                                          for r in res.recommendations]))
    return sections


def metrics(res: Result) -> list[tuple[str, int]]:
    by = defaultdict(int)
    for f in res.findings:
        by[f.type] += 1
    st = defaultdict(int)
    for r in function_rows(res):
        st[r["_status"]] += 1
    lost_label = "Потеряно функций" if res.lost_checks else "Функций не найдено"
    return [("Создано подразделений", by["unit_created"]), ("Сохранено", by["unit_retained"]),
            ("Передано функций", st["moved"]), (lost_label, st["lost"] if res.lost_checks else st["lost_candidate"]),
            ("Дублирование", by["duplicate"]), ("Конфликты интересов", by["conflict"])]


# ---------- Таблицы ----------

def unit_rows(res: Result) -> list[dict]:
    ra, rb = res.reg_a, res.reg_b
    renamed = {f.title.split(" → ")[1]: f.title.split(" → ")[0] for f in res.findings if f.type == "unit_renamed"}
    rows = []
    for abbr in list(ra.units) + [x for x in rb.units if x not in ra.units]:
        if abbr in renamed.values():
            continue
        if abbr in renamed:
            status, old_src = "✏️ Переименовано (было " + renamed[abbr] + ")", ra.unit_src[renamed[abbr]]
        elif abbr in ra.units and abbr in rb.units:
            status, old_src = "✅ Сохранено", ra.unit_src[abbr]
        elif abbr in rb.units:
            status, old_src = "🆕 Создано", ""
        else:
            status, old_src = "❌ Упразднено", ra.unit_src[abbr]
        rows.append({"Подразделение": abbr, "Полное название": rb.units.get(abbr) or ra.units[abbr],
                     "Что произошло": status, "Было": clauses([old_src]) if old_src else "—",
                     "Стало": clauses([rb.unit_src[abbr]]) if abbr in rb.unit_src else "—"})
    order = {"🆕": 0, "❌": 1, "✏️": 2, "✅": 3}
    rows.sort(key=lambda r: order.get(r["Что произошло"].split()[0], 9))
    return rows


def position_rows(res: Result) -> list[dict]:
    rows = []
    for f in res.findings:
        if f.type not in ("role_abolished", "role_created", "positions_changed"):
            continue
        desc = f.description
        removed = re.search(r"Исключены: (.+?)\.(?: Добавлены|$)", desc)
        added = re.search(r"Добавлены: (.+?)\.$", desc)
        listed = re.search(r"\(должности: (.+)\)", desc)
        if f.type == "role_abolished":
            status, rem, add = "❌ Должность руководителя упразднена", listed.group(1) if listed else "", ""
        elif f.type == "role_created":
            status, rem, add = "🆕 Новый руководитель", "", listed.group(1) if listed else ""
        else:
            status, rem, add = "✏️ Изменён состав", removed.group(1) if removed else "", added.group(1) if added else ""
        rows.append({"Руководитель": person(f.title), "Что произошло": status,
                     "Убраны должности": rem.replace(", ", "\n") or "—",
                     "Добавлены должности": add.replace(", ", "\n") or "—", "Где в документах": refs(f)})
    return rows


def function_rows(res: Result, include_unchanged: bool = True) -> list[dict]:
    rows = []
    for m in res.matches:
        status, note = m.status, ""
        if m.status == "lost_candidate" and m.a.id in res.lost_checks:
            v = res.lost_checks[m.a.id]
            status = "lost" if v.get("verdict") == "lost" else "covered"
            note = v.get("explanation", "")
            cov = [s.clause for s in v.get("covered_sources") or [] if s.verified]
            if cov:
                note = f"Найдена в новой редакции: {clauses(cov)}. " + note
        elif m.status == "modified":
            frags = change_fragments(strip_marker(m.a.text), strip_marker(m.b.text))[:2]
            note = "; ".join(f"{o} → {n}" for o, n in frags)
        elif m.status == "moved":
            note = f"Владелец сменился: {owner(m.a.owner)} → {owner(m.b.owner)}"
        if not include_unchanged and status == "retained":
            continue
        f = m.b if m.status in ("new", "modified") else (m.a or m.b)
        rows.append({
            "Что произошло": FUNC_STATUS[status],
            "Функция": short(f.text),
            "Было": f"{owner(m.a.owner)}, п. {m.a.id}" if m.a else "—",
            "Стало": f"{owner(m.b.owner)}, п. {m.b.id}" if m.b else "—",
            "Пояснение": note,
            "_status": status,
        })
    rows.sort(key=lambda r: FUNC_ORDER.index(r["_status"]))
    return rows


def moved_rows(res: Result) -> list[dict]:
    rows = []
    for f in sorted((f for f in res.findings if f.type == "function_moved"), key=lambda f: -len(f.sources)):
        src_from, src_to = f.title.split(" → ", 1)
        pairs = list(zip(f.sources[0::2], f.sources[1::2]))
        items = [f"• {short(a.quote, 110)} (п. {a.clause} → п. {b.clause})" for a, b in pairs]
        rows.append({"От кого": owner(src_from), "Кому": owner(src_to), "Сколько": len(pairs),
                     "Какие функции": "\n".join(items)})
    return rows


def issue_rows(findings: list[Finding]) -> list[dict]:
    rows = []
    for f in findings:
        if f.origin == "llm":
            quotes = "\n".join(f"п. {s.clause}: «{short(s.quote, 160)}»" for s in f.sources if s.quote)
            check = "✓ цитаты найдены в документе" if f.verified else "⚠ ссылка не подтверждена — проверить вручную"
        else:
            quotes, check = "", "Найдено автоматически по тексту"
        rows.append({"Важность": SEVERITY.get(f.severity, f.severity), "Тип": ISSUE.get(f.type, f.type),
                     "Что найдено": f.title, "Пояснение": f.description,
                     "Где в документах": refs(f), "Цитаты": quotes, "Как проверено": check})
    return rows


def change_rows(res: Result) -> list[dict]:
    rows = []
    for p in res.pairs:
        if p.status == "same":
            continue
        note = res.change_notes.get(p.id, {})
        kind = {"editorial": "Редакционное", "substantive": "⚠ По смыслу"}.get(note.get("kind", ""), "")
        if p.status == "changed":
            num = p.a.id if not p.renumbered else f"{p.a.id} → {p.b.id}"
            frags = change_fragments(p.a.text, p.b.text)
            more = f"\n(и ещё правок: {len(frags) - 4})" if len(frags) > 4 else ""
            was = "\n".join(o for o, _ in frags[:4]) + more
            now = "\n".join(n for _, n in frags[:4]) + more
            what = note.get("summary") or f"Правок в тексте: {len(frags)}"
        elif p.status == "added":
            num, was, now, what = f"новый {p.b.id}", "—", short(p.b.text, 300), "Пункт добавлен"
        else:
            num, was, now, what = f"{p.a.id} (удалён)", short(p.a.text, 300), "—", "Пункт удалён"
        if (p.b or p.a).id == "0":
            num, topic = "шапка", "Реквизиты документа"
        else:
            topic = short((p.b or p.a).header, 70)
        rows.append({"Пункт": num, "Тема": topic, "Что изменилось": what,
                     "Характер": kind, "Было": was, "Стало": now})
    return rows


def public(rows: list[dict]) -> list[dict]:
    """Убирает служебные поля (начинаются с «_») и колонки, пустые во всех строках."""
    keep = [k for k in (rows[0] if rows else {}) if not k.startswith("_") and any(r[k] not in ("", None) for r in rows)]
    return [{k: r[k] for k in keep} for r in rows]


# ---------- HTML ----------

CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e3e8;--head:#f4f5f8;--accent:#305496;--zebra:#fafbfc}
@media (prefers-color-scheme:dark){:root{--bg:#16171a;--fg:#e8e8ea;--muted:#9a9aa2;--line:#2c2d33;--head:#1f2025;--accent:#8fb0ec;--zebra:#1a1b1f}}
.og{color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.og .scroll{overflow-x:auto;margin:8px 0 20px}
.og table{border-collapse:collapse;width:100%;font-size:13px}
.og th,.og td{border-bottom:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top;white-space:pre-line}
.og th{background:var(--head);font-weight:600;position:sticky;top:0}
.og tr:nth-child(even) td{background:var(--zebra)}
.og td:first-child{white-space:nowrap}
.og .muted{color:var(--muted)}
.og ul{margin:4px 0 16px;padding-left:20px} .og li{margin:3px 0}
.og h3{font-size:15px;margin:18px 0 4px}
"""


def table_html(rows: list[dict], empty: str = "Нет данных") -> str:
    rows = public(rows)
    if not rows:
        return f"<p class=muted>{html.escape(empty)}</p>"
    e = html.escape
    head = "".join(f"<th>{e(h)}</th>" for h in rows[0])
    body = "".join("<tr>" + "".join(f"<td>{e(str(v))}</td>" for v in r.values()) + "</tr>" for r in rows)
    return f"<div class=scroll><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def conclusion_html(res: Result) -> str:
    e = html.escape
    out = []
    for title, items in conclusion(res):
        lis: list[str] = []
        for i in items:
            if i.startswith("— ") and lis:  # подпункт: вложенный список в предыдущем <li>
                sub = f"<li>{e(i[2:])}</li>"
                if lis[-1].endswith("</ul></li>"):
                    lis[-1] = lis[-1][: -len("</ul></li>")] + sub + "</ul></li>"
                else:
                    lis[-1] = lis[-1][: -len("</li>")] + f"<ul>{sub}</ul></li>"
            else:
                lis.append(f"<li>{e(i)}</li>")
        out.append(f"<h3>{e(title)}</h3><ul>{''.join(lis)}</ul>")
    return "".join(out)


def by_type(res: Result, *types: str) -> list[Finding]:
    return [f for f in res.findings if f.type in types]
