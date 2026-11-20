# Quality evaluation harness

Scripts that test PhosphoChat on real papers through the real app (in-process `TestClient`,
throwaway admin account, real `vaults/`). Results go to `eval/results/` (gitignored); the
findings are written up in `report/quality-evaluation-*.md`.

Run from `backend/`, with Neo4j up and model keys in `.env`:

```bash
# 1. Profile the papers, pick the pilot set (text cached in results/text/)
.venv/Scripts/python -m eval.corpus --pick 5

# 2. Capture the pilot papers with one job model, into an evaluation vault
.venv/Scripts/python -m eval.run_capture --job-model groq:openai/gpt-oss-120b --vault eval-gptoss120b

# 3. Score the captured wiki and claims (judge: JUDGE_MODEL, default google:gemini-3.5-flash)
.venv/Scripts/python -m eval.score_capture --vault eval-gptoss120b

# 4. Ask the question set with one chat model, then score the answers
.venv/Scripts/python -m eval.run_chat --chat-model groq:openai/gpt-oss-20b --vault eval-gptoss120b
.venv/Scripts/python -m eval.score_chat --file chat-eval-gptoss120b-gpt-oss-20b.json

# 5. Tables and summary.json
.venv/Scripts/python -m eval.report > eval/results/tables.md
```

Every script is resumable: rerun it after a rate limit or a crash and it continues where it
stopped. Point `EVAL_DATASET` at another folder of PDFs to use other papers.

| File | What it does |
|---|---|
| `corpus.py` | Extracts every PDF, records size, pages, term density and the topic gate's code decision, finds duplicates, picks the pilot papers |
| `run_capture.py` | Uploads papers through `POST /capture` with a chosen job model; records status, time, tokens, tool calls, pages, claims |
| `score_capture.py` | Source coverage, page statistics, groundedness and findings coverage (judge with the full source), claim labels (judge) and a recall proxy |
| `questions.yaml` | 17 questions: database (gold from Neo4j), paper, cross-paper, claims, negative, off-topic |
| `run_chat.py` | Asks each question in a fresh conversation through `POST /chat`; records answer, events, time, usage |
| `score_chat.py` | Checks in code (citations exist, kinase precision/recall vs the graph, sites, refusal) plus a blind judge rubric (0–2 × 4) |
| `judges.py` | The judge prompts and structured outputs |
| `report.py` | Combines everything into `summary.json` and Markdown tables |
