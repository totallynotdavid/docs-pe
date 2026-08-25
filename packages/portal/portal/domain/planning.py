from __future__ import annotations

from datetime import timedelta

from fetch.domain.types import Doc
from fetch.sites.registry import SITES, STABLE_SITES

from portal.domain.errors import Reason, SourceValidationError
from portal.domain.models import (
    ExcludedInput,
    InputLine,
    PlannedItem,
    SubmissionPlan,
    SubmissionReview,
)


# Reuse only successful or explicitly not-found answers.
SOURCE_FRESHNESS: dict[str, timedelta] = {
    "osiptel": timedelta(days=7),
    "sunat": timedelta(days=90),
    "sunat_reps": timedelta(days=30),
}


def plan_submission(
    lines: tuple[InputLine, ...],
    sources: tuple[str, ...],
) -> SubmissionPlan:
    """Route valid documents and retain excluded input."""
    _validate_sources(sources)

    items: list[PlannedItem] = []
    exclusions: list[ExcludedInput] = []
    seen: set[str] = set()

    for line in lines:
        try:
            doc = Doc(line.value)
        except ValueError:
            exclusions.append(
                ExcludedInput(
                    line.ordinal,
                    line.value,
                    "invalid_document",
                )
            )
            continue

        document = str(doc)

        if document in seen:
            exclusions.append(
                ExcludedInput(
                    line.ordinal,
                    document,
                    "duplicate_document",
                )
            )
            continue

        seen.add(document)

        accepted_sources = [source for source in sources if SITES[source].accepts(doc)]

        if not accepted_sources:
            exclusions.append(
                ExcludedInput(
                    line.ordinal,
                    document,
                    "no_compatible_source",
                )
            )
            continue

        items.extend(
            PlannedItem(line.ordinal, document, source) for source in accepted_sources
        )

    return SubmissionPlan(tuple(items), tuple(exclusions))


def build_review(
    plan: SubmissionPlan,
    reusable_pairs: frozenset[tuple[str, str]],
) -> SubmissionReview:
    """Mark which planned items this team can reuse."""
    reusable = tuple(
        item for item in plan.items if (item.document, item.source) in reusable_pairs
    )

    return SubmissionReview(
        items=plan.items,
        exclusions=plan.exclusions,
        reusable=reusable,
    )


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
