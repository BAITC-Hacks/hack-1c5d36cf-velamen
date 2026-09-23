import asyncio
import re
from collections.abc import Callable

from pydantic import Field

from .chunking import make_chunks
from .llm import ModelError, PROMPT_VERSION, TruncatedOutput, generate, runtime
from .models import Chunk, Document, Entity, Evidence, ExtractionBatch, Model, ProgressEvent, SCHEMA_VERSION, Snapshot, SourceBlock
from .storage import cache_key


def normalize(text: str) -> str:
    return " ".join(text.split())


def quote_exists(quote: str, text: str) -> bool:
    return bool(normalize(quote)) and normalize(quote) in normalize(text)


def entity_key(name: str, kind: str, scope_id: str | None) -> tuple[str, str, str | None]:
    return normalize(name).casefold(), kind, scope_id


def extraction_cache_key(chunk: Chunk, source_index: dict[str, SourceBlock], model: str, prompt_version: str) -> str:
    return cache_key(SCHEMA_VERSION, model, prompt_version, chunk,
                     [source_index[i] for i in dict.fromkeys(chunk.body_ids + chunk.context_ids)])


def source_index(snapshot: Snapshot) -> dict[str, SourceBlock]:
    return {b.id: b for d in snapshot.documents for b in d.blocks}


def validate_evidence(evidence: list[Evidence], index: dict[str, SourceBlock], *, required=True):
    if required and not evidence:
        raise ValueError("Отсутствует подтверждающая цитата.")
    for item in evidence:
        if item.block_id not in index or not quote_exists(item.quote, index[item.block_id].text):
            raise ValueError(f"Недопустимая цитата: {item.block_id}: {item.quote}")


def validate_batch(batch: ExtractionBatch, chunk: Chunk, index: dict[str, SourceBlock]):
    if set(batch.processed_body_ids) != set(chunk.body_ids):
        raise ValueError("processed_body_ids должен в точности покрывать body_ids.")
    supplied = {key: index[key] for key in chunk.body_ids + chunk.context_ids}
    ids = [e.id for e in batch.entities]
    fact_ids = [f.id for f in batch.functions]
    if any(not x for x in ids + fact_ids) or len(set(ids + fact_ids)) != len(ids + fact_ids):
        raise ValueError("Пустые или повторяющиеся локальные ID.")
    for entity in batch.entities:
        validate_evidence(entity.evidence, supplied)
        if not entity.name.strip() or entity.scope_id == entity.id or (entity.scope_id and entity.scope_id not in ids):
            raise ValueError(f"Некорректная сущность/область: {entity.id}.")
        for alias in entity.aliases:
            if not any(quote_exists(alias, ev.quote) for ev in entity.evidence):
                raise ValueError(f"Псевдоним {alias} не подтверждён цитатой.")
    for relation in batch.relations:
        validate_evidence(relation.evidence, supplied)
        if relation.subject_id not in ids or relation.object_id not in ids:
            raise ValueError("Неизвестный конец отношения.")
    for fact in batch.functions:
        validate_evidence(fact.evidence, supplied)
        if any(holder not in ids for holder in fact.holder_ids):
            raise ValueError(f"Неизвестный исполнитель: {fact.id}.")
        if fact.owner_resolution == "resolved" and not fact.holder_ids:
            raise ValueError(f"У функции {fact.id} не указан разрешённый исполнитель.")
    for entity in batch.entities:
        seen, current = set(), entity
        by_id = {e.id: e for e in batch.entities}
        while current.scope_id:
            if current.id in seen:
                raise ValueError("Цикл scope_id.")
            seen.add(current.id)
            current = by_id[current.scope_id]


def split_chunk(chunk: Chunk) -> list[Chunk]:
    """Split transmitted BODY text, including a single oversized source block."""
    sections = re.findall(r"\[(BODY|CONTEXT) ([^\]]+)\]\n(.*?)(?=\n\n\[(?:BODY|CONTEXT) |\Z)", chunk.text, re.S)
    context = [(bid, text) for kind, bid, text in sections if kind == "CONTEXT"]
    body = [(bid, text) for kind, bid, text in sections if kind == "BODY"]
    if not body:
        return []
    midpoint = sum(len(text) for _, text in body) // 2
    groups = [[], []]
    used = 0
    for bid, text in body:
        take = min(len(text), max(0, midpoint - used))
        if take:
            groups[0].append((bid, text[:take]))
        if take < len(text):
            groups[1].append((bid, text[take:]))
        used += len(text)
    result = []
    for number, parts in enumerate(groups):
        if not parts:
            continue
        # Preserve introductions from the first half for the second half.
        attached = context + (groups[0] if number else [])
        text = "\n\n".join([f"[CONTEXT {bid}]\n{text}" for bid, text in attached] +
                            [f"[BODY {bid}]\n{text}" for bid, text in parts])
        result.append(Chunk(id=f"{chunk.id}:split{number}", document_id=chunk.document_id,
                            body_ids=list(dict.fromkeys(bid for bid, _ in parts)),
                            context_ids=list(dict.fromkeys(bid for bid, _ in attached)), text=text))
    return result


async def extract_chunk(chunk: Chunk, index: dict[str, SourceBlock], *, split=True) -> list[ExtractionBatch]:
    run = runtime()
    payload = {"passage": chunk.text, "body_ids": chunk.body_ids,
               "context_identity": extraction_cache_key(chunk, index, run.settings.openai_model, PROMPT_VERSION)}
    try:
        batch = await generate("extract", payload, ExtractionBatch, validate=lambda b: validate_batch(b, chunk, index))
        # Model IDs never escape their document/chunk namespace.
        batch = batch.model_copy(deep=True)
        mapping = {e.id: f"{chunk.id}:entity:{e.id}" for e in batch.entities}
        for entity in batch.entities:
            entity.id = mapping[entity.id]
            entity.scope_id = mapping.get(entity.scope_id)
        for relation in batch.relations:
            relation.subject_id, relation.object_id = mapping[relation.subject_id], mapping[relation.object_id]
        for fact in batch.functions:
            fact.id = f"{chunk.id}:function:{fact.id}"
            fact.holder_ids = [mapping[i] for i in fact.holder_ids]
        return [batch]
    except TruncatedOutput:
        children = split_chunk(chunk) if split else []
        if len(children) < 2:
            raise
        batches = []
        for child in children:
            batches.extend(await extract_chunk(child, index, split=False))
        return batches


def unique_evidence(items):
    return list({(e.block_id, normalize(e.quote)): e for e in items}.values())


def merge_batches(snapshot: Snapshot, batches: list[ExtractionBatch]) -> None:
    entities = snapshot.entities + [e for b in batches for e in b.entities]
    relations = snapshot.relations + [r for b in batches for r in b.relations]
    functions = snapshot.functions + [f for b in batches for f in b.functions]
    by_id = {e.id: e for e in entities}
    canonical: dict[str, Entity] = {}
    mapping = {}
    visiting = set()
    parents = {}
    for relation in relations:
        if relation.kind in {"part_of", "role_in_unit"}:
            parents.setdefault(relation.subject_id, set()).add(relation.object_id)

    def register(entity):
        if entity.id in mapping:
            return mapping[entity.id]
        if entity.id in visiting:
            return entity.id
        visiting.add(entity.id)
        declared = entity.scope_id
        if not declared and len(parents.get(entity.id, set())) == 1:
            declared = next(iter(parents[entity.id]))
        scope = register(by_id[declared]) if declared in by_id else None
        names = {normalize(n).casefold() for n in [entity.name, *entity.aliases]}
        chosen = None
        for other in canonical.values():
            if other.kind != entity.kind or other.scope_id != scope:
                continue
            shared_source = bool({e.block_id for e in other.evidence} & {e.block_id for e in entity.evidence})
            if scope is None and not shared_source:
                continue
            if names & {normalize(n).casefold() for n in [other.name, *other.aliases]}:
                chosen = other
                break
        if chosen is None:
            chosen = entity.model_copy(update={"scope_id": scope}, deep=True)
            canonical[chosen.id] = chosen
        else:
            chosen.aliases = list(dict.fromkeys(chosen.aliases + entity.aliases + ([entity.name] if entity.name != chosen.name else [])))
            chosen.evidence = unique_evidence(chosen.evidence + entity.evidence)
        mapping[entity.id] = chosen.id
        visiting.discard(entity.id)
        return chosen.id

    for entity in entities:
        register(entity)
    snapshot.entities = list(canonical.values())
    merged_relations = {}
    for relation in relations:
        relation = relation.model_copy(update={"subject_id": mapping[relation.subject_id], "object_id": mapping[relation.object_id]})
        key = (relation.subject_id, relation.object_id, relation.kind)
        if key in merged_relations:
            merged_relations[key].evidence = unique_evidence(merged_relations[key].evidence + relation.evidence)
        else:
            merged_relations[key] = relation
    snapshot.relations = list(merged_relations.values())
    merged_functions = {}
    for fact in functions:
        fact = fact.model_copy(update={"holder_ids": sorted({mapping[i] for i in fact.holder_ids})})
        assertion = (tuple(fact.holder_ids), *(normalize(getattr(fact, k) or "").casefold() for k in
                      ("action", "object", "modality", "scope", "conditions", "exceptions", "frequency")))
        key = (assertion, tuple(sorted({e.block_id for e in fact.evidence})))
        if key in merged_functions:
            merged_functions[key].evidence = unique_evidence(merged_functions[key].evidence + fact.evidence)
        else:
            merged_functions[key] = fact
    snapshot.functions = list(merged_functions.values())
    snapshot.unresolved_references = list(dict.fromkeys(snapshot.unresolved_references + [r for b in batches for r in b.unresolved_references]))


class Resolution(Model):
    equivalent_ids: list[str] = Field(default_factory=list)
    holder_ids: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    explanation_ru: str


async def resolve_holders(snapshot: Snapshot):
    index = source_index(snapshot)
    for fact in snapshot.functions:
        if fact.owner_resolution == "resolved":
            continue
        context_ids = {cid for ev in fact.evidence for cid in index[ev.block_id].context_ids}
        source_ids = context_ids | {e.block_id for e in fact.evidence}
        candidates = [e for e in snapshot.entities if source_ids & {ev.block_id for ev in e.evidence}]
        if not candidates:
            continue
        payload = {"mode": "holder", "function": fact.model_dump(mode="json"),
                   "headings": [index[i].model_dump(mode="json") for i in context_ids if i in index],
                   "candidates": [e.model_dump(mode="json") for e in candidates]}
        import json
        if len(json.dumps(payload, ensure_ascii=False)) > runtime().settings.evidence_chars:
            snapshot.warnings.append(f"{fact.id}: контекст разрешения исполнителя превышает лимит; исполнитель не определён.")
            continue
        allowed = {e.id for e in candidates}
        allowed_blocks = source_ids | {ev.block_id for entity in candidates for ev in entity.evidence}

        def validate(result):
            if not set(result.holder_ids) <= allowed or result.equivalent_ids:
                raise ValueError("Неизвестные исполнители в решении.")
            validate_evidence(result.evidence, {i: index[i] for i in allowed_blocks if i in index}, required=bool(result.holder_ids))

        try:
            result = await generate("resolve", payload, Resolution, validate=validate)
            if result.holder_ids:
                fact.holder_ids = result.holder_ids
                fact.owner_resolution = "resolved"
                fact.evidence = unique_evidence(fact.evidence + result.evidence)
        except ModelError as exc:
            snapshot.warnings.append(f"{fact.id}: {exc}")


async def extract_snapshot(side: str, documents: list[Document], progress: Callable[[ProgressEvent], None] = lambda e: None) -> Snapshot:
    snapshot = Snapshot(side=side, documents=documents, warnings=[w for d in documents for w in d.warnings])
    index = source_index(snapshot)
    settings = runtime().settings
    chunks = []
    for document in documents:
        try:
            chunks.extend(make_chunks(document, settings.body_chars, settings.context_chars))
        except ValueError as exc:
            snapshot.failed_chunk_ids.append(f"{document.id}:chunking")
            snapshot.warnings.append(str(exc))
    completed = 0

    async def process(chunk):
        nonlocal completed
        try:
            return await extract_chunk(chunk, index)
        except (ModelError, ValueError) as exc:
            snapshot.failed_chunk_ids.append(chunk.id)
            snapshot.warnings.append(f"{side}, {chunk.id}: {exc}")
            return []
        finally:
            completed += 1
            progress(ProgressEvent(stage=f"extract_{side}", completed=completed, total=len(chunks), message_ru="Извлечение фрагментов"))

    results = await asyncio.gather(*(process(chunk) for chunk in chunks))
    merge_batches(snapshot, [batch for group in results for batch in group])
    await resolve_holders(snapshot)
    snapshot.coverage_complete = bool(documents) and all(d.read_complete for d in documents) and not snapshot.failed_chunk_ids
    return snapshot
