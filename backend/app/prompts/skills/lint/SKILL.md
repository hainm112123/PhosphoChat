---
name: lint
description: Health-check the wiki for broken links, orphan pages, stale pages, contradictions, duplicates and thin coverage, then write a report and fix only the safe mechanical issues. Use for a "health check", "audit" or "cleanup" of the vault, or after a large capture or sync.
---

# Lint: wiki health check

Report first, then fix only what's mechanical. Anything that changes meaning goes to the user.

PhosphoChat has already run the mechanical checks and put the results in the task:
broken links, orphan pages, index drift (missing or dead entries), schema violations
(missing frontmatter) and uncited blocks (paragraphs, list items or table rows without a
`[[source: ...]]` or SIGNOR citation). You do the checks that need reading.

## Checks you run
1. **Stale pages**: a source newer than a page's `last_updated` mentions the page's topic but isn't
   in its `sources`. Compare `list_sources` dates with `list_pages`.
2. **Contradictions**: compare claims across pages about the same entity or concept (numbers,
   dates, definitions, conclusions). List each pair with both citations. Also list unresolved
   `> [!warning] Contradiction` callouts (`search_pages("[!warning]")`).
3. **Duplicates**: pages covering the same thing under different names.
4. **Coverage gaps**: concepts linked often but with a thin page or none, and topics backed by only
   one source. These are research suggestions.

## Safe auto-fixes (do these)
- `upsert_index` for pages missing from the index (use the page summary as the description).
- Fill in missing frontmatter fields where the value is obvious from the page (`write_page`).
- Fix broken links when the intended target is unambiguous (a typo or renamed page).

## Needs user approval (don't do these)
Merging or deleting pages, resolving contradictions, rewriting content or removing claims
(including uncited ones: list them for the user instead).

## Output
1. `write_page("lint-report", ...)` with frontmatter (`title: Lint report`, a `summary` with the
   counts, `sources: []`), grouped by check, counts at the top and `[[links]]` to affected pages.
   Overwrite the previous report.
2. Return the report:
   - `report`: the counts, what you fixed, and the top 3 decisions for the user with a proposed action each.
   - `log_lines`: `["Issues found: <total> (broken links X, orphans X, stale X, contradictions X, duplicates X, schema X, uncited X)", "Auto-fixed: <list or none>", "Needs decision: <list or none>"]`.

## Rules
- Never modify sources.
- If the wiki is small (under ~10 pages), say so and keep the report brief.
