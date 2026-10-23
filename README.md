# PhosphoChat

A website for an LLM-Wiki "second brain" (Karpathy's method) over the PhosphoSIGNOR
kinase–substrate graph:

- **Capture**: paste text, a URL or a file. It is saved unchanged in `sources/`, then an agent
  folds it into `wiki/` pages.
- **Chat**: answers from the wiki and the Neo4j graph, citing `[[pages]]`, `[[source: …]]` and
  `(SIGNOR-…; PMID …)`. A good answer can be saved back into the wiki.
- **Wiki and Sources**: a browser with wiki links, callouts and backlinks.
- **Log**: every session that changed the vault, with buttons for Sync, Lint and Digest.

The SIGNOR graph tools from the old `mcp/` server run inside the backend, not as a separate MCP
process. The agents get only narrow tools: no shell, no code and no Cypher, so they can't wander
off and write scripts.

## Run it

Prerequisites: Python 3.12+, Node 20+, Docker, and a Groq API key (https://console.groq.com).

```bash
# 1. Config
cp .env.example .env           # set GROQ_API_KEY (and NEO4J_PASSWORD if not testpassword)

# 2. Neo4j with PhosphoSIGNOR (skip if the old signor-neo4j-test container is running)
docker compose up -d
backend/.venv/Scripts/python backend/scripts/load_signor.py --file "../mcp/data/PhosphoSIGNOR_all_Data.tsv" --wipe

# 3. Backend
cd backend
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000

# 4. Frontend (second terminal)
cd frontend
npm install
npm run dev                    # http://localhost:5173 (proxies /api to :8000)
```

On first start the backend creates an **empty** vault in `vaults/default/` from the template
and gives it its own git repo. `npm run build` puts the UI in `frontend/dist`, which the backend
then serves itself at http://localhost:8000.

## Models

`MODEL` in `.env` is a pydantic-ai model string. Each provider reads its own key, so
switching provider is a config change:

| Provider | `MODEL` | Key |
|---|---|---|
| Groq (default) | `groq:openai/gpt-oss-120b`, `groq:openai/gpt-oss-20b`, `groq:qwen/qwen3.8-27b` | `GROQ_API_KEY` |
| Claude | `anthropic:claude-sonnet-5-5` | `ANTHROPIC_API_KEY` (Console key, not a Pro login) |
| Google AI Studio | `google-gla:gemini-2.5-flash` | `GOOGLE_API_KEY` |
| OpenAI-compatible / Ollama | `openai:qwen3:14b` | `OPENAI_API_KEY=ollama`, `OPENAI_BASE_URL=http://localhost:11434/v1` |

Groq's free tier has tight tokens-per-minute limits. Every tool result is capped at
`TOOL_OUTPUT_CHARS`, and a rate-limit error shows up as a readable message in the UI.

## How it is built

```
frontend (React+Vite)  ──/api──▶  FastAPI
                                  ├─ api/          capture, jobs (+SSE), chat (SSE), wiki browse
                                  ├─ agents/       chat agent (read-only) · job agent (skill-driven) · fact-checker
                                  ├─ prompts/      AGENTS.md schema + skills, one copy for all vaults
                                  ├─ tools/        narrow vault tools + graph tools + URL fetch
                                  ├─ graph/        PhosphoSIGNOR queries (ported MCP server)
                                  ├─ vault/        Vault: rules enforced in code, git per job
                                  └─ jobs/         in-process queue, one writer per vault
                                     │                         │
                              vaults/<name>/ (Markdown+git)   Neo4j (read-only queries)
```

**The code does the bookkeeping and the model does the editorial work.** A weak model can't
break the bookkeeping, because the code handles it:

| Done in code | Done by the agent (tools) |
|---|---|
| Save sources unchanged with frontmatter (create-only, dedup by URL and content hash) | Read sources, the index, pages and the graph |
| Find unprocessed sources (sync); add frontmatter to files dropped into `sources/` | Write whole wiki pages (`write_page` rejects missing frontmatter, unknown sources and uncited paragraphs) |
| Mechanical lint (broken links, orphans, index drift, schema, uncited blocks) and its index auto-fixes | Semantic lint (stale pages, contradictions, duplicates), `lint-report` |
| Prepend the `log.md` entry under `# Log`, index any page left unindexed, check links | `upsert_index` with descriptions; the recap for digest |
| Fact-check new pages against the source (verifier agent) and send unsupported claims back once | Revise the flagged claims |
| git commit per job; roll back wiki/index/log if the job fails | |

A vault holds content only: `sources/`, `wiki/`, `index.md`, `log.md` and its `.git`. The
schema and skills are app-wide, in `backend/app/prompts/`. Editing a skill there changes every
vault, and takes effect on the next job without a restart. Obsidian can still open a vault to read it.

**Grounding.** The wiki records what the sources say, not what the model knows. This is enforced
three ways:
1. The schema forbids background knowledge.
2. `write_page` rejects any paragraph, list item or table row without a `[[source: …]]` or SIGNOR
   citation.
3. After capture and sync, a verifier agent with no tools compares each claim with the source
   text. The writer gets one turn to fix whatever it flags.

The fact-check costs roughly 1.5–2× the tokens of a capture. Turn it off with `VERIFY_CAPTURES=false`.

Captured text is untrusted. The prompts treat sources as data, URL fetches refuse
private and loopback addresses, and agents have no tool that runs code.

## Tests

```bash
cd backend && .venv/Scripts/python -m pytest -q
```

- `test_graph.py`: the old MCP smoke test, ported. It needs Neo4j and is skipped otherwise.
- `test_vault.py`: the vault rules (paths, immutability, frontmatter, log, index, lint, git).
- `test_jobs.py`: capture, lint and sync with a scripted model, so no API key is needed. Covers
  tool-call correction, rollback, output caps and the fact-check/revision loop.

## Prototype limits

- Single user, no auth. Bind to localhost.
- Jobs and chat history live in memory and are lost on restart; the vault itself is on disk and in git.
- `search_pages` is a plain substring search. Add SQLite FTS or embeddings once the wiki grows.
- Not built yet: graph view, scheduled digest, model picker in the UI, and an MCP facade over `app/graph/signor.py`.
