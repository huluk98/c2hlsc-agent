import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c2hlsc_agent.cli import (
    _permit_optional_llm_fallback,
    build_parser,
    run_convert,
    run_status,
)
from c2hlsc_agent.equivalence import PhaseResult, VerificationState
from c2hlsc_agent.run_control import (
    BudgetedLLMClient,
    RunBudget,
    RunBudgetExceeded,
    RunClosed,
    RunController,
    RunLedger,
    RunStatus,
    derive_run_id,
    failure_fingerprint,
    snapshot_for_record,
    stable_fingerprint,
)


class _FakeLLM:
    model = 'fake-model'

    def complete(self, system, user, *, max_tokens=4096):
        del system, user, max_tokens
        return 'done'


def _failed_state() -> VerificationState:
    state = VerificationState()
    state.add_phase(
        PhaseResult(
            'software_equivalence',
            'fail',
            stderr='stable mismatch',
        )
    )
    return state


class RunControlTests(unittest.TestCase):
    def test_repeated_failure_persists_across_process_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            budget = RunBudget(2, 600, 2, 2)
            first = RunController(project, 'shared-run', budget, 'identity')
            first.reserve_attempt('source-a')
            self.assertEqual(first.record_verification('source-a', 'failure-a'), 1)
            first.finish(RunStatus.FAILED, 'needs another attempt')

            resumed = RunController(project, 'shared-run', budget, 'identity')
            self.assertEqual(resumed.record.usage.attempts, 1)
            resumed.reserve_attempt('source-a')
            self.assertEqual(resumed.record_verification('source-a', 'failure-a'), 2)
            resumed.finish(RunStatus.EXHAUSTED, 'repeated state')

            latest = resumed.ledger.latest('shared-run')
            self.assertIsNotNone(latest)
            self.assertEqual(latest.status, RunStatus.EXHAUSTED)
            with self.assertRaises(RunClosed):
                RunController(project, 'shared-run', budget, 'identity')

    def test_budgets_are_reserved_before_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = RunController(
                Path(tmp),
                'budget-run',
                RunBudget(1, 600, 1, 1),
                'identity',
            )
            controller.reserve_attempt('source')
            with self.assertRaises(RunBudgetExceeded) as attempt_error:
                controller.reserve_attempt('source')
            self.assertEqual(attempt_error.exception.resource, 'attempts')
            controller.reserve_vitis_run()
            with self.assertRaises(RunBudgetExceeded) as vitis_error:
                controller.reserve_vitis_run()
            self.assertEqual(vitis_error.exception.resource, 'vitis_runs')
            controller.record.started_at = '2000-01-01T00:00:00Z'
            with self.assertRaises(RunBudgetExceeded) as wall_error:
                controller.reserve_llm_call('test')
            self.assertEqual(wall_error.exception.resource, 'wall_seconds')
            controller.finish(RunStatus.EXHAUSTED, 'budgets reached')

    def test_llm_ledger_counts_calls_without_prompt_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            controller = RunController(
                project,
                'privacy-run',
                RunBudget(1, 600, 1, 1),
                'identity',
            )
            client = BudgetedLLMClient(_FakeLLM(), controller, 'test')
            self.assertEqual(
                client.complete('SECRET SYSTEM', 'SECRET USER'),
                'done',
            )
            with self.assertRaises(RunBudgetExceeded):
                client.complete('SECRET AGAIN', 'SECRET AGAIN')
            ledger_text = controller.ledger.path.read_text(encoding='utf-8')
            self.assertNotIn('SECRET', ledger_text)
            self.assertEqual(controller.record.usage.llm_calls, 1)

    def test_optional_fallback_never_ignores_wall_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = RunController(
                Path(tmp),
                'fallback-run',
                RunBudget(1, 600, 1, 1),
                'identity',
            )
            _permit_optional_llm_fallback(
                controller,
                RunBudgetExceeded('llm_calls', 'LLM limit'),
                'generation',
            )
            self.assertEqual(controller.record.status, RunStatus.RUNNING)
            with self.assertRaises(SystemExit):
                _permit_optional_llm_fallback(
                    controller,
                    RunBudgetExceeded('wall_seconds', 'wall limit'),
                    'generation',
                )
            self.assertEqual(controller.record.status, RunStatus.EXHAUSTED)

    def test_convert_report_tracks_failure_then_repeated_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'input.c'
            source.write_text(
                'int bump(int n) { return n + 1; }\n',
                encoding='utf-8',
            )
            project = root / 'project'
            args = build_parser().parse_args(
                [
                    'convert',
                    '--input',
                    str(source),
                    '--top',
                    'bump',
                    '--out',
                    str(project),
                    '--no-run-vitis',
                    '--max-iterations',
                    '3',
                ]
            )
            with patch(
                'c2hlsc_agent.cli.verify_project',
                return_value=_failed_state(),
            ):
                self.assertEqual(run_convert(args), 1)
            first = json.loads(
                (project / 'conversion_report.json').read_text(encoding='utf-8')
            )
            self.assertEqual(first['status'], 'fail')
            self.assertEqual(first['run_control']['status'], 'failed')
            self.assertEqual(first['run_control']['usage']['attempts'], 1)

            with patch(
                'c2hlsc_agent.cli.verify_project',
                return_value=_failed_state(),
            ):
                self.assertEqual(run_convert(args), 1)
            second = json.loads(
                (project / 'conversion_report.json').read_text(encoding='utf-8')
            )
            self.assertEqual(second['run_control']['status'], 'exhausted')
            self.assertEqual(second['run_control']['usage']['attempts'], 2)

    def test_status_command_reads_without_resuming_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            controller = RunController(
                project,
                'status-run',
                RunBudget(2, 600, 2, 2),
                'identity',
            )
            controller.reserve_attempt('source')
            controller.finish(RunStatus.FAILED, 'manual evidence needed')
            args = build_parser().parse_args(
                ['status', '--project', str(project), '--json']
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(run_status(args), 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload['status'], 'failed')
            self.assertEqual(payload['usage']['attempts'], 1)
            latest = RunLedger(controller.ledger.path).latest()
            self.assertEqual(latest.status, RunStatus.FAILED)

    def test_identity_and_snapshot_are_stable(self):
        first = derive_run_id({'top': 'add', 'clock': 10.0})
        second = derive_run_id({'clock': 10.0, 'top': 'add'})
        self.assertEqual(first, second)
        self.assertEqual(
            stable_fingerprint({'a': 1, 'b': 2}),
            stable_fingerprint({'b': 2, 'a': 1}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            controller = RunController(
                Path(tmp),
                first,
                RunBudget(1, 600, 1, 1),
                'identity',
            )
            snapshot = snapshot_for_record(
                controller.record,
                controller.ledger.path,
            )
            self.assertEqual(snapshot['ledger_file'], 'run_ledger.jsonl')
            self.assertEqual(snapshot['remaining']['attempts'], 1)

    def test_failure_fingerprint_ignores_machine_specific_paths(self):
        windows = VerificationState()
        windows.add_phase(
            PhaseResult(
                'csynth',
                'fail',
                stderr=r'C:\work\case\top.cpp:10: error: bad type',
            )
        )
        linux = VerificationState()
        linux.add_phase(
            PhaseResult(
                'csynth',
                'fail',
                stderr='/home/user/case/top.cpp:10: error: bad type',
            )
        )
        self.assertEqual(
            failure_fingerprint(windows),
            failure_fingerprint(linux),
        )


if __name__ == '__main__':
    unittest.main()
