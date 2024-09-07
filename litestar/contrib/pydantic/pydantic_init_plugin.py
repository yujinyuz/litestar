from __future__ import annotations

from contextlib import suppress
from functools import partial
from inspect import isclass
from typing import TYPE_CHECKING, Any, Callable, TypeVar, cast
from uuid import UUID

from msgspec import ValidationError
from typing_extensions import Buffer, TypeGuard

from litestar._signature.types import ExtendedMsgSpecValidationError
from litestar.contrib.pydantic.utils import is_pydantic_constrained_field, is_pydantic_v2
from litestar.exceptions import MissingDependencyException
from litestar.params import BodyKwarg, ParameterKwarg, KwargDefinition
from litestar.plugins import InitPluginProtocol
from litestar.types import Empty
from litestar.typing import _KWARG_META_EXTRACTORS
from litestar.utils import is_class_and_subclass

try:
    import pydantic as _  # noqa: F401
except ImportError as e:
    raise MissingDependencyException("pydantic") from e

try:
    import pydantic as pydantic_v2

    if not is_pydantic_v2(pydantic_v2):
        raise ImportError

    from pydantic import v1 as pydantic_v1
except ImportError:
    import pydantic as pydantic_v1  # type: ignore[no-redef]

    pydantic_v2 = None  # type: ignore[assignment]


if TYPE_CHECKING:
    from litestar.config.app import AppConfig
    from litestar.types.serialization import PydanticV1FieldsListType, PydanticV2FieldsListType


T = TypeVar("T")


def _dec_pydantic_v1(model_type: type[pydantic_v1.BaseModel], value: Any) -> pydantic_v1.BaseModel:
    try:
        return model_type.parse_obj(value)
    except pydantic_v1.ValidationError as e:
        raise ExtendedMsgSpecValidationError(errors=cast("list[dict[str, Any]]", e.errors())) from e


def _dec_pydantic_v2(model_type: type[pydantic_v2.BaseModel], value: Any, strict: bool) -> pydantic_v2.BaseModel:
    try:
        return model_type.model_validate(value, strict=strict)
    except pydantic_v2.ValidationError as e:
        raise ExtendedMsgSpecValidationError(errors=cast("list[dict[str, Any]]", e.errors())) from e


def _dec_pydantic_uuid(
    uuid_type: type[pydantic_v1.UUID1] | type[pydantic_v1.UUID3] | type[pydantic_v1.UUID4] | type[pydantic_v1.UUID5],
    value: Any,
) -> (
    type[pydantic_v1.UUID1] | type[pydantic_v1.UUID3] | type[pydantic_v1.UUID4] | type[pydantic_v1.UUID5]
):  # pragma: no cover
    if isinstance(value, str):
        value = uuid_type(value)

    elif isinstance(value, Buffer):
        value = bytes(value)
        try:
            value = uuid_type(value.decode())
        except ValueError:
            # 16 bytes in big-endian order as the bytes argument fail
            # the above check
            value = uuid_type(bytes=value)
    elif isinstance(value, UUID):
        value = uuid_type(str(value))

    if not isinstance(value, uuid_type):
        raise ValidationError(f"Invalid UUID: {value!r}")

    if value._required_version != value.version:
        raise ValidationError(f"Invalid UUID version: {value!r}")

    return cast(
        "type[pydantic_v1.UUID1] | type[pydantic_v1.UUID3] | type[pydantic_v1.UUID4] | type[pydantic_v1.UUID5]", value
    )


def _is_pydantic_v1_uuid(value: Any) -> bool:  # pragma: no cover
    return is_class_and_subclass(value, (pydantic_v1.UUID1, pydantic_v1.UUID3, pydantic_v1.UUID4, pydantic_v1.UUID5))


_base_encoders: dict[Any, Callable[[Any], Any]] = {
    pydantic_v1.EmailStr: str,
    pydantic_v1.NameEmail: str,
    pydantic_v1.ByteSize: lambda val: val.real,
}

if pydantic_v2 is not None:  # pragma: no cover
    _base_encoders.update(
        {
            pydantic_v2.EmailStr: str,
            pydantic_v2.NameEmail: str,
            pydantic_v2.ByteSize: lambda val: val.real,
        }
    )


def is_pydantic_v1_model_class(annotation: Any) -> TypeGuard[type[pydantic_v1.BaseModel]]:
    return is_class_and_subclass(annotation, pydantic_v1.BaseModel)


def is_pydantic_v2_model_class(annotation: Any) -> TypeGuard[type[pydantic_v2.BaseModel]]:
    return is_class_and_subclass(annotation, pydantic_v2.BaseModel)


def _extract_constrained_field_metadata(
    annotation: Any, name: str | None, default: Any, kwarg_definition_cls: type[KwargDefinition]
) -> tuple[KwargDefinition, dict[str, Any]] | None:

    if pydantic_v1 is Empty:  # pragma: no cover
        return None
    if not isclass(annotation):
        return None

    if issubclass(annotation, pydantic_v1.ConstrainedBytes):
        return kwarg_definition_cls(
            min_length=annotation.min_length,
            max_length=annotation.max_length,
            lower_case=annotation.to_lower,
            upper_case=annotation.to_upper,
        ), {}
    if issubclass(annotation, pydantic_v1.ConstrainedStr):
        return kwarg_definition_cls(
            min_length=annotation.min_length,
            max_length=annotation.max_length,
            lower_case=annotation.to_lower,
            upper_case=annotation.to_upper,
            pattern=annotation.regex,
        ), {}
    if issubclass(annotation, pydantic_v1.ConstrainedDate):
        return kwarg_definition_cls(
            gt=annotation.gt,
            ge=annotation.ge,
            lt=annotation.lt,
            le=annotation.le,
        ), {}
    if issubclass(
        annotation, (pydantic_v1.ConstrainedInt, pydantic_v1.ConstrainedFloat, pydantic_v1.ConstrainedDecimal)
    ):
        return kwarg_definition_cls(
            gt=annotation.gt,
            ge=annotation.ge,
            lt=annotation.lt,
            le=annotation.le,
            multiple_of=annotation.multiple_of,
        ), {}
    if issubclass(
        annotation, (pydantic_v1.ConstrainedList, pydantic_v1.ConstrainedSet, pydantic_v1.ConstrainedFrozenSet)
    ):
        return kwarg_definition_cls(
            max_items=annotation.max_items,
            min_items=annotation.min_items,
        ), {}
    return None


class PydanticInitPlugin(InitPluginProtocol):
    __slots__ = (
        "exclude",
        "exclude_defaults",
        "exclude_none",
        "exclude_unset",
        "include",
        "prefer_alias",
        "validate_strict",
    )

    def __init__(
        self,
        exclude: PydanticV1FieldsListType | PydanticV2FieldsListType | None = None,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
        exclude_unset: bool = False,
        include: PydanticV1FieldsListType | PydanticV2FieldsListType | None = None,
        prefer_alias: bool = False,
        validate_strict: bool = False,
    ) -> None:
        """Pydantic Plugin to support serialization / validation of Pydantic types / models

        :param exclude: Fields to exclude during serialization
        :param exclude_defaults: Fields to exclude during serialization when they are set to their default value
        :param exclude_none: Fields to exclude during serialization when they are set to ``None``
        :param exclude_unset: Fields to exclude during serialization when they arenot set
        :param include: Fields to exclude during serialization
        :param prefer_alias: Use the ``by_alias=True`` flag when dumping models
        :param validate_strict: Use ``strict=True`` when calling ``.model_validate`` on Pydantic 2.x models
        """
        self.exclude = exclude
        self.exclude_defaults = exclude_defaults
        self.exclude_none = exclude_none
        self.exclude_unset = exclude_unset
        self.include = include
        self.prefer_alias = prefer_alias
        self.validate_strict = validate_strict

    @classmethod
    def encoders(
        cls,
        exclude: PydanticV1FieldsListType | PydanticV2FieldsListType | None = None,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
        exclude_unset: bool = False,
        include: PydanticV1FieldsListType | PydanticV2FieldsListType | None = None,
        prefer_alias: bool = False,
    ) -> dict[Any, Callable[[Any], Any]]:
        encoders = {
            **_base_encoders,
            **cls._create_pydantic_v1_encoders(
                prefer_alias=prefer_alias,
                exclude=exclude,
                exclude_defaults=exclude_defaults,
                exclude_none=exclude_none,
                exclude_unset=exclude_unset,
                include=include,
            ),
        }
        if pydantic_v2 is not None:  # pragma: no cover
            encoders.update(
                cls._create_pydantic_v2_encoders(
                    prefer_alias=prefer_alias,
                    exclude=exclude,
                    exclude_defaults=exclude_defaults,
                    exclude_none=exclude_none,
                    exclude_unset=exclude_unset,
                    include=include,
                )
            )
        return encoders

    @classmethod
    def decoders(cls, validate_strict: bool = False) -> list[tuple[Callable[[Any], bool], Callable[[Any, Any], Any]]]:
        decoders: list[tuple[Callable[[Any], bool], Callable[[Any, Any], Any]]] = [
            (is_pydantic_v1_model_class, _dec_pydantic_v1)
        ]

        if pydantic_v2 is not None:  # pragma: no cover
            decoders.append(
                (
                    is_pydantic_v2_model_class,
                    partial(_dec_pydantic_v2, strict=validate_strict),
                )
            )

        decoders.append((_is_pydantic_v1_uuid, _dec_pydantic_uuid))

        return decoders

    @staticmethod
    def _create_pydantic_v1_encoders(
        exclude: PydanticV1FieldsListType | None = None,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
        exclude_unset: bool = False,
        include: PydanticV1FieldsListType | None = None,
        prefer_alias: bool = False,
    ) -> dict[Any, Callable[[Any], Any]]:  # pragma: no cover
        return {
            pydantic_v1.BaseModel: lambda model: {
                k: v.decode() if isinstance(v, bytes) else v
                for k, v in model.dict(
                    by_alias=prefer_alias,
                    exclude=exclude,
                    exclude_defaults=exclude_defaults,
                    exclude_none=exclude_none,
                    exclude_unset=exclude_unset,
                    include=include,
                ).items()
            },
            pydantic_v1.SecretField: str,
            pydantic_v1.StrictBool: int,
            pydantic_v1.color.Color: str,
            pydantic_v1.ConstrainedBytes: lambda val: val.decode("utf-8"),
            pydantic_v1.ConstrainedDate: lambda val: val.isoformat(),
            pydantic_v1.AnyUrl: str,
        }

    @staticmethod
    def _create_pydantic_v2_encoders(
        exclude: PydanticV2FieldsListType | None = None,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
        exclude_unset: bool = False,
        include: PydanticV2FieldsListType | None = None,
        prefer_alias: bool = False,
    ) -> dict[Any, Callable[[Any], Any]]:
        encoders: dict[Any, Callable[[Any], Any]] = {
            pydantic_v2.BaseModel: lambda model: model.model_dump(
                by_alias=prefer_alias,
                exclude=exclude,
                exclude_defaults=exclude_defaults,
                exclude_none=exclude_none,
                exclude_unset=exclude_unset,
                include=include,
                mode="json",
            ),
            pydantic_v2.types.SecretStr: lambda val: "**********" if val else "",
            pydantic_v2.types.SecretBytes: lambda val: "**********" if val else "",
            pydantic_v2.AnyUrl: str,
        }

        with suppress(ImportError):
            from pydantic_extra_types import color

            encoders[color.Color] = str

        return encoders

    def on_app_init(self, app_config: AppConfig) -> AppConfig:
        app_config.type_encoders = {
            **self.encoders(
                prefer_alias=self.prefer_alias,
                exclude=self.exclude,
                exclude_defaults=self.exclude_defaults,
                exclude_none=self.exclude_none,
                exclude_unset=self.exclude_unset,
                include=self.include,
            ),
            **(app_config.type_encoders or {}),
        }
        app_config.type_decoders = [
            *self.decoders(validate_strict=self.validate_strict),
            *(app_config.type_decoders or []),
        ]

        # _KWARG_META_EXTRACTORS.add(_extract_constrained_field_metadata)
        return app_config
