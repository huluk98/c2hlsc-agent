"""The repaired DUT must never delegate to the macro-included golden oracle.

`_repair_missing_original_support` injects `#define <top> <top>_c2hlsc_repair_reference`
plus `#include "../input.c"` so a preserved top body can call the original HELPER
functions. Those helpers keep their own names -- the alias is only the renamed top. A
candidate that CALLS the alias passes host equivalence, CSim, CSynth and CoSim by
construction, because the DUT is then literally the oracle, and the ladder cannot detect
it. These tests pin the gate that refuses such a candidate.

Two of the cases below are bypasses that defeated an earlier attempt at this guard: a
heuristic that skipped "comment-looking" lines beginning with `*`, and a whitelist that
accepted any preprocessor line mentioning the alias. Keep them.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent import hlsc_repair_agent as hra  # noqa: E402
from c2hlsc_agent.analyze import analyze_source  # noqa: E402
from c2hlsc_agent.config import AgentConfig, ArgumentConfig  # noqa: E402

TOP = "top"
ALIAS = f"{TOP}_c2hlsc_repair_reference"
GOLDEN = (
    "#include <stdint.h>\n"
    "static int helper_scale(int v) { return v * 3; }\n"
    f"void {TOP}(const int *a, int *out, int n) "
    "{ for (int i = 0; i < n; ++i) out[i] = helper_scale(a[i]); }\n"
)


def _with_support(body: str) -> str:
    return '#include "hls_top.hpp"\n' + hra._support_include_block(TOP) + "\n" + body


class ReferenceOracleViolationTests(unittest.TestCase):
    def test_support_block_alone_is_legitimate(self):
        # The injected block is why the alias exists at all; it must not self-trip.
        cand = _with_support(f"void {TOP}(const int *a, int *out, int n) {{ out[0] = a[0]; }}")
        self.assertEqual(hra._reference_oracle_violation(cand, TOP), "")

    def test_calling_an_original_helper_is_legitimate(self):
        # This is the whole point of the support block: helpers keep their own names.
        cand = _with_support(
            f"void {TOP}(const int *a, int *out, int n) "
            "{ for (int i = 0; i < n; ++i) out[i] = helper_scale(a[i]); }"
        )
        self.assertEqual(hra._reference_oracle_violation(cand, TOP), "")

    def test_no_support_block_is_legitimate(self):
        cand = f'#include "hls_top.hpp"\nvoid {TOP}(const int *a, int *out, int n) {{ out[0] = a[0]; }}\n'
        self.assertEqual(hra._reference_oracle_violation(cand, TOP), "")

    def test_plain_delegation_is_rejected(self):
        cand = _with_support(f"void {TOP}(const int *a, int *out, int n) {{ {ALIAS}(a, out, n); }}")
        self.assertIn(ALIAS, hra._reference_oracle_violation(cand, TOP))

    def test_star_prefixed_line_does_not_bypass(self):
        # BYPASS: an earlier guard skipped lines whose first character is '*', treating
        # them as block-comment continuations. A delegation shaped like one slipped past.
        cand = _with_support(
            f"void {TOP}(const int *a, int *out, int n) {{\n"
            f"  *out = ({ALIAS}(a, out, n), *out);\n"
            "}"
        )
        self.assertIn(ALIAS, hra._reference_oracle_violation(cand, TOP))

    def test_extra_define_of_the_alias_does_not_bypass(self):
        # BYPASS: an earlier guard whitelisted ANY #define/#undef/#ifdef mentioning the
        # alias, not only the support block's own rename directive.
        cand = _with_support(
            f"#define GO {ALIAS}\n"
            f"void {TOP}(const int *a, int *out, int n) {{ GO(a, out, n); }}"
        )
        self.assertIn(ALIAS, hra._reference_oracle_violation(cand, TOP))

    def test_ifdef_of_the_alias_does_not_bypass(self):
        cand = _with_support(
            f"#ifdef {ALIAS}\n#endif\n"
            f"void {TOP}(const int *a, int *out, int n) {{ {ALIAS}(a, out, n); }}"
        )
        self.assertIn(ALIAS, hra._reference_oracle_violation(cand, TOP))

    def test_function_pointer_to_the_alias_is_rejected(self):
        cand = _with_support(
            f"void {TOP}(const int *a, int *out, int n) {{ "
            f"void (*f)(const int *, int *, int) = {ALIAS}; f(a, out, n); }}"
        )
        self.assertIn(ALIAS, hra._reference_oracle_violation(cand, TOP))

    def test_raw_oracle_include_without_the_block_is_rejected(self):
        cand = (
            '#include "hls_top.hpp"\n#include "../input.c"\n'
            f"void {TOP}(const int *a, int *out, int n) {{ out[0] = a[0]; }}\n"
        )
        self.assertIn("outside the repair support block", hra._reference_oracle_violation(cand, TOP))

    def test_duplicate_oracle_include_is_rejected(self):
        cand = _with_support(
            '#include "../input.c"\n'
            f"void {TOP}(const int *a, int *out, int n) {{ out[0] = a[0]; }}"
        )
        self.assertIn("2 times", hra._reference_oracle_violation(cand, TOP))


class _CannedLLM:
    model = "fake"

    def __init__(self, candidate: str) -> None:
        self.candidate = candidate

    def complete(self, system: str, user: str, *, max_tokens: int = 8000) -> str:
        return "```cpp\n" + self.candidate + "```"


class LlmRepairRefusesDelegationTests(unittest.TestCase):
    def _project(self, tmp: Path) -> object:
        (tmp / "src").mkdir()
        (tmp / "input.c").write_text(GOLDEN, encoding="utf-8")
        (tmp / "src" / "hls_top.cpp").write_text(self.original, encoding="utf-8")
        (tmp / "src" / "hls_top.hpp").write_text(
            f"#pragma once\nvoid {TOP}(const int *, int *, int);\n", encoding="utf-8"
        )
        config = AgentConfig(
            top=TOP,
            arguments={"a": ArgumentConfig(length=8), "out": ArgumentConfig(length=8)},
        )
        return analyze_source(tmp / "input.c", TOP, config), config

    original = f'#include "hls_top.hpp"\nvoid {TOP}(const int *a, int *out, int n) {{ (void)a; (void)out; (void)n; }}\n'

    def test_delegating_candidate_is_not_written_to_disk(self):
        candidate = _with_support(
            f"void {TOP}(const int *a, int *out, int n) {{ {ALIAS}(a, out, n); }}"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis, config = self._project(root)
            changes, oscillated = hra._llm_repair(
                root, analysis, type("D", (), {"family": "x"})(), "csim", "evidence",
                _CannedLLM(candidate), config,
            )
            self.assertEqual(changes, [])
            self.assertFalse(oscillated)
            # The refusal must leave the previous source byte-identical.
            self.assertEqual((root / "src" / "hls_top.cpp").read_text(encoding="utf-8"), self.original)

    def test_legitimate_candidate_is_still_accepted(self):
        candidate = _with_support(
            f"void {TOP}(const int *a, int *out, int n) "
            "{ for (int i = 0; i < n; ++i) out[i] = helper_scale(a[i]); }"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            analysis, config = self._project(root)
            changes, _ = hra._llm_repair(
                root, analysis, type("D", (), {"family": "x"})(), "csim", "evidence",
                _CannedLLM(candidate), config,
            )
            self.assertTrue(changes, "the guard must not block a normal helper-calling repair")
            self.assertIn("helper_scale", (root / "src" / "hls_top.cpp").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
