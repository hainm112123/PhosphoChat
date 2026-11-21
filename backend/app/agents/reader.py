"""Code reads every captured source; the model only extracts and writes.

A paper of 100k+ characters doesn't fit one model request on small budgets (Groq's free tier
allows 8,000 tokens per request), and an agent paging through a source tends to stop early.
So code splits each source into parts at its section headings, skipping sections with no
findings (references, methods, acknowledgements...). A tool-less reader extracts from each
part the key findings and the kinase/phosphatase -> substrate relations, each with a quote.
Code keeps only quotes that appear verbatim in that part, labels each with its section and
page, records the relations as claims, and hands the writer compact notes (or, for a short
source, its full text).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import logging
import re
from collections.abc import Awaitable, Callable
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelAPIError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ThinkingPart, ToolCallPart, ToolReturnPart

from app.config import get_settings
from app.llm import get_model, model_name, model_settings
from app.vault.store import Vault, VaultError, split_frontmatter

Emit = Callable[[dict], Awaitable[None]]
PAGE_RE = re.compile(r"<!-- page (\d+) -->")
HEADING_RE = re.compile(r"#{1,3} +(.+)")
MAIN_SECTIONS = re.compile(
    r"abstract|summary|highlights|in brief|significance|introduction|background|"
    r"results?( and discussion)?|discussion|conclusions?|concluding remarks|outlook|"
    r"limitations( of (the|this) study)?", re.I)
OLD_RESULT_CHARS = 1500  # tool results from earlier steps are cut to this in an agent's history

# Measured with eval/reader_variants.py (gemini-3-flash, two runs): asking for every distinct result
# (up to 10) instead of "up to 5 of the most important" found all 8 key facts of nature18003 in
# both runs (old prompt: 4 and 6 of 8), with the same share of verbatim quotes.
READER_RULES = """\
You read one part of a scientific source for a research wiki about kinases, substrates, proteins,
drugs and diseases. Extract only what this part states; never add outside knowledge.

- findings: every distinct result in this part, up to 10: each measured effect, comparison,
  number, named protein, phosphosite or drug finding, and each conclusion the authors draw. For
  each, a short plain statement that keeps the numbers and names, and a quote that supports it,
  copied character for character from the text (one sentence or clause, under 300 characters).
  Prefer specific results over general background.
- relations: every explicit statement that a kinase or phosphatase phosphorylates or
  dephosphorylates a protein (optionally at a site such as S81 or Thr308). Use gene symbols.
  Give the quote that states it, copied exactly. Skip correlations, binding, co-expression,
  pathway mentions, hypotheses and negative results.

Return empty lists only for parts with no results (references, acknowledgements, boilerplate).
"""


class Finding(BaseModel):
    statement: str
    quote: str


class Relation(BaseModel):
    enzyme: str
    target: str
    site: str | None = None
    mechanism: Literal["phosphorylation", "dephosphorylation"] = "phosphorylation"
    effect: Literal["up-regulates", "down-regulates", "unknown"] = "unknown"
    quote: str


class PartNotes(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)


@lru_cache
def reader_agent() -> Agent[None, PartNotes]:
    # No in-run output retries: a retry would re-send the malformed reply (often long reasoning)
    # and can push the request over a per-request cap. read_in_parts retries with a fresh request.
    return Agent(name="phosphochat-reader", instructions=READER_RULES, output_type=PartNotes,
                 retries={"output": 0}, defer_model_check=True)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def section_name(heading: str) -> str:
    """'2.1 Results' -> 'Results'; 'IV. Discussion' -> 'Discussion'."""
    h = re.sub(r"^(?:\d+(?:\.\d+)*\.?|[IVX]+\.)\s+", "", heading.strip())
    return h.strip(" *_:#").strip()


@dataclass
class Part:
    page: int                           # page the part starts on
    text: str
    paras: list[tuple[str, int, str]]   # (section, page, paragraph), to say where a quote is from

    def where(self, quote: str) -> str:
        q = _norm(quote)
        for probe in (q, q[:60], q[:30]):
            for section, page, para in self.paras:
                if probe and probe in _norm(para):
                    return f"{section}, p. {page}" if section else f"p. {page}"
        section, page, _ = self.paras[0]
        return f"{section}, p. {page}" if section else f"p. {page}"


def _sub_part(paras: list[tuple[str, int, str]]) -> Part:
    return Part(paras[0][1], "\n\n".join(p for *_, p in paras), list(paras))


def _sections(body: str, size: int) -> list[tuple[str, list[tuple[int, str]]]]:
    """(section name, [(page, paragraph)]) in order; text before the first heading has name ''."""
    sections: list[tuple[str, list[tuple[int, str]]]] = [("", [])]
    page = 1
    for para in re.split(r"\n\s*\n", body):
        markers = PAGE_RE.findall(para)
        text = PAGE_RE.sub("", para).strip()
        if markers and para.lstrip().startswith("<!--"):
            page = int(markers[0])
        if not text:
            if markers:
                page = int(markers[-1])
            continue
        first, _, rest = text.partition("\n")
        m = HEADING_RE.fullmatch(first.strip())
        if m:
            sections.append((section_name(m.group(1)), [(page, first.strip())]))
            text = rest.strip()
        if text:
            sections[-1][1].append((page, text[:size * 2]))  # a pathological paragraph can't swallow the budget
        if markers:
            page = int(markers[-1])
    return [s for s in sections if s[1]]


def split_parts(body: str, size: int, skip: str | None = None) -> list[Part]:
    """Parts of at most ~size characters, cut between sections where possible, then between
    paragraphs. Sections whose heading matches `skip` are left out, unless that would leave
    almost nothing (a source whose headings were misread)."""
    sections = _sections(body, size)
    if skip:
        pattern = re.compile(skip, re.I)
        kept, skipping = [], False
        for s in sections:
            # A skipped section's sub-headings ("Protein extraction" under Methods) are skipped
            # with it, up to the next main section.
            if s[0] and pattern.fullmatch(s[0]):
                skipping = True
            elif s[0] and MAIN_SECTIONS.fullmatch(s[0]):
                skipping = False
            if not skipping:
                kept.append(s)
        total = sum(len(p) for _, ps in sections for _, p in ps)
        if sum(len(p) for _, ps in kept for _, p in ps) >= 0.3 * total:
            sections = kept
    parts: list[Part] = []
    current: list[tuple[str, int, str]] = []

    def flush():
        if current:
            parts.append(Part(current[0][1], "\n\n".join(p for _, _, p in current), list(current)))
            current.clear()
    for name, paras in sections:
        if current and sum(len(p) for *_, p in current) + sum(len(p) for _, p in paras) > size:
            flush()
        for page, para in paras:
            if current and sum(len(p) for *_, p in current) + len(para) > size:
                flush()
                if name:
                    current.append((name, page, f"## {name} (continued)"))
            current.append((name, page, para))
    flush()
    return parts


@dataclass
class Notes:
    source: str
    parts: int
    findings: list[tuple[str, Finding]] = field(default_factory=list)   # (where, finding)
    claims_added: int = 0
    rejected_quotes: int = 0
    rejected_relations: int = 0
    failed_parts: int = 0
    relations: list[Relation] = field(default_factory=list)  # the accepted ones, to re-record on a retry
    full_text: str | None = None   # a short source: the writer gets it whole

    def render(self, cap: int) -> str:
        """Notes for the writer, capped: the full text of a short source, otherwise the
        findings, spread over the whole source."""
        if self.full_text and len(self.full_text) <= cap:
            return "Full text:\n" + self.full_text
        lines = [f"- ({w}) {f.statement} — \"{f.quote}\"" for w, f in self.findings]
        out, used = [], 0
        step = max(1, len(lines) // max(1, cap // 220)) if sum(map(len, lines)) > cap else 1
        for line in lines[::step]:
            if used + len(line) > cap:
                break
            out.append(line)
            used += len(line) + 1
        return "\n".join(out) or "(no findings could be extracted)"


def _record(vault: Vault, r: Relation, source: str):
    from app.knowledge.claims import record
    return record(vault, enzyme=r.enzyme, target=r.target, site=r.site, mechanism=r.mechanism,
                  effect=r.effect, source=source, quote=r.quote)


async def read_in_parts(vault: Vault, source: str, emit: Emit, claims: list, model: str | None = None,
                        waits: tuple[int, ...] = (20, 60, 90)) -> Notes:
    from app.agents.runtime import is_bad_tool_call, is_too_large, is_transient
    from app.knowledge.claims import statuses
    s = get_settings()
    _, body = split_frontmatter(vault.source_path(source).read_text(encoding="utf-8", errors="replace"))
    key = (hashlib.sha256(body.encode("utf-8")).hexdigest(), model_name(model, "job"),
           s.capture_part_chars, s.capture_skip_sections,
           hashlib.sha256(READER_RULES.encode("utf-8")).hexdigest()[:12])  # a new reader prompt reads again
    cached = _cache.get(key) or _disk_get(key)
    if cached is not None:  # a retried capture: reuse the reading, only the claims (rolled back) are recorded again
        notes = replace(cached, source=source, claims_added=0)
        for r in cached.relations:
            claim, added = _record(vault, r, source)
            if added:
                notes.claims_added += 1
                claims.append(statuses([claim])[0])
        await emit({"type": "info", "message": f"Using the reading of {source} from the earlier attempt "
                                               f"({len(notes.findings)} findings, {notes.claims_added} relations)."})
        return notes
    parts = split_parts(body, s.capture_part_chars, s.capture_skip_sections)
    notes = Notes(source=source, parts=len(parts),
                  full_text=body.strip() if len(body) <= s.capture_notes_chars else None)
    queue, i = list(parts), 0
    while queue:
        part = queue.pop(0)
        i += 1
        await emit({"type": "info", "message": f"Reading {source}: part {i} of {i + len(queue)}"})
        result, pending, slipped = None, list(waits), False
        while result is None:
            try:
                result = (await reader_agent().run(part.text, model=get_model(model, "job"),
                                                   model_settings=model_settings())).output
            except Exception as e:  # noqa: BLE001
                if pending and is_transient(e):
                    await asyncio.sleep(pending.pop(0))
                    continue
                malformed = is_bad_tool_call(e) or isinstance(e, UnexpectedModelBehavior)
                if (malformed or is_too_large(e)) and not slipped:  # worth one more, fresh request
                    slipped = True
                    continue
                if (malformed or is_too_large(e)) and len(part.paras) > 1:
                    half = len(part.paras) // 2   # read it as two smaller parts instead
                    queue[:0] = [_sub_part(part.paras[:half]), _sub_part(part.paras[half:])]
                    notes.parts += 1
                    await emit({"type": "info", "message": f"Part {i} failed ({type(e).__name__}); reading it in two halves."})
                    i -= 1
                    break
                if isinstance(e, ModelAPIError) and not is_transient(e) and not malformed and not is_too_large(e):
                    raise  # daily quota, bad key, unknown model...: every other part would fail too
                notes.failed_parts += 1
                await emit({"type": "info", "message": f"Part {i} could not be read ({type(e).__name__}); skipped."})
                break
        if result is None:
            continue
        flat = _norm(part.text)
        for f in result.findings:
            if _norm(f.quote) and _norm(f.quote) in flat:
                notes.findings.append((part.where(f.quote), f))
            else:
                notes.rejected_quotes += 1
        for r in result.relations:
            try:
                claim, added = _record(vault, r, source)
            except VaultError:
                notes.rejected_relations += 1
                continue
            notes.relations.append(r)
            if added:
                notes.claims_added += 1
                claims.append(statuses([claim])[0])
    if not notes.failed_parts:
        _cache[key] = notes
        while len(_cache) > 50:
            _cache.pop(next(iter(_cache)))
        _disk_put(key, notes)
    await emit({"type": "info", "message": (
        f"Read {source} in {len(parts)} parts: {len(notes.findings)} findings, {notes.claims_added} relations recorded"
        + (f", {notes.failed_parts} parts skipped" if notes.failed_parts else "") + ".")})
    return notes


# Readings of sources already done in this process, so a retried capture doesn't pay for them again.
_cache: dict[tuple, Notes] = {}


def _disk_file(key: tuple):
    """READING_CACHE_DIR (unset in the app; the evaluation sets it) keeps readings across
    processes, so trying a new writer prompt doesn't pay for reading the papers again."""
    folder = os.environ.get("READING_CACHE_DIR", "").strip()
    if not folder:
        return None
    from pathlib import Path
    name = hashlib.sha256(json.dumps(key, default=str).encode("utf-8")).hexdigest()[:32]
    return Path(folder) / f"{name}.json"


def _disk_get(key: tuple) -> Notes | None:
    path = _disk_file(key)
    if path is None or not path.is_file():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return Notes(source=d["source"], parts=d["parts"],
                     findings=[(w, Finding(**f)) for w, f in d["findings"]],
                     rejected_quotes=d.get("rejected_quotes", 0),
                     rejected_relations=d.get("rejected_relations", 0),
                     relations=[Relation(**r) for r in d["relations"]], full_text=d.get("full_text"))
    except (ValueError, KeyError, TypeError):
        return None  # an unreadable entry is just a miss


def _disk_put(key: tuple, notes: Notes) -> None:
    path = _disk_file(key)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "key": list(key), "source": notes.source, "parts": notes.parts,
        "findings": [[w, f.model_dump()] for w, f in notes.findings],
        "relations": [r.model_dump() for r in notes.relations],
        "rejected_quotes": notes.rejected_quotes, "rejected_relations": notes.rejected_relations,
        "full_text": notes.full_text}, ensure_ascii=False), encoding="utf-8")



def history_chars() -> int:
    """After the task (with its notes), the rest of an agent's history is kept under about this
    many characters (settings.agent_history_chars): every request re-sends the whole history."""
    return get_settings().agent_history_chars


def _chars(m: ModelMessage) -> int:
    n = 0
    for p in m.parts:
        if isinstance(p, ToolCallPart):
            n += len(p.args_as_json_str())
        else:
            n += len(str(getattr(p, "content", "") or ""))
    return n


def _current_reads(messages: list[ModelMessage]) -> list[tuple[str, int]]:
    """(tool_call_id, size) of the latest read_page of each page not written since, newest first.
    Cutting these made writers re-read the same pages until the request limit: the page text they
    are about to update must stay in the history."""
    names: dict[str, str] = {}   # read_page call id -> page name
    latest: dict[str, tuple[int, str, int]] = {}   # page -> (position, call id, size)
    for pos, m in enumerate(messages):
        for p in m.parts:
            if isinstance(p, ToolCallPart) and p.tool_name in ("read_page", "write_page"):
                name = str(p.args_as_dict().get("name", ""))
                if p.tool_name == "read_page":
                    names[p.tool_call_id] = name
                else:
                    latest.pop(name, None)   # written since: the agent knows what it wrote
            elif isinstance(p, ToolReturnPart) and p.tool_call_id in names and isinstance(p.content, str):
                latest[names[p.tool_call_id]] = (pos, p.tool_call_id, len(p.content))
    return [(i, size) for _, i, size in sorted(latest.values(), reverse=True)]


def _compact(messages: list[ModelMessage], result_chars: int, page_chars: int,
             keep: set[str] = frozenset()) -> list[ModelMessage]:
    out = []
    for i, m in enumerate(messages):
        if isinstance(m, ModelRequest) and 0 < i < len(messages) - 2:
            # Older tool results (an index, a page read earlier) are cut short; the latest stay
            # whole, and so do the pages in `keep`.
            m = replace(m, parts=[replace(p, content=p.content[:result_chars] + _CUT)
                                  if isinstance(p, ToolReturnPart) and isinstance(p.content, str)
                                  and p.tool_call_id not in keep
                                  and len(p.content) > result_chars else p for p in m.parts])
        if isinstance(m, ModelResponse) and i < len(messages) - 1:
            parts = []
            for p in m.parts:
                if isinstance(p, ThinkingPart):
                    continue  # reasoning from earlier steps: re-sending it only costs tokens
                if isinstance(p, ToolCallPart) and p.tool_name == "write_page":
                    args = p.args_as_dict()
                    if len(str(args.get("content", ""))) > page_chars:
                        args = {**args, "content": f"{_WRITTEN}{len(str(args.get('content', '')))} "
                                                   "characters; use read_page to see it)"}
                        p = replace(p, args=args)
                parts.append(p)
            m = replace(m, parts=parts)
        out.append(m)
    return out


# The processed history is stored back as the run's history, so without this a cut would be
# permanent: a page cut once could never be kept whole again, and placeholders were cut into
# placeholders. Originals are kept by tool-call id (unique per call) and put back first.
_ORIGINALS: OrderedDict[str, str] = OrderedDict()
_ORIGINALS_MAX = 2000
_CUT = " ... (cut; call again for the rest)"
_WRITTEN = "(page written earlier, "


def _remember(key: str, text: str) -> None:
    _ORIGINALS[key] = text
    _ORIGINALS.move_to_end(key)
    while len(_ORIGINALS) > _ORIGINALS_MAX:
        _ORIGINALS.popitem(last=False)


def _restore(messages: list[ModelMessage]) -> list[ModelMessage]:
    out = []
    for m in messages:
        parts = []
        for p in m.parts:
            if isinstance(p, ToolReturnPart) and isinstance(p.content, str):
                if p.content.endswith(_CUT) and p.tool_call_id in _ORIGINALS:
                    p = replace(p, content=_ORIGINALS[p.tool_call_id])
                elif not p.content.endswith(_CUT):
                    _remember(p.tool_call_id, p.content)
            elif isinstance(p, ToolCallPart) and p.tool_name == "write_page":
                content = str(p.args_as_dict().get("content", ""))
                key = "write:" + p.tool_call_id
                if content.startswith(_WRITTEN) and key in _ORIGINALS:
                    p = replace(p, args={**p.args_as_dict(), "content": _ORIGINALS[key]})
                elif not content.startswith(_WRITTEN):
                    _remember(key, content)
            parts.append(p)
        out.append(replace(m, parts=parts))
    return out


def compact_history(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Before each model request: drop earlier reasoning, replace pages the agent already wrote
    with a placeholder (read_page fetches them) and cut older tool results, but keep the current
    text of pages it read and hasn't written yet. If the history is still over history_chars(),
    everything else is cut harder and only the newest of those pages stay whole (up to 70%).
    It always works from the original messages (see _restore)."""
    messages = _restore(messages)
    budget, reads = history_chars(), _current_reads(messages)
    out = _compact(messages, OLD_RESULT_CHARS, 300, {i for i, _ in reads})
    if sum(_chars(m) for m in out[1:]) > budget:
        keep, used = set(), 0
        for i, size in reads:
            if used + size > budget * 0.7:
                break
            keep.add(i)
            used += size
        out = _compact(messages, 200, 0, keep)
    log.info("agent request: %d messages, %d characters (task %d, latest message %d)", len(out),
             sum(map(_chars, out)), _chars(out[0]), _chars(out[-1]))
    return out


log = logging.getLogger("phosphochat.history")
