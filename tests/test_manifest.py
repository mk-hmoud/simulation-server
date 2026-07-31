from __future__ import annotations

import pytest

from simserver.manifest import (
    Manifest,
    ManifestValidationError,
    parse_magnitude,
    validate_params,
)


def make_manifest(**overrides) -> Manifest:
    defaults = dict(
        model_id="m1",
        sweep_parameter="l",
        parameters={
            "l": {"unit": "um", "min": 0.5, "max": 2.0, "geometry": False},
            "p": {"unit": "um", "min": 1.0, "max": 3.0, "geometry": True},
        },
    )
    defaults.update(overrides)
    return Manifest(**defaults)


def test_parse_magnitude_with_unit() -> None:
    assert parse_magnitude("0.8[um]") == 0.8


def test_parse_magnitude_without_unit() -> None:
    assert parse_magnitude("1.33") == 1.33


def test_parse_magnitude_rejects_garbage() -> None:
    with pytest.raises(ManifestValidationError):
        parse_magnitude("not-a-number")


def test_validate_params_accepts_scalar_value() -> None:
    validate_params(make_manifest(), {"l": "0.8[um]"})  # must not raise


def test_validate_params_accepts_sweep_list_for_sweep_parameter() -> None:
    validate_params(make_manifest(), {"l": ["0.6[um]", "0.8[um]", "1.0[um]"]})  # must not raise


def test_validate_params_rejects_list_for_non_sweep_parameter() -> None:
    with pytest.raises(ManifestValidationError, match="only allowed for the manifest's sweep_parameter"):
        validate_params(make_manifest(), {"p": ["1.5[um]", "2.0[um]"]})


def test_validate_params_rejects_empty_sweep_list() -> None:
    with pytest.raises(ManifestValidationError, match="must not be empty"):
        validate_params(make_manifest(), {"l": []})


def test_validate_params_rejects_out_of_range_value_within_sweep_list() -> None:
    with pytest.raises(ManifestValidationError, match="outside allowed range"):
        validate_params(make_manifest(), {"l": ["0.8[um]", "9999[um]"]})


def test_validate_params_rejects_unit_mismatch_within_sweep_list() -> None:
    with pytest.raises(ManifestValidationError, match="unit"):
        validate_params(make_manifest(), {"l": ["0.8[um]", "800[nm]"]})
