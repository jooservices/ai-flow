"""Domain events published by the scale_proof feature."""
from __future__ import annotations

from dataclasses import dataclass

from ...platform.events import Audit, DomainEvent, event


@event
@dataclass(frozen=True)
class ScaleProofRecorded(DomainEvent):
    type = "scale_proof.result_filed"
    owner = "scale_proof"
    aggregate = "scale_proof_run"
    aggregate_field = "run_id"

    run_id: int
    release: str
    actor: str
    passed: bool

    def audit(self) -> Audit:
        return Audit("aiflow-control", "scale-proof-result", "", self.actor,
                     "passed" if self.passed else "failed", {"run_id": self.run_id, "release": self.release})
