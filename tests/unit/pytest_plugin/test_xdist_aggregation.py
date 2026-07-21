# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Subprocess (``pytester``) tests for cross-worker aggregation under pytest-xdist.

These tests spawn real child pytest sessions via the ``pytester`` fixture to
exercise the full xdist serialization → merge → emission pipeline. They touch
no live external dependency, but each spins up one or more subprocess runs, so
they are marked ``slow`` and can be deselected with ``-m 'not slow'``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from _pytest.pytester import Pytester


pytest_plugins = ["pytester"]

pytestmark = pytest.mark.slow


_CONFTEST = """\
from pathlib import Path

import pytest

from rampart.reporting import JsonFileReportSink


_OUT_DIR = Path("rampart_reports").absolute()


@pytest.fixture(scope="session")
def rampart_sinks():
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    Path("rampart_report_dir.txt").write_text(str(_OUT_DIR))
    return [JsonFileReportSink(output_dir=_OUT_DIR)]
"""


# Each ``pytester`` child session is configuration-isolated from the repository's
# ``pyproject.toml``, so pytest-asyncio reads an empty
# ``asyncio_default_fixture_loop_scope`` via ``config.getini(...)`` and emits a
# ``PytestDeprecationWarning`` once per subprocess run. Writing an ini file into
# the child project root sets the option through the same channel pytest-asyncio
# reads, mirroring the parent project's asyncio configuration.
_INI = """\
[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = session
"""


@pytest.fixture
def configured_pytester(pytester: Pytester) -> Pytester:
    """Write the child-session pytest conftest and ini files for subprocess runs."""
    pytester.makeconftest(_CONFTEST)
    pytester.makeini(_INI)
    return pytester


def _load_reports(configured_pytester: Pytester) -> list[dict[str, Any]]:
    marker = configured_pytester.path / "rampart_report_dir.txt"
    if not marker.exists():
        default_dir = configured_pytester.path / "rampart_reports"
        if default_dir.exists():
            return [
                json.loads(p.read_text())
                for p in sorted(default_dir.glob("run_report_*.json"))
            ]
        return []
    out_dir = Path(marker.read_text().strip())
    if not out_dir.exists():
        return []
    return [
        json.loads(p.read_text()) for p in sorted(out_dir.glob("run_report_*.json"))
    ]


def _setup_simple_tests(configured_pytester: Pytester) -> None:
    configured_pytester.makepyfile(
        test_a="""
        import pytest
        from rampart import record_result
        from rampart.core.result import Result, SafetyStatus
        from rampart.core.types import ObservabilityLevel

        @pytest.mark.harm("test")
        def test_a_one():
            record_result(Result(
                safe=True, status=SafetyStatus.SAFE, summary="a1",
                observability_level=ObservabilityLevel.RESPONSE_ONLY,
            ))

        @pytest.mark.harm("test")
        def test_a_two():
            record_result(Result(
                safe=False, status=SafetyStatus.UNSAFE, summary="a2",
                observability_level=ObservabilityLevel.RESPONSE_ONLY,
            ))
        """,
        test_b="""
        import pytest
        from rampart import record_result
        from rampart.core.result import Result, SafetyStatus
        from rampart.core.types import ObservabilityLevel

        @pytest.mark.harm("test")
        def test_b_one():
            record_result(Result(
                safe=True, status=SafetyStatus.SAFE, summary="b1",
                observability_level=ObservabilityLevel.RESPONSE_ONLY,
            ))

        @pytest.mark.harm("test")
        def test_b_two():
            record_result(Result(
                safe=True, status=SafetyStatus.SAFE, summary="b2",
                observability_level=ObservabilityLevel.RESPONSE_ONLY,
            ))
        """,
    )


class TestSingleProcessBaseline:
    def test_baseline_emits_one_report(self, configured_pytester: Pytester) -> None:
        _setup_simple_tests(configured_pytester)
        result = configured_pytester.runpytest("-p", "no:cacheprovider")
        result.assert_outcomes(passed=4)
        reports = _load_reports(configured_pytester)
        assert len(reports) == 1
        assert reports[0]["total_runs"] == 4


class TestXdistConsolidation:
    def test_xdist_emits_single_consolidated_report(
        self,
        configured_pytester: Pytester,
    ) -> None:
        _setup_simple_tests(configured_pytester)
        result = configured_pytester.runpytest(
            "-p",
            "no:cacheprovider",
            "-n",
            "2",
        )
        result.assert_outcomes(passed=4)
        reports = _load_reports(configured_pytester)
        assert len(reports) == 1, (
            f"Expected exactly one report under xdist, got {len(reports)}: "
            f"{[r.get('total_runs') for r in reports]}"
        )

    def test_population_statistics_over_full_set(
        self,
        configured_pytester: Pytester,
    ) -> None:
        _setup_simple_tests(configured_pytester)
        configured_pytester.runpytest("-p", "no:cacheprovider", "-n", "2")
        reports = _load_reports(configured_pytester)
        assert len(reports) == 1
        report = reports[0]
        assert report["total_runs"] == 4
        assert report["passed"] == 3
        assert report["failed"] == 1
        assert report["population_summary"]["total_runs"] == 4
        assert report["population_summary"]["safe_count"] == 3
        assert report["population_summary"]["unsafe_count"] == 1


class TestPopulationPytestVerdict:
    def _make_population_test(
        self,
        *,
        configured_pytester: Pytester,
        threshold: float,
    ) -> None:
        configured_pytester.makepyfile(
            test_population=f"""
            import pytest

            from rampart.core.execution import BaseExecution
            from rampart.core.result import Result, SafetyStatus


            class SequenceExecution(BaseExecution):
                def __init__(self):
                    super().__init__()
                    self.statuses = [
                        SafetyStatus.SAFE,
                        SafetyStatus.SAFE,
                        SafetyStatus.SAFE,
                        SafetyStatus.UNSAFE,
                        SafetyStatus.UNSAFE,
                    ]

                @property
                def strategy_name(self):
                    return "sequence"

                async def _execute_async(self, *, adapter):
                    status = self.statuses.pop(0)
                    return Result(
                        safe=status is SafetyStatus.SAFE,
                        status=status,
                        summary=status.value,
                    )


            @pytest.mark.trial(n=5, threshold={threshold})
            async def test_population_threshold():
                result = await SequenceExecution().execute_trials_async(
                    adapter=object(),
                    n=5,
                    threshold={threshold},
                )
                assert result, result.summary
            """,
        )

    def test_exact_threshold_passes_one_pytest_item(
        self,
        configured_pytester: Pytester,
    ) -> None:
        self._make_population_test(
            configured_pytester=configured_pytester,
            threshold=0.6,
        )

        result = configured_pytester.runpytest("-p", "no:cacheprovider", "-q")

        result.assert_outcomes(passed=1)
        assert not any("trial-" in line for line in result.outlines)

    def test_below_threshold_fails_one_pytest_item(
        self,
        configured_pytester: Pytester,
    ) -> None:
        self._make_population_test(
            configured_pytester=configured_pytester,
            threshold=0.8,
        )

        result = configured_pytester.runpytest("-p", "no:cacheprovider", "-q")

        result.assert_outcomes(failed=1)
        assert not any("trial-" in line for line in result.outlines)


class TestXdistMetadata:
    def test_report_includes_xdist_metadata(
        self,
        configured_pytester: Pytester,
    ) -> None:
        _setup_simple_tests(configured_pytester)
        configured_pytester.runpytest("-p", "no:cacheprovider", "-n", "2")
        reports = _load_reports(configured_pytester)
        assert len(reports) == 1
        metadata = reports[0].get("metadata", {})
        assert metadata.get("xdist_active") is True
        assert metadata.get("worker_count") == 2
        assert "dist_mode" in metadata
        assert "population_summary" in reports[0]

    def test_size_cap_marks_run_incomplete(self, configured_pytester: Pytester) -> None:
        """Forcing a 1-byte cap surfaces incompleteness in report metadata.

        Triggers the truncation path so the controller must record
        ``incomplete=True`` plus a reason in the merged report.
        """
        _setup_simple_tests(configured_pytester)
        configured_pytester.runpytest(
            "-p",
            "no:cacheprovider",
            "-n",
            "2",
            "--rampart-xdist-max-bytes=1",
        )
        reports = _load_reports(configured_pytester)
        assert len(reports) == 1
        metadata = reports[0].get("metadata", {})
        assert metadata.get("incomplete") is True
        reasons = metadata.get("incomplete_reasons", [])
        assert any("truncated" in r for r in reasons)


class TestCollectOnly:
    def test_collect_only_does_not_emit_reports(
        self,
        configured_pytester: Pytester,
    ) -> None:
        _setup_simple_tests(configured_pytester)
        configured_pytester.runpytest("-p", "no:cacheprovider", "--collect-only")
        # No sinks emit when no tests run
        marker = configured_pytester.path / "rampart_report_dir.txt"
        if marker.exists():
            out_dir = Path(marker.read_text().strip())
            if out_dir.exists():
                reports = list(out_dir.glob("run_report_*.json"))
                assert reports == []


class TestTrialMarkerDeclaration:
    def test_trial_marker_collects_one_item(
        self,
        configured_pytester: Pytester,
    ) -> None:
        configured_pytester.makepyfile(
            test_det="""
            import pytest

            @pytest.mark.trial(n=3)
            def test_x():
                pass
            """,
        )
        result = configured_pytester.runpytest(
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "-q",
        )

        result.assert_outcomes(passed=0)
        assert sum("test_det.py::test_x" in line for line in result.outlines) == 1
        assert not any("trial-" in line for line in result.outlines)

    def test_trial_marker_remains_selectable(
        self,
        configured_pytester: Pytester,
    ) -> None:
        configured_pytester.makepyfile(
            test_selection="""
            import pytest

            @pytest.mark.trial(n=3, threshold=0.8)
            def test_population():
                pass

            def test_plain():
                pass
            """,
        )

        result = configured_pytester.runpytest(
            "-p",
            "no:cacheprovider",
            "-m",
            "trial",
            "-q",
        )

        result.assert_outcomes(passed=1, deselected=1)
