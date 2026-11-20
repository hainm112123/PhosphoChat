"""Profile the paper collection and pick the pilot papers.

    cd backend && .venv/Scripts/python -m eval.corpus [--pick 5] [--max-chars 130000]

For every PDF: size, pages, characters, density of kinase/phosphorylation terms, and the topic
gate's code decision. Extracted text is cached in results/text/ (used by the scoring scripts).
"""

from __future__ import annotations

import argparse
import re
import time

from eval.common import DATASET, RESULTS, load, save

TERMS = re.compile(r"\b(kinases?|phosphorylat\w*|phosphosites?|substrates?)\b", re.I)


def profile(force: bool = False) -> list[dict]:
    from app.config import get_settings
    limit = get_settings().max_upload_mb * 1_000_000
    from app.knowledge import topic
    from app.vault.store import extract_text
    cache = load(RESULTS / "corpus.json", {}) if not force else {}
    out = []
    for pdf in sorted(DATASET.glob("*.pdf")):
        row = cache.get(pdf.name)
        if row is None:
            t = time.time()
            data = pdf.read_bytes()
            try:
                text, _ = extract_text(pdf.name, data)
                error = None
            except Exception as e:  # noqa: BLE001 - recorded as a finding
                text, error = "", f"{type(e).__name__}: {e}"
            (RESULTS / "text").mkdir(parents=True, exist_ok=True)
            (RESULTS / "text" / f"{pdf.stem}.txt").write_text(text, encoding="utf-8")
            first = next((ln.strip() for ln in text.splitlines()
                          if len(ln.strip()) > 25 and not ln.startswith("<!--")), pdf.stem)
            terms = len(TERMS.findall(text))
            quick = topic.quick_check(f"{pdf.stem}\n{text}", kind="source")
            row = {"file": pdf.name, "mb": round(len(data) / 1e6, 2), "over_upload_limit": len(data) > limit,
                   "pages": text.count("<!-- page "), "chars": len(text), "terms": terms,
                   "headings": len(re.findall(r"(?m)^## ", text)),
                   "density": round(1000 * terms / max(len(text), 1), 2), "first_line": first[:120],
                   "topic_code": "on-topic" if quick else "undecided (model needed)",
                   "extract_s": round(time.time() - t, 1), "error": error}
            cache[pdf.name] = row
            save(RESULTS / "corpus.json", cache)
            print(f"{pdf.name[:50]:50} {row['chars']:7d} chars  {row['headings']:3d} headings  "
                  f"{row['extract_s']:5.1f} s  density {row['density']:5.2f}  {row['topic_code']}")
        out.append(row)
    return out


def _fingerprint(file: str) -> str:
    """The same paper saved twice (journal PDF and an 'ext' copy) differs only slightly:
    compare the first words of the text, ignoring spacing."""
    text = (RESULTS / "text" / f"{file.rsplit('.', 1)[0]}.txt").read_text(encoding="utf-8")
    return re.sub(r"\W+", "", text[:3000].lower())[:400]


def duplicates(rows: list[dict]) -> list[list[str]]:
    groups: dict[str, list[str]] = {}
    for r in rows:
        if r["chars"]:
            groups.setdefault(_fingerprint(r["file"]), []).append(r["file"])
    return [g for g in groups.values() if len(g) > 1]


def pick(rows: list[dict], n: int, max_chars: int) -> list[str]:
    ok = [r for r in rows if not r["error"] and not r["over_upload_limit"] and 0 < r["chars"] <= max_chars]
    chosen, seen = [], set()
    for r in sorted(ok, key=lambda r: -r["density"]):
        fp = _fingerprint(r["file"])
        if fp not in seen:
            seen.add(fp)
            chosen.append(r["file"])
    return chosen[:n]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pick", type=int, default=5)
    ap.add_argument("--max-chars", type=int, default=130_000)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    rows = profile(a.force)
    chosen = pick(rows, a.pick, a.max_chars)
    save(RESULTS / "pilot_papers.json", chosen)
    dups = duplicates(rows)
    save(RESULTS / "duplicates.json", dups)
    print(f"\n{len(rows)} papers; {sum(1 for r in rows if r['error'])} unreadable; "
          f"{sum(r['over_upload_limit'] for r in rows)} over the upload limit; "
          f"{sum(1 for r in rows if r['topic_code'] == 'on-topic')} on-topic by code alone; "
          f"duplicate groups: {dups}")
    print("Pilot papers:")
    for f in chosen:
        r = next(x for x in rows if x["file"] == f)
        print(f"  {f}  ({r['chars']} chars, density {r['density']})  {r['first_line'][:80]}")


if __name__ == "__main__":
    main()
