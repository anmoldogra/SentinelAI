"""forensics background jobs — arq (guide Part 12).

**No acquisition parser is implemented, and this module says so rather than pretending.**

`event-driven-architecture.md` §25.5 triggers `forensics.artifact_processed` when "artifact
parsing/normalization completes", and the two halves of that sentence are built to different depths.
*Normalization* — mapping the registered record onto the canonical evidence model — is built, runs
synchronously on `POST /forensics/artifacts/{id}/publish` (api-design.md §4.5), and publishes the
event from there. *Parsing* — opening an E01 image, walking an Oxygen extraction, decoding a
`registry_hive` into per-artifact rows — is not: each format needs its own parser, and any one of
them is an increment in itself.

Raising a named error is the honest behaviour for that state. A failing job dead-letters where an
operator sees it (event-driven §15), which is strictly better than the alternative: stamping an
artifact `processed` and publishing `artifact_processed` for content nobody extracted would tell an
examiner their disk image had been analysed when the platform had not read a byte of it. In a domain
where the output is evidence, a silent no-op is the worst available outcome.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sentinelai.platform.logging import log


class ArtifactParserNotConfigured(RuntimeError):
    """No parser exists for the requested artifact's acquisition format.

    Named rather than a bare ``NotImplementedError`` so the dead-letter reason an operator reads
    names the actual cause — a missing format parser, not a broken job — and so it is
    distinguishable from a parser that exists and failed on a corrupt image. Those need different
    responses: one is a feature that is not built, the other is evidence that may be damaged.
    """


async def process_artifact(ctx: dict[str, Any], artifact_id: UUID) -> None:
    """Parse a registered artifact's acquisition container into per-artifact rows.

    The publish path needs none of this: an examiner who has already extracted the attributes their
    tool produced states them in `device_info` and publishes directly. This job is for the case
    where the platform itself must open the container, and that is what is unbuilt.
    """
    log.error(
        "artifact_parsing_unavailable",
        reason="no acquisition-format parser is built",
        artifact_id=str(artifact_id),
    )
    raise ArtifactParserNotConfigured(
        "no forensic acquisition parser is configured: disk-image and mobile-extraction parsers "
        "are not built. An artifact whose attributes are already extracted can be published "
        "through POST /api/v1/forensics/artifacts/{artifact_id}/publish in the meantime."
    )


__all__ = ["ArtifactParserNotConfigured", "process_artifact"]
