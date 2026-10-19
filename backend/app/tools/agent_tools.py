"""The only tools agents get: narrow vault reads/writes and the PhosphoSIGNOR graph.

No shell, no code execution, no general file access. Fixable mistakes (bad page name, unknown
protein, missing frontmatter) become ModelRetry so the model sees the message and corrects
its call; every result is capped to TOOL_OUTPUT_CHARS so one call can't blow the context.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic_ai import FunctionToolset, ModelRetry, RunContext

from app.config import get_settings
from app.graph import signor
from app.vault.store import Vault, VaultError, page_links

# --------------------------------------------------------------------------- deps / output


@dataclass
class Deps:
    vault: Vault
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)

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
            obj["note"] = ("Result shortened to fit the context. Use limit/min_score/offset or a "
                           "narrower query to see other items.")
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
    """Read index.md: every wiki page with a one-line description. Start here."""
    return out(ctx.deps.vault.read_index())


@_error_as_result
def list_pages(ctx: RunContext[Deps]) -> str:
    """List wiki pages with title, summary, last_updated and sources."""
    return out(ctx.deps.vault.list_pages())


@_error_as_result
def read_page(ctx: RunContext[Deps], name: str) -> str:
    """Read one wiki page (frontmatter + body) by name, e.g. 'protein-kinase-c'."""
    return out(ctx.deps.vault.read_page(name))


@_error_as_result
def search_pages(ctx: RunContext[Deps], query: str,
                 scope: Literal["wiki", "sources", "all"] = "wiki", limit: int = 10) -> str:
    """Case-insensitive text search over wiki pages and/or source notes. Returns names and matching lines."""
    return out(ctx.deps.vault.search(query, scope, max(1, min(limit, 30))))


@_error_as_result
def list_sources(ctx: RunContext[Deps]) -> str:
    """List files in sources/ with title, content_type and ingested_at."""
    return out(ctx.deps.vault.list_sources())


@_error_as_result
def read_source(ctx: RunContext[Deps], name: str, offset: int = 0, limit: int = 150) -> str:
    """Read a source by filename, a page of lines at a time. If remaining_lines > 0, call again with offset."""
    return out(ctx.deps.vault.read_source(name, offset, limit))


@_error_as_result
def read_log(ctx: RunContext[Deps], limit: int = 10) -> str:
    """Read the newest log.md entries (capture, sync, lint, digest)."""
    return out([e.as_dict() for e in ctx.deps.vault.log_entries()[: max(1, min(limit, 50))]])


# --------------------------------------------------------------------------- vault: write

@_retry_on_vault_error
def write_page(ctx: RunContext[Deps], name: str, content: str) -> str:
    """Create or overwrite a whole wiki page.

    Args:
        name: lowercase-hyphenated page name, e.g. 'protein-kinase-c' (no folder, no .md).
        content: the complete page: YAML frontmatter with title, summary and sources
            (list of source filenames), then the Markdown body. last_updated is set for you.
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
    if missing or missing_src:
        result["warning"] = ("Links to pages that don't exist yet: "
                             f"{[f'[[{m}]]' for m in missing]} "
                             f"{[f'[[source: {s}]]' for s in missing_src]}. "
                             "Create those pages, or fix the links before finishing.")
    return out(result)


@_retry_on_vault_error
def upsert_index(ctx: RunContext[Deps], name: str, description: str,
                 section: Literal["Pages", "Recaps"] = "Pages") -> str:
    """Add or update a page's line in index.md: '- [[name]] — description'."""
    return out({"status": ctx.deps.vault.upsert_index(name, description, section), "name": name})


# --------------------------------------------------------------------------- skills (chat)

@_error_as_result
def load_skill(ctx: RunContext[Deps], name: Literal["query", "capture", "sync", "lint", "digest"]) -> str:
    """Load a skill's instructions (SKILL.md) from the vault, e.g. 'query' for how to answer and cite."""
    path = ctx.deps.vault.root / ".agents" / "skills" / name / "SKILL.md"
    if not path.is_file():
        raise VaultError(f"No skill '{name}' in this vault.")
    return out(path.read_text(encoding="utf-8"))


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

READ_TOOLS = [read_index, list_pages, read_page, search_pages, list_sources, read_source, read_log]
WRITE_TOOLS = [write_page, upsert_index]


def vault_read_toolset() -> FunctionToolset[Deps]:
    return FunctionToolset(READ_TOOLS, max_retries=3)


def vault_write_toolset() -> FunctionToolset[Deps]:
    return FunctionToolset(WRITE_TOOLS, max_retries=5)


def skill_toolset() -> FunctionToolset[Deps]:
    return FunctionToolset([load_skill], max_retries=2)


def graph_toolset() -> FunctionToolset[Deps]:
    return FunctionToolset([_graph_tool(f) for f in signor.GRAPH_TOOLS], max_retries=3)
