from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class DecisionDiff:
    path: str
    left: Any
    right: Any


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
