"""Compare reader prompts by code: findings kept, quotes rejected, key facts found.

    cd backend && .venv/Scripts/python -m eval.reader_variants --model compat:cliproxy:gemini-3-flash \
        --papers 1495 nature18003 [--variants current more]

Each variant's instructions replace the reader's for this process only; the app is unchanged.
A key fact counts as found when all of its marker words appear in one finding (statement or
quote). The markers come from the question set's key facts for that paper (KEY_MARKERS below).
No judge model is needed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from eval.common import RESULTS, save

VARIANTS = {
    "current": None,  # the app's READER_RULES
    "more": """\
You read one part of a scientific source for a research wiki about kinases, substrates, proteins,
drugs and diseases. Extract only what this part states; never add outside knowledge.

- findings: every distinct result in this part, up to 10: each measured effect, comparison,
  number, named protein, phosphosite or drug finding, and each conclusion the authors draw. For
  each, a short plain statement that keeps the numbers and names, and a quote that supports it,
  copied character for character from the text (one sentence or clause, under 300 characters).
  Prefer specific results over general background.
- relations: every explicit statement that a kinase or phosphatase phosphorylates or
  dephosphorylates a protein (optionally at a site such as S81 or Thr308). Use gene symbols.
  Give the quote that states it, copied exactly. Skip correlations, binding, co-expression,
  pathway mentions, hypotheses and negative results.

Return empty lists only for parts with no results (references, acknowledgements, boilerplate).
""",
}

# Marker words per key fact (all must appear in one finding), from eval/questions.yaml.
KEY_MARKERS = {
    "1495": [["5 min", "50%"], ["MAPK12"], ["SHC1", "GAB1"], ["heterogeneity"], ["patient"],
             ["serine", "threonine"], ["21 phosph"]],
    "nature18003": [["CDK12", "PAK1"], ["62 phospho"], ["15 of", "26"], ["helical"], ["CETN3"], ["SKP1", "EGFR"],
                    ["PIK3CA"], ["TP53"]],
}


def found(markers: list[list[str]], notes) -> list[bool]:
    texts = [f"{f.statement} {f.quote}".lower() for _, f in notes.findings]
    return [any(all(m.lower() in t for m in ms) for t in texts) for ms in markers]


async def read(variant: str, model: str, needle: str) -> dict:
    import os
    os.environ["READING_CACHE_DIR"] = ""  # always a fresh reading
    from app.agents import reader
    from app.api.deps import registry
    from app.vault.store import VaultRegistry
    reader._cache.clear()
    original = reader.READER_RULES
    reader.READER_RULES = VARIANTS[variant] or original
    reader.reader_agent.cache_clear()
    try:
        tmp = Path(tempfile.mkdtemp())
        shutil.copytree(registry().get("eval-proxy-g38").root, tmp / "probe")
        v = VaultRegistry(tmp).get("probe")
        name = next(s["name"] for s in v.list_sources() if needle in s["name"] and s["name"].endswith(".md"))

        async def emit(ev):
            pass
        notes = await reader.read_in_parts(v, name, emit, [], model=model)
    finally:
        reader.READER_RULES = original
        reader.reader_agent.cache_clear()
    hits = found(KEY_MARKERS.get(needle, []), notes)
    return {"paper": needle, "variant": variant, "parts": notes.parts, "findings": len(notes.findings),
            "rejected_quotes": notes.rejected_quotes, "failed_parts": notes.failed_parts,
            "key_facts_found": sum(hits), "key_facts": len(hits), "notes_chars": len(notes.render(10**6))}


async def main_async(model: str, papers: list[str], variants: list[str]) -> None:
    from app.config import get_settings
    get_settings()
    rows = []
    for p in papers:
        for var in variants:
            r = await read(var, model, p)
            rows.append(r)
            print(json.dumps(r), flush=True)
    save(RESULTS / f"reader-variants-{model.split(':')[-1]}.json", rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--papers", nargs="+", default=["1495", "nature18003"])
    ap.add_argument("--variants", nargs="+", default=list(VARIANTS))
    a = ap.parse_args()
    asyncio.run(main_async(a.model, a.papers, a.variants))


if __name__ == "__main__":
    main()
