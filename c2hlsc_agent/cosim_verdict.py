from __future__ import annotations

from dataclasses import dataclass


COSIM_FAILURE_MARKERS = (
    'co-simulation finished: fail',
    'cosim design failed',
    'co-simulation failed',
    'aborting cosim',
)

#: An explicit statement that the co-simulation ran and agreed. Vitis writes
#: 'C/RTL co-simulation finished: PASS', of which the first entry is a substring.
COSIM_SUCCESS_MARKERS = (
    'co-simulation finished: pass',
    'cosim design passed',
    'co-simulation passed',
)


@dataclass(frozen=True)
class CosimVerdict:
    status: str
    reason: str = ''
    failure_marker: str | None = None


def evaluate_cosim_verdict(status: str, evidence: str) -> CosimVerdict:
    '''Apply the authoritative CoSim log gate to an initial process status.

    Vitis can return zero while its log contains an explicit failing verdict, and it can
    also return zero having produced no verdict at all -- a co-simulation that never ran,
    ran zero transactions, or whose log was truncated. A pass therefore requires the log to
    SAY it passed; the absence of a failure marker is not evidence of success, which is the
    same rule the host tiers apply by counting what they compared.

    An unrecognised log yields 'blocked', never 'pass' and never 'fail': the design has not
    been judged, so claiming equivalence would be false and blaming the design would be
    unfair. Only a process-level pass is eligible for this downgrade; existing timeout,
    failure, blocked, and skipped states are returned unchanged.
    '''

    normalized_status = str(status).lower()
    if normalized_status != 'pass':
        return CosimVerdict(normalized_status)

    haystack = (evidence or '').lower()
    marker = next(
        (candidate for candidate in COSIM_FAILURE_MARKERS if candidate in haystack),
        None,
    )
    if marker is not None:
        return CosimVerdict(
            'fail',
            'Vitis exited 0 but the CoSim log reports a co-simulation failure',
            marker,
        )
    if any(candidate in haystack for candidate in COSIM_SUCCESS_MARKERS):
        return CosimVerdict('pass')
    return CosimVerdict(
        'blocked',
        'Vitis exited 0 but the CoSim log carries no verdict, so the co-simulation did '
        'not judge this design (it may not have run, or the log may be truncated)',
    )
