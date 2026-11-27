"""Measure whether "Mark says…" is faithful.

Two questions, both answerable without opinion:

1. Is the quote actually his? Every output sentence must appear in the cited transcript.
2. Does the persona refuse when the corpus has nothing? It must, or it is a horoscope.

Run:  .venv/Scripts/python.exe eval/persona_fidelity.py
"""

from __future__ import annotations

import json
import sys
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mmdesk import persona  # noqa: E402
from mmdesk.retrieval_shim import search  # noqa: E402

GOLDEN = ROOT / "eval" / "retrieval_golden.jsonl"
REPORT = ROOT / "docs" / "PERSONA-FIDELITY.md"


def transcript_text(video_id: str) -> str:
    """All spoken text for a video, to check a quote against the whole thing."""
    path = ROOT / "transcripts" / f"{video_id}.json"
    if not path.exists():
        return ""
    data = json.loads(path.read_text(encoding="utf-8"))
    return " ".join(seg.get("text", "") for seg in data.get("segments", []))


def normalise(text: str) -> str:
    return " ".join(text.replace("…", "").split()).lower()


def verbatim_score(quote: str, source: str) -> float:
    """How much of the quote is literally in the source, by character overlap.

    1.0 means every character of the quote (after whitespace/chunk normalisation) is present.
    Below 1.0 means something was added, reordered or edited — which the persona must never do.
    """
    q = normalise(quote)
    s = normalise(source)
    if not q:
        return 0.0
    if q in s:
        return 1.0
    matcher = SequenceMatcher(None, q, s, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(q)


def main() -> int:
    lines = GOLDEN.read_text(encoding="utf-8").splitlines()
    items = [json.loads(line) for line in lines if line.strip()]
    rows = []
    for item in items:
        hits = search(item["question"], k=5)
        utt = persona.speak(item["question"], hits)
        source = transcript_text(utt.video_id) if utt.video_id else ""
        score = verbatim_score(utt.quote, source) if (source and not utt.refused) else (1.0 if utt.refused else 0.0)
        numbers_ok = True
        if not utt.refused and source:
            missing_numbers = {n for n in persona.numbers_in(utt.quote) if n not in persona.numbers_in(source)}
            numbers_ok = not missing_numbers
        asked = set(utt.supported_terms) | set(utt.missing_terms)
        coverage = (len(utt.supported_terms) / len(asked)) if asked else 1.0
        rows.append({
            "id": item["id"], "label": item["label"], "question": item["question"],
            "refused": utt.refused, "verbatim": round(score, 3),
            "numbers_ok": numbers_ok, "video": utt.video_id, "stamp": utt.stamp,
            "quote": utt.quote, "reason": utt.refusal_reason,
            "coverage": round(coverage, 2), "missing": utt.missing_terms,
        })

    answerable = [r for r in rows if r["label"] == "answerable"]
    no_answer = [r for r in rows if r["label"] == "no_answer"]
    mentioned = [r for r in rows if r["label"] == "mentioned_only"]

    served = [r for r in answerable if not r["refused"]]
    perfect = [r for r in served if r["verbatim"] == 1.0]
    clean_numbers = [r for r in served if r["numbers_ok"]]

    lines = [
        "# Persona fidelity",
        "",
        "Does “Mark says…” actually say what he said?",
        "",
        f"- Golden set: **{len(rows)} questions** ({len(answerable)} answerable, {len(no_answer)} never discussed, {len(mentioned)} mentioned only).",
        "- A quote is scored by how much of its text appears **verbatim** in the cited transcript. "
        "1.0 means nothing was added, reordered or paraphrased.",
        "",
        "## Results",
        "",
        f"- **Verbatim rate: {len(perfect)}/{len(served)} served quotes are 100% his words** "
        f"({(len(perfect) / max(1, len(served))):.0%}).",
        f"- **Number fidelity: {len(clean_numbers)}/{len(served)} quotes contain only numbers present in the transcript.**",
        f"- **Refusal on never-discussed topics: {sum(1 for r in no_answer if r['refused'])}/{len(no_answer)}.**",
        f"- **Mentioned-only topics served: {sum(1 for r in mentioned if not r['refused'])}/{len(mentioned)}** — correct, "
        "and labelled as a mention rather than as his view.",
        "",
        f"- **Question coverage: mean {sum(r['coverage'] for r in served) / max(1, len(served)):.2f}** of the asked terms "
        f"appear in the quote; {sum(1 for r in served if r['coverage'] == 1.0)}/{len(served)} quotes cover every term. "
        "This is the honest measure of whether the quote answers the question — verbatim rate only proves "
        "it is really his sentence.",
        "",
        "## Per question",
        "",
        "| id | label | refused | verbatim | numbers ok | source |",
        "|---|---|---|---:|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['id']} | {r['label']} | {'yes' if r['refused'] else 'no'} | "
            f"{r['verbatim']:.2f} | {'yes' if r['numbers_ok'] else 'no'} | "
            f"{r['video'] or '—'}{(' @ ' + r['stamp']) if r['stamp'] else ''} |"
        )

    lines += ["", "## Quotes as returned", ""]
    for r in rows[:6]:
        if r["refused"]:
            lines.append(f"- **{r['id']}** refused — `{r['reason']}`")
        else:
            lines.append(f"- **{r['id']}** [{r['video']} @ {r['stamp']}]: “{r['quote'][:220]}”")

    lines += [
        "",
        "## What this does and does not prove",
        "",
        "- It proves the persona **does not invent his words**: the quote is a substring of the",
        "  transcript, and every number in it is his, not ours. Note that this is structural — the",
        "  quote is *selected*, not generated — so 100% verbatim is a regression guard, not a result",
        "  to boast about. The number that carries information is question coverage.",
        "- It does **not** prove the quote answers the question. Extraction can pick a sentence that",
        "  mentions the topic without addressing it — `missing_terms` records which words of the",
        "  question the quote never covers, and that is shown rather than hidden.",
        "- It says nothing about whether following his rules makes money. That needs the journal's",
        "  horizons to close.",
        "",
        "Reproduce with `python eval/persona_fidelity.py`.",
    ]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"served {len(served)}/{len(answerable)} answerable · verbatim {len(perfect)}/{len(served)} · "
          f"numbers {len(clean_numbers)}/{len(served)} · refused {sum(1 for r in no_answer if r['refused'])}/{len(no_answer)}")
    print(f"wrote {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
