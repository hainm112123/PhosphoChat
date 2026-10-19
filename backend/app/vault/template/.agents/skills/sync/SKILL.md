---
name: sync
description: Reconcile the wiki with every source added since the last sync. The task lists the unprocessed files in sources/; update or create the wiki/ pages they affect. Use whenever the user dropped files into sources/ or asks to "update the wiki", "catch up" or "process new sources".
---

# Sync: reconcile wiki/ with new sources

The same editorial work as capture, but across a batch. PhosphoChat has already worked out which
sources are unprocessed (listed in the task), added any missing source frontmatter, and will write
the `log.md` entry and the git commit.

## Step 1: Plan before writing
1. `read_index`.
2. `read_source` each listed source and note its entities, concepts and claims.
3. Build a plan of which wiki pages each source touches. Group changes by **page**, not by source,
   so every page is written once with all its new information together.

## Step 2: Write to wiki/
Apply the capture rules for each page (`read_page` first, then `write_page` with the whole page):
- Frontmatter `title`, `summary`, `sources` (add the new filenames).
- Cite inline as `[[source: filename.md]]`.
- Mark conflicts with a `> [!warning] Contradiction` callout citing both sides; don't silently overwrite.
- Create new pages only for significant entities/concepts; link each from a parent page and call
  `upsert_index` for it.
- Merge rather than duplicate: if two sources describe the same entity under different names, use
  one page and note the alias.

## Step 3: Finish
Return the report:
- `report`: sources processed, pages created/updated, contradictions found; recommend running lint.
- `log_lines`: `["Contradictions flagged: <count and pages, or none>", "Key takeaway: <one or two sentences>"]`.

## Rules
- Never edit sources.
