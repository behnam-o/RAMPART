# CI Integration

RAMPART tests run as standard pytest tests. This guide covers patterns for CI pipelines.

---

## Running in CI

```bash
pytest tests/ -v --tb=short
```

RAMPART tests interact with real or simulated agents and may take longer than unit tests. Set appropriate timeouts:

```bash
pytest tests/ -v --timeout=300
```

### Parallel Execution

For faster CI runs, use [`pytest-xdist`](xdist.md):

```bash
pip install pytest-xdist
pytest tests/ -n auto
```

RAMPART aggregates results across worker processes and emits a single unified report under **any** `--dist` mode. Each call to `execute_trials_async` remains one pytest item and therefore runs on one worker.

---

## Repeated Executions for Statistical Confidence

Use `execute_trials_async` for tests where a single run is not conclusive:

```python
async def test_injection_resistance(adapter):
    result = await Attacks.xpia(...).execute_trials_async(
        adapter=adapter,
        n=10,
        threshold=0.8,
    )
    assert result, result.summary
```

This runs 10 independent trials. The single pytest test passes only if ≥ 80% of trials are `SAFE`.

**Trial semantics in CI:**

- The population is one pytest item
- The returned `PopulationResult` is the aggregate verdict
- The aggregate passes when the SAFE pass rate meets the threshold
- Any `ERROR` trial makes the aggregate fail
- `UNDETERMINED` trials count against the pass rate
- `@pytest.mark.trial` is declaration-only and does not execute repetitions

---

## Structured Reports

Configure `rampart_sinks` to write JSON reports for downstream processing:

```python
# conftest.py
from pathlib import Path
import pytest
from rampart.reporting import JsonFileReportSink, ReportSink

@pytest.fixture(scope="session")
def rampart_sinks() -> list[ReportSink]:
    return [JsonFileReportSink(output_dir=Path(".report"))]
```

The JSON file contains aggregate statistics and per-result data that CI dashboards can consume.

!!! tip "Running in parallel"
    Under [`pytest-xdist`](xdist.md), prefer the `pytest_rampart_sinks` hook over the fixture — it is resolved on the controller, so it works the same in single-process and parallel CI runs. See [Registering Sinks](pytest-integration.md#pytest_rampart_sinks-hook).

---

## Pytest Options

RAMPART is configured via pytest options and Python (sinks, adapters, payloads).

### `--rampart-xdist-max-bytes`

Maximum size in bytes of a worker's serialized result payload when running under [`pytest-xdist`](xdist.md). Defaults to `67108864` (64 MB). Workers that exceed the cap log a warning and the controller marks the run as incomplete. Also configurable via the `rampart_xdist_max_bytes` ini option.

```bash
pytest -n auto --rampart-xdist-max-bytes=134217728   # 128 MB
```

---

## Environment Variables

Your adapter and test configuration typically read environment variables. Setting them locally for ad-hoc runs:

=== "Linux / macOS"

    ```bash
    export AGENT_API_KEY="..."
    export AGENT_ENDPOINT="https://..."
    pytest tests/
    ```

=== "Windows (PowerShell)"

    ```powershell
    $env:AGENT_API_KEY = "..."
    $env:AGENT_ENDPOINT = "https://..."
    pytest tests/
    ```

Then consume them in your adapter and configuration:

```python
import os
from rampart.core.llm import LLMConfig

@pytest.fixture
def adapter():
    return MyAdapter(
        api_key=os.environ["AGENT_API_KEY"],
        endpoint=os.environ["AGENT_ENDPOINT"],
    )

# For LLM-driven attacks
llm = LLMConfig(
    model="gpt-4o",
    endpoint=os.environ["OPENAI_ENDPOINT"],
    api_key=os.environ.get("OPENAI_API_KEY"),  # None → azure-identity
    deployment=os.environ.get("OPENAI_DEPLOYMENT"),
)
```

---

## Exit Codes

RAMPART does not alter pytest's exit codes:

| Exit Code | Meaning |
|-----------|---------|
| `0` | All tests passed |
| `1` | Some tests failed |
| `2` | Test execution interrupted |
| `5` | No tests collected |


