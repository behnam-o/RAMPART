# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Subprocess tests for the trial_config pytest fixture."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from _pytest.pytester import Pytester

pytest_plugins = ["pytester"]


@pytest.fixture
def configured_pytester(pytester: Pytester) -> Pytester:
    """Configure child pytest sessions consistently with the repository."""
    pytester.makeini(
        """
        [pytest]
        asyncio_mode = auto
        asyncio_default_fixture_loop_scope = session
        """,
    )
    return pytester


def test_fixture_resolves_marker_values(configured_pytester: Pytester) -> None:
    """The fixture returns values declared by the closest trial marker."""
    configured_pytester.makepyfile(
        """
        import pytest

        @pytest.mark.trial(n=10, threshold=0.3)
        def test_population(trial_config):
            assert trial_config.n == 10
            assert trial_config.threshold == 0.3
        """,
    )

    result = configured_pytester.runpytest("-p", "no:cacheprovider", "-q")

    result.assert_outcomes(passed=1)


def test_cli_overrides_only_n(configured_pytester: Pytester) -> None:
    """The CLI count replaces n without changing the declared threshold."""
    configured_pytester.makepyfile(
        """
        import pytest

        @pytest.mark.trial(n=10, threshold=0.3)
        def test_population(trial_config):
            assert trial_config.n == 25
            assert trial_config.threshold == 0.3
        """,
    )

    result = configured_pytester.runpytest(
        "-p",
        "no:cacheprovider",
        "--rampart-trials=25",
        "-q",
    )

    result.assert_outcomes(passed=1)


def test_method_marker_shadows_class_marker(
    configured_pytester: Pytester,
) -> None:
    """A method marker shadows, rather than merges with, its class marker."""
    configured_pytester.makepyfile(
        """
        import pytest

        @pytest.mark.trial(n=5, threshold=0.9)
        class TestPopulation:
            def test_inherits(self, trial_config):
                assert trial_config.n == 5
                assert trial_config.threshold == 0.9

            @pytest.mark.trial(n=2)
            def test_shadows(self, trial_config):
                assert trial_config.n == 2
                assert trial_config.threshold == 1.0
        """,
    )

    result = configured_pytester.runpytest("-p", "no:cacheprovider", "-q")

    result.assert_outcomes(passed=2)


def test_unmarked_fixture_request_is_rejected(
    configured_pytester: Pytester,
) -> None:
    """The fixture rejects tests that do not declare trial configuration."""
    configured_pytester.makepyfile(
        """
        def test_population(trial_config):
            pass
        """,
    )

    result = configured_pytester.runpytest("-p", "no:cacheprovider", "-q")

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*trial_config requires @pytest.mark.trial*"])


def test_marked_test_without_fixture_is_rejected(
    configured_pytester: Pytester,
) -> None:
    """A trial declaration cannot silently run once without its fixture."""
    configured_pytester.makepyfile(
        """
        import pytest

        @pytest.mark.trial(n=10, threshold=0.3)
        def test_population():
            pass
        """,
    )

    result = configured_pytester.runpytest("-p", "no:cacheprovider", "-q")

    assert result.ret != pytest.ExitCode.OK
    result.stderr.fnmatch_lines(
        ["*ERROR: @pytest.mark.trial requires trial_config*"],
    )


def test_invalid_marker_without_fixture_is_rejected(
    configured_pytester: Pytester,
) -> None:
    """Marker values are validated even when the fixture is omitted."""
    configured_pytester.makepyfile(
        """
        import pytest

        @pytest.mark.trial(n=0)
        def test_population():
            pass
        """,
    )

    result = configured_pytester.runpytest("-p", "no:cacheprovider", "-q")

    assert result.ret != pytest.ExitCode.OK
    result.stderr.fnmatch_lines(["*trial n must be a positive integer*"])
