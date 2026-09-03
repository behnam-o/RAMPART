# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Tests for trial declaration configuration resolution."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock

import pytest

from rampart.pytest_plugin import TrialConfig
from rampart.pytest_plugin._trial import parse_positive_int, resolve_trial_config


def _resolve(
    marker: pytest.Mark | None,
    *,
    override: int | None = None,
) -> TrialConfig:
    """Resolve a marker with a minimal pytest node and config."""
    node = MagicMock(nodeid="test_file.py::test_population")
    node.get_closest_marker.return_value = marker
    config = MagicMock()
    config.getoption.return_value = override
    return resolve_trial_config(node=node, config=config)


class TestResolveTrialConfig:
    def test_resolves_marker_values(self) -> None:
        marker = pytest.mark.trial(n=10, threshold=0.3).mark

        assert _resolve(marker) == TrialConfig(n=10, threshold=0.3)

    def test_cli_override_replaces_only_n(self) -> None:
        marker = pytest.mark.trial(n=10, threshold=0.3).mark

        assert _resolve(marker, override=25) == TrialConfig(n=25, threshold=0.3)

    def test_defaults_marker_values(self) -> None:
        assert _resolve(pytest.mark.trial.mark) == TrialConfig(n=1, threshold=1.0)

    def test_supports_positional_values(self) -> None:
        assert _resolve(pytest.mark.trial(4, 0.75).mark) == TrialConfig(
            n=4,
            threshold=0.75,
        )

    def test_rejects_unmarked_test(self) -> None:
        with pytest.raises(pytest.UsageError, match=r"requires @pytest\.mark\.trial"):
            _resolve(None)

    @pytest.mark.parametrize("n", [0, -1, True, 1.5, "3"])
    def test_rejects_invalid_n(self, n: object) -> None:
        marker = pytest.mark.trial(n=n).mark

        with pytest.raises(pytest.UsageError, match="positive integer"):
            _resolve(marker)

    @pytest.mark.parametrize("threshold", [-0.1, 1.1, True, "0.5"])
    def test_rejects_invalid_threshold(self, threshold: object) -> None:
        marker = pytest.mark.trial(threshold=threshold).mark

        with pytest.raises(pytest.UsageError, match="threshold"):
            _resolve(marker)

    def test_rejects_unknown_arguments(self) -> None:
        marker = pytest.mark.trial(n=2, target=0.5).mark

        with pytest.raises(pytest.UsageError, match=r"unsupported argument.*target"):
            _resolve(marker)

    def test_rejects_duplicate_n(self) -> None:
        marker = pytest.mark.trial(2, n=3).mark

        with pytest.raises(pytest.UsageError, match="both positionally and by keyword"):
            _resolve(marker)


class TestParsePositiveInt:
    @pytest.mark.parametrize("value", ["0", "-1", "invalid"])
    def test_rejects_non_positive_or_invalid_values(self, value: str) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            parse_positive_int(value)

    def test_returns_positive_integer(self) -> None:
        assert parse_positive_int("7") == 7
