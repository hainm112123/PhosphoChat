---
name: capture
description: Write wiki pages from the reading notes of one or more new sources (capture and sync).
---

# Skill: write wiki pages from reading notes
PhosphoChat has read the source(s) and gives you notes below the task: findings with exact
quotes and where they come from, or the full text of a short source. The kinase-substrate
relations are already recorded. Saving, the log and the commit are done for you.

1. The task shows the index; `read_page` only pages you will change.
2. Update or create a page for each significant protein, drug, disease or concept in the notes
   (2 to 4 pages for a paper; no pages for passing mentions). Write each page once, whole, with
   `write_page(name, content)`. Work in few steps: call `write_page` for several pages in the
   same turn, and `upsert_index` with them.
3. Write only what the notes state, cited `[[source: <filename>]]`. Name the relations listed
   under "Relations" where they belong; say "not in any database yet" for those marked new.
4. Link new pages from a related page; `upsert_index(name, "<one line>")` for each new page.
5. Return the report: 3 to 5 key takeaways and the pages created and updated; `key_takeaway`
   (one sentence); `contradictions` (pages where you added a callout).

If the notes have nothing in scope, write no pages and say so in the report.
