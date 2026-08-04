import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.cli import build_parser, main as cli_main
from c2hlsc_agent.cross_reference import (
    build_arm_tu,
    build_framing_b,
    build_harness,
    done_record_ids,
    executability_reason,
    normalize_signature,
    strip_extern_c,
)
from c2hlsc_agent.nl_records import extract_named_function, load_records


REFERENCE_MARKER = "DATASET_REFERENCE_DO_NOT_LEAK"

RECORD = {
    "record_id": 7,
    "top_function": "xadd",
    "original_file": "7_hls.txt",
    "HLS_instruction": (
        "You are a Vitis HLS code generation assistant. Follow the task.\n"
        "**Design Task:** Add two to each element\n"
        "Given an input array `data` of `n` int elements, write each element plus 2 "
        "into `out` and return n. Arrays hold at most 16 elements.\n"
        "Return only HLS C/C++ code."
    ),
    "hls_cpp": f"// {REFERENCE_MARKER}\nint xadd(const int *data, int *out, int n) {{ return 0; }}\n",
}

ARM_A_GOOD = """Here is the implementation.
```cpp
#define STEP 2
int xadd(const int *data, int *out, int n) {
  for (int i = 0; i < n; ++i) {
    out[i] = data[i] + STEP;
  }
  return n;
}
```
"""

ARM_B_GOOD = """```cpp
#define STEP 7
static int bump(int v) { return v + 2; }
int xadd(const int *values, int *result, int count) {
  for (int i = 0; i < count; ++i) {
    result[i] = bump(values[i]);
  }
  return count;
}
```
"""

ARM_B_DIVERGENT = """```cpp
int xadd(const int *data, int *out, int n) {
  for (int i = 0; i < n; ++i) {
    out[i] = data[i] + 3;
  }
  return n;
}
```
"""


class SeqLLM:
    def __init__(self, responses, model="fake-model"):
        self.responses = list(responses)
        self.model = model
        self.calls = []

    def complete(self, system, user, *, max_tokens=8000):
        self.calls.append((system, user))
        if not self.responses:
            raise AssertionError("SeqLLM exhausted")
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class ExplodingLLM:
    model = "exploding"

    def complete(self, system, user, *, max_tokens=8000):
        raise RuntimeError("backend down")


def _xref_args(records_path, out_dir, extra=()):
    return build_parser().parse_args(
        ["cross-reference", "--records", str(records_path), "--out", str(out_dir), *extra]
    )


def _write_records(tmp, records):
    path = Path(tmp) / "records.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


class UnitTests(unittest.TestCase):
    def test_framing_b_cuts_boilerplate_and_restates(self):
        system, user = build_framing_b(RECORD)
        self.assertIn("ONE complete, self-contained", system)
        self.assertIn("Add two to each element", user)
        self.assertIn("`xadd`", user)
        self.assertNotIn("You are a Vitis HLS code generation assistant", user)
        self.assertNotIn(REFERENCE_MARKER, system + user)

    def test_strip_extern_c_block_and_prefix(self):
        code = 'extern "C" {\nint xadd(int a) { return a; }\n}\n'
        stripped, flag = strip_extern_c(code)
        self.assertTrue(flag)
        self.assertNotIn('extern "C"', stripped)
        self.assertIn("int xadd(int a) { return a; }", stripped)
        prefix = 'extern "C" int xadd(int a) { return a; }\n'
        stripped2, flag2 = strip_extern_c(prefix)
        self.assertTrue(flag2)
        self.assertNotIn("extern", stripped2)

    def test_arm_tu_confines_defines_and_lifts_includes(self):
        code = "#include <cstdint>\n#define N 4\nint xadd(int a) { return a + N; }\n"
        tu = build_arm_tu(code, "xref_a")
        include_pos = tu.index("#include <cstdint>")
        namespace_pos = tu.index("namespace xref_a {")
        define_pos = tu.index("#define N 4")
        self.assertLess(include_pos, namespace_pos)
        self.assertGreater(define_pos, namespace_pos)

    def test_normalize_signature_ignores_argument_names(self):
        sig_a = extract_named_function("int xadd(const int *data, int *out, int n) { return n; }", "xadd")
        sig_b = extract_named_function("int xadd(const int *values, int *result, int count) { return count; }", "xadd")
        self.assertEqual(normalize_signature(sig_a), normalize_signature(sig_b))

    def test_executability_gate_orders_hls_types_before_integers(self):
        ap = extract_named_function("ap_int<8> f(ap_int<8> a) { return a; }", "f")
        self.assertEqual(executability_reason(ap), "ap_int_arg")
        stream = extract_named_function("void f(hls::stream<int> &in_s) { }", "f")
        self.assertEqual(executability_reason(stream), "hls_stream_arg")
        plain = extract_named_function("int f(const int *a, int n) { return n; }", "f")
        self.assertIsNone(executability_reason(plain))

    def test_harness_clamps_length_scalar(self):
        sig = extract_named_function("int xadd(const int *data, int *out, int n) { return n; }", "xadd")
        harness = build_harness(sig, sig, seed=1, n_vectors=4)
        self.assertIn("bounded_scalar<long long>(test_idx, rng, 1, 16)", harness)
        self.assertIn("clamp_count(static_cast<long long>(n), 16)", harness)
        self.assertIn("namespace xref_a", harness)
        self.assertIn("namespace xref_b", harness)


class DualGenerationTests(unittest.TestCase):
    def _run(self, tmp, llm, records=None, extra=()):
        records_path = _write_records(tmp, records or [RECORD])
        out_dir = Path(tmp) / "out"
        args = _xref_args(records_path, out_dir, extra)
        with patch("c2hlsc_agent.cross_reference.build_llm_client", return_value=llm):
            from c2hlsc_agent.cross_reference import run_cross_reference

            rc = run_cross_reference(args)
        return rc, out_dir

    def test_two_isolated_calls_with_different_framings(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm = SeqLLM([ARM_A_GOOD, ARM_B_GOOD])
            rc, out_dir = self._run(tmp, llm)
            self.assertEqual(len(llm.calls), 2)
            (system_a, user_a), (system_b, user_b) = llm.calls
            self.assertNotEqual(user_a, user_b)
            # Arm B saw neither arm A's output nor the dataset reference.
            self.assertNotIn("STEP 2", system_b + user_b)
            for text in (system_a, user_a, system_b, user_b):
                self.assertNotIn(REFERENCE_MARKER, text)
            self.assertEqual(rc, 0)

    def test_agreeing_arms_cross_verify_despite_macro_and_name_differences(self):
        # Same semantics, different arg names, different helper structure, and
        # CONFLICTING #define values — the separate-TU isolation must keep arm A's
        # STEP=2 from leaking into arm B (which defines STEP=7 but never uses it).
        with tempfile.TemporaryDirectory() as tmp:
            rc, out_dir = self._run(tmp, SeqLLM([ARM_A_GOOD, ARM_B_GOOD]))
            self.assertEqual(rc, 0)
            rows = [json.loads(l) for l in (out_dir / "results.jsonl").read_text().splitlines()]
            self.assertEqual(rows[0]["classification"], "cross_verified")
            corpus = load_records(out_dir / "cross_referenced_corpus.jsonl")
            self.assertEqual(len(corpus), 1)
            self.assertEqual(corpus[0]["top_function"], "xadd")
            self.assertEqual(corpus[0]["status"], "cross_verified")
            self.assertIn("cross_reference", corpus[0]["verification_provenance"])
            review = (out_dir / "needs_review.jsonl").read_text(encoding="utf-8")
            self.assertEqual(review.strip(), "")

    def test_divergent_arms_land_in_needs_review_with_mismatches(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out_dir = self._run(tmp, SeqLLM([ARM_A_GOOD, ARM_B_DIVERGENT]))
            self.assertEqual(rc, 0)  # divergent is a result, not an error
            rows = [json.loads(l) for l in (out_dir / "results.jsonl").read_text().splitlines()]
            self.assertEqual(rows[0]["classification"], "divergent")
            self.assertEqual(rows[0]["reason"], "output_mismatch")
            self.assertTrue(rows[0]["mismatches"])
            first = rows[0]["mismatches"][0]
            self.assertEqual(first["argument"], "out")
            review = [json.loads(l) for l in (out_dir / "needs_review.jsonl").read_text().splitlines()]
            self.assertEqual(len(review), 1)
            self.assertEqual((out_dir / "cross_referenced_corpus.jsonl").read_text().strip(), "")

    def test_unparseable_arm_classifies_unparseable(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out_dir = self._run(tmp, SeqLLM([ARM_A_GOOD, "I cannot produce code for this."]))
            self.assertEqual(rc, 0)
            rows = [json.loads(l) for l in (out_dir / "results.jsonl").read_text().splitlines()]
            self.assertEqual(rows[0]["classification"], "unparseable")

    def test_ap_type_record_is_unavailable_without_compiling(self):
        ap_a = "```cpp\nap_int<8> f(ap_int<8> a) { return a; }\n```"
        record = dict(RECORD, top_function="f", record_id=8)
        with tempfile.TemporaryDirectory() as tmp:
            rc, out_dir = self._run(tmp, SeqLLM([ap_a, ap_a]), records=[record])
            self.assertEqual(rc, 0)
            rows = [json.loads(l) for l in (out_dir / "results.jsonl").read_text().splitlines()]
            self.assertEqual(rows[0]["classification"], "unavailable")
            self.assertEqual(rows[0]["reason"], "ap_int_arg")

    def test_infra_error_rows_are_retried_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out_dir = self._run(tmp, ExplodingLLM())
            self.assertEqual(rc, 1)  # infra failure -> nonzero
            rows = [json.loads(l) for l in (out_dir / "results.jsonl").read_text().splitlines()]
            self.assertTrue(rows[0]["infra_error"])
            self.assertEqual(done_record_ids(out_dir / "results.jsonl"), set())
            # Rerun with a working backend: the record is processed, not skipped.
            records_path = _write_records(tmp, [RECORD])
            args = _xref_args(records_path, out_dir)
            with patch(
                "c2hlsc_agent.cross_reference.build_llm_client",
                return_value=SeqLLM([ARM_A_GOOD, ARM_B_GOOD]),
            ):
                from c2hlsc_agent.cross_reference import run_cross_reference

                rc2 = run_cross_reference(args)
            self.assertEqual(rc2, 0)
            self.assertEqual(done_record_ids(out_dir / "results.jsonl"), {7})
            rows = [json.loads(l) for l in (out_dir / "results.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 2)  # append stream keeps the failed attempt
            self.assertEqual(rows[-1]["classification"], "cross_verified")

    def test_completed_records_are_skipped_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm = SeqLLM([ARM_A_GOOD, ARM_B_GOOD])
            rc, out_dir = self._run(tmp, llm)
            self.assertEqual(rc, 0)
            calls_after_first = len(llm.calls)
            records_path = _write_records(tmp, [RECORD])
            args = _xref_args(records_path, out_dir)
            with patch("c2hlsc_agent.cross_reference.build_llm_client", return_value=llm):
                from c2hlsc_agent.cross_reference import run_cross_reference

                rc2 = run_cross_reference(args)
            self.assertEqual(rc2, 0)
            self.assertEqual(len(llm.calls), calls_after_first)  # no new LLM calls

    def test_cli_dispatch_reaches_cross_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            records_path = _write_records(tmp, [RECORD])
            out_dir = Path(tmp) / "out"
            with patch(
                "c2hlsc_agent.cross_reference.build_llm_client",
                return_value=SeqLLM([ARM_A_GOOD, ARM_B_GOOD]),
            ):
                rc = cli_main(
                    ["cross-reference", "--records", str(records_path), "--out", str(out_dir)]
                )
            self.assertEqual(rc, 0)
            self.assertTrue((out_dir / "results.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
