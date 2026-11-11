from dataclasses import dataclass


@dataclass(frozen=True)
class Question:
    id: str
    text: str


@dataclass(frozen=True)
class Query:
    latest: str
    history: str
    search_text: str
    focus: str | None = None


@dataclass(frozen=True)
class Document:
    path: str
    text: str


@dataclass(frozen=True)
class Chunk:
    key: str
    path: str
    heading: str
    text: str


@dataclass(frozen=True)
class Evidence:
    chunk: Chunk
    rank: int
