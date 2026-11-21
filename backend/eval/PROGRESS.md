# Pilot quality evaluation: progress note (updated 2026-11-21, test-evaluate-redesign loop)

Scope chosen by the user:
- a pilot of 5 papers from `C:\yuuhi\int3132\dataset\pdf`;
- a 21-question chat test;
- lint, digest and sync jobs;
- an EN+VI quality report;
- the reusable harness in `backend/eval/`;
- a cheap loop: one change at a time, compared with a baseline, strong models only when needed.

**Status (2026-11-21).**
- The baseline is complete: 5 papers, 21 questions, lint/digest/sync.
- 11 iterations are done, all kept (the loop table below).
- 210 tests pass.
- The final run is complete for capture (5 papers) and chat (21 questions), Claude-judged; jobs skipped on request. See "Final run" below.
- A third Google account was added to the proxy on 2026-11-21; the first two are in weekly cooldown for `gemini-3.8-flash-high` until about 2026-11-27/28.
- The EN+VI quality report is in `report/`.

## The loop (2026-11-20)

**Tools** (all in `backend/eval/`):
- **`--tier code`** on `score_capture`/`score_chat`: free mechanical checks.
- **`--judge-model`:** score file names include the judge.
- **`READING_CACHE_DIR=results/readings`:** readings kept on disk. The key includes the model, the part settings and a hash of the reader prompt.
- **`results/runs.jsonl`:** the run ledger, with a design hash of the prompts and agent code.
- **Comparison scripts:** `compare.py` (before/after per metric, with regressions per item), `agreement.py` (cheap vs strong judge), `run_jobs.py` (lint, digest, sync on a copy of the vault), `reader_variants.py` (reader prompts scored by findings and key facts, no judge).

**Judges.**
- **`gpt-oss-120b-medium` can't judge here:** through the proxy it returns reasoning only on long prompts (and on short ones at `max_tokens` 16000), and the proxy refuses JSON mode for it.
- **`claude-sonnet-4-6`** scored all 5 papers and 7 of 21 answers before its quota ran out.
- **`gemini-3.8-flash-high`** is the routine judge:
  - against Claude it is within 1 point on 94% of chat items (73% identical);
  - findings labels agree on 18 of 25;
  - unsupported counts are similar.

  It is used only to compare runs, because it judges its own family.
- **Noise:** the same prompt can get completeness 2 one time and 1 the next, so a single-item, single-point change is noise.

**Baseline** (writer `gemini-3.8-flash-high`, chat `gemini-3-flash`):
- **Capture (Claude judge):** all 5 papers succeeded on the first attempt, with 6–14 writer requests each. 3 of 179 statements were unsupported; 0 uncited blocks.
- **Main findings covered:** 1495 3 yes / 2 partial; nature18003 3 / 1 / 1; s41467 3 of 5 labels agreeing between judges.
- **Chat (Gemini judge, 21 questions):**
  - correctness 1.80, completeness 1.30, groundedness 1.95, citations 1.95;
  - database precision 0.95, top-5 recall 0.32;
  - refusal correct.
- **Jobs (`gemini-3-flash`):**
  - lint and digest pass every code check;
  - **sync failed** on Gemini's `MALFORMED_FUNCTION_CALL`, which was not treated as a retryable slip. Fixed: `runtime.BAD_TOOL_CALL_MARKERS`, with a test. Not re-run yet.
- **Reader models:** `gemini-3-flash` keeps far fewer findings than `gemini-3.8-flash-high` (8 vs 21 on 1495), so capture iterations should use the stronger reader.

**Iterations**

| # | Change | Measured on | Before → after | Kept |
|---|---|---|---|---|
| 1 | Retrieved context: direct links and paths first (they were cut first when over budget); lists 6 → 12 items, compact, "top N of M" plus a hint to use `interactions` (`knowledge/context.py`) | 21 questions, Gemini judge | correctness 1.80 → 1.95; DB items named 3–8 → 7–12; top-5 recall 0.32 → 0.48; C2 (CDK1 → RB1) 0 → 2; input tokens per answer 7.3k → 5.8k. P7/X2 dips were noise (X2 back on rerun) | yes |
| 2 | Citation check after each chat answer: SIGNOR ids, PMIDs, pages and sources verified by code, and a note is appended for unknown ones (`knowledge/citations.py`, `api/chat.py`; the eval uses the same check) | 21 questions, code tier | 3 of 3 answers with an unverifiable citation got the note, and no false notes. It caught a real slip: `SIGNOR-81115` for `SIGNOR-81114` | yes |
| 3 | Chat prompt: "cite a page as [[page-name]] and a source note as [[source: file.md]]; never put one inside the other". Plus repair of common link slips in the UI (`Markdown.tsx::repairLinks`) and in the check (`citations.repair_links`), same rules, counted as `format_slips` | 21 questions, code tier | answers with broken links 3 → 1; the nested `[[source: [[page]]]]` form is gone (0); the remaining `[[[[page]]]]` is now repaired | yes |
| 4 | Reader prompt: "every distinct result, up to 10 … keep numbers and names" instead of "up to 5 of the most important" | `reader_variants.py`, `gemini-3-flash`, 2 runs | nature18003 key facts 4 / 6 of 8 → 8 / 8 in both runs; findings 16–17 → 25–29; quote rejections about the same. 1495 unchanged (4 of 7) | yes |

| 5 | `CAPTURE_NOTES_CHARS` 7000 → 14000. The new reader finds 32–76 findings, but at 7000 only about 15 lines reached the writer, which dropped P8's fact. Measured offline on the cached notes: 14000 keeps every key fact found (6/6, 7/7). Section-balanced selection was tried and was no better | Re-capture of 1495 + nature18003 (new vault `eval-it5`), Gemini judge | unsupported 2.1% → 0; findings covered 0.85 → 1.0; writer requests 12.5 → 10; P8 1 → 2 (62 phosphosites, RPS6KA5, EIF2AK4 now in the wiki and the answer) | yes |
| 6 | Wiki excerpts in the chat context: lines ranked by the question's words (12 keywords) and proteins, cut around the match, 400 chars, instead of the first 3 lines | `context_check` + 6 paper questions on `eval-it5` + all 21 | key facts in the context 3 → 6 /9 (`eval-it5`), 4 → 6 /9 (5-paper vault); 6 paper questions: correctness 1.33 → 2.0, nothing worse, tokens −61% | yes, but it cut the database section (fixed in 7) |
| 7 | Pages ranked by how much of the question they cover; the wiki capped at half the budget when the question names proteins (the database section was crowded out: D2/D3/D6/N1 had none); link repair generalised (`[[synth: page]]`, `[[page, source: f]]`); new `eval/context_check.py` | `context_check`, 21 + 6 questions, reruns of the dips | database section present 9/9; DB precision 0.97 → 1.0, top-5 recall 0.48 → 0.52, input tokens per answer 5.8k → 3.9k; `eval-it5` 6 questions all better, nothing worse. P2's 0 was noise (2/2 in 2 reruns) | yes |
| 8 | Chat prompt: "Before saying the wiki has nothing on a topic, search it (`search_pages`) with other words for it" (X2: the wording "sample handling" vs the page "cold ischemia") | X2, N1, N2, P1, P5, 2 runs each | X2 correctness 0.33 → 1.0 (mean of runs); the negatives N1/N2 stay 2/2 (no invention) | yes |
| 9 | Topic guard (`knowledge/topic.py`): "A term, acronym or tool you don't recognize at all is in scope (it may come from the user's papers); one you know belongs to another field is not". The final run refused P6 ("What is PROTsi…?"): the guard never sees the wiki, and earlier runs passed it by chance | 5 unfamiliar-term + 6 off-topic questions, guard `gemini-3-flash`, 2 runs | 11/11 correct in both runs (P6 passes; RLHF, Kubernetes, Black-Scholes, protein shakes still refused). A first wording ("unfamiliar … nothing tying it to another field") let RLHF through | yes |
| 10 | Chat prompt: the citation example `(SIGNOR-277418; PMID 30419043)` replaced by placeholders, "cite each link with the ids given for it … with no id given, name the database only". The final run copied the example id (a real PRKCA → ARX record, so the citation check passed it) into D5 and C2 | D1, D5, C2, P6, P7, N2 on `eval-final`, 2 runs, code tier + Claude judge | copied example id 2 → 0 answers; citations D5 1 → 1/2, C2 1 → 1/1, N2 0 → 2/1, D1/P7 stay 2; correctness 2 everywhere; nothing worse. C2 (run a) still attaches PMIDs of neighbouring links | yes |

| 11 | Writer history (`reader.compact_history`): **cuts were permanent**. PydanticAI stores the processed history back as the run's history, so a page cut to 200 characters once could never be whole again, and placeholders were cut into placeholders ("page written earlier, 65 characters"). The writer then re-read the same pages in rotation until the 40-request limit (nature18003 and PROTsi first attempts, 15× `read_page` of one page). Fix: originals kept by tool-call id and restored before compacting; the latest read of each page not yet written stays whole; `agent_history_chars` setting 14000 → 40000 (Groq users set it back); capture skill "page by page: read a page, write it, then the next" | The two looping papers, each captured twice from the vault state just before (vault git reset), Claude judge on one of each | first attempts reaching the limit 3 of 4 → **0 of 4**; tool-call order is now read → write per page; findings covered 0.9 → 0.9 (PROTsi), 0.8 → 0.9 (nature); unsupported 1 → 0 and 0 → 1 statements (noise); requests per successful attempt 8–11 → 15–18, but about 50 → 15–18 counting the failed attempts | yes |

Eval fixes:
- `score_chat --tier code` saved its file only when a later answer was refused or failed (a `continue` skipped `save`).
- A test vault made by deleting a paper's source notes is invalid: its pages still list those sources, so every update is refused ("Unknown sources") and the writer starts probing (`test-probe` pages). Reset a copy of the vault in git to the commit before the capture instead.

### Final run (2026-11-21, final design, Claude judge)
Vault `eval-final`, writer `gemini-3.8-flash-high`, chat `gemini-3-flash`, judge `claude-sonnet-4-6`. Lint, digest and sync were skipped on the user's request.
- **Capture, all 5 papers** (1495, nature18003, s41467 on 2026-11-20/21; mcdermott and PROTsi after the account change). nature18003 and PROTsi needed a second attempt (the loop fixed in iteration 11, which came after these captures). Against the baseline, same judge:

  | metric | baseline | final |
  |---|---|---|
  | unsupported statements | 2.0% | **0.5%** (2 of 412, both on PROTsi) |
  | main findings covered | 0.66 | **0.86** |
  | uncited blocks | 0 | 0 |
  | pages per paper | 4.0 | 4.6 |
  | writer requests per paper | 9.4 | 10.8 |

- **Chat, 21 questions, all scored** (run `final2`, after iterations 9 and 10, on the 5-paper vault):

  | metric | first final run (3 papers, before 9–10) | final |
  |---|---|---|
  | correctness | 1.95 | **1.95** |
  | completeness | 1.42 | **1.65** |
  | groundedness | 1.89 | **2.0** |
  | citations | 1.63 | **1.75** |
  | invalid citations per answer | 0.05 | **0** |
  | broken link formats | 0.11 | **0** |
  | database precision / top-5 recall | 0.94 / 0.52 | 0.94 / 0.52 |
  | off-topic refusal | correct | correct |
  | input tokens per answer | 4.7k | 3.6k |

  - Below 2: completeness 1 on D6, P4, P6, P8, X1, X2, where answers leave out secondary key facts (e.g. P8's 58% read-out); N1 correctness 1 (it leads with database facts when the question asks about "my sources"); citations 0 on C1 and N2, correct "nothing found" answers the judge marked down for having no citation (its own note says none was needed).

Robustness fixes found while testing:
- **Sync (`gemini-3.8-flash-high`) passes:** 40 findings, 4 pages, 7 requests.
- **`gemini-3-flash` as job model:** it rewrote one page 19 times until the request limit, so a page can now be written at most 3 times per run (`agent_tools.MAX_WRITES_PER_PAGE`; the fact-check revision may still rewrite). It also sends garbled tool calls twice in a row, so it is unfit for jobs.
- **Account switch:** on 2026-11-20 a second Google account was added to the proxy. The first login failed ("no project_id in response") until the account was activated in Google's own tools.

Known limits:
- Paraphrased questions (X2) need semantic search; the wiki search is substring-based.
- `gemini-3-flash` sometimes misses facts that are in its context (P2 noise).

Also fixed along the way:
- the reading cache now includes the reader-prompt hash, so a new prompt reads again;
- the chat runner no longer hard-codes Groq for the topic check;
- question P6 was about a paper outside the pilot; it now asks about the PROTsi paper. Added P7, P8, D6 and N2.

**Next:**
1. ~~The EN+VI quality report~~: done, `report/quality-evaluation-en.md` and `report/quality-evaluation-vi.md` (2026-11-21).
2. Later, if wanted: `run_jobs` (lint, digest, sync) on `eval-final`.
3. **Candidates if needed:**
   - answers leaving out secondary facts (completeness 1 on 6 questions);
   - check that cited PMIDs belong to the cited link (C2), not only that they exist;
   - semantic search for paraphrased questions (X2);
   - section detection for methods without a heading (nature18003).

## 0. First complete run: Gemini through CLIProxyAPI (2026-11-20)

**Setup.** The user runs CLIProxyAPI in Docker (`C:\yuuhi\tools\CLIProxyAPI_8.0.79_windows_amd64`), logged in with a Google account (`-antigravity-login`). In `.env`: `COMPAT_CLIPROXY_BASE_URL=http://localhost:8317/v1` and `COMPAT_CLIPROXY_API_KEY`. Models are then `compat:cliproxy:<id>`. The proxy's `config.yaml` needs `server.host: ""` when it runs in Docker; it was `127.0.0.1`, which made the proxy unreachable, and was changed (backup `config.yaml.bak`).

**Models.** `/v1/models` offers `gemini-3-flash`, `gemini-3.8-flash-high`, `gemini-3.8-flash-tiered`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `claude-sonnet-4-6`, `claude-opus-4-6-thinking`, `gpt-oss-120b-medium` (and an image model). All eight passed a plain request and a tool-call-plus-structured-output request through `app.llm`.

**Roles.**
- Writer and reader: `gemini-3.8-flash-high`.
- Chat: `gemini-3-flash`.
- Judge: `claude-sonnet-4-6`, a different family from the writer.

The vault is `eval-proxy-g38`; results are `capture-eval-proxy-g38.json`, `score-capture-eval-proxy-g38.json`, `chat-…-gemini-3-flash.json` and `score-chat-…json`.

**Capture**

| | 1495.pdf | nature18003.pdf |
|---|---|---|
| Time, model requests | 152 s, 14 | 357 s, 11 |
| Share of text read | 72% (refs and methods skipped) | 99.7% |
| Findings kept | 21 | 43 |
| Pages created / updated | 3 / 0 | 3 / 1 |
| Uncited blocks | 0 | 0 |
| Unsupported statements (judge, full source) | 0 / 34 | 1 / 36 (overstated) |
| Main findings in the wiki | 3 yes, 2 partial of 5 | 3 yes, 1 partial, 1 no of 5 |
| Claims | 0 (no enzyme → substrate statements) | 1: ATM → PRKDC, confirmed, judge "supported" |

**Chat (17 questions).**
- **Off-topic question:** refused.
- **Speed:** 0.5–5 s per answer.
- **Database answers:** precision 1.0 (every kinase named is in the databases), every PMID valid, D5 site recall 1.0. One invented SIGNOR ID in C2 (`SIGNOR-47898`).
- **Judge means (out of 2):** correctness 1.75, completeness 1.38, groundedness 2.0, citations 1.56.
- **Low completeness has two causes:**
  - The database lists are short: the pre-fetched context lists at most 6 regulators or targets.
  - P1, P5, P6 and X1 ask about pilot papers not captured in this run; the model said so correctly, except X1, which answered off-target.

**Fixed during this run.**
- **Parallel tool calls:** pydantic-ai runs several `upsert_index` calls at once in threads. They shared one temp file, which crashed the job and could lose index lines. Fix: a per-vault lock (`store.serialized`), unique temp files, and a retry when Windows reports a file locked. Test: `test_parallel_index_updates_are_not_lost`.
- **Chat judge:** it was not given the pre-fetched database context, so correct database facts were scored as invented (groundedness 1.38). `score_chat.retrieved_text` now rebuilds that context; the old scores are kept as `…judge-without-db.json`.
- **Tests:** `test_llm.clean_env` now ignores the `COMPAT_*` settings in the real `.env`. 200 tests pass.

**Next.**
- Capture the other 3 pilot papers.
- Optionally raise the 6-item cap in `knowledge/context.py::_brief` so database answers list more.
- Write the EN+VI quality report.

## 1. Redesign: cheaper prompts, one code-read capture flow (2026-11-20)

Fixed prompt per request (instructions plus tool definitions), as Groq counts it
(`python -m eval.overhead`; `results/overhead-before.json`, `results/overhead-after.json`):

| Agent | Before (tokens) | After (tokens) |
|---|---|---|
| Capture/sync | 5,026 (writer for long sources: 2,457) | 1,101 |
| Lint | 4,688 | 1,227 |
| Digest | 4,597 | 1,092 |
| Chat | 4,061 | 869 |
| Chat without tools | 1,973 | 459 |
| Reader / topic guard | 319 / 296 | 303 / 296 |

What changed:
- **PDF to Markdown with PyMuPDF** (`vault/store.py::_pdf_text`): `## ` headings, page markers,
  joined hyphenation, running headers and footers removed. All 46 papers extract in 0.1–1.2 s,
  with headings found in every paper (`results/corpus.json`, `headings` column).
- **One capture flow:** code reads every source (`agents/reader.py`):
  - The source is cut at its section headings into parts.
  - Sections matching `CAPTURE_SKIP_SECTIONS` (references, methods, acknowledgements, funding,
    declarations, supplementary) are skipped, with their sub-headings. On the Cell paper this
    takes 132k characters down to 49k.
  - Findings are labelled `(Section, p. N)`, and a short source goes to the writer whole.
  - Claims are recorded by code only; the `record_relation` tool is gone.
- **Lean prompts:**
  - `AGENTS.md` is a short core (grounding, page rules, safety) for jobs only.
  - The skills are shorter. Sync reuses the capture skill; the query and sync skills and `load_skill` are gone.
  - Chat has its own short prompt.
- **Tools per kind:** one `job_agent()` whose tools are filtered per job kind (writer: 5 tools,
  lint: 7). Chat gets 3 vault tools and 4 slim graph tools (`graph/agent_tools.py`:
  `search_entities`, `interactions`, `find_paths`, `get_interaction`).
- **Budget guard:** `tests/test_prompt_budget.py` fails if a fixed prompt grows past its budget.
- **Harness:** `--groq-key alternative` on `run_capture`, `run_chat`, `score_capture` and
  `score_chat` uses `GROQ_API_KEY_ALTERNATIVE`. `run_capture` keeps every attempt's infos, tool
  calls and tool results, and logs the size of every agent request.

## 2. Re-tests after the redesign, and what they found

| Run (1495.pdf unless noted, alternative key) | Result | Cause, fix |
|---|---|---|
| gpt-oss-120b, 1495 + nature18003 | failed | The writer looped for 8 min until its history exceeded 8k tokens, then restarted twice with shorter notes: about 190k tokens, the whole daily cap. **Fix:** history capped at `HISTORY_CHARS` (`reader.compact_history`), the index is put in the task, and the skill asks for several pages per turn |
| gpt-oss-20b #1 | failed | The same writer loop; 13 rewrites of one page |
| gpt-oss-20b #2 | failed | Reader: a malformed reply was re-sent by pydantic-ai's output retry, so the request grew to 9.8k tokens, and a 413 in the reader was fatal. **Fix:** no in-run output retries; one fresh retry, then the part is read in two halves |
| gpt-oss-20b #3 | failed (daily cap) | Reading succeeded: 5 parts, 22 verified findings. The history stayed small (7–10k characters). The tool results showed why the writer loops: (a) it puts `[[source: …]]` after the frontmatter `summary`, which is invalid YAML, so 5 of 9 writes were rejected; (b) it rewrote pages to remove links to pages not written yet. **Fix:** the frontmatter parser quotes such values and drops the citation; links to pages not written are made plain text by code at the end, and the tool says so |

After these fixes the writer should need about 4–8 requests of about 4k tokens each. Not yet
confirmed on a real model.

## Free-tier facts measured

**Groq:**
- Every model has an 8,000 tokens-per-minute cap, which a single request must also stay under.
- Each model has a 200,000 tokens-per-day rolling cap, per key.
- qwen3.8-27b is limited to 1,000 output tokens per minute, so it is unusable for jobs.
- gpt-oss reasoning output counts toward both caps.

**Google AI Studio (this key):**
- The 2.5 family returns 404.
- The pro and preview models return 429.
- The 3.x flash models mostly return 503.
- `gemini-flash-lite-latest` answered intermittently.

**Quota state at pause (2026-11-21 02:43):** gpt-oss-120b is used up on both keys; gpt-oss-20b
on the alternative key is used up. Both recover within 24 h, on a rolling window.

## Earlier fixes (first pilot, 161 → 179 tests)
1. **PDF extraction:** 21 of 46 papers failed with pypdf. This is now superseded by PyMuPDF (all 46 extract).
2. **Upload limit:** now `MAX_UPLOAD_MB` (60).
3. **Extraction blocked the event loop:** PDF extraction now runs in `asyncio.to_thread`.
4. **Busy providers:** retries after a 503, a per-minute 429 or a timeout (`runtime.is_transient`).
5. **Stalled requests:** `MODEL_TIMEOUT_SECONDS` (300).
6. **Long sources:** staged reading. This is now the only flow (section 1).
7. **Crash leftovers:** job markers with a pid liveness check.
8. **Hopeless reading:** reading stops at the first non-recoverable error (daily cap, bad key).
9. **Malformed replies:** these are retried.
10. **Provider messages:** shown without the org/tier details; a used-up daily quota is now named as such.
11. **Harness:** de-duplication (39 unique papers of 46), resumable runner, live progress.

Not fixed: the fact-checker (`agents/verify.py`) sees only the first 12,000 characters of a source.

## How to resume

Prerequisites:
- Docker Desktop is running (Neo4j `signor-neo4j-test`).
- Quotas have recovered.

```bash
cd backend
# 1. Capture (resumable). Watch the "agent request:" lines: each writer request should stay small.
.venv/Scripts/python -m eval.run_capture --job-model groq:openai/gpt-oss-20b --vault eval-lean-gptoss20b --papers 1495.pdf --attempts 1 --groq-key alternative
.venv/Scripts/python -m eval.run_capture --job-model groq:openai/gpt-oss-120b --vault eval-lean-gptoss120b --papers 1495.pdf nature18003.pdf --groq-key alternative
# 2. Score (JUDGE_MODEL env var; a Gemini model if one answers, else groq:openai/gpt-oss-120b)
JUDGE_MODEL=groq:openai/gpt-oss-120b .venv/Scripts/python -m eval.score_capture --vault eval-lean-gptoss120b --groq-key alternative
# 3. Chat (17 questions) and its score
.venv/Scripts/python -m eval.run_chat --chat-model groq:openai/gpt-oss-20b --vault eval-lean-gptoss120b --groq-key alternative --pause 20
.venv/Scripts/python -m eval.score_chat --file chat-eval-lean-gptoss120b-gpt-oss-20b.json --groq-key alternative
# 4. Tables
.venv/Scripts/python -m eval.report > eval/results/tables.md
```

Then write `report/quality-evaluation-en.md` and `-vi.md`, covering:
- the corpus;
- the robustness tables above;
- the redesign's before/after numbers;
- the quality scores;
- threats to validity.

Notes:
- The `eval-*` vaults hold only their init commit: every failed job was rolled back.
- Nothing is committed yet.
