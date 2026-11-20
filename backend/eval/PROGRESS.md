# Pilot quality evaluation: progress note (updated 2026-11-20, first complete run)

Scope chosen by the user:
- a pilot of a few papers from `C:\yuuhi\int3132\dataset\pdf`;
- a 17-question chat test;
- an EN+VI quality report;
- the reusable harness in `backend/eval/`;
- fixes for the problems found, then a re-test.

**Status:** no capture has finished yet, so there are no quality numbers. The first pilot (23
attempts, all failed) showed that the prompts did not fit Groq's free tier, so the prompts and
the capture flow were redesigned (section 1). The re-tests after the redesign found three more
problems, now fixed (section 2). Each re-test then ended on a daily token cap, not on an app error.
199 tests pass.

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
