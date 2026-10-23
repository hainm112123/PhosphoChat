"""Vault: a folder of Markdown (sources/, wiki/, index.md, log.md) plus its own git repo.

Every rule the LLM-Wiki schema states is enforced here rather than trusted to the model:
paths stay inside the vault, sources are create-only, wiki pages need frontmatter, the log
is prepended under its heading, and each job ends in one git commit.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import yaml

TEMPLATE_DIR = Path(__file__).resolve().parent / "template"

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VAULT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
LOG_HEADING = "# Log"
REQUIRED_PAGE_KEYS = ("title", "summary", "sources")
SYSTEM_PAGES = {"lint-report"}
TEXT_EXTS = {".md", ".txt", ".markdown"}
TABLE_EXTS = {".tsv", ".csv"}
ContentType = Literal["article", "paper", "transcript", "data", "note", "other"]


class VaultError(ValueError):
    """A request the caller (often the model) can correct."""


class DuplicateSource(VaultError):
    pass


# --------------------------------------------------------------------------- helpers

def slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:max_len].rstrip("-")) or "untitled"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def split_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise VaultError(f"Frontmatter is not valid YAML: {e}") from e
    if not isinstance(meta, dict):
        raise VaultError("Frontmatter must be a YAML mapping (key: value lines).")
    return meta, text[m.end():]


def join_frontmatter(meta: dict, body: str) -> str:
    dumped = yaml.safe_dump(_plain(meta), sort_keys=False, allow_unicode=True,
                            default_flow_style=False).strip()
    return f"---\n{dumped}\n---\n\n{body.lstrip()}"


def _plain(v):
    """YAML-load turns dates into date objects; keep them as ISO strings."""
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def page_links(text: str) -> tuple[set[str], set[str]]:
    """(wiki page links, source links) found in a page."""
    pages, sources = set(), set()
    for raw in LINK_RE.findall(text):
        raw = raw.strip()
        if raw.lower().startswith("source:"):
            sources.add(raw.split(":", 1)[1].strip())
        else:
            pages.add(raw.removesuffix(".md"))
    return pages, sources


CITATION_RE = re.compile(r"\[\[\s*source:|SIGNOR-[A-Z]*\d+|\bPMIDs?\b", re.I)
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}")


def _blocks(body: str) -> list[str]:
    """Paragraphs, list items and table body rows of a Markdown body (blockquotes unwrapped).

    Headings, code fences, HTML comments, table header/separator rows and callout title
    lines are not blocks."""
    lines, out, para, in_code, table_row, in_item = body.splitlines(), [], [], False, 0, False
    def flush():
        if para:
            out.append(" ".join(x.strip() for x in para))
            para.clear()
    for raw in lines:
        line = raw
        if line.lstrip().startswith("```"):
            flush()
            in_code = not in_code
            continue
        if in_code:
            continue
        while line.lstrip().startswith(">"):  # unwrap blockquotes / callouts
            line = line.lstrip()[1:].lstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "<!--")):
            flush()
            table_row, in_item = 0, False
            continue
        if re.match(r"^\[!\w+\]", stripped):  # callout title line
            flush()
            continue
        if stripped.startswith("|"):
            flush()
            table_row += 1
            if table_row > 1 and not TABLE_SEP_RE.match(stripped):
                out.append(stripped)
            continue
        table_row = 0
        if LIST_ITEM_RE.match(line):
            flush()
            out.append(LIST_ITEM_RE.sub("", line, count=1).strip())
            in_item = True
            continue
        if in_item and raw[:1] in (" ", "	"):  # wrapped continuation of a list item
            out[-1] += " " + stripped
            continue
        in_item = False
        para.append(stripped)
    flush()
    return out


def uncited_blocks(body: str, allow_page_links: bool = False) -> list[str]:
    """Blocks with no citation. A citation is [[source: ...]], a SIGNOR id or a PMID
    (or any [[page]] link when allow_page_links, for recaps). Exempt: the **Summary:** line
    and blocks that are just links (e.g. a 'See also' list)."""
    missing = []
    for block in _blocks(body):
        if CITATION_RE.search(block):
            continue
        links = LINK_RE.findall(block)
        if links and allow_page_links:
            continue
        if block.startswith(("**Summary:**", "**Summary**:", "Summary:")):
            continue
        if block.endswith(":"):  # lead-in to a list; the items themselves are checked
            continue
        rest = re.sub(r"[^A-Za-z]+", " ", LINK_RE.sub(" ", block)).split()
        if links and len(rest) <= 3:  # link-only lines like "Related: [[a]], [[b]]"
            continue
        missing.append(block if len(block) <= 160 else block[:157] + "...")
    return missing


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def extract_text(filename: str, data: bytes) -> tuple[str, ContentType]:
    """Readable Markdown body for an uploaded or dropped-in file."""
    ext = Path(filename).suffix.lower()
    if ext in TEXT_EXTS:
        return data.decode("utf-8", errors="replace"), "note"
    if ext in TABLE_EXTS:
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        sep = "\t" if ext == ".tsv" else ","
        header = lines[0].split(sep) if lines else []
        preview = "\n".join(lines[:31])
        return (f"Data file `{filename}`: {max(len(lines) - 1, 0)} rows, "
                f"{len(header)} columns.\n\n## Columns\n"
                + "".join(f"- `{c}`\n" for c in header)
                + f"\n## First rows\n```\n{preview}\n```\n"), "data"
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
        return "\n\n".join(f"<!-- page {i + 1} -->\n{t}" for i, t in enumerate(pages) if t), "paper"
    raise VaultError(f"Unsupported file type '{ext}'. Use .md, .txt, .csv, .tsv or .pdf.")


@dataclass
class LogEntry:
    timestamp: str
    kind: str
    body: str

    def as_dict(self) -> dict:
        return {"timestamp": self.timestamp, "kind": self.kind, "body": self.body}


# --------------------------------------------------------------------------- vault

class Vault:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.name = self.root.name
        self.lock = asyncio.Lock()  # serialises write jobs

    # ---- paths

    @property
    def wiki_dir(self) -> Path:
        return self.root / "wiki"

    @property
    def sources_dir(self) -> Path:
        return self.root / "sources"

    def _inside(self, path: Path) -> Path:
        p = path.resolve()
        if p != self.root and self.root not in p.parents:
            raise VaultError("Path escapes the vault.")
        return p

    @staticmethod
    def page_name(name: str) -> str:
        n = name.strip().removeprefix("wiki/").removesuffix(".md").strip()
        n = n.removeprefix("[[").removesuffix("]]")
        if not NAME_RE.match(n):
            raise VaultError(f"Invalid page name '{name}'. Use lowercase letters, digits and "
                             f"hyphens, e.g. '{slugify(n)}'.")
        return n

    def page_path(self, name: str) -> Path:
        return self._inside(self.wiki_dir / f"{self.page_name(name)}.md")

    def source_path(self, name: str) -> Path:
        n = name.strip().removeprefix("sources/")
        if not n or "/" in n or "\\" in n or n.startswith("."):
            raise VaultError(f"Invalid source name '{name}'.")
        return self._inside(self.sources_dir / n)

    # ---- pages

    def page_names(self) -> list[str]:
        return sorted(p.stem for p in self.wiki_dir.glob("*.md") if NAME_RE.match(p.stem))

    def page_exists(self, name: str) -> bool:
        try:
            return self.page_path(name).is_file()
        except VaultError:
            return False

    def read_page(self, name: str) -> str:
        path = self.page_path(name)
        if not path.is_file():
            raise VaultError(f"No page '{self.page_name(name)}'. Use list_pages or search_pages.")
        return path.read_text(encoding="utf-8")

    def page_meta(self, name: str) -> dict:
        try:
            meta, _ = split_frontmatter(self.read_page(name))
        except VaultError:
            meta = {}
        return _plain(meta)

    def list_pages(self) -> list[dict]:
        out = []
        for n in self.page_names():
            m = self.page_meta(n)
            out.append({"name": n, "title": m.get("title", n), "summary": m.get("summary", ""),
                        "last_updated": m.get("last_updated"),
                        "sources": m.get("sources") or []})
        return out

    def write_page(self, name: str, content: str) -> Literal["created", "updated"]:
        """Write a whole wiki page. Validates frontmatter and sets last_updated."""
        name = self.page_name(name)
        meta, body = split_frontmatter(content)
        missing = [k for k in REQUIRED_PAGE_KEYS if k not in meta]
        if missing:
            raise VaultError(
                f"Page is missing frontmatter {missing}. Start the content with:\n"
                "---\ntitle: <Title>\nsummary: <one sentence>\nsources:\n  - <source file>.md\n---")
        srcs = meta.get("sources") or []
        if isinstance(srcs, str):
            srcs = [srcs]
        if not isinstance(srcs, list):
            raise VaultError("'sources' must be a list of source filenames.")
        srcs = [str(s).strip().removeprefix("sources/").removeprefix("[[source:").removesuffix("]]").strip()
                for s in srcs]
        unknown = [s for s in srcs if not (self.sources_dir / s).is_file()]
        if unknown:
            raise VaultError(f"Unknown sources {unknown}. Use exact filenames from list_sources.")
        if not body.strip():
            raise VaultError("Page body is empty.")
        name_ = self.page_name(name)
        if name_ not in SYSTEM_PAGES:
            uncited = uncited_blocks(body, allow_page_links=name_.endswith("-recap"))
            if uncited:
                raise VaultError(
                    f"{len(uncited)} paragraph(s)/list item(s)/table row(s) have no citation. End each "
                    "with [[source: <file>.md]] or (SIGNOR-...; PMID ...) if a source states it; "
                    "otherwise remove it. Uncited:\n" + "\n".join(f"- {b}" for b in uncited[:5]))
        meta = {"title": meta["title"], "summary": meta["summary"], "sources": srcs,
                "last_updated": date.today().isoformat(),
                **{k: v for k, v in meta.items() if k not in (*REQUIRED_PAGE_KEYS, "last_updated")}}
        path = self.page_path(name)
        existed = path.is_file()
        _atomic_write(path, join_frontmatter(meta, body))
        return "updated" if existed else "created"

    def search(self, query: str, scope: Literal["wiki", "sources", "all"] = "wiki",
               limit: int = 10) -> list[dict]:
        q = query.strip().lower()
        if not q:
            raise VaultError("Empty query.")
        files: list[tuple[str, Path]] = []
        if scope in ("wiki", "all"):
            files += [("page", self.wiki_dir / f"{n}.md") for n in self.page_names()]
        if scope in ("sources", "all"):
            files += [("source", p) for p in sorted(self.sources_dir.glob("*.md"))]
        hits = []
        for kind, path in files:
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            matched = [ln.strip() for ln in lines if q in ln.lower()]
            if not matched and q not in path.stem.lower():
                continue
            hits.append({"kind": kind, "name": path.stem if kind == "page" else path.name,
                         "matches": len(matched),
                         "snippets": [m[:240] for m in matched[:3]]})
        hits.sort(key=lambda h: -h["matches"])
        return hits[:limit]

    def backlinks(self, name: str) -> list[str]:
        name = self.page_name(name)
        out = []
        for n in self.page_names():
            if n == name:
                continue
            links, _ = page_links(self.read_page(n))
            if name in links:
                out.append(n)
        return out

    # ---- sources

    def list_sources(self) -> list[dict]:
        out = []
        for p in sorted(self.sources_dir.iterdir()):
            if p.name.startswith(".") or not p.is_file():
                continue
            item = {"name": p.name, "size": p.stat().st_size}
            if p.suffix == ".md":
                try:
                    meta, _ = split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
                except VaultError:
                    meta = {}
                meta = _plain(meta)
                item.update(title=meta.get("title"), content_type=meta.get("content_type"),
                            ingested_at=meta.get("ingested_at"), source_url=meta.get("source_url"))
            out.append(item)
        return out

    def read_source(self, name: str, offset: int = 0, limit: int = 200) -> dict:
        path = self.source_path(name)
        if not path.is_file():
            raise VaultError(f"No source '{name}'. Use list_sources.")
        if path.suffix.lower() not in TEXT_EXTS | TABLE_EXTS:
            raise VaultError(f"'{name}' is binary; read its sibling note "
                             f"'{path.stem}.md' instead.")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 1000))
        chunk = lines[offset:offset + limit]
        remaining = max(0, len(lines) - offset - len(chunk))
        return {"name": path.name, "total_lines": len(lines), "offset": offset,
                "remaining_lines": remaining, "text": "\n".join(chunk)}

    def _source_hashes_and_urls(self) -> tuple[set[str], set[str]]:
        hashes, urls = set(), set()
        for p in self.sources_dir.glob("*.md"):
            try:
                meta, _ = split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
            except VaultError:
                continue
            if meta.get("sha256"):
                hashes.add(str(meta["sha256"]))
            url = str(meta.get("source_url") or "")
            if url.startswith(("http://", "https://")):
                urls.add(url.rstrip("/"))
        return hashes, urls

    def save_source(self, *, title: str, body: str, source_url: str,
                    content_type: ContentType = "other",
                    original: tuple[str, bytes] | None = None) -> str:
        """Create a new immutable source note (and optionally keep the original file).

        Returns the note's filename. Raises DuplicateSource for a URL or content seen before.
        """
        digest = hashlib.sha256((original[1] if original else body.encode("utf-8"))).hexdigest()
        hashes, urls = self._source_hashes_and_urls()
        if source_url.startswith(("http://", "https://")) and source_url.rstrip("/") in urls:
            raise DuplicateSource(f"A source with URL {source_url} already exists.")
        if digest in hashes:
            raise DuplicateSource("A source with identical content already exists.")

        self.sources_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{date.today().isoformat()}-{slugify(title)}"
        i = 1
        candidate = stem
        while (self.sources_dir / f"{candidate}.md").exists() or (
                original and (self.sources_dir / f"{candidate}{Path(original[0]).suffix.lower()}").exists()):
            i += 1
            candidate = f"{stem}-{i}"
        if original:
            orig_path = self.sources_dir / f"{candidate}{Path(original[0]).suffix.lower()}"
            if orig_path.suffix != ".md":
                orig_path.write_bytes(original[1])
            else:
                original = None  # the note itself is the original
        meta = {"title": title, "source_url": source_url, "content_type": content_type,
                "ingested_at": now_iso(), "sha256": digest}
        if original:
            meta["original_file"] = f"{candidate}{Path(original[0]).suffix.lower()}"
        path = self.sources_dir / f"{candidate}.md"
        with open(path, "x", encoding="utf-8", newline="\n") as fh:  # "x": never overwrite
            fh.write(join_frontmatter(meta, body))
        return path.name

    def prepare_dropped_sources(self) -> list[str]:
        """For sources added outside PhosphoChat: add missing frontmatter to .md notes and
        create a sibling .md note for other files. Only metadata is added; bodies are untouched.
        Returns the notes that were created or given frontmatter."""
        changed = []
        notes = {p.stem for p in self.sources_dir.glob("*.md")}
        for p in sorted(self.sources_dir.iterdir()):
            if p.name.startswith(".") or not p.is_file():
                continue
            if p.suffix == ".md":
                text = p.read_text(encoding="utf-8", errors="replace")
                if not FRONTMATTER_RE.match(text):
                    meta = {"title": p.stem, "source_url": p.name, "content_type": "other",
                            "ingested_at": now_iso(),
                            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
                    _atomic_write(p, join_frontmatter(meta, text))
                    changed.append(p.name)
            elif p.stem not in notes:
                try:
                    body, ctype = extract_text(p.name, p.read_bytes())
                except VaultError:
                    continue
                meta = {"title": p.name, "source_url": p.name, "content_type": ctype,
                        "ingested_at": now_iso(), "original_file": p.name,
                        "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                note = p.with_suffix(".md")
                _atomic_write(note, join_frontmatter(meta, body))
                notes.add(p.stem)
                changed.append(note.name)
        return changed

    def unprocessed_sources(self) -> list[str]:
        """Source notes never mentioned in log.md."""
        log = self.read_log_text()
        return [p.name for p in sorted(self.sources_dir.glob("*.md")) if p.name not in log]

    # ---- index

    def read_index(self) -> str:
        p = self.root / "index.md"
        return p.read_text(encoding="utf-8") if p.is_file() else "# Index\n\n## Pages\n\n## Recaps\n"

    def index_entries(self) -> dict[str, str]:
        out = {}
        for line in self.read_index().splitlines():
            m = re.match(r"^\s*-\s*\[\[([^\]|#]+)[^\]]*\]\]\s*[—–-]?\s*(.*)$", line)
            if m:
                out[m.group(1).strip().removesuffix(".md")] = m.group(2).strip()
        return out

    def upsert_index(self, name: str, description: str,
                     section: Literal["Pages", "Recaps"] = "Pages") -> Literal["added", "updated"]:
        name = self.page_name(name)
        if not self.page_exists(name):
            raise VaultError(f"No page '{name}' yet; write_page it first.")
        desc = " ".join(description.split()) or self.page_meta(name).get("summary", "")
        entry = f"- [[{name}]] — {desc}"
        lines = self.read_index().splitlines()
        for i, line in enumerate(lines):
            if re.match(rf"^\s*-\s*\[\[{re.escape(name)}(\]\]|[|#])", line):
                lines[i] = entry
                _atomic_write(self.root / "index.md", "\n".join(lines) + "\n")
                return "updated"
        header = f"## {section}"
        if header not in lines:
            lines += ["", header, ""]
        start = lines.index(header) + 1
        end = next((j for j in range(start, len(lines)) if lines[j].startswith("## ")), len(lines))
        last_item = max((j for j in range(start, end) if lines[j].lstrip().startswith("- ")), default=None)
        insert_at = (last_item + 1) if last_item is not None else start
        if last_item is None and insert_at < len(lines) and lines[insert_at].strip() == "":
            insert_at += 1
        lines.insert(insert_at, entry)
        # keep a blank line before the next section
        if insert_at + 1 < len(lines) and lines[insert_at + 1].startswith("## "):
            lines.insert(insert_at + 1, "")
        _atomic_write(self.root / "index.md", "\n".join(lines).rstrip() + "\n")
        return "added"

    def remove_index_entry(self, name: str) -> bool:
        lines = self.read_index().splitlines()
        kept = [ln for ln in lines if not re.match(rf"^\s*-\s*\[\[{re.escape(name)}(\]\]|[|#])", ln)]
        if len(kept) == len(lines):
            return False
        _atomic_write(self.root / "index.md", "\n".join(kept).rstrip() + "\n")
        return True

    # ---- log

    def read_log_text(self) -> str:
        p = self.root / "log.md"
        return p.read_text(encoding="utf-8") if p.is_file() else f"{LOG_HEADING}\n"

    def log_entries(self) -> list[LogEntry]:
        """Entries newest first, as written."""
        out = []
        for block in re.split(r"(?m)^(?=## )", self.read_log_text()):
            m = re.match(r"## (\S+)\s*·\s*(\S+)[^\n]*\n?(.*)", block, re.S)
            if m:
                out.append(LogEntry(m.group(1), m.group(2), m.group(3).strip()))
        return out

    def prepend_log(self, kind: str, lines: list[str]) -> str:
        """Insert a new entry directly below the '# Log' heading (newest first)."""
        entry = f"## {now_iso()} · {kind}\n" + "".join(
            f"- {ln.strip().removeprefix('- ')}\n" for ln in lines if ln.strip())
        text = self.read_log_text()
        m = re.search(rf"(?m)^{re.escape(LOG_HEADING)}[ \t]*$", text)
        if m:
            head, tail = text[:m.end()], text[m.end():].lstrip("\n")
        else:
            head, tail = LOG_HEADING, text.lstrip("\n")
        new = f"{head}\n\n{entry}" + (f"\n{tail}" if tail else "")
        _atomic_write(self.root / "log.md", new.rstrip() + "\n")
        return entry

    # ---- lint (mechanical part)

    def lint_mechanical(self) -> dict:
        names = set(self.page_names())
        source_names = {p.name for p in self.sources_dir.iterdir() if p.is_file()}
        index = self.index_entries()
        incoming: dict[str, set[str]] = {n: set() for n in names}
        broken, schema, uncited = [], [], []
        for n in sorted(names):
            text = self.read_page(n)
            links, src_links = page_links(text)
            for link in sorted(links):
                if link in incoming and link != n:
                    incoming[link].add(n)
                elif link not in names:
                    broken.append({"page": n, "link": f"[[{link}]]"})
            for s in sorted(src_links):
                if s not in source_names:
                    broken.append({"page": n, "link": f"[[source: {s}]]"})
            if n in SYSTEM_PAGES:
                continue
            try:
                meta, body = split_frontmatter(text)
                missing = [k for k in (*REQUIRED_PAGE_KEYS, "last_updated") if k not in meta]
            except VaultError as e:
                missing, body = [str(e)], text
            if missing:
                schema.append({"page": n, "missing": missing})
            elif not body.strip():
                schema.append({"page": n, "missing": ["body"]})
            blocks = uncited_blocks(body, allow_page_links=n.endswith("-recap"))
            if blocks:
                uncited.append({"page": n, "count": len(blocks), "examples": blocks[:3]})
        orphans = sorted(n for n in names if not incoming[n] and n not in SYSTEM_PAGES
                         and not n.endswith("-recap"))
        return {
            "pages": len(names),
            "sources": len(source_names),
            "broken_links": broken,
            "orphans": orphans,
            "missing_from_index": sorted(n for n in names if n not in index and n not in SYSTEM_PAGES),
            "dead_index_entries": sorted(n for n in index if n not in names),
            "schema_violations": schema,
            "uncited_blocks": uncited,
        }

    # ---- git

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-c", "user.name=PhosphoChat", "-c", "user.email=phosphochat@localhost",
             "-c", "core.autocrlf=false", "-C", str(self.root), *args],
            capture_output=True, text=True, encoding="utf-8")

    def git_init(self) -> None:
        if not (self.root / ".git").exists():
            self._git("init", "-q")
        self.commit("init vault")

    def commit(self, message: str) -> str | None:
        """Commit everything; returns the short sha, or None when nothing changed."""
        if shutil.which("git") is None:
            return None
        self._git("add", "-A")
        if not self._git("status", "--porcelain").stdout.strip():
            return None
        r = self._git("commit", "-q", "-m", message)
        if r.returncode != 0:
            raise RuntimeError(f"git commit failed: {r.stderr.strip()}")
        return self._git("rev-parse", "--short", "HEAD").stdout.strip()

    def rollback(self) -> None:
        """Discard uncommitted changes to the wiki, index and log (a failed job's partial work).
        Jobs commit pending external changes first, so only the job's own edits are lost."""
        if shutil.which("git") is None:
            return
        self._git("checkout", "-q", "HEAD", "--", "wiki", "index.md", "log.md")
        self._git("clean", "-fdq", "--", "wiki")

    def history(self, limit: int = 20) -> list[dict]:
        r = self._git("log", f"-{limit}", "--pretty=format:%h%x09%aI%x09%s")
        out = []
        for line in r.stdout.splitlines():
            sha, when, msg = (line.split("\t", 2) + ["", ""])[:3]
            out.append({"sha": sha, "date": when, "message": msg})
        return out


# --------------------------------------------------------------------------- registry

LEGACY_AGENT_FILES = ("AGENTS.md", "CLAUDE.md", ".agents")


def is_vault_dir(p: Path) -> bool:
    return (p / "index.md").is_file() and (p / "wiki").is_dir()


class VaultRegistry:
    def __init__(self, base: Path):
        self.base = base
        self._vaults: dict[str, Vault] = {}

    def names(self) -> list[str]:
        if not self.base.is_dir():
            return []
        return sorted(p.name for p in self.base.iterdir() if p.is_dir() and is_vault_dir(p))

    def get(self, name: str) -> Vault:
        if name not in self._vaults:
            if not VAULT_NAME_RE.match(name) or not is_vault_dir(self.base / name):
                raise KeyError(name)
            self._vaults[name] = Vault(self.base / name)
        return self._vaults[name]

    def create(self, name: str) -> Vault:
        if not VAULT_NAME_RE.match(name):
            raise VaultError("Vault names use lowercase letters, digits, '-' and '_'.")
        root = self.base / name
        if root.exists():
            raise VaultError(f"Vault '{name}' already exists.")
        self.base.mkdir(parents=True, exist_ok=True)
        shutil.copytree(TEMPLATE_DIR, root)
        vault = Vault(root)
        vault.git_init()
        self._vaults[name] = vault
        return vault

    def migrate_all(self) -> list[str]:
        """Remove per-vault agent files left by older versions (they now live in app/prompts)."""
        changed = []
        for name in self.names():
            v = self.get(name)
            removed = []
            for f in LEGACY_AGENT_FILES:
                p = v.root / f
                if p.is_dir():
                    shutil.rmtree(p)
                    removed.append(f)
                elif p.is_file():
                    p.unlink()
                    removed.append(f)
            if removed:
                v.commit("move agent files to the app (" + ", ".join(removed) + ")")
                changed.append(name)
        return changed

    def ensure(self, name: str) -> Vault:
        try:
            return self.get(name)
        except KeyError:
            return self.create(name)
