# 18. Observability and Distributed Tracing

## Status

**Accepted — Built (tracing)** in modernization Wave 4.3. This is the formal write-up
`engineering-roadmap.md`'s open-ADR register has carried as "Resolved — formal ADR write-up still
pending" for the monitoring stack, plus the decisions Wave 4.3 actually had to make: what crosses the
event bus, and what a deployment with no egress does.

| Decision | State |
|---|---|
| §1 OpenTelemetry as the tracing API/SDK; Tempo as the backend | **Decided** in `deployment-architecture.md` Part 20 — recorded here, not re-opened |
| §2 Trace context crosses the outbox in the existing `trace_id` column | **Built** — a W3C `traceparent`, injected at publish, continued at delivery |
| §3 Instrumentation: FastAPI + SQLAlchemy, both entrypoints | **Built** — guide Part 10's "FastAPI and SQLAlchemy at startup" |
| §4 Export is opt-in; no default endpoint | **Built** — the air-gapped invariant, below |
| §5 `trace_id` on every log line, so Loki joins to Tempo | **Built** — bound at the HTTP edge and in the dispatcher |
| §6 OTLP over HTTP/protobuf, not gRPC | **Built** — an air-gapped mirroring constraint, below |
| §7 Metrics and logs via OTel | **Not built** — Prometheus and structlog already serve both; see "what this does not change" |
| §8 Spans for arq jobs | **Not built** — no instrumentation package exists; see "carried forward" |

## §1 The stack was already decided; this records it

`deployment-architecture.md` Part 20 commits to "Tempo receives OpenTelemetry traces" and
`backend-implementation-guide.md` Part 10 to "OpenTelemetry instruments FastAPI and SQLAlchemy at
startup, using the same `trace_id` format `event-driven-architecture.md` §11 already specifies". Part
20 also says each such commitment "should be recorded as an ADR per `CLAUDE.md`'s convention", and
`engineering-roadmap.md`'s register lists this one as pending. So the *choice* of OpenTelemetry is
not litigated here — adopting it is, along with four decisions the documents leave open.

## §2 Trace context crosses the outbox as a `traceparent`, in the column that already exists

`event-driven-architecture.md` §9 gives every event a nullable `trace_id`, and §11 defines it as "W3C
Trace Context, generated at the entrypoint ... possibly spanning process/network boundaries". Until
now every publisher wrote `NULL` into it.

**What goes in the column is a full `traceparent`, not a bare 32-hex trace id**, and the distinction
is the whole mechanism. A `traceparent` is `version-traceid-spanid-flags`: it carries the trace *and
the span to attach to*. A consumer given only the trace id could label its span with the right trace
but could not be a **child** of anything in it — the result renders in Tempo as a pile of sibling
spans with no shape, which answers "did this happen?" but not "what caused this to take nine
seconds?". §11's own wording anticipates this: the field "changes at every process/network boundary
rather than staying constant like `correlation_id` does", which is true of a `traceparent` and false
of a bare trace id.

**Injected in `OutboxWriter.publish`, and nowhere else.** The span that belongs on an event is the one
open when the business transaction ran; by the time the dispatcher relays the row, seconds or minutes
later, that span is closed and its context gone. An explicit `trace_id=` argument still wins, so a
replay tool or backfill can state its own context — or `None`, for work that honestly has no trace.

**It is inside the signed envelope (ADR-0007), which is why an absent trace stays absent.**
`trace_id` is part of the signed field set. `current_traceparent()` therefore returns `None` rather
than a zeroed placeholder when nothing is being traced: a signature is an attestation, and attesting
to an execution path that never existed would make an evidentiary record assert something false about
how the evidence moved. The cost of getting this wrong is not a confusing dashboard.

**The consumer span is a child, not a link.** OTel's messaging conventions allow either; a child is
right here because the outbox is a continuation of one workflow rather than a fan-out to unrelated
consumers, and because a single trace spanning `POST /evidence → evidence.ingested →
threat_intel.ioc_matched → investigation.correlation_generated` is exactly the picture §11's causal
diagram draws. The trade is that a *retried* event can attach a child to a trace whose other spans
Tempo has already aged out, which renders as a partial trace. That is a legible failure — an operator
sees a lone consumer span and its `sentinelai.attempt` attribute — and it is preferable to severing
the relationship for every event to tidy up the rare one.

**A malformed or missing `trace_id` yields a root span, never a child of whatever the dispatcher was
inside.** `extract` does not raise on garbage; it returns the current context unchanged, so passing
its result through blindly would parent the handler onto an unrelated span and draw a causal edge
that does not exist. Inventing causality in a system whose whole purpose is evidentiary provenance is
the one failure mode worth writing a guard for.

## §3 One span per handler, not per event

Two handlers on one event succeed and fail independently, and a single span covering both would
attribute one's failure to the other. The unit an operator acts on is "this handler, on this event",
so that is the unit that gets a span, a status, and a recorded exception.

## §4 Export is opt-in, and the default is silence

`deployment-architecture.md` rule 6: air-gapped and classified deployments must have **zero
configured or observed egress paths — verify, don't assume**. A default OTLP endpoint would be a
configured path out of the enclave that nobody chose, which is precisely what that rule forbids. So
`OTEL_EXPORTER_OTLP_ENDPOINT` is empty by default and there is no fallback.

With no exporter the SDK still runs: spans are created, context still propagates through the outbox,
and they are dropped at the processor. That is deliberate. It means the propagation path is exercised
on every deployment and in the whole test suite, rather than being a code path that only executes
where nobody is watching it — the arrangement that lets tracing rot quietly between releases.

**Air-gapped deployments can still be traced**, and should be: a collector inside the enclave is
east-west traffic, not egress. What the profile forbids is an endpoint *the platform chose*, not one
the operator did. The related invariant that is checkable is enforced instead — console export is
refused in production-grade profiles, because a span per request on stdout would bury the structured
events Promtail ships to Loki under a volume of trace noise.

## §5 `trace_id` on the log line, in both processes

Part 20 promises logs and traces "correlatable by the same `correlation_id`/`trace_id`". The HTTP
correlation middleware binds the active trace id; the dispatcher binds it for the handler's span, so
asynchronous work carries the same two ids as the request that caused it. This is what makes a
Grafana pivot from a Loki line to the Tempo trace work at all, and it was the one part of Part 20's
promise that `platform/logging.py`'s own docstring already claimed and the code did not do.

`trace_id` is **not** echoed to clients in a response header, unlike `X-Request-Id` and
`X-Correlation-Id`. §11 is explicit that it "has no business meaning"; handing a caller a handle on
the platform's internal execution topology serves them nothing and describes the system to anyone
who asks.

## §6 OTLP over HTTP/protobuf, not gRPC

The gRPC exporter pulls in `grpcio`, a native extension with platform-specific wheels. Part 1's
air-gapped philosophy makes that a real cost: a native dependency turns an offline mirror into a
build toolchain, for a transport Tempo accepts either way. Same reasoning the `asn1crypto` choice
records in `pyproject.toml`.

## What this does not change

**Metrics stay on Prometheus** (`prometheus-fastapi-instrumentator`, `/metrics`, api-design.md §12)
and **logs stay on structlog → Promtail → Loki**. OTel can carry all three signals; adopting it for
the two that already work would be churn with a migration risk and no new answer — Part 20's
architecture is three pipelines into one Grafana, not one pipeline. Tracing is adopted because it is
the signal with *nothing* serving it.

## Consequences

- Five new runtime dependencies (`opentelemetry-{api,sdk}`, two instrumentation packages, the OTLP
  HTTP exporter). All pure-Python, all mirror cleanly.
- Every event published from a traced context now carries a `traceparent` **inside its signature**.
  A deployment that re-signs or re-verifies historical events sees no change: the field was always in
  the signed set, previously as `NULL`.
- Spans are created even when nothing exports them. Measured cost is a few microseconds per span
  against a no-op processor; the alternative is an untested propagation path.
- A retried or replayed event may attach a span to an aged-out trace, rendering as a partial trace.

## Carried forward

- **No spans for arq jobs.** There is no `opentelemetry-instrumentation-arq`, and hand-rolling one is
  a separate piece of work with its own failure modes. The worker still produces the dispatcher's
  consumer spans and the SQLAlchemy client spans beneath them; a scheduled job's own execution is
  visible in logs and metrics, not in a trace.
- **No OTel metrics or logs pipeline** (§7 above) — a deliberate non-adoption, not an oversight.
- **No alert rules shipped.** Part 20's alert-routing table names the signals; `infra/` carries no
  Alertmanager configuration yet, and Wave 4.3's alerting half remains open.
- **No sampling policy beyond a ratio knob.** `OTEL_TRACES_SAMPLE_RATIO` defaults to 1.0 and is
  applied `ParentBased`, so one decision covers a whole workflow rather than leaving traces with
  holes. Tail sampling — keeping the slow and failed traces specifically — is a collector-side
  configuration and belongs with the `infra/` work above.
