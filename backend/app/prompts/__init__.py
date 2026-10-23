"""App-wide agent instructions: the LLM-Wiki schema (AGENTS.md) and the skills.

One copy for every vault; vaults hold content only. Files are read on each call, so editing
a skill takes effect on the next run without restarting the server.
"""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent
SKILLS_DIR = PROMPTS_DIR / "skills"


def schema() -> str:
    return (PROMPTS_DIR / "AGENTS.md").read_text(encoding="utf-8")


def skill_names() -> list[str]:
    return sorted(p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md"))


def skill(name: str) -> str:
    path = SKILLS_DIR / name / "SKILL.md"
    if name not in skill_names() or not path.is_file():
        raise KeyError(name)
    return path.read_text(encoding="utf-8")
