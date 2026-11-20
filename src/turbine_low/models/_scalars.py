"""Wire field constraints shared by Turbine extensions."""

from typing import Annotated

from pydantic import Field

Identifier = Annotated[str, Field(min_length=1, max_length=256)]
Sha256Digest = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
CanonicalUuid = Annotated[
    str,
    Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    ),
]
ExecutionTargets = Annotated[list[Identifier], Field(min_length=1, max_length=4096)]
