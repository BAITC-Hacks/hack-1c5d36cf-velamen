import re
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import tiktoken

from .llm import ModelError, runtime
from .models import FunctionFact, SourceBlock
from .storage import cache_key, read_json, write_json


@lru_cache(maxsize=1)
def encoding():
    return tiktoken.get_encoding("cl100k_base")


def token_count(text):
    return len(encoding().encode(text, disallowed_special=()))


def function_text(fact: FunctionFact) -> str:
    # Deliberately exclude holder and clause number for transfer/renumbering search.
    return " | ".join(value for value in (fact.action, fact.object, fact.scope, fact.conditions,
                                          fact.exceptions, fact.frequency, fact.modality) if value)


def valid_vector(value):
    array = np.asarray(value, dtype=float)
    if array.ndim != 1 or not array.size or not np.all(np.isfinite(array)) or np.linalg.norm(array) == 0:
        raise ValueError("Некорректный или пустой вектор embedding.")
    return array.tolist()


async def embed_texts(texts: list[str]) -> np.ndarray:
    # Concurrent comparisons share corpus vectors instead of buying them repeatedly.
    async with runtime().embedding_lock:
        return await _embed_texts(texts)


async def _embed_texts(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.empty((0, 0))
    if any(not text.strip() or token_count(text) > 8191 for text in texts):
        raise ValueError("Embedding требует непустой текст длиной не более 8191 токена.")
    run = runtime()
    model = run.settings.openai_embedding_model
    vectors, pending = {}, []
    root = run.settings.artifact_dir / "cache" / "embeddings"
    for text in dict.fromkeys(texts):
        path = root / f"{cache_key(run.settings.openai_base_url, model, text)}.json"
        if path.exists():
            try:
                vectors[text] = valid_vector(read_json(path))
                run.usage["embeddings"]["cache_hits"] += 1
                continue
            except (ValueError, OSError):
                pass
        pending.append(text)
    while pending:
        batch, total = [], 0
        while pending and len(batch) < run.settings.embedding_batch_size:
            count = token_count(pending[0])
            if total + count > 250_000:
                break
            batch.append(pending.pop(0))
            total += count
        response = await run.request("embeddings", lambda: run.api().embeddings.create(
            model=model, input=batch, encoding_format="float"))
        if sorted(item.index for item in response.data) != list(range(len(batch))):
            raise ModelError("Embeddings: неполный или повторяющийся индекс ответа.")
        received = {batch[item.index]: valid_vector(item.embedding) for item in response.data}
        if len({len(v) for v in [*vectors.values(), *received.values()]}) > 1:
            raise ModelError("Embeddings: несовместимые размерности векторов.")
        for text, vector in received.items():
            write_json(root / f"{cache_key(run.settings.openai_base_url, model, text)}.json", vector)
        vectors.update(received)
    if len({len(v) for v in vectors.values()}) != 1:
        raise ModelError("Embeddings: несовместимые размерности кэша.")
    return np.asarray([vectors[text] for text in texts], dtype=float)


def words(text):
    return set(re.findall(r"[\w]+", text.casefold()))


async def rank_candidates(query: str, texts: dict[str, str], limit: int) -> list[str]:
    if not texts or limit <= 0:
        return []
    ids = list(texts)
    vectors = await embed_texts([query, *texts.values()])
    norms = np.linalg.norm(vectors, axis=1)
    scores = vectors[1:] @ vectors[0] / (norms[1:] * norms[0])
    semantic = [ids[i] for i in np.argsort(-scores, kind="stable")[:limit]]
    query_words = words(query)
    lexical = sorted(ids, key=lambda i: (-len(query_words & words(texts[i])), i))
    lexical = [i for i in lexical if query_words & words(texts[i])][:limit]
    exact = [i for i in ids if query.casefold() == texts[i].casefold()]
    return list(dict.fromkeys(exact + semantic + lexical))


@dataclass
class SourceWindow:
    id: str
    block_id: str
    text: str


def raw_windows(index: dict[str, SourceBlock], size=512, overlap=64) -> list[SourceWindow]:
    if not 0 <= overlap < size:
        raise ValueError("Перекрытие должно быть меньше окна.")
    result = []
    for block in index.values():
        if block.navigation or not block.text.strip():
            continue
        tokens = encoding().encode(block.text, disallowed_special=())
        _, offsets = encoding().decode_with_offsets(tokens)
        offsets.append(len(block.text))
        start = 0
        while start < len(tokens):
            end = min(start + size, len(tokens))
            text = block.text[offsets[start]:offsets[end]]
            while token_count(text) > size and end > start + 1:
                end -= 1
                text = block.text[offsets[start]:offsets[end]]
            if text.strip():
                result.append(SourceWindow(id=f"{block.id}:window:{start}", block_id=block.id, text=text))
            if end == len(tokens):
                break
            start = max(start + 1, end - overlap)
    return result
