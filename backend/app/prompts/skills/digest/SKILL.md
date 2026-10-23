---
name: digest
description: Write the weekly recap for the vault. Reads this week's log.md entries and the pages they touched, then writes a synthesis page in wiki/ that surfaces themes, new pages, open questions and patterns. Use for a "weekly recap", "weekly review" or "what did I learn this week".
---

# Digest: weekly synthesis

The goal is synthesis, not a changelog. Connect what came in this week to what the wiki already knows.
PhosphoChat puts the period, the recap page name, the log entries for the period and the previous
recap name (if any) in the task.

## Step 1: Gather
1. From the log entries in the task, collect the sources ingested and the pages created or updated.
2. `read_page` those pages (at least their summaries). Read the full page for anything that appears
   in more than one log entry.
3. `read_page` the previous recap, if any.

## Step 2: Write the recap
`write_page("<recap name from the task>", ...)`:
```markdown
---
title: Week WW recap (YYYY-MM-DD to YYYY-MM-DD)
summary: <one or two sentences capturing the week>
sources:
  - <every source ingested this week>
---
# Week WW recap

<2 to 3 sentence summary of the week's direction>

## What came in
<sources, grouped by theme, one line each with [[source: ...]] citations>

## What changed in the wiki
<new and significantly updated pages as [[links]], one line each on why it matters>

## Themes and patterns
<3 to 5 insights connecting multiple sources or pages. Cite every claim.>

## Contradictions and tensions
<open contradictions from this week and whether they were resolved>

## Open questions
<gaps the week exposed, and what to read or capture next>

## Compared with last week
<how this week builds on or departs from [[previous-recap]]; omit if there is none>
```

## Step 3: Link it in
- `upsert_index("<recap name>", "<one-line description>", section="Recaps")`.
- If there is a previous recap, add a forward link to this one at its end (`read_page`, then
  `write_page` with the whole page, changing nothing else).

## Step 4: Finish
Return the report:
- `report`: the 2 to 3 sentence summary plus the top open question.
- `log_lines`: `["Period: <start> to <end>", "Recap: [[<recap name>]]", "Top theme: <one sentence>"]`.

## Rules
- Every insight must trace back to a cited source or wiki page; don't invent patterns.
- Don't edit the pages you're summarizing (except the forward link on the previous recap).
