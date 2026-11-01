"""Parse dependency declarations and project them into installer requirements."""

from __future__ import annotations

from dataclasses import dataclass

from packaging.requirements import Requirement

IMPORT_SEPARATOR = "|"


def split_requirement(value: str) -> tuple[str, str | None]:
    """Separate an optional import name without rewriting the installer argument."""
    specification, separator, origin = value.partition(IMPORT_SEPARATOR)
    return specification, origin.strip() if separator else None


@dataclass(frozen=True)
class DependencySpec:
    """Keep a parsed Python requirement and its original pip and session-cache key."""

    specification: str
    requirement: Requirement
    import_name: str

    @classmethod
    def parse(cls, value: str) -> DependencySpec:
        """Parse ish's import override while delegating PEP 508 to packaging."""
        specification, origin = split_requirement(value)
        requirement = Requirement(specification.strip())
        return cls(specification, requirement, origin or requirement.name)

    @property
    def active(self) -> bool:
        """Evaluate the requirement marker against the current environment."""
        marker = self.requirement.marker
        return marker is None or marker.evaluate()

    @property
    def constraint(self) -> str | None:
        """Project an active requirement into a pip constraint without extras."""
        if not self.active:
            return None
        requirement = self.requirement
        if requirement.url:
            return f"{requirement.name} @ {requirement.url}"
        return f"{requirement.name}{requirement.specifier}"
