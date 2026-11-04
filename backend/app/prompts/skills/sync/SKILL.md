---
name: sync
description: Reconcile the wiki with every source added since the last sync. The task lists the unprocessed files in sources/; update or create the wiki/ pages they affect. Use whenever the user dropped files into sources/ or asks to "update the wiki", "catch up" or "process new sources".
---

# Sync: reconcile wiki/ with new sources

The same editorial work as capture, but across a batch. PhosphoChat has already worked out which
sources are unprocessed (listed in the task) and added any missing source frontmatter. It will
fact-check your pages, write the `log.md` entry and make the git commit.

## Step 1: Plan before writing
1. `read_index`.
2. `read_source` each listed source and note only what it states: entities, concepts and claims.
3. Build a plan of which wiki pages each source touches. Group changes by **page**, not by source,
   so every page is written once with all its new information together.

## Step 2: Record kinase-substrate relations (biology sources only)
For every statement in the source that a kinase or phosphatase modifies a protein, call
`record_relation(enzyme, target, source, quote, mechanism, site, effect, page)`:
- `quote` is the sentence from the source that says it, copied exactly. The tool checks it
  appears in the source and rejects paraphrases; fix and resend if it complains.
- Give the `site` (e.g. Ser81) and `effect` only when the source states them.
- The reply tells you whether the databases already know the link (confirmed), don't (new), or
  disagree (conflicting). Mention new and conflicting ones on the wiki page, cited to the source,
  e.g. "reported by this source; not in any database yet".
- Don't record speculation ("may", "could") as a relation unless the source states it as a finding.

## Step 3: Write to wiki/
Apply the capture rules for each page (`read_page` first, then `write_page` with the whole page):
- **Grounding:** only claims the sources state, closely paraphrased. No background knowledge.
- Every paragraph, list item and table row ends with `[[source: filename.md]]` (or a SIGNOR
  citation for graph facts). Pages with uncited paragraphs are rejected.
- Frontmatter `title`, `summary`, `sources` (add the new filenames).
- Mark conflicts with a `> [!warning] Contradiction` callout citing both sides; don't silently overwrite.
- Create new pages only for significant entities/concepts; link each from a parent page and call
  `upsert_index` for it.
- Merge rather than duplicate: if two sources describe the same entity under different names, use
  one page and note the alias.

## Step 4: Finish
Return the report:
- `report`: sources processed, pages created/updated, contradictions found; recommend running lint.
- `key_takeaway`: one or two sentences on what changed in the big picture.
- `contradictions`: names of pages where you added a contradiction callout (empty if none).

## Rules
- Never edit sources.
