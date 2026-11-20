---
name: lint
description: Health-check the wiki, write the lint report and fix only safe mechanical issues.
---

# Skill: lint
The task has the mechanical checks already run (broken links, orphans, index drift, missing
frontmatter, uncited blocks, claims new to or conflicting with the databases). You add:
1. Stale pages: a newer source (`list_sources`) on a page's topic missing from its `sources`
   (`list_pages`).
2. Contradictions across pages, with both citations, and open `[!warning]` callouts
   (`search_pages("[!warning]")`).
3. Duplicates: pages on the same thing under different names.
4. Coverage gaps: often-linked topics with a thin page or none; topics with one source.

Fix only: missing index lines (`upsert_index`), obvious missing frontmatter, links whose
intended target is unambiguous. Everything else (merging, deleting, resolving contradictions,
rewriting, claims) goes under "Needs decision".

Write `lint-report` (frontmatter `title: Lint report`, a `summary` with the counts,
`sources: []`): counts first, then one section per check with `[[links]]`. Small wiki (under
10 pages): keep it brief. Return `report` (counts, fixes, top 3 decisions with a proposed
action) and `log_lines`: ["Issues found: <total> (broken links X, orphans X, stale X,
contradictions X, duplicates X, schema X, uncited X)", "Auto-fixed: <list or none>",
"Needs decision: <list or none>"].
