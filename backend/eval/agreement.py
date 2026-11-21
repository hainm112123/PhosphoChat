"""How far the cheap routine judge agrees with the strong one, on the same pages and answers.

    cd backend && .venv/Scripts/python -m eval.agreement --cheap <score file> --strong <score file>

Both files must score the same run (same vault for capture, same chat file for chat) with
different judges. For chat answers: per dimension (0-2), the share of identical scores and of
scores within 1. For capture: findings labels (yes/partial/no) matched one by one, and the
unsupported-statement counts per paper. The cheap judge is trusted for routine iterations when
at least 80% of chat scores are within 1 and the findings labels mostly agree.
"""

from __future__ import annotations

import argparse

from eval.common import RESULTS, load

DIMENSIONS = ("correctness", "completeness", "groundedness", "citations")


def chat_agreement(cheap: dict, strong: dict) -> list[str]:
    lines, within_all, n_all = ["| dimension | same | within 1 | items |", "|---|---|---|---|"], 0, 0
    shared = [q for q in cheap["answers"] if q in strong["answers"]
              and cheap["answers"][q].get("judge") and strong["answers"][q].get("judge")]
    for d in DIMENSIONS:
        pairs = [(cheap["answers"][q]["judge"][d], strong["answers"][q]["judge"][d]) for q in shared]
        same = sum(a == b for a, b in pairs)
        within = sum(abs(a - b) <= 1 for a, b in pairs)
        within_all, n_all = within_all + within, n_all + len(pairs)
        lines.append(f"| {d} | {same}/{len(pairs)} | {within}/{len(pairs)} | {len(pairs)} |")
    far = [f"- {q} {d}: cheap {cheap['answers'][q]['judge'][d]}, strong {strong['answers'][q]['judge'][d]}"
           for q in shared for d in DIMENSIONS
           if abs(cheap["answers"][q]["judge"][d] - strong["answers"][q]["judge"][d]) > 1]
    share = within_all / n_all if n_all else 0
    lines += ["", f"Within 1 overall: {share:.0%} -> "
              + ("the cheap judge is good enough for routine runs." if share >= .8 else
                 "too far apart: use code checks plus a small strong-judge sample."),
              *(["", "Two points apart:", *far] if far else [])]
    return lines


def capture_agreement(cheap: dict, strong: dict) -> list[str]:
    lines = ["| paper | unsupported (cheap / strong) | statements (cheap / strong) | findings labels agreeing |",
             "|---|---|---|---|"]
    for paper in sorted(set(cheap["papers"]) & set(strong["papers"])):
        c, s = cheap["papers"][paper], strong["papers"][paper]
        gc, gs = c.get("grounding") or {}, s.get("grounding") or {}
        # The two judges list findings in their own words: compare the label distribution.
        lc = sorted(x["in_wiki"] for x in c.get("findings") or [])
        ls = sorted(x["in_wiki"] for x in s.get("findings") or [])
        agree = sum(min(lc.count(k), ls.count(k)) for k in ("yes", "partial", "no"))
        lines.append(f"| {paper} | {len(gc.get('unsupported', []))} / {len(gs.get('unsupported', []))} | "
                     f"{gc.get('statements_checked')} / {gs.get('statements_checked')} | "
                     f"{agree}/{max(len(lc), len(ls))} |")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cheap", required=True)
    ap.add_argument("--strong", required=True)
    a = ap.parse_args()
    cheap, strong = load(RESULTS / a.cheap), load(RESULTS / a.strong)
    print(f"# Judge agreement: {cheap.get('judge')} vs {strong.get('judge')}\n")
    print("\n".join(capture_agreement(cheap, strong) if "papers" in cheap else chat_agreement(cheap, strong)))


if __name__ == "__main__":
    main()
