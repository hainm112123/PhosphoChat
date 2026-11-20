---
name: digest
description: Write the weekly recap page from the period's log entries.
---

# Skill: digest
Synthesis, not a changelog. The task gives the period, the recap page name, the previous recap
and the log entries.
1. `read_page` the pages the entries created or updated (at least their summaries), and the
   previous recap.
2. `write_page(<recap name>)`: frontmatter (`title: Week WW recap (start to end)`, `summary`,
   `sources`: the week's sources); a 2 to 3 sentence overview; then `## What came in`,
   `## What changed in the wiki`, `## Themes and patterns` (3 to 5 insights across sources),
   `## Contradictions and tensions`, `## Open questions`, `## Compared with last week` (omit
   without a previous recap). Cite every claim to a source or link the page it comes from.
3. `upsert_index(<recap name>, "<one line>", section="Recaps")`; add a link to the new recap at
   the end of the previous one (whole page, nothing else changed).
4. Return `report` (the overview and the top open question) and `log_lines`:
   ["Period: <start> to <end>", "Recap: [[<recap name>]]", "Top theme: <one sentence>"].
