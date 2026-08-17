from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Mapping, Sequence

from domain.enums import ExecutionState
from domain.models import DecisionTrace, OpenExecutionDecision


@dataclass(frozen=True)
class DecisionDiff:
    path: str
    left: Any
    right: Any


@dataclass(frozen=True)
class ReplayDecisionSnapshot:
    final_decisions: Mapping[str, DecisionTrace]
    open_decisions: Mapping[str, OpenExecutionDecision]


@dataclass(frozen=True)
class DecisionDiffSummary:
    grade_changes: Sequence[str]
    added_cancels: Sequence[str]
    removed_cancels: Sequence[str]
    trigger_changes: Sequence[str]
    details: Sequence[DecisionDiff]


def diff_decisions(left: Any, right: Any, path: str = "decision") -> Sequence[DecisionDiff]:
    if is_dataclass(left):
        left = asdict(left)
    if is_dataclass(right):
        right = asdict(right)
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        diffs = []
        for key in sorted(set(left) | set(right)):
            diffs.extend(diff_decisions(left.get(key), right.get(key), f"{path}.{key}"))
        return tuple(diffs)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        diffs = []
        for index in range(max(len(left), len(right))):
            lval = left[index] if index < len(left) else None
            rval = right[index] if index < len(right) else None
            diffs.extend(diff_decisions(lval, rval, f"{path}[{index}]"))
        return tuple(diffs)
    if left != right:
        return (DecisionDiff(path, left, right),)
    return ()


def diff_replay_snapshots(left: ReplayDecisionSnapshot, right: ReplayDecisionSnapshot) -> DecisionDiffSummary:
    tickers = sorted(
        set(left.final_decisions)
        | set(right.final_decisions)
        | set(left.open_decisions)
        | set(right.open_decisions)
    )
    grade_changes = []
    added_cancels = []
    removed_cancels = []
    trigger_changes = []
    cancelled_states = {ExecutionState.CANCELLED, ExecutionState.HARD_CANCELLED}
    for ticker in tickers:
        left_final = left.final_decisions.get(ticker)
        right_final = right.final_decisions.get(ticker)
        left_grade = left_final.grade if left_final is not None else None
        right_grade = right_final.grade if right_final is not None else None
        if left_grade != right_grade:
            grade_changes.append(ticker)

        left_open = left.open_decisions.get(ticker)
        right_open = right.open_decisions.get(ticker)
        left_cancelled = left_open is not None and left_open.state in cancelled_states
        right_cancelled = right_open is not None and right_open.state in cancelled_states
        if right_cancelled and not left_cancelled:
            added_cancels.append(ticker)
        if left_cancelled and not right_cancelled:
            removed_cancels.append(ticker)
        left_trigger = None if left_open is None else (left_open.state, left_open.trigger_price)
        right_trigger = None if right_open is None else (right_open.state, right_open.trigger_price)
        if left_trigger != right_trigger:
            trigger_changes.append(ticker)
    return DecisionDiffSummary(
        tuple(grade_changes),
        tuple(added_cancels),
        tuple(removed_cancels),
        tuple(trigger_changes),
        diff_decisions(left, right, path="replay"),
    )
