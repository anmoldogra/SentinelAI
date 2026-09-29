"""OpenTelemetry tracing — ADR-0018, deployment-architecture.md Part 20, guide Part 10.

The third signal beside `logging.py`'s structured logs and `/metrics`' Prometheus counters, and the
one that crosses process boundaries: Tempo receives these spans, and Grafana joins them to Loki logs
on the same `trace_id` Part 20 already promised was correlatable.

**Nothing here decides whether to trace — configuration does, and the default is silence.** With no
exporter configured the SDK still runs: spans are created, context still propagates, and they are
dropped at the processor. That is deliberate rather than lazy. `deployment-architecture.md` rule 6
requires air-gapped and classified deployments to have "zero configured or observed egress paths",
so a default endpoint — any default endpoint — would be a path out of the enclave that nobody chose.
An operator names their in-cluster collector or gets no export, and a collector inside the
enclave is east-west traffic, not egress, which is why those profiles can be traced at all.

**Configuring the provider is separate from instrumenting anything.** `configure_tracing` is called
once per process at startup; `instrument_fastapi`/`instrument_sqlalchemy` attach to objects the
entrypoint owns. Splitting them is what lets a test build an app without a tracer provider, and a
worker configure a provider with no FastAPI in sight.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from sentinelai.platform.config import Settings
from sentinelai.platform.logging import log

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine

# The W3C Trace Context header name, which is also the shape `event-driven-architecture.md` §9's
# `trace_id` column carries. §11 calls the field "W3C Trace Context, generated at the entrypoint"
# and notes it "changes at every process/network boundary" — a `traceparent` is that: a constant
# trace id plus the *current* span id, which is what a consumer needs to attach itself as a child.
# The bare 32-hex trace id alone could not do that; there would be nothing to be a child of.
TRACEPARENT: Final = "traceparent"

_TRACER_NAME: Final = "sentinelai"

_propagator = TraceContextTextMapPropagator()
_configured = False


def configure_tracing(
    settings: Settings, *, service_name: str, service_version: str = "0.0.0"
) -> None:
    """Install the process-wide tracer provider. Call once, at entrypoint startup.

    Idempotent by design: calling it twice keeps the first provider rather than raising or silently
    replacing it. Two providers in one process means spans split across them, and the second
    ``set_tracer_provider`` would be ignored by the SDK anyway — better to say so once in a log line
    than to leave an operator wondering why half a trace is missing.

    ``service_name`` distinguishes the two processes of the one deployable (`sentinelai-http` and
    `sentinelai-worker`), which is what makes a trace crossing the outbox legible in Tempo: without
    it both halves render as one service talking to itself.
    """
    global _configured
    if _configured:
        log.debug("tracing_already_configured", service_name=service_name)
        return

    exporter = _build_exporter(settings)
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name,
                "service.version": service_version,
                "deployment.environment": settings.app_env,
            }
        ),
        # `ParentBased` so a sampling decision made at the HTTP entrypoint is honoured by every
        # downstream span, including the consumer spans this module's outbox propagation creates.
        # Sampling each hop independently would produce traces with holes in them, which is worse
        # than either sampling the whole workflow or dropping it.
        sampler=ParentBased(TraceIdRatioBased(settings.otel_traces_sample_ratio)),
    )
    if exporter is not None:
        # Batched for the network exporter, immediate for the console one: a developer watching
        # stdout wants the span when it ends, not when a batch flushes.
        processor = (
            SimpleSpanProcessor(exporter)
            if isinstance(exporter, ConsoleSpanExporter)
            else BatchSpanProcessor(exporter)
        )
        provider.add_span_processor(processor)

    trace.set_tracer_provider(provider)
    _configured = True
    log.info(
        "tracing_configured",
        service_name=service_name,
        exporter=type(exporter).__name__ if exporter is not None else "none",
        sample_ratio=settings.otel_traces_sample_ratio,
    )


def _build_exporter(settings: Settings) -> SpanExporter | None:
    """The exporter this profile asks for, or ``None`` — which means spans are dropped, not refused.

    An OTLP endpoint wins over the console because a deployment that has both configured is a
    deployment with a collector; the console flag exists for a developer without one.
    """
    endpoint = settings.otel_exporter_otlp_endpoint.strip()
    if endpoint:
        # Imported here rather than at module scope: the OTLP exporter pulls in the protobuf
        # encoder and an HTTP session, and a process that exports nothing should pay for neither.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(endpoint=endpoint)
    if settings.otel_console_export:
        return ConsoleSpanExporter()
    return None


def get_tracer() -> trace.Tracer:
    """The platform tracer. Safe before ``configure_tracing`` — a no-op provider stands in."""
    return trace.get_tracer(_TRACER_NAME)


def current_trace_id() -> str | None:
    """The active trace as 32 lowercase hex, or ``None`` when nothing is being traced.

    The id Tempo knows a trace by, bound into the log context so Grafana can join a Loki line to
    the trace it came from — Part 20 promised that correlation; this is what makes it true.

    ``None`` rather than the SDK's all-zero invalid id, which would hand an operator a trace id that
    resolves to nothing and looks like a lost trace rather than an untraced process.
    """
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


def current_traceparent() -> str | None:
    """The active span as a ``traceparent`` string, or ``None`` when nothing is being traced.

    ``None`` rather than a synthetic all-zero id, and that distinction is load-bearing: the outbox
    signs `trace_id` along with the rest of the envelope (ADR-0007), so writing a placeholder would
    put an attested claim about execution into a signed evidentiary record that no trace can back.
    An absent trace is an honest absence; an invalid one is a lie with a signature on it.

    The propagator injects nothing when the current span context is invalid, so an empty carrier is
    exactly the "no active span" case — no separate check needed.
    """
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    return carrier.get(TRACEPARENT)


def context_from_traceparent(traceparent: str | None) -> Context | None:
    """Parse a stored ``traceparent`` into a context to parent a span on, or ``None``.

    ``None`` for anything unusable — absent, malformed, or a well-formed header whose span context
    is invalid. The distinction matters because ``extract`` does not raise on garbage: it returns
    the *current* context unchanged, so a handler that passed that straight through would silently
    parent the consumer span onto whatever the dispatcher happened to be inside, inventing a causal
    link that does not exist. Returning ``None`` starts a root span instead, which is the truthful
    rendering of "this event arrived with no usable trace".
    """
    if not traceparent:
        return None
    context = _propagator.extract({TRACEPARENT: traceparent})
    if not trace.get_current_span(context).get_span_context().is_valid:
        return None
    return context


def parent_context(traceparent: str | None) -> Context:
    """The context to start a consumer span in: the producer's, or an explicitly empty one.

    **Empty, never ``None``** — and this is a trap worth naming, because it silently defeats the
    guard in `context_from_traceparent`. Passing ``context=None`` to ``start_as_current_span`` does
    not mean "no parent"; it means "use whatever context is current", which inside the dispatcher is
    the dispatcher's own ambient span. An event that arrived with an unusable `trace_id` would then
    be parented onto unrelated relay internals — a causal edge invented by the very code path
    written to avoid inventing one.

    An empty ``Context`` is the way to say "root span" to the SDK, so that is what an event with no
    usable trace gets.
    """
    extracted = context_from_traceparent(traceparent)
    # Not `or`: an empty `Context` is a dict and therefore falsy, so `extracted or Context()` would
    # discard a perfectly good parent whose context happened to hold nothing else.
    return Context() if extracted is None else extracted


def instrument_fastapi(app: FastAPI) -> None:
    """Attach ASGI instrumentation to one application (server spans for every request).

    Per-app rather than global: `create_app()` is called many times in the test suite, and the
    global `FastAPIInstrumentor().instrument()` would patch the class for the whole process.
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        # The probes Kubernetes and Prometheus poll on a schedule. Tracing them would bury real
        # request traces under liveness checks, and the answer they give is already on `/metrics`.
        excluded_urls="healthz,readyz,startupz,metrics",
    )


def instrument_sqlalchemy(engine: AsyncEngine) -> None:
    """Attach client spans to one engine's queries (guide Part 10's "FastAPI and SQLAlchemy").

    Takes the async engine's `sync_engine`, which is the object the instrumentation hooks; passing
    the async wrapper attaches to nothing and fails silently.
    """
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)


def reset_tracing_for_tests() -> None:
    """Forget that a provider was installed, so a test can configure one again.

    Test-only, and named so. The SDK keeps the first provider set in a process for good reasons,
    which is exactly why a test asserting configuration behaviour needs an explicit way to say "this
    is a new process, pretend".
    """
    global _configured
    _configured = False


def span_attributes(**values: Any) -> dict[str, Any]:
    """Drop ``None`` values from span attributes.

    The SDK warns (and in strict builds refuses) on a ``None`` attribute value, and an event
    legitimately has no `causation_id` at the start of a workflow. An absent attribute is the
    correct encoding of "there isn't one".
    """
    return {key: value for key, value in values.items() if value is not None}


__all__ = [
    "TRACEPARENT",
    "configure_tracing",
    "context_from_traceparent",
    "current_trace_id",
    "current_traceparent",
    "get_tracer",
    "instrument_fastapi",
    "instrument_sqlalchemy",
    "parent_context",
    "reset_tracing_for_tests",
    "span_attributes",
]
