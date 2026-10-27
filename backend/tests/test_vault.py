from __future__ import annotations

import pytest

from app.vault.store import DuplicateSource, VaultError, VaultRegistry, split_frontmatter, uncited_blocks

PAGE = """---
title: Protein kinase C
summary: A family of Ser/Thr kinases.
sources:
  - {src}
---
# Protein kinase C

**Summary:** PKC phosphorylates [[marcks]] [[source: {src}]].
"""


def _source(vault, title="MARCKS and TLR4", body="Body text.", url="https://example.org/a"):
    return vault.save_source(title=title, body=body, source_url=url, content_type="article")


def test_new_vault_is_empty_and_committed(vault):
    assert vault.page_names() == []
    assert vault.list_sources() == []
    assert vault.read_log_text().strip() == "# Log"
    for agent_file in ("AGENTS.md", "CLAUDE.md", ".agents"):  # content only
        assert not (vault.root / agent_file).exists()
    assert [h["message"] for h in vault.history()] == ["init vault"]


def test_migrate_removes_legacy_agent_files(tmp_path):
    reg = VaultRegistry(tmp_path)
    v = reg.create("old")
    (v.root / "AGENTS.md").write_text("schema", encoding="utf-8")
    (v.root / "CLAUDE.md").write_text("@AGENTS.md", encoding="utf-8")
    (v.root / ".agents/skills/capture").mkdir(parents=True)
    (v.root / ".agents/skills/capture/SKILL.md").write_text("x", encoding="utf-8")
    v.commit("legacy layout")
    assert reg.migrate_all() == ["old"]
    assert not (v.root / "AGENTS.md").exists() and not (v.root / ".agents").exists()
    assert v.history()[0]["message"].startswith("move agent files to the app")
    assert reg.migrate_all() == []  # idempotent


def test_prompts_are_app_wide():
    from app import prompts
    assert set(prompts.skill_names()) == {"capture", "digest", "lint", "query", "sync"}
    assert "Grounding" in prompts.schema()
    with pytest.raises(KeyError):
        prompts.skill("../AGENTS")


def test_uncited_blocks():
    body = """**Summary:** a summary needs no citation.

A cited paragraph [[source: a.md]].

An uncited paragraph.

Phosphorylation can:
- change activity [[source: a.md]]
- change stability
  over a wrapped line
- See also [[protein-kinase]]

| enzyme | site |
|---|---|
| PRKCA | Ser174 (SIGNOR-277418) |
| CDK2 | Ser315 |

> [!warning] Contradiction
> Source A says X [[source: a.md]].

```
code is ignored
```
"""
    assert uncited_blocks(body) == ["An uncited paragraph.", "change stability over a wrapped line",
                                    "| CDK2 | Ser315 |"]
    assert uncited_blocks("Week summary links [[marcks]].", allow_page_links=True) == []


def test_page_name_rules(vault):
    for bad in ("../x", "Protein Kinase", "a/b", "", "x.md/../y"):
        with pytest.raises(VaultError):
            vault.page_path(bad)
    assert vault.page_name("wiki/protein-kinase-c.md") == "protein-kinase-c"


def test_source_paths_cannot_escape(vault):
    for bad in ("../AGENTS.md", "a/b.md", ".hidden", ""):
        with pytest.raises(VaultError):
            vault.source_path(bad)


def test_save_source_frontmatter_and_immutability(vault):
    name = _source(vault)
    assert name.endswith("-marcks-and-tlr4.md")
    meta, body = split_frontmatter((vault.sources_dir / name).read_text(encoding="utf-8"))
    assert {"title", "source_url", "content_type", "ingested_at"} <= meta.keys()
    assert body.strip() == "Body text."
    with pytest.raises(DuplicateSource):
        _source(vault, title="Other", body="Different")  # same URL
    with pytest.raises(DuplicateSource):
        _source(vault, title="Other", url="pasted")      # same content
    second = _source(vault, body="Another body", url="https://example.org/b")
    assert second != name and second.endswith("-2.md")


def test_write_page_validates_and_stamps(vault):
    src = _source(vault)
    with pytest.raises(VaultError, match="frontmatter"):
        vault.write_page("pkc", "# PKC\nno frontmatter")
    with pytest.raises(VaultError, match="Unknown sources"):
        vault.write_page("pkc", PAGE.format(src="nope.md"))
    with pytest.raises(VaultError, match="no citation"):
        vault.write_page("pkc", PAGE.format(src=src) + "\nKinases use ATP.\n")
    assert vault.write_page("protein-kinase-c", PAGE.format(src=src)) == "created"
    assert vault.write_page("protein-kinase-c", PAGE.format(src=src)) == "updated"
    meta = vault.page_meta("protein-kinase-c")
    assert meta["last_updated"] and meta["sources"] == [src]


def test_prepend_log_goes_below_heading(vault):
    vault.prepend_log("capture", ["Source: sources/a.md"])
    vault.prepend_log("lint", ["Issues found: 0"])
    text = vault.read_log_text()
    assert text.startswith("# Log\n\n## ")
    kinds = [e.kind for e in vault.log_entries()]
    assert kinds == ["lint", "capture"]


def test_upsert_index_sections(vault):
    src = _source(vault)
    vault.write_page("protein-kinase-c", PAGE.format(src=src))
    vault.write_page("2026-w42-recap", PAGE.format(src=src))
    assert vault.upsert_index("protein-kinase-c", "PKC family") == "added"
    assert vault.upsert_index("protein-kinase-c", "PKC kinase family") == "updated"
    vault.upsert_index("2026-w42-recap", "Week 42", section="Recaps")
    text = vault.read_index()
    assert text.index("[[protein-kinase-c]] — PKC kinase family") < text.index("## Recaps") \
        < text.index("[[2026-w42-recap]]")
    assert vault.index_entries() == {"protein-kinase-c": "PKC kinase family",
                                     "2026-w42-recap": "Week 42"}
    with pytest.raises(VaultError):
        vault.upsert_index("missing-page", "x")


def test_lint_mechanical(vault):
    src = _source(vault)
    vault.write_page("protein-kinase-c", PAGE.format(src=src))  # links to missing [[marcks]]
    (vault.wiki_dir / "bad.md").write_text("# no frontmatter [[source: ghost.md]]\n\nUncited claim.\n",
                                           encoding="utf-8")
    f = vault.lint_mechanical()
    assert f["uncited_blocks"] == [{"page": "bad", "count": 1, "examples": ["Uncited claim."]}]
    assert {"page": "protein-kinase-c", "link": "[[marcks]]"} in f["broken_links"]
    assert {"page": "bad", "link": "[[source: ghost.md]]"} in f["broken_links"]
    assert set(f["orphans"]) == {"protein-kinase-c", "bad"}
    assert set(f["missing_from_index"]) == {"protein-kinase-c", "bad"}
    assert f["schema_violations"][0]["page"] == "bad"


def test_unprocessed_and_dropped_sources(vault):
    name = _source(vault)
    (vault.sources_dir / "dropped.md").write_text("Raw notes", encoding="utf-8")
    (vault.sources_dir / "table.tsv").write_text("a\tb\n1\t2\n", encoding="utf-8")
    prepared = vault.prepare_dropped_sources()
    assert set(prepared) == {"dropped.md", "table.md"}
    meta, body = split_frontmatter((vault.sources_dir / "dropped.md").read_text(encoding="utf-8"))
    assert meta["title"] == "dropped" and body.strip() == "Raw notes"
    assert set(vault.unprocessed_sources()) == {name, "dropped.md", "table.md"}
    vault.prepend_log("capture", [f"Source: sources/{name}"])
    assert name not in vault.unprocessed_sources()


def test_commit_and_rollback(vault):
    src = _source(vault)
    assert vault.commit("source") is not None
    assert vault.commit("nothing") is None
    base = vault.head()
    new_src = _source(vault, title="Later", body="Other body", url="https://example.org/later")
    (vault.sources_dir / "raw.tsv").write_text("a\tb\n", encoding="utf-8")
    vault.write_page("protein-kinase-c", PAGE.format(src=src))
    vault.upsert_index("protein-kinase-c", "PKC")
    vault.prepend_log("capture", ["x"])
    vault.rollback_to(base)
    assert vault.page_names() == []
    assert vault.read_log_text().strip() == "# Log"
    assert vault.index_entries() == {}
    assert (vault.sources_dir / src).is_file()          # committed before base: kept
    assert not (vault.sources_dir / new_src).exists()   # created after base: removed
    assert not (vault.sources_dir / "raw.tsv").exists()
    assert vault.head() == base


def test_search_and_backlinks(vault):
    src = _source(vault)
    vault.write_page("protein-kinase-c", PAGE.format(src=src))
    vault.write_page("marcks", PAGE.format(src=src).replace("[[marcks]]", "[[protein-kinase-c]]"))
    hits = vault.search("phosphorylates")
    assert {h["name"] for h in hits} == {"protein-kinase-c", "marcks"}
    assert vault.backlinks("marcks") == ["protein-kinase-c"]
