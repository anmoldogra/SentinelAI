/**
 * Evidence detail (`/evidence/:evidenceId`, frontend-architecture.md §26).
 *
 * A top-level route, not a route nested under a case: an evidence item exists independently of
 * any single case and can be linked to several, so its URL must not imply one owner.
 *
 * **The two panels load and fail independently, deliberately.** `GET /evidence/{id}` requires the
 * `investigator` role; `GET /evidence/{id}/custody-events` admits `investigator` *or*
 * `compliance`. A compliance user is entitled to the ledger and not the item, so each panel owns
 * its own pending and error state and neither gates the other. Coupling them would deny that user
 * a view they have every right to see.
 *
 * Read-only by design in this increment: the re-verify integrity action (§26), supersede, and
 * download are separate increments, and rendering disabled controls for them would advertise
 * capability that is not wired up.
 */

import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { describeApiError } from "@/shared/api/errors";

import { useEvidenceCustody } from "../api/useEvidenceCustody";
import { useEvidenceItem } from "../api/useEvidenceItem";
import type { CustodyEvent, Evidence, IntegrityVerificationStatus } from "../types";
import { verifyCustodyChain, type ChainVerification } from "../utils/verifyChain";

/** §25.1: panels are `surface` regions separated by borders, not by whitespace. */
const PANEL = "rounded-lg border border-border bg-surface";

/** §19.1's field-name role. */
const PANEL_LABEL = "font-mono text-xs uppercase tracking-wider text-text-muted";

const CELL = "px-4 py-3";

/**
 * Local rather than imported from `features/cases/format.ts`: that is another feature's internal
 * module, and §3 makes a feature's internals private. Timestamp formatting is not case-domain
 * logic and belongs in `shared/` — promoting it there is a cleanup that touches files outside
 * this increment, so the six lines live here until then.
 */
function formatDateTime(iso: string): string {
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString();
}

const INTEGRITY_STYLES: Record<IntegrityVerificationStatus, string> = {
  verified: "bg-status-open/15 text-status-open",
  failed: "bg-danger/15 text-danger",
  pending: "bg-status-archived/15 text-status-archived",
  not_applicable: "bg-canvas text-text-muted",
};

function badgeClass(extra: string): string {
  return `inline-flex items-center rounded-full px-2.5 py-0.5 font-mono text-xs ${extra}`;
}

/** §36: colour is never the only signal — the label is always rendered alongside it. */
function IntegrityBadge({ status }: { status: string | null }) {
  if (status === null) {
    return <span className="text-text-muted">—</span>;
  }
  const style =
    status in INTEGRITY_STYLES
      ? INTEGRITY_STYLES[status as IntegrityVerificationStatus]
      : "bg-canvas text-text-muted";
  return <span className={badgeClass(style)}>{status}</span>;
}

/**
 * Legal hold gets a badge only when it is active. A "not held" badge would be visual noise on
 * every item, whereas an active hold changes what may lawfully be done with the evidence
 * (`security-architecture.md` §39) and should be impossible to miss.
 */
function LegalHoldBadge({ held }: { held: boolean }) {
  if (!held) {
    return null;
  }
  return <span className={badgeClass("bg-danger/15 text-danger")}>legal hold</span>;
}

function ShortHash({ value }: { value: string }) {
  return (
    <span className="font-mono text-xs" title={value}>
      {value.slice(0, 16)}
      <span className="text-text-muted">…</span>
    </span>
  );
}

function BackLink() {
  return (
    <Link to="/cases" className="text-sm text-text-muted hover:text-text hover:underline">
      ← Back to cases
    </Link>
  );
}

function PanelError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const { title, detail, retryable } = describeApiError(error);
  return (
    <div role="alert" className="px-4 py-10 text-center">
      <p className="text-sm font-medium text-text">{title}</p>
      <p className="mx-auto mt-1 max-w-prose text-sm text-text-muted">{detail}</p>
      {retryable && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-4 rounded border border-border px-3 py-1.5 font-mono text-xs font-medium uppercase tracking-wider hover:bg-canvas"
        >
          Try again
        </button>
      )}
    </div>
  );
}

/** One label/value pair. `<dl>` semantics so the relationship is exposed, not just visual. */
function Field({
  label,
  mono = false,
  children,
}: {
  label: string;
  mono?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="px-6 py-3">
      <dt className={PANEL_LABEL}>{label}</dt>
      <dd className={`mt-1 text-sm ${mono ? "break-all font-mono text-xs" : ""}`}>{children}</dd>
    </div>
  );
}

/** §25.1's command header: the identity strip an analyst orients from. */
function CommandHeader({ item }: { item: Evidence }) {
  return (
    <header className={`${PANEL} px-6 py-4`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={PANEL_LABEL}>Evidence</span>
        <span className="break-all font-mono text-xs text-text-muted">{item.evidence_id}</span>
      </div>

      <h1 className="mt-2 text-2xl font-semibold tracking-tight">{item.title}</h1>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <span className={badgeClass("bg-canvas text-text-muted")}>{item.status}</span>
        <IntegrityBadge status={item.integrity_verification_status} />
        <LegalHoldBadge held={item.legal_hold} />
      </div>

      <div className="mt-2 flex flex-wrap gap-x-6 gap-y-1 font-mono text-xs text-text-muted">
        <span>Collected {formatDateTime(item.collected_at)}</span>
        <span>Ingested {formatDateTime(item.ingested_at)}</span>
      </div>
    </header>
  );
}

function MetadataPanel({ item }: { item: Evidence }) {
  return (
    <section aria-labelledby="metadata-heading" className={`${PANEL} overflow-hidden`}>
      <div className="border-b border-border px-6 py-3">
        <h2 id="metadata-heading" className={PANEL_LABEL}>
          Artifact metadata
        </h2>
      </div>
      <dl className="divide-y divide-border">
        <Field label="Category">{item.category}</Field>
        <Field label="Artifact type" mono>
          {item.artifact_type}
        </Field>
        <Field label="Status" mono>
          {item.status}
        </Field>
        {/* A string on the wire, not a number — the backend types it Decimal (see types.ts). */}
        <Field label="Confidence" mono>
          {item.confidence}
        </Field>
        <Field label="Integrity">
          <IntegrityBadge status={item.integrity_verification_status} />
        </Field>
        <Field label="Legal hold" mono>
          {item.legal_hold ? "true" : "false"}
        </Field>
        <Field label="Schema version" mono>
          {item.schema_version}
        </Field>
        <Field label="Evidence ID" mono>
          {item.evidence_id}
        </Field>
        <Field label="Description">
          {item.description ?? <span className="text-text-muted">No description</span>}
        </Field>
      </dl>
    </section>
  );
}

/**
 * The chain's cryptographic status, in §19.1's status language.
 *
 * Emerald verified, rose failed — and **amber, not rose, for "not checked"**. That distinction is
 * the whole point of the token split: amber is "needs attention", rose is "something broke", and
 * telling an analyst a ledger failed verification when the browser merely lacks Web Crypto would
 * be a false accusation of tampering on a legal record.
 *
 * `role="status"` with a polite live region so the outcome is announced when it resolves rather
 * than only appearing visually (§36), and the label always carries the meaning in text — colour
 * is never the sole signal.
 */
function VerificationBadge({ verification }: { verification: ChainVerification }) {
  const { style, label } = ((): { style: string; label: string } => {
    switch (verification.status) {
      case "verified":
        return {
          style: "bg-status-open/15 text-status-open",
          label: `chain verified · ${String(verification.count)} entries`,
        };
      case "failed":
        return { style: "bg-danger/15 text-danger", label: "verification failed" };
      case "unavailable":
        return { style: "bg-status-archived/15 text-status-archived", label: "not verified" };
      case "idle":
        return { style: "bg-canvas text-text-muted", label: "nothing to verify" };
      case "pending":
        return { style: "bg-canvas text-text-muted", label: "verifying…" };
    }
  })();

  return (
    <span role="status" aria-live="polite" className={badgeClass(style)}>
      {label}
    </span>
  );
}

/**
 * The explanation behind a non-verified badge.
 *
 * Rendered only when there is something an analyst must act on. A failure names the entry it
 * found, because "which entry" is the first question anyone asks of a broken custody chain; an
 * unavailable check states that the ledger was *not* assessed, so its silence is never mistaken
 * for a pass.
 */
function VerificationDetail({ verification }: { verification: ChainVerification }) {
  if (verification.status === "failed") {
    return (
      <div role="alert" className="border-b border-border bg-danger/10 px-6 py-3">
        <p className="text-sm font-medium text-danger">
          Integrity check failed at sequence {verification.sequenceNumber}
        </p>
        <p className="mt-0.5 text-sm text-text-muted">{verification.reason}</p>
      </div>
    );
  }
  if (verification.status === "unavailable") {
    return (
      <div className="border-b border-border px-6 py-3">
        <p className="text-sm text-text-muted">
          This ledger has not been cryptographically checked. {verification.reason}
        </p>
      </div>
    );
  }
  return null;
}

function CustodyRow({ event }: { event: CustodyEvent }) {
  return (
    <tr className="hover:bg-canvas">
      <td className={`${CELL} font-mono text-xs text-text-muted`}>{event.sequence_number}</td>
      <td className={`${CELL} font-mono text-xs`}>{formatDateTime(event.occurred_at)}</td>
      <td className={`${CELL} text-sm`}>{event.event_type}</td>
      <td className={CELL}>
        {event.actor_user_id === null ? (
          <span className="text-text-muted">—</span>
        ) : (
          <ShortHash value={event.actor_user_id} />
        )}
        {/*
         * The role as it was at the time of the action, not the actor's role today (CEM §4) —
         * which is exactly why it is worth showing beside the identifier rather than resolving
         * the user's current role.
         */}
        {event.actor_role !== null && (
          <span className="mt-0.5 block font-mono text-xs text-text-muted">{event.actor_role}</span>
        )}
      </td>
      <td className={CELL}>
        <ShortHash value={event.entry_hash} />
      </td>
      {/* Prose, so proportional rather than monospaced (§19.1), and width-bounded so a long
          note wraps instead of stretching the ledger's hash columns off-screen. */}
      <td className={`${CELL} max-w-xs text-sm`}>
        {event.notes ?? <span className="text-text-muted">—</span>}
      </td>
    </tr>
  );
}

function CustodySkeleton() {
  return (
    <div className="divide-y divide-border" aria-hidden="true">
      {Array.from({ length: 4 }, (_, index) => (
        <div key={index} className="flex items-center gap-4 px-4 py-3">
          <div className="h-4 w-8 animate-pulse rounded bg-border" />
          <div className="h-4 flex-1 animate-pulse rounded bg-border" />
          <div className="h-4 w-32 animate-pulse rounded bg-border" />
        </div>
      ))}
    </div>
  );
}

/**
 * The custody ledger (§26).
 *
 * Chain of custody is central to this product's legal value, so it gets its own clearly
 * chronological presentation rather than a buried tab. The ledger renders in the server's order
 * and is never client-sorted (`api-design.md` §5: "this is a legal ledger, not a flexible list
 * view").
 *
 * The hashes are now recomputed in the browser rather than displayed on trust — see
 * `utils/verifyChain.ts`. The badge in the header reports that result, and it reports it
 * honestly: an environment without Web Crypto reads as *not checked*, never as failed, because a
 * missing platform API says nothing whatsoever about the ledger's integrity.
 */
function CustodyPanel({ evidenceId }: { evidenceId: string | undefined }) {
  const { events, data, isPending, isError, error, refetch } = useEvidenceCustody(evidenceId);
  const [verification, setVerification] = useState<ChainVerification>({ status: "pending" });

  /*
   * Verification is async, so it cannot be a React Query `select` (those must be synchronous).
   *
   * The dependency is `data` — the envelope straight from the cache — and deliberately not the
   * `events` array: that one is `data?.data ?? []`, which allocates a fresh `[]` on every render
   * while the query is pending, and depending on it would re-run this effect every render and
   * loop through `setVerification`. `data` is reference-stable under React Query's structural
   * sharing.
   */
  useEffect(() => {
    const rows = data?.data;
    if (rows === undefined) {
      setVerification({ status: "pending" });
      return;
    }

    let cancelled = false;
    setVerification({ status: "pending" });
    void verifyCustodyChain(rows).then((result) => {
      // A result arriving after the user navigated to another item must not be shown against it.
      if (!cancelled) {
        setVerification(result);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [data]);

  return (
    <section aria-labelledby="custody-heading" className={`${PANEL} overflow-hidden`}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-6 py-3">
        <h2 id="custody-heading" className={PANEL_LABEL}>
          Chain of custody
        </h2>
        {!isPending && !isError && events.length > 0 && (
          <VerificationBadge verification={verification} />
        )}
      </div>

      {!isPending && !isError && <VerificationDetail verification={verification} />}

      <div className="overflow-x-auto">
        {isPending ? (
          <CustodySkeleton />
        ) : isError ? (
          <PanelError
            error={error}
            onRetry={() => {
              void refetch();
            }}
          />
        ) : events.length === 0 ? (
          <p className="px-4 py-10 text-center text-sm text-text-muted">
            No custody events recorded.
          </p>
        ) : (
          <table className="w-full text-left">
            <thead className="border-b border-border text-xs uppercase tracking-wide text-text-muted">
              <tr>
                <th scope="col" className="px-4 py-2 font-medium">
                  Seq
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Occurred
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Event
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Actor
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Entry hash
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Notes
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {events.map((event) => (
                <CustodyRow key={event.custody_event_id} event={event} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}

function EvidenceHeaderSkeleton() {
  return (
    <div className={`${PANEL} space-y-3 px-6 py-4`} aria-hidden="true">
      <div className="h-3 w-64 animate-pulse rounded bg-border" />
      <div className="h-7 w-1/3 animate-pulse rounded bg-border" />
      <div className="h-4 w-48 animate-pulse rounded bg-border" />
    </div>
  );
}

export function EvidenceDetailPage() {
  const { evidenceId } = useParams<{ evidenceId: string }>();
  const { data, isPending, isError, error, refetch } = useEvidenceItem(evidenceId);

  return (
    <section className="space-y-4">
      <BackLink />

      {isPending ? (
        <EvidenceHeaderSkeleton />
      ) : isError ? (
        <div className={PANEL}>
          <PanelError
            error={error}
            onRetry={() => {
              void refetch();
            }}
          />
        </div>
      ) : (
        <CommandHeader item={data} />
      )}

      {/*
       * The ledger keeps its own column even while the item above is unresolved — the two
       * requests are authorized differently, so one failing must not blank the other.
       */}
      <div className="grid gap-4 xl:grid-cols-3">
        <div className="xl:col-span-1">
          {isPending ? (
            <div className={`${PANEL} h-64 animate-pulse`} aria-hidden="true" />
          ) : isError ? null : (
            <MetadataPanel item={data} />
          )}
        </div>
        <div className="xl:col-span-2">
          <CustodyPanel evidenceId={evidenceId} />
        </div>
      </div>
    </section>
  );
}
