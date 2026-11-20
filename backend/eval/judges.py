"""LLM judges used by the scoring scripts. Each returns structured output and runs at temperature 0
on JUDGE_MODEL (env; default google:gemini-3.5-flash). The scoring scripts take
--groq-key for a groq: judge. Judges never see which model wrote what."""

from __future__ import annotations

import asyncio
import os
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings

from app.llm import get_model

JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "google:gemini-3.5-flash")
SETTINGS = ModelSettings(temperature=0, max_tokens=16000)
MAX_SOURCE_CHARS = 400_000


class Unsupported(BaseModel):
    page: str = ""
    claim: str = Field(description="The exact sentence or list item from the page.")
    problem: Literal["not_in_source", "contradicts_source", "wrong_citation", "overstated"] = "not_in_source"
    note: str = ""


class Grounding(BaseModel):
    statements_checked: int = Field(description="How many factual statements the pages make in total.")
    unsupported: list[Unsupported] = Field(default_factory=list)


class Finding(BaseModel):
    finding: str
    in_wiki: Literal["yes", "partial", "no"]


class Findings(BaseModel):
    findings: list[Finding]


class ClaimLabel(BaseModel):
    id: str
    label: Literal["supported", "wrong_direction", "wrong_site", "not_kinase_substrate", "unsupported"]
    note: str = ""


class ClaimLabels(BaseModel):
    labels: list[ClaimLabel]


class Score(BaseModel):
    correctness: int = Field(ge=0, le=2)
    completeness: int = Field(ge=0, le=2)
    groundedness: int = Field(ge=0, le=2)
    citations: int = Field(ge=0, le=2)
    note: str = ""


GROUNDING_RULES = """\
You check wiki pages against the source document they were written from. Count the factual
statements the pages make, and list every statement the source does not support: not stated,
contradicted, cited to the wrong source, or overstated (e.g. the source says "may"). Paraphrase
that keeps the meaning is fine. Be strict but fair; do not list statements the source supports."""

FINDINGS_RULES = """\
From the paper's text, name its 5 most important findings (from the abstract and results).
For each, say whether the wiki pages state it: yes, partial (mentioned but missing the key
point), or no."""

CLAIM_RULES = """\
Each claim says an enzyme (kinase or phosphatase) acts on a target, optionally at a site, backed
by a quote from the paper and the text around it. Label each claim:
- supported: the quote and context state this enzyme phosphorylates/dephosphorylates this target
  (at this site, if a site is given);
- wrong_direction: the roles are swapped (e.g. the target phosphorylates the enzyme);
- wrong_site: the relation is right but the site is not the one stated;
- not_kinase_substrate: the text describes something else (correlation, binding, co-expression,
  a pathway mention, a hypothesis or a negative result);
- unsupported: the text does not say it at all."""

ANSWER_RULES = """\
You grade an assistant's answer to a researcher's question. You get the question, the reference
key facts (what a good answer contains), and the context the assistant retrieved. Score 0-2:
- correctness: 2 no false statements; 1 minor errors; 0 a main point is wrong or invented.
- completeness: 2 covers the key facts; 1 covers some; 0 misses most.
- groundedness: 2 every claim is backed by the retrieved context or cited database records;
  1 some unbacked general knowledge; 0 mostly unbacked.
- citations: 2 claims carry specific, plausible citations ([[page]], [[source: ...]], SIGNOR ids,
  PMIDs); 1 some; 0 none or wrong.
For a question whose right answer is "the library has nothing on this", a clear statement of
that scores 2 on correctness and completeness. Write one short note."""


@lru_cache
def _agent(kind: str) -> Agent:
    rules, out = {"grounding": (GROUNDING_RULES, Grounding), "findings": (FINDINGS_RULES, Findings),
                  "claims": (CLAIM_RULES, ClaimLabels), "answer": (ANSWER_RULES, Score)}[kind]
    return Agent(instructions=rules, output_type=out, defer_model_check=True, name=f"judge-{kind}")


async def judge(kind: str, prompt: str, retries: int = 4):
    """Run one judge; waits and retries on rate limits (free tiers)."""
    for attempt in range(retries):
        try:
            r = await _agent(kind).run(prompt, model=get_model(JUDGE_MODEL), model_settings=SETTINGS)
            return r.output
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            wait = 65 if any(m in str(e) for m in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE")) else 10
            print(f"   judge {kind} failed ({type(e).__name__}: {str(e)[:120]}); retry in {wait}s", flush=True)
            await asyncio.sleep(wait)


def clip(text: str, n: int = MAX_SOURCE_CHARS) -> str:
    return text if len(text) <= n else text[:n] + "\n[... truncated ...]"
