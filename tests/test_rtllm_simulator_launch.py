"""A simulator that never started must not be booked as a design failure.

Reproduced on native Windows: ``vvp.exe`` cannot resolve ``libvvp-1.dll`` unless the OSS CAD
Suite ``lib`` directory is on PATH (its ``environment.bat`` adds both ``bin`` and ``lib``;
adding only ``bin`` is not enough). The Windows loader then terminates the process with
STATUS_DLL_NOT_FOUND *before* it can write to stderr, so the harness saw an empty log and
labelled every design ``no_output`` -- and the sweep printed a clean ``reference: 0/50``,
which reads as "these designs are bad" rather than "your simulator is unusable".

The calibration gate in docs/rtllm_v2_session_handoff.md is what caught it (reference must be
47/50). These tests make the harness itself say so.
"""

from __future__ import annotations

import unittest

from c2hlsc_agent.rtllm_bench import (
    FAILURE_FAMILIES,
    SIMULATOR_LAUNCH_FAILURE_CODES,
    SimResult,
    classify_failure,
)

#: The exact signature observed on this machine.
STATUS_DLL_NOT_FOUND = 3221225781


def family(sim_log: str, rc: "int | None") -> "str | None":
    return classify_failure(
        compile_log="",
        sim_log=sim_log,
        syntax_pass=True,
        timed_out=False,
        runaway=False,
        sim_returncode=rc,
    )


class SimulatorLaunchFailureTests(unittest.TestCase):
    def test_family_is_registered(self):
        self.assertIn("simulator_launch_failed", FAILURE_FAMILIES)

    def test_windows_loader_abort_with_empty_log(self):
        """The real-world case: both streams empty, NTSTATUS exit code."""

        self.assertEqual(family("", STATUS_DLL_NOT_FOUND), "simulator_launch_failed")

    def test_every_registered_launch_code_is_detected(self):
        for rc in sorted(SIMULATOR_LAUNCH_FAILURE_CODES):
            with self.subTest(returncode=rc):
                self.assertEqual(family("", rc), "simulator_launch_failed")

    def test_launch_that_never_happened(self):
        """When Popen raises, ``_run`` puts its own reason on stderr; that is the evidence."""

        self.assertEqual(
            family("failed to launch vvp: [Errno 2] No such file or directory", None),
            "simulator_launch_failed",
        )

    def test_unrecorded_returncode_does_not_imply_a_launch_failure(self):
        """``None`` means "this caller recorded no returncode", NOT "the launch failed".

        Conflating the two would relabel every legacy empty-log record -- and it broke
        tests.test_rtllm_bench.ClassifyFailureTests.test_no_output when first written this way.
        """

        self.assertEqual(family("", None), "no_output")

    def test_posix_loader_message(self):
        log = "vvp: error while loading shared libraries: libvvp-1.dll: cannot open shared object file"
        self.assertEqual(family(log, 127), "simulator_launch_failed")

    def test_genuine_no_output_is_still_no_output(self):
        """A simulator that ran cleanly and printed nothing is a DESIGN verdict. Unchanged."""

        self.assertEqual(family("", 0), "no_output")

    def test_design_output_is_never_relabelled(self):
        """If the design printed something, the simulator plainly ran -- keep the real verdict."""

        self.assertEqual(
            family("Failed at time 40: out=8 expected=4\n", STATUS_DLL_NOT_FOUND),
            "functional_mismatch",
        )

    def test_pass_banner_still_passes(self):
        self.assertIsNone(family("===========Your Design Passed===========\n", 0))


class ReproducibilityTests(unittest.TestCase):
    """classify_failure must stay a pure function of the STORED fields."""

    def test_simresult_carries_the_returncode(self):
        result = SimResult(
            design="d",
            syntax_pass=True,
            func_pass=False,
            func_pass_strict=False,
            timed_out=False,
            compile_log="",
            sim_log="",
            duration_s=0.0,
            failure_family="simulator_launch_failed",
            sim_returncode=STATUS_DLL_NOT_FOUND,
        )
        self.assertEqual(result.sim_returncode, STATUS_DLL_NOT_FOUND)
        # Replaying classification off the stored record reproduces the stored label.
        self.assertEqual(
            classify_failure(
                result.compile_log,
                result.sim_log,
                result.syntax_pass,
                result.timed_out,
                result.runaway_output,
                sim_returncode=result.sim_returncode,
            ),
            result.failure_family,
        )

    def test_to_dict_carries_the_returncode(self):
        """It must survive serialization, or a stored simulator_launch_failed row is
        indistinguishable from a no_output row -- the distinguishing evidence is a loader exit
        code with both streams empty, which an empty sim_log by definition cannot carry."""

        result = SimResult(
            design="d",
            syntax_pass=True,
            func_pass=False,
            func_pass_strict=False,
            timed_out=False,
            compile_log="",
            sim_log="",
            duration_s=0.0,
            failure_family="simulator_launch_failed",
            sim_returncode=STATUS_DLL_NOT_FOUND,
        )
        payload = result.to_dict()
        self.assertIn("sim_returncode", payload)
        self.assertEqual(payload["sim_returncode"], STATUS_DLL_NOT_FOUND)
        # Replay straight off the serialized dict.
        self.assertEqual(
            classify_failure(
                payload["compile_log"],
                payload["sim_log"],
                payload["syntax_pass"],
                payload["timed_out"],
                payload["runaway_output"],
                sim_returncode=payload["sim_returncode"],
            ),
            payload["failure_family"],
        )

    def test_default_returncode_preserves_legacy_behaviour(self):
        """Older records have no returncode; an empty log must not become a launch failure
        just because the field defaults to None -- so callers that omit it keep no_output."""

        self.assertEqual(SimResult.__dataclass_fields__["sim_returncode"].default, None)


if __name__ == "__main__":
    unittest.main()
