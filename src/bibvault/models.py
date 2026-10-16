"""Normalized references and acquisition results."""

from pydantic import BaseModel, Field


class Reference(BaseModel):
    citation_key: str
    entry_type: str
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: str | None = None
    doi: str | None = None
    url: str | None = None
    eprint: str | None = None
    archive_prefix: str | None = None
    arxiv_id: str | None = None
    publisher: str | None = None
    venue: str | None = None
    volume: str | None = None
    number: str | None = None
    pages: str | None = None
    source: str | None = None


class Candidate(BaseModel):
    url: str
    provider: str
    version: str
    source_rank: int
    source_url: str | None = None


class FileRecord(BaseModel):
    reference_id: str
    role: str
    version: str
    relative_path: str
    sha256: str
    byte_size: int
    source_url: str | None = None
    provider: str | None = None
    retrieved_at: str


class KeyRecord(BaseModel):
    citation_key: str
    reference_id: str
    entry_type: str | None = None
    title: str | None = None
    filename_stem: str
    status: str
    error_category: str | None = None
    error_detail: str | None = None


class AcquireResult(BaseModel):
    citation_key: str
    title: str | None = None
    reference_id: str
    status: str
    path: str | None = None
    provider: str | None = None
    version: str | None = None
    error: str | None = None
