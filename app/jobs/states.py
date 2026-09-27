"""Job state machine."""
from __future__ import annotations

from enum import Enum
from typing import Dict, Set


class JobState(str, Enum):
    DISCOVERED = "DISCOVERED"
    WAITING_FOR_PAIR = "WAITING_FOR_PAIR"
    VALIDATING = "VALIDATING"
    GENERATING_METADATA = "GENERATING_METADATA"
    #: legacy: produced by older versions that generated covers. Kept so old
    #: databases still load; never entered by the current pipeline.
    GENERATING_IMAGE = "GENERATING_IMAGE"
    BUILDING_VIDEO = "BUILDING_VIDEO"
    VALIDATING_VIDEO = "VALIDATING_VIDEO"
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRY_PENDING = "RETRY_PENDING"
    DUPLICATE = "DUPLICATE"


TERMINAL: Set[JobState] = {JobState.COMPLETED, JobState.FAILED, JobState.DUPLICATE}

ALLOWED: Dict[JobState, Set[JobState]] = {
    JobState.DISCOVERED: {JobState.WAITING_FOR_PAIR, JobState.VALIDATING, JobState.FAILED,
                          JobState.DUPLICATE},
    JobState.WAITING_FOR_PAIR: {JobState.VALIDATING, JobState.FAILED, JobState.DUPLICATE,
                                JobState.WAITING_FOR_PAIR},
    JobState.VALIDATING: {JobState.GENERATING_METADATA, JobState.FAILED, JobState.RETRY_PENDING,
                          JobState.DUPLICATE},
    JobState.GENERATING_METADATA: {JobState.BUILDING_VIDEO, JobState.FAILED,
                                   JobState.RETRY_PENDING},
    # legacy state, only reachable when loading an old database
    JobState.GENERATING_IMAGE: {JobState.BUILDING_VIDEO, JobState.FAILED, JobState.RETRY_PENDING},
    JobState.BUILDING_VIDEO: {JobState.VALIDATING_VIDEO, JobState.FAILED, JobState.RETRY_PENDING},
    JobState.VALIDATING_VIDEO: {JobState.UPLOADING, JobState.COMPLETED, JobState.FAILED,
                                JobState.RETRY_PENDING},
    JobState.UPLOADING: {JobState.UPLOADED, JobState.FAILED, JobState.RETRY_PENDING},
    JobState.UPLOADED: {JobState.COMPLETED, JobState.FAILED},
    JobState.RETRY_PENDING: {JobState.VALIDATING, JobState.GENERATING_METADATA,
                             JobState.BUILDING_VIDEO,
                             JobState.VALIDATING_VIDEO, JobState.UPLOADING, JobState.FAILED},
    JobState.FAILED: {JobState.RETRY_PENDING, JobState.VALIDATING},
    JobState.COMPLETED: {JobState.RETRY_PENDING, JobState.VALIDATING},
    JobState.DUPLICATE: {JobState.VALIDATING, JobState.RETRY_PENDING},
}


class InvalidTransition(RuntimeError):
    pass


def can_transition(src: JobState, dst: JobState) -> bool:
    if src == dst:
        return True
    return dst in ALLOWED.get(src, set())


def assert_transition(src: JobState, dst: JobState) -> None:
    if not can_transition(src, dst):
        raise InvalidTransition(f"illegal job transition {src.value} -> {dst.value}")


#: non-terminal states that indicate work was interrupted (crash, reboot,
#: Ctrl+C). On startup these jobs are requeued instead of being lost.
INTERRUPTED: Set[JobState] = {
    JobState.VALIDATING, JobState.GENERATING_METADATA, JobState.GENERATING_IMAGE,
    JobState.BUILDING_VIDEO, JobState.VALIDATING_VIDEO, JobState.UPLOADING,
}
