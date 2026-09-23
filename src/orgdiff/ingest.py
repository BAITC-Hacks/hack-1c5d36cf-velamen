"""Read-only adapters. IDs address original text, never invented Word pages."""
import hashlib
import re
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from docx import Document as WordDocument
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from openpyxl import load_workbook
from pypdf import PdfReader

from .models import Document, InputFile, SourceBlock
from .storage import cache_key, read_json, write_json

PARSER_VERSION = "3"
SUPPORTED_EXTENSIONS = {".docx", ".pdf", ".xlsx"}
CLAUSE = re.compile(r"^\s*(\d+(?:\.\d+)*)(?:\.|\))?(?:\s|$)")
OWNER_INTRO = re.compile(
    r"^(?:Директор\w*|Руководител\w*|Начальник\w*|Менеджер\w*|Работник\w*|"
    r"Главный аудитор|Председател\w*|Комитет\w*|Совет\w*|Департамент\w*|БВА)\b"
)


def parse_file(file: InputFile) -> Document:
    digest = hashlib.sha256(file.content).hexdigest()
    document = Document(id=digest, filename=file.name, sha256=digest)
    extension = Path(file.name).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Неподдерживаемый формат: {file.name}. Допустимы DOCX, PDF, XLSX.")
    try:
        {".docx": _docx, ".pdf": _pdf, ".xlsx": _xlsx}[extension](file.content, document)
    except Exception as exc:
        raise ValueError(f"Не удалось прочитать {file.name}: {type(exc).__name__}") from exc
    if not document.blocks:
        _warn(document, "Документ не содержит доступного для чтения текста.")
    return document


def parse_cached(file: InputFile, cache_dir: Path) -> Document:
    key = cache_key(PARSER_VERSION, file.name, hashlib.sha256(file.content).hexdigest())
    path = cache_dir / "parse" / f"{key}.json"
    if path.exists():
        return Document.model_validate(read_json(path))
    document = parse_file(file)
    write_json(path, document)
    return document


def _warn(document: Document, message: str) -> None:
    document.warnings.append(f"{document.filename}: {message}")
    document.read_complete = False


def _block(document: Document, locator: str, text: str, **kwargs) -> SourceBlock | None:
    if not text.strip():
        return None
    match = CLAUSE.match(text)
    block = SourceBlock(id=f"{document.id}:{locator}", document_id=document.id,
                        filename=document.filename, locator=locator, text=text,
                        clause=match.group(1) if match else None, **kwargs)
    document.blocks.append(block)
    return block


def _docx(content: bytes, document: Document) -> None:
    word = WordDocument(BytesIO(content))
    headings: list[tuple[int, SourceBlock]] = []
    clauses: dict[str, SourceBlock] = {}
    introduction: SourceBlock | None = None

    def paragraph(item: Paragraph, locator: str, force_navigation: bool = False):
        nonlocal introduction, headings
        style = item.style.name if item.style else ""
        text = item.text
        navigation = force_navigation or style.lower().startswith(("toc", "table of contents", "оглавление")) or bool(
            item._p.xpath('.//w:instrText[contains(text(), "TOC")]'))
        clause_match = CLAUSE.match(text)
        clause = clause_match.group(1) if clause_match else None
        if clause:
            headings = [(n, b) for n, b in headings
                        if b.clause is None or clause.startswith(b.clause + ".")]
        heading_match = re.search(r"(?:heading|заголовок)\s*(\d+)", style, re.I)
        level = int(heading_match.group(1)) if heading_match else None
        # Some regulations style only the first role heading. A subsequent
        # numbered owner introduction at the same depth replaces it even when
        # Word gives that paragraph the Normal style. This is a structural hint,
        # not an assertion about the identity of either role.
        if clause_match and text.rstrip().endswith(":") and OWNER_INTRO.match(text[clause_match.end():]):
            level = len(clause.split("."))
        if level is not None:
            headings = [(n, b) for n, b in headings if n < level]
            introduction = None
        if clause:
            # A newly numbered sibling ends the preceding list's introduction.
            clauses_to_keep = {k: b for k, b in clauses.items() if clause.startswith(k + ".")}
            clauses.clear()
            clauses.update(clauses_to_keep)
            introduction = None
        context = [b for _, b in headings] + list(clauses.values())
        if introduction is not None:
            context.append(introduction)
        context = list({b.id: b for b in context}.values())
        num = item._p.xpath("./w:pPr/w:numPr")
        numbering = None
        if num:
            numbering = "; ".join(f"{child.tag.split('}')[-1]}={child.get(qn('w:val'))}" for child in num[0])
        block = _block(document, locator, text, heading_path=[b.text for b in context],
                       context_ids=[b.id for b in context], navigation=navigation, numbering=numbering)
        if block and not navigation:
            if level is not None:
                headings.append((level, block))
            if clause:
                clauses[clause] = block
            if text.rstrip().endswith(":"):
                introduction = block
        for tag, label in (("ins", "необработанные исправления"), ("del", "удалённый текст исправлений"),
                           ("drawing", "рисунок/диаграмма"), ("object", "встроенный объект"),
                           ("pict", "графический объект")):
            if item._p.xpath(f".//w:{tag}"):
                _warn(document, f"{locator}: {label}; содержимое может быть неполным.")

    def traverse(container, prefix="", navigation=False):
        p_number = t_number = 0
        element = container.element.body if hasattr(container, "element") else container._tc

        def children(parent, inherited_navigation=False):
            for child in parent:
                if child.tag in (qn("w:p"), qn("w:tbl")):
                    yield child, inherited_navigation
                elif child.tag in (qn("w:sdt"), qn("w:sdtContent"), qn("w:ins"), qn("w:del"), qn("w:customXml")):
                    is_toc = bool(child.xpath('.//w:instrText[contains(text(), "TOC")]'))
                    yield from children(child, inherited_navigation or is_toc)

        for node, is_navigation in children(element, navigation):
            if node.tag == qn("w:p"):
                p_number += 1
                paragraph(Paragraph(node, container), f"{prefix}p{p_number}", is_navigation)
            elif node.tag == qn("w:tbl"):
                item = Table(node, container)
                t_number += 1
                seen = set()
                saved_headings, saved_clauses, saved_intro = list(headings), dict(clauses), introduction
                for row_number, row in enumerate(item.rows, 1):
                    for col_number, cell in enumerate(row.cells, 1):
                        if cell._tc in seen:
                            continue
                        seen.add(cell._tc)
                        headings[:] = saved_headings
                        clauses.clear()
                        clauses.update(saved_clauses)
                        restore_intro(saved_intro)
                        traverse(cell, f"{prefix}t{t_number}/r{row_number}c{col_number}/", is_navigation)
                headings[:] = saved_headings
                clauses.clear()
                clauses.update(saved_clauses)
                # Table content must not change the surrounding document's list owner.
                restore_intro(saved_intro)

    def restore_intro(value):
        nonlocal introduction
        introduction = value

    traverse(word)
    # Direct XML wrappers (tracked changes / content controls) may not be exposed
    # by python-docx traversal. Report omissions rather than claiming coverage.
    known = {qn("w:p"), qn("w:tbl"), qn("w:sectPr"), qn("w:sdt")}
    for item in word.element.body:
        if item.tag not in known:
            _warn(document, f"Тело документа: неподдерживаемый контейнер {item.tag.split('}')[-1]}.")
    if word.element.xpath(".//w:ins | .//w:del | .//w:moveFrom | .//w:moveTo"):
        _warn(document, "word/document.xml: есть исправления; выбор принятой редакции не гарантирован.")
    with ZipFile(BytesIO(content)) as archive:
        for name in archive.namelist():
            if name.startswith(("word/embeddings/", "word/charts/")):
                _warn(document, f"{name}: встроенное содержимое не разобрано.")
            if re.match(r"word/(footnotes|endnotes|header\d+|footer\d+)\.xml$", name):
                from lxml import etree
                root = etree.fromstring(archive.read(name))
                if any((node.text or "").strip() for node in root.iter(qn("w:t"))):
                    _warn(document, f"{name}: сноски/колонтитулы не входят в основной текст.")


def _pdf(content: bytes, document: Document) -> None:
    pdf = PdfReader(BytesIO(content))
    for number, page in enumerate(pdf.pages, 1):
        try:
            text = page.extract_text() or ""
            if not text.strip():
                _warn(document, f"Страница {number}: нет извлекаемого текста; OCR не поддерживается.")
            if page.images:
                _warn(document, f"Страница {number}: изображения не анализируются.")
            for index, part in enumerate(re.split(r"\n\s*\n", text), 1):
                _block(document, f"page{number}/b{index}", part)
        except Exception as exc:
            _warn(document, f"Страница {number}: ошибка чтения ({type(exc).__name__}).")


def _xlsx(content: bytes, document: Document) -> None:
    formulas = load_workbook(BytesIO(content), data_only=False)
    values = load_workbook(BytesIO(content), data_only=True)
    try:
        for sheet in formulas:
            labels: SourceBlock | None = None
            for row in sheet.iter_rows():
                cells = []
                for cell in row:
                    if cell.value is None:
                        continue
                    value = cell.value
                    if cell.data_type == "f":
                        cached = values[sheet.title][cell.coordinate].value
                        if cached is None:
                            _warn(document, f"{sheet.title}!{cell.coordinate}: формула без сохранённого значения; не вычислена.")
                        else:
                            value = cached
                    cells.append(f"{cell.coordinate}: {value}")
                if cells:
                    locator = f"{sheet.title}!{row[0].coordinate}:{row[-1].coordinate}"
                    block = _block(document, locator, " | ".join(cells),
                                   heading_path=[sheet.title] + ([labels.text] if labels else []),
                                   context_ids=[labels.id] if labels else [])
                    if labels is None:
                        labels = block
            if sheet._charts or sheet._images:
                _warn(document, f"Лист {sheet.title}: диаграммы/рисунки не анализируются.")
    finally:
        formulas.close()
        values.close()
    with ZipFile(BytesIO(content)) as archive:
        if any(name.startswith(("xl/drawings/", "xl/embeddings/")) for name in archive.namelist()):
            _warn(document, "Графические или встроенные объекты XLSX не анализируются.")


def deduplicate(files: list[InputFile]) -> list[InputFile]:
    return list({hashlib.sha256(file.content).hexdigest(): file for file in reversed(files)}.values())[::-1]
