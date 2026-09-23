from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Function:
    """Атомарная функция/полномочие: подпункт X.Y.Z или буквенный пункт «а.»."""

    doc: str
    id: str  # «5.4.4», «5.4.4.а», «5.3.а»
    chunk_id: str  # пункт второго уровня, в котором находится функция
    text: str
    owner: str  # подразделение/роль-владелец (метка)
    owner_units: list[str] = field(default_factory=list)  # аббревиатуры подразделений


@dataclass
class Chunk:
    """Пункт документа (X.Y) вместе со всеми подпунктами — единица сравнения и цитирования."""

    doc: str
    id: str  # «5.4», для раздела — «5», для реквизитов — «0»
    section: str  # номер раздела
    header: str  # первая строка пункта
    lines: list[str] = field(default_factory=list)
    owner: str | None = None
    owner_units: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class Document:
    doc: str
    name: str
    chunks: list[Chunk] = field(default_factory=list)
    functions: list[Function] = field(default_factory=list)
    sections: dict[str, str] = field(default_factory=dict)

    def chunk(self, cid: str) -> Chunk | None:
        return next((c for c in self.chunks if c.id == cid), None)

    def function(self, fid: str) -> Function | None:
        return next((f for f in self.functions if f.id == fid), None)

    def clause_text(self, cid: str) -> str | None:
        """Текст пункта любого уровня: сначала функция, затем пункт, затем префикс."""
        f = self.function(cid)
        if f:
            return f.text
        c = self.chunk(cid)
        if c:
            return c.text
        return None


@dataclass
class Source:
    doc: str
    clause: str
    quote: str = ""
    verified: bool = True


@dataclass
class Finding:
    id: str
    type: str
    title: str
    description: str
    severity: str = "info"  # info | medium | high
    sources: list[Source] = field(default_factory=list)
    origin: str = "rule"  # rule | llm
    verified: bool = True
