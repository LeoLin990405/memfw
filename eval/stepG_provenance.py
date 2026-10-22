"""Step G provenance audit (G6): are inferred memories recorded as user verbatim?

The rubric is written to ``results/stepG/provenance_rubric.md`` *before* any
judgement is produced. Two sampling frames are reported:

* ``derived_frame`` (primary): up to 30 memories per system whose stored text is
  not a contiguous substring of its source turn, i.e. genuinely inferred text.
* ``all_frame`` (secondary, fixed denominator): up to 30 uniformly sampled
  memories per system, used when a system stores no inferred memories at all.

Only public LoCoMo input is used. No private vault or user data is read.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.stepG_runner import (  # noqa: E402
    _resolve,
    _write_json,
    load_config,
    load_subset,
)

AUDIT_SAMPLE_SIZE = 30
DERIVED_LABEL_KEYS = ("derived", "inferred", "is_inference", "source_type", "provenance_kind")
RUBRIC = """# Step G provenance audit rubric

Scope: public LoCoMo turns only. Goal: check whether a stored memory that is *not*
verbatim source text is nevertheless recorded as if it were the user's own words.

## Definitions

- **Source text**: the LoCoMo turn (`dia_id`) attached to the stored memory via
  metadata (`mem0`) or the deterministic memory-id mapping (`memfw`).
- **Verbatim**: lower-cased, punctuation-stripped memory text (after removing a
  leading `speaker X` prefix) is a contiguous substring of the normalized source
  turn. Setext/label prefixes added by a writer are ignored.
- **Derived/inferred**: any stored memory that is not verbatim under the rule above.

## Rules (applied mechanically, identical to the Day 11 audit rules)

- **R0**: no resolvable source turn id -> `missing_source_link` (not flagged).
- **R1**: verbatim -> `verbatim_source_text` (not flagged).
- **R2**: derived and metadata carries an explicit derived/inferred label
  (keys: derived, inferred, is_inference, source_type, provenance_kind) ->
  `derived_labelled` (not flagged).
- **R3**: derived, unlabelled, and the system records the memory in the user's
  memory namespace / attributes it to the user -> \
`derived_attributed_to_user_unlabelled` (**FLAGGED**: inferred text stored as the user's).
- **R4**: derived, unlabelled, no user attribution -> `derived_unlabelled` (not flagged).

## Attribution determination per system

- **memfw**: the record has an explicit `provenance` field. `verbatim-public-locomo-turn`
  is a verbatim label; it is not user attribution. A derived memfw record without a
  derived label would be R4 unless its provenance says otherwise.
- **mem0**: `Memory.add(..., user_id=...)` stores every produced memory in the user's
  memory namespace and mem0 exposes no per-memory inference/derived label. Step G also
  passes Mem0's public `prompt=` custom-instructions knob, which asks the extractor to
  emit `"attributed_to": "user"`. In the audited run this request was only partly
  followed: most derived memories are explicitly user-attributed, while a minority are
  explicitly assistant-attributed. Under the **Step-G rule**, a record with no
  `attributed_to` field defaults to the user namespace and therefore counts as R3; only
  an explicit non-user value (`assistant`) falls to R4. Under the **Day-11 rule**, a
  missing attribution is R4. Consequently the R3 flag rate is partly prompt-forced and
  partly model behaviour, not an independent quality measurement.
- **Difference from the Day 11 audit**: Day 11 sampled 30 memories uniformly and applied
  R3 only when `attributed_to == "user"` exactly. Step G's primary `derived_frame` samples
  only inferred memories, and its `all_frame` is a uniform fallback; a `day11_frame`
  (uniform sample, Day-11 R3 rule) is also reported for like-for-like direction checks.

## Outputs

- `provenance_audit.csv`: one row per audited memory, with its frame, classification,
  rule label, flagged boolean, and (truncated) public memory/source text.
- `provenance_summary.json`: denominators and flag counts for all three sampling frames.
  The audit uses run `rep1` only and reports this run id in the summary.
"""

_WORD = re.compile(r"[a-z0-9']+")


def write_rubric(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(RUBRIC, encoding="utf-8")


def normalize_text(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


def classify_memory(
    memory_text: str,
    source_text: str | None,
    speaker: str | None,
    metadata: dict[str, Any],
    *,
    attributed_to_user: bool,
    provenance: str | None = None,
) -> dict[str, Any]:
    """Apply the rubric to one stored memory (public text only)."""
    norm_memory = normalize_text(memory_text)
    # Writers may prepend a date and/or speaker label; strip those before the verbatim test.
    norm_memory = re.sub(r"^\d{4} \d{2} \d{2} ", "", norm_memory)
    if speaker:
        norm_memory = re.sub(rf"^(speaker )?{re.escape(speaker.lower())} ", "", norm_memory)
    if source_text is None:
        return {
            "derived": False,
            "label": "missing_source_link",
            "flagged": False,
            "novel_numbers": False,
        }
    norm_source = normalize_text(source_text)
    verbatim = bool(norm_memory) and norm_memory in norm_source
    source_words = set(norm_source.split()) | set(normalize_text(speaker or "").split())
    memory_words = norm_memory.split()
    novel = [word for word in memory_words if word not in source_words]
    novel_numbers = any(any(ch.isdigit() for ch in word) for word in novel)
    labelled = any(key in metadata for key in DERIVED_LABEL_KEYS)
    if provenance is not None and any(
        marker in provenance.lower() for marker in ("inferred", "derived", "model")
    ):
        labelled = True
    if verbatim:
        label = "verbatim_source_text"
    elif labelled:
        label = "derived_labelled"
    elif attributed_to_user:
        label = "derived_attributed_to_user_unlabelled"
    else:
        label = "derived_unlabelled"
    return {
        "derived": not verbatim,
        "label": label,
        "flagged": label == "derived_attributed_to_user_unlabelled",
        "novel_numbers": novel_numbers,
    }


def load_turn_index(subset: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {turn["dia_id"]: turn for turn in subset["turns"]}


def _load_memories(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    loaded: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def classify_system(
    system: str,
    memories: Sequence[dict[str, Any]],
    turn_index: dict[str, dict[str, str]],
    *,
    day11_attribution: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in memories:
        dia_ids = item.get("dia_ids") or []
        source = turn_index.get(dia_ids[0]) if dia_ids else None
        memory_text = str(item.get("memory") or "")
        raw_metadata = item.get("metadata")
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        if system == "memfw":
            attributed = False
            provenance = item.get("provenance")
        else:
            provenance = None
            if day11_attribution:
                # Day-11 rule: only an explicit attributed_to == "user" counts.
                attributed = str(item.get("attributed_to") or "").lower() == "user"
            else:
                # Step-G rule: mem0 stores memories in the user namespace by default.
                attributed = True
                if item.get("attributed_to"):
                    attributed = str(item["attributed_to"]).lower() == "user"
        judgement = classify_memory(
            memory_text,
            source["text"] if source else None,
            source["speaker"] if source else None,
            metadata,
            attributed_to_user=attributed,
            provenance=provenance,
        )
        rows.append(
            {
                "system": system,
                "memory_id": str(item.get("memory_id") or ""),
                "dia_id": dia_ids[0] if dia_ids else "",
                "memory_text": memory_text,
                "source_text": source["text"] if source else "",
                **judgement,
            }
        )
    return rows


def summarize_frame(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    labels: dict[str, int] = {}
    for row in rows:
        labels[row["label"]] = labels.get(row["label"], 0) + 1
    return {
        "sampled": len(rows),
        "derived": sum(1 for row in rows if row["derived"]),
        "flagged": sum(1 for row in rows if row["flagged"]),
        "novel_numbers": sum(1 for row in rows if row["novel_numbers"]),
        "label_counts": dict(sorted(labels.items())),
    }


def run_audit(config: dict[str, Any], *, run_id: str = "rep1") -> dict[str, Any]:
    results_dir = _resolve(config["paths"]["results_dir"])
    subset = load_subset(config)
    turn_index = load_turn_index(subset)
    rubric_path = results_dir / "provenance_rubric.md"
    write_rubric(rubric_path)
    seed = int(config["seed"])
    audit_rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "schema_version": "stepg-provenance-v1",
        "rubric": str(rubric_path.relative_to(REPO_ROOT)),
        "seed": seed,
        "run_id": run_id,
        "framing": (
            "derived_frame is the primary G6 frame (up to 30 inferred memories per system); "
            "all_frame is a fixed-denominator fallback over uniformly sampled memories; "
            "day11_frame re-uses the Day 11 uniform frame and R3 rule for direction checks. "
            "mem0 R3 is partly prompt-forced: the custom extraction prompt requests "
            "attributed_to='user', but some derived memories are still returned as "
            "assistant-attributed or without attribution (R4)."
        ),
        "audited_run_id": run_id,
        "systems": {},
        "source_files": {},
    }
    for system in ("memfw", "mem0"):
        memories_path = results_dir / "runs" / run_id / system / "memories.json"
        memories = _load_memories(memories_path)
        classified = classify_system(system, memories, turn_index)
        rng = random.Random(seed)
        derived = [row for row in classified if row["derived"]]
        derived_sample = rng.sample(derived, min(AUDIT_SAMPLE_SIZE, len(derived)))
        all_sample = rng.sample(classified, min(AUDIT_SAMPLE_SIZE, len(classified)))
        attribution_counts: dict[str, int] = {}
        if system == "mem0":
            for item in memories:
                value = item.get("attributed_to")
                key = str(value).lower() if isinstance(value, str) else "missing"
                attribution_counts[key] = attribution_counts.get(key, 0) + 1
        day11_classified = classify_system(system, memories, turn_index, day11_attribution=True)
        day11_sample = rng.sample(day11_classified, min(AUDIT_SAMPLE_SIZE, len(day11_classified)))
        summary["systems"][system] = {
            "memories_total": len(classified),
            "derived_total": len(derived),
            "derived_frame": summarize_frame(derived_sample),
            "all_frame": summarize_frame(all_sample),
            "day11_frame": summarize_frame(day11_sample),
            "derived_shortfall": max(0, AUDIT_SAMPLE_SIZE - len(derived)),
            "attribution_counts": attribution_counts,
        }
        summary["source_files"][system] = str(memories_path.relative_to(REPO_ROOT))
        for frame_name, sample in (
            ("derived_frame", derived_sample),
            ("all_frame", all_sample),
            ("day11_frame", day11_sample),
        ):
            for row in sample:
                audit_rows.append(
                    {
                        "frame": frame_name,
                        **row,
                    }
                )
    csv_path = results_dir / "provenance_audit.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        fieldnames = [
            "frame",
            "system",
            "memory_id",
            "dia_id",
            "derived",
            "label",
            "flagged",
            "novel_numbers",
            "memory_text",
            "source_text",
        ]
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for row in audit_rows:
            writer.writerow(
                {
                    **{key: row[key] for key in fieldnames if key in row},
                    "memory_text": row["memory_text"][:400],
                    "source_text": row["source_text"][:400],
                }
            )
    summary["csv"] = str(csv_path.relative_to(REPO_ROOT))
    _write_json(results_dir / "provenance_summary.json", summary)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Step G provenance audit")
    parser.add_argument(
        "--config", type=Path, default=REPO_ROOT / "results" / "stepG" / "config.yaml"
    )
    parser.add_argument("--run-id", default="rep1")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    summary = run_audit(config, run_id=args.run_id)
    print(json.dumps(summary["systems"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
