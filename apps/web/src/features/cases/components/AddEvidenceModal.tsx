/**
 * Evidence-adding modal (frontend-architecture.md §17's form-modal category).
 *
 * Two ways to put evidence on a case, as two tabs over one dialog: **link an item already in the
 * store**, or **upload a new artifact**. They are tabs rather than two separate modals because
 * they answer the same question — "what evidence belongs to this case?" — and an analyst who opens
 * the picker, finds nothing, and needs to upload should not have to back out and find a different
 * button.
 *
 * The dialog mechanics come from the platform, not from hand-written effects: native `<dialog>` +
 * `showModal()` gives the focus trap, `Esc` handling, and background inerting §17 and §36 require.
 * Focus restoration to the trigger is explicit, since that is the part browsers have historically
 * been inconsistent about.
 *
 * **The cross-feature boundary.** Everything about evidence — the list, the upload flow, the
 * registered artifact types — comes from `@/features/evidence/public`, the evidence feature's
 * declared surface, never from its internals. §3 makes a feature's internals private, and
 * `eslint.config.js` fails the build on a deeper import, so this is enforced rather than merely
 * intended.
 *
 * **`caseId` comes from the route, not from a prop.** This modal only ever opens inside
 * `/cases/:caseId/evidence`, and §9 makes the URL the source of truth for which case is open —
 * the same reasoning `CaseOverviewPage` already follows by re-reading the case from the cache
 * instead of receiving it through props.
 */

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import {
  BASELINE_EVIDENCE_SCHEMAS,
  EVIDENCE_TITLE_MAX_LENGTH,
  LEGAL_AUTHORITY_REQUIRED_CATEGORIES,
  PUBLIC_SOURCE_SENTINEL,
  useEvidence,
  useUploadEvidence,
  type Evidence,
  type UploadEvidenceInput,
  type UploadPhase,
} from "@/features/evidence/public";
import { describeApiError } from "@/shared/api/errors";

import { useCaseEvidence } from "../api/useCaseEvidence";
import { useLinkEvidence } from "../api/useLinkEvidence";

interface AddEvidenceModalProps {
  open: boolean;
  onClose: () => void;
}

const ACTION_BASE =
  "rounded px-3 py-1.5 font-mono text-xs font-medium uppercase tracking-wider disabled:opacity-60";

const FIELD_LABEL = "font-mono text-xs uppercase tracking-wider text-text-muted";

/** Fields sit on `canvas` — a step *below* the raised panel — so the input well reads as recessed. */
const FIELD_CONTROL =
  "rounded border border-border bg-canvas px-3 py-2 text-sm outline-none focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-accent/40 disabled:opacity-60";

type Tab = "existing" | "upload";

/**
 * What the button says during each step of the upload.
 *
 * Named per phase rather than a single "Working…", because the steps fail and stall for different
 * reasons: hashing is local and scales with file size, uploading is network-bound and can hang on
 * a bad link. An analyst watching a large extraction needs to know which one they are waiting for.
 */
const PHASE_LABEL: Record<UploadPhase, string> = {
  idle: "Uploading…",
  reserving: "Reserving…",
  hashing: "Hashing…",
  uploading: "Uploading…",
  finalizing: "Recording…",
};

/** Truncated for scanning, full value in `title` for copy/inspect (§19.1). */
function ShortId({ value }: { value: string }) {
  return (
    <span className="font-mono text-xs text-text-muted" title={value}>
      {value.slice(0, 12)}
      <span>…</span>
    </span>
  );
}

/**
 * One selectable row.
 *
 * A native radio in a `<fieldset>`, not a custom listbox: radios are keyboard-navigable, announced
 * as a group with a position, and selectable without JavaScript-driven roving focus — everything
 * §36's WCAG 2.1 AA target needs, for free and correctly.
 */
function EvidenceOption({
  item,
  name,
  checked,
  disabled,
  onSelect,
}: {
  item: Evidence;
  name: string;
  checked: boolean;
  disabled: boolean;
  onSelect: (evidenceId: string) => void;
}) {
  return (
    <label
      className={`flex cursor-pointer items-start gap-3 border-b border-border px-4 py-3 last:border-b-0 ${
        checked ? "bg-accent/10" : "hover:bg-canvas"
      } ${disabled ? "cursor-not-allowed opacity-60" : ""}`}
    >
      <input
        type="radio"
        name={name}
        value={item.evidence_id}
        checked={checked}
        disabled={disabled}
        onChange={() => {
          onSelect(item.evidence_id);
        }}
        className="mt-1 accent-accent"
      />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{item.title}</span>
        <span className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5">
          <ShortId value={item.evidence_id} />
          <span className="font-mono text-xs text-text-muted">{item.artifact_type}</span>
        </span>
      </span>
    </label>
  );
}

export function AddEvidenceModal({ open, onClose }: AddEvidenceModalProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  /** The element focused before the dialog opened, so it can be focused again on close (§17). */
  const triggerRef = useRef<HTMLElement | null>(null);
  /**
   * A file input's value cannot be assigned from state — the only permitted write is `""`, which
   * clears it. So the reset-on-open below reaches for the element directly rather than pretending
   * this is a controlled field.
   */
  const fileInputRef = useRef<HTMLInputElement>(null);
  const existingTabRef = useRef<HTMLButtonElement>(null);
  const uploadTabRef = useRef<HTMLButtonElement>(null);

  const headingId = useId();
  const listName = useId();
  const searchId = useId();
  const existingTabId = useId();
  const uploadTabId = useId();
  const existingPanelId = useId();
  const uploadPanelId = useId();
  const fileId = useId();
  const categoryId = useId();
  const artifactTypeId = useId();
  const titleId = useId();
  const authorityId = useId();
  const authorityHintId = useId();

  const { caseId } = useParams<{ caseId: string }>();
  const [tab, setTab] = useState<Tab>("existing");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Upload form state. Kept as individual fields rather than one object: they are independent,
  // and `category` drives `artifactType` in a way a single setter would obscure.
  const [file, setFile] = useState<File | null>(null);
  const [category, setCategory] = useState("");
  const [artifactType, setArtifactType] = useState("");
  const [uploadTitle, setUploadTitle] = useState("");
  const [authorityRef, setAuthorityRef] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  /*
   * Two pieces of state, not one: `search` is what the analyst is typing and must update on every
   * keystroke to keep the input responsive, while `debouncedSearch` is what the query keys on.
   * Collapsing them would fire a request per character.
   */
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedSearch(search);
    }, 300);
    return () => {
      window.clearTimeout(timer);
    };
  }, [search]);

  // Gated on `open`: the evidence store is a whole-table read, and no case screen needs it until
  // the analyst actually opens the picker.
  const {
    evidence,
    isPending: isListPending,
    isError: isListError,
    error: listError,
    isFetching: isListFetching,
    hasNextPage,
    fetchNextPage,
    isFetchingNextPage,
  } = useEvidence({ enabled: open, text: debouncedSearch });

  // Already-linked items are filtered out rather than offered and rejected: linking a duplicate
  // raises `EvidenceAlreadyLinkedError` (409), and an avoidable server error is a worse
  // experience than simply not showing the option.
  const { links } = useCaseEvidence(caseId);
  const linkedIds = useMemo(() => new Set(links.map((link) => link.evidence_id)), [links]);
  const selectable = useMemo(
    () => evidence.filter((item) => !linkedIds.has(item.evidence_id)),
    [evidence, linkedIds],
  );

  const { mutate, isPending: isLinking, error: linkError, reset } = useLinkEvidence(caseId);
  const {
    mutate: upload,
    isPending: isUploading,
    error: uploadError,
    phase,
    reset: resetUpload,
  } = useUploadEvidence();

  /** Nothing may be dismissed, switched, or edited while bytes are in flight or a link is pending. */
  const isBusy = isLinking || isUploading;

  // The categories the registry actually holds, de-duplicated in declaration order. A module
  // constant, so this is computed once rather than per render.
  const categories = useMemo(() => {
    const byValue = new Map<string, string>();
    for (const schema of BASELINE_EVIDENCE_SCHEMAS) {
      if (!byValue.has(schema.category)) {
        byValue.set(schema.category, schema.categoryLabel);
      }
    }
    return [...byValue].map(([value, label]) => ({ value, label }));
  }, []);

  const artifactTypes = useMemo(
    () => BASELINE_EVIDENCE_SCHEMAS.filter((schema) => schema.category === category),
    [category],
  );

  const authorityRequired = LEGAL_AUTHORITY_REQUIRED_CATEGORIES.includes(category);

  // Drive the native dialog from the `open` prop, and clear the previous attempt's state and
  // errors each time it opens so nothing bleeds into the next one.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) {
      return;
    }
    if (open && !dialog.open) {
      triggerRef.current = document.activeElement as HTMLElement | null;
      setTab("existing");
      setSelectedId(null);
      setSearch("");
      setDebouncedSearch("");
      setFile(null);
      setCategory("");
      setArtifactType("");
      setUploadTitle("");
      setAuthorityRef("");
      setFormError(null);
      if (fileInputRef.current !== null) {
        fileInputRef.current.value = "";
      }
      reset();
      resetUpload();
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open, reset, resetUpload]);

  function requestClose() {
    onClose();
    // Queued after the dialog actually closes, so focus is not stolen back by the closing dialog.
    window.setTimeout(() => triggerRef.current?.focus(), 0);
  }

  function selectTab(next: Tab) {
    setTab(next);
    setFormError(null);
  }

  /**
   * Arrow-key navigation with selection following focus — the ARIA authoring practice for tabs
   * whose panels are already mounted and cheap to show. `Home`/`End` are omitted deliberately:
   * with exactly two tabs they would duplicate the arrows.
   */
  function handleTabKeyDown(event: React.KeyboardEvent<HTMLButtonElement>) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") {
      return;
    }
    event.preventDefault();
    const next: Tab = tab === "existing" ? "upload" : "existing";
    selectTab(next);
    (next === "existing" ? existingTabRef : uploadTabRef).current?.focus();
  }

  function handleLinkSubmit() {
    if (selectedId === null) {
      return;
    }
    mutate({ evidence_id: selectedId }, { onSuccess: requestClose });
  }

  /**
   * Client-side checks are UX only (§15) — they exist so the analyst is told about a missing field
   * before a round trip, never as the authority. Every one of them is also enforced server-side,
   * and the server's rejection is what the error region reports if these are somehow passed.
   */
  function handleUploadSubmit() {
    if (file === null) {
      setFormError("Choose a file to upload.");
      return;
    }
    if (category.length === 0 || artifactType.length === 0) {
      setFormError("Choose a category and artifact type.");
      return;
    }
    const trimmedTitle = uploadTitle.trim();
    if (trimmedTitle.length === 0) {
      setFormError("A title is required.");
      return;
    }
    const trimmedAuthority = authorityRef.trim();
    if (authorityRequired && trimmedAuthority.length === 0) {
      setFormError(
        `A legal authority reference is required for ${category} evidence (or the ` +
          `“${PUBLIC_SOURCE_SENTINEL}” sentinel).`,
      );
      return;
    }
    setFormError(null);

    // `exactOptionalPropertyTypes` is on, so a blank reference is omitted rather than sent as
    // `undefined` — and the backend's field is nullable, so omitting it is correct.
    const input: UploadEvidenceInput =
      trimmedAuthority.length > 0
        ? {
            file,
            category,
            artifact_type: artifactType,
            title: trimmedTitle,
            legal_authority_ref: trimmedAuthority,
          }
        : { file, category, artifact_type: artifactType, title: trimmedTitle };

    /*
     * The two mutations are chained, not merged. Uploading creates evidence that exists in its own
     * right — it is a valid record whether or not this case ends up referencing it — so linking is
     * a second, separate evidentiary act with its own audit trail and its own `case.evidence_linked`
     * event. If the link fails, the upload is not rolled back: the artifact is in the store, its
     * custody ledger has begun, and the analyst can link it from the other tab. The error region
     * says which half failed.
     */
    upload(input, {
      onSuccess: (created) => {
        mutate({ evidence_id: created.evidence_id }, { onSuccess: requestClose });
      },
    });
  }

  function handleSubmit(event: React.SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    if (tab === "existing") {
      handleLinkSubmit();
    } else {
      handleUploadSubmit();
    }
  }

  const apiError =
    (uploadError ?? linkError) === null ? null : describeApiError(uploadError ?? linkError);
  const listFailure = isListError ? describeApiError(listError) : null;

  const submitLabel = isUploading
    ? PHASE_LABEL[phase]
    : isLinking
      ? "Linking…"
      : tab === "existing"
        ? "Link evidence"
        : "Upload and link";

  const submitDisabled = isBusy || (tab === "existing" && selectedId === null);

  function tabClassName(value: Tab) {
    return `-mb-px border-b-2 px-3 py-2 font-mono text-xs uppercase tracking-wider disabled:opacity-60 ${
      tab === value
        ? "border-accent text-text"
        : "border-transparent text-text-muted hover:text-text"
    }`;
  }

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby={headingId}
      onCancel={(event) => {
        // `Esc` fires `cancel`; block it mid-flight so the dialog cannot vanish while an upload or
        // link it started is still running.
        event.preventDefault();
        if (!isBusy) {
          requestClose();
        }
      }}
      className="m-auto w-[min(36rem,calc(100vw-2rem))] rounded-lg border border-border bg-surface-raised p-0 text-text shadow-2xl backdrop:bg-scrim"
    >
      <form onSubmit={handleSubmit}>
        <div className="border-b border-border px-6 pt-4">
          <p className="font-mono text-xs uppercase tracking-widest text-text-muted">Evidence</p>
          <h2 id={headingId} className="mt-0.5 text-lg font-semibold tracking-tight">
            Add evidence
          </h2>

          <div role="tablist" aria-label="How to add evidence" className="mt-3 flex gap-1">
            <button
              ref={existingTabRef}
              type="button"
              role="tab"
              id={existingTabId}
              aria-selected={tab === "existing"}
              aria-controls={existingPanelId}
              tabIndex={tab === "existing" ? 0 : -1}
              disabled={isBusy}
              onClick={() => {
                selectTab("existing");
              }}
              onKeyDown={handleTabKeyDown}
              className={tabClassName("existing")}
            >
              Select existing
            </button>
            <button
              ref={uploadTabRef}
              type="button"
              role="tab"
              id={uploadTabId}
              aria-selected={tab === "upload"}
              aria-controls={uploadPanelId}
              tabIndex={tab === "upload" ? 0 : -1}
              disabled={isBusy}
              onClick={() => {
                selectTab("upload");
              }}
              onKeyDown={handleTabKeyDown}
              className={tabClassName("upload")}
            >
              Upload new
            </button>
          </div>
        </div>

        <div className="px-6 py-5">
          {/*
           * Both panels stay mounted and one is `hidden`, rather than being conditionally
           * rendered: switching tabs then preserves the search text, the fetched pages, and the
           * half-filled upload form, instead of discarding an analyst's work on a stray click.
           */}
          <div
            role="tabpanel"
            id={existingPanelId}
            aria-labelledby={existingTabId}
            hidden={tab !== "existing"}
          >
            <fieldset disabled={isBusy}>
              <legend className={`${FIELD_LABEL} mb-2`}>Select an item to link</legend>

              <div className="mb-2 flex items-center gap-2">
                <label htmlFor={searchId} className="sr-only">
                  Search evidence by title or description
                </label>
                <input
                  id={searchId}
                  type="search"
                  value={search}
                  onChange={(event) => {
                    setSearch(event.target.value);
                  }}
                  // Says what the server actually matches. The backend's `text` filter is an
                  // ILIKE over title and description only — an analyst pasting an evidence ID
                  // would otherwise get an empty list and conclude the item does not exist.
                  placeholder="Search title or description…"
                  className="flex-1 rounded border border-border bg-canvas px-3 py-2 text-sm outline-none focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-accent/40"
                />
                {/*
                 * Only while refreshing an existing list — never during the first load, which the
                 * skeleton already covers. `keepPreviousData` keeps the old rows on screen, so this
                 * is the only signal that a newer result is on its way.
                 */}
                {isListFetching && !isListPending && !isFetchingNextPage && (
                  <span role="status" className="font-mono text-xs text-text-muted">
                    searching…
                  </span>
                )}
              </div>

              <div className="max-h-72 overflow-y-auto rounded border border-border bg-canvas">
                {isListPending ? (
                  <div className="space-y-3 px-4 py-4" aria-hidden="true">
                    {Array.from({ length: 4 }, (_, index) => (
                      <div key={index} className="space-y-1.5">
                        <div className="h-4 w-48 animate-pulse rounded bg-border" />
                        <div className="h-3 w-32 animate-pulse rounded bg-border" />
                      </div>
                    ))}
                  </div>
                ) : listFailure !== null ? (
                  <div role="alert" className="px-4 py-8 text-center">
                    <p className="text-sm font-medium text-text">{listFailure.title}</p>
                    <p className="mt-1 text-sm text-text-muted">{listFailure.detail}</p>
                  </div>
                ) : selectable.length === 0 ? (
                  <p className="px-4 py-8 text-center text-sm text-text-muted">
                    {debouncedSearch.trim().length > 0
                      ? "Nothing matches that search."
                      : evidence.length === 0
                        ? "There is no evidence in the system yet."
                        : "Every available item is already linked to this case."}
                  </p>
                ) : (
                  selectable.map((item) => (
                    <EvidenceOption
                      key={item.evidence_id}
                      item={item}
                      name={listName}
                      checked={selectedId === item.evidence_id}
                      disabled={isBusy}
                      onSelect={setSelectedId}
                    />
                  ))
                )}

                {/*
                 * Inside the scroll container, after the rows: the affordance belongs where the
                 * list actually ends, so scrolling to the bottom reveals it. A button rather than an
                 * intersection observer — an explicit action is predictable, keyboard-reachable, and
                 * does not fetch pages an analyst never scrolled to see.
                 */}
                {hasNextPage && !isListPending && listFailure === null && (
                  <div className="border-t border-border p-2">
                    <button
                      type="button"
                      onClick={() => {
                        void fetchNextPage();
                      }}
                      disabled={isFetchingNextPage || isBusy}
                      className={`${ACTION_BASE} w-full border border-border hover:bg-surface`}
                    >
                      {isFetchingNextPage ? "Loading…" : "Load more"}
                    </button>
                  </div>
                )}
              </div>
            </fieldset>
          </div>

          <div
            role="tabpanel"
            id={uploadPanelId}
            aria-labelledby={uploadTabId}
            hidden={tab !== "upload"}
          >
            {/* Disabled as a group while the upload runs: §17 requires the form be inert
                mid-flight, and a `fieldset` does that for every control at once. */}
            <fieldset disabled={isBusy} className="flex flex-col gap-4">
              <legend className={`${FIELD_LABEL} mb-2`}>Upload a new artifact</legend>

              <div className="flex flex-col gap-1.5">
                <label htmlFor={fileId} className={FIELD_LABEL}>
                  File
                </label>
                <input
                  ref={fileInputRef}
                  id={fileId}
                  type="file"
                  onChange={(event) => {
                    const chosen = event.target.files?.[0] ?? null;
                    setFile(chosen);
                    setFormError(null);
                    // A filename is a better default title than an empty box, and it is what the
                    // analyst would type anyway. Only ever fills a title they have not written.
                    if (chosen !== null && uploadTitle.trim().length === 0) {
                      setUploadTitle(chosen.name.slice(0, EVIDENCE_TITLE_MAX_LENGTH));
                    }
                  }}
                  className={`${FIELD_CONTROL} file:mr-3 file:rounded file:border-0 file:bg-surface file:px-2 file:py-1 file:font-mono file:text-xs file:uppercase file:tracking-wider file:text-text`}
                />
              </div>

              <div className="grid gap-4 sm:grid-cols-2">
                <div className="flex flex-col gap-1.5">
                  <label htmlFor={categoryId} className={FIELD_LABEL}>
                    Category
                  </label>
                  <select
                    id={categoryId}
                    value={category}
                    onChange={(event) => {
                      const next = event.target.value;
                      setCategory(next);
                      setFormError(null);
                      /*
                       * Auto-select only when the category leaves no choice. Where there is a
                       * genuine choice — `drone_iot` has two log formats — the field is cleared
                       * and the analyst picks, because a silently pre-filled artifact type is a
                       * mislabelled exhibit waiting to happen.
                       */
                      const options = BASELINE_EVIDENCE_SCHEMAS.filter(
                        (schema) => schema.category === next,
                      );
                      setArtifactType(
                        options.length === 1 ? (options[0]?.artifact_type ?? "") : "",
                      );
                    }}
                    className={FIELD_CONTROL}
                  >
                    <option value="">Choose…</option>
                    {categories.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="flex flex-col gap-1.5">
                  <label htmlFor={artifactTypeId} className={FIELD_LABEL}>
                    Artifact type
                  </label>
                  <select
                    id={artifactTypeId}
                    value={artifactType}
                    // Nothing to choose until a category narrows the list — the pair is what the
                    // registry validates, so offering types across categories would offer
                    // combinations the server would reject.
                    disabled={category.length === 0}
                    onChange={(event) => {
                      setArtifactType(event.target.value);
                      setFormError(null);
                    }}
                    className={FIELD_CONTROL}
                  >
                    <option value="">
                      {category.length === 0 ? "Choose a category…" : "Choose…"}
                    </option>
                    {artifactTypes.map((schema) => (
                      <option key={schema.artifact_type} value={schema.artifact_type}>
                        {schema.artifactTypeLabel}
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              <div className="flex flex-col gap-1.5">
                <label htmlFor={titleId} className={FIELD_LABEL}>
                  Title
                </label>
                <input
                  id={titleId}
                  value={uploadTitle}
                  onChange={(event) => {
                    setUploadTitle(event.target.value);
                    setFormError(null);
                  }}
                  maxLength={EVIDENCE_TITLE_MAX_LENGTH}
                  className={FIELD_CONTROL}
                />
              </div>

              <div className="flex flex-col gap-1.5">
                <label htmlFor={authorityId} className={FIELD_LABEL}>
                  Legal authority ref{" "}
                  {!authorityRequired && (
                    <span className="normal-case tracking-normal">(optional)</span>
                  )}
                </label>
                <input
                  id={authorityId}
                  value={authorityRef}
                  onChange={(event) => {
                    setAuthorityRef(event.target.value);
                    setFormError(null);
                  }}
                  aria-describedby={authorityHintId}
                  placeholder="e.g. warrant-2026-0142"
                  className={FIELD_CONTROL}
                />
                <p id={authorityHintId} className="text-sm text-text-muted">
                  {authorityRequired
                    ? `Required for this category (CEM §13). For material needing no authority, enter “${PUBLIC_SOURCE_SENTINEL}”.`
                    : "Not required for this category, but recorded with the evidence when given."}
                </p>
              </div>

              {/*
               * A live region, not just a button label: the button is disabled mid-flight, and a
               * disabled control's changing text is not reliably announced. This is what tells a
               * screen-reader user that hashing has given way to uploading.
               */}
              <p
                role="status"
                aria-live="polite"
                className="min-h-5 font-mono text-xs text-text-muted"
              >
                {isUploading
                  ? phase === "hashing"
                    ? "Hashing the file locally — nothing has been sent yet."
                    : phase === "uploading"
                      ? "Uploading the file to secure storage…"
                      : phase === "finalizing"
                        ? "Recording the evidence and opening its custody ledger…"
                        : "Reserving an evidence identifier…"
                  : isLinking
                    ? "Linking the uploaded evidence to this case…"
                    : ""}
              </p>
            </fieldset>
          </div>

          {formError !== null && (
            <div
              role="alert"
              className="mt-4 rounded border border-danger/40 bg-danger/10 px-3 py-2"
            >
              <p className="text-sm text-danger">{formError}</p>
            </div>
          )}

          {apiError !== null && (
            // `alert` so the failure is announced; the server's reason is shown, never a generic
            // "something went wrong" (§12).
            <div
              role="alert"
              className="mt-4 rounded border border-danger/40 bg-danger/10 px-3 py-2"
            >
              <p className="text-sm font-medium text-danger">{apiError.title}</p>
              <p className="mt-0.5 text-sm text-text-muted">{apiError.detail}</p>
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-border px-6 py-4">
          <button
            type="button"
            onClick={requestClose}
            disabled={isBusy}
            className={`${ACTION_BASE} border border-border hover:bg-canvas`}
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={submitDisabled}
            aria-busy={isBusy}
            className={`${ACTION_BASE} bg-accent text-accent-contrast hover:opacity-90`}
          >
            {submitLabel}
          </button>
        </div>
      </form>
    </dialog>
  );
}
