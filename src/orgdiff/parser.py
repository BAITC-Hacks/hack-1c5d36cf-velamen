"""Нарезка документа на пункты (X.Y) и атомарные функции (X.Y.Z, «а.»).

Документы приходят как «плоский» текст: автонумерации нет, стили заголовков
ненадёжны, несколько пунктов бывают склеены в один абзац. Поэтому границы
ищем по номерам пунктов в тексте, а не по структуре Word.
"""

from __future__ import annotations

import re
from pathlib import Path

from .loader import load_lines
from .models import Chunk, Document, Function

NUM = re.compile(r"(?<![\d.])(\d{1,2}(?:\.\d{1,2}){0,2})(?:\.(?!\d))?\s*(?=[«A-Za-zА-Яа-яЁё])")
# «п. 5.3», «пп. 2.1», «пункта 3.4» — это ссылки, а не начало пункта.
REFERENCE_BEFORE = re.compile(r"(?:\bп|\bпп|\bпункт[а-я]*|\bст|\bразд[а-я]*|\bгл|\bприл[а-я]*)\.?$", re.I)
LETTER_ITEM = re.compile(r"^([а-яa-z])[.)]\s+(.*)", re.I)
TOC = re.compile(r"^(оглавление|содержание)\b", re.I)
ROLE_HEADER = re.compile(
    r"^(Главн\w* аудитор|Директор\w*|Руководител\w*|Менеджер\w*|Работник\w*|Аудитор\w*|"
    r"Куратор\w*|Департамент\w*|Отдел\w*|Управлени\w*|Служб\w*|Комитет\w*|Совет\w*|Президент\w*|"
    r"Правлени\w*|БВА)",
)
NOT_FUNCTION_LIST = re.compile(r"подчиня|состоит из|в составе следующих", re.I)


def _split_line(line: str, cur_sec: int) -> tuple[list[tuple[str | None, str]], int]:
    """Режет абзац на куски по началу пунктов. Возвращает [(номер|None, текст)], новый раздел."""
    cuts: list[tuple[int, int, str]] = []  # (начало номера, начало текста, номер)
    for m in NUM.finditer(line):
        num = m.group(1)
        parts = num.split(".")
        top = int(parts[0])
        before = line[: m.start()].rstrip()
        at_start = before == ""
        if not at_start and (before[-1] not in ".;:" or REFERENCE_BEFORE.search(before)):
            continue
        nxt = line[m.end(): m.end() + 1]
        if len(parts) == 1:
            # Новый раздел — только следующий по порядку и с заглавной буквы.
            if top != cur_sec + 1 or not nxt.isupper():
                continue
        elif not (top == cur_sec or (top == cur_sec + 1 and parts[1] == "1")):
            continue
        cuts.append((m.start(), m.end(), num))
        cur_sec = top

    if not cuts:
        return [(None, line)], cur_sec
    out: list[tuple[str | None, str]] = []
    if line[: cuts[0][0]].strip():
        out.append((None, line[: cuts[0][0]].strip()))
    for i, (_, text_start, num) in enumerate(cuts):
        end = cuts[i + 1][0] if i + 1 < len(cuts) else len(line)
        out.append((num, line[text_start:end].strip()))
    return out, cur_sec


def _owner_of(header: str) -> str | None:
    """«5.4. Директор ДНМ:» → «Директор ДНМ». Только для перечней полномочий роли."""
    h = header.strip()
    if not h.endswith(":") or len(h) > 250 or NOT_FUNCTION_LIST.search(h):
        return None
    if not ROLE_HEADER.match(h):
        return None
    h = re.sub(r"\(далее[^)]*\)", "", h.rstrip(":")).strip()
    # «Директор ДНМ обязан обеспечить …, а также имеет право» → «Директор ДНМ»
    h = re.split(r"\s(?:обязан\w*|имеет|имеют|не имеют|информирует|несут|несет|несёт|"
                 r"осуществля\w*|– по вопросам)\b", h)[0]
    return re.sub(r"\s+", " ", h).strip(" ,")


def parse_lines(lines: list[str], doc: str, name: str = "") -> Document:
    d = Document(doc=doc, name=name or doc)
    cur_sec = 0
    head = Chunk(doc=doc, id="0", section="0", header="Реквизиты")
    d.chunks.append(head)
    chunk = head
    sub_id: str | None = None  # текущий подпункт X.Y.Z (для буквенных пунктов)

    for raw in lines:
        if TOC.match(raw.strip()) and cur_sec > 0:
            break
        pieces, cur_sec = _split_line(raw, cur_sec)
        for num, text in pieces:
            if num is None:
                chunk.lines.append(text)
                m = LETTER_ITEM.match(text)
                if m and chunk.id != "0":
                    parent = sub_id or chunk.id
                    _add_function(d, chunk, f"{parent}.{m.group(1).lower()}", text)
                continue
            parts = num.split(".")
            if len(parts) == 1:
                d.sections[num] = text[:120]
                chunk = Chunk(doc=doc, id=num, section=num, header=text, lines=[f"{num}. {text}"])
                d.chunks.append(chunk)
                sub_id = None
            elif len(parts) == 2:
                chunk = Chunk(doc=doc, id=num, section=parts[0], header=text, lines=[f"{num}. {text}"])
                chunk.owner = _owner_of(text)
                d.chunks.append(chunk)
                sub_id = None
            else:
                parent = ".".join(parts[:2])
                if chunk.id != parent:
                    chunk = Chunk(doc=doc, id=parent, section=parts[0], header=text)
                    d.chunks.append(chunk)
                chunk.lines.append(f"{num}. {text}")
                sub_id = num
                _add_function(d, chunk, num, text)
    return d


def _add_function(d: Document, chunk: Chunk, fid: str, text: str) -> None:
    if NOT_FUNCTION_LIST.search(chunk.header) or chunk.section in ("0", "1"):
        return
    base, n = fid, 2
    while d.function(fid):  # в исходнике бывают повторы буквенной нумерации
        fid, n = f"{base}#{n}", n + 1
    d.functions.append(
        Function(doc=d.doc, id=fid, chunk_id=chunk.id, text=text, owner=chunk.owner or "БВА (общие положения)")
    )


def parse_file(path: str | Path, doc: str) -> Document:
    return parse_lines(load_lines(path), doc=doc, name=Path(path).name)
