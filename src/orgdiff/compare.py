import asyncio
import json
from typing import Literal

from pydantic import Field

from .extract import extract_chunk, merge_batches, source_index, unique_evidence, validate_evidence
from .llm import ModelError, generate, runtime
from .models import Chunk, ComparisonResult, Evidence, Finding, FunctionMatch, Model, Snapshot, UnitMatch
from .retrieve import function_text, rank_candidates, raw_windows
from .storage import cache_key


def absence_label(coverage_complete: bool, found_elsewhere: bool) -> str:
    if found_elsewhere:
        return "matched"
    return "not_found" if coverage_complete else "unresolved"


class Decision(Model):
    decision: Literal["matched", "not_found", "unresolved"]
    target_ids: list[str] = Field(default_factory=list)
    changes: list[Literal["unchanged", "reworded", "owner_changed", "scope_changed", "modality_changed", "condition_changed"]] = Field(default_factory=list)
    unit_kind: Literal["retained", "renamed", "split", "merged"] = "retained"
    basis: Literal["explicit", "inferred", "unresolved"] = "inferred"
    explanation_ru: str
    evidence: list[Evidence] = Field(default_factory=list)


def evidence_payload(item, snapshot):
    index = source_index(snapshot)
    result = item.model_dump(mode="json")
    entities = {e.id: e for e in snapshot.entities}
    result["holders"] = [entities[i].model_dump(mode="json") for i in getattr(item, "holder_ids", []) if i in entities]
    related_ids = set(getattr(item, "holder_ids", [])) | {item.id}
    # Resolve the ancestor chain and relation endpoints, not just opaque IDs.
    pending = list(related_ids)
    while pending:
        current = pending.pop()
        entity = entities.get(current)
        parents = {entity.scope_id} if entity and entity.scope_id else set()
        parents |= {r.object_id for r in snapshot.relations if r.subject_id == current}
        new = (parents & entities.keys()) - related_ids
        related_ids.update(new)
        pending.extend(new)
    result["related_entities"] = [entities[i].model_dump(mode="json") for i in sorted(related_ids) if i in entities]
    if hasattr(item, "kind"):
        result["relations"] = [r.model_dump(mode="json") for r in snapshot.relations if item.id in (r.subject_id, r.object_id)]
        result["functions"] = [function_text(f) for f in snapshot.functions if item.id in f.holder_ids]
    # Quotes plus original heading context; never substitute summaries for evidence.
    evs = list(item.evidence)
    for holder in result["holders"]:
        evs.extend(Evidence.model_validate(e) for e in holder["evidence"])
    for entity in result["related_entities"]:
        evs.extend(Evidence.model_validate(e) for e in entity["evidence"])
    context = {cid for e in evs for cid in index[e.block_id].context_ids}
    result["source_context"] = [{"block_id": cid, "text": index[cid].text} for cid in sorted(context) if cid in index]
    return result


def payload_size(payload):
    return len(json.dumps(payload, ensure_ascii=False))


async def judge(mode, anchor, candidates, origin, opposite):
    base = {"mode": mode, "anchor_side": origin.side, "anchor": evidence_payload(anchor, origin)}
    cap = runtime().settings.evidence_chars
    groups, group = [], []
    for candidate in candidates:
        payload = evidence_payload(candidate, opposite)
        if payload_size({**base, "candidates": [payload]}) > cap:
            raise ModelError(f"{anchor.id}: исходные свидетельства пары превышают лимит {cap}; сравнение не завершено.")
        if group and payload_size({**base, "candidates": group + [payload]}) > cap:
            groups.append(group)
            group = []
        group.append(payload)
    if group:
        groups.append(group)
    if not groups:
        return Decision(decision="not_found", explanation_ru="В противоположном наборе нет извлечённых кандидатов.")
    index = {**source_index(origin), **source_index(opposite)}
    decisions = []
    for group in groups:
        allowed = {item["id"] for item in group}
        group_sources = {ev["block_id"] for item in [base["anchor"], *group] for ev in item["evidence"]}
        group_sources |= {c["block_id"] for item in [base["anchor"], *group] for c in item["source_context"]}
        group_sources |= {ev["block_id"] for item in [base["anchor"], *group] for holder in item["holders"] for ev in holder["evidence"]}
        group_sources |= {ev["block_id"] for item in [base["anchor"], *group] for entity in item["related_entities"] for ev in entity["evidence"]}

        def validate(result):
            if not set(result.target_ids) <= allowed or len(result.target_ids) != len(set(result.target_ids)):
                raise ValueError("Сравнение ссылается на неизвестный/повторяющийся ID.")
            if result.decision == "matched":
                if not result.target_ids or (mode == "function" and not result.changes):
                    raise ValueError("Для соответствия нужны ID и признаки сравнения.")
                validate_evidence(result.evidence, {i: index[i] for i in group_sources})
                cited = {e.block_id for e in result.evidence}
                if not cited & {e.block_id for e in anchor.evidence}:
                    raise ValueError("Нет свидетельства исходной стороны.")
                for candidate in candidates:
                    if candidate.id in result.target_ids and not cited & {e.block_id for e in candidate.evidence}:
                        raise ValueError("Нет свидетельства выбранного кандидата.")
                if "unchanged" in result.changes and len(result.changes) > 1:
                    raise ValueError("unchanged несовместим с изменениями.")
            elif result.target_ids or result.changes:
                raise ValueError("Неподтверждённое решение не должно содержать соответствия.")

        decisions.append(await generate("compare", {**base, "candidates": group}, Decision, validate=validate))
    matches = [d for d in decisions if d.decision == "matched"]
    if any(d.decision == "unresolved" for d in decisions):
        return Decision(decision="unresolved", explanation_ru="Часть групп кандидатов осталась неопределённой.")
    if not matches:
        return Decision(decision="not_found", explanation_ru="Соответствие не найдено в проверенных группах кандидатов.")
    changes = {c for d in matches for c in d.changes}
    if len(changes) > 1 and "unchanged" in changes:
        return Decision(decision="unresolved", explanation_ru="Противоречивые решения по группам кандидатов.")
    return Decision(decision="matched", target_ids=list(dict.fromkeys(i for d in matches for i in d.target_ids)),
                    changes=sorted(changes), unit_kind=matches[0].unit_kind,
                    basis="inferred" if any(d.basis != "explicit" for d in matches) else "explicit",
                    explanation_ru=" ".join(d.explanation_ru for d in matches),
                    evidence=unique_evidence([e for d in matches for e in d.evidence]))


def reconcile_matches(matches: list[FunctionMatch]) -> list[FunctionMatch]:
    groups = []
    for match in matches:
        nodes = {("before", i) for i in match.before_ids} | {("after", i) for i in match.after_ids}
        touching = [g for g in groups if nodes & g[0]]
        combined = [match]
        for existing in touching:
            nodes |= existing[0]
            combined += existing[1]
            groups.remove(existing)
        groups.append((nodes, combined))
    result = []
    for nodes, proposals in groups:
        changes = {c for p in proposals for c in p.changes}
        paired = any(p.before_ids and p.after_ids for p in proposals)
        conflict = "unresolved" in changes or ("unchanged" in changes and len(changes) > 1) or (paired and bool(changes & {"added", "not_found"}))
        result.append(FunctionMatch(before_ids=sorted(i for side, i in nodes if side == "before"),
                                    after_ids=sorted(i for side, i in nodes if side == "after"),
                                    changes=["unresolved"] if conflict else sorted(changes),
                                    explanation_ru=("Противоречивые/неполные предложения; требуется проверка. " if conflict else "") +
                                                   " ".join(dict.fromkeys(p.explanation_ru for p in proposals)),
                                    evidence=unique_evidence([e for p in proposals for e in p.evidence])))
    return sorted(result, key=lambda m: (m.before_ids, m.after_ids))


async def compare_units(before, after, warnings):
    proposals = []
    for origin, opposite in ((before, after), (after, before)):
        for entity in origin.entities:
            texts = {e.id: " | ".join([e.name, *e.aliases]) for e in opposite.entities if e.kind == entity.kind}
            names = {e.id: e for e in opposite.entities}
            if entity.id in names and entity == names[entity.id]:
                proposals.append(UnitMatch(before_ids=[entity.id], after_ids=[entity.id], kind="retained", basis="explicit",
                                           explanation_ru="Одинаковое извлечённое упоминание в том же исходном документе.", evidence=entity.evidence))
                continue
            try:
                ids = await rank_candidates(" | ".join([entity.name, *entity.aliases]), texts, 8)
                aliases = {entity.name.casefold(), *(a.casefold() for a in entity.aliases)}
                ids = list(dict.fromkeys(ids + [e.id for e in opposite.entities if e.kind == entity.kind and aliases &
                                               {e.name.casefold(), *(a.casefold() for a in e.aliases)}]))
                decision = await judge("unit", entity, [names[i] for i in ids], origin, opposite)
                if decision.decision == "matched":
                    left, right = ([entity.id], decision.target_ids) if origin.side == "before" else (decision.target_ids, [entity.id])
                    kind = "split" if len(right) > 1 else "merged" if len(left) > 1 else decision.unit_kind
                    proposals.append(UnitMatch(before_ids=left, after_ids=right, kind=kind, basis=decision.basis,
                                               explanation_ru=decision.explanation_ru, evidence=decision.evidence))
                else:
                    complete = opposite.coverage_complete and not opposite.unresolved_references and decision.decision != "unresolved"
                    proposals.append(UnitMatch(before_ids=[entity.id] if origin.side == "before" else [],
                                               after_ids=[entity.id] if origin.side == "after" else [],
                                               kind=("absent_from_set" if origin.side == "before" else "added_to_set") if complete else "unresolved",
                                               basis="inferred" if complete else "unresolved", evidence=entity.evidence,
                                               explanation_ru="Соответствующее упоминание не найдено в предоставленном противоположном наборе. " + decision.explanation_ru))
            except (ModelError, ValueError) as exc:
                warnings.append(str(exc))
                proposals.append(UnitMatch(before_ids=[entity.id] if origin.side == "before" else [],
                                           after_ids=[entity.id] if origin.side == "after" else [], kind="unresolved", basis="unresolved",
                                           explanation_ru=f"Сравнение не завершено: {exc}", evidence=entity.evidence))
    # Connected components preserve split/merge hypotheses and contradictory negatives.
    components = reconcile_matches([FunctionMatch(before_ids=p.before_ids, after_ids=p.after_ids,
        changes=["unresolved"] if p.kind == "unresolved" else ["reworded"] if p.before_ids and p.after_ids else ["not_found"] if p.before_ids else ["added"],
        explanation_ru=p.explanation_ru, evidence=p.evidence) for p in proposals])
    result = []
    for component in components:
        related = [p for p in proposals if set(p.before_ids) & set(component.before_ids) or set(p.after_ids) & set(component.after_ids)]
        kinds = {p.kind for p in related}
        kind = "unresolved" if "unresolved" in component.changes or ("retained" in kinds and "renamed" in kinds) else (
            "split" if len(component.after_ids) > 1 else "merged" if len(component.before_ids) > 1 else related[0].kind)
        result.append(UnitMatch(before_ids=component.before_ids, after_ids=component.after_ids, kind=kind,
                               basis="unresolved" if kind == "unresolved" else "inferred" if any(p.basis != "explicit" for p in related) else "explicit",
                               explanation_ru=component.explanation_ru, evidence=component.evidence))
    return result


async def function_candidates(fact, origin, opposite, units, limit):
    texts = {f.id: function_text(f) for f in opposite.functions}
    ids = await rank_candidates(function_text(fact), texts, limit)
    owners = set()
    for match in units:
        source, target = (match.before_ids, match.after_ids) if origin.side == "before" else (match.after_ids, match.before_ids)
        if match.kind != "unresolved" and set(fact.holder_ids) & set(source):
            owners.update(target)
    local = {f.id: texts[f.id] for f in opposite.functions if owners & set(f.holder_ids)}
    ids += await rank_candidates(function_text(fact), local, limit)
    index = {f.id: f for f in opposite.functions}
    return [index[i] for i in dict.fromkeys(ids)]


async def recover(fact, after, warnings):
    """One bounded raw-source search and extraction repair; original blocks stay intact."""
    index = source_index(after)
    windows = {w.id: w for w in raw_windows(index)}
    ids = await rank_candidates(function_text(fact), {i: w.text for i, w in windows.items()}, 20)
    batches = []
    for wid in ids:
        window = windows[wid]
        block = index[window.block_id]
        context = [index[i] for i in block.context_ids if i in index]
        text = "\n\n".join([f"[CONTEXT {b.id}]\n{b.text}" for b in context] + [f"[BODY {block.id}]\n{window.text}"])
        if len(text) > runtime().settings.evidence_chars:
            warnings.append(f"{fact.id}: контекст окна {wid} превышает лимит; поиск неполон.")
            return False, len(ids)
        chunk = Chunk(id=f"{after.side}:repair:{cache_key(text)[:20]}", document_id=block.document_id,
                      body_ids=[block.id], context_ids=[b.id for b in context], text=text)
        try:
            batches.extend(await extract_chunk(chunk, index))
        except (ModelError, ValueError) as exc:
            warnings.append(f"{fact.id}: восстановление {wid}: {exc}")
            return False, len(ids)
    merge_batches(after, batches)
    return True, len(ids)


async def compare_snapshots(before: Snapshot, after: Snapshot) -> ComparisonResult:
    result = ComparisonResult(before=before, after=after, stages={"extract": "completed" if before.coverage_complete and after.coverage_complete else "partial",
                                                               "compare": "running", "risks": "not_run", "report": "not_run"})
    result.unit_matches = await compare_units(before, after, result.warnings)
    proposals = []

    async def compare_one(fact, origin, opposite, repair=False):
        identical = next((f for f in opposite.functions if f.id == fact.id and f == fact), None)
        if identical:
            return FunctionMatch(before_ids=[fact.id], after_ids=[fact.id], changes=["unchanged"],
                                 explanation_ru="Одинаковый факт с теми же исполнителями и исходными свидетельствами.", evidence=fact.evidence)
        prefix = f"Поиск по извлечённым функциям всех исполнителей ({len(opposite.functions)}), top-8 semantic + lexical и совпавшие исполнители. "
        try:
            candidates = await function_candidates(fact, origin, opposite, result.unit_matches, 8)
            decision = await judge("function", fact, candidates, origin, opposite)
            search_complete = True
            if decision.decision == "not_found" and repair:
                candidates = await function_candidates(fact, origin, opposite, result.unit_matches, 20)
                decision = await judge("function", fact, candidates, origin, opposite)
                prefix += "Расширен поиск до top-20 по всем исполнителям. "
                if decision.decision == "not_found":
                    search_complete, count = await recover(fact, opposite, result.warnings)
                    prefix += f"Проверены исходные окна: {count}; один раунд повторного извлечения. "
                    candidates = await function_candidates(fact, origin, opposite, result.unit_matches, 20)
                    decision = await judge("function", fact, candidates, origin, opposite)
            if decision.decision == "matched":
                left, right = ([fact.id], decision.target_ids) if origin.side == "before" else (decision.target_ids, [fact.id])
                selected = [c for c in candidates if c.id in decision.target_ids]
                unresolved = fact.owner_resolution != "resolved" or any(c.owner_resolution != "resolved" for c in selected)
                return FunctionMatch(before_ids=left, after_ids=right, changes=["unresolved"] if unresolved else decision.changes,
                                     explanation_ru=prefix + decision.explanation_ru, evidence=decision.evidence)
            complete = (opposite.coverage_complete and not opposite.unresolved_references and not origin.unresolved_references
                        and search_complete and fact.owner_resolution == "resolved"
                        and all(f.owner_resolution == "resolved" for f in opposite.functions))
            label = absence_label(complete, False) if decision.decision == "not_found" else "unresolved"
            change = "added" if origin.side == "after" and label == "not_found" else label
            if label == "unresolved":
                prefix += "Неполное покрытие, неразрешённые ссылки/исполнитель или неопределённое соответствие; отсутствие не установлено. "
            return FunctionMatch(before_ids=[fact.id] if origin.side == "before" else [], after_ids=[fact.id] if origin.side == "after" else [],
                                 changes=[change], explanation_ru=prefix + "Не найдено в предоставленном противоположном наборе. " + decision.explanation_ru,
                                 evidence=fact.evidence)
        except (ModelError, ValueError) as exc:
            result.warnings.append(f"{fact.id}: {exc}")
            return FunctionMatch(before_ids=[fact.id] if origin.side == "before" else [], after_ids=[fact.id] if origin.side == "after" else [],
                                 changes=["unresolved"], explanation_ru=prefix + f"Поиск не завершён: {exc}", evidence=fact.evidence)

    # The shared semaphore caps API calls. Only repair mutates the registry.
    initial = await asyncio.gather(*(compare_one(f, before, after) for f in before.functions))
    registry_before_repair = cache_key(after.entities, after.relations)
    for fact, proposal in zip(before.functions, initial):
        # Even incomplete extraction needs the wider search; it can recover a match.
        if not proposal.after_ids and "Поиск не завершён" not in proposal.explanation_ru:
            proposal = await compare_one(fact, before, after, repair=True)
        proposals.append(proposal)
    if cache_key(after.entities, after.relations) != registry_before_repair:
        result.unit_matches = await compare_units(before, after, result.warnings)
    proposals.extend(await asyncio.gather(*(compare_one(f, after, before) for f in after.functions)))
    result.function_matches = reconcile_matches(proposals)
    for match in result.function_matches:
        if "not_found" in match.changes:
            result.findings.append(Finding(id=f"loss:{cache_key(match.before_ids)[:16]}", kind="potential_loss",
                before_fact_ids=match.before_ids, explanation_ru="Функция не найдена в предоставленном наборе после изменений; это не доказательство упразднения. " + match.explanation_ru,
                recommendation_ru="Ответственному сотруднику проверить исходные документы и возможный перенос функции.",
                evidence=match.evidence, basis="inferred"))
    unresolved = any("unresolved" in m.changes for m in result.function_matches) or any(m.kind == "unresolved" for m in result.unit_matches)
    if unresolved or not before.coverage_complete or not after.coverage_complete:
        result.warnings.append("Сравнение содержит непроверенные/неопределённые соответствия; отсутствие не устанавливается по неполному покрытию.")
    result.stages["compare"] = "partial" if result.warnings else "completed"
    result.warnings.append("Дублирование и конфликты интересов на этом этапе ещё не проверялись.")
    return result
