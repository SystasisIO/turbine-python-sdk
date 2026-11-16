"""Wire field constraints shared by the package catalog and pricing preview."""

from typing import Annotated

from pydantic import Field

Identifier = Annotated[str, Field(min_length=1, max_length=256)]
Sha256Digest = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
