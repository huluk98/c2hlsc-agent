from __future__ import annotations

from dataclasses import dataclass


COSIM_FAILURE_MARKERS = (
    'co-simulation finished: fail',
    'cosim design failed',
    'co-simulation failed',
    'aborting cosim',
)


@dataclass(frozen=True)
class CosimVerdict:
    status: str
    reason: str = ''
    failure_marker: str | None = None


def evaluate_cosim_verdict(status: str, evidence: str) -> CosimVerdict:
    '''Apply the authoritative CoSim log gate to an initial process status.

    Vitis can return zero while its log contains an explicit failing verdict.
    Only a process-level pass is eligible for this downgrade; existing timeout,
    failure, blocked, and skipped states remain unchanged.
    '''

    normalized_status = str(status).lower()
    if normalized_status != 'pass':
        return CosimVerdict(normalized_status)

    haystack = (evidence or '').lower()
    marker = next(
        (candidate for candidate in COSIM_FAILURE_MARKERS if candidate in haystack),
        None,
    )
    if marker is None:
        return CosimVerdict('pass')
    return CosimVerdict(
        'fail',
        'Vitis exited 0 but the CoSim log reports a co-simulation failure',
        marker,
    )
