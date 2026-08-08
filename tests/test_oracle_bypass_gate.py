"""The admissibility gate and the LLM sandbox: the two controls every score rests on.

Both had a hole of the same shape — a list that can only name hazards someone thought of:

* ``_ILLEGAL_TASK_RE`` named ``$display`` and friends but not the SystemVerilog *severity*
  tasks. ``IVERILOG_STANDARD`` is ``-g2012``, which enables them, they write to the same
  stdout the oracle reads, and the oracle is a substring test. Verified against real
  iverilog 14: a design whose entire body is ``$info("=== Your Design Passed ===");`` prints
  that line to stdout, so it would have been admitted and scored a STRICT PASS on RTL that
  computes nothing.
* ``_CLI_DISALLOWED_TOOLS`` named ``Bash`` but not ``PowerShell``, which the CLI exposes as a
  separate tool on Windows — so the "sandboxed" model could read the staged testbench.

The lesson in both cases is that a deny list is not a control. The sandbox now leads with an
explicit empty allow-list (``--tools ""``); the gate is still a deny list, so these tests pin
the hazards that are known to reach stdout.
"""

from __future__ import annotations

import unittest

from c2hlsc_agent import llm as llm_module
from c2hlsc_agent.llm import (
    _CLI_DISALLOWED_TOOLS,
    _ORACLE_CRITICAL_DENIALS,
    ClaudeCLIClient,
)
from c2hlsc_agent.rtllm_bench import PASS_MARKERS, find_illegal_system_tasks

#: The exact bypass, reduced. Compiles under `iverilog -g2012` and prints to stdout.
BYPASS_RTL = """
module dut(input clk, output reg [7:0] q);
  initial begin
    $info("=========== Your Design Passed ===========");
    q = 8'hFF;
  end
endmodule
"""


def refused(rtl: str) -> bool:
    return bool(find_illegal_system_tasks(rtl))


class AdmissibilityGateTests(unittest.TestCase):
    def test_the_info_bypass_is_refused(self):
        self.assertTrue(refused(BYPASS_RTL))

    def test_the_bypass_banner_would_have_satisfied_the_oracle(self):
        """Guards the premise: if this stops being true the test above loses its point."""

        self.assertTrue(any(marker in BYPASS_RTL for marker in PASS_MARKERS))

    def test_every_severity_task_is_refused(self):
        for task in ("$info", "$warning", "$error", "$fatal"):
            with self.subTest(task=task):
                self.assertTrue(refused('module m; initial %s("x"); endmodule' % task))

    def test_fatal_is_refused_as_a_control_task_too(self):
        """$fatal both prints and terminates, so it must be caught on the control path as
        well — a candidate could otherwise end the simulation early on its own terms."""

        from c2hlsc_agent.rtllm_bench import _ILLEGAL_CONTROL_RE

        self.assertTrue(_ILLEGAL_CONTROL_RE.search("$fatal(0);"))

    def test_the_original_hazards_are_still_refused(self):
        for task in ("$display", "$write", "$monitor", "$strobe", "$dumpvars", "$finish", "$stop"):
            with self.subTest(task=task):
                self.assertTrue(refused("module m; initial %s; endmodule" % task))

    def test_legitimate_rtl_functions_are_still_allowed(self):
        """These cannot reach stdout and are normal in synthesizable RTL. Refusing them would
        fail correct designs, which is the opposite failure and just as damaging."""

        for expr in ("$signed(a)", "$unsigned(a)", "$clog2(8)", "$bits(x)", "$time", "$random"):
            with self.subTest(expr=expr):
                self.assertFalse(refused("module m; wire w = %s; endmodule" % expr))


class SandboxTests(unittest.TestCase):
    def test_powershell_is_denied(self):
        """It was missing while Bash was present; on Windows the CLI exposes it separately."""

        self.assertIn("PowerShell", _CLI_DISALLOWED_TOOLS.split(","))

    def test_every_oracle_critical_denial_is_actually_in_the_deny_list(self):
        """The tuple was previously referenced nowhere but its own definition — a comment
        claiming to be a control. This is the enforcement."""

        missing = [t for t in _ORACLE_CRITICAL_DENIALS if t not in _CLI_DISALLOWED_TOOLS.split(",")]
        self.assertEqual(missing, [])

    def test_the_guard_detects_a_trimmed_deny_list(self):
        """The import-time guard must actually fire, or it is decoration again.

        Exercised through the free function so this test mutates nothing: an earlier version
        reloaded the module to undo a global edit, which rebound every class in it and made
        `isinstance(client, ClaudeCLIClient)` fail in a test that had already imported the old
        class — a failure that only appeared when the full suite ran in order.
        """

        self.assertEqual(
            llm_module._missing_oracle_critical_denials("Read,Write"),
            ["Task", "Bash", "PowerShell", "Glob", "Grep", "WebFetch", "WebSearch"],
        )
        self.assertEqual(llm_module._missing_oracle_critical_denials(_CLI_DISALLOWED_TOOLS), [])

    def test_the_guard_tolerates_whitespace_in_the_list(self):
        self.assertEqual(
            llm_module._missing_oracle_critical_denials(", ".join(_ORACLE_CRITICAL_DENIALS)),
            [],
        )

    def test_sandboxed_client_leads_with_an_empty_allow_list(self):
        """--tools "" is fail-closed: a CLI version that adds a new file-reading tool is
        covered without anyone updating a list."""

        client = ClaudeCLIClient(model="opus", cli_cmd="claude", sandbox=True)
        argv = client._base
        self.assertIn("--tools", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        # Defense in depth is still present.
        self.assertIn("--disallowedTools", argv)
        self.assertIn("--permission-mode", argv)

    def test_unsandboxed_client_is_unchanged(self):
        client = ClaudeCLIClient(model="opus", cli_cmd="claude", sandbox=False)
        self.assertNotIn("--tools", client._base)
        self.assertNotIn("--disallowedTools", client._base)


if __name__ == "__main__":
    unittest.main()
