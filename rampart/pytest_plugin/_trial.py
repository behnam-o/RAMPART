# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Trial declaration and configuration resolution for the pytest plugin."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any

import pytest

TRIALS_OPTION = "rampart_trials"
_MAX_POSITIONAL_ARGS = 2


@dataclass(frozen=True, kw_only=True)
class TrialConfig:
    """Effective configuration for one declared trial population.

    Args:
        n (int): Number of executions in the population.
        threshold (float): Minimum safe-result rate required to pass.
    """

    n: int
    threshold: float


class TrialMismatchWarning(UserWarning):
    """A population execution disagreed with its resolved trial declaration."""


def parse_positive_int(value: str) -> int:
    """Parse a positive integer for the trial-count CLI option.

    Args:
        value (str): Raw command-line value.

    Returns:
        int: Parsed positive integer.

    Raises:
        argparse.ArgumentTypeError: If value is not a positive integer.
    """
    try:
        parsed = int(value)
    except ValueError as exc:
        msg = f"expected a positive integer, got {value!r}"
        raise argparse.ArgumentTypeError(msg) from exc
    if parsed < 1:
        msg = f"expected a positive integer, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return parsed


def resolve_trial_config(
    *,
    node: pytest.Item,
    config: pytest.Config,
) -> TrialConfig:
    """Resolve the closest trial marker against the CLI sample-depth override.

    Pytest's closest-marker lookup supplies the inheritance contract: method-level
    markers shadow class-level markers, and arguments from separate markers are not
    merged.

    Args:
        node (pytest.Item): Test item requesting trial configuration.
        config (pytest.Config): Active pytest configuration.

    Returns:
        TrialConfig: Effective population configuration.

    Raises:
        pytest.UsageError: If the test has no trial marker or the declaration is
            invalid.
    """
    marker = node.get_closest_marker("trial")
    if marker is None:
        msg = f"trial_config requires @pytest.mark.trial on {node.nodeid}"
        raise pytest.UsageError(msg)

    unknown_kwargs = set(marker.kwargs) - {"n", "threshold"}
    if unknown_kwargs:
        names = ", ".join(sorted(unknown_kwargs))
        msg = f"trial marker has unsupported argument(s): {names}"
        raise pytest.UsageError(msg)
    if len(marker.args) > _MAX_POSITIONAL_ARGS:
        msg = "trial marker accepts at most two positional arguments"
        raise pytest.UsageError(msg)
    if marker.args and "n" in marker.kwargs:
        msg = "trial n was provided both positionally and by keyword"
        raise pytest.UsageError(msg)
    if len(marker.args) > 1 and "threshold" in marker.kwargs:
        msg = "trial threshold was provided both positionally and by keyword"
        raise pytest.UsageError(msg)

    raw_n: Any = marker.kwargs.get("n", marker.args[0] if marker.args else 1)
    raw_threshold: Any = marker.kwargs.get(
        "threshold",
        marker.args[1] if len(marker.args) > 1 else 1.0,
    )
    if not isinstance(raw_n, int) or isinstance(raw_n, bool) or raw_n < 1:
        msg = f"trial n must be a positive integer, got {raw_n!r}"
        raise pytest.UsageError(msg)
    if not isinstance(raw_threshold, int | float) or isinstance(raw_threshold, bool):
        msg = f"trial threshold must be a number, got {raw_threshold!r}"
        raise pytest.UsageError(msg)
    threshold = float(raw_threshold)
    if not 0.0 <= threshold <= 1.0:
        msg = f"trial threshold must be between 0.0 and 1.0, got {raw_threshold!r}"
        raise pytest.UsageError(msg)

    override = config.getoption(TRIALS_OPTION, default=None)
    return TrialConfig(
        n=override if override is not None else raw_n,
        threshold=threshold,
    )
