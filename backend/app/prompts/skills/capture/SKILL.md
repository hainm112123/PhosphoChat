---
name: capture
description: Ingest one new source into the vault. The source is already saved in sources/; read it, then update every wiki/ page it affects. Use whenever the user wants to "add to the vault", "save this", "clip this" or "ingest this".
---

# Capture: ingest one source

Process exactly one source per run. The source is immutable; all interpretation goes into wiki/.
PhosphoChat has already saved the source (its filename is in the task). It will fact-check your
pages against the source, write the `log.md` entry and make the git commit. Do not try to save
sources or edit log.md.

## Step 1: Read and extract
1. `read_source(<filename>)`. If the result says more lines remain, keep reading with `offset`
   until you have the whole text (for large data files, the first page plus the profile is enough).
2. List for yourself only what the source actually states: its claims, the entities (proteins,
   genes, pathways, people, datasets), the concepts, and any numbers or dates. Nothing else.

## Step 2: Find affected wiki pages
1. `read_index` to see every existing page. Use `search_pages` for names that might have a page
   under a different title.
2. Mark pages where this source adds new information, contradicts an existing claim, or fills a gap.
   `read_page` each one before changing it.
3. Decide which new pages are needed: one per significant entity or concept that has no page yet.
   Don't create pages for passing mentions. For a short source, 1 to 3 pages is typical.

## Step 3: Check the graph (biology sources only)
For each key protein/kinase in the source, you may call `regulators_of`, `targets_of` or `sites_of`
to add graph facts. Cite them as `(SIGNOR-xxxxxx; PMID nnnn)`. Skip this for sources that are not
about specific proteins in phosphorylation signalling.

## Step 4: Write to wiki/
For every affected or new page call `write_page(name, content)` with the **whole** page:
```markdown
---
title: <Title>
summary: <one sentence>
sources:
  - <every source filename this page cites>
---
# <Title>

**Summary:** <1 to 3 sentences>

## <Section>
<a claim the source states, in close paraphrase> [[source: YYYY-MM-DD-slug.md]]

- <another stated claim> [[source: YYYY-MM-DD-slug.md]]
```
- **Grounding:** write only what the source states (see the Grounding rules). No background
  knowledge, no added detail, no examples or mechanisms the source doesn't give. A short page
  that is fully supported is better than a long one that isn't.
- **Every paragraph, list item and table row ends with a citation.** Pages with uncited
  paragraphs are rejected; fix and resend them.
- When updating, keep all existing content and sources; add the new information in the right section.
- If the source contradicts an existing claim, keep both, cite both, and add
  `> [!warning] Contradiction` with one line explaining the conflict.
- Link related pages with `[[page-name]]`. Every new page must be linked from at least one other
  page (update a parent page to link it). Don't copy the same text onto several pages; link instead.
- `last_updated` is set automatically.
- For every new page, call `upsert_index(name, "<one-line description>")`.

## Step 5: Finish
Return the report:
- `report`: for the user, 3 to 5 key takeaways from the source, the pages created and updated,
  and any contradictions.
- `key_takeaway`: one sentence, the single most important thing the source says.
- `contradictions`: names of pages where you added a contradiction callout (empty if none).

## Rules
- Never delete wiki content to make room; merge it.
- Only link to pages that exist or that you create in this run.
- If the source is low quality or off-topic, write no pages and say so in `report`.
