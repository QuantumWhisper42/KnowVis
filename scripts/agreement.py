#!/usr/bin/env python3
"""Agreement, Cohen's kappa and confusion matrices between two labelings of the same cases.

Either side can be a human annotator, a judge, or a later run of the same judge
configuration. ``--gt`` is the reference labeling and ``--pred`` is the labeling being
compared. The same command covers a judge against a human labeling, two human
annotators, two judge configurations, and two runs of one configuration at different
times. The caller supplies both files; this script does not schedule a rerun.

Both files are jsonl dumps of the current checklist, one case per line, and the label
on every item is ``judgment``. ``--pred`` is matched item by item against the reference
files passed with ``--gt``.

OPTIONAL items are scored two ways. The strict slices keep PASS/FAIL/NA apart, while the
``basic`` ones fold NA into PASS and score FAIL vs not-FAIL, which is what the old
``lib/statistics/agent_judge.py`` measured.

Alongside accuracy every slice reports precision, recall and F1 with FAIL as the positive
class, so a true positive is the judge catching a real defect in the generated image.

Case level is likewise reported twice: ``primary`` aggregates ``vqa_checklist`` alone and
is the headline number, ``auxiliary`` also folds in ``additional_vqa_checklist``.

Every VQA-level number is also broken out per ``component`` under
``vqa_level_by_component``, to show which kinds of checklist item the judge agrees on.

A judge run that did not cover every case is tolerated. A case the prediction is missing
or cannot be aligned with is dropped whole, out of both numerator and denominator, and
listed under ``coverage`` in the report; an NA where only PASS/FAIL is meaningful is
counted as FAIL. Only a dump that is malformed outright is still fatal. A run that never
judged ``additional_vqa_checklist`` at all is a declared scope rather than a failure, so
it takes ``--vqa-only`` instead of losing every case to a length mismatch.

Everything is stdlib-only so the script runs under a bare ``python3`` without ``uv sync``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_GT_PATHS = (
    PROJECT_ROOT / "seed" / "data" / "test_gt_zh.jsonl",
    PROJECT_ROOT / "seed" / "data" / "val_gt_zh.jsonl",
)
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "stat" / "agreement.json"

VQA_CHECKLIST = "vqa_checklist"
ADDITIONAL_VQA_CHECKLIST = "additional_vqa_checklist"

REQUIRED = "REQUIRED"
OPTIONAL = "OPTIONAL"
ADDITIONAL = "ADDITIONAL"
"""`judge_type` stand-in for additional items, which carry no `judge_type` of their own."""

UNKNOWN_COMPONENT = "(unspecified)"
"""Stand-in so a dump missing `component` still groups instead of crashing."""

PASS = "PASS"
FAIL = "FAIL"
NA = "NA"

BINARY_LABELS = (PASS, FAIL)
TERNARY_LABELS = (PASS, FAIL, NA)

MAX_REPORTED_PROBLEMS = 20
MAX_LISTED_CASES = 20


def label_domain(judge_type: str) -> tuple[str, ...]:
    """OPTIONAL items are scored PASS/FAIL/NA, everything else is strictly binary."""
    return TERNARY_LABELS if judge_type == OPTIONAL else BINARY_LABELS


# ---------------------------------------------------------------------------
# Input shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    question: str
    judge_type: str
    component: str
    judgment: Any
    """Kept untyped on purpose: a malformed dump reaches strict validation, not a crash."""


@dataclass(frozen=True)
class Case:
    case_id: str
    vqa_checklist: tuple[Item, ...]
    additional_vqa_checklist: tuple[Item, ...]


@dataclass(frozen=True)
class Pair:
    case_id: str
    checklist: str
    index: int
    question: str
    judge_type: str
    component: str
    gt: str
    pred: str


@dataclass
class Source:
    path: Path
    name: str | None = None
    gt_paths: tuple[Path, ...] = ()
    """Where this source's ground truth came from, filled in once the inputs are known."""
    vqa_only: bool = False
    """Set for a run that never judged the additional checklist, so its absence is the
    declared scope rather than a case that failed halfway."""

    @property
    def label(self) -> str:
        return self.name or self.path.stem

    @property
    def checklists(self) -> tuple[str, ...]:
        if self.vqa_only:
            return (VQA_CHECKLIST,)
        return (VQA_CHECKLIST, ADDITIONAL_VQA_CHECKLIST)


class LoadError(Exception):
    """A dump that cannot be read at all: bad JSON, wrong shape, duplicate case_id.

    Distinct from `StrictError`, which is about labels the metrics cannot use; this one
    fires before there is anything to score.
    """


def _read_items(
    raw_case: dict[str, Any],
    key: str,
    *,
    origin: str,
) -> tuple[Item, ...]:
    items = []
    for index, raw in enumerate(raw_case.get(key) or []):
        location = f"{origin} case={raw_case.get('case_id')} {key}[{index}]"
        if not isinstance(raw, dict):
            raise LoadError(f"{location}: expected an object, got {type(raw).__name__}")
        judge_type = (
            ADDITIONAL
            if key == ADDITIONAL_VQA_CHECKLIST
            else raw.get("judge_type") or REQUIRED
        )
        items.append(
            Item(
                question=raw.get("question") or "",
                judge_type=judge_type,
                component=raw.get("component") or UNKNOWN_COMPONENT,
                judgment=raw.get("judgment"),
            )
        )
    return tuple(items)


def _to_case(raw_case: dict[str, Any], *, origin: str) -> Case:
    case_id = raw_case.get("case_id")
    if not case_id:
        raise LoadError(f"{origin}: a record has no case_id")
    return Case(
        case_id=case_id,
        vqa_checklist=_read_items(raw_case, VQA_CHECKLIST, origin=origin),
        additional_vqa_checklist=_read_items(
            raw_case, ADDITIONAL_VQA_CHECKLIST, origin=origin
        ),
    )


def load_jsonl_cases(path: Path) -> dict[str, Case]:
    cases: dict[str, Case] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            origin = f"{path.name}:{line_number}"
            try:
                raw_case = json.loads(line)
            except json.JSONDecodeError as error:
                raise LoadError(f"{origin}: {error}") from error
            case = _to_case(raw_case, origin=origin)
            if case.case_id in cases:
                raise LoadError(f"{origin}: duplicate case_id {case.case_id}")
            cases[case.case_id] = case
    return cases


def load_ground_truth(paths: Sequence[Path]) -> dict[str, Case]:
    ground_truth: dict[str, Case] = {}
    for path in paths:
        for case_id, case in load_jsonl_cases(path).items():
            if case_id in ground_truth:
                raise LoadError(
                    f"{path.name}: case_id {case_id} already came from an earlier gt file"
                )
            ground_truth[case_id] = case
    return ground_truth


# ---------------------------------------------------------------------------
# Pairing and strict validation
# ---------------------------------------------------------------------------


class StrictError(Exception):
    """Raised when the input is broken in a way no amount of leniency can absorb."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__(f"{len(problems)} problem(s) found")
        self.problems = list(problems)


class CaseSkipped(Exception):
    """A case the prediction cannot be scored against, dropped whole."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Coverage:
    """What actually made it into the metrics, and what silently did not."""

    gt_cases: int = 0
    evaluated_cases: int = 0
    skipped_cases: list[dict[str, str]] = field(default_factory=list)
    unused_prediction_cases: list[str] = field(default_factory=list)
    na_coerced_to_fail: dict[str, int] = field(
        default_factory=lambda: {"gt": 0, "pred": 0}
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "gt_cases": self.gt_cases,
            "evaluated_cases": self.evaluated_cases,
            "skipped_cases": self.skipped_cases,
            "unused_prediction_cases": self.unused_prediction_cases,
            "na_coerced_to_fail": dict(self.na_coerced_to_fail),
        }


def normalize_label(
    coverage: Coverage,
    problems: list[str],
    *,
    case_id: str,
    checklist: str,
    index: int,
    side: str,
    judge_type: str,
    judgment: Any,
) -> str | None:
    """Map a raw judgment onto its slice's label domain, or None if it is unusable.

    NA on a REQUIRED or additional item is not a real label, it is a judge that failed to
    answer, so it is counted as FAIL rather than thrown away. A value that is not a
    judgment at all means the dump is malformed and stays fatal.
    """
    if judgment in label_domain(judge_type):
        return judgment
    if judgment == NA:
        coverage.na_coerced_to_fail[side] += 1
        return FAIL
    problems.append(
        f"case={case_id} {checklist}[{index}] {side} judgment={judgment!r} "
        f"is not a judgment"
    )
    return None


def _pair_case(
    coverage: Coverage,
    problems: list[str],
    *,
    case_id: str,
    gt_case: Case,
    pred_case: Case,
    checklists: Sequence[str],
) -> list[Pair]:
    """Pair one case, raising CaseSkipped as soon as the two sides do not line up."""
    pairs: list[Pair] = []
    for checklist in checklists:
        gt_items: tuple[Item, ...] = getattr(gt_case, checklist)
        pred_items: tuple[Item, ...] = getattr(pred_case, checklist)
        if len(gt_items) != len(pred_items):
            raise CaseSkipped(
                f"{checklist} length differs: {len(gt_items)} gt item(s) vs "
                f"{len(pred_items)} predicted item(s)"
            )

        for index, (gt_item, pred_item) in enumerate(zip(gt_items, pred_items)):
            if gt_item.question != pred_item.question:
                raise CaseSkipped(f"question text differs at {checklist}[{index}]")
            if gt_item.judge_type != pred_item.judge_type:
                raise CaseSkipped(
                    f"judge_type differs at {checklist}[{index}]: "
                    f"gt={gt_item.judge_type} pred={pred_item.judge_type}"
                )

            gt_label = normalize_label(
                coverage,
                problems,
                case_id=case_id,
                checklist=checklist,
                index=index,
                side="gt",
                judge_type=gt_item.judge_type,
                judgment=gt_item.judgment,
            )
            pred_label = normalize_label(
                coverage,
                problems,
                case_id=case_id,
                checklist=checklist,
                index=index,
                side="pred",
                judge_type=gt_item.judge_type,
                judgment=pred_item.judgment,
            )
            if gt_label is None or pred_label is None:
                continue

            pairs.append(
                Pair(
                    case_id=case_id,
                    checklist=checklist,
                    index=index,
                    question=gt_item.question,
                    judge_type=gt_item.judge_type,
                    # Grouping metadata, not something the judge decides, so the gt's
                    # value wins rather than adding another way to skip a case.
                    component=gt_item.component,
                    gt=gt_label,
                    pred=pred_label,
                )
            )
    return pairs


def pair_against_ground_truth(
    ground_truth: dict[str, Case],
    predictions: dict[str, Case],
    checklists: Sequence[str],
) -> tuple[list[Pair], Coverage]:
    """Match a prediction dump item by item against the ground truth.

    A case the prediction did not fully cover is dropped whole rather than partially
    scored: case aggregation fails a case on a single FAIL, so keeping the surviving
    items would silently flip a FAIL case to PASS.
    """
    coverage = Coverage(gt_cases=len(ground_truth))
    coverage.unused_prediction_cases = sorted(set(predictions) - set(ground_truth))
    problems: list[str] = []
    pairs: list[Pair] = []

    for case_id, gt_case in ground_truth.items():
        pred_case = predictions.get(case_id)
        if pred_case is None:
            coverage.skipped_cases.append(
                {"case_id": case_id, "reason": "missing from the prediction"}
            )
            continue
        try:
            case_pairs = _pair_case(
                coverage,
                problems,
                case_id=case_id,
                gt_case=gt_case,
                pred_case=pred_case,
                checklists=checklists,
            )
        except CaseSkipped as skipped:
            coverage.skipped_cases.append(
                {"case_id": case_id, "reason": skipped.reason}
            )
            continue
        coverage.evaluated_cases += 1
        pairs += case_pairs

    if problems:
        raise StrictError(problems)
    return pairs, coverage


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def agreement(pairs: Sequence[Pair]) -> dict[str, Any]:
    """Accuracy plus the FAIL-as-positive detection rates.

    FAIL is the positive class, so a true positive is the judge catching a real mistake in
    the generated image. NA is negative alongside PASS, which keeps precision and recall
    defined the same way whether the slice is binary or ternary. An undefined rate stays
    None rather than collapsing to 0.0, so "never predicted FAIL" cannot be read as
    "predicted FAIL and got them all wrong".
    """
    agree = sum(1 for pair in pairs if pair.gt == pair.pred)
    tp = sum(1 for pair in pairs if pair.gt == FAIL and pair.pred == FAIL)
    fp = sum(1 for pair in pairs if pair.gt != FAIL and pair.pred == FAIL)
    fn = sum(1 for pair in pairs if pair.gt == FAIL and pair.pred != FAIL)

    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    if precision is None or recall is None:
        f1 = None
    elif precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)

    return {
        "n": len(pairs),
        "agree": agree,
        "accuracy": agree / len(pairs) if pairs else None,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": len(pairs) - tp - fp - fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def confusion(pairs: Sequence[Pair], labels: Sequence[str]) -> dict[str, Any]:
    """Rows are the ground truth, columns are the prediction."""
    position = {label: i for i, label in enumerate(labels)}
    matrix = [[0] * len(labels) for _ in labels]
    for pair in pairs:
        matrix[position[pair.gt]][position[pair.pred]] += 1
    row_totals = [sum(row) for row in matrix]
    col_totals = [sum(row[i] for row in matrix) for i in range(len(labels))]
    return {
        "labels": list(labels),
        "matrix": matrix,
        "row_totals": row_totals,
        "col_totals": col_totals,
        "n": sum(row_totals),
    }


def cohen_kappa(matrix: dict[str, Any]) -> dict[str, Any]:
    """Cohen's kappa straight off the confusion matrix, for any number of classes."""
    labels = matrix["labels"]
    n = matrix["n"]
    if n == 0:
        return {"labels": labels, "n": 0, "po": None, "pe": None, "kappa": None}

    po = sum(matrix["matrix"][i][i] for i in range(len(labels))) / n
    pe = sum(
        (matrix["row_totals"][i] / n) * (matrix["col_totals"][i] / n)
        for i in range(len(labels))
    )
    return {
        "labels": labels,
        "n": n,
        "po": po,
        "pe": pe,
        # pe == 1 means both raters put everything in the same single class, so chance
        # already explains the agreement and kappa is undefined.
        "kappa": (po - pe) / (1 - pe) if pe != 1 else None,
    }


def aggregate_cases(pairs: Sequence[Pair]) -> list[Pair]:
    """Collapse each case to one PASS/FAIL pair: a single FAIL anywhere fails the case."""
    gt_by_case: dict[str, str] = {}
    pred_by_case: dict[str, str] = {}
    for pair in pairs:
        if pair.gt == FAIL:
            gt_by_case[pair.case_id] = FAIL
        else:
            gt_by_case.setdefault(pair.case_id, PASS)
        if pair.pred == FAIL:
            pred_by_case[pair.case_id] = FAIL
        else:
            pred_by_case.setdefault(pair.case_id, PASS)

    return [
        Pair(
            case_id=case_id,
            checklist="case",
            index=0,
            question="",
            judge_type=REQUIRED,
            component=UNKNOWN_COMPONENT,
            gt=gt,
            pred=pred_by_case[case_id],
        )
        for case_id, gt in gt_by_case.items()
    ]


def collapse_na(pairs: Sequence[Pair]) -> list[Pair]:
    """Fold NA into PASS so FAIL vs not-FAIL is all that is left.

    This is what the old `calculate_vqa_consistency` scored: FAIL is the positive class
    and `PASS | NA` is one negative class, so calling a PASS item NA counted as agreement.
    Reported as the "basic" slices next to the strict ones.
    """
    return [
        pair
        if pair.gt != NA and pair.pred != NA
        else Pair(
            case_id=pair.case_id,
            checklist=pair.checklist,
            index=pair.index,
            question=pair.question,
            judge_type=pair.judge_type,
            component=pair.component,
            gt=PASS if pair.gt == NA else pair.gt,
            pred=PASS if pair.pred == NA else pair.pred,
        )
        for pair in pairs
    ]


@dataclass(frozen=True)
class Slice:
    id: str
    title: str
    labels: tuple[str, ...]
    basic: bool = False
    """Whether NA is folded into PASS before scoring."""


VQA_SLICES: tuple[Slice, ...] = (
    Slice("binary_required", f"{VQA_CHECKLIST} (REQUIRED)", BINARY_LABELS),
    Slice("binary_additional", ADDITIONAL_VQA_CHECKLIST, BINARY_LABELS),
    Slice("binary_combined", f"{VQA_CHECKLIST} (REQUIRED) + additional", BINARY_LABELS),
    Slice("ternary_optional", f"{VQA_CHECKLIST} (OPTIONAL)", TERNARY_LABELS),
    Slice(
        "basic_optional",
        f"basic {VQA_CHECKLIST} (OPTIONAL)",
        BINARY_LABELS,
        basic=True,
    ),
    Slice("basic_vqa_checklist", f"basic {VQA_CHECKLIST}", BINARY_LABELS, basic=True),
    Slice(
        "basic_combined",
        f"basic {VQA_CHECKLIST} + additional",
        BINARY_LABELS,
        basic=True,
    ),
)


def slice_pairs(pairs: Sequence[Pair], slice_id: str) -> list[Pair]:
    match slice_id:
        case "binary_required":
            return [
                p
                for p in pairs
                if p.checklist == VQA_CHECKLIST and p.judge_type == REQUIRED
            ]
        case "binary_additional":
            return [p for p in pairs if p.checklist == ADDITIONAL_VQA_CHECKLIST]
        case "binary_combined":
            return [
                p
                for p in pairs
                if p.checklist == ADDITIONAL_VQA_CHECKLIST or p.judge_type == REQUIRED
            ]
        case "ternary_optional" | "basic_optional":
            return [
                p
                for p in pairs
                if p.checklist == VQA_CHECKLIST and p.judge_type == OPTIONAL
            ]
        case "basic_vqa_checklist":
            return [p for p in pairs if p.checklist == VQA_CHECKLIST]
        case "basic_combined":
            return list(pairs)
        case _:
            raise ValueError(f"unknown slice: {slice_id}")


CASE_SCOPES: tuple[tuple[str, str], ...] = (
    ("primary", VQA_CHECKLIST),
    ("auxiliary", f"{VQA_CHECKLIST} + {ADDITIONAL_VQA_CHECKLIST}"),
)


def build_case_level(pairs: Sequence[Pair], scope: str) -> dict[str, Any]:
    case_pairs = aggregate_cases(pairs)
    matrix = confusion(case_pairs, BINARY_LABELS)
    return {
        "scope": scope,
        "agreement": agreement(case_pairs),
        "kappa": cohen_kappa(matrix),
        "confusion": matrix,
    }


def build_vqa_level(pairs: Sequence[Pair]) -> dict[str, Any]:
    """Every VQA-level number for one set of pairs, whole report or a single component."""
    vqa_pairs = [p for p in pairs if p.checklist == VQA_CHECKLIST]
    additional_pairs = [p for p in pairs if p.checklist == ADDITIONAL_VQA_CHECKLIST]

    kappas: dict[str, Any] = {}
    confusions: dict[str, Any] = {}
    detection: dict[str, Any] = {}
    for vqa_slice in VQA_SLICES:
        selected = slice_pairs(pairs, vqa_slice.id)
        if vqa_slice.basic:
            selected = collapse_na(selected)
        matrix = confusion(selected, vqa_slice.labels)
        confusions[vqa_slice.id] = matrix
        kappas[vqa_slice.id] = cohen_kappa(matrix)
        detection[vqa_slice.id] = agreement(selected)

    return {
        "counts": {
            VQA_CHECKLIST: len(vqa_pairs),
            ADDITIONAL_VQA_CHECKLIST: len(additional_pairs),
            "total": len(pairs),
        },
        "agreement": {
            VQA_CHECKLIST: agreement(vqa_pairs),
            ADDITIONAL_VQA_CHECKLIST: agreement(additional_pairs),
            "combined": agreement(pairs),
            "basic_vqa_checklist": agreement(collapse_na(vqa_pairs)),
            "basic_combined": agreement(collapse_na(pairs)),
        },
        "kappa": kappas,
        "confusion": confusions,
        "detection": detection,
    }


def build_by_component(pairs: Sequence[Pair]) -> dict[str, Any]:
    """The same VQA-level block once per `component`, keyed in a stable order.

    Sorted by name rather than by size so the tables line up across sources, and because
    the naming already groups the families (ANNOTATION*, ATTRIBUTE*, ENTITY*).
    """
    grouped: dict[str, list[Pair]] = {}
    for pair in pairs:
        grouped.setdefault(pair.component, []).append(pair)
    return {
        component: build_vqa_level(grouped[component]) for component in sorted(grouped)
    }


def build_report(source: Source, pairs: list[Pair], coverage: Coverage) -> dict:
    vqa_level = build_vqa_level(pairs)
    # A case always has at least one vqa_checklist item, so the primary scope still
    # covers every case even though it ignores the additional checklist.
    case_level = {
        "primary": build_case_level(
            [p for p in pairs if p.checklist == VQA_CHECKLIST], CASE_SCOPES[0][1]
        ),
        "auxiliary": build_case_level(pairs, CASE_SCOPES[1][1]),
    }
    return {
        "name": source.label,
        "source": str(source.path),
        "ground_truth": [str(path) for path in source.gt_paths],
        "scored_checklists": list(source.checklists),
        "coverage": coverage.as_dict(),
        "counts": {
            "cases": case_level["primary"]["agreement"]["n"],
            **vqa_level["counts"],
        },
        "vqa_level": vqa_level,
        "vqa_level_by_component": build_by_component(pairs),
        "case_level": case_level,
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    rows = [[str(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(cells: Sequence[str]) -> str:
        return "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    separator = "| " + " | ".join("-" * width for width in widths) + " |"
    return "\n".join([line(headers), separator, *(line(row) for row in rows)])


def fmt(value: float | None, digits: int = 4) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def render_confusion(matrix: dict[str, Any]) -> str:
    labels = matrix["labels"]
    headers = ["GT \\ Pred", *labels, "row total"]
    rows = [
        [
            label,
            *(str(count) for count in matrix["matrix"][i]),
            str(matrix["row_totals"][i]),
        ]
        for i, label in enumerate(labels)
    ]
    rows.append(
        ["col total", *(str(total) for total in matrix["col_totals"]), str(matrix["n"])]
    )
    return render_table(headers, rows)


def render_coverage(coverage: dict[str, Any]) -> list[str]:
    skipped = coverage["skipped_cases"]
    unused = coverage["unused_prediction_cases"]
    coerced = coverage["na_coerced_to_fail"]

    out = [
        (
            f"- coverage: evaluated {coverage['evaluated_cases']}/"
            f"{coverage['gt_cases']} case(s), {len(skipped)} skipped"
        )
    ]
    if coerced["gt"] or coerced["pred"]:
        out.append(
            f"- NA coerced to FAIL on REQUIRED/additional items: "
            f"gt {coerced['gt']}, pred {coerced['pred']}"
        )
    if unused:
        out.append(
            f"- {len(unused)} prediction case(s) absent from the gt, ignored: "
            f"{', '.join(unused[:MAX_LISTED_CASES])}"
            + (" ..." if len(unused) > MAX_LISTED_CASES else "")
        )
    if skipped:
        out += ["", "Skipped cases (excluded from every metric):", ""]
        out.append(
            render_table(
                ["case_id", "reason"],
                [
                    [entry["case_id"], entry["reason"]]
                    for entry in skipped[:MAX_LISTED_CASES]
                ],
            )
        )
        if len(skipped) > MAX_LISTED_CASES:
            out.append(f"... and {len(skipped) - MAX_LISTED_CASES} more")
    return out


COMPONENT_KAPPA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("binary_combined", "binary"),
    ("ternary_optional", "ternary"),
    ("basic_combined", "basic"),
)
"""The three kappa slices worth breaking out per component.

The per-checklist slices are left to the json: split seven ways they get thin enough that
the kappa stops meaning much.
"""


def render_by_component(by_component: dict[str, Any]) -> list[str]:
    agreement_rows = []
    kappa_rows = []
    for component, level in by_component.items():
        stats = level["agreement"]
        agreement_rows.append(
            [
                component,
                level["counts"][VQA_CHECKLIST],
                level["counts"][ADDITIONAL_VQA_CHECKLIST],
                stats["combined"]["n"],
                fmt(stats["combined"]["accuracy"]),
                fmt(stats["basic_combined"]["accuracy"]),
                *(fmt(stats["combined"][key]) for key in ("precision", "recall", "f1")),
            ]
        )
        kappa_rows.append(
            [
                component,
                *(
                    cell
                    for slice_id, _ in COMPONENT_KAPPA_COLUMNS
                    for cell in (
                        level["kappa"][slice_id]["n"],
                        fmt(level["kappa"][slice_id]["kappa"]),
                    )
                ),
            ]
        )

    return [
        "### VQA-level agreement by component",
        "",
        render_table(
            [
                "component",
                VQA_CHECKLIST,
                ADDITIONAL_VQA_CHECKLIST,
                "N",
                "accuracy",
                "basic accuracy",
                "precision",
                "recall",
                "F1",
            ],
            agreement_rows,
        ),
        "",
        "### VQA-level Cohen's kappa by component",
        "",
        render_table(
            [
                "component",
                *(
                    header
                    for _, title in COMPONENT_KAPPA_COLUMNS
                    for header in (f"{title} N", f"{title} kappa")
                ),
            ],
            kappa_rows,
        ),
        "",
        (
            "`binary` is REQUIRED + additional items, `ternary` is the OPTIONAL ones "
            "scored PASS/FAIL/NA, `basic` is every item with NA folded into PASS."
        ),
    ]


def render_report(report: dict) -> str:
    counts = report["counts"]
    vqa_level = report["vqa_level"]
    case_level = report["case_level"]

    out = [
        f"## {report['name']}",
        "",
        f"- source: `{report['source']}`",
        "- ground truth: "
        + (
            ", ".join(f"`{path}`" for path in report["ground_truth"])
            or "the same file (self-contained)"
        ),
        "- scored checklists: " + ", ".join(report["scored_checklists"]),
        (
            f"- cases: {counts['cases']}, "
            f"{VQA_CHECKLIST}: {counts[VQA_CHECKLIST]}, "
            f"{ADDITIONAL_VQA_CHECKLIST}: {counts[ADDITIONAL_VQA_CHECKLIST]}, "
            f"total items: {counts['total']}"
        ),
        *render_coverage(report["coverage"]),
        "",
        "### VQA-level agreement",
        "",
        render_table(
            ["slice", "N", "agree", "accuracy", "precision", "recall", "F1"],
            [
                [
                    label,
                    stats["n"],
                    stats["agree"],
                    fmt(stats["accuracy"]),
                    fmt(stats["precision"]),
                    fmt(stats["recall"]),
                    fmt(stats["f1"]),
                ]
                for label, stats in (
                    (VQA_CHECKLIST, vqa_level["agreement"][VQA_CHECKLIST]),
                    (
                        ADDITIONAL_VQA_CHECKLIST,
                        vqa_level["agreement"][ADDITIONAL_VQA_CHECKLIST],
                    ),
                    ("combined", vqa_level["agreement"]["combined"]),
                    (
                        f"basic {VQA_CHECKLIST}",
                        vqa_level["agreement"]["basic_vqa_checklist"],
                    ),
                    ("basic combined", vqa_level["agreement"]["basic_combined"]),
                )
            ],
        ),
        "",
        "`basic` folds NA into PASS, scoring FAIL vs not-FAIL only.",
        "",
        "### VQA-level Cohen's kappa",
        "",
        render_table(
            ["slice", "classes", "N", "p_o", "p_e", "kappa"],
            [
                [
                    vqa_slice.title,
                    "/".join(vqa_slice.labels),
                    vqa_level["kappa"][vqa_slice.id]["n"],
                    fmt(vqa_level["kappa"][vqa_slice.id]["po"]),
                    fmt(vqa_level["kappa"][vqa_slice.id]["pe"]),
                    fmt(vqa_level["kappa"][vqa_slice.id]["kappa"]),
                ]
                for vqa_slice in VQA_SLICES
            ],
        ),
        "",
        "### VQA-level FAIL detection (FAIL is the positive class)",
        "",
        render_table(
            ["slice", "TP", "FP", "FN", "TN", "precision", "recall", "F1"],
            [
                [
                    vqa_slice.title,
                    *(
                        vqa_level["detection"][vqa_slice.id][key]
                        for key in ("tp", "fp", "fn", "tn")
                    ),
                    *(
                        fmt(vqa_level["detection"][vqa_slice.id][key])
                        for key in ("precision", "recall", "f1")
                    ),
                ]
                for vqa_slice in VQA_SLICES
            ],
        ),
        "",
        (
            "A true positive is the compared labeling calling FAIL on an item the reference "
            "labeling also marked FAIL, so recall is the share of real defects caught and "
            "precision the share of flagged items that are real defects. NA counts as "
            "negative, like PASS."
        ),
        "",
        "### VQA-level confusion matrices",
    ]

    for vqa_slice in VQA_SLICES:
        out += [
            "",
            f"**{vqa_slice.title}**",
            "",
            render_confusion(vqa_level["confusion"][vqa_slice.id]),
        ]

    out += ["", *render_by_component(report["vqa_level_by_component"])]

    out += [
        "",
        "### Case-level (a case FAILs when any of its VQA items FAILs)",
        "",
        render_table(
            ["metric", *(f"{key} ({scope})" for key, scope in CASE_SCOPES)],
            [
                [
                    metric,
                    *(
                        formatter(case_level[key][section][metric_key])
                        for key, _ in CASE_SCOPES
                    ),
                ]
                for metric, section, metric_key, formatter in (
                    ("N", "agreement", "n", str),
                    ("agree", "agreement", "agree", str),
                    ("accuracy", "agreement", "accuracy", fmt),
                    ("TP", "agreement", "tp", str),
                    ("FP", "agreement", "fp", str),
                    ("FN", "agreement", "fn", str),
                    ("TN", "agreement", "tn", str),
                    ("precision", "agreement", "precision", fmt),
                    ("recall", "agreement", "recall", fmt),
                    ("F1", "agreement", "f1", fmt),
                    ("p_o", "kappa", "po", fmt),
                    ("p_e", "kappa", "pe", fmt),
                    ("kappa", "kappa", "kappa", fmt),
                )
            ],
        ),
    ]

    for key, scope in CASE_SCOPES:
        out += [
            "",
            f"**{key} ({scope})**",
            "",
            render_confusion(case_level[key]["confusion"]),
        ]
    return "\n".join(out)


def render_comparison(reports: Sequence[dict]) -> str:
    headers = [
        "source",
        "VQA acc",
        "VQA precision",
        "VQA recall",
        "VQA F1",
        "kappa (binary)",
        "kappa (ternary)",
        "kappa (basic)",
        "case acc",
        "case recall",
        "case kappa",
    ]
    rows = [
        [
            report["name"],
            fmt(report["vqa_level"]["agreement"]["combined"]["accuracy"]),
            fmt(report["vqa_level"]["agreement"]["combined"]["precision"]),
            fmt(report["vqa_level"]["agreement"]["combined"]["recall"]),
            fmt(report["vqa_level"]["agreement"]["combined"]["f1"]),
            fmt(report["vqa_level"]["kappa"]["binary_combined"]["kappa"]),
            fmt(report["vqa_level"]["kappa"]["ternary_optional"]["kappa"]),
            fmt(report["vqa_level"]["kappa"]["basic_combined"]["kappa"]),
            fmt(report["case_level"]["primary"]["agreement"]["accuracy"]),
            fmt(report["case_level"]["primary"]["agreement"]["recall"]),
            fmt(report["case_level"]["primary"]["kappa"]["kappa"]),
        ]
        for report in reports
    ]
    return "\n".join(
        [
            "## Comparison",
            "",
            render_table(headers, rows),
            "",
            (
                f"case columns are the primary metric ({CASE_SCOPES[0][1]} only); "
                f"precision and recall take FAIL as the positive class."
            ),
        ]
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


PRED_FLAGS = "--pred"


class _AppendSource(argparse.Action):
    """Collects every ``--pred`` into one list, in command line order."""

    def __call__(self, parser, namespace, values, option_string=None):
        namespace.sources.append(Source(path=Path(str(values))))


class _NameLastSource(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if not namespace.sources:
            parser.error(f"--name must come after the {PRED_FLAGS} it names")
        namespace.sources[-1].name = str(values)


class _VqaOnlyLastSource(argparse.Action):
    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        if not namespace.sources:
            parser.error(f"--vqa-only must come after the {PRED_FLAGS} it applies to")
        namespace.sources[-1].vqa_only = True


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--gt",
        type=Path,
        action="append",
        metavar="PATH",
        help="reference labeling jsonl, repeatable "
        f"(default: {', '.join(p.name for p in DEFAULT_GT_PATHS)})",
    )
    parser.set_defaults(sources=[])
    parser.add_argument(
        "--pred",
        action=_AppendSource,
        metavar="PATH",
        help="labeling to compare, a judged_vqa_checklist_<language>_<slug>.jsonl dump, repeatable",
    )
    parser.add_argument(
        "--name",
        action=_NameLastSource,
        metavar="NAME",
        help=f"report name for the {PRED_FLAGS} it follows (default: the file stem)",
    )
    parser.add_argument(
        "--vqa-only",
        action=_VqaOnlyLastSource,
        help="score only vqa_checklist for the source it follows, for a run that never "
        "judged additional_vqa_checklist (otherwise every case is skipped as a "
        "length mismatch)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        metavar="PATH",
        help="where to write the json report (default: %(default)s)",
    )
    return parser.parse_args(argv)


def report_error(message: str) -> None:
    """Flush first so the message does not overtake whatever is queued on stdout."""
    sys.stdout.flush()
    print(f"[error] {message}", file=sys.stderr)


def report_problems(name: str, error: StrictError) -> None:
    sys.stdout.flush()
    print(f"[error] {name}: {len(error.problems)} problem(s)", file=sys.stderr)
    for problem in error.problems[:MAX_REPORTED_PROBLEMS]:
        print(f"  - {problem}", file=sys.stderr)
    if len(error.problems) > MAX_REPORTED_PROBLEMS:
        remaining = len(error.problems) - MAX_REPORTED_PROBLEMS
        print(f"  ... and {remaining} more", file=sys.stderr)


def warn_coverage(name: str, coverage: Coverage) -> None:
    """Say on stderr that the metrics are not over the full set, so a redirected report
    cannot hide it."""
    coerced = sum(coverage.na_coerced_to_fail.values())
    if not coverage.skipped_cases and not coerced:
        return
    sys.stdout.flush()
    notes = []
    if coverage.skipped_cases:
        notes.append(
            f"{len(coverage.skipped_cases)} of {coverage.gt_cases} case(s) skipped"
        )
    if coerced:
        notes.append(f"{coerced} NA judgment(s) counted as FAIL")
    print(f"[warn] {name}: {', '.join(notes)}", file=sys.stderr)


def score_source(
    source: Source,
    ground_truth: dict[str, Case],
) -> tuple[list[Pair], Coverage]:
    return pair_against_ground_truth(
        ground_truth, load_jsonl_cases(source.path), source.checklists
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    sources: list[Source] = args.sources
    if not sources:
        report_error(f"pass at least one {PRED_FLAGS} file")
        return 2

    gt_paths = list(args.gt) if args.gt else list(DEFAULT_GT_PATHS)
    try:
        ground_truth = load_ground_truth(gt_paths)
    except LoadError as error:
        report_error(str(error))
        return 1
    # Progress goes to stderr so stdout stays a clean markdown report.
    print(
        f"Loaded {len(ground_truth)} reference case(s) from "
        f"{', '.join(path.name for path in gt_paths)}",
        file=sys.stderr,
    )

    reports = []
    for source in sources:
        source.gt_paths = tuple(gt_paths)
        try:
            pairs, coverage = score_source(source, ground_truth)
        except LoadError as error:
            report_error(f"{source.label}: {error}")
            return 1
        except StrictError as error:
            report_problems(source.label, error)
            return 1
        warn_coverage(source.label, coverage)
        reports.append(build_report(source, pairs, coverage))

    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ground_truth": sorted(
            {str(path) for source in sources for path in source.gt_paths}
        ),
        "reports": reports,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print("\n\n".join(render_report(report) for report in reports))
    if len(reports) > 1:
        print()
        print(render_comparison(reports))
    sys.stdout.flush()
    print(f"Wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
