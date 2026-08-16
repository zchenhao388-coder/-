from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class SetupType(StrEnum):
    ONE_TO_TWO = "ONE_TO_TWO"
    HIGH_BOARD = "HIGH_BOARD"
    WEAK_TO_STRONG = "WEAK_TO_STRONG"


class CandidateGrade(StrEnum):
    A1 = "A1"
    A2 = "A2"
    B_CONFIRMATION = "B_CONFIRMATION"
    B_DISAGREEMENT = "B_DISAGREEMENT"
    B_CONSENSUS_RISK = "B_CONSENSUS_RISK"
    B_HIGH_QUALITY = "B_HIGH_QUALITY"
    C_NIGHT_DEGRADED = "C_NIGHT_DEGRADED"
    C_AUCTION_EMERGENT = "C_AUCTION_EMERGENT"
    DROP = "DROP"


class ValidationState(StrEnum):
    UNVALIDATED = "UNVALIDATED"
    VALID = "VALID"
    PARTIAL = "PARTIAL"
    INVALID = "INVALID"
    HARD_INVALID = "HARD_INVALID"


class AuthenticityState(StrEnum):
    UNKNOWN = "UNKNOWN"
    AUTHENTIC = "AUTHENTIC"
    HEALTHY_DISAGREEMENT = "HEALTHY_DISAGREEMENT"
    SUSPICIOUS = "SUSPICIOUS"
    FAKE_STRONG = "FAKE_STRONG"


class ExecutionState(StrEnum):
    IDLE = "IDLE"
    ARMED = "ARMED"
    WAIT = "WAIT"
    EXECUTE = "EXECUTE"
    HARD_CANCELLED = "HARD_CANCELLED"
    EXPIRED = "EXPIRED"


class DataQualityState(StrEnum):
    GOOD = "GOOD"
    DEGRADED = "DEGRADED"
    BROKEN = "BROKEN"


class LeadershipState(StrEnum):
    LEADER = "LEADER"
    CORE = "CORE"
    FRONT_ROW = "FRONT_ROW"
    FOLLOWER = "FOLLOWER"
    UNKNOWN = "UNKNOWN"


class PermissionState(StrEnum):
    ALLOW = "ALLOW"
    REDUCE = "REDUCE"
    DENY = "DENY"


class ThresholdStatus(StrEnum):
    STRUCTURAL = "STRUCTURAL"
    SEED = "SEED"
    CALIBRATED = "CALIBRATED"
    RETIRED = "RETIRED"


class ReplayMode(StrEnum):
    FULL_SPEED = "FULL_SPEED"
    REAL_TIME = "REAL_TIME"
    STEP = "STEP"
    CHECKPOINT = "CHECKPOINT"


class FakeStrongFlag(StrEnum):
    PRE20_MIRAGE = "PRE20_MIRAGE"
    POST20_CONTINUOUS_DECAY = "POST20_CONTINUOUS_DECAY"
    PRICE_WITHOUT_LIQUIDITY = "PRICE_WITHOUT_LIQUIDITY"
    ISOLATED_STRENGTH = "ISOLATED_STRENGTH"
    LAST_SECOND_SPIKE = "LAST_SECOND_SPIKE"
