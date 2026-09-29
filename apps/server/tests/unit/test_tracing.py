"""Unit tests for `platform/tracing.py` — ADR-0018.

The propagation itself (publish → relay → handler, across real outbox rows) is proven against
Postgres in `tests/integration/test_trace_propagation_db.py`. What this file covers is the decisions
that file cannot isolate, and every one of them is a decision about *absence*: what happens when
nothing is being traced, when a stored `traceparent` is garbage, and when a profile forbids the
exporter someone configured.

Each test drives a real `TracerProvider` with an in-memory exporter rather than mocking the SDK —
a mock would happily agree that a span was created with the parent we asked for, which is the one
thing worth checking for real.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sentinelai.platform import tracing
from sentinelai.platform.config import ConfigurationError, Settings
from sentinelai.platform.tracing import (
    _build_exporter,
    configure_tracing,
    context_from_traceparent,
    current_trace_id,
    current_traceparent,
    parent_context,
    span_attributes,
)
from tests.fixtures.tracing import fresh_tracing_process, in_memory_tracing

# A syntactically valid W3C traceparent: version 00, a nonzero trace id, a nonzero span id, sampled.
_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    """A real provider whose finished spans land in memory, restored afterwards.

    Shared, because restoring the provider correctly is subtler than it looks and getting it wrong
    breaks unrelated tests in a later file — see `tests/fixtures/tracing.py`.
    """
    with in_memory_tracing() as memory:
        yield memory


# --- what an untraced process reports ---------------------------------------
def test_an_untraced_process_has_no_traceparent() -> None:
    """``None``, never a zeroed placeholder — and this is the security-relevant case.

    `OutboxWriter.publish` puts this value inside the **signed** envelope (ADR-0007). A synthetic
    all-zero traceparent would be an attestation that the evidence moved along an execution path
    that never existed, signed by the platform's own key.
    """
    assert current_traceparent() is None


def test_an_untraced_process_has_no_trace_id() -> None:
    """Same reasoning one layer up: a log line with a trace id that resolves to nothing in Tempo
    looks like a lost trace, which is worse than a line with no trace id at all."""
    assert current_trace_id() is None


def test_an_active_span_yields_a_traceparent_and_matching_trace_id(
    exporter: InMemorySpanExporter,
) -> None:
    """The two accessors must describe the same span — they are joined on in Grafana."""
    with trace.get_tracer("test").start_as_current_span("work"):
        traceparent = current_traceparent()
        trace_id = current_trace_id()

    assert traceparent is not None and trace_id is not None
    assert traceparent.startswith("00-")
    assert traceparent.split("-")[1] == trace_id, "the log line and the event must name one trace"


# --- parsing a stored traceparent -------------------------------------------
def test_a_stored_traceparent_parses_into_a_parent_context() -> None:
    context = context_from_traceparent(_TRACEPARENT)

    assert context is not None
    span_context = trace.get_current_span(context).get_span_context()
    assert format(span_context.trace_id, "032x") == _TRACE_ID
    assert span_context.is_valid


@pytest.mark.parametrize(
    "value",
    [None, "", "not-a-traceparent", "00--00f067aa0ba902b7-01", f"00-{'0' * 32}-{'0' * 16}-01"],
    ids=["absent", "empty", "garbage", "missing-trace-id", "all-zero"],
)
def test_an_unusable_traceparent_yields_no_parent(value: str | None) -> None:
    """``None`` means "start a root span", and that is the point.

    ``extract`` does not raise on garbage — it returns the *current* context unchanged. A caller
    that passed that through would parent the handler's span onto whatever the dispatcher happened
    to be inside, inventing a causal link. In a platform whose purpose is evidentiary provenance,
    a fabricated causal edge is the worst available failure.
    """
    assert context_from_traceparent(value) is None


def test_garbage_does_not_silently_inherit_the_ambient_span(
    exporter: InMemorySpanExporter,
) -> None:
    """The same rule, asserted where it would actually bite: inside an active span."""
    with trace.get_tracer("test").start_as_current_span("dispatcher-internals"):
        assert context_from_traceparent("not-a-traceparent") is None


def test_a_child_span_joins_the_stored_trace(exporter: InMemorySpanExporter) -> None:
    """The mechanism the whole ADR exists for: a consumer span inside the producer's trace."""
    parent = context_from_traceparent(_TRACEPARENT)

    with trace.get_tracer("test").start_as_current_span("consume", context=parent):
        pass

    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    assert format(finished[0].context.trace_id, "032x") == _TRACE_ID
    assert finished[0].parent is not None
    assert format(finished[0].parent.span_id, "016x") == "00f067aa0ba902b7"


# --- span attributes --------------------------------------------------------
def test_span_attributes_drops_none_but_keeps_falsy_values() -> None:
    """An absent `causation_id` is "no attribute", not "attribute with no value" — the SDK warns on
    the latter. Zero and empty string are real values and must survive."""
    assert span_attributes(a=1, b=None, c=0, d="") == {"a": 1, "c": 0, "d": ""}


# --- configuration invariants -----------------------------------------------
def test_the_default_profile_configures_no_exporter() -> None:
    """`deployment-architecture.md` rule 6: no default endpoint, ever.

    A built-in exporter target would be a configured egress path out of an air-gapped enclave that
    nobody chose — which is exactly what that rule forbids.
    """
    settings = Settings(app_env="development")

    assert settings.otel_exporter_otlp_endpoint == ""
    assert settings.otel_console_export is False


def test_console_export_is_refused_in_a_production_grade_profile() -> None:
    """A span per request on stdout buries the structured events Promtail ships to Loki."""
    settings = Settings(
        app_env="production",
        otel_console_export=True,
        kms_provider="vault_transit",
        malware_scanner_provider="clamav",
        storage_access_key="a-real-key",
        storage_secret_key="a-real-secret",
    )

    with pytest.raises(ConfigurationError, match="OTEL_CONSOLE_EXPORT"):
        settings.validate_for_profile()


def test_console_export_is_allowed_in_development() -> None:
    """The flag exists for a developer with no collector; that is the profile it is for."""
    Settings(app_env="development", otel_console_export=True).validate_for_profile()


@pytest.mark.parametrize("ratio", [-0.1, 1.5])
def test_a_sample_ratio_outside_the_unit_interval_is_refused(ratio: float) -> None:
    """Checked for every profile, including development: `TraceIdRatioBased` would clamp it
    silently, and a ratio outside [0, 1] is a typo, not a policy."""
    with pytest.raises(ConfigurationError, match="OTEL_TRACES_SAMPLE_RATIO"):
        Settings(app_env="development", otel_traces_sample_ratio=ratio).validate_for_profile()


def test_an_air_gapped_profile_may_still_configure_a_collector() -> None:
    """A collector inside the enclave is east-west traffic, not egress.

    Rule 6 forbids an endpoint *the platform* chose, not one the operator did — and an air-gapped
    deployment needs traces as much as any other. Asserted so a later "harden the air-gapped
    profile" change cannot quietly take observability away from the deployments that can least
    afford to debug blind.
    """
    settings = Settings(
        app_env="air-gapped",
        otel_exporter_otlp_endpoint="http://otel-collector.sentinelai-observability:4318/v1/traces",
        kms_provider="vault_transit",
        malware_scanner_provider="clamav",
        storage_access_key="a-real-key",
        storage_secret_key="a-real-secret",
    )

    settings.validate_for_profile()


# --- the root-span trap -----------------------------------------------------
def test_parent_context_returns_an_empty_context_not_none(exporter: InMemorySpanExporter) -> None:
    """The distinction the SDK makes, which is the opposite of what reads naturally.

    ``start_as_current_span(context=None)`` means "inherit whatever is current", not "no parent".
    Inside the dispatcher that is the relay's own ambient span, so returning ``None`` here would
    parent every untraced event onto unrelated internals — precisely the fabricated causal edge
    `context_from_traceparent` refuses to produce.
    """
    with trace.get_tracer("test").start_as_current_span("ambient"):
        context = parent_context(None)
        assert context is not None
        assert not trace.get_current_span(context).get_span_context().is_valid

        with trace.get_tracer("test").start_as_current_span("consume", context=context):
            pass

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["consume"].parent is None, "a root span, despite an ambient span being open"


def test_parent_context_keeps_a_usable_parent(exporter: InMemorySpanExporter) -> None:
    """And the other half: a valid traceparent is not discarded.

    Pinned because the obvious spelling — ``extracted or Context()`` — silently fails here: an
    OTel ``Context`` is a dict, so one carrying only a span is still falsy.
    """
    context = parent_context(_TRACEPARENT)

    assert format(trace.get_current_span(context).get_span_context().trace_id, "032x") == _TRACE_ID


# --- which exporter a profile gets ------------------------------------------
@pytest.fixture
def unconfigured() -> Iterator[None]:
    """A process that has installed no provider yet, restored afterwards.

    `configure_tracing` is deliberately once-per-process, so a test of it has to say "pretend this
    is a fresh process" explicitly. Two SDK globals move, and the second is the one that bites —
    see `tests/fixtures/tracing.py`.
    """
    with fresh_tracing_process():
        yield


def test_no_configuration_means_no_exporter() -> None:
    """§4, and the reason it is written this way: `deployment-architecture.md` rule 6.

    A built-in endpoint would be a configured egress path out of an air-gapped enclave that nobody
    chose. Spans are still created and context still propagates — they are dropped at the processor,
    so the propagation path this ADR exists for is exercised on every deployment rather than only
    where a collector happens to be wired.
    """
    assert _build_exporter(Settings(app_env="development")) is None


def test_the_console_flag_selects_the_console_exporter() -> None:
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter

    exporter = _build_exporter(Settings(app_env="development", otel_console_export=True))

    assert isinstance(exporter, ConsoleSpanExporter)


def test_a_configured_endpoint_selects_otlp_and_beats_the_console_flag() -> None:
    """Both configured means the deployment has a collector; the console flag is the fallback."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    exporter = _build_exporter(
        Settings(
            app_env="development",
            otel_console_export=True,
            otel_exporter_otlp_endpoint="http://collector:4318/v1/traces",
        )
    )

    assert isinstance(exporter, OTLPSpanExporter)


def test_whitespace_is_not_an_endpoint() -> None:
    """An env var set to blanks in a manifest is unset, not a hostname."""
    assert (
        _build_exporter(Settings(app_env="development", otel_exporter_otlp_endpoint="   ")) is None
    )


# --- installing the provider ------------------------------------------------
def test_configure_tracing_installs_a_provider_carrying_the_service_identity(
    unconfigured: None,
) -> None:
    """The resource attributes are what make a trace crossing the outbox legible in Tempo.

    Without a distinct `service.name` per process, the HTTP span and the consumer span it parents
    render as one service calling itself — which is precisely the hop an operator opened the trace
    to see.
    """
    configure_tracing(
        Settings(app_env="testing"), service_name="sentinelai-test", service_version="9.9.9"
    )

    provider = trace.get_tracer_provider()
    attributes = provider.resource.attributes
    assert attributes["service.name"] == "sentinelai-test"
    assert attributes["service.version"] == "9.9.9"
    assert attributes["deployment.environment"] == "testing"


def test_configure_tracing_keeps_the_first_provider(unconfigured: None) -> None:
    """Idempotent, and it says so in a log line rather than silently half-replacing anything.

    Two providers in one process means spans split across them, and the SDK ignores the second
    `set_tracer_provider` anyway — so the honest behaviour is to keep the first and not pretend.
    """
    configure_tracing(Settings(app_env="testing"), service_name="first")
    first = trace.get_tracer_provider()

    configure_tracing(Settings(app_env="testing"), service_name="second")

    assert trace.get_tracer_provider() is first
    assert first.resource.attributes["service.name"] == "first"


def test_configured_tracing_actually_produces_spans(unconfigured: None) -> None:
    """The end of the wiring: after configuration, `current_trace_id` reports a real trace.

    Before it, the API's no-op provider stands in and reports nothing — which is what every test in
    this file that asserts ``None`` depends on.
    """
    assert current_trace_id() is None

    configure_tracing(Settings(app_env="testing"), service_name="sentinelai-test")

    with trace.get_tracer("test").start_as_current_span("work"):
        assert current_trace_id() is not None


def test_a_configured_exporter_actually_receives_spans(
    unconfigured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch that attaches a processor at all: configure, create a span, see it exported.

    The exporter is substituted at `_build_exporter` rather than pointed at a real collector, so
    this exercises the production `BatchSpanProcessor` path — the one a deployment with an OTLP
    endpoint takes — without a listener or a network.

    Worth its own test because a provider with no processor attached still produces spans that
    *look* fine from inside the process. Nothing else would notice that they go nowhere.
    """
    memory = InMemorySpanExporter()
    monkeypatch.setattr(tracing, "_build_exporter", lambda _settings: memory)

    configure_tracing(
        Settings(app_env="development", otel_exporter_otlp_endpoint="http://collector:4318"),
        service_name="sentinelai-test",
    )
    with trace.get_tracer("test").start_as_current_span("exported-work"):
        pass
    trace.get_tracer_provider().force_flush()

    assert [span.name for span in memory.get_finished_spans()] == ["exported-work"]


# --- the instrumentation seam -----------------------------------------------
def test_sqlalchemy_instrumentation_targets_the_sync_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The async wrapper is not the object the instrumentation hooks, and it fails *silently*.

    Passing an `AsyncEngine` attaches to nothing: no error, no spans, and a DB layer that looks
    instrumented in code review. Pinned here because the failure mode is invisible at runtime.
    """
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from sqlalchemy.ext.asyncio import create_async_engine

    from sentinelai.platform.tracing import instrument_sqlalchemy

    seen: dict[str, object] = {}
    monkeypatch.setattr(
        SQLAlchemyInstrumentor,
        "instrument",
        lambda self, **kwargs: seen.update(kwargs),
        raising=True,
    )
    engine = create_async_engine("postgresql+asyncpg://user:pw@localhost/db")

    instrument_sqlalchemy(engine)

    assert seen["engine"] is engine.sync_engine
