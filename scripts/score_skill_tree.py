#!/usr/bin/env python3
"""Write the Skill-Tree leaderboard JSON from judged checklists.

A case passes when none of its REQUIRED ``vqa_checklist`` items is FAIL. The
score of a slice is that pass rate. Slices are overall, category level 1,
category level 2, education level, and knowledge operation type.

The dataset jsonl supplies ``category`` and ``labels``. Each ``--judged``
argument is ``<model-key-or-name>=<judged-jsonl>``. Users may pass any subset
of models or register new models on the fly, and optionally ``--merge`` scores
into an existing ``docs/data/skill-tree.json``.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "docs" / "data" / "skill-tree.json"

FAIL = "FAIL"
REQUIRED = "REQUIRED"

MODELS: list[dict[str, str]] = [
    {"key": "nano_banana_2", "name": "Nano Banana 2", "logo": "assets/logos/Google.svg"},
    {"key": "nano_banana_pro", "name": "Nano Banana Pro", "logo": "assets/logos/Google.svg"},
    {"key": "gpt_image_2", "name": "GPT-Image-2.0", "logo": "assets/logos/OpenAI.svg"},
    {"key": "flux2_pro", "name": "FLUX.2 Pro", "logo": "assets/logos/Black_Forest_Labs.svg"},
    {"key": "flux2_dev", "name": "FLUX.2 Dev", "logo": "assets/logos/Black_Forest_Labs.svg"},
    {"key": "recraft_v4", "name": "Recraft v4", "logo": "assets/logos/Recraft.png"},
    {"key": "qwen_image_2", "name": "Qwen Image 2.0", "logo": "assets/logos/Alibaba.svg"},
    {"key": "qwen_image_2512", "name": "Qwen Image 2512", "logo": "assets/logos/Alibaba.svg"},
    {"key": "wan_2_7_pro", "name": "Wan 2.7 Pro", "logo": "assets/logos/Alibaba.svg"},
    {"key": "hunyuan_image_3", "name": "HunyuanImage 3.0", "logo": "assets/logos/Tencent.svg"},
    {"key": "midjourney_v7", "name": "Midjourney v7", "logo": "assets/logos/Midjourney.png"},
    {"key": "seedream_4", "name": "Seedream 4.0", "logo": "assets/logos/ByteDance_Seed.svg"},
    {"key": "seedream_4_5", "name": "Seedream 4.5", "logo": "assets/logos/ByteDance_Seed.svg"},
    {"key": "seedream_5_lite", "name": "Seedream 5.0 Lite", "logo": "assets/logos/ByteDance_Seed.svg"},
    {"key": "kling_image_3_0_omni", "name": "Kling Image 3.0 Omni", "logo": "assets/logos/klingai.svg"},
]

MODEL_ALIASES: dict[str, str] = {
    "flux_2_pro": "flux2_pro",
    "flux_2_dev": "flux2_dev",
    "qwen_image_2_0": "qwen_image_2",
    "hunyuan_image_3_0": "hunyuan_image_3",
    "seedream_4_0": "seedream_4",
    "seedream_5_0_lite": "seedream_5_lite",
    "gpt_image_2_0": "gpt_image_2",
    "kolors_o3": "kling_image_3_0_omni",
}

META = {
    "eyebrow": "KnowVis Benchmark Leaderboard",
    "title": "🌏 Skill-Tree Benchmark",
    "subtitle": (
        "Case-level correctness results for world-knowledge visual generation models, "
        "organized by Skill-Tree evaluation slices."
    ),
    "scoreRange": "0-1",
    "footerNote": (
        "Scores are evaluated on the 240-case KnowVis Skill-Tree unseen validation split "
        "(val_gt) using Gemini 3.1 Pro Preview."
    ),
}

# (slice_type, slice_value, emoji, short_label or None)
DOMAIN_SLICES = [
    ("all", "all", "🏆", "Overall"),
    ("category_l1", "Natural Science", "🔬", None),
    ("category_l1", "Formal Science", "📐", None),
    ("category_l1", "Human Civilization & Society", "🏛️", None),
]
SUBDOMAIN_SLICES = [
    ("category_l2", "Physics", "⚛️", "Physics"),
    ("category_l2", "Chemistry", "🧪", "Chemistry"),
    ("category_l2", "Biology", "🧬", "Biology"),
    ("category_l2", "Climate & Astronomy", "🌦️", "Climate & Astronomy"),
    ("category_l2", "Physical Geography", "🗻", "Physical Geography"),
    ("category_l2", "Mathematics", "📊", "Mathematics"),
    ("category_l2", "History & Culture", "📜", "History & Culture"),
    ("category_l2", "Humanities & Architecture", "🏛️", "Humanities & Architecture"),
    ("category_l2", "Language & Writing", "✍️", "Language & Writing"),
    ("category_l2", "General Social Knowledge", "🌐", "Social Knowledge"),
    ("category_l2", "Occupations & Identities", "👥", "Occupations & Identities"),
    ("category_l2", "Systems & Processes", "⚙️", "Systems & Processes"),
]
EDUCATION_SLICES = [
    ("education_level", "Primary School", "🎒", None),
    ("education_level", "Junior High School", "📓", None),
    ("education_level", "High School", "🎓", None),
    ("education_level", "University", "🏫", None),
    ("education_level", "Professional Areas", "💼", None),
]
OPERATION_SLICES = [
    ("knowledge_operation_type", "Explicit Knowledge Recall", "🔍", None),
    ("knowledge_operation_type", "Implicit Knowledge Recall", "📖", None),
    ("knowledge_operation_type", "Knowledge Reasoning", "🧠", None),
]

GROUPS = [
    ("domain", "Overall & Top-level Domains", 1, DOMAIN_SLICES),
    ("subdomain", "Knowledge Subdomains", 2, SUBDOMAIN_SLICES),
    ("education", "Education Level", 3, EDUCATION_SLICES),
    ("operation", "Knowledge Operation", 4, OPERATION_SLICES),
]


def slugify(text: str) -> str:
    text = text.lower().replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def default_display_name(raw_key: str) -> str:
    cleaned = raw_key.strip()
    if any(ch.isupper() or ch == " " or ch == "-" for ch in cleaned):
        return cleaned
    return " ".join(part.capitalize() for part in cleaned.split("_") if part)


def resolve_model_entry(raw_key: str) -> dict[str, str]:
    known_by_key = {model["key"]: dict(model) for model in MODELS}
    known_by_slug = {slugify(model["name"]): dict(model) for model in MODELS}
    candidate = raw_key.strip()
    slug = MODEL_ALIASES.get(slugify(candidate), slugify(candidate))
    if candidate in known_by_key:
        return known_by_key[candidate]
    if slug in known_by_key:
        return known_by_key[slug]
    if slug in known_by_slug:
        return known_by_slug[slug]
    return {"key": slug or candidate, "name": default_display_name(candidate)}


def label_value(raw: object) -> str | None:
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def case_passes(case: dict) -> bool:
    for item in case.get("vqa_checklist") or []:
        if item.get("judge_type") == REQUIRED and item.get("judgment") == FAIL:
            return False
    return True


def slice_membership(dataset_case: dict) -> dict[str, str]:
    category = dataset_case.get("category") or []
    labels = dataset_case.get("labels") or {}
    found: dict[str, str] = {"all": "all"}
    if len(category) >= 1 and category[0]:
        found["category_l1"] = str(category[0])
    if len(category) >= 2 and category[1]:
        found["category_l2"] = str(category[1])
    if education := label_value(labels.get("education_level")):
        found["education_level"] = education
    if operation := label_value(labels.get("knowledge_operation_type")):
        found["knowledge_operation_type"] = operation
    return found


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_number}: {exc}") from exc
    return rows


def parse_judged(values: list[str]) -> tuple[list[dict[str, str]], dict[str, Path]]:
    models: list[dict[str, str]] = []
    judged: dict[str, Path] = {}
    for value in values:
        raw_key, separator, raw_path = value.partition("=")
        if not separator or not raw_key.strip() or not raw_path.strip():
            raise SystemExit(f"--judged must look like <model-key>=<jsonl>, got {value!r}")
        model_entry = resolve_model_entry(raw_key)
        key = model_entry["key"]
        if key in judged:
            raise SystemExit(f"Duplicate --judged for {key}")
        models.append(model_entry)
        judged[key] = Path(raw_path.strip())
    return models, judged


def score_models(
    dataset: dict[str, dict], judged_paths: dict[str, Path]
) -> dict[tuple[str, str], dict[str, float]]:
    """Pass rate of each model inside each (slice_type, slice_value)."""
    buckets: dict[tuple[str, str], dict[str, list[bool]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for model_key, path in judged_paths.items():
        for case in load_jsonl(path):
            case_id = case.get("case_id")
            if case_id not in dataset:
                raise SystemExit(f"{path}: case_id {case_id!r} is not in the dataset")
            passed = case_passes(case)
            for slice_type, slice_value in slice_membership(dataset[case_id]).items():
                buckets[(slice_type, slice_value)][model_key].append(passed)

    scores: dict[tuple[str, str], dict[str, float]] = {}
    for key, by_model in buckets.items():
        scores[key] = {
            model_key: round(sum(flags) / len(flags), 3)
            for model_key, flags in by_model.items()
            if flags
        }
    return scores


def merge_existing_scores(
    existing_doc: dict,
    new_models: list[dict[str, str]],
    new_scores: dict[tuple[str, str], dict[str, float]],
) -> tuple[list[dict[str, str]], dict[tuple[str, str], dict[str, float]]]:
    existing_models: list[dict[str, str]] = [
        dict(m) for m in (existing_doc.get("models") or [])
    ]
    existing_keys = [m["key"] for m in existing_models]
    merged_scores: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)

    slice_groups = existing_doc.get("sliceGroups") or {}
    for group_key, _, _, slices in GROUPS:
        group_obj = slice_groups.get(group_key) or {}
        by_slice_key = {
            s.get("key"): s.get("values") or []
            for s in (group_obj.get("slices") or [])
        }
        for slice_type, slice_value, _, _ in slices:
            s_key = "all" if slice_value == "all" else slugify(slice_value)
            values = by_slice_key.get(s_key) or []
            for idx, m_key in enumerate(existing_keys):
                if idx < len(values):
                    merged_scores[(slice_type, slice_value)][m_key] = float(values[idx])

    for slice_id, by_model in new_scores.items():
        merged_scores[slice_id].update(by_model)

    by_key = {m["key"]: m for m in existing_models}
    ordered_keys = list(existing_keys)
    for m in new_models:
        if m["key"] not in by_key:
            ordered_keys.append(m["key"])
        by_key[m["key"]] = m

    known_order = {m["key"]: idx for idx, m in enumerate(MODELS)}
    ordered_models = [by_key[k] for k in ordered_keys]
    ordered_models.sort(key=lambda m: (0, known_order[m["key"]]) if m["key"] in known_order else (1, ordered_keys.index(m["key"])))
    return ordered_models, merged_scores


def build_document(
    models: list[dict[str, str]],
    scores: dict[tuple[str, str], dict[str, float]],
) -> dict:
    model_keys = [model["key"] for model in models]
    slice_groups: dict[str, dict] = {}
    for group_key, label, order, slices in GROUPS:
        built = []
        for slice_type, slice_value, emoji, short_label in slices:
            by_model = scores.get((slice_type, slice_value), {})
            missing = [key for key in model_keys if key not in by_model]
            if missing:
                raise SystemExit(
                    f"Slice {slice_type}={slice_value!r} has no cases for: "
                    + ", ".join(missing)
                )
            entry = {
                "key": "all" if slice_value == "all" else slugify(slice_value),
                "label": "Overall" if slice_value == "all" else slice_value,
                "values": [by_model[key] for key in model_keys],
                "emoji": emoji,
            }
            if short_label is not None:
                entry["shortLabel"] = short_label
            built.append(entry)
        slice_groups[group_key] = {"label": label, "order": order, "slices": built}
    return {"meta": META, "models": models, "sliceGroups": slice_groups}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True, help="Skill-Tree jsonl with category and labels")
    parser.add_argument(
        "--judged",
        action="append",
        required=True,
        metavar="KEY=PATH",
        help="judged jsonl for a model (<model-key-or-name>=<jsonl>); repeat for each model to compare",
    )
    parser.add_argument(
        "--merge",
        type=Path,
        default=None,
        help="optional existing leaderboard JSON to merge/update model scores into",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="leaderboard JSON to write")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dataset_rows = load_jsonl(args.dataset)
    dataset = {row["case_id"]: row for row in dataset_rows}
    if len(dataset) != len(dataset_rows):
        raise SystemExit(f"{args.dataset} contains duplicate case_id values")
    models, judged_paths = parse_judged(args.judged)
    scores = score_models(dataset, judged_paths)
    if args.merge is not None:
        existing_doc = json.loads(args.merge.read_text(encoding="utf-8"))
        models, scores = merge_existing_scores(existing_doc, models, scores)
    document = build_document(models, scores)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
