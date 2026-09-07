"""Shared product-adapter contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from hoh.models import CheckBundle, Diagnostic


@dataclass(frozen=True)
class AdapterContext:
    """Candidate project and host-owned destination for adapter records."""

    project: Path
    output: Path


class ProductAdapter(Protocol):
    """Runs deterministic checks against an isolated product candidate."""

    def doctor(self, project: Path) -> tuple[Diagnostic, ...]: ...

    def summarize(self, project: Path) -> dict[str, object]: ...

    def baseline(
        self, context: AdapterContext, plan: dict[str, object]
    ) -> CheckBundle: ...

    def check(self, context: AdapterContext, plan: dict[str, object]) -> CheckBundle: ...

    def collect(
        self, context: AdapterContext, bundle: CheckBundle
    ) -> dict[str, str]: ...
