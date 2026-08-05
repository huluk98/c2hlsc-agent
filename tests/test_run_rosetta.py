"""Offline tests for the Rosetta harness.

Everything here is hermetic: ``subprocess.run`` is replaced per test, so no g++, no Xilinx
tooling and no Rosetta checkout are required. The app trees are tiny synthetic fixtures that
reproduce the shapes that decide a verdict -- a Makefile source list with a backslash
continuation, an app that ships ``outputs_golden.txt`` and one that does not, and a host
binary that exits 0 while writing nothing. The driver lives in ``scripts/`` rather than the
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
SCRIPT_PATH = SCRIPTS_DIR / "run_rosetta.py"
sys.path.insert(0, str(SCRIPTS_DIR))
spec = importlib.util.spec_from_file_location("run_rosetta", SCRIPT_PATH)
assert spec and spec.loader
driver = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = driver
spec.loader.exec_module(driver)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

MAKEFILE = """\
# Set kernel name
KERNEL_NAME = {kernel}

# Set host source and headers
HOST_SRC_CPP = {host}
HOST_SRC_H   = ./src/host/utils.h ./src/host/check_result.h ./src/host/typedefs.h
DATA         = {data}

# Set host code include paths
HOST_INC = -I$(XILINX_VIVADO)/include
HOST_LIB = -L$(XILINX_VIVADO)/lib

# Set kernel file
OCL_KERNEL_SRC = ./src/ocl/{kernel}.cl
OCL_KERNEL_H = ./src/host/typedefs.h
SDSOC_KERNEL_SRC = ./src/sdsoc/{kernel}_sdsoc.cpp
SDSOC_KERNEL_H = ./src/host/typedefs.h
SW_KERNEL_SRC = {sw}
SW_KERNEL_H = ./src/host/typedefs.h ./src/sw/{kernel}_sw.h

#-------------------------
# Leave the rest to harness
#-------------------------
include ../harness/harness.mk
"""

#: The accuracy line both outputs.txt and the golden file carry, tab-indented the way
#: Rosetta's check_result.cpp writes it.
GOLDEN_DIGIT = (
    "Test 133: expected = 0, result = 6\n"
    "Test 160: expected = 0, result = 6\n"
    "Test 1997: expected = 9, result = 7\n"
    "\n\t 1878 / 2000 correct!\n"
)
#: face-detection's golden has no accuracy line at all, so it is judged by full content.
GOLDEN_RECTS = "\nresult_size = 43\n\n [Test Bench (main) ] detected rects: 50 89 35 35\n"

SW_KERNEL_SOURCE = """\
#include "{kernel}_sw.h"

// the software kernel the suite itself calls the top
void {kernel}_sw(int input[10], int output[10])
{{
  for (int i = 0; i < 10; i++)
    output[i] = input[i];
}}
"""


def make_app(
    root: Path,
    name: str,
    *,
    kernel: str = "kern",
    host_sources: "tuple[str, ...]" = ("./src/host/main.cpp", "./src/host/utils.cpp"),
    sw_kernel: "str | None" = None,
    golden: "str | None" = None,
    golden_name: str = "outputs_golden.txt",
    decoys: "tuple[str, ...]" = (),
    data: str = "",
    imagelib: bool = False,
    makefile: "str | None" = None,
    continued: bool = False,
) -> Path:
    """One Rosetta-shaped app directory."""

    directory = root / name
    (directory / "src" / "host").mkdir(parents=True, exist_ok=True)
    (directory / "src" / "sw").mkdir(parents=True, exist_ok=True)
    (directory / "src" / "ocl").mkdir(parents=True, exist_ok=True)
    sw_kernel = sw_kernel or f"./src/sw/{kernel}_sw.cpp"

    host_text = " ".join(host_sources)
    if continued:  # optical-flow declares its host list across two lines
        half = len(host_sources) // 2
        host_text = " ".join(host_sources[:half]) + " \\\n               " + " ".join(host_sources[half:])
    (directory / "Makefile").write_text(
        makefile if makefile is not None
        else MAKEFILE.format(kernel=kernel, host=host_text, sw=sw_kernel, data=data),
        encoding="utf-8",
    )
    for relative in (*host_sources, sw_kernel, *decoys):
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix in {".cpp", ".c"} and "sw/" in relative and relative == sw_kernel:
            path.write_text(SW_KERNEL_SOURCE.format(kernel=kernel), encoding="utf-8")
        else:
            path.write_text(f"// {relative}\n", encoding="utf-8")
    (directory / "src" / "ocl" / f"{kernel}.cl").write_text("// opencl\n", encoding="utf-8")
    if golden is not None:
        (directory / golden_name).write_text(golden, encoding="utf-8")
    if imagelib:
        (directory / "imageLib").mkdir(exist_ok=True)
        (directory / "imageLib" / "Image.h").write_text("// imageLib\n", encoding="utf-8")
    return directory


OPTICAL_FLOW_SOURCES = (
    "./src/host/optical_flow_host.cpp", "./src/host/utils.cpp", "./src/host/check_result.cpp",
    "./imageLib/Convert.cpp", "./imageLib/Convolve.cpp", "./imageLib/flowIO.cpp",
    "./imageLib/Image.cpp", "./imageLib/ImageIO.cpp", "./imageLib/RefCntMem.cpp",
)


def make_suite(root: Path) -> Path:
    """The five apps of the real suite, in their three oracle shapes."""

    root.mkdir(parents=True, exist_ok=True)
    make_app(root, "digit-recognition", kernel="DigitRec", golden=GOLDEN_DIGIT,
             host_sources=("./src/host/digit_recognition.cpp", "./src/host/utils.cpp",
                           "./src/host/check_result.cpp"),
             decoys=("./src/host/not_in_the_makefile.cpp", "./src/sw/digitrec_sw_old.cpp"),
             data="./196data/*.dat")
    make_app(root, "face-detection", kernel="face_detect", golden=GOLDEN_RECTS,
             host_sources=("./src/host/face_detect_host.cpp", "./src/host/image.cpp"))
    make_app(root, "3d-rendering", kernel="rendering", golden="Image After Rendering: \n0000\n",
             host_sources=("./src/host/3d_rendering_host.cpp", "./src/host/utils.cpp"))
    # optical-flow and spam-filter ship `output_golden.txt` (singular), which is not the file
    # their host code writes, so neither app has an oracle this harness can trust.
    make_app(root, "optical-flow", kernel="optical_flow", golden="flow field\n",
             golden_name="output_golden.txt", host_sources=OPTICAL_FLOW_SOURCES,
             continued=True, imagelib=True)
    make_app(root, "spam-filter", kernel="SgdLR", golden="Training TPR: 97.8287\n",
             golden_name="output_golden.txt",
             host_sources=("./src/host/spam_filter.cpp", "./src/host/utils.cpp"))
    # Neither of these is an app: harness/ ships only harness.mk, BNN/ has no Makefile.
    (root / "harness").mkdir(exist_ok=True)
    (root / "harness" / "harness.mk").write_text("HOST_SRC_CPP = nope.cpp\n", encoding="utf-8")
    (root / "BNN").mkdir(exist_ok=True)
    (root / "BNN" / "README.md").write_text("# BNN\n", encoding="utf-8")
    return root


APP_NAMES = ["3d-rendering", "digit-recognition", "face-detection", "optical-flow", "spam-filter"]
GOLDEN_APPS = ["3d-rendering", "digit-recognition", "face-detection"]


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
        self.bench = make_suite(self.tmp / "rosetta")
        self.out = self.tmp / "out"
        self.addCleanup(self._tmp.cleanup)

    def app(self, name: str):
        return {a.name: a for a in driver.discover_apps(self.bench)}[name]


# --------------------------------------------------------------------------- #
# discovery: the source list comes from the Makefile, not from a glob
# --------------------------------------------------------------------------- #


class DiscoveryTests(TempTreeTestCase):
    def test_host_sources_are_the_ones_the_makefile_declares(self):
        digit = self.app("digit-recognition")
        self.assertEqual(digit.host_sources,
                         ("./src/host/digit_recognition.cpp", "./src/host/utils.cpp",
                          "./src/host/check_result.cpp"))
        self.assertEqual(digit.sw_kernel, "./src/sw/DigitRec_sw.cpp")

    def test_a_cpp_next_to_the_declared_ones_is_not_swept_in(self):
        # src/host also holds not_in_the_makefile.cpp and src/sw holds digitrec_sw_old.cpp.
        # Globbing would compile both: one adds a duplicate main, the other a second kernel.
        digit = self.app("digit-recognition")
        self.assertTrue((digit.directory / "src" / "host" / "not_in_the_makefile.cpp").exists())
        self.assertTrue((digit.directory / "src" / "sw" / "digitrec_sw_old.cpp").exists())
        joined = " ".join((*digit.host_sources, digit.sw_kernel))
        self.assertNotIn("not_in_the_makefile", joined)
        self.assertNotIn("_sw_old", joined)

    def test_a_source_list_split_over_a_backslash_continuation_is_read_whole(self):
        flow = self.app("optical-flow")
        self.assertEqual(flow.host_sources, OPTICAL_FLOW_SOURCES)
        self.assertEqual(len(flow.host_sources), 9)
        # The imageLib half lives on the continuation line; dropping it loses the linker's
        # entire image library and the app never builds.
        self.assertIn("./imageLib/RefCntMem.cpp", flow.host_sources)

    def test_the_ocl_and_sdsoc_kernels_are_never_taken_for_the_sw_kernel(self):
        for app in driver.discover_apps(self.bench):
            with self.subTest(app=app.name):
                self.assertIn("/sw/", app.sw_kernel)
                self.assertNotIn("/ocl/", app.sw_kernel)
                self.assertNotIn("/sdsoc/", app.sw_kernel)
                self.assertTrue(app.sw_kernel.endswith(".cpp"))

    def test_headers_and_data_globs_are_not_mistaken_for_sources(self):
        digit = self.app("digit-recognition")
        joined = " ".join((*digit.host_sources, digit.sw_kernel))
        self.assertNotIn(".h", joined)
        self.assertNotIn(".dat", joined)      # DATA = ./196data/*.dat
        self.assertNotIn("*", joined)

    def test_every_app_in_the_suite_is_found_and_nothing_else_is(self):
        self.assertEqual([a.name for a in driver.discover_apps(self.bench)], APP_NAMES)

    def test_an_app_whose_makefile_declares_no_sources_is_skipped(self):
        make_app(self.bench, "halfbaked", makefile="KERNEL_NAME = x\nHOST_SRC_CPP = ./a.cpp\n")
        self.assertNotIn("halfbaked", [a.name for a in driver.discover_apps(self.bench)])
        make_app(self.bench, "kernelonly", makefile="SW_KERNEL_SRC = ./src/sw/k.cpp\n")
        self.assertNotIn("kernelonly", [a.name for a in driver.discover_apps(self.bench)])

    def test_only_filter_selects_by_directory_name(self):
        selected = driver.discover_apps(self.bench, ("spam-filter", "digit-recognition"))
        self.assertEqual([a.name for a in selected], ["digit-recognition", "spam-filter"])

    def test_app_metadata_round_trips(self):
        payload = self.app("face-detection").to_dict()
        self.assertEqual(payload["name"], "face-detection")
        self.assertEqual(payload["host_sources"],
                         ["./src/host/face_detect_host.cpp", "./src/host/image.cpp"])


class MakeVarTests(unittest.TestCase):
    def test_a_variable_that_is_absent_or_empty_yields_nothing(self):
        self.assertEqual(driver._make_var("OTHER = a.cpp\n", "HOST_SRC_CPP"), [])
        self.assertEqual(driver._make_var("HOST_SRC_CPP = \nDATA = x\n", "HOST_SRC_CPP"), [])

    def test_a_longer_variable_name_is_not_matched_by_a_shorter_one(self):
        text = ("OCL_KERNEL_SRC = ./src/ocl/k.cl\n"
                "SDSOC_KERNEL_SRC = ./src/sdsoc/k.cpp\n"
                "SW_KERNEL_SRC = ./src/sw/k_sw.cpp\n")
        self.assertEqual(driver._make_var(text, "SW_KERNEL_SRC"), ["./src/sw/k_sw.cpp"])

    def test_only_translation_units_are_kept(self):
        text = "HOST_SRC_CPP = ./a.cpp ./b.c ./c.h -I/usr/include $(EXTRA)\n"
        self.assertEqual(driver._make_var(text, "HOST_SRC_CPP"), ["./a.cpp", "./b.c"])


# --------------------------------------------------------------------------- #
# the oracle
# --------------------------------------------------------------------------- #


class FakeSwRun:
    """Stands in for g++ and for the built host binary."""

    def __init__(self, *, build_rc: int = 0, run_rc: int = 0, stdout: str = "",
                 stderr: str = "", outputs: "str | None" = None,
                 run_exc: "BaseException | None" = None,
                 build_exc: "BaseException | None" = None) -> None:
        self.build_rc = build_rc
        self.run_rc = run_rc
        self.stdout = stdout
        self.stderr = stderr
        self.outputs = outputs
        self.run_exc = run_exc
        self.build_exc = build_exc
        self.commands: "list[list[str]]" = []
        self.cwds: "list[Any]" = []

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.commands.append(cmd)
        self.cwds.append(kwargs.get("cwd"))
        if cmd[0] == "g++":
            if self.build_exc is not None:
                raise self.build_exc
            return completed(cmd, self.build_rc, "", "g++: fatal error" if self.build_rc else "")
        if self.run_exc is not None:
            raise self.run_exc
        if self.outputs is not None:
            (Path(kwargs["cwd"]) / "outputs.txt").write_text(self.outputs, encoding="utf-8")
        return completed(cmd, self.run_rc, self.stdout, self.stderr)

    @property
    def build_cmd(self) -> "list[str]":
        return next(cmd for cmd in self.commands if cmd[0] == "g++")


class OracleTests(TempTreeTestCase):
    def _run(self, name: str, fake: FakeSwRun):
        with mock.patch.object(driver.subprocess, "run", fake):
            return driver.run_sw(self.app(name), self.out, 30)

    def test_an_app_with_a_golden_file_is_scored_against_it(self):
        row = self._run("digit-recognition", FakeSwRun(outputs=GOLDEN_DIGIT))
        self.assertEqual(row.oracle, "golden_file")
        self.assertTrue(row.ok)
        self.assertEqual(row.measured, "1878/2000")
        self.assertEqual(row.expected, "1878/2000")
        self.assertIsNone(row.failure_family)
        self.assertTrue(row.built and row.ran)

    def test_a_golden_vs_measured_mismatch_fails_and_records_both_numbers(self):
        # The real run measured 1870/2000 against a golden of 1878/2000.
        produced = GOLDEN_DIGIT.replace("1878 / 2000", "1870 / 2000")
        row = self._run("digit-recognition", FakeSwRun(outputs=produced))
        self.assertIs(row.ok, False)
        self.assertEqual(row.failure_family, "accuracy_mismatch")
        self.assertEqual(row.measured, "1870/2000")
        self.assertEqual(row.expected, "1878/2000")
        self.assertEqual(row.oracle, "golden_file")

    def test_the_digit_recognition_trap_exit_zero_and_no_figure_is_not_a_pass(self):
        # The regression that nearly shipped as a silent false green: the host prints
        # "Checking results:" and exits 0, but the accuracy only ever lands in outputs.txt.
        # Anything that reads the exit code or the stdout banner scores this a PASS.
        stdout = "Digit Recognition Application\nChecking results:\nelapsed time: 262567 us\n"
        row = self._run("digit-recognition", FakeSwRun(run_rc=0, stdout=stdout, outputs=None))
        self.assertIn("Checking results:", row.evidence)
        self.assertIsNot(row.ok, True)
        self.assertIs(row.ok, False)
        self.assertEqual(row.failure_family, "no_output_file")
        self.assertEqual(row.oracle, "golden_file")  # a golden exists, so it *is* judged
        self.assertTrue(any("did not write outputs.txt" in note for note in row.notes))
        # ... and it does not sneak through the summary as a pass either.
        summary = driver._summarise([row], 1, 0.0)
        self.assertEqual(summary["passed"], 0)
        self.assertEqual(summary["judged"], 1)
        self.assertEqual(summary["no_trustworthy_oracle"], [])

    def test_an_outputs_file_without_the_figure_is_not_a_pass_either(self):
        row = self._run("digit-recognition", FakeSwRun(outputs="Checking results:\n"))
        self.assertIs(row.ok, False)
        self.assertEqual(row.failure_family, "golden_diff")
        self.assertEqual(row.expected, "1878/2000")
        self.assertEqual(row.measured, "")

    def test_an_app_without_a_golden_file_is_reported_not_scored(self):
        for name in ("optical-flow", "spam-filter"):
            with self.subTest(app=name):
                row = self._run(name, FakeSwRun(run_rc=0, stdout="finished\n"))
                self.assertEqual(row.oracle, "no_trustworthy_oracle")
                self.assertIsNone(row.ok)          # not True, and not False either
                self.assertEqual(row.failure_family, "no_golden_output")
                self.assertTrue(any("exit code alone is not evidence" in n for n in row.notes))

    def test_the_singular_output_golden_file_is_not_accepted_as_the_oracle(self):
        # optical-flow ships `output_golden.txt`, which is not the file the host writes.
        flow = self.app("optical-flow")
        self.assertTrue((flow.directory / "output_golden.txt").exists())
        self.assertFalse((flow.directory / "outputs_golden.txt").exists())
        row = self._run("optical-flow", FakeSwRun(outputs="flow field\n"))
        self.assertEqual(row.oracle, "no_trustworthy_oracle")
        self.assertIsNone(row.ok)

    def test_an_unjudged_app_is_excluded_from_the_denominator(self):
        rows = [
            self._run("digit-recognition", FakeSwRun(outputs=GOLDEN_DIGIT)),
            self._run("spam-filter", FakeSwRun(stdout="done\n")),
        ]
        summary = driver._summarise(rows, 2, 0.0)
        self.assertEqual(summary["judged"], 1)
        self.assertEqual(summary["passed"], 1)      # 1/1 judged, not 1/2 apps
        self.assertEqual(summary["no_trustworthy_oracle"], ["spam-filter"])

    def test_a_golden_without_an_accuracy_line_is_compared_whole(self):
        row = self._run("face-detection", FakeSwRun(outputs=GOLDEN_RECTS))
        self.assertTrue(row.ok)
        self.assertEqual(row.oracle, "golden_file")
        self.assertTrue(any("compared full file contents" in note for note in row.notes))

        differs = self._run("face-detection", FakeSwRun(outputs=GOLDEN_RECTS.replace("43", "41")))
        self.assertIs(differs.ok, False)
        self.assertEqual(differs.failure_family, "golden_diff")

    def test_a_stale_outputs_file_in_the_checkout_is_never_judged(self):
        # A checkout that was ever built in place carries outputs.txt at the app root. If the
        # sandbox inherited it, a binary that wrote nothing would be scored against someone
        # else's output -- here, a PASS for a run that produced nothing.
        digit = self.app("digit-recognition")
        (digit.directory / "outputs.txt").write_text(GOLDEN_DIGIT, encoding="utf-8")
        row = self._run("digit-recognition", FakeSwRun(run_rc=0, stdout="Checking results:\n"))
        self.assertIs(row.ok, False)
        self.assertEqual(row.failure_family, "no_output_file")

    def test_a_crash_before_writing_the_output_is_a_fail_not_an_oracle_gap(self):
        row = self._run("digit-recognition",
                        FakeSwRun(run_rc=134, stderr="terminate called after throwing\n"))
        self.assertIs(row.ok, False)
        self.assertEqual(row.oracle, "golden_file")
        self.assertEqual(row.failure_family, "no_output_file")
        self.assertIn("terminate called", row.evidence)

    def test_a_build_failure_never_runs_anything(self):
        fake = FakeSwRun(build_rc=1)
        row = self._run("digit-recognition", fake)
        self.assertEqual(row.failure_family, "build_error")
        self.assertFalse(row.built)
        self.assertFalse(row.ran)
        self.assertIsNone(row.ok)
        self.assertEqual([cmd[0] for cmd in fake.commands], ["g++"])

    def test_a_run_timeout_and_a_launch_failure_are_their_own_families(self):
        timed_out = self._run("digit-recognition",
                              FakeSwRun(run_exc=subprocess.TimeoutExpired(["bin"], 30)))
        self.assertEqual(timed_out.failure_family, "run_timeout")
        self.assertFalse(timed_out.ran)
        self.assertIsNone(timed_out.ok)
        broken = self._run("digit-recognition", FakeSwRun(run_exc=OSError("no such file")))
        self.assertEqual(broken.failure_family, "run_error")

    def test_a_missing_declared_source_is_reported_before_the_compiler_runs(self):
        digit = self.app("digit-recognition")
        (digit.directory / "src" / "host" / "check_result.cpp").unlink()
        blow_up = mock.Mock(side_effect=AssertionError("must not shell out"))
        with mock.patch.object(driver.subprocess, "run", blow_up):
            row = driver.run_sw(digit, self.out, 30)
        self.assertEqual(row.failure_family, "missing_source")
        self.assertIn("check_result.cpp", row.evidence)
        blow_up.assert_not_called()


class BuildCommandTests(TempTreeTestCase):
    def _build_cmd(self, name: str) -> "list[str]":
        fake = FakeSwRun(outputs="")
        with mock.patch.object(driver.subprocess, "run", fake):
            driver.run_sw(self.app(name), self.out, 30)
        return fake.build_cmd

    def test_the_compile_line_carries_every_declared_source_and_selects_the_sw_path(self):
        cmd = self._build_cmd("digit-recognition")
        directory = self.app("digit-recognition").directory
        for relative in ("./src/host/digit_recognition.cpp", "./src/host/utils.cpp",
                         "./src/host/check_result.cpp", "./src/sw/DigitRec_sw.cpp"):
            self.assertIn(str((directory / relative).resolve()), cmd)
        self.assertNotIn(str((directory / "src/host/not_in_the_makefile.cpp").resolve()), cmd)
        # -DSW is what puts the Xilinx-only headers behind their #ifdef.
        self.assertIn("-DSW", cmd)
        self.assertIn(f"-I{directory / 'src' / 'host'}", cmd)
        self.assertIn(f"-I{directory / 'src' / 'sw'}", cmd)

    def test_the_kernel_is_compiled_after_the_host_sources_in_declaration_order(self):
        cmd = self._build_cmd("digit-recognition")
        directory = self.app("digit-recognition").directory
        order = [cmd.index(str((directory / relative).resolve()))
                 for relative in ("./src/host/digit_recognition.cpp", "./src/host/utils.cpp",
                                  "./src/host/check_result.cpp", "./src/sw/DigitRec_sw.cpp")]
        self.assertEqual(order, sorted(order))

    def test_the_imagelib_include_is_added_only_for_the_app_that_ships_one(self):
        flow = self.app("optical-flow")
        self.assertIn(f"-I{flow.directory / 'imageLib'}", self._build_cmd("optical-flow"))
        digit = self.app("digit-recognition")
        self.assertNotIn(f"-I{digit.directory / 'imageLib'}", self._build_cmd("digit-recognition"))

    def test_the_binary_runs_in_a_sandbox_copy_leaving_the_checkout_untouched(self):
        fake = FakeSwRun(outputs=GOLDEN_DIGIT)
        with mock.patch.object(driver.subprocess, "run", fake):
            driver.run_sw(self.app("digit-recognition"), self.out, 30)
        directory = self.app("digit-recognition").directory
        sandbox = Path(fake.cwds[-1])
        self.assertNotEqual(sandbox, directory)
        self.assertFalse((directory / "outputs.txt").exists())
        self.assertTrue((sandbox / "outputs.txt").exists())
        # Data files come along; sources do not (they are already compiled in).
        self.assertTrue((sandbox / "outputs_golden.txt").exists())
        self.assertFalse((sandbox / "src").exists())

    def test_each_run_starts_from_a_clean_sandbox(self):
        first = FakeSwRun(outputs=GOLDEN_DIGIT)
        with mock.patch.object(driver.subprocess, "run", first):
            driver.run_sw(self.app("digit-recognition"), self.out, 30)
        second = FakeSwRun(run_rc=0, outputs=None)
        with mock.patch.object(driver.subprocess, "run", second):
            row = driver.run_sw(self.app("digit-recognition"), self.out, 30)
        # The previous run's outputs.txt must not survive into this verdict.
        self.assertIs(row.ok, False)
        self.assertEqual(row.failure_family, "no_output_file")


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def sw_row(app: str, **kwargs: Any) -> Any:
    return driver.AppResult(app=app, mode="sw", **kwargs)


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            sw_row("3d-rendering", built=True, ran=True, oracle="golden_file", ok=False,
                   failure_family="golden_diff", duration_s=1.75),
            sw_row("digit-recognition", built=True, ran=True, oracle="golden_file", ok=False,
                   failure_family="accuracy_mismatch", measured="1870/2000", expected="1878/2000",
                   duration_s=2.03),
            sw_row("face-detection", built=True, ran=True, oracle="golden_file", ok=True,
                   duration_s=1.82),
            sw_row("optical-flow", built=True, ran=True, oracle="no_trustworthy_oracle",
                   failure_family="no_golden_output", duration_s=4.57),
            sw_row("spam-filter", built=True, ran=True, oracle="no_trustworthy_oracle",
                   failure_family="no_golden_output", duration_s=2.04),
        ]
        self.summary = driver._summarise(self.rows, 5, 6.3)

    def test_the_denominator_is_the_judged_apps_not_every_app(self):
        self.assertEqual(self.summary["apps"], 5)
        self.assertEqual(self.summary["completed"], 5)
        self.assertEqual(self.summary["built"], 5)
        self.assertEqual(self.summary["ran"], 5)
        self.assertEqual(self.summary["judged"], 3)
        self.assertEqual(self.summary["passed"], 1)
        self.assertEqual(self.summary["no_trustworthy_oracle"], ["optical-flow", "spam-filter"])

    def test_no_xilinx_claim_is_made_anywhere(self):
        self.assertIs(self.summary["xilinx_available"], False)
        self.assertEqual(self.summary["rungs_not_attempted"],
                         ["hls_synthesis", "sdaccel", "sdsoc"])
        for row in self.rows:
            with self.subTest(app=row.app):
                payload = row.to_dict()
                self.assertIs(payload["xilinx_available"], False)
                self.assertEqual(payload["rungs_not_attempted"],
                                 ["hls_synthesis", "sdaccel", "sdsoc"])

    def test_markdown_shows_the_unjudged_apps_as_neither_pass_nor_fail(self):
        text = driver._render_markdown(self.rows, self.summary)
        self.assertIn("**passed: 1/3**", text)
        self.assertIn("| `optical-flow` | Y | Y | no_trustworthy_oracle | - | - | - |", text)
        self.assertIn("| `face-detection` | Y | Y | golden_file | PASS |", text)
        self.assertIn("1870/2000", text)
        self.assertIn("1878/2000", text)
        self.assertIn("## No trustworthy oracle", text)
        self.assertIn("an exit code proves only that the program did not crash", text)
        self.assertIn("- `spam-filter`", text)

    def test_the_ladder_note_disclaims_every_xilinx_rung(self):
        note = self.summary["ladder_note"]
        for claim in ("HLS synthesis", "SDAccel", "SDSoC"):
            self.assertIn(claim, note)
        self.assertIn("not out of all apps", note)

    def test_a_recorded_row_round_trips_back_into_the_report(self):
        payload = self.rows[1].to_dict()
        rebuilt = driver.row_from_dict(payload)
        assert rebuilt is not None
        self.assertEqual(rebuilt.to_dict(), payload)
        self.assertIs(rebuilt.ok, False)          # a recorded FAIL stays a FAIL
        unjudged = driver.row_from_dict(self.rows[3].to_dict())
        assert unjudged is not None
        self.assertIsNone(unjudged.ok)            # ... and an unjudged app stays unjudged
        self.assertIs(driver.row_from_dict({"no": "app"}), None)
        self.assertIs(driver.row_from_dict("not a row"), None)


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


class DriverTests(TempTreeTestCase):
    def _fake(self, outputs: "dict[str, str | None] | None" = None):
        """g++ always succeeds; each app writes whatever ``outputs`` says it writes."""

        outputs = outputs or {}

        def fake_run(cmd, **kwargs):
            if cmd[0] == "g++":
                return completed(cmd, 0)
            sandbox = Path(kwargs["cwd"])
            name = sandbox.parent.name
            text = outputs.get(name)
            if text is not None:
                (sandbox / "outputs.txt").write_text(text, encoding="utf-8")
            return completed(cmd, 0, f"{name} done\n")

        return mock.patch.object(driver.subprocess, "run", fake_run)

    def _argv(self, *extra: str) -> "list[str]":
        return ["--benchmark", str(self.bench), "--out-dir", str(self.out), *extra]

    def _outputs(self) -> "dict[str, str | None]":
        return {
            "digit-recognition": GOLDEN_DIGIT.replace("1878 / 2000", "1870 / 2000"),
            "face-detection": GOLDEN_RECTS,
            "3d-rendering": "Image After Rendering: \n1111\n",
        }

    def test_a_full_sweep_reproduces_the_committed_verdicts(self):
        with self._fake(self._outputs()):
            code, output = run_main(self._argv())
        self.assertEqual(code, 0)
        rows = {row["app"]: row for row in read_jsonl(self.out / "results.jsonl")}
        self.assertEqual(sorted(rows), APP_NAMES)
        self.assertTrue(rows["face-detection"]["ok"])
        self.assertIs(rows["digit-recognition"]["ok"], False)
        self.assertEqual(rows["digit-recognition"]["measured"], "1870/2000")
        self.assertEqual(rows["digit-recognition"]["expected"], "1878/2000")
        self.assertIs(rows["3d-rendering"]["ok"], False)
        self.assertIsNone(rows["optical-flow"]["ok"])
        self.assertIsNone(rows["spam-filter"]["ok"])
        report = json.loads((self.out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual((report["judged"], report["passed"]), (3, 1))
        self.assertIn("no trustworthy oracle (excluded)", output)

    def test_every_recorded_row_disclaims_the_xilinx_rungs(self):
        with self._fake(self._outputs()):
            run_main(self._argv())
        rows = read_jsonl(self.out / "results.jsonl")
        self.assertEqual(len(rows), 5)
        for row in rows:
            with self.subTest(app=row["app"]):
                self.assertIs(row["xilinx_available"], False)
                self.assertEqual(row["rungs_not_attempted"], ["hls_synthesis", "sdaccel", "sdsoc"])
        report = json.loads((self.out / "report.json").read_text(encoding="utf-8"))
        self.assertIs(report["xilinx_available"], False)
        for row in report["results"]:
            self.assertIs(row["xilinx_available"], False)

    def test_results_are_flushed_per_app_not_at_the_end(self):
        results_path = self.out / "results.jsonl"
        observed: "list[int]" = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "g++":
                observed.append(len(read_jsonl(results_path)) if results_path.exists() else 0)
                return completed(cmd, 0)
            return completed(cmd, 0, "done\n")

        with mock.patch.object(driver.subprocess, "run", fake_run):
            run_main(self._argv())
        self.assertEqual(observed, [0, 1, 2, 3, 4])

    def test_resume_skips_apps_already_recorded_and_still_reports_the_whole_suite(self):
        with self._fake(self._outputs()):
            run_main(self._argv("--apps", "digit-recognition", "face-detection"))
        self.assertEqual(len(read_jsonl(self.out / "results.jsonl")), 2)

        built: "list[str]" = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "g++":
                built.append(Path(cmd[cmd.index("-o") + 1]).stem)
                return completed(cmd, 0)
            return completed(cmd, 0, "done\n")

        with mock.patch.object(driver.subprocess, "run", fake_run):
            code, output = run_main(self._argv("--resume"))

        self.assertEqual(code, 0)
        self.assertEqual(sorted(built), ["3d-rendering", "optical-flow", "spam-filter"])
        self.assertIn("resume: 2 apps already done", output)
        report = json.loads((self.out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["completed"], 5)
        self.assertEqual(report["resumed"], 2)
        # The verdicts from the first leg survive into the resumed report.
        self.assertEqual(report["judged"], 3)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(sorted(row["app"] for row in report["results"]), APP_NAMES)

    def test_a_fresh_run_does_not_append_to_the_previous_sweeps_rows(self):
        with self._fake(self._outputs()):
            run_main(self._argv("--apps", "digit-recognition"))
            run_main(self._argv())
        rows = read_jsonl(self.out / "results.jsonl")
        self.assertEqual(sorted(row["app"] for row in rows), APP_NAMES)
        self.assertEqual([row["app"] for row in read_jsonl(self.out / "results.jsonl.prev")],
                         ["digit-recognition"])

    def test_a_corrupt_line_in_the_results_file_does_not_abort_a_resume(self):
        with self._fake(self._outputs()):
            run_main(self._argv("--apps", "digit-recognition"))
        with (self.out / "results.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        with self._fake(self._outputs()):
            code, _ = run_main(self._argv("--resume"))
        self.assertEqual(code, 0)
        report = json.loads((self.out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["completed"], 5)

    def test_a_missing_checkout_exits_with_a_message_naming_the_upstream_repo(self):
        with self.assertRaises(SystemExit) as ctx:
            run_main(["--benchmark", str(self.tmp / "nope"), "--out-dir", str(self.out)])
        self.assertIn("not found", str(ctx.exception))
        self.assertIn(driver.ROSETTA_URL, str(ctx.exception))

    def test_a_checkout_with_no_apps_exits_rather_than_reporting_zero_of_zero(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(SystemExit) as ctx:
            run_main(["--benchmark", str(empty), "--out-dir", str(self.out)])
        self.assertIn("no Rosetta apps", str(ctx.exception))


# --------------------------------------------------------------------------- #
# agent rung (the second mode; the software baseline above is the calibration rung)
# --------------------------------------------------------------------------- #


class AgentModeTests(TempTreeTestCase):
    def test_the_sw_top_comes_from_the_makefiles_kernel_name(self):
        # Same discipline as CHStone reading hls.tcl: the suite itself names the kernel.
        self.assertEqual(driver.discover_sw_top(self.app("digit-recognition")), "DigitRec_sw")
        self.assertEqual(driver.discover_sw_top(self.app("spam-filter")), "SgdLR_sw")

    def test_a_kernel_without_a_matching_definition_has_no_top(self):
        app = self.app("face-detection")
        (app.directory / app.sw_kernel).write_text("int unrelated(void) { return 0; }\n",
                                                   encoding="utf-8")
        self.assertIsNone(driver.discover_sw_top(app))

    def test_agent_only_flags_select_the_agent_mode_so_they_cannot_be_ignored(self):
        parser = driver.build_parser()
        base = ["--benchmark", "/b", "--out-dir", "/o"]
        self.assertFalse(parser.parse_args(base).agent)
        self.assertTrue(parser.parse_args([*base, "--agent"]).agent)
        self.assertTrue(parser.parse_args([*base, "--use-llm"]).use_llm)
        # --agent and --sw-baseline are mutually exclusive rather than silently ordered.
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args([*base, "--agent", "--sw-baseline"])

    def test_the_equivalence_pass_line_is_the_only_pass_signal(self):
        rung, family, walls = driver.classify_agent(
            {}, "All 20 tests passed.\n", self.tmp / "nowhere", "DigitRec_sw", "")
        self.assertEqual((rung, family, walls), ("host_equivalence", None, []))

    def _project(self, *, generated: bool = False, ran: bool = False) -> Path:
        project = self.tmp / "project"
        (project / "src").mkdir(parents=True, exist_ok=True)
        if generated:
            (project / "src" / "hls_top.cpp").write_text("// generated\n", encoding="utf-8")
        if ran:
            (project / "software_equivalence.log").write_text("make test\n", encoding="utf-8")
        return project

    def test_a_run_that_never_reached_equivalence_is_not_scored_as_generated(self):
        # Claiming the generate rung for a run that compiled nothing overstates how far the
        # app got, and the failure family then describes the wrong stage.
        order = driver.AGENT_RUNG_ORDER
        report = {"diagnostics": [{"severity": "error", "code": "vla", "message": "no"}]}
        for label, project in (("no project", self._project()),
                               ("project but no equivalence run",
                                self._project(generated=True))):
            with self.subTest(case=label):
                rung, family, _walls = driver.classify_agent(
                    report, "error: nope\n", project, "DigitRec_sw", "")
                self.assertLess(order.index(rung), order.index("generated"))
                self.assertIsNotNone(family)

    def test_a_generated_project_that_does_not_compile_stops_at_the_generate_rung(self):
        rung, family, walls = driver.classify_agent(
            {}, "src/hls_top.cpp:3:1: error: 'x' was not declared in this scope\n",
            self._project(generated=True, ran=True), "DigitRec_sw", "")
        self.assertEqual(rung, "generated")
        self.assertEqual(family, "generated_hlsc_does_not_compile")
        self.assertTrue(walls)
        self.assertNotEqual(rung, "host_equivalence")

    def test_an_agent_row_makes_no_xilinx_claim(self):
        row = driver.AgentResult(app="digit-recognition", rung="generated")
        payload = row.to_dict()
        self.assertIs(payload["xilinx_available"], False)
        self.assertEqual(payload["rungs_not_attempted"], ["hls_synthesis", "sdaccel", "sdsoc"])
        summary = driver._summarise_agent([row], 1, 0.0)
        self.assertIs(summary["xilinx_available"], False)
        for claim in ("HLS synthesis", "SDAccel", "SDSoC"):
            self.assertIn(claim, summary["ladder_note"])

    def test_an_agent_row_round_trips_through_the_results_file(self):
        row = driver.AgentResult(app="spam-filter", top="SgdLR_sw", rung="generated",
                                 walls=["multidim_array_arg_unsupported"])
        rebuilt = driver.agent_row_from_dict(row.to_dict())
        assert rebuilt is not None
        self.assertEqual(rebuilt.to_dict(), row.to_dict())
        self.assertIsNone(driver.agent_row_from_dict({"nothing": True}))


if __name__ == "__main__":
    unittest.main()
