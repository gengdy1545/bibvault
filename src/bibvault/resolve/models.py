"""Metadata lookup results shared by publisher and OpenAlex resolution."""

from pydantic import BaseModel, Field

from bibvault.models import Candidate


class OpenAlexUnavailable(Exception):
    """OpenAlex will not answer again in this run."""

    def __init__(self, message: str, *, rate_limited: bool) -> None:
        super().__init__(message)
        self.message = message
        self.rate_limited = rate_limited


class Lookup(BaseModel):
    """What one metadata source said about a publication."""

    found: bool = False
    candidates: list[Candidate] = Field(default_factory=list)
    landing_url: str | None = None


class Resolution(BaseModel):
    candidates: list[Candidate] = Field(default_factory=list)
    found: bool = False
    landing_url: str | None = None
    error: str | None = None
    rate_limited: bool = False
