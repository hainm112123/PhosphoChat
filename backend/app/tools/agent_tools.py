"""The only tools agents get: narrow vault reads/writes and the kinase-substrate graph.

No shell, no code execution, no general file access. Fixable mistakes (bad page name, unknown
protein, missing frontmatter) become ModelRetry so the model sees the message and corrects
its call; every result is capped to TOOL_OUTPUT_CHARS so one call can't blow the context.

Tool definitions are sent with every model request, so each agent gets only the tools its
work needs, with one-line descriptions (see JOB_TOOLS and chat_toolsets).
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic_ai import FunctionToolset, ModelRetry, RunContext

from app.config import get_settings
from app.graph import agent_tools as graph_tools
from app.vault.store import Vault, VaultError, page_links

# --------------------------------------------------------------------------- deps / output


@dataclass
class Deps:
    vault: Vault
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    claims: list[dict] = field(default_factory=list)   # relation claims recorded in this run

    def touched(self) -> list[str]:
        return self.created + [p for p in self.updated if p not in self.created]


def _shrink(obj: Any, budget: int) -> tuple[Any, bool]:
    """Halve the longest list in a result until its JSON fits the budget."""
    cut = False
    while len(json.dumps(obj, ensure_ascii=False, default=str)) > budget:
        lists = _lists(obj)
        if not lists:
            break
        holder, key, lst = max(lists, key=lambda t: len(json.dumps(t[2], default=str)))
        if len(lst) <= 1:
            break
        holder[key] = lst[: len(lst) // 2]
        cut = True
    return obj, cut


def _lists(obj: Any) -> list[tuple[Any, Any, list]]:
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, list):
                found.append((obj, k, v))
            found += _lists(v)
    elif isinstance(obj, list):
        for x in obj:
            found += _lists(x)
    return found


def out(obj: Any) -> str:
    """Serialise a tool result within the character budget."""
    budget = get_settings().tool_output_chars
    if isinstance(obj, str):
        text = obj
    else:
        obj, cut = _shrink(obj, budget)
        if cut and isinstance(obj, dict):
            obj["note"] = "Result shortened to fit the context. Ask a narrower question to see other items."
        text = json.dumps(obj, ensure_ascii=False, default=str)
    if len(text) > budget:
        text = text[:budget] + f"\n...[truncated {len(text) - budget} chars]"
    return text


def _retry_on_vault_error(fn):
    """Writes: a malformed page must be fixed, so the model is asked to retry the call."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except VaultError as e:
            raise ModelRetry(str(e)) from e
    return wrapper


def _error_as_result(fn):
    """Reads: 'not found' is an answer, not a failure. Returning it (instead of ModelRetry)
    means a model that keeps guessing names can't exhaust the retry budget and abort the run."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except VaultError as e:
            return out({"error": str(e)})
    return wrapper


# --------------------------------------------------------------------------- vault: read

@_error_as_result
def read_index(ctx: RunContext[Deps]) -> str:
    """index.md: every wiki page with a one-line description."""
    return out(ctx.deps.vault.read_index())


@_error_as_result
def list_pages(ctx: RunContext[Deps]) -> str:
    """Wiki pages with title, summary, last_updated and sources."""
    return out(ctx.deps.vault.list_pages())


@_error_as_result
def read_page(ctx: RunContext[Deps], name: str) -> str:
    """One wiki page (frontmatter and body) by name, e.g. 'protein-kinase-c'."""
    return out(ctx.deps.vault.read_page(name))


@_error_as_result
def search_pages(ctx: RunContext[Deps], query: str, scope: Literal["wiki", "sources", "all"] = "wiki") -> str:
    """Case-insensitive text search over wiki pages and/or sources: names and matching lines."""
    return out(ctx.deps.vault.search(query, scope, 10))


@_error_as_result
def list_sources(ctx: RunContext[Deps]) -> str:
    """Files in sources/ with title, content_type and ingested_at."""
    return out(ctx.deps.vault.list_sources())


@_error_as_result
def vault_claims(ctx: RunContext[Deps], protein: str | None = None,
                 status: Literal["confirmed", "new", "conflicting"] | None = None) -> str:
    """Kinase-substrate claims from this vault's sources, with status against the databases, quote and source."""
    from app.knowledge.claims import list_claims
    return out(list_claims(ctx.deps.vault, protein, status))


# --------------------------------------------------------------------------- vault: write

@_retry_on_vault_error
def write_page(ctx: RunContext[Deps], name: str, content: str) -> str:
    """Create or overwrite a whole wiki page.

    Args:
        name: lowercase-hyphenated, e.g. 'protein-kinase-c'.
        content: the complete page: frontmatter (title, summary, sources), then the Markdown body.
    """
    v = ctx.deps.vault
    status = v.write_page(name, content)
    name = v.page_name(name)
    target = ctx.deps.created if status == "created" else ctx.deps.updated
    if name not in ctx.deps.created and name not in target:
        target.append(name)
    links, src_links = page_links(content)
    sources = {s["name"] for s in v.list_sources()}
    missing = sorted(l for l in links if not v.page_exists(l))
    missing_src = sorted(s for s in src_links if s not in sources)
    result: dict[str, Any] = {"status": status, "name": name}
    if missing:
        result["note"] = (f"Not written yet: {', '.join(missing)}. Write the ones worth a page; links "
                          "still missing at the end become plain text, so don't rewrite a page just for them.")
    if missing_src:
        result["warning"] = (f"Unknown sources cited: {', '.join(missing_src)}. Cite exact source filenames.")
    return out(result)


@_retry_on_vault_error
def upsert_index(ctx: RunContext[Deps], name: str, description: str,
                 section: Literal["Pages", "Recaps"] = "Pages") -> str:
    """Add or update a page's line in index.md."""
    return out({"status": ctx.deps.vault.upsert_index(name, description, section), "name": name})


# --------------------------------------------------------------------------- graph

def _graph_tool(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return out(fn(*args, **kwargs))
        except ValueError as e:  # unknown name etc.: the message says what to try next
            return out({"error": str(e)})
        except RuntimeError as e:  # database down: tell the model, don't crash the run
            return out({"error": str(e)})
    wrapper.__annotations__ = {**fn.__annotations__, "return": str}
    return wrapper


# --------------------------------------------------------------------------- toolsets

WRITE_TOOLS = [write_page, upsert_index]
_WRITER_READS = [read_index, list_pages, read_page]
JOB_TOOLS: dict[str, list] = {   # per job kind: everything else is left out of the request
    "capture": [*_WRITER_READS, *WRITE_TOOLS],
    "sync": [*_WRITER_READS, *WRITE_TOOLS],
    "digest": [*_WRITER_READS, *WRITE_TOOLS],
    "lint": [*_WRITER_READS, search_pages, list_sources, *WRITE_TOOLS],
}
CHAT_TOOLS = [read_page, search_pages, vault_claims]


def job_toolsets() -> list:
    """The tools of every job kind; each run sees only its kind's (ctx.deps.kind)."""
    names = {k: {f.__name__ for f in fs} for k, fs in JOB_TOOLS.items()}
    keep = lambda ctx, tool: tool.name in names[ctx.deps.kind]  # noqa: E731
    reads = {f.__name__: f for fs in JOB_TOOLS.values() for f in fs if f not in WRITE_TOOLS}
    return [FunctionToolset(list(reads.values()), max_retries=3).filtered(keep),
            FunctionToolset(WRITE_TOOLS, max_retries=5).filtered(keep)]


def job_tool_names(kind: str) -> set[str]:
    return {f.__name__ for f in JOB_TOOLS[kind]}


def chat_toolsets() -> list:
    return [FunctionToolset(CHAT_TOOLS, max_retries=3),
            FunctionToolset([_graph_tool(f) for f in graph_tools.GRAPH_TOOLS], max_retries=3)]


def chat_tool_names() -> set[str]:
    return {f.__name__ for f in (*CHAT_TOOLS, *graph_tools.GRAPH_TOOLS)}
