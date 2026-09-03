# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Session-scoped state for the RAMPART pytest plugin.

Accumulates Result objects and builds the final TestRunReport.
"""

from __future__ import annotations

import copy
import logging
from collections import Counter
from typing import TYPE_CHECKING, Any

from rampart.core.result import Result, SafetyStatus
from rampart.reporting.sink import ReportSink, TestRunReport

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pytest

    from rampart.pytest_plugin._collection import ResultCollector

logger = logging.getLogger(__name__)


def _result_sort_key(result: Result) -> tuple[str, int, str]:
    """Return a total-ordering key for a result.

    Orders by full node ID, then the result's index within its test,
    then the originating xdist worker. The worker tie-breaker keeps the
    order total when the same node ID arrives from multiple workers
    (e.g. ``--dist=each``); it is absent — and therefore constant —
    outside xdist, so single-process ordering is unchanged.
    """
    metadata = result.metadata
    nodeid = str(
        metadata.get("_pytest_nodeid", metadata.get("_pytest_test_name", "")),
    )
    raw_index = metadata.get("_rampart_result_index", 0)
    index = raw_index if isinstance(raw_index, int) else 0
    source_worker = str(metadata.get("_rampart_source_worker", ""))
    return (nodeid, index, source_worker)


def tag_collected_results(
    *,
    node: pytest.Item,
    results: Sequence[Result],
) -> list[Result]:
    """Copy and tag Results with their pytest item metadata.

    Returns:
        list[Result]: Tagged shallow copies in their original order.
    """
    test_name = node.name
    harm_marker = node.get_closest_marker("harm")
    harm_category = harm_marker.args[0] if harm_marker and harm_marker.args else None
    tagged: list[Result] = []
    for result_index, original_result in enumerate(results):
        result = copy.copy(original_result)
        result.metadata = {
            **result.metadata,
            "_pytest_test_name": test_name,
            "_pytest_nodeid": node.nodeid,
            "_rampart_result_index": result_index,
        }
        if harm_category is not None and result.harm_category is None:
            result.harm_category = harm_category
        tagged.append(result)
    return tagged


class RampartSession:
    """Session-scoped state for the RAMPART plugin.

    Accumulates Result objects from all tests, tracks session duration,
    and builds the final TestRunReport. Holds configured sinks for report
    emission.

    Args:
        sinks (list[ReportSink]): Report sinks to emit to at session
            end. Defaults to an empty list (terminal-only output).
    """

    def __init__(self, *, sinks: list[ReportSink] | None = None) -> None:
        self._results: list[Result] = []
        self._results_by_nodeid: dict[str, list[Result]] = {}
        self._sinks: list[ReportSink] = sinks or []
        self._duration_seconds: float = 0.0
        self._cached_report: TestRunReport | None = None
        self._emitted: bool = False
        self._incomplete: bool = False
        self._incomplete_reasons: list[str] = []
        self._report_metadata: dict[str, object] = {}

    @property
    def sinks(self) -> list[ReportSink]:
        """Configured report sinks."""
        return list(self._sinks)

    @property
    def results_by_nodeid(self) -> dict[str, list[Result]]:
        """Read-only view of results grouped by pytest node ID."""
        return {
            nodeid: list(results) for nodeid, results in self._results_by_nodeid.items()
        }

    @property
    def is_emitted(self) -> bool:
        """True once report emission has been attempted (idempotency guard)."""
        return self._emitted

    @property
    def is_incomplete(self) -> bool:
        """True if any worker failed to deliver complete results."""
        return self._incomplete

    @property
    def incomplete_reasons(self) -> list[str]:
        """The recorded reasons the run is incomplete (empty if complete)."""
        return list(self._incomplete_reasons)

    def add_sinks(self, *, sinks: list[ReportSink]) -> None:
        """Register additional sinks for report emission.

        Called by the fixture-based bootstrap to add team-provided
        sinks.

        Args:
            sinks (list[ReportSink]): Sinks to append.

        Raises:
            TypeError: If any item does not satisfy ReportSink.
        """
        for sink in sinks:
            if not isinstance(sink, ReportSink):
                msg = (
                    f"Expected ReportSink, got {type(sink).__name__}. "
                    "Sinks must implement: "
                    "async def emit_async(*, report: TestRunReport) -> None"
                )
                raise TypeError(msg)
            self._sinks.append(sink)

    def set_duration(self, *, duration_seconds: float) -> None:
        """Set the total session duration.

        Called by the plugin at session finish with the elapsed time
        since pytest_configure.

        Args:
            duration_seconds (float): Total wall-clock seconds.
        """
        self._duration_seconds = duration_seconds

    def absorb(self, *, node: pytest.Item, collector: ResultCollector) -> None:
        """Absorb results from a completed test's collector.

        Tags each result with the short test name (extracted from the
        node ID), the full node ID, its index within the test, and the
        harm category from ``@pytest.mark.harm``. The nodeid and index
        give a total, deterministic ordering for the terminal summary and
        report regardless of xdist worker completion order.

        Results are shallow-copied before tagging to avoid mutating
        objects the test body may still reference.

        Args:
            node (pytest.Item): The test item that just completed.
            collector (ResultCollector): The test's result collector.
        """
        tagged = tag_collected_results(node=node, results=collector.results)
        self._results.extend(tagged)
        self._results_by_nodeid[node.nodeid] = tagged
        self._cached_report = None

    @property
    def has_results(self) -> bool:
        """True if any results have been collected."""
        return bool(self._results)

    def merge_worker_results(
        self,
        *,
        results_by_nodeid: dict[str, list[Result]],
    ) -> None:
        """Merge an xdist worker's results into this session.

        Extends both the flat ``_results`` list and the
        ``_results_by_nodeid`` mapping. Invalidates any cached report
        so the next ``build_report()`` reflects the merged data.

        Args:
            results_by_nodeid (dict[str, list[Result]]): Worker results
                grouped by pytest node ID.
        """
        for nodeid, results in results_by_nodeid.items():
            self._results.extend(results)
            self._results_by_nodeid.setdefault(nodeid, []).extend(results)
        self._cached_report = None

    def mark_emitted(self) -> None:
        """Mark the session as having attempted report emission."""
        self._emitted = True

    def mark_incomplete(self, *, reason: str) -> None:
        """Record that a worker failed to deliver complete results.

        Args:
            reason (str): A short human-readable explanation surfaced
                in the report metadata.
        """
        self._incomplete = True
        if reason not in self._incomplete_reasons:
            self._incomplete_reasons.append(reason)
        self._cached_report = None

    def set_report_metadata(self, *, metadata: dict[str, object]) -> None:
        """Attach run-level metadata that will appear on ``TestRunReport``.

        Used by the plugin to surface xdist run-mode information
        (active, worker count, dist mode). Subsequent calls merge into
        existing metadata.

        Args:
            metadata (dict[str, object]): Key/value pairs to attach.
        """
        self._report_metadata.update(metadata)
        self._cached_report = None

    def build_report(self) -> TestRunReport:
        """Build a TestRunReport from all collected results.

        The report is cached and reused on subsequent calls. The
        cache is invalidated when new results are absorbed or merged
        or when metadata is updated.

        Results are sorted by ``(_pytest_nodeid, _rampart_result_index,
        _rampart_source_worker)`` for a total, deterministic ordering across
        xdist worker completion orders. ``_pytest_nodeid`` falls back to
        ``_pytest_test_name`` and ``_rampart_source_worker`` is absent
        (constant) outside xdist, so single-process ordering is unaffected.

        These leading-underscore keys are RAMPART scheduling bookkeeping,
        namespaced to avoid colliding with user-supplied result metadata.

        Returns:
            TestRunReport: Aggregated test run results.
        """
        if self._cached_report is not None:
            return self._cached_report
        sorted_results = sorted(self._results, key=_result_sort_key)
        counts = Counter(r.status for r in sorted_results)
        metadata: dict[str, Any] = dict(self._report_metadata)
        if self._incomplete:
            metadata["incomplete"] = True
            metadata["incomplete_reasons"] = list(self._incomplete_reasons)
        self._cached_report = TestRunReport(
            results=sorted_results,
            total_runs=len(sorted_results),
            passed=counts[SafetyStatus.SAFE],
            failed=counts[SafetyStatus.UNSAFE],
            undetermined=counts[SafetyStatus.UNDETERMINED],
            errors=counts[SafetyStatus.ERROR],
            duration_seconds=self._duration_seconds,
            metadata=metadata,
        )
        return self._cached_report
