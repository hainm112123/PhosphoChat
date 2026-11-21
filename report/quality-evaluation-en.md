# PhosphoChat quality evaluation (pilot, November 2026)

## Summary

We tested PhosphoChat on 5 real research papers and 21 chat questions, then improved it in 11
measured steps. In the final run, all scored by an independent judge model (Claude Sonnet 4.6):

- **Wiki pages written from papers are faithful to the sources.** Only 2 of 412 checked
  statements (0.5%) went beyond what the paper says, down from 2.0% in the first run. Every
  paragraph carries a citation.
- **The wiki keeps most of what matters in each paper.** 86% of each paper's main findings are
  in the wiki (counting partial coverage as half), up from 66%.
- **Chat answers are correct and grounded.** On a 0–2 scale: correctness 1.95, groundedness
  2.0, citations 1.75, completeness 1.65. No answer cited an invalid database record, and the
  off-topic question was refused.
- **The main weakness is completeness.** Answers are right but sometimes leave out secondary
  facts. Questions worded differently from the wiki ("sample handling" for a page on "cold
  ischemia") depend on the model searching again.

## 1. What was tested

PhosphoChat is a research assistant for kinase–substrate biology. It has two parts:

- **Capture:** the user adds a paper, and the app writes wiki pages from it with citations.
  Kinase → substrate statements are recorded as claims and checked against the databases.
- **Chat:** questions are answered from the user's wiki and from a Neo4j graph that merges
  PhosphoSIGNOR (with PhosphoSitePlus, EPSD and iPTMNet) and KiNet. Each answer has up to three
  parts: what the wiki says, what the databases say, and how they fit together.

**Papers.** Five papers from the course dataset, chosen to cover different kinds of content:

| File | Topic |
|---|---|
| `1495.pdf` | Cold ischemia and phosphotyrosine signalling in surgical tumour specimens |
| `nature18003.pdf` | CPTAC breast cancer proteogenomics |
| `s41467-025-59779-y.pdf` | benchmarKIN: a benchmark of kinase activity inference methods |
| `mcdermott-2020-…ovarian-hgsc….pdf` | Proteogenomic characterisation of ovarian high-grade serous carcinoma (HGSC) |
| `1-s2.0-S2666979X25001077-main.pdf` | PROTsi, a protein-based stemness index across cancers |

**Questions.** 21 questions in six groups (the full list is in `backend/eval/questions.yaml`):

| Group | Count | What it checks | Example |
|---|---|---|---|
| Database | 6 | Lists and facts from the kinase databases | "Which kinases phosphorylate RB1?" |
| Paper | 8 | One paper's specific results | "What is PROTsi, and what did my sources use it for?" |
| Cross-paper | 2 | Synthesis across several papers | "How is kinase activity inferred from phosphoproteomics data, and what limits it?" |
| Claims | 2 | Recorded claims against the databases | "What do my sources and the databases each say about CDK1 phosphorylating RB1?" |
| Negative | 2 | Topics the wiki does not cover; the right answer says so | "What do my sources say about LRRK2 phosphorylation of Rab GTPases?" |
| Off-topic | 1 | Must be refused | "Plan a three-day sightseeing trip to Da Nang for me." |

**Models.** All models were reached through CLIProxyAPI, which serves Google Antigravity models
on a free but weekly-capped quota.

| Role | Model |
|---|---|
| Reading papers and writing pages | `gemini-3.8-flash-high` |
| Chat and topic check | `gemini-3-flash` |
| Final judge | `claude-sonnet-4-6`, from a different model family than the writer |
| Routine judge, during the improvement loop | `gemini-3.8-flash-high` |

## 2. How it was measured

**Code checks** cost nothing and ran on every step:

- **Capture:** the share of each paper actually read, uncited paragraphs, and frontmatter validity.
- **Chat:** whether every cited SIGNOR ID, PMID, wiki page and source exists; broken link formats;
  database precision (are the kinases named really in the databases?) and top-5 recall (are the
  best-supported ones named?); and whether the off-topic question was refused.

**A judge model** compared the output with the source.

- **Capture:** every statement on the written pages was checked against the full paper text, and
  labelled supported or unsupported (for example "overstated"). Five main findings were extracted
  from each paper and marked as in the wiki, partially in it, or missing.
- **Chat:** each answer was scored 0–2 for correctness, completeness, groundedness and citations,
  against key facts written for each question. The judge was given the same retrieved context the
  chat model saw.

**Judge choice.** Before the cheaper judge was used for routine comparisons, it was checked
against Claude: on chat items it scored within 1 point of Claude 94% of the time, and gave the
same score 73% of the time. It was used only to compare versions of the app. All final numbers
come from Claude.

**Noise.** The same answer can score 2 on one run and 1 on the next. A change on a single
question by a single point was treated as noise and re-run before drawing a conclusion.

## 3. Results

### 3.1 Capture

| Paper | Text read | Pages | Statements checked | Unsupported | Main findings (yes / partial / no) | Writer requests |
|---|---|---|---|---|---|---|
| 1495 (cold ischemia) | 72% | 3 | 59 | 0 | 5 / 0 / 0 | 5 |
| nature18003 (breast cancer) | 100% | 5 | 75 | 0 | 3 / 2 / 0 | 11 |
| s41467 (benchmarKIN) | 58% | 4 | 68 | 0 | 3 / 1 / 1 | 14 |
| McDermott (ovarian HGSC) | 46% | 5 | 48 | 0 | 4 / 1 / 0 | 16 |
| PROTsi | 45% | 6 | 162 | 2 | 4 / 1 / 0 | 8 |
| **Total** | | **23** | **412** | **2 (0.5%)** | **19 / 5 / 1 (86%)** | 10.8 per paper |

- **Text read below 100% is intended.** References, methods, acknowledgements, funding and
  supplementary sections are skipped, so the reader spends its effort on results.
- **Both unsupported statements were overstatements**, not inventions. One page said Haspin
  activity was elevated in several tumour types, where the paper says "particularly" in some. The
  other generalised a SNRPB finding from GBM.
- **Uncited paragraphs: 0** on all 23 pages.
- **Compared with the first run** (same papers, same judge): unsupported statements went from
  2.0% to 0.5%, main findings covered from 0.66 to 0.86, and pages per paper from 4.0 to 4.6.

### 3.2 Chat

Judge scores by question group (0–2):

| Group | Questions | Correctness | Completeness | Groundedness | Citations |
|---|---|---|---|---|---|
| Database | 6 | 2.0 | 1.83 | 2.0 | 2.0 |
| Paper | 8 | 2.0 | 1.62 | 2.0 | 2.0 |
| Cross-paper | 2 | 2.0 | 1.0 | 2.0 | 1.5 |
| Claims | 2 | 2.0 | 2.0 | 2.0 | 1.0 |
| Negative | 2 | 1.5 | 1.5 | 2.0 | 1.0 |
| **All 20 answered questions** | 20 | **1.95** | **1.65** | **2.0** | **1.75** |

Off-topic: refused correctly, without calling the chat model.

Code checks:

| Check | First run | Final |
|---|---|---|
| Answers citing an invalid record or page | 1 of 17 | **0 of 21** |
| Broken link formats per answer | not measured | **0** |
| Database precision (kinases named that are really in the databases) | 0.95 | 0.94 |
| Database top-5 recall (best-supported kinases named) | 0.32 | **0.52** |
| Input tokens per answer | about 7,300 | **about 3,600** |
| Time per answer | 0.5–5 s | 1.4–4.9 s |

**Where points were lost:**

- **Completeness 1, on 6 questions.** The answer is correct but leaves out secondary key facts.
  For example, P8 names the 62 phosphosites and both kinases but not the 58% pathway read-out.
- **Database lists.** The answer shows the 12 best-supported kinases out of up to 77, so some
  expected names (e.g. CDK5 for TP53) are absent. The answer gives the total and offers the full
  list.
- **N1 (LRRK2), correctness 1.** The wiki has nothing on it and the answer says so, but it leads
  with database facts. The judge expected the "nothing in your sources" message first.
- **Citations 0 on C1 and N2.** Both are correct "nothing found" answers. The judge took a point
  for missing citations, though its own note says none were needed. We count this as judge
  strictness, not an app error.

## 4. What the improvement loop changed

Each step changed one thing. Each was measured against the previous good run, and kept only if
the target metric improved and nothing else got worse beyond noise. All 11 were kept.

| # | Change | Effect measured |
|---|---|---|
| 1 | Chat context: direct links first; database lists 6 → 12 items with totals | Correctness 1.80 → 1.95; top-5 recall 0.32 → 0.48 |
| 2 | Code check of every citation after each answer, with a note for unknown ones | 3 of 3 bad citations flagged, no false flags |
| 3 | Citation format rule, and automatic repair of common link slips | Answers with broken links 3 → 1 |
| 4 | Reader asks for every distinct result, keeping numbers and names | Key facts kept from nature18003: 4–6 of 8 → 8 of 8 |
| 5 | Notes handed to the writer: 7,000 → 14,000 characters | Unsupported 2.1% → 0; main findings covered 0.85 → 1.0 (2 papers) |
| 6 | Wiki excerpts ranked by the question's words, not the first lines of a page | Key facts in the context 3 → 6 of 9 |
| 7 | Pages ranked by coverage of the question; wiki capped at half the context | Database section present 9 of 9; input tokens −33% |
| 8 | "Search the wiki with other words before saying it has nothing" | Paraphrased question X2: 0.33 → 1.0, negatives unchanged |
| 9 | Topic check accepts terms it does not recognise (e.g. PROTsi) | 11 of 11 test questions right, twice |
| 10 | Example citation in the chat prompt replaced by placeholders (the model had copied it) | Copied ID 2 → 0 answers |
| 11 | Writer history: page text no longer lost when older messages are shortened | Writer runs hitting the 40-request limit: 3 of 4 → 0 of 4 |

**Prompt cost.** A redesign before the loop cut the fixed prompt sent with every request (counted
in Groq tokens):

| Agent | Before | After |
|---|---|---|
| Capture | 5,026 | 1,101 |
| Lint | 4,688 | 1,227 |
| Digest | 4,597 | 1,092 |
| Chat | 4,061 | 869 |

## 5. Robustness problems found and fixed

Testing on real papers found failures that unit tests had not:

- **Writer loop (step 11).** When older messages were shortened to fit the history limit, the cut
  became permanent: PydanticAI stores the shortened history back as the run's history. A page cut
  once could never be seen whole again, so the writer re-read the same pages up to 15 times until
  it hit its request limit. Two of the five papers needed a second attempt in the final run
  because of this; it was fixed afterwards and re-tested.
- **Parallel index updates** shared one temporary file and could lose index lines. They are now
  locked, with unique temporary files.
- **Gemini's malformed tool calls** are now retried instead of failing the job. A page can be
  written at most 3 times per run, which stops rewrite loops.
- **Invalid YAML in page frontmatter**, caused by a citation placed after the summary, is now
  repaired.

## 6. Limitations

- **Small pilot.** 5 papers and 21 questions are enough to find problems and compare versions,
  not to give precise rates. One question is about 5% of the chat score.
- **Paraphrased questions** depend on the chat model choosing to search again. Wiki search
  matches words, not meaning; semantic search would fix this.
- **Citation check.** It verifies that a cited PMID exists, not that it belongs to the link it
  is cited for. One answer attached PMIDs from neighbouring links.
- **Lint, digest and sync** were not part of the final run. In earlier runs, lint and digest
  passed every code check, and sync passed after the tool-call fix.
- **Judge noise.** Single-point differences on one question are within noise.
- **Model dependence.** These results are for the models above. Smaller models did worse: as a
  reader, `gemini-3-flash` kept 8 findings where `gemini-3.8-flash-high` kept 21, and it is unfit
  for writing pages.

## 7. Reproducing

The harness is in `backend/eval/`. A full log of every run and decision is in
`backend/eval/PROGRESS.md`, and the raw results are in `backend/eval/results/`.

```bash
cd backend
.venv/Scripts/python -m eval.run_capture --job-model compat:cliproxy:gemini-3.8-flash-high --vault eval-final --papers <files>
.venv/Scripts/python -m eval.score_capture --vault eval-final --judge-model compat:cliproxy:claude-sonnet-4-6
.venv/Scripts/python -m eval.run_chat --chat-model compat:cliproxy:gemini-3-flash --vault eval-final --tag final2
.venv/Scripts/python -m eval.score_chat --file chat-eval-final-cliproxy-gemini-3-flash-final2.json --judge-model compat:cliproxy:claude-sonnet-4-6
.venv/Scripts/python -m eval.compare --base <score file> --new <score file>
```

`--tier code` on either scorer runs only the free code checks. The backend has 210 offline
tests (`pytest -q`).
