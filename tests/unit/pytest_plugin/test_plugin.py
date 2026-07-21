# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

"""Tests for the RAMPART pytest plugin hooks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from rampart.core.result import Result, SafetyStatus
from rampart.core.types import ObservabilityLevel
from rampart.pytest_plugin._collection import ResultCollectionHandler, ResultCollector
from rampart.pytest_plugin._session import RampartSession
from rampart.pytest_plugin.plugin import (
    _emit_sinks,
    _enforce_incomplete_exit_status,
    _has_sink_hook_impl,
    _resolve_hook_sinks,
    _sanitize_for_terminal,
    _write_result_line,
    pytest_configure,
    pytest_sessionfinish,
    pytest_terminal_summary,
    pytest_unconfigure,
)
from rampart.reporting.sink import ReportSink

if TYPE_CHECKING:
    from _pytest.terminal import TerminalReporter


class _StashStub:
    """Minimal pytest.Stash test double backed by a dict."""

    def __init__(self) -> None:
        self._data: dict[Any, Any] = {}

    def __setitem__(self, key: Any, value: Any) -> None:
        self._data[key] = value

    def __getitem__(self, key: Any) -> Any:
        return self._data[key]

    def __contains__(self, key: Any) -> bool:
        return key in self._data

    def __delitem__(self, key: Any) -> None:
        del self._data[key]

    def get(self, key: Any, default: Any = None) -> Any:
        """Return value for key, or default."""
        return self._data.get(key, default)

    def pop(self, key: Any, *args: Any) -> Any:
        """Remove and return value for key."""
        return self._data.pop(key, *args)


class _ConfigStub:
    """Minimal pytest.Config test double with stash support."""

    def __init__(self) -> None:
        self._ini_lines: list[tuple[str, str]] = []
        self.stash = _StashStub()

    def addinivalue_line(self, name: str, line: str) -> None:
        """Record marker registrations."""
        self._ini_lines.append((name, line))


class TestDefaultHandlerFactory:
    """Handler factory is set during configure and cleared on unconfigure."""

    def test_configure_sets_factory(self) -> None:
        config: Any = _ConfigStub()
        pytest_configure(config)
        try:
            from rampart.core.execution import _default_handler_factory

            handlers = _default_handler_factory()
            assert len(handlers) == 1
            assert isinstance(handlers[0], ResultCollectionHandler)
        finally:
            pytest_unconfigure(config)

    def test_unconfigure_clears_factory(self) -> None:
        config: Any = _ConfigStub()
        pytest_configure(config)
        pytest_unconfigure(config)

        from rampart.core.execution import _default_handler_factory

        assert _default_handler_factory() == []

    def test_configure_creates_session_in_stash(self) -> None:
        config: Any = _ConfigStub()
        pytest_configure(config)
        try:
            from rampart.pytest_plugin.plugin import _rampart_key

            assert isinstance(config.stash.get(_rampart_key), RampartSession)
        finally:
            pytest_unconfigure(config)

    def test_unconfigure_removes_session_from_stash(self) -> None:
        config: Any = _ConfigStub()
        pytest_configure(config)
        pytest_unconfigure(config)

        from rampart.pytest_plugin.plugin import _rampart_key

        assert config.stash.get(_rampart_key) is None


class TestRampartSession:
    """RampartSession accumulates results and builds reports."""

    def test_absorb_accumulates_results(self) -> None:
        session = RampartSession()
        collector = ResultCollector()
        collector.record(
            result=Result(safe=True, status=SafetyStatus.SAFE, summary="ok"),
        )
        node = MagicMock()
        node.nodeid = "test_file.py::test_absorb"

        session.absorb(node=node, collector=collector)

        assert session.has_results
        report = session.build_report()
        assert report.total_runs == 1
        assert report.passed == 1

    def test_has_results_false_when_empty(self) -> None:
        session = RampartSession()
        assert not session.has_results

    def test_build_report_counts(self) -> None:
        session = RampartSession()

        collector = ResultCollector()
        collector.record(
            result=Result(safe=True, status=SafetyStatus.SAFE, summary="s"),
        )
        collector.record(
            result=Result(safe=False, status=SafetyStatus.UNSAFE, summary="u"),
        )
        collector.record(
            result=Result(safe=False, status=SafetyStatus.ERROR, summary="e"),
        )
        node = MagicMock()
        node.nodeid = "test_file.py::test_counts"

        session.absorb(node=node, collector=collector)
        report = session.build_report()

        assert report.total_runs == 3
        assert report.passed == 1
        assert report.failed == 1
        assert report.errors == 1


class TestSanitizeForTerminal:
    """ANSI escape sequences are stripped from terminal output."""

    def test_strips_color_codes(self) -> None:
        text = "\x1b[31mRED TEXT\x1b[0m"
        assert _sanitize_for_terminal(text) == "RED TEXT"

    def test_strips_cursor_movement(self) -> None:
        text = "\x1b[2Ahidden"
        assert _sanitize_for_terminal(text) == "hidden"

    def test_passthrough_clean_text(self) -> None:
        text = "normal summary line"
        assert _sanitize_for_terminal(text) == "normal summary line"

    def test_strips_clear_screen(self) -> None:
        text = "\x1b[2J\x1b[Hinjected"
        assert _sanitize_for_terminal(text) == "injected"

    def test_strips_osc_hyperlink(self) -> None:
        text = "\x1b]8;;http://evil\x07link\x1b]8;;\x07"
        assert _sanitize_for_terminal(text) == "link"


class TestWriteResultLine:
    """_write_result_line writes formatted status, summary, and observability level."""

    def test_safe_result_includes_observability(self) -> None:
        reporter = MagicMock()
        result = Result(
            safe=True,
            status=SafetyStatus.SAFE,
            summary="ok",
            observability_level=ObservabilityLevel.RESPONSE_ONLY,
        )
        _write_result_line(
            terminalreporter=cast("TerminalReporter", reporter),
            result=result,
        )
        reporter.write_line.assert_called_once_with("  PASS  ok (response_only)")

    def test_unsafe_result_includes_observability(self) -> None:
        reporter = MagicMock()
        result = Result(
            safe=False,
            status=SafetyStatus.UNSAFE,
            summary="bad",
            observability_level=ObservabilityLevel.TOOL_AND_SIDE_EFFECTS,
        )
        _write_result_line(
            terminalreporter=cast("TerminalReporter", reporter),
            result=result,
        )
        reporter.write_line.assert_called_once_with(
            "  FAIL  bad (tool_and_side_effects)",
        )

    def test_with_test_name(self) -> None:
        reporter = MagicMock()
        result = Result(
            safe=True,
            status=SafetyStatus.SAFE,
            summary="SAFE",
            observability_level=ObservabilityLevel.TOOL_ONLY,
        )
        _write_result_line(
            terminalreporter=cast("TerminalReporter", reporter),
            result=result,
            test_name="test_exfil",
        )
        reporter.write_line.assert_called_once_with(
            "  PASS  test_exfil -- SAFE (tool_only)",
        )

    def test_ansi_stripped_from_summary(self) -> None:
        reporter = MagicMock()
        result = Result(
            safe=True,
            status=SafetyStatus.SAFE,
            summary="\x1b[31mevil\x1b[0m",
        )
        _write_result_line(
            terminalreporter=cast("TerminalReporter", reporter),
            result=result,
        )
        line = reporter.write_line.call_args[0][0]
        assert "evil" in line
        assert "\x1b" not in line


class TestTerminalSummary:
    """pytest_terminal_summary renders harm-category grouped output."""

    def _make_session_with_results(self) -> RampartSession:
        """Build a RampartSession with two results in different categories."""
        session = RampartSession()
        collector = ResultCollector()
        collector.record(
            result=Result(
                safe=True,
                status=SafetyStatus.SAFE,
                summary="safe-one",
                harm_category="data_exfiltration",
            ),
        )
        collector.record(
            result=Result(
                safe=False,
                status=SafetyStatus.UNSAFE,
                summary="unsafe-one",
                harm_category="jailbreak",
            ),
        )
        node = MagicMock()
        node.nodeid = "test_file.py::test_summary"
        session.absorb(node=node, collector=collector)
        return session

    def test_noop_when_no_session(self) -> None:
        reporter = MagicMock()
        config = MagicMock()
        config.stash = _StashStub()
        pytest_terminal_summary(
            terminalreporter=cast("TerminalReporter", reporter),
            exitstatus=0,
            config=cast("pytest.Config", config),
        )
        reporter.write_sep.assert_not_called()

    def test_noop_when_no_results(self) -> None:
        reporter = MagicMock()
        config = MagicMock()
        config.stash = _StashStub()
        from rampart.pytest_plugin.plugin import _rampart_key

        config.stash[_rampart_key] = RampartSession()
        pytest_terminal_summary(
            terminalreporter=cast("TerminalReporter", reporter),
            exitstatus=0,
            config=cast("pytest.Config", config),
        )
        reporter.write_sep.assert_not_called()

    def test_writes_incomplete_warning_even_without_results(self) -> None:
        reporter = MagicMock()
        config = MagicMock()
        config.stash = _StashStub()
        from rampart.pytest_plugin.plugin import _rampart_key

        session = RampartSession()
        session.mark_incomplete(reason="worker gw0 crashed \x1b[31mred")
        config.stash[_rampart_key] = session
        pytest_terminal_summary(terminalreporter=reporter, exitstatus=0, config=config)

        sep_titles = [str(c) for c in reporter.write_sep.call_args_list]
        assert any("INCOMPLETE RUN" in t for t in sep_titles)
        reason_args = [
            c.args[0]
            for c in reporter.write_line.call_args_list
            if c.args and "gw0 crashed" in c.args[0]
        ]
        assert reason_args
        assert all("\x1b" not in arg for arg in reason_args)

    def test_writes_summary_header(self) -> None:
        reporter = MagicMock()
        config = MagicMock()
        config.stash = _StashStub()
        from rampart.pytest_plugin.plugin import _rampart_key

        config.stash[_rampart_key] = self._make_session_with_results()
        pytest_terminal_summary(
            terminalreporter=cast("TerminalReporter", reporter),
            exitstatus=0,
            config=cast("pytest.Config", config),
        )
        reporter.write_sep.assert_called_once_with("=", "RAMPART Safety Summary")

    def test_writes_population_stats(self) -> None:
        reporter = MagicMock()
        config = MagicMock()
        config.stash = _StashStub()
        from rampart.pytest_plugin.plugin import _rampart_key

        config.stash[_rampart_key] = self._make_session_with_results()
        pytest_terminal_summary(
            terminalreporter=cast("TerminalReporter", reporter),
            exitstatus=0,
            config=cast("pytest.Config", config),
        )
        # Check that the Population line was written
        population_calls = [
            c for c in reporter.write_line.call_args_list if "Population:" in str(c)
        ]
        assert len(population_calls) == 1


class TestRampartSessionSinks:
    """RampartSession accepts and exposes sinks."""

    def test_default_no_sinks(self) -> None:
        session = RampartSession()
        assert session.sinks == []

    def test_accepts_sinks(self) -> None:
        mock_sink = MagicMock()
        session = RampartSession(sinks=[mock_sink])
        assert len(session.sinks) == 1

    def test_sinks_returns_copy(self) -> None:
        mock_sink = MagicMock()
        session = RampartSession(sinks=[mock_sink])
        sinks = session.sinks
        sinks.clear()
        assert len(session.sinks) == 1


class TestRampartSessionAddSinks:
    """RampartSession.add_sinks merges fixture-provided sinks."""

    def test_add_sinks_appends(self) -> None:
        config_sink = MagicMock(spec=["emit_async"])
        config_sink.emit_async = MagicMock()
        session = RampartSession(sinks=[config_sink])

        fixture_sink = MagicMock(spec=["emit_async"])
        fixture_sink.emit_async = MagicMock()
        session.add_sinks(sinks=[fixture_sink])

        assert len(session.sinks) == 2

    def test_add_sinks_empty_list_noop(self) -> None:
        session = RampartSession()
        session.add_sinks(sinks=[])
        assert len(session.sinks) == 0

    def test_add_sinks_rejects_non_conforming(self) -> None:
        session = RampartSession()

        class NotASink:
            pass

        with pytest.raises(TypeError, match="Expected ReportSink"):
            session.add_sinks(sinks=[NotASink()])  # ty: ignore[invalid-argument-type]

    def test_add_sinks_preserves_existing(self) -> None:
        """Config-loaded sinks are not lost when fixture sinks are added."""
        sink_a = MagicMock(spec=["emit_async"])
        sink_a.emit_async = MagicMock()
        sink_b = MagicMock(spec=["emit_async"])
        sink_b.emit_async = MagicMock()

        session = RampartSession(sinks=[sink_a])
        session.add_sinks(sinks=[sink_b])

        assert session.sinks[0] is sink_a
        assert session.sinks[1] is sink_b


class TestRampartSessionDuration:
    """RampartSession tracks and reports duration."""

    def test_default_duration_zero(self) -> None:
        session = RampartSession()
        collector = ResultCollector()
        collector.record(
            result=Result(safe=True, status=SafetyStatus.SAFE, summary="ok"),
        )
        node = MagicMock()
        node.nodeid = "test.py::test_dur"
        session.absorb(node=node, collector=collector)
        report = session.build_report()
        assert report.duration_seconds == pytest.approx(0.0)

    def test_set_duration_reflected_in_report(self) -> None:
        session = RampartSession()
        collector = ResultCollector()
        collector.record(
            result=Result(safe=True, status=SafetyStatus.SAFE, summary="ok"),
        )
        node = MagicMock()
        node.nodeid = "test.py::test_dur"
        session.absorb(node=node, collector=collector)
        session.set_duration(duration_seconds=42.5)
        report = session.build_report()
        assert report.duration_seconds == pytest.approx(42.5)


class TestEmitSinks:
    """Sink emission calls emit_async and handles errors."""

    def test_noop_when_no_sinks(self) -> None:
        session = RampartSession()
        _emit_sinks(rampart_session=session)

    def test_sink_error_swallowed(self) -> None:
        """A failing sink does not raise."""
        mock_sink = MagicMock()
        mock_sink.emit_async = AsyncMock(side_effect=RuntimeError("Kusto down"))
        session = RampartSession(sinks=[mock_sink])
        collector = ResultCollector()
        collector.record(
            result=Result(safe=True, status=SafetyStatus.SAFE, summary="ok"),
        )
        node = MagicMock()
        node.nodeid = "test.py::test_sink"
        session.absorb(node=node, collector=collector)
        # Should not raise
        _emit_sinks(rampart_session=session)


class TestSessionFinishIntegration:
    """pytest_sessionfinish aggregates trials, evaluates gates, and emits sinks."""

    def test_sets_duration(self) -> None:
        import time

        from rampart.pytest_plugin.plugin import (
            _rampart_key,
            _session_start_key,
        )

        session_mock = MagicMock()
        config_stash = _StashStub()
        rs = RampartSession()
        config_stash[_rampart_key] = rs
        config_stash[_session_start_key] = time.monotonic() - 5.0
        session_mock.config.stash = config_stash
        session_mock.items = []

        pytest_sessionfinish(session=cast("pytest.Session", session_mock), exitstatus=0)

        report = rs.build_report()
        assert report.duration_seconds >= 4.0


class TestSinkHookResolution:
    """The pytest_rampart_sinks hook is resolved and validated."""

    def test_has_sink_hook_impl_true_when_impls_present(self) -> None:
        config = MagicMock()
        hook = config.pluginmanager.hook.pytest_rampart_sinks
        hook.get_hookimpls.return_value = [MagicMock()]
        assert _has_sink_hook_impl(config=config) is True

    def test_has_sink_hook_impl_false_when_no_impls(self) -> None:
        config = MagicMock()
        hook = config.pluginmanager.hook.pytest_rampart_sinks
        hook.get_hookimpls.return_value = []
        assert _has_sink_hook_impl(config=config) is False

    def test_resolve_hook_sinks_flattens_implementations(self) -> None:
        sink_a = MagicMock(spec=ReportSink)
        sink_b = MagicMock(spec=ReportSink)
        config = MagicMock()
        config.pluginmanager.hook.pytest_rampart_sinks.return_value = [
            [sink_a],
            [sink_b],
        ]
        result = _resolve_hook_sinks(config=config)
        assert result == [sink_a, sink_b]

    def test_resolve_hook_sinks_drops_non_report_sinks(self) -> None:
        sink_a = MagicMock(spec=ReportSink)
        config = MagicMock()
        config.pluginmanager.hook.pytest_rampart_sinks.return_value = [
            [sink_a, "not-a-sink"],
        ]
        result = _resolve_hook_sinks(config=config)
        assert result == [sink_a]

    def test_resolve_hook_sinks_skips_non_list_results(self) -> None:
        sink_a = MagicMock(spec=ReportSink)
        config = MagicMock()
        config.pluginmanager.hook.pytest_rampart_sinks.return_value = [
            "bad-impl-return",
            [sink_a],
        ]
        result = _resolve_hook_sinks(config=config)
        assert result == [sink_a]


class TestIncompleteExitStatus:
    """Incomplete runs are forced to a non-zero exit status."""

    def test_incomplete_run_forces_tests_failed(self) -> None:
        session = MagicMock()
        session.exitstatus = pytest.ExitCode.OK
        rampart_session = RampartSession()
        rampart_session.mark_incomplete(reason="worker gw1 crashed")
        _enforce_incomplete_exit_status(
            session=cast("pytest.Session", session),
            rampart_session=rampart_session,
        )
        assert session.exitstatus == pytest.ExitCode.TESTS_FAILED

    def test_complete_run_preserves_ok_status(self) -> None:
        session = MagicMock()
        session.exitstatus = pytest.ExitCode.OK
        rampart_session = RampartSession()
        _enforce_incomplete_exit_status(
            session=cast("pytest.Session", session),
            rampart_session=rampart_session,
        )
        assert session.exitstatus == pytest.ExitCode.OK

    def test_incomplete_run_does_not_mask_existing_failure(self) -> None:
        session = MagicMock()
        session.exitstatus = pytest.ExitCode.INTERRUPTED
        rampart_session = RampartSession()
        rampart_session.mark_incomplete(reason="worker gw1 crashed")
        _enforce_incomplete_exit_status(
            session=cast("pytest.Session", session),
            rampart_session=rampart_session,
        )
        assert session.exitstatus == pytest.ExitCode.INTERRUPTED
