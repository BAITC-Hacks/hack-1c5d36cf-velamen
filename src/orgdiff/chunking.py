from .models import Chunk, Document
from .storage import cache_key


def make_chunks(document: Document, body_chars: int = 8000, context_chars: int = 2000) -> list[Chunk]:
    if body_chars <= 0 or context_chars < 0:
        raise ValueError("Размеры фрагментов должны быть положительными.")
    index = {block.id: block for block in document.blocks}
    chunks = []
    pieces: list[tuple[str, str]] = []
    size = 0

    def flush():
        nonlocal pieces, size
        if not pieces:
            return
        body_ids = list(dict.fromkeys(block_id for block_id, _ in pieces))
        context_ids = list(dict.fromkeys(cid for bid in body_ids for cid in index[bid].context_ids if cid not in body_ids))
        # Closest introductions first. A large parent paragraph is carried in
        # BODY instead of silently omitted or cutting its conditions in half.
        selected, carried, context_size = [], [], 0
        for cid in reversed(context_ids):
            if context_size + len(index[cid].text) <= context_chars:
                selected.insert(0, cid)
                context_size += len(index[cid].text)
            else:
                carried.insert(0, (cid, index[cid].text))
        capacity = body_chars - sum(len(text) for _, text in carried)
        if capacity <= 0:
            raise ValueError(f"{document.filename}: родительский контекст не помещается в фрагмент; увеличьте BODY_CHARS или CONTEXT_CHARS.")

        def emit(group):
            all_pieces = carried + group
            text = "\n\n".join([f"[CONTEXT {cid}]\n{index[cid].text}" for cid in selected] +
                               [f"[BODY {bid}]\n{part}" for bid, part in all_pieces])
            chunks.append(Chunk(id=f"{document.id}:chunk:{cache_key(text)[:16]}", document_id=document.id,
                                body_ids=list(dict.fromkeys(bid for bid, _ in all_pieces)),
                                context_ids=selected, text=text))

        group, used = [], 0
        for bid, part in pieces:
            while part:
                take = min(len(part), capacity - used)
                group.append((bid, part[:take]))
                part = part[take:]
                used += take
                if used == capacity:
                    emit(group)
                    group, used = [], 0
        if group:
            emit(group)
        pieces, size = [], 0

    for block in document.blocks:
        if block.navigation:
            continue
        # Split oversized paragraphs without losing a single character. Each
        # part retains the original block ID so quotes resolve to the document.
        for offset in range(0, len(block.text), body_chars):
            part = block.text[offset:offset + body_chars]
            if size + len(part) > body_chars:
                flush()
            pieces.append((block.id, part))
            size += len(part)
    flush()
    return chunks
