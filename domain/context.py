import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from domain.enums import SetupType
from domain.models import NightPlan


CONTEXT_SCHEMA = "NIGHT_PLAN_CONTEXT_V1"


def _canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("NightPlan timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_night_plan_identity_payload(night_plan: NightPlan) -> Mapping[str, Any]:
    """Canonical strategy identity; excludes non-strategy generation metadata."""
    candidate_pool = sorted({ticker.upper() for ticker in night_plan.candidate_pool})
    setups = sorted(
        (ticker.upper(), setup.value)
        for ticker, setup in night_plan.setup_by_ticker.items()
    )
    return {
        "schema": CONTEXT_SCHEMA,
        "trade_date": night_plan.trade_date,
        "information_available_at": _canonical_timestamp(night_plan.information_available_at),
        "candidate_pool": candidate_pool,
        "setups": setups,
    }


def night_plan_context_id(night_plan: NightPlan) -> str:
    canonical = json.dumps(
        canonical_night_plan_identity_payload(night_plan),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def serialize_night_plan(night_plan: NightPlan) -> str:
    payload = {
        **canonical_night_plan_identity_payload(night_plan),
        "generated_at": _canonical_timestamp(night_plan.generated_at),
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def deserialize_night_plan(payload: str) -> NightPlan:
    raw = json.loads(payload)
    if raw.get("schema") != CONTEXT_SCHEMA:
        raise ValueError("unsupported NightPlan context schema")
    candidate_pool = tuple(raw["candidate_pool"])
    setup_by_ticker = {ticker: SetupType(setup) for ticker, setup in raw["setups"]}
    return NightPlan(
        trade_date=raw["trade_date"],
        candidate_pool=candidate_pool,
        setup_by_ticker=setup_by_ticker,
        generated_at=_parse_timestamp(raw["generated_at"]),
        information_available_at=_parse_timestamp(raw["information_available_at"]),
    )
