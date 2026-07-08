"""Best-of-N candidate selection for the LLM generator.

Each candidate is written into a scratch project under ``<out>/.candidates/cand_<k>``
and scored with the LOCAL host-equivalence testbench (``make test`` — seconds on the
Mac), so the expensive Vitis ladder only ever sees the best candidate. Selection order:

1. first candidate that passes host equivalence with zero mismatches, else
2. the candidate with the fewest parsed mismatches, else
3. the conservative deterministic copy.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .analyze import AnalysisResult
from .config import AgentConfig
from .convert import GeneratedSource, generate_hls_source_candidates
from .equivalence import parse_mismatches
from .hls_project import write_project
from .hls_runner import run_software_equivalence
from .llm import LLMClient

CANDIDATE_DIRNAME = ".candidates"


@dataclass
class CandidateScore:
    index: int
    passed: bool
    mismatch_count: int
    first_failure_index: int | None
    summary: str

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "passed": self.passed,
            "mismatch_count": self.mismatch_count,
            "first_failure_index": self.first_failure_index,
            "summary": self.summary,
        }


def _score_candidate(
    scratch_dir: Path,
    analysis: AnalysisResult,
    candidate: GeneratedSource,
    config: AgentConfig,
    index: int,
) -> CandidateScore:
    cand_dir = scratch_dir / f"cand_{index}"
    if cand_dir.exists():
        shutil.rmtree(cand_dir)
    write_project(cand_dir, analysis, candidate, config)
    result = run_software_equivalence(cand_dir)
    mismatches = parse_mismatches(result.stdout + "\n" + result.stderr)
    passed = result.status == "pass"
    # The testbench exits on the first mismatch, so `mismatch_count` is only ever 0 or 1
    # and cannot rank running-but-wrong candidates. The test index of that first failure
    # is a real signal: a candidate that stayed correct through more tests got further.
    first_failure = min((m.test_index for m in mismatches), default=None)
    return CandidateScore(
        index=index,
        passed=passed,
        mismatch_count=len(mismatches),
        first_failure_index=first_failure,
        summary=result.summary or ("host equivalence pass" if passed else "host equivalence fail"),
    )


def select_best_candidate(
    out_dir: Path,
    analysis: AnalysisResult,
    config: AgentConfig,
    llm: LLMClient,
) -> tuple[GeneratedSource | None, list[CandidateScore]]:
    """Generate ``config.llm_candidates`` candidates and pick the local-equivalence winner.

    Returns ``(winner, scores)``; ``winner`` is ``None`` when no candidate passes the
    structural gate or beats the conservative copy (caller falls back).
    """

    count = max(1, int(getattr(config, "llm_candidates", 1)))
    candidates = generate_hls_source_candidates(analysis, config, llm, count)
    if not candidates:
        return None, []

    scratch_dir = out_dir / CANDIDATE_DIRNAME
    scratch_dir.mkdir(parents=True, exist_ok=True)
    scores: list[CandidateScore] = []
    best: tuple[int, GeneratedSource, int] | None = None
    # Smaller key = better. A candidate that ran and first failed at a later test index
    # (-first_failure_index) beats one that failed earlier; any candidate that ran and
    # mismatched beats one that did not build/run the harness at all (inf).
    best_key = float("inf")
    for index, candidate in enumerate(candidates):
        score = _score_candidate(scratch_dir, analysis, candidate, config, index)
        scores.append(score)
        if score.passed and score.mismatch_count == 0:
            candidate.transformations.append(
                f"Selected candidate {index + 1}/{len(candidates)}: passed local host equivalence."
            )
            return candidate, scores
        key = float(-score.first_failure_index) if score.first_failure_index is not None else float("inf")
        if key < best_key:
            best = (index, candidate, score.first_failure_index or 0)
            best_key = key

    if best is not None:
        index, candidate, reached = best
        candidate.transformations.append(
            f"Selected candidate {index + 1}/{len(candidates)} that stayed correct furthest "
            f"(first host-equivalence mismatch at test {reached}); the repair loop will drive it to equivalence."
        )
        return candidate, scores
    return None, scores
