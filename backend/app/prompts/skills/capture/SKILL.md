---
name: capture
description: Write wiki pages from the reading notes of one or more new sources (capture and sync).
---

# Skill: write wiki pages from reading notes
PhosphoChat has read the source(s) and gives you notes below the task: findings with exact
quotes and where they come from, or the full text of a short source. The kinase-substrate
relations are already recorded. Saving, the log and the commit are done for you.

1. The task shows the index. Pick the pages first: one for each significant protein, drug,
   disease or concept in the notes (2 to 4 for a paper; no pages for passing mentions).
2. Then go page by page: `read_page` an existing one and write it, whole, with
   `write_page(name, content)` before reading the next (older page text is cut from your
   history). Write each page once. New pages need no reading; write several in one turn, with
   their `upsert_index`.
3. Write only what the notes state, cited `[[source: <filename>]]`. Name the relations listed
   under "Relations" where they belong; say "not in any database yet" for those marked new.
4. Link new pages from a related page; `upsert_index(name, "<one line>")` for each new page.
5. Return the report: 3 to 5 key takeaways and the pages created and updated; `key_takeaway`
   (one sentence); `contradictions` (pages where you added a callout).

If the notes have nothing in scope, write no pages and say so in the report.
