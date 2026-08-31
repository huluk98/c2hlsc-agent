"""Offline tests for the CHStone harness.

Everything here is hermetic: ``subprocess.run`` is replaced per test, so no gcc, no
``vitis_hls``, no LLM and no CHStone checkout are required. The benchmark trees are tiny
synthetic fixtures that reproduce the three shapes that actually matter -- a top file that
is *not* ``<dir>/<dir>.c``, CHStone's K&R return-type-on-its-own-line ``main`` layout, and a
top that ``#include``s sibling sources. The driver lives in ``scripts/`` rather than the
package, so it is loaded from its path the way ``tests/test_run_rtllm_v2.py`` loads its own.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "run_chstone.py"
sys.path.insert(0, str(SCRIPTS_DIR))
spec = importlib.util.spec_from_file_location("run_chstone", SCRIPT_PATH)
assert spec and spec.loader
driver = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = driver
spec.loader.exec_module(driver)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

#: CHStone declares every top this way: the return type sits on its own line, and the
#: definition line starts with the identifier. Anything that prepends a return type turns
#: this into ``int int``.
KNR_TOP = """\
#include <stdio.h>
{includes}
int
main ()
{{
  int main_result;
  main_result = {result};
  {helper}
  printf ("%d\\n", main_result);
  return main_result;
}}
"""

TCL = """\
open_project {name}_syn
source ../config.tcl

add_files -tb ../common/tb.c
add_files {top} -cflags "-Dmain=chstone_main"

set_top chstone_main

open_solution -reset solution
"""


def make_top_source(*, includes: str = "", result: str = "0", helper: str = "") -> str:
    return KNR_TOP.format(includes=includes, result=result, helper=helper)


def make_benchmark(
    root: Path,
    name: str,
    top: str,
    *,
    siblings: "tuple[str, ...]" = (),
    decoys: "tuple[str, ...]" = (),
    tcl_body: "str | None" = None,
    source: "str | None" = None,
) -> Path:
    """One CHStone-shaped benchmark directory: hls.tcl + a K&R top + its siblings."""

    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "hls.tcl").write_text(
        tcl_body if tcl_body is not None else TCL.format(name=name, top=top), encoding="utf-8"
    )
    includes = "".join(f'#include "{sibling}"\n' for sibling in siblings)
    (directory / top).write_text(source or make_top_source(includes=includes), encoding="utf-8")
    for sibling in siblings:
        (directory / sibling).write_text(f"/* {sibling} helper */\nint {name}_helper (void) {{ return 0; }}\n",
                                         encoding="utf-8")
    for decoy in decoys:
        # A real file at the path a "<dir>/<dir>.c" guess would produce. Discovery must not
        # pick it: only hls.tcl knows which source the suite considers the kernel.
        (directory / decoy).write_text("/* NOT the top: nothing sets this as the kernel */\n",
                                       encoding="utf-8")
    return directory


#: The four top-file shapes the real suite ships, including the three that are not
#: ``<dir>/<dir>.c``.
SUITE = {
    "adpcm": "adpcm.c",
    "jpeg": "main.c",
    "motion": "mpeg2.c",
    "sha": "sha_driver.c",
}


def make_suite(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "common").mkdir(exist_ok=True)
    (root / "common" / "tb.c").write_text("int main () { return chstone_main (); }\n", encoding="utf-8")
    (root / "config.tcl").write_text("set part_name xczu3eg\nset period 10\n", encoding="utf-8")
    make_benchmark(root, "adpcm", "adpcm.c")
    make_benchmark(root, "jpeg", "main.c", siblings=("jpeg2bmp.c",), decoys=("jpeg.c",))
    make_benchmark(root, "motion", "mpeg2.c", decoys=("motion.c",))
    make_benchmark(root, "sha", "sha_driver.c", siblings=("sha.c",), decoys=())
    return root


def completed(cmd: "list[str]", returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)


def run_main(argv: "list[str]") -> "tuple[int, str]":
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = driver.main(argv)
    return code, buffer.getvalue()


def read_jsonl(path: Path) -> "list[dict[str, Any]]":
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TempTreeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.bench = make_suite(self.tmp / "CHStone")
        self.out = self.tmp / "out"
        self.addCleanup(self._tmp.cleanup)


# --------------------------------------------------------------------------- #
# discovery: the top file comes from hls.tcl, never from the directory name
# --------------------------------------------------------------------------- #


class DiscoveryTests(TempTreeTestCase):
    def test_top_file_is_read_out_of_hls_tcl_not_guessed_from_the_directory(self):
        found = {b.name: b.top_file.name for b in driver.discover_benchmarks(self.bench)}
        self.assertEqual(found, SUITE)
        # jpeg/ and motion/ both contain a real <dir>.c decoy, so a harness that guessed
        # <dir>/<dir>.c would find an existing file and convert the wrong source.
        self.assertTrue((self.bench / "jpeg" / "jpeg.c").exists())
        self.assertTrue((self.bench / "motion" / "motion.c").exists())
        self.assertEqual(found["jpeg"], "main.c")
        self.assertEqual(found["motion"], "mpeg2.c")

    def test_top_file_path_points_inside_the_benchmark_directory(self):
        by_name = {b.name: b for b in driver.discover_benchmarks(self.bench)}
        sha = by_name["sha"]
        self.assertEqual(sha.directory, self.bench / "sha")
        self.assertEqual(sha.top_file, self.bench / "sha" / "sha_driver.c")
        self.assertTrue(sha.top_file.exists())
        self.assertEqual(sha.to_dict()["top_file"], "sha_driver.c")

    def test_the_testbench_add_files_line_is_never_taken_as_the_top(self):
        # The fixture puts `add_files -tb ../common/tb.c` *before* the top's add_files line,
        # so dropping the -tb guard silently converts CHStone's shared testbench instead of
        # the benchmark, and every row would describe the same file.
        tcl = (self.bench / "adpcm" / "hls.tcl").read_text(encoding="utf-8")
        self.assertLess(tcl.index("-tb"), tcl.index("add_files adpcm.c"))
        by_name = {b.name: b for b in driver.discover_benchmarks(self.bench)}
        self.assertEqual(by_name["adpcm"].top_file.name, "adpcm.c")

    def test_a_benchmark_whose_tcl_names_no_source_is_skipped(self):
        make_benchmark(self.bench, "empty", "empty.c",
                       tcl_body="open_project empty_syn\nset_top chstone_main\n")
        self.assertNotIn("empty", [b.name for b in driver.discover_benchmarks(self.bench)])

    def test_a_tcl_naming_a_file_that_does_not_exist_is_skipped(self):
        directory = self.bench / "ghost"
        directory.mkdir()
        (directory / "hls.tcl").write_text(
            'add_files ghost.c -cflags "-Dmain=chstone_main"\n', encoding="utf-8")
        self.assertNotIn("ghost", [b.name for b in driver.discover_benchmarks(self.bench)])

    def test_directories_without_an_hls_tcl_are_not_benchmarks(self):
        names = [b.name for b in driver.discover_benchmarks(self.bench)]
        self.assertNotIn("common", names)
        self.assertEqual(names, sorted(SUITE))

    def test_only_filter_selects_by_directory_name(self):
        selected = driver.discover_benchmarks(self.bench, ("jpeg", "sha"))
        self.assertEqual([b.name for b in selected], ["jpeg", "sha"])
        self.assertEqual([b.top_file.name for b in selected], ["main.c", "sha_driver.c"])


# --------------------------------------------------------------------------- #
# the main -> chstone_main rename
# --------------------------------------------------------------------------- #


class MainRenameTests(unittest.TestCase):
    def test_knr_layout_renames_the_identifier_and_leaves_the_return_type_alone(self):
        source = "int\nmain ()\n{\n  return 0;\n}\n"
        renamed, count = driver.rename_main(source)
        self.assertEqual(count, 1)
        self.assertEqual(renamed, "int\nchstone_main ()\n{\n  return 0;\n}\n")
        # Prepending a return type instead of renaming the identifier yields `int int` on
        # this layout, and every benchmark then fails to compile for a harness reason.
        self.assertNotIn("int int", renamed)
        self.assertNotIn("\nmain (", renamed)

    def test_the_rename_target_matches_the_hls_tcl_define(self):
        # hls.tcl compiles the top with -Dmain=chstone_main; the harness must rename to the
        # same symbol or the generated project's top does not exist.
        self.assertEqual(driver.CHSTONE_TOP_FUNCTION, "chstone_main")
        renamed, _ = driver.rename_main("int\nmain ()\n{\n}\n")
        self.assertIn("chstone_main (", renamed)

    def test_indentation_before_the_definition_is_preserved(self):
        renamed, count = driver.rename_main("static int\n  main (void)\n{\n}\n")
        self.assertEqual(count, 1)
        self.assertEqual(renamed, "static int\n  chstone_main (void)\n{\n}\n")

    def test_other_identifiers_that_merely_contain_main_are_left_alone(self):
        source = make_top_source(result="0", helper="jpeg2bmp_main ();")
        renamed, count = driver.rename_main(source)
        self.assertEqual(count, 1)
        self.assertIn("jpeg2bmp_main ();", renamed)      # a call, not the definition
        self.assertIn("int main_result;", renamed)       # a variable, not the definition
        self.assertIn("main_result = 0;", renamed)
        self.assertNotIn("chstone_main_result", renamed)
        self.assertNotIn("jpeg2bmp_chstone_main", renamed)

    def test_no_main_and_two_mains_are_both_reported_by_the_count(self):
        # run_agent refuses to convert unless the count is exactly 1, so this is the guard
        # that stops a half-renamed source reaching the converter.
        self.assertEqual(driver.rename_main("int helper (void) { return 0; }\n")[1], 0)
        self.assertEqual(driver.rename_main("int\nmain ()\n{}\nint\nmain ()\n{}\n")[1], 2)

    def test_every_top_shape_the_suite_ships_renames_exactly_once(self):
        for name, top in SUITE.items():
            with self.subTest(benchmark=name):
                source = make_top_source(includes=f'#include "{top}"\n', helper=f"{name}_main ();")
                renamed, count = driver.rename_main(source)
                self.assertEqual(count, 1)
                # The definition line still *starts* with the identifier, so the return type
                # on the line above is neither duplicated ("int\nint chstone_main ()", which
                # the compiler reads as `int int`) nor lost.
                definition = next(line for line in renamed.splitlines() if "chstone_main (" in line)
                self.assertTrue(definition.lstrip().startswith("chstone_main ("), definition)
                self.assertEqual(renamed.splitlines()[renamed.splitlines().index(definition) - 1], "int")


# --------------------------------------------------------------------------- #
# native baseline: the exit code is the verdict
# --------------------------------------------------------------------------- #


class NativeBaselineTests(TempTreeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.jpeg = {b.name: b for b in driver.discover_benchmarks(self.bench)}["jpeg"]
        self.calls: "list[list[str]]" = []

    def _fake(self, *, build_rc: int = 0, run_rc: int = 0, stdout: str = "0\n",
              stderr: str = "", run_exc: "BaseException | None" = None):
        def fake_run(cmd, **kwargs):
            self.calls.append(list(cmd))
            if cmd[0] == "gcc":
                return completed(cmd, build_rc, "", "gcc: error: no" if build_rc else "")
            if run_exc is not None:
                raise run_exc
            return completed(cmd, run_rc, stdout, stderr)

        return mock.patch.object(driver.subprocess, "run", fake_run)

    def test_exit_zero_is_the_pass_and_reaches_the_native_pass_rung(self):
        with self._fake(run_rc=0, stdout="0\n"):
            row = driver.run_native(self.jpeg, self.out / "native" / "jpeg", 30)
        self.assertTrue(row.ok)
        self.assertEqual(row.rung, "native_pass")
        self.assertEqual(row.returncode, 0)
        self.assertEqual(row.stdout_head, "0\n")
        self.assertIsNone(row.failure_family)
        self.assertEqual(row.mode, "native")

    def test_a_nonzero_exit_fails_even_when_stdout_still_looks_like_success(self):
        # CHStone's self-check prints main_result *and* returns it. Judging the text instead
        # of the exit code turns a failed self-check into a pass.
        with self._fake(run_rc=1, stdout="0\n"):
            row = driver.run_native(self.jpeg, self.out / "native" / "jpeg", 30)
        self.assertFalse(row.ok)
        self.assertEqual(row.rung, "discovered")
        self.assertEqual(row.returncode, 1)
        self.assertEqual(row.failure_family, "native_selfcheck_failed")

    def test_a_failed_selfcheck_keeps_stdout_and_stderr_as_evidence(self):
        with self._fake(run_rc=3, stdout="17\n", stderr="assertion failed\n"):
            row = driver.run_native(self.jpeg, self.out / "native" / "jpeg", 30)
        self.assertIn("17", row.evidence)
        self.assertIn("assertion failed", row.evidence)

    def test_a_build_failure_never_runs_the_binary(self):
        with self._fake(build_rc=1):
            row = driver.run_native(self.jpeg, self.out / "native" / "jpeg", 30)
        self.assertEqual(row.failure_family, "native_build_error")
        self.assertFalse(row.ok)
        self.assertIsNone(row.returncode)
        self.assertIn("gcc: error", row.evidence)
        self.assertEqual([c[0] for c in self.calls], ["gcc"])

    def test_gcc_compiles_the_tcl_named_top_with_the_benchmark_on_the_include_path(self):
        with self._fake():
            driver.run_native(self.jpeg, self.out / "native" / "jpeg", 30)
        build = self.calls[0]
        self.assertIn(str(self.bench / "jpeg" / "main.c"), build)
        self.assertNotIn(str(self.bench / "jpeg" / "jpeg.c"), build)
        self.assertIn(f"-I{self.bench / 'jpeg'}", build)
        self.assertIn("-lm", build)

    def test_the_binary_runs_inside_the_benchmark_directory(self):
        seen: "dict[str, Any]" = {}

        def fake_run(cmd, **kwargs):
            if cmd[0] == "gcc":
                return completed(cmd, 0)
            seen.update(kwargs)
            return completed(cmd, 0, "0\n")

        with mock.patch.object(driver.subprocess, "run", fake_run):
            driver.run_native(self.jpeg, self.out / "native" / "jpeg", 30)
        # CHStone tops read data files by relative path.
        self.assertEqual(seen["cwd"], str(self.bench / "jpeg"))

    def test_a_timeout_is_its_own_family_and_not_a_pass(self):
        with self._fake(run_exc=subprocess.TimeoutExpired(["bin"], 30)):
            row = driver.run_native(self.jpeg, self.out / "native" / "jpeg", 30)
        self.assertFalse(row.ok)
        self.assertEqual(row.failure_family, "native_timeout")
        self.assertIn("30", row.evidence)

    def test_a_missing_binary_is_a_run_error_and_not_a_pass(self):
        with self._fake(run_exc=OSError("no such file")):
            row = driver.run_native(self.jpeg, self.out / "native" / "jpeg", 30)
        self.assertFalse(row.ok)
        self.assertEqual(row.failure_family, "native_run_error")

    def test_a_native_row_still_disclaims_the_vitis_rungs(self):
        with self._fake():
            row = driver.run_native(self.jpeg, self.out / "native" / "jpeg", 30)
        payload = row.to_dict()
        self.assertFalse(payload["vitis_available"])
        self.assertEqual(payload["rungs_not_attempted"], ["csim", "csynth", "cosim"])


# --------------------------------------------------------------------------- #
# rung classification
# --------------------------------------------------------------------------- #


def phase_report(status: str, *, diagnostics: "tuple[dict, ...]" = (), **extra: Any) -> dict:
    """A conversion_report.json shaped the way c2hlsc_agent.report actually writes one.

    ``phases[name]`` is a serialized ``PhaseResult`` *dict*; the flat top-level key mirrors
    the same status as a string. Both layouts have to be understood.
    """

    return {
        "status": "ok" if status == "pass" else "failed",
        "top": "chstone_main",
        "software_equivalence": status,
        "phases": {
            "software_equivalence": {
                "name": "software_equivalence",
                "status": status,
                "returncode": 0 if status == "pass" else 2,
                "stdout": "",
                "stderr": "",
                "log_path": None,
                "summary": "",
            }
        },
        "diagnostics": list(diagnostics),
        **extra,
    }


ERROR_DIAG = {"severity": "error", "code": "file-io",
              "message": "file or console I/O inside the top is not synthesizable"}


class RungClassificationTests(unittest.TestCase):
    def test_the_testbench_pass_line_is_the_pass_signal(self):
        rung, family = driver._classify_conversion(
            phase_report("fail"), "g++ ...\nAll 20 tests passed.\n")
        self.assertEqual((rung, family), ("host_equivalence", None))

    def test_a_recorded_phase_pass_is_read_out_of_the_serialized_phase_result(self):
        # Regression: `phases["software_equivalence"]` is a dict, and str()-ing it compared a
        # dict repr against "pass", so a real equivalence pass whose log did not carry the
        # "all N tests passed" line was scored as host_behavior_mismatch.
        rung, family = driver._classify_conversion(phase_report("pass"), "make: done\n")
        self.assertEqual((rung, family), ("host_equivalence", None))
        self.assertEqual(driver._phase_status(phase_report("pass"), "software_equivalence"), "pass")

    def test_a_flat_status_string_is_understood_too(self):
        self.assertEqual(
            driver._phase_status({"software_equivalence": "pass"}, "software_equivalence"), "pass")
        self.assertEqual(driver._phase_status({}, "software_equivalence"), "")

    def test_a_recorded_pass_is_overruled_by_an_error_in_the_log(self):
        # The report records the phase as the converter saw it, before sibling sources were
        # staged; the re-run log is authoritative.
        rung, family = driver._classify_conversion(
            phase_report("pass"), "tb/../input.c:12: error: 'x' was not declared in this scope\n")
        self.assertEqual(rung, "generated")
        self.assertEqual(family, "generated_hlsc_does_not_compile")

    def test_equivalence_never_reached_is_the_analyze_rung(self):
        # The earliest failing stage is static analysis: nothing was generated *and* run, so
        # reporting `generated` would claim a rung the benchmark never got to.
        for status, label in (("skipped", "skipped phase"), ("", "no phase at all")):
            with self.subTest(case=label):
                report = phase_report(status, diagnostics=(ERROR_DIAG,))
                if status == "":
                    report = {"diagnostics": [ERROR_DIAG], "phases": {}}
                rung, family = driver._classify_conversion(report, "error: not synthesizable\n")
                self.assertEqual(rung, "analyzed")
                self.assertEqual(family, "static_source_rejected")

    def test_a_link_collision_between_golden_and_candidate_has_its_own_family(self):
        log = "/usr/bin/ld: tb/golden.o:(.bss+0x0): multiple definition of `main_result'\n"
        self.assertEqual(driver._classify_conversion(phase_report("fail"), log),
                         ("generated", "golden_candidate_symbol_collision"))

    def test_c_that_is_not_valid_cpp_is_blamed_on_the_flow_not_the_candidate(self):
        log = ("In file included from tb/../input.c:83:\n"
               "tb/../bf_enc.c:82:1: error: variable or field 'BF_encrypt' declared void\n")
        self.assertEqual(driver._classify_conversion(phase_report("fail"), log),
                         ("generated", "original_c_not_valid_cpp"))

    def test_the_same_marker_without_the_original_source_is_the_candidates_fault(self):
        # No `tb/../` in the log means the error is not in a staged CHStone sibling, so it
        # must not be excused as "the original C is not valid C++".
        log = "input.c:82:1: error: narrowing conversion of '300' from 'int' to 'char'\n"
        self.assertEqual(driver._classify_conversion(phase_report("fail"), log),
                         ("generated", "generated_hlsc_does_not_compile"))

    def test_a_run_that_compiled_but_disagreed_is_a_behavior_mismatch(self):
        log = "test 4: expected 17 got 12\nFAILED\n"
        self.assertEqual(driver._classify_conversion(phase_report("fail"), log),
                         ("generated", "host_behavior_mismatch"))

    def test_a_family_recorded_by_the_converter_wins_over_the_default(self):
        report = phase_report("fail", multi_agent={"failure_family": "interface_mismatch"})
        self.assertEqual(driver._classify_conversion(report, "no markers here\n"),
                         ("generated", "interface_mismatch"))
        report = phase_report("skipped", assessment={"failure_family": "unsupported_construct"})
        self.assertEqual(driver._classify_conversion(report, "error: nope\n"),
                         ("analyzed", "unsupported_construct"))

    def test_the_rung_order_places_analyze_before_generate_before_equivalence(self):
        order = driver.RUNG_ORDER
        self.assertLess(order.index("analyzed"), order.index("generated"))
        self.assertLess(order.index("generated"), order.index("host_equivalence"))


# --------------------------------------------------------------------------- #
# the agent rung
# --------------------------------------------------------------------------- #


class FakeConversion:
    """Stands in for ``python -m c2hlsc_agent.cli convert`` and the ``make test`` re-run."""

    def __init__(self, *, report: "dict | None" = None, convert_log: str = "converted\n",
                 make_log: str = "All 20 tests passed.\n", equivalence_log: "str | None" = None,
                 project_files: "dict[str, str] | None" = None, convert_rc: int = 0,
                 make_exc: "BaseException | None" = None, convert_exc: "BaseException | None" = None,
                 emit_project: bool = True) -> None:
        self.report = phase_report("fail") if report is None else report
        self.convert_log = convert_log
        self.make_log = make_log
        self.equivalence_log = equivalence_log
        self.project_files = project_files or {}
        self.convert_rc = convert_rc
        self.make_exc = make_exc
        self.convert_exc = convert_exc
        self.emit_project = emit_project
        self.commands: "list[list[str]]" = []
        self.cwds: "list[Any]" = []
        self.input_at_convert_time = ""

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.commands.append(cmd)
        self.cwds.append(kwargs.get("cwd"))
        if "c2hlsc_agent.cli" in cmd:
            if self.convert_exc is not None:
                raise self.convert_exc
            self.input_at_convert_time = Path(cmd[cmd.index("--input") + 1]).read_text(encoding="utf-8")
            if self.emit_project:
                project = Path(cmd[cmd.index("--out") + 1])
                project.mkdir(parents=True, exist_ok=True)
                if self.report is not None:
                    (project / "conversion_report.json").write_text(
                        json.dumps(self.report), encoding="utf-8")
                if self.equivalence_log is not None:
                    (project / "software_equivalence.log").write_text(
                        self.equivalence_log, encoding="utf-8")
                for name, text in self.project_files.items():
                    (project / name).write_text(text, encoding="utf-8")
            return completed(cmd, self.convert_rc, self.convert_log, "")
        if cmd[:2] == ["make", "test"]:
            if self.make_exc is not None:
                raise self.make_exc
            return completed(cmd, 0 if "passed" in self.make_log else 2, self.make_log, "")
        raise AssertionError(f"unexpected command: {cmd}")

    @property
    def convert_cmd(self) -> "list[str]":
        return next(cmd for cmd in self.commands if "c2hlsc_agent.cli" in cmd)


class AgentRungTests(TempTreeTestCase):
    def _args(self, *extra: str):
        return driver.build_parser().parse_args(
            ["--benchmark", str(self.bench), "--out-dir", str(self.out), *extra])

    def _bench(self, name: str = "jpeg"):
        return {b.name: b for b in driver.discover_benchmarks(self.bench)}[name]

    def _run(self, fake: FakeConversion, *extra: str, name: str = "jpeg"):
        with mock.patch.object(driver.subprocess, "run", fake):
            return driver.run_agent(self._bench(name), self.out, self._args(*extra))

    def test_the_converter_is_pointed_at_the_renamed_copy_of_the_tcl_named_top(self):
        fake = FakeConversion()
        row = self._run(fake)
        cmd = fake.convert_cmd
        source = Path(cmd[cmd.index("--input") + 1])
        self.assertEqual(source.name, "main.c")           # from hls.tcl, not jpeg.c
        self.assertEqual(source.parent.name, "src_copy")  # a copy: the checkout is untouched
        self.assertIn("chstone_main (", fake.input_at_convert_time)
        self.assertNotIn("int int", fake.input_at_convert_time)
        self.assertEqual(cmd[cmd.index("--top") + 1], "chstone_main")
        self.assertIn("--no-run-vitis", cmd)
        self.assertIn("--no-llm", cmd)
        self.assertEqual(row.mode, "agent")

    def test_the_checkout_is_never_modified(self):
        original = (self.bench / "jpeg" / "main.c").read_text(encoding="utf-8")
        self._run(FakeConversion())
        self.assertEqual((self.bench / "jpeg" / "main.c").read_text(encoding="utf-8"), original)
        self.assertIn("\nmain ()", original)

    def test_llm_flags_are_forwarded_only_when_requested(self):
        fake = FakeConversion()
        self._run(fake, "--use-llm", "--llm-backend", "anthropic", "--llm-model", "claude-opus-5",
                  "--llm-cli-cmd", "claude")
        cmd = fake.convert_cmd
        self.assertIn("--use-llm", cmd)
        self.assertNotIn("--no-llm", cmd)
        self.assertEqual(cmd[cmd.index("--llm-backend") + 1], "anthropic")
        self.assertEqual(cmd[cmd.index("--llm-model") + 1], "claude-opus-5")
        self.assertEqual(cmd[cmd.index("--llm-cli-cmd") + 1], "claude")

    def test_auto_repair_is_independent_of_use_llm(self):
        # The deterministic repairs need no model; gating them on --use-llm measured
        # single-shot generation and called it the agent.
        fake = FakeConversion()
        self._run(fake, "--auto-repair", "--max-iterations", "5")
        cmd = fake.convert_cmd
        self.assertIn("--no-llm", cmd)
        self.assertIn("--auto-repair", cmd)
        self.assertEqual(cmd[cmd.index("--max-iterations") + 1], "5")

        without = FakeConversion()
        self._run(without)
        self.assertNotIn("--auto-repair", without.convert_cmd)

    def test_sibling_sources_are_staged_next_to_the_golden_reference(self):
        # jpeg's top #includes jpeg2bmp.c. Without staging, the *golden* reference cannot
        # compile and the missing include is misread as a defect in the generated HLS-C.
        fake = FakeConversion()
        row = self._run(fake)
        project = Path(row.project_dir or "")
        self.assertTrue((project / "jpeg2bmp.c").exists())
        self.assertIn("jpeg2bmp.c helper", (project / "jpeg2bmp.c").read_text(encoding="utf-8"))
        self.assertFalse((project / "hls.tcl").exists())
        self.assertFalse((project / "main.c").exists())  # the top is already the input
        self.assertIn("host equivalence re-run after staging sibling sources", row.notes)
        self.assertIn(["make", "test"], fake.commands)

    def test_staging_never_overwrites_a_file_the_converter_produced(self):
        fake = FakeConversion(project_files={"jpeg2bmp.c": "/* generated by the converter */\n"})
        row = self._run(fake)
        text = (Path(row.project_dir or "") / "jpeg2bmp.c").read_text(encoding="utf-8")
        self.assertEqual(text, "/* generated by the converter */\n")

    def test_the_restaged_run_is_what_judges_the_row(self):
        # The converter's own log predates the staging fix-up, so a stale pass in it must not
        # survive a failing re-run.
        fake = FakeConversion(equivalence_log="All 20 tests passed.\n",
                              make_log="tb/../jpeg2bmp.c:3: error: 'x' was not declared in this scope\n")
        row = self._run(fake)
        self.assertFalse(row.ok)
        self.assertEqual(row.rung, "generated")
        self.assertIn("was not declared", row.evidence)
        log = (Path(row.project_dir or "") / "software_equivalence.log").read_text(encoding="utf-8")
        self.assertIn("was not declared", log)

    def test_a_passing_re_run_reaches_the_host_equivalence_rung(self):
        row = self._run(FakeConversion(make_log="All 20 tests passed.\n"))
        self.assertTrue(row.ok)
        self.assertEqual(row.rung, "host_equivalence")
        self.assertIsNone(row.failure_family)

    def test_diagnostics_are_carried_onto_the_row(self):
        row = self._run(FakeConversion(report=phase_report("fail", diagnostics=(ERROR_DIAG,)),
                                       make_log="error: boom\n"))
        self.assertEqual(row.diagnostics,
                         ["[error] file-io: file or console I/O inside the top is not synthesizable"])

    def test_a_top_the_rename_cannot_place_never_reaches_the_converter(self):
        make_benchmark(self.bench, "twomains", "twomains.c",
                       source="int\nmain ()\n{}\nint\nmain ()\n{}\n")
        blow_up = mock.Mock(side_effect=AssertionError("must not shell out"))
        with mock.patch.object(driver.subprocess, "run", blow_up):
            row = driver.run_agent(self._bench("twomains"), self.out, self._args())
        self.assertEqual(row.failure_family, "main_rename_failed")
        self.assertFalse(row.ok)
        self.assertEqual(row.rung, "discovered")
        self.assertIn("renamed 2", row.evidence)
        blow_up.assert_not_called()

    def test_a_conversion_timeout_is_recorded_rather_than_scored(self):
        fake = FakeConversion(convert_exc=subprocess.TimeoutExpired(["convert"], 1800))
        row = self._run(fake)
        self.assertFalse(row.ok)
        self.assertEqual(row.failure_family, "conversion_timeout")
        self.assertEqual(row.rung, "discovered")

    def test_a_converter_that_cannot_be_launched_is_recorded(self):
        row = self._run(FakeConversion(convert_exc=OSError("python missing")))
        self.assertEqual(row.failure_family, "conversion_error")
        self.assertFalse(row.ok)

    def test_evidence_is_the_tail_of_the_log_capped_at_the_limit(self):
        head = "first line that must be dropped\n"
        log = head + "x" * (driver.EVIDENCE_LIMIT + 500) + "\ntail marker\n"
        row = self._run(FakeConversion(make_log=log))
        self.assertEqual(len(row.evidence), driver.EVIDENCE_LIMIT)
        self.assertIn("tail marker", row.evidence)
        self.assertNotIn("must be dropped", row.evidence)

    def test_every_agent_row_disclaims_the_vitis_rungs_and_offers_the_followup(self):
        row = self._run(FakeConversion(make_log="All 20 tests passed.\n"))
        payload = row.to_dict()
        self.assertIs(payload["vitis_available"], False)
        self.assertEqual(payload["rungs_not_attempted"], ["csim", "csynth", "cosim"])
        cmd = payload["vitis_followup_cmd"]
        assert isinstance(cmd, str)
        self.assertIn("--top chstone_main", cmd)
        self.assertIn("--vitis-ssh USER@VITIS_HOST", cmd)
        self.assertIn("--input ", cmd)
        self.assertIn("src_copy/main.c", cmd)
        self.assertIn(str(Path(row.project_dir or "")), cmd)
        # It must be runnable as written: every flag carries its value.
        tokens = cmd.split()
        for flag in ("--input", "--top", "--out", "--vitis-ssh"):
            self.assertFalse(tokens[tokens.index(flag) + 1].startswith("--"),
                             f"{flag} has no value in {cmd!r}")

    def test_a_pass_still_never_claims_a_vitis_rung(self):
        row = self._run(FakeConversion(make_log="All 20 tests passed.\n"))
        self.assertTrue(row.ok)
        self.assertEqual(row.rung, "host_equivalence")
        self.assertNotIn(row.rung, driver.VITIS_RUNGS)


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def result_row(name: str, rung: str, ok: bool, family: "str | None" = None) -> Any:
    return driver.BenchmarkResult(benchmark=name, mode="agent", rung=rung, ok=ok,
                                  failure_family=family, duration_s=1.0)


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            result_row("adpcm", "host_equivalence", True),
            result_row("jpeg", "generated", False, "original_c_not_valid_cpp"),
            result_row("motion", "generated", False, "original_c_not_valid_cpp"),
            result_row("sha", "analyzed", False, "static_source_rejected"),
        ]
        self.summary = driver._summarise(self.rows, "agent", 12.34, 4)

    def test_counts_and_families(self):
        self.assertEqual(self.summary["benchmarks"], 4)
        self.assertEqual(self.summary["completed"], 4)
        self.assertEqual(self.summary["passed"], 1)
        self.assertEqual(self.summary["rung_reached"],
                         {"host_equivalence": 1, "generated": 2, "analyzed": 1})
        self.assertEqual(self.summary["failure_families"],
                         {"original_c_not_valid_cpp": 2, "static_source_rejected": 1})

    def test_the_summary_disclaims_the_vitis_rungs(self):
        self.assertIs(self.summary["vitis_available"], False)
        self.assertEqual(self.summary["rungs_not_attempted"], ["csim", "csynth", "cosim"])
        note = self.summary["ladder_note"]
        for rung in ("CSim", "CSynth", "CoSim"):
            self.assertIn(rung, note)
        self.assertIn("NOT attempted", note)

    def test_markdown_names_the_ladder_and_lists_every_benchmark(self):
        text = driver._render_markdown(self.rows, self.summary)
        self.assertIn("passed: **1/4**", text)
        self.assertIn("Ladder coverage", text)
        self.assertIn("CSim, CSynth and C/RTL CoSim were NOT attempted", text)
        for name in ("adpcm", "jpeg", "motion", "sha"):
            self.assertIn(f"`{name}`", text)
        self.assertIn("| `adpcm` | host_equivalence | PASS | - |", text)
        self.assertIn("original_c_not_valid_cpp", text)
        # sorted by benchmark name
        self.assertLess(text.index("`adpcm`"), text.index("`jpeg`"))

    def test_an_empty_run_still_renders(self):
        summary = driver._summarise([], "agent", 0.0, 0)
        self.assertEqual(summary["passed"], 0)
        self.assertIn("# CHStone run", driver._render_markdown([], summary))

    def test_a_recorded_row_round_trips_back_into_the_report(self):
        payload = self.rows[1].to_dict()
        rebuilt = driver.row_from_dict(payload)
        assert rebuilt is not None
        self.assertEqual(rebuilt.to_dict(), payload)
        self.assertIsNone(driver.row_from_dict({"nothing": "useful"}))
        self.assertIsNone(driver.row_from_dict("not a row"))

    def test_an_unknown_extra_field_does_not_break_the_round_trip(self):
        payload = {**self.rows[0].to_dict(), "future_field": 1}
        rebuilt = driver.row_from_dict(payload)
        assert rebuilt is not None
        self.assertEqual(rebuilt.benchmark, "adpcm")
        self.assertTrue(rebuilt.ok)


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


class DriverTests(TempTreeTestCase):
    """End-to-end ``main`` over the native rung, with gcc and the binary faked."""

    def _fake_native(self, failures: "tuple[str, ...]" = ()):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "gcc":
                return completed(cmd, 0)
            name = Path(kwargs.get("cwd", "")).name
            return completed(cmd, 1 if name in failures else 0, "0\n")

        return mock.patch.object(driver.subprocess, "run", fake_run)

    def _argv(self, *extra: str) -> "list[str]":
        return ["--benchmark", str(self.bench), "--out-dir", str(self.out),
                "--native-baseline", *extra]

    def test_a_full_sweep_records_one_row_per_benchmark(self):
        with self._fake_native(failures=("sha",)):
            code, output = run_main(self._argv())
        self.assertEqual(code, 0)
        rows = read_jsonl(self.out / "results.jsonl")
        self.assertEqual(sorted(row["benchmark"] for row in rows), sorted(SUITE))
        verdicts = {row["benchmark"]: row["ok"] for row in rows}
        self.assertFalse(verdicts["sha"])
        self.assertTrue(verdicts["jpeg"])
        report = json.loads((self.out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["passed"], 3)
        self.assertEqual(report["completed"], 4)
        self.assertEqual(report["mode"], "native")
        self.assertIn("native", output)

    def test_every_recorded_row_carries_the_vitis_disclaimer(self):
        with self._fake_native():
            run_main(self._argv())
        rows = read_jsonl(self.out / "results.jsonl")
        self.assertEqual(len(rows), 4)
        for row in rows:
            with self.subTest(benchmark=row["benchmark"]):
                self.assertIs(row["vitis_available"], False)
                self.assertEqual(row["rungs_not_attempted"], ["csim", "csynth", "cosim"])
        report = json.loads((self.out / "report.json").read_text(encoding="utf-8"))
        self.assertIs(report["vitis_available"], False)
        for row in report["results"]:
            self.assertIs(row["vitis_available"], False)

    def test_results_are_flushed_per_benchmark_not_at_the_end(self):
        results_path = self.out / "results.jsonl"
        observed: "list[int]" = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "gcc":
                observed.append(len(read_jsonl(results_path)) if results_path.exists() else 0)
                return completed(cmd, 0)
            return completed(cmd, 0, "0\n")

        with mock.patch.object(driver.subprocess, "run", fake_run):
            run_main(self._argv())
        # Benchmark i sees the i rows its predecessors already flushed, so an interrupted
        # sweep keeps its progress for --resume.
        self.assertEqual(observed, [0, 1, 2, 3])

    def test_resume_skips_benchmarks_already_recorded(self):
        with self._fake_native():
            run_main(self._argv("--benchmarks", "adpcm", "jpeg"))
        self.assertEqual(len(read_jsonl(self.out / "results.jsonl")), 2)

        compiled: "list[str]" = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "gcc":
                compiled.append(Path(cmd[-2]).parent.name)
                return completed(cmd, 0)
            return completed(cmd, 0, "0\n")

        with mock.patch.object(driver.subprocess, "run", fake_run):
            code, output = run_main(self._argv("--resume"))

        self.assertEqual(code, 0)
        self.assertEqual(sorted(compiled), ["motion", "sha"])  # adpcm/jpeg not rebuilt
        self.assertIn("resume: 2 benchmarks already done", output)
        rows = read_jsonl(self.out / "results.jsonl")
        self.assertEqual(sorted(row["benchmark"] for row in rows), sorted(SUITE))

    def test_a_resumed_report_covers_the_whole_suite_not_just_the_tail(self):
        with self._fake_native(failures=("jpeg",)):
            run_main(self._argv("--benchmarks", "adpcm", "jpeg"))
        with self._fake_native():
            run_main(self._argv("--resume"))

        report = json.loads((self.out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["completed"], 4)
        self.assertEqual(report["resumed"], 2)
        self.assertEqual(report["passed"], 3)  # jpeg failed in the first leg and stays failed
        self.assertEqual(sorted(row["benchmark"] for row in report["results"]), sorted(SUITE))
        text = (self.out / "report.md").read_text(encoding="utf-8")
        self.assertIn("passed: **3/4**", text)
        for name in SUITE:
            self.assertIn(f"`{name}`", text)

    def test_a_resumed_row_recorded_twice_is_only_counted_once(self):
        with self._fake_native():
            run_main(self._argv("--benchmarks", "adpcm"))
        with (self.out / "results.jsonl").open("a", encoding="utf-8") as handle:
            handle.write((self.out / "results.jsonl").read_text(encoding="utf-8"))
        with self._fake_native():
            run_main(self._argv("--resume"))
        report = json.loads((self.out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["completed"], 4)
        self.assertEqual([row["benchmark"] for row in report["results"]], sorted(SUITE))

    def test_a_fresh_run_does_not_append_to_the_previous_sweeps_rows(self):
        # Otherwise a native sweep and an agent sweep into one out-dir produce a file with
        # two rows per benchmark, and the next --resume skips everything.
        with self._fake_native():
            run_main(self._argv("--benchmarks", "adpcm", "jpeg"))
        with self._fake_native():
            run_main(self._argv())
        rows = read_jsonl(self.out / "results.jsonl")
        self.assertEqual(sorted(row["benchmark"] for row in rows), sorted(SUITE))
        previous = read_jsonl(self.out / "results.jsonl.prev")
        self.assertEqual(sorted(row["benchmark"] for row in previous), ["adpcm", "jpeg"])

    def test_a_corrupt_line_in_the_results_file_does_not_abort_a_resume(self):
        with self._fake_native():
            run_main(self._argv("--benchmarks", "adpcm"))
        with (self.out / "results.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        with self._fake_native():
            code, _ = run_main(self._argv("--resume"))
        self.assertEqual(code, 0)
        report = json.loads((self.out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["completed"], 4)

    def test_a_missing_checkout_exits_with_a_message_naming_the_upstream_repo(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main(["--benchmark", str(self.tmp / "nope"), "--out-dir", str(self.out)])
        message = str(ctx.exception)
        self.assertIn("not found", message)
        self.assertIn(driver.CHSTONE_URL, message)

    def test_a_checkout_with_no_benchmarks_exits_rather_than_reporting_zero_of_zero(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(SystemExit) as ctx:
            run_main(["--benchmark", str(empty), "--out-dir", str(self.out)])
        self.assertIn("no CHStone benchmarks", str(ctx.exception))


class AgentResumeTests(TempTreeTestCase):
    def test_resume_retries_a_saved_llm_error_and_preserves_the_outage_file(self):
        self.out.mkdir(parents=True)
        old_project = self.out / "old" / "project"
        old_project.mkdir(parents=True)
        (old_project / "conversion_report.md").write_text(
            "- LLM generation attempt 1 failed [RuntimeError: claude CLI failed: quota].\n",
            encoding="utf-8",
        )
        failed = driver.BenchmarkResult(
            benchmark="adpcm", mode="agent", rung="generated", ok=False,
            failure_family="generated_hlsc_does_not_compile", project_dir=str(old_project),
        )
        complete = driver.BenchmarkResult(
            benchmark="jpeg", mode="agent", rung="host_equivalence", ok=True,
        )
        (self.out / "results.jsonl").write_text(
            json.dumps(failed.to_dict()) + "\n" + json.dumps(complete.to_dict()) + "\n",
            encoding="utf-8",
        )
        calls: "list[str]" = []

        def fake_run(bench, out_dir, args):
            calls.append(bench.name)
            return driver.BenchmarkResult(
                benchmark=bench.name, mode="agent", rung="generated", ok=False,
                failure_family="generated_hlsc_does_not_compile",
            )

        argv = [
            "--benchmark", str(self.bench), "--out-dir", str(self.out),
            "--benchmarks", "adpcm", "jpeg", "--use-llm", "--resume",
        ]
        with mock.patch.object(driver, "run_agent_staged", fake_run):
            code, output = run_main(argv)

        self.assertEqual(code, 0)
        self.assertEqual(calls, ["adpcm"])
        self.assertIn("preserved 1 backend-error benchmark row", output)
        rows = read_jsonl(self.out / "results.jsonl")
        self.assertEqual(sorted(row["benchmark"] for row in rows), ["adpcm", "jpeg"])
        self.assertEqual(len(rows), 2)
        backups = list(self.out.glob("results.jsonl.backend-errors.*.bak"))
        self.assertEqual(len(backups), 1)
        saved = read_jsonl(backups[0])
        self.assertEqual(saved[0]["project_dir"], str(old_project))


class ParserTests(unittest.TestCase):
    def test_every_documented_flag_parses(self):
        args = driver.build_parser().parse_args([
            "--benchmark", "/bench", "--out-dir", "/out", "--benchmarks", "adpcm", "sha",
            "--workers", "4", "--native-baseline", "--strict-diagnostics", "--use-llm",
            "--llm-backend", "anthropic", "--llm-model", "claude-opus-5", "--llm-cli-cmd", "claude",
            "--auto-repair", "--max-iterations", "3", "--timeout", "60", "--native-timeout", "30",
            "--resume", "--verbose",
        ])
        self.assertEqual(args.benchmark, Path("/bench"))
        self.assertEqual(args.benchmarks, ["adpcm", "sha"])
        self.assertTrue(args.native_baseline and args.use_llm and args.auto_repair)
        self.assertTrue(args.resume and args.verbose)
        self.assertFalse(args.keep_going)  # --strict-diagnostics turns it off
        self.assertEqual((args.workers, args.max_iterations, args.timeout, args.native_timeout),
                         (4, 3, 60, 30))

    def test_keep_going_defaults_on(self):
        args = driver.build_parser().parse_args(["--benchmark", "/b", "--out-dir", "/o"])
        self.assertTrue(args.keep_going)
        self.assertFalse(args.native_baseline)
        self.assertEqual((args.timeout, args.native_timeout),
                         (driver.DEFAULT_TIMEOUT, driver.DEFAULT_NATIVE_TIMEOUT))


if __name__ == "__main__":
    unittest.main()
