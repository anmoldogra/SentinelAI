/**
 * Quick case-creation form modal (frontend-architecture.md §14's form table, §17's modal rules).
 *
 * Built on the native `<dialog>` element rather than a hand-rolled overlay, because `showModal()`
 * gives the three things §17 and §36 actually require — a focus trap while open, `Esc` to close
 * (§37's shortcut table), and the rest of the page marked inert — as browser behaviour instead of
 * as effects that have to be kept correct by hand. Focus restoration to the trigger is done
 * explicitly rather than relying on the spec's implicit restore, since that is the one part
 * browsers have historically been inconsistent about.
 *
 * The form is designed *from* `POST /cases` (§14): its fields are exactly `CaseCreate`'s. Client
 * validation is UX only (§15) — the server remains the authority, and its rejection is what the
 * error region reports.
 *
 * Styling follows §17's layout spec literally: a `surface-raised` panel on the `scrim` backdrop,
 * a bordered heading row, a single-column field stack, the error region directly above the
 * actions, and a right-aligned action row (accent-filled primary, bordered ghost cancel). Labels
 * are monospaced because they name `CaseCreate`'s actual fields (§19.1's field-name rule); the
 * helper and error prose beside them stays proportional.
 */

import { useEffect, useId, useRef, useState } from "react";

import { describeApiError } from "@/shared/api/errors";

import { useCreateCase } from "../api/useCreateCase";
import { CASE_TITLE_MAX_LENGTH, type CreateCaseDTO } from "../types";

interface CreateCaseModalProps {
  open: boolean;
  onClose: () => void;
}

/** §19.1's field-name role: a label that names an API field is monospaced, compact, and quiet. */
const FIELD_LABEL = "font-mono text-xs uppercase tracking-wider text-text-muted";

/** Fields sit on `canvas` — a step *below* the raised panel — so the input well reads as recessed. */
const FIELD_CONTROL =
  "rounded border border-border bg-canvas px-3 py-2 text-sm outline-none focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-accent/40 disabled:opacity-60";

/** §17's action row: the labels are commands, so they take the console's monospaced treatment. */
const ACTION_BASE =
  "rounded px-3 py-1.5 font-mono text-xs font-medium uppercase tracking-wider disabled:opacity-60";

export function CreateCaseModal({ open, onClose }: CreateCaseModalProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  /** The element focused before the dialog opened, so it can be focused again on close (§17). */
  const triggerRef = useRef<HTMLElement | null>(null);

  const titleFieldId = useId();
  const descriptionFieldId = useId();
  const headingId = useId();
  const titleErrorId = useId();

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [titleError, setTitleError] = useState<string | null>(null);

  const { mutate, isPending, error, reset } = useCreateCase();

  // Drive the native dialog from the `open` prop, and reset the form each time it opens so a
  // previous attempt's text and errors never bleed into the next one.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) {
      return;
    }
    if (open && !dialog.open) {
      triggerRef.current = document.activeElement as HTMLElement | null;
      setTitle("");
      setDescription("");
      setTitleError(null);
      reset();
      dialog.showModal();
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open, reset]);

  function requestClose() {
    onClose();
    // Queued after the dialog actually closes, so focus is not stolen back by the closing dialog.
    window.setTimeout(() => triggerRef.current?.focus(), 0);
  }

  // `SyntheticEvent`, not the `FormEvent` alias React 19's types deprecate. Only
  // `preventDefault` is needed here, which the base type provides.
  function handleSubmit(event: React.SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();

    const trimmedTitle = title.trim();
    if (trimmedTitle.length === 0) {
      setTitleError("A title is required.");
      return;
    }
    if (trimmedTitle.length > CASE_TITLE_MAX_LENGTH) {
      setTitleError(`Keep the title to ${String(CASE_TITLE_MAX_LENGTH)} characters or fewer.`);
      return;
    }
    setTitleError(null);

    // `exactOptionalPropertyTypes` is on, so an empty description is omitted rather than sent as
    // `undefined` — and the backend's `description` is nullable, so omitting it is correct.
    const trimmedDescription = description.trim();
    const payload: CreateCaseDTO =
      trimmedDescription.length > 0
        ? { title: trimmedTitle, description: trimmedDescription }
        : { title: trimmedTitle };

    mutate(payload, { onSuccess: requestClose });
  }

  const apiError = error === null ? null : describeApiError(error);

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby={headingId}
      // `Esc` fires `cancel`; block it mid-flight so the dialog cannot vanish while the request
      // it started is still running.
      onCancel={(event) => {
        if (isPending) {
          event.preventDefault();
          return;
        }
        event.preventDefault();
        requestClose();
      }}
      // §17: `surface-raised` on the tokenised `scrim`, capped at ~32rem and always inset from the
      // viewport edge so a narrow window gets an inset panel rather than a full-bleed sheet.
      className="m-auto w-[min(32rem,calc(100vw-2rem))] rounded-lg border border-border bg-surface-raised p-0 text-text shadow-2xl backdrop:bg-scrim"
    >
      <form onSubmit={handleSubmit}>
        {/* Heading row, separated by a border rather than by whitespace (§25.1's density rule). */}
        <div className="border-b border-border px-6 py-4">
          <p className="font-mono text-xs uppercase tracking-widest text-text-muted">Case</p>
          <h2 id={headingId} className="mt-0.5 text-lg font-semibold tracking-tight">
            New case
          </h2>
        </div>

        <div className="flex flex-col gap-4 px-6 py-5">
          <div className="flex flex-col gap-1.5">
            <label htmlFor={titleFieldId} className={FIELD_LABEL}>
              Title
            </label>
            <input
              id={titleFieldId}
              value={title}
              onChange={(event) => {
                setTitle(event.target.value);
              }}
              // The browser's own required/maxlength UI is deliberately not used: it reports
              // errors in a transient bubble a screen reader user can miss, so the messages below
              // are rendered in the DOM and wired up with aria-describedby instead.
              maxLength={CASE_TITLE_MAX_LENGTH}
              autoFocus
              aria-invalid={titleError !== null}
              aria-describedby={titleError === null ? undefined : titleErrorId}
              className={FIELD_CONTROL}
              disabled={isPending}
            />
            {titleError !== null && (
              // Rose, not amber: §19.1 reserves amber for "needs attention" and rose for a
              // failure, and a rejected field is a failure.
              <p id={titleErrorId} className="text-sm text-danger">
                {titleError}
              </p>
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            <label htmlFor={descriptionFieldId} className={FIELD_LABEL}>
              Description <span className="normal-case tracking-normal">(optional)</span>
            </label>
            <textarea
              id={descriptionFieldId}
              value={description}
              onChange={(event) => {
                setDescription(event.target.value);
              }}
              rows={3}
              className={`resize-y ${FIELD_CONTROL}`}
              disabled={isPending}
            />
          </div>

          {apiError !== null && (
            // `alert` so the failure is announced; the server's reason is what is shown, never a
            // generic "something went wrong" (§12). §17 places this directly above the actions.
            <div role="alert" className="rounded border border-danger/40 bg-danger/10 px-3 py-2">
              <p className="text-sm font-medium text-danger">{apiError.title}</p>
              <p className="mt-0.5 text-sm text-text-muted">{apiError.detail}</p>
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-border px-6 py-4">
          <button
            type="button"
            onClick={requestClose}
            disabled={isPending}
            className={`${ACTION_BASE} border border-border hover:bg-canvas`}
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={isPending}
            aria-busy={isPending}
            className={`${ACTION_BASE} bg-accent text-accent-contrast hover:opacity-90`}
          >
            {isPending ? "Creating…" : "Create case"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
