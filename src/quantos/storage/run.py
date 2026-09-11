"""Canonical append-only Run Manifest persistence, without business payloads."""

from dataclasses import asdict
from datetime import date, datetime
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from quantos.config import Settings
from quantos.schemas.run import (
    ArtifactReference, KNOWLEDGE_RUN_SCHEMA_VERSION, KnowledgeOperationalState,
    KnowledgeOperationalStatus, ModuleAvailabilityStatus, ModuleExecutionResult,
    ModuleExecutionStatus, ModuleName, ModulePlanEntry, QuantOSRunManifest,
    RUN_MANIFEST_SCHEMA_VERSIONS, RUN_SCHEMA_VERSION, RunContext, RunSummary,
    RunType,
)
from .market import StorageError


def manifest_record(manifest: QuantOSRunManifest) -> dict:
    record = asdict(manifest)
    if manifest.knowledge_state is None:
        record.pop("knowledge_state")
    else:
        record["knowledge_state"]["state_id"] = manifest.knowledge_state.state_id
    record["run_context"]["run_id"] = manifest.run_context.run_id
    record["provenance"] = dict(manifest.provenance)
    # Encode date types explicitly; all other fields are closed dataclasses/enums.
    return json.loads(json.dumps(record, default=_date_json))


def _date_json(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError("unsupported manifest value")


def manifest_from_record(record: dict) -> QuantOSRunManifest:
    if not isinstance(record, dict):
        raise ValueError("invalid manifest fields")
    schema_version = record.get("schema_version")
    if schema_version not in RUN_MANIFEST_SCHEMA_VERSIONS:
        raise ValueError("unsupported manifest schema")
    fields = {
        "schema_version", "run_context", "plan", "execution_results",
        "summary", "provenance",
    }
    if schema_version == KNOWLEDGE_RUN_SCHEMA_VERSION:
        fields.add("knowledge_state")
    if set(record) != fields:
        raise ValueError("invalid manifest fields")
    values = dict(record["run_context"])
    identity = values.pop("run_id")
    for name in ("target_trade_date", "market_basis_trade_date"):
        values[name] = date.fromisoformat(values[name]) if values[name] else None
    for name in ("as_of_time", "generated_at"):
        values[name] = datetime.fromisoformat(values[name])
    values["run_type"] = RunType(values["run_type"])
    context = RunContext(**values)
    if context.run_id != identity:
        raise ValueError("run identity mismatch")
    plan = tuple(ModulePlanEntry(
        module=ModuleName(x["module"]), availability=ModuleAvailabilityStatus(x["availability"]),
        basis_trade_date=date.fromisoformat(x["basis_trade_date"]) if x["basis_trade_date"] else None,
        reason_code=x["reason_code"], dependencies=tuple(ModuleName(d) for d in x["dependencies"]),
        soft_dependencies=tuple(ModuleName(d) for d in x["soft_dependencies"]), will_execute=x["will_execute"],
    ) for x in record["plan"])
    results = tuple(ModuleExecutionResult(
        module=ModuleName(x["module"]), status=ModuleExecutionStatus(x["status"]),
        reason_code=x["reason_code"],
        started_at=datetime.fromisoformat(x["started_at"]) if x["started_at"] else None,
        finished_at=datetime.fromisoformat(x["finished_at"]) if x["finished_at"] else None,
        duration_ms=x["duration_ms"],
        artifact_refs=tuple(ArtifactReference(**ref) for ref in x["artifact_refs"]),
        safe_error_code=x["safe_error_code"],
    ) for x in record["execution_results"])
    if tuple(x.module for x in plan) != tuple(ModuleName) or tuple(x.module for x in results) != tuple(ModuleName):
        raise ValueError("invalid manifest module order")
    knowledge_state = None
    if schema_version == KNOWLEDGE_RUN_SCHEMA_VERSION:
        state_record = dict(record["knowledge_state"])
        state_fields = {
            "status", "reason_code", "product_ref", "source_report_id",
            "candidate_count", "ready_candidate_count", "empty_candidate_count",
            "schema_version", "state_id",
        }
        if set(state_record) != state_fields:
            raise ValueError("invalid Knowledge operational state fields")
        state_identity = state_record.pop("state_id")
        reference = state_record.pop("product_ref")
        knowledge_state = KnowledgeOperationalState(
            status=KnowledgeOperationalStatus(state_record.pop("status")),
            product_ref=ArtifactReference(**reference) if reference is not None else None,
            **state_record,
        )
        if knowledge_state.state_id != state_identity:
            raise ValueError("Knowledge operational state identity mismatch")
    value = QuantOSRunManifest(
        schema_version, context, plan, results, RunSummary(**record["summary"]),
        tuple(record["provenance"].items()), knowledge_state,
    )
    # Fail closed on unknown nested fields instead of silently discarding payloads.
    if json.dumps(manifest_record(value), sort_keys=True) != json.dumps(record, sort_keys=True):
        raise ValueError("noncanonical manifest fields")
    plan_only = dict(value.provenance).get("execution_mode") == "PLAN_ONLY"
    for entry, result in zip(plan, results):
        ready = entry.availability == ModuleAvailabilityStatus.READY
        attempted = result.status != ModuleExecutionStatus.SKIP
        if type(entry.will_execute) is not bool or entry.will_execute != ready:
            raise ValueError("manifest plan execution flag disagrees with availability")
        if attempted != (ready and not plan_only):
            raise ValueError("manifest execution disagrees with plan")
    counts = {
        "planned_modules": len(plan),
        "executed_modules": sum(x.status != ModuleExecutionStatus.SKIP for x in results),
        "passed_modules": sum(x.status == ModuleExecutionStatus.PASS for x in results),
        "skipped_modules": sum(x.status == ModuleExecutionStatus.SKIP for x in results),
        "failed_modules": sum(x.status == ModuleExecutionStatus.FAIL for x in results),
    }
    if any(type(getattr(value.summary, key)) is not int or getattr(value.summary, key) != count
           for key, count in counts.items()):
        raise ValueError("manifest summary disagrees with execution")
    report_passed = results[-1].status == ModuleExecutionStatus.PASS
    if (bool(value.summary.report_id) != report_passed
        or (value.summary.report_generated + value.summary.report_reused) != int(report_passed)):
        raise ValueError("manifest report summary disagrees with execution")
    required_product_module = (
        ModuleName.DAILY_REPORT
        if context.run_type is RunType.POST_CLOSE
        else ModuleName.ANOMALY_TRIAGE
    )
    required_product_passed = any(
        result.module is required_product_module
        and result.status == ModuleExecutionStatus.PASS
        for result in results
    )
    if (
        knowledge_state is not None
        and knowledge_state.is_hard_failure
        and required_product_passed
    ):
        raise ValueError("hard Knowledge state contradicts product execution")
    if (
        knowledge_state is not None
        and not knowledge_state.is_hard_failure
        and not any(
            result.module is required_product_module
            and result.status == ModuleExecutionStatus.PASS
            and knowledge_state.product_ref in result.artifact_refs
            for result in results
        )
    ):
        raise ValueError("Knowledge product reference was not validated by execution")
    return value


class RunRepository:
    def __init__(self, settings: Settings):
        self.root = settings.data_root / "derived" / "runs"

    def write(self, manifest: QuantOSRunManifest) -> Path:
        temporary = None
        lock = None
        owns_lock = False
        try:
            record = manifest_record(manifest)
            manifest_from_record(record)
            directory = self.root / f"target_trade_date={manifest.run_context.target_trade_date}"
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{manifest.run_context.run_id}-{uuid4().hex}.json"
            # Exclusive reservation also protects against a generation-id collision.
            lock = target.with_suffix(".lock")
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            owns_lock = True
            os.close(descriptor)
            if target.exists():
                raise FileExistsError()
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             prefix=".manifest-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(record, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, target)
            return target
        except Exception:
            raise StorageError("failed to persist run manifest") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if owns_lock:
                lock.unlink(missing_ok=True)

    def read(self, path: Path) -> QuantOSRunManifest:
        try:
            return manifest_from_record(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            raise StorageError("failed to read run manifest") from None
