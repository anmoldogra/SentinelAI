/**
 * Case workspace — Evidence tab (`/cases/:caseId/evidence`, frontend-architecture.md §25).
 *
 * **Two endpoints, composed in the client.** `GET /cases/{case_id}/evidence` returns
 * `CaseEvidenceLinkRead` — the *link* row only (`link_id`, `evidence_id`, `linked_by_user_id`,
 * `linked_at`). It carries no title, category, or integrity status, and that is by design:
 * `ingestion.evidence` belongs to another module and `database-design.md` forbids the
 * cross-schema foreign key that would let case-management join to it. Each row therefore resolves
 * its own `evidence_id` through the evidence feature's public surface, and the query cache makes
 * that cheap.
 *
 * **The two data sources fail independently, and the table is built around that.** The link
 * columns (`Linked`, `Linked by`) come from the list response and always render. Only the four
 * resolved columns depend on the per-row request, so a resolution failure degrades those cells
 * while the row itself — and crucially the evidence id, which is the forensically meaningful
 * value — stays on screen.
 *
 * That failure mode is real rather than theoretical: `GET /cases/{id}/evidence` is gated on
 * `require_case_access()` while `GET /evidence/{id}` is gated on `require_role("investigator")`,
 * so an analyst with case access but a different role sees the links and cannot resolve them.
 * Blanking the row for that user would hide evidence they are entitled to know is attached.
 */

import { useEffect, useId, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useEvidenceItem, type IntegrityVerificationStatus } from "@/features/evidence/public";
import { describeApiError } from "@/shared/api/errors";

import { useCaseEvidence } from "../api/useCaseEvidence";
import { useUnlinkEvidence } from "../api/useUnlinkEvidence";
import { AddEvidenceModal } from "../components/AddEvidenceModal";
import { formatDateTime } from "../format";
import type { CaseEvidenceLink } from "../types";

/** What the confirmation dialog needs to describe the pending removal. */
interface UnlinkTarget {
  evidenceId: string;
  /** The resolved title, or `null` when the row never resolved — then the id is all we can show. */
  title: string | null;
}

const ACTION_BASE =
  "rounded px-3 py-1.5 font-mono text-xs font-medium uppercase tracking-wider disabled:opacity-60";

const PANEL = "rounded-lg border border-border bg-surface";

const PANEL_LABEL = "font-mono text-xs uppercase tracking-wider text-text-muted";

const CELL = "px-4 py-3";

/**
 * Integrity treatment, in §19.1's status language: emerald for verified, rose for a failed hash
 * comparison, amber for still-pending, neutral for not-applicable. Keyed by the union so a new
 * backend value fails the build here until it has a treatment.
 */
const INTEGRITY_STYLES: Record<IntegrityVerificationStatus, string> = {
  verified: "bg-status-open/15 text-status-open",
  failed: "bg-danger/15 text-danger",
  pending: "bg-status-archived/15 text-status-archived",
  not_applicable: "bg-canvas text-text-muted",
};

function integrityStyle(status: string): string {
  return status in INTEGRITY_STYLES
    ? INTEGRITY_STYLES[status as IntegrityVerificationStatus]
    : "bg-canvas text-text-muted";
}

/**
 * §36: colour is never the only signal — the label is always rendered, so the badge reads
 * correctly in high-contrast mode and to a colour-blind analyst.
 */
function IntegrityBadge({ status }: { status: string | null }) {
  if (status === null) {
    return <span className="text-text-muted">—</span>;
  }
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 font-mono text-xs ${integrityStyle(status)}`}
    >
      {status}
    </span>
  );
}

/** Truncated for scanning, with the full value in `title` for copy/inspect. */
function ShortId({ value }: { value: string }) {
  return (
    <span className="font-mono text-xs" title={value}>
      {value.slice(0, 12)}
      <span className="text-text-muted">…</span>
    </span>
  );
}

/**
 * Navigation to the evidence detail route (§26).
 *
 * A real anchor, not an `onClick` on the row: only a link is keyboard reachable, announced as a
 * link, and openable in a new tab or middle-clicked — all of which §36's WCAG 2.1 AA target and
 * §31's table semantics require.
 *
 * Cross-feature navigation by URL, which §3 sanctions explicitly — no import of the evidence
 * feature is involved, so this creates no coupling between the two features.
 */
function EvidenceLink({ evidenceId, children }: { evidenceId: string; children: React.ReactNode }) {
  return (
    <Link
      to={`/evidence/${evidenceId}`}
      className="hover:underline focus-visible:underline focus-visible:outline-none"
    >
      {children}
    </Link>
  );
}

/** A cell awaiting resolution. Placeholder text is transparent so the column keeps its width. */
function ResolvingCell({ width }: { width: string }) {
  return (
    <td className={CELL}>
      <span className={`inline-block animate-pulse rounded bg-surface-raised ${width}`}>
        &nbsp;
      </span>
    </td>
  );
}

/**
 * One evidence row, resolving its own metadata.
 *
 * Each row owning its request is what lets the table stream in — a slow or forbidden item does
 * not hold up the rest — and the cache key is the evidence id, so the same item referenced from
 * two cases or a graph node is fetched once for the whole session.
 */
function EvidenceTableRow({
  link,
  onUnlink,
}: {
  link: CaseEvidenceLink;
  onUnlink: (target: UnlinkTarget) => void;
}) {
  const { data, isPending, isError } = useEvidenceItem(link.evidence_id);

  return (
    <tr className="hover:bg-canvas" aria-busy={isPending}>
      {isPending ? (
        <>
          <ResolvingCell width="w-48" />
          <ResolvingCell width="w-20" />
          <ResolvingCell width="w-24" />
          <ResolvingCell width="w-16" />
        </>
      ) : isError ? (
        <>
          {/*
           * The id survives the failure — it is the value an analyst can act on elsewhere. The
           * wording matches §12's shared FORBIDDEN/NOT_FOUND treatment, which deliberately does
           * not reveal whether the item exists but is inaccessible.
           */}
          {/*
           * Still a link even though resolution failed: the detail route's custody ledger is
           * authorized differently from the evidence item itself, so a user who cannot resolve
           * the metadata here may still be entitled to the ledger there.
           */}
          <td className={CELL}>
            <EvidenceLink evidenceId={link.evidence_id}>
              <ShortId value={link.evidence_id} />
            </EvidenceLink>
            <span className="ml-2 text-xs text-text-muted">Unavailable</span>
          </td>
          <td className={`${CELL} text-text-muted`}>—</td>
          <td className={`${CELL} text-text-muted`}>—</td>
          <td className={`${CELL} text-text-muted`}>—</td>
        </>
      ) : (
        <>
          <td className={CELL}>
            <EvidenceLink evidenceId={data.evidence_id}>
              <span className="font-medium">{data.title}</span>
            </EvidenceLink>
            <span className="mt-0.5 block">
              <ShortId value={data.evidence_id} />
            </span>
          </td>
          <td className={`${CELL} text-sm`}>{data.category}</td>
          <td className={`${CELL} font-mono text-xs text-text-muted`}>{data.artifact_type}</td>
          <td className={CELL}>
            <IntegrityBadge status={data.integrity_verification_status} />
          </td>
        </>
      )}

      {/* Link-owned columns: always available, never gated on resolution. */}
      <td className={`${CELL} font-mono text-xs text-text-muted`}>
        {formatDateTime(link.linked_at)}
      </td>
      <td className={CELL}>
        <ShortId value={link.linked_by_user_id} />
      </td>
      <td className={`${CELL} text-right`}>
        <button
          type="button"
          onClick={() => {
            onUnlink({ evidenceId: link.evidence_id, title: data?.title ?? null });
          }}
          // Rose on hover only: §19.1 reserves rose for destructive intent, and painting every
          // row's action red would make a table of ordinary evidence read as a wall of alarms.
          className={`${ACTION_BASE} border border-border hover:border-danger/40 hover:bg-danger/10 hover:text-danger`}
        >
          Unlink
        </button>
      </td>
    </tr>
  );
}

/**
 * Destructive-action confirmation (§17's confirmation category: "Requires explicit confirm; cancel
 * returns to prior state unchanged").
 *
 * Built on the native `<dialog>` for the same reason every other modal here is — the focus trap,
 * `Esc`, and background inerting come from the platform rather than from effects kept correct by
 * hand. Local to this route by design; a shared confirmation primitive is worth extracting on the
 * second caller, not the first.
 *
 * **The copy is the point.** In a forensics product an analyst must not have to guess whether
 * "unlink" destroys evidence. It says plainly that the artifact and its custody ledger survive,
 * that only the association is removed, and that the removal is itself recorded — all three of
 * which are true of the backend, which publishes `case.evidence_unlinked` and writes an audit row.
 */
function UnlinkConfirmDialog({
  target,
  isPending,
  error,
  onConfirm,
  onCancel,
}: {
  target: UnlinkTarget | null;
  isPending: boolean;
  error: unknown;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const triggerRef = useRef<HTMLElement | null>(null);
  const headingId = useId();

  const open = target !== null;

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) {
      return;
    }
    if (open && !dialog.open) {
      triggerRef.current = document.activeElement as HTMLElement | null;
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  function requestCancel() {
    onCancel();
    // Queued after the dialog closes so focus is not stolen back by the closing dialog. If the
    // row was removed the trigger is detached and this is a harmless no-op.
    window.setTimeout(() => triggerRef.current?.focus(), 0);
  }

  const apiError = error === null || error === undefined ? null : describeApiError(error);

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby={headingId}
      onCancel={(event) => {
        // `Esc` must not dismiss the dialog while the DELETE it started is still in flight.
        event.preventDefault();
        if (!isPending) {
          requestCancel();
        }
      }}
      className="m-auto w-[min(32rem,calc(100vw-2rem))] rounded-lg border border-border bg-surface-raised p-0 text-text shadow-2xl backdrop:bg-scrim"
    >
      <div className="border-b border-border px-6 py-4">
        <p className="font-mono text-xs uppercase tracking-widest text-text-muted">Confirm</p>
        <h2 id={headingId} className="mt-0.5 text-lg font-semibold tracking-tight">
          Unlink evidence from this case?
        </h2>
      </div>

      <div className="px-6 py-5">
        {target !== null && (
          <div className="rounded border border-border bg-canvas px-3 py-2">
            <p className="text-sm font-medium">{target.title ?? "Unresolved evidence item"}</p>
            <p className="mt-0.5 break-all font-mono text-xs text-text-muted">
              {target.evidenceId}
            </p>
          </div>
        )}

        <p className="mt-3 max-w-prose text-sm text-text-muted">
          This removes the association only. The evidence item and its chain of custody are not
          deleted, and it remains available to link again or to other cases. The removal is recorded
          in the case&rsquo;s audit trail.
        </p>

        {apiError !== null && (
          <div role="alert" className="mt-4 rounded border border-danger/40 bg-danger/10 px-3 py-2">
            <p className="text-sm font-medium text-danger">{apiError.title}</p>
            <p className="mt-0.5 text-sm text-text-muted">{apiError.detail}</p>
          </div>
        )}
      </div>

      <div className="flex justify-end gap-2 border-t border-border px-6 py-4">
        <button
          type="button"
          onClick={requestCancel}
          disabled={isPending}
          className={`${ACTION_BASE} border border-border hover:bg-canvas`}
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={onConfirm}
          disabled={isPending}
          aria-busy={isPending}
          className={`${ACTION_BASE} bg-danger text-accent-contrast hover:opacity-90`}
        >
          {isPending ? "Unlinking…" : "Unlink"}
        </button>
      </div>
    </dialog>
  );
}

/** §13: a skeleton matching the eventual row shape, so the table does not reflow when data lands. */
function EvidenceSkeleton() {
  return (
    <div className="divide-y divide-border" aria-hidden="true">
      {Array.from({ length: 4 }, (_, index) => (
        <div key={index} className="flex items-center gap-4 px-4 py-3">
          <div className="h-4 w-48 animate-pulse rounded bg-border" />
          <div className="h-4 flex-1 animate-pulse rounded bg-border" />
          <div className="h-4 w-40 animate-pulse rounded bg-border" />
        </div>
      ))}
    </div>
  );
}

function EvidenceError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
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

export function CaseEvidencePage() {
  const { caseId } = useParams<{ caseId: string }>();
  const { links, isPending, isError, error, refetch } = useCaseEvidence(caseId);

  // Transient UI state, owned by the component that uses it (§9).
  const [isAddOpen, setAddOpen] = useState(false);
  const [unlinkTarget, setUnlinkTarget] = useState<UnlinkTarget | null>(null);

  const {
    mutate: unlink,
    isPending: isUnlinking,
    error: unlinkError,
    reset: resetUnlink,
  } = useUnlinkEvidence(caseId);

  function closeUnlinkDialog() {
    setUnlinkTarget(null);
    // Clears a previous failure so reopening the dialog for another row does not show a stale one.
    resetUnlink();
  }

  return (
    <section aria-labelledby="evidence-heading" className="space-y-4">
      <div className="flex items-center justify-between gap-4">
        <h2 id="evidence-heading" className={PANEL_LABEL}>
          Linked evidence
        </h2>
        <button
          type="button"
          onClick={() => {
            setAddOpen(true);
          }}
          className="rounded bg-accent px-3 py-1.5 font-mono text-xs font-medium uppercase tracking-wider text-accent-contrast hover:opacity-90"
        >
          Add evidence
        </button>
      </div>

      <AddEvidenceModal
        open={isAddOpen}
        onClose={() => {
          setAddOpen(false);
        }}
      />

      {/*
       * §17 forbids modal stacking. These two never coexist: the picker is opened from the header
       * and the confirmation from a row, and whichever is open traps focus, so neither trigger is
       * reachable while the other is showing.
       */}
      <UnlinkConfirmDialog
        target={unlinkTarget}
        isPending={isUnlinking}
        error={unlinkError}
        onCancel={closeUnlinkDialog}
        onConfirm={() => {
          if (unlinkTarget !== null) {
            unlink(unlinkTarget.evidenceId, { onSuccess: closeUnlinkDialog });
          }
        }}
      />

      <div className={`${PANEL} overflow-x-auto`}>
        {isPending ? (
          <EvidenceSkeleton />
        ) : isError ? (
          <EvidenceError
            error={error}
            onRetry={() => {
              void refetch();
            }}
          />
        ) : links.length === 0 ? (
          <p className="px-4 py-10 text-center text-sm text-text-muted">
            No evidence is linked to this case yet.
          </p>
        ) : (
          <table className="w-full text-left">
            <thead className="border-b border-border text-xs uppercase tracking-wide text-text-muted">
              <tr>
                <th scope="col" className="px-4 py-2 font-medium">
                  Evidence
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Category
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Artifact type
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Integrity
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Linked
                </th>
                <th scope="col" className="px-4 py-2 font-medium">
                  Linked by
                </th>
                <th scope="col" className="px-4 py-2 text-right font-medium">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {links.map((item) => (
                <EvidenceTableRow key={item.link_id} link={item} onUnlink={setUnlinkTarget} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
