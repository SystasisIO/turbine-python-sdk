"""Typed wire models for the TurbineOS service package catalog."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    ValidationInfo,
    field_validator,
    model_validator,
)

from ._scalars import Identifier, Sha256Digest
from .graph import _require_json

CATALOG_DEFAULT_LIMIT = 100
CATALOG_MAX_LIMIT = 200
CatalogLimit = Annotated[int, Field(ge=1, le=CATALOG_MAX_LIMIT)]
CatalogToken = Annotated[Identifier, Field(pattern=r"^[^\x00]+$")]

_IDENTIFIER_PATTERN = r"^[A-Za-z0-9_.-]+$"
_DescriptorId = Annotated[str, Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)]
_ManifestId = Annotated[Identifier, Field(pattern=_IDENTIFIER_PATTERN)]
_AccountName = Annotated[str, Field(pattern=r"^(?:[.1-5a-z]{0,11}[1-5a-z]|[.1-5a-z]{12}[1-5a-j])$")]
_UtcTimestamp = Annotated[
    str,
    Field(
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$",
        json_schema_extra={"format": "date-time"},
    ),
]
# Pydantic 2.6 treats a validator's second positional argument as ValidationInfo.
_JsonSchema = Annotated[dict[str, JsonValue], BeforeValidator(lambda value: _require_json(value))]
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACE_OR_CONTROL = re.compile(r"[\x00-\x20\x7f]")


class _CatalogModel(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        strict=True,
        revalidate_instances="always",
        protected_namespaces=("model_validate", "model_dump"),
    )


def _timestamp_key(value: str) -> tuple[str, str]:
    # Keep sub-microsecond digits when ordering UTC timestamps.
    return value[:19], value[20:-1].rstrip("0")


class CatalogSnapshot(_CatalogModel):
    """A revision and its validity window, with UTC timestamps in wire form."""

    revision: CatalogToken
    created_at: _UtcTimestamp
    expires_at: _UtcTimestamp

    @field_validator("created_at", "expires_at")
    @classmethod
    def _calendar_date(cls, value: str) -> str:
        datetime.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def _valid_window(self) -> Self:
        if _timestamp_key(self.expires_at) <= _timestamp_key(self.created_at):
            raise ValueError("expires_at must be later than created_at")
        return self


class PackageRef(_CatalogModel):
    """The immutable release identified by the digest of its descriptor."""

    id: _DescriptorId
    version: _DescriptorId
    digest: Sha256Digest


class ModelDescriptor(_CatalogModel):
    """A canonical model with a version when its provider guarantees one."""

    id: _DescriptorId
    display_name: Annotated[str, Field(min_length=1, max_length=256)]
    version: Annotated[str, Field(min_length=1, max_length=128)] | None

    @field_validator("display_name", "version")
    @classmethod
    def _model_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None:
            max_bytes = 256 if info.field_name == "display_name" else 128
            if (
                len(value.encode("utf-8")) > max_bytes
                or not value.strip(" ")
                or _CONTROL.search(value)
            ):
                raise ValueError(
                    "Model text must be nonblank, bounded UTF-8 without control characters"
                )
        return value


def _reject_variant_fields(value: Any, *fields: str) -> Any:
    if isinstance(value, Mapping) and any(field in value for field in fields):
        raise ValueError("A field belongs to a different catalog variant")
    return value


class _Binding(_CatalogModel):
    id: _ManifestId
    input_schema: _JsonSchema


class ComfyBinding(_Binding):
    """A Comfy node class with a selected, fixed, or absent model."""

    protocol: Literal["comfy"]
    class_type: Annotated[Identifier, Field(pattern=r"^[^\x00-\x20\x7f]+$")]
    model_id: _DescriptorId | None
    model_from_input: bool

    @model_validator(mode="before")
    @classmethod
    def _no_openai_fields(cls, value: Any) -> Any:
        return _reject_variant_fields(value, "api")

    @field_validator("class_type")
    @classmethod
    def _class_type_size(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 256:
            raise ValueError("Class type must fit in 256 UTF-8 bytes")
        return value

    @model_validator(mode="after")
    def _model_selection(self) -> Self:
        if self.model_from_input and self.model_id is None:
            raise ValueError("A model selected from input must have a model_id")
        return self


class OpenAIBinding(_Binding):
    """An OpenAI protocol operation with a required model selector."""

    protocol: Literal["openai"]
    api: Literal["chat.completions", "images.generations"]
    model_id: _DescriptorId

    @model_validator(mode="before")
    @classmethod
    def _no_comfy_fields(cls, value: Any) -> Any:
        return _reject_variant_fields(value, "class_type", "model_from_input")


Binding = Annotated[ComfyBinding | OpenAIBinding, Field(discriminator="protocol")]


class Operation(_CatalogModel):
    """A package operation and the protocol bindings visible in the snapshot."""

    id: _ManifestId
    bindings: Annotated[list[Binding], Field(min_length=1)]
    output_schema: _JsonSchema

    @model_validator(mode="after")
    def _unique_binding_ids(self) -> Self:
        if len({binding.id for binding in self.bindings}) != len(self.bindings):
            raise ValueError("Binding IDs must be unique within an operation")
        return self


class UnsignedAuthor(_CatalogModel):
    """A release admitted by exact digest without an author signature."""

    status: Literal["unsigned"]

    @model_validator(mode="before")
    @classmethod
    def _no_verified_fields(cls, value: Any) -> Any:
        return _reject_variant_fields(value, "chain_id", "account", "verified_at_block_id")


class VerifiedAuthor(_CatalogModel):
    """An author whose authority was verified at a finalized Spine block."""

    status: Literal["verified"]
    chain_id: Sha256Digest
    account: _AccountName
    verified_at_block_id: Sha256Digest

    @field_validator("chain_id")
    @classmethod
    def _nonzero_chain_id(cls, value: str) -> str:
        if value == "0" * 64:
            raise ValueError("Author chain ID must be nonzero")
        return value


Author = Annotated[UnsignedAuthor | VerifiedAuthor, Field(discriminator="status")]


class Support(_CatalogModel):
    """Contact channels recorded in the immutable release descriptor."""

    url: Annotated[str, Field(min_length=1, max_length=2048)] | None
    email: Annotated[str, Field(min_length=1, max_length=254)] | None

    @field_validator("url")
    @classmethod
    def _https_url(cls, value: str | None) -> str | None:
        if value is not None:
            parsed = urlsplit(value)
            if (
                len(value.encode("utf-8")) > 2048
                or _SPACE_OR_CONTROL.search(value)
                or parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
            ):
                raise ValueError("Support URL must be an absolute HTTPS URL without credentials")
        return value

    @field_validator("email")
    @classmethod
    def _email_address(cls, value: str | None) -> str | None:
        if value is not None:
            local, _, domain = value.partition("@")
            if (
                len(value.encode("utf-8")) > 254
                or not local
                or not domain
                or "@" in domain
                or re.search(r'[\x00-\x20\x7f<>:,;"()\\]', value)
            ):
                raise ValueError("Support email must be a single email address")
        return value

    @model_validator(mode="after")
    def _has_contact(self) -> Self:
        if self.url is None and self.email is None:
            raise ValueError("Support must contain a contact channel")
        return self


class Package(_CatalogModel):
    """A visible release with the operations and models admitted for this snapshot."""

    reference: PackageRef
    descriptor_format: Literal["turbine::compute-package/1.1"]
    models: list[ModelDescriptor]
    operations: Annotated[list[Operation], Field(min_length=1)]
    author: Author
    support: Support | None

    @model_validator(mode="after")
    def _identities(self) -> Self:
        model_ids = {model.id for model in self.models}
        if len(model_ids) != len(self.models):
            raise ValueError("Model IDs must be unique within a package")
        if len({operation.id for operation in self.operations}) != len(self.operations):
            raise ValueError("Operation IDs must be unique within a package")
        bound_model_ids = {
            binding.model_id
            for operation in self.operations
            for binding in operation.bindings
            if binding.model_id is not None
        }
        if bound_model_ids != model_ids:
            raise ValueError("Package models must match the models referenced by its bindings")
        return self


class PackagePage(_CatalogModel):
    """One ordered page of the requested catalog revision."""

    catalog_revision: CatalogToken
    data: Annotated[list[Package], Field(max_length=CATALOG_MAX_LIMIT)]
    next_cursor: CatalogToken | None
    has_more: bool

    @model_validator(mode="after")
    def _pagination(self) -> Self:
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("has_more must match the presence of next_cursor")
        if self.has_more and not self.data:
            raise ValueError("An empty page cannot have a continuation")
        digests = [package.reference.digest for package in self.data]
        if any(left >= right for left, right in zip(digests, digests[1:])):
            raise ValueError("Package digests must be unique and sorted in ascending order")
        return self


__all__ = [
    "Author",
    "Binding",
    "CatalogSnapshot",
    "ComfyBinding",
    "ModelDescriptor",
    "OpenAIBinding",
    "Operation",
    "Package",
    "PackagePage",
    "PackageRef",
    "Support",
    "UnsignedAuthor",
    "VerifiedAuthor",
]
