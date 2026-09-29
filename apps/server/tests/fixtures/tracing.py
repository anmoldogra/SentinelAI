"""An in-memory tracer provider for tests — ADR-0018.

**Why this exists as a shared fixture rather than four lines in each test file.** The obvious
spelling is wrong in a way that does not fail locally:

    previous = trace.get_tracer_provider()      # a ProxyTracerProvider when none is set
    trace._TRACER_PROVIDER = provider
    ...
    trace._TRACER_PROVIDER = previous           # the proxy is now the global it delegates to

``get_tracer_provider()`` returns a **proxy** when no provider has been installed, and that proxy
resolves every call by reading the module global. Restoring it *into* that global makes it delegate
to itself, so the next ``get_tracer`` anywhere in the process recurses until Python gives up. The
symptom lands far away — every later test that builds a FastAPI app dies with ``RecursionError`` —
and it only appears when a tracing test shares a session with those tests, which is exactly what CI
does and a single-file run does not.

Reading the module global directly is the fix: unset is ``None``, and restoring ``None`` leaves the
process as it was found.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

__all__ = ["fresh_tracing_process", "in_memory_tracing"]


@contextmanager
def in_memory_tracing() -> Iterator[InMemorySpanExporter]:
    """Install a real SDK provider whose finished spans land in memory; restore on exit.

    A real provider and a real exporter, not a mock: the assertions worth making are about parent
    links and span kinds, and a mock would cheerfully agree to whatever it was asked.

    ``SimpleSpanProcessor`` so a span is exported the moment it ends — a test that had to wait for a
    batch to flush would be a test that sometimes passes.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # The module global, NOT `get_tracer_provider()` — see this module's docstring.
    previous = trace._TRACER_PROVIDER
    trace._TRACER_PROVIDER = provider
    try:
        yield exporter
    finally:
        trace._TRACER_PROVIDER = previous


@contextmanager
def fresh_tracing_process() -> Iterator[None]:
    """Pretend this is a process that has installed no tracer provider yet.

    Two globals have to move, not one, and the second is easy to miss: ``set_tracer_provider`` is
    guarded by a module-level ``Once``, so clearing ``_TRACER_PROVIDER`` alone leaves the guard
    spent and the next ``set_tracer_provider`` logs "Overriding of current TracerProvider is not
    allowed" and does **nothing**. A test would then assert against whatever provider a previous
    test happened to install — passing or failing for reasons unrelated to the code under test.

    `configure_tracing`'s own once-per-process flag is reset with them, so the unit under test
    starts from the same blank slate the SDK does.
    """
    from sentinelai.platform.tracing import reset_tracing_for_tests

    previous_provider = trace._TRACER_PROVIDER
    previous_once = trace._TRACER_PROVIDER_SET_ONCE
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = trace.Once()
    reset_tracing_for_tests()
    try:
        yield
    finally:
        trace._TRACER_PROVIDER = previous_provider
        trace._TRACER_PROVIDER_SET_ONCE = previous_once
        reset_tracing_for_tests()
