from __future__ import annotations

from fetch.domain.types import Doc
from fetch.sites.registry import SITES, STABLE_SITES

from portal.domain.errors import Reason, SourceValidationError
from portal.domain.models import (
    ExcludedInput,
    InputLine,
    PlannedItem,
    SubmissionPlan,
)


def plan_submission(
    lines: tuple[InputLine, ...], sources: tuple[str, ...]
) -> SubmissionPlan:
    """Route valid documents to stable fetch adapters and retain exclusions.

    The fetch ``Site.accepts`` function remains the authority for source eligibility.
    Invalid or duplicate input is represented as an exclusion instead of being lost.
    """
    _validate_sources(sources)
    items: list[PlannedItem] = []
    exclusions: list[ExcludedInput] = []
    seen: set[str] = set()
    for line in lines:
        try:
            document = str(Doc(line.value))
        except ValueError:
            exclusions.append(
                ExcludedInput(line.ordinal, line.value, "documento_invalido")
            )
            continue
        if document in seen:
            exclusions.append(
                ExcludedInput(line.ordinal, document, "documento_duplicado")
            )
            continue
        seen.add(document)
        accepted_sources = [
            source for source in sources if SITES[source].accepts(Doc(document))
        ]
        if not accepted_sources:
            exclusions.append(
                ExcludedInput(line.ordinal, document, "sin_fuente_compatible")
            )
            continue
        items.extend(
            PlannedItem(line.ordinal, document, source) for source in accepted_sources
        )
    return SubmissionPlan(tuple(items), tuple(exclusions))


def _validate_sources(sources: tuple[str, ...]) -> None:
    if not sources:
        raise SourceValidationError(Reason.SOURCE_REQUIRED)
    if len(set(sources)) != len(sources):
        raise SourceValidationError(Reason.SOURCE_DUPLICATED)
    invalid = sorted(set(sources).difference(STABLE_SITES))
    if invalid:
        raise SourceValidationError(
            Reason.SOURCE_NOT_ENABLED,
            invalid=", ".join(invalid),
            allowed=", ".join(sorted(STABLE_SITES)),
        )
