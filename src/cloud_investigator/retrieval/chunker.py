# Vendored from lellisz/incident-copilot (Apache-2.0), commit 3b7e761,
# src/incident_copilot/rag/chunker.py. Modifications: none (verbatim lift).
# See ATTRIBUTION.md and third_party/incident-copilot/.
"""Heading-aware markdown chunker.

Splits on markdown headings, keeps the full heading path, merges chunks below
MIN_TOKENS and splits chunks above MAX_TOKENS. Token counts are a deliberate
len // 4 heuristic (no tokenizer dependency — see docs/notes/embeddings.md).
"""

from __future__ import annotations

import hashlib
import re

from pydantic import BaseModel

MIN_TOKENS = 200
MAX_TOKENS = 800

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


class Chunk(BaseModel):
    doc_id: str
    heading_path: str
    content: str
    content_hash: str


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _content_hash(doc_id: str, heading_path: str, content: str) -> str:
    h = hashlib.sha256()
    h.update(f"{doc_id}\n{heading_path}\n{content}".encode())
    return h.hexdigest()


def _raw_sections(text: str) -> list[tuple[str, str]]:
    """(heading_path, content) in document order, headings excluded from content."""
    stack: list[tuple[int, str]] = []
    sections: list[tuple[str, str]] = []
    buf: list[str] = []

    def path() -> str:
        return " > ".join(title for _, title in stack)

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            sections.append((path(), body))

    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            flush()
            buf.clear()
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        else:
            buf.append(line)
    flush()
    return sections


def _common_path(paths: list[str]) -> str:
    parts = [p.split(" > ") for p in paths if p]
    if not parts:
        return ""
    common = parts[0]
    for other in parts[1:]:
        i = 0
        while i < len(common) and i < len(other) and common[i] == other[i]:
            i += 1
        common = common[:i]
    return " > ".join(common) or paths[0]


def _split_large(heading_path: str, content: str) -> list[tuple[str, str]]:
    paragraphs = re.split(r"\n\s*\n", content)
    pieces: list[str] = []
    cur: list[str] = []
    for para in paragraphs:
        candidate = "\n\n".join([*cur, para])
        if cur and estimate_tokens(candidate) > MAX_TOKENS:
            pieces.append("\n\n".join(cur))
            cur = [para]
        else:
            cur.append(para)
    if cur:
        pieces.append("\n\n".join(cur))
    return [(heading_path, p.strip()) for p in pieces if p.strip()]


def chunk_markdown(text: str, doc_id: str) -> list[Chunk]:
    sections = _raw_sections(text)

    # Merge small adjacent sections.
    merged: list[tuple[list[str], str]] = []
    for hp, body in sections:
        if merged and estimate_tokens(merged[-1][1]) < MIN_TOKENS:
            prev_paths, prev_body = merged[-1]
            merged[-1] = ([*prev_paths, hp], f"{prev_body}\n\n{body}")
        else:
            merged.append(([hp], body))

    # If the tail is still too small, fold it into the previous chunk.
    if len(merged) > 1 and estimate_tokens(merged[-1][1]) < MIN_TOKENS:
        paths, body = merged.pop()
        pp, pb = merged[-1]
        merged[-1] = ([*pp, *paths], f"{pb}\n\n{body}")

    # Split any oversized chunk, then materialise.
    out: list[Chunk] = []
    for paths, body in merged:
        hp = _common_path(paths)
        parts = _split_large(hp, body) if estimate_tokens(body) > MAX_TOKENS else [(hp, body)]
        for part_hp, part_body in parts:
            out.append(
                Chunk(
                    doc_id=doc_id,
                    heading_path=part_hp,
                    content=part_body,
                    content_hash=_content_hash(doc_id, part_hp, part_body),
                )
            )
    return out
