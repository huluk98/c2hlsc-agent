from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable


RUN_LEDGER_FILENAME = 'run_ledger.jsonl'
RUN_LEDGER_SCHEMA_VERSION = 1


class RunStatus(str, Enum):
    RUNNING = 'running'
    PASSED = 'passed'
    FAILED = 'failed'
    BLOCKED = 'blocked'
    EXHAUSTED = 'exhausted'
    CANCELLED = 'cancelled'


CLOSED_STATUSES = {RunStatus.PASSED, RunStatus.EXHAUSTED, RunStatus.CANCELLED}


class RunControlError(RuntimeError):
    '''Base exception for persistent run-controller failures.'''


class RunBudgetExceeded(RunControlError):
    '''Raised before work that would exceed a configured run budget.'''

    def __init__(self, resource: str, message: str) -> None:
        super().__init__(message)
        self.resource = resource


class RunClosed(RunControlError):
    '''Raised when a closed run id is reused without an intentional reset.'''


@dataclass(frozen=True)
class RunBudget:
    max_attempts: int
    max_wall_seconds: int = 14_400
    max_llm_calls: int = 8
    max_vitis_runs: int = 8

    def __post_init__(self) -> None:
        for name, value in self.to_dict().items():
            if value < 1:
                raise ValueError(f'{name} must be at least 1')

    def to_dict(self) -> dict[str, int]:
        return {
            'max_attempts': self.max_attempts,
            'max_wall_seconds': self.max_wall_seconds,
            'max_llm_calls': self.max_llm_calls,
            'max_vitis_runs': self.max_vitis_runs,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'RunBudget':
        return cls(
            max_attempts=int(data['max_attempts']),
            max_wall_seconds=int(data['max_wall_seconds']),
            max_llm_calls=int(data['max_llm_calls']),
            max_vitis_runs=int(data['max_vitis_runs']),
        )


@dataclass
class RunUsage:
    attempts: int = 0
    llm_calls: int = 0
    vitis_runs: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            'attempts': self.attempts,
            'llm_calls': self.llm_calls,
            'vitis_runs': self.vitis_runs,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'RunUsage':
        return cls(
            attempts=int(data.get('attempts', 0)),
            llm_calls=int(data.get('llm_calls', 0)),
            vitis_runs=int(data.get('vitis_runs', 0)),
        )


@dataclass
class RunRecord:
    run_id: str
    identity_fingerprint: str
    status: RunStatus
    budget: RunBudget
    usage: RunUsage
    started_at: str
    updated_at: str
    reason: str = ''
    source_fingerprint: str | None = None
    failure_fingerprint: str | None = None
    seen_states: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            'schema_version': RUN_LEDGER_SCHEMA_VERSION,
            'run_id': self.run_id,
            'identity_fingerprint': self.identity_fingerprint,
            'status': self.status.value,
            'budget': self.budget.to_dict(),
            'usage': self.usage.to_dict(),
            'started_at': self.started_at,
            'updated_at': self.updated_at,
            'reason': self.reason,
            'source_fingerprint': self.source_fingerprint,
            'failure_fingerprint': self.failure_fingerprint,
            'seen_states': dict(self.seen_states),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'RunRecord':
        if int(data.get('schema_version', 0)) != RUN_LEDGER_SCHEMA_VERSION:
            raise ValueError('unsupported run-ledger schema version')
        return cls(
            run_id=str(data['run_id']),
            identity_fingerprint=str(data.get('identity_fingerprint', '')),
            status=RunStatus(str(data['status'])),
            budget=RunBudget.from_dict(dict(data['budget'])),
            usage=RunUsage.from_dict(dict(data.get('usage', {}))),
            started_at=str(data['started_at']),
            updated_at=str(data['updated_at']),
            reason=str(data.get('reason', '')),
            source_fingerprint=(
                str(data['source_fingerprint'])
                if data.get('source_fingerprint') is not None
                else None
            ),
            failure_fingerprint=(
                str(data['failure_fingerprint'])
                if data.get('failure_fingerprint') is not None
                else None
            ),
            seen_states={
                str(key): int(value)
                for key, value in dict(data.get('seen_states', {})).items()
            },
        )


class RunLedger:
    '''Atomic JSONL event ledger for one output project.

    Each event contains a complete snapshot. Team coordination still follows a
    single-writer rule: one claimed issue, one branch, one output directory.
    '''

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(
        self,
        record: RunRecord,
        event: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = record.to_dict()
        payload['event'] = event
        payload['details'] = details or {}
        line = json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\n'
        previous = self.path.read_bytes() if self.path.exists() else b''
        if previous and not previous.endswith(b'\n'):
            raise ValueError(f'run ledger has an incomplete final event: {self.path}')
        temp = self.path.with_name(
            f'.{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp'
        )
        try:
            with temp.open('xb') as handle:
                handle.write(previous)
                handle.write(line.encode('utf-8'))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            if temp.exists():
                temp.unlink()

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        text = self.path.read_text(encoding='utf-8')
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f'invalid run-ledger event at line {line_number}: {exc}'
                ) from exc
            if not isinstance(event, dict):
                raise ValueError(
                    f'run-ledger event at line {line_number} is not an object'
                )
            events.append(event)
        return events

    def latest(self, run_id: str | None = None) -> RunRecord | None:
        selected: dict[str, Any] | None = None
        for event in self.events():
            if run_id is None or event.get('run_id') == run_id:
                selected = event
        return RunRecord.from_dict(selected) if selected is not None else None


class RunController:
    '''Persistent finite-state controller around finite worker attempts.'''

    def __init__(
        self,
        project_dir: Path,
        run_id: str,
        budget: RunBudget,
        identity_fingerprint: str,
    ) -> None:
        validate_run_id(run_id)
        self.ledger = RunLedger(project_dir / RUN_LEDGER_FILENAME)
        latest = self.ledger.latest(run_id)
        if latest is None:
            now = utc_now()
            self.record = RunRecord(
                run_id=run_id,
                identity_fingerprint=identity_fingerprint,
                status=RunStatus.RUNNING,
                budget=budget,
                usage=RunUsage(),
                started_at=now,
                updated_at=now,
            )
            self.ledger.append(self.record, 'run_started')
            return
        if latest.identity_fingerprint != identity_fingerprint:
            raise RunClosed(
                f'run id {run_id!r} belongs to different inputs; '
                'choose --new-run or another --run-id'
            )
        if latest.budget != budget:
            raise RunClosed(
                f'budgets for run id {run_id!r} are immutable; '
                'use --new-run to change them'
            )
        if latest.status in CLOSED_STATUSES:
            raise RunClosed(
                f'run {run_id!r} is already {latest.status.value}; '
                'inspect it with the status command or pass --new-run'
            )
        self.record = latest
        self.record.status = RunStatus.RUNNING
        self.record.reason = ''
        self._touch()
        self.ledger.append(self.record, 'run_resumed')

    def _touch(self) -> None:
        self.record.updated_at = utc_now()

    def _ensure_running(self) -> None:
        if self.record.status != RunStatus.RUNNING:
            raise RunClosed(
                f'run {self.record.run_id!r} is {self.record.status.value}'
            )

    def _check_wall_budget(self) -> None:
        elapsed = elapsed_seconds(self.record.started_at)
        if elapsed >= self.record.budget.max_wall_seconds:
            self._deny(
                'wall_seconds',
                f'wall-time budget exhausted '
                f'({elapsed}/{self.record.budget.max_wall_seconds} seconds)'
            )

    def _deny(self, resource: str, reason: str) -> None:
        self._touch()
        self.ledger.append(
            self.record,
            'budget_denied',
            {'resource': resource, 'reason': reason},
        )
        raise RunBudgetExceeded(resource, reason)

    def reserve_attempt(self, source_fingerprint: str) -> None:
        self._ensure_running()
        self._check_wall_budget()
        used = self.record.usage.attempts
        limit = self.record.budget.max_attempts
        if used >= limit:
            self._deny(
                'attempts',
                f'verification-attempt budget exhausted ({used}/{limit})',
            )
        self.record.usage.attempts += 1
        self.record.source_fingerprint = source_fingerprint
        self._touch()
        self.ledger.append(self.record, 'attempt_reserved')

    def reserve_llm_call(self, purpose: str) -> None:
        self._ensure_running()
        self._check_wall_budget()
        used = self.record.usage.llm_calls
        limit = self.record.budget.max_llm_calls
        if used >= limit:
            self._deny(
                'llm_calls',
                f'LLM-call budget exhausted ({used}/{limit})',
            )
        self.record.usage.llm_calls += 1
        self._touch()
        self.ledger.append(
            self.record,
            'llm_call_reserved',
            {'purpose': purpose},
        )

    def reserve_vitis_run(self) -> None:
        self._ensure_running()
        self._check_wall_budget()
        used = self.record.usage.vitis_runs
        limit = self.record.budget.max_vitis_runs
        if used >= limit:
            self._deny(
                'vitis_runs',
                f'Vitis-run budget exhausted ({used}/{limit})',
            )
        self.record.usage.vitis_runs += 1
        self._touch()
        self.ledger.append(self.record, 'vitis_run_reserved')

    def record_activity(
        self,
        event: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._ensure_running()
        self._touch()
        self.ledger.append(self.record, event, details)

    def record_verification(
        self,
        source_fingerprint: str,
        failure: str | None,
    ) -> int:
        self._ensure_running()
        self.record.source_fingerprint = source_fingerprint
        self.record.failure_fingerprint = failure
        repeat_count = 0
        if failure is not None:
            state_key = stable_fingerprint(
                {'source': source_fingerprint, 'failure': failure}
            )
            repeat_count = self.record.seen_states.get(state_key, 0) + 1
            self.record.seen_states[state_key] = repeat_count
        self._touch()
        self.ledger.append(
            self.record,
            'verification_recorded',
            {'passed': failure is None, 'repeat_count': repeat_count},
        )
        return repeat_count

    def finish(self, status: RunStatus, reason: str) -> None:
        if status == RunStatus.RUNNING:
            raise ValueError('finish requires a terminal status')
        if self.record.status in CLOSED_STATUSES:
            if self.record.status == status:
                return
            raise RunClosed(
                f'run {self.record.run_id!r} is already {self.record.status.value}'
            )
        self.record.status = status
        self.record.reason = reason
        self._touch()
        self.ledger.append(self.record, 'run_finished')

    def snapshot(self) -> dict[str, Any]:
        return snapshot_for_record(self.record, self.ledger.path)


class BudgetedLLMClient:
    '''Count model calls without storing prompts, responses, keys, or endpoints.'''

    def __init__(
        self,
        delegate: Any,
        controller: RunController,
        purpose: str = 'conversion',
    ) -> None:
        self._delegate = delegate
        self._controller = controller
        self._purpose = purpose
        self.model = delegate.model

    @property
    def remaining_llm_calls(self) -> int:
        """Unspent model-call budget. The failure_analyst refinement checks this and
        stands down when fewer than two calls remain, so an optional classification
        never eats the final call the repair itself needs."""

        record = self._controller.record
        return max(0, record.budget.max_llm_calls - record.usage.llm_calls)

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 4096,
    ) -> str:
        self._controller.reserve_llm_call(self._purpose)
        try:
            response = self._delegate.complete(
                system,
                user,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            self._controller.record_activity(
                'llm_call_failed',
                {'error_type': type(exc).__name__},
            )
            raise
        self._controller.record_activity(
            'llm_call_completed',
            {'purpose': self._purpose},
        )
        return response


def utc_now() -> str:
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    return now.replace('+00:00', 'Z')


def elapsed_seconds(started_at: str) -> int:
    started = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))


def snapshot_for_record(record: RunRecord, ledger_path: Path) -> dict[str, Any]:
    elapsed = elapsed_seconds(record.started_at)
    payload = record.to_dict()
    payload['ledger_file'] = ledger_path.name
    payload['elapsed_seconds'] = elapsed
    payload['remaining'] = {
        'attempts': max(
            0,
            record.budget.max_attempts - record.usage.attempts,
        ),
        'wall_seconds': max(
            0,
            record.budget.max_wall_seconds - elapsed,
        ),
        'llm_calls': max(
            0,
            record.budget.max_llm_calls - record.usage.llm_calls,
        ),
        'vitis_runs': max(
            0,
            record.budget.max_vitis_runs - record.usage.vitis_runs,
        ),
    }
    return payload


def validate_run_id(run_id: str) -> None:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', run_id):
        raise ValueError(
            'run id must be 1-80 characters using letters, numbers, dots, '
            'underscores, or hyphens'
        )


def stable_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def files_fingerprint(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    ordered = sorted((Path(item) for item in paths), key=lambda item: item.name)
    for path in ordered:
        digest.update(path.name.encode('utf-8'))
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def derive_run_id(identity: dict[str, Any]) -> str:
    return f'run-{stable_fingerprint(identity)[:16]}'


def fresh_run_id(base_run_id: str) -> str:
    validate_run_id(base_run_id)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return f'{base_run_id[:48]}-{stamp}-{uuid.uuid4().hex[:6]}'


def _normalize_failure_evidence(text: str) -> str:
    normalized = (text or '').replace('\r\n', '\n')
    normalized = re.sub(
        r'\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+Z?\b',
        '<timestamp>',
        normalized,
    )
    normalized = re.sub(r'\b0x[0-9a-fA-F]+\b', '<address>', normalized)
    normalized = re.sub(
        r'(?i)\b[A-Z]:[\\/][^\s\' ]+',
        '<path>',
        normalized,
    )
    normalized = re.sub(
        r'(?<!\w)/(?:[^/\s]+/)+[^\s\' ]+',
        '<path>',
        normalized,
    )
    normalized = '\n'.join(
        ' '.join(line.split()) for line in normalized.splitlines()
    )
    return normalized.strip()[-4000:]


def failure_fingerprint(state: Any) -> str:
    phases: list[dict[str, Any]] = []
    for name, result in sorted(getattr(state, 'phases', {}).items()):
        status = str(getattr(result, 'status', 'unknown'))
        if status in {'pass', 'skipped'}:
            continue
        evidence = '\n'.join(
            str(item)
            for item in (
                getattr(result, 'summary', ''),
                getattr(result, 'stderr', ''),
                getattr(result, 'stdout', ''),
            )
            if item
        )
        phases.append(
            {
                'name': name,
                'status': status,
                'returncode': getattr(result, 'returncode', None),
                'evidence': _normalize_failure_evidence(evidence),
            }
        )
    mismatches = [
        mismatch.to_dict() if hasattr(mismatch, 'to_dict') else str(mismatch)
        for mismatch in getattr(state, 'mismatches', [])
    ]
    return stable_fingerprint({'phases': phases, 'mismatches': mismatches})
