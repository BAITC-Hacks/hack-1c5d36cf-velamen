from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputFile(Model):
    name: str
    content: bytes


class SourceBlock(Model):
    id: str
    document_id: str
    filename: str
    locator: str
    text: str
    heading_path: list[str] = Field(default_factory=list)
    clause: str | None = None
    context_ids: list[str] = Field(default_factory=list)
    navigation: bool = False
    numbering: str | None = None


class Document(Model):
    id: str
    filename: str
    sha256: str
    blocks: list[SourceBlock] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    read_complete: bool = True


class Chunk(Model):
    id: str
    document_id: str
    body_ids: list[str] = Field(default_factory=list)
    context_ids: list[str] = Field(default_factory=list)
    text: str


class Evidence(Model):
    block_id: str
    quote: str


class Entity(Model):
    id: str
    name: str
    kind: Literal["unit", "role", "governing_body"]
    aliases: list[str] = Field(default_factory=list)
    scope_id: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class Relation(Model):
    subject_id: str
    object_id: str
    kind: Literal["part_of", "role_in_unit", "reports_functionally", "reports_administratively"]
    evidence: list[Evidence] = Field(default_factory=list)


class FunctionFact(Model):
    id: str
    holder_ids: list[str] = Field(default_factory=list)
    action: str
    object: str
    description: str
    modality: Literal["duty", "permission", "prohibition", "descriptive"]
    scope: str | None = None
    conditions: str | None = None
    exceptions: str | None = None
    frequency: str | None = None
    activity_types: list[Literal["execute", "approve", "record", "audit", "monitor", "advise", "other"]] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    owner_resolution: Literal["resolved", "unresolved"] = "unresolved"


class ExtractionBatch(Model):
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    functions: list[FunctionFact] = Field(default_factory=list)
    processed_body_ids: list[str] = Field(default_factory=list)
    unresolved_references: list[str] = Field(default_factory=list)


class Snapshot(Model):
    side: Literal["before", "after"]
    documents: list[Document] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    functions: list[FunctionFact] = Field(default_factory=list)
    failed_chunk_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    unresolved_references: list[str] = Field(default_factory=list)
    coverage_complete: bool = False


class UnitMatch(Model):
    before_ids: list[str] = Field(default_factory=list)
    after_ids: list[str] = Field(default_factory=list)
    kind: Literal["retained", "renamed", "split", "merged", "added_to_set", "absent_from_set", "unresolved"]
    basis: Literal["explicit", "inferred", "unresolved"]
    explanation_ru: str
    evidence: list[Evidence] = Field(default_factory=list)


class FunctionMatch(Model):
    before_ids: list[str] = Field(default_factory=list)
    after_ids: list[str] = Field(default_factory=list)
    changes: list[Literal["unchanged", "reworded", "owner_changed", "scope_changed", "modality_changed", "condition_changed", "added", "not_found", "unresolved"]] = Field(default_factory=list)
    explanation_ru: str
    evidence: list[Evidence] = Field(default_factory=list)


class Finding(Model):
    id: str
    kind: Literal["potential_loss", "potential_duplication", "potential_conflict", "coverage_gap"]
    before_fact_ids: list[str] = Field(default_factory=list)
    after_fact_ids: list[str] = Field(default_factory=list)
    explanation_ru: str
    recommendation_ru: str
    evidence: list[Evidence] = Field(default_factory=list)
    basis: Literal["explicit", "inferred", "unresolved"]
    review_status: Literal["needs_review"] = "needs_review"


class ComparisonResult(Model):
    before: Snapshot
    after: Snapshot
    unit_matches: list[UnitMatch] = Field(default_factory=list)
    function_matches: list[FunctionMatch] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    stages: dict[str, str] = Field(default_factory=dict)


class ProgressEvent(Model):
    stage: str
    completed: int
    total: int
    message_ru: str


class JobStatus(Model):
    id: str
    state: Literal["queued", "running", "completed", "partial", "failed", "interrupted"]
    stage: str
    message_ru: str
    completed: int = 0
    total: int = 0
    warnings: list[str] = Field(default_factory=list)
    error_ru: str | None = None
    result_url: str | None = None
    report_url: str | None = None
