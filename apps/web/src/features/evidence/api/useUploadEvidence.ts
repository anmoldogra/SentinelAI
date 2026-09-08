/**
 * Direct-to-storage evidence upload (api-design.md §2.11, §13's ingest sequence).
 *
 * Four steps, in one mutation, in this order — the order is the contract, not an implementation
 * detail:
 *
 *   A. **Reserve** — `POST /evidence/uploads` returns an `evidence_id` and a short-lived presigned
 *      PUT URL into the *quarantine* bucket. No evidence row exists yet.
 *   B. **Hash** — SHA-256 over the file's bytes, computed **before** the upload, in the browser.
 *   C. **Upload** — the bytes go straight to object storage. They never pass through the API.
 *   D. **Finalize** — `POST /evidence` writes the record, carrying `payload_ref` and the hash from
 *      step B. This is the call that creates the evidence and its genesis custody entry.
 *
 * **Why the hash is computed before the upload and not after.** The point of a client-side digest
 * is to attest to what the analyst actually held. Hashing bytes we have already handed to storage
 * would attest to nothing the server could not have computed itself. Hashing first means the
 * digest travelling in step D describes the local file, and any corruption in transit shows up
 * later as a failed server-side re-verification rather than as a self-consistent lie.
 *
 * **Nothing is written until step D succeeds.** A failure at B or C leaves an orphaned object in
 * quarantine and no database row — which is the correct failure mode: the evidence store never
 * gains a record for bytes that were not fully accounted for. Quarantine sweeping is a storage
 * concern, not this hook's.
 *
 * **One correlation ID spans all four steps** (api-design.md §2.8): reserve and finalize are two
 * halves of one analyst action, and giving them separate IDs would split that action across two
 * traces in exactly the place an investigator would need it whole.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import { apiRequest, newCorrelationId } from "@/shared/api/client";

import { EVIDENCE_PATH } from "./getEvidenceItem";
import { evidenceQueryKeys } from "./useEvidenceItem";
import { EVIDENCE_SCHEMA_VERSION, type Evidence } from "../types";

/** `UploadReservationRead` — what `POST /evidence/uploads` returns in `data`. */
interface UploadReservation {
  evidence_id: string;
  upload_url: string;
}

/**
 * `EvidenceCreate`, restricted to the fields this flow sends.
 *
 * The backend schema has more optional fields (`description`, `inline_payload`,
 * `reliability_rating`, `retention_policy_ref`); they are omitted rather than sent as nulls, so
 * the server's own defaults apply.
 */
interface EvidenceCreateDTO {
  schema_version: string;
  category: string;
  artifact_type: string;
  title: string;
  source: { system: string; collector_id: string };
  collected_at: string;
  attributes: Record<string, never>;
  confidence: number;
  payload_ref: string;
  integrity_algorithm: "SHA-256";
  integrity_hash: string;
  legal_authority_ref?: string;
}

/** What the form collects. */
export interface UploadEvidenceInput {
  file: File;
  category: string;
  artifact_type: string;
  title: string;
  /** Omitted when the analyst left it blank and the category does not require one. */
  legal_authority_ref?: string;
}

/**
 * Which of the four steps is running.
 *
 * Exposed because they are not interchangeable to the person waiting: hashing is local, CPU-bound,
 * and scales with file size, while uploading is network-bound and can stall. "Working…" for both
 * would leave an analyst unable to tell a slow disk from a dead link.
 *
 * On failure the phase is *retained*, so the error message can say which step failed.
 */
export type UploadPhase = "idle" | "reserving" | "hashing" | "uploading" | "finalizing";

/**
 * `crypto.subtle`, or `undefined` where the platform does not provide it.
 *
 * Web Crypto exists only in a secure context, and frontend-architecture.md §2's air-gapped profile
 * is served over plain HTTP on a LAN address. `utils/verifyChain.ts` probes for the same reason.
 * Narrowing `globalThis` states the possibility honestly rather than suppressing the lint that
 * would call the check dead code.
 */
function subtleCrypto(): SubtleCrypto | undefined {
  return (globalThis as { crypto?: { subtle?: SubtleCrypto } }).crypto?.subtle;
}

/** Lowercase hex, matching Python's `hexdigest()` — the form the backend stores and compares. */
function toHex(buffer: ArrayBuffer): string {
  return Array.from(new Uint8Array(buffer))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * SHA-256 of the whole file.
 *
 * **Reads the entire file into memory.** Web Crypto has no streaming digest API, so there is no
 * incremental alternative without a third-party hasher — a dependency decision, not something to
 * slip in here. This is fine for logs and extractions and is the honest limit of this increment;
 * multi-gigabyte disk images need the chunked upload path, which is explicitly out of scope.
 */
async function sha256File(file: File): Promise<string> {
  const subtle = subtleCrypto();
  if (subtle === undefined) {
    throw new Error(
      "Web Crypto is unavailable in this context, so the file's integrity hash cannot be " +
        "computed. Evidence is never uploaded without one. A secure context (HTTPS or localhost) " +
        "is required.",
    );
  }
  return toHex(await subtle.digest("SHA-256", await file.arrayBuffer()));
}

/**
 * Derive the canonical `s3://bucket/key` reference from the presigned URL the server issued.
 *
 * The reference is read back off the URL rather than rebuilt from the key convention, so
 * `payload_ref` always names the object the bytes were actually PUT to — if the server changes
 * where it puts uploads, this follows without a coordinated frontend change.
 *
 * **This depends on path-style addressing** (`http://host/bucket/key…`), which the backend's S3
 * client pins explicitly (`addressing_style: "path"` in `platform/storage/minio.py`, required for
 * MinIO). A virtual-host-style URL would put the bucket in the hostname and this would read the
 * first key segment as the bucket instead.
 */
function payloadRefFromUploadUrl(uploadUrl: string): string {
  const { pathname } = new URL(uploadUrl);
  // Each segment is percent-encoded by the signer; decode individually so the `/` separators
  // inside the key survive.
  const segments = pathname.split("/").filter((segment) => segment.length > 0);
  const [bucket, ...keyParts] = segments.map((segment) => decodeURIComponent(segment));
  if (bucket === undefined || keyParts.length === 0) {
    throw new Error("The upload URL from the server was not in the expected form.");
  }
  return `s3://${bucket}/${keyParts.join("/")}`;
}

/**
 * PUT the bytes to object storage.
 *
 * **`file.slice()`, not `file`, is deliberate and load-bearing.** `fetch` derives a
 * `Content-Type` header from a `Blob`'s `type`, and a `File` picked from disk carries the browser's
 * guess (`text/csv`, `application/octet-stream`, …). The server presigned this URL without a
 * `Content-Type` in the signature, so a header the signature does not cover makes the request
 * fail with `SignatureDoesNotMatch` (403). `slice()` with no arguments returns the same bytes as a
 * `Blob` whose `type` is the empty string, so `fetch` sends no `Content-Type` at all and the
 * request matches what was signed.
 *
 * This is a cross-origin request to storage, not to the API: no `Authorization` header (the
 * signature *is* the credential) and no cookies — `fetch`'s default `credentials: "same-origin"`
 * already withholds them, and sending session cookies to the storage broker would be wrong.
 */
async function putBytes(uploadUrl: string, file: File): Promise<void> {
  const response = await fetch(uploadUrl, { method: "PUT", body: file.slice() });
  if (response.ok) {
    return;
  }
  // Storage answers with an XML error document, not the API's JSON envelope, so `describeApiError`
  // has nothing to work with. The `<Code>` is the actionable part (`SignatureDoesNotMatch`,
  // `ExpiredToken`) and is surfaced rather than swallowed behind a generic message.
  const body = await response.text().catch(() => "");
  const code = /<Code>([^<]+)<\/Code>/.exec(body)?.[1];
  throw new Error(
    code === undefined
      ? `The file could not be uploaded to storage (HTTP ${String(response.status)}).`
      : `Storage rejected the upload: ${code} (HTTP ${String(response.status)}).`,
  );
}

export function useUploadEvidence() {
  const queryClient = useQueryClient();
  const [phase, setPhase] = useState<UploadPhase>("idle");

  const mutation = useMutation<Evidence, Error, UploadEvidenceInput>({
    mutationFn: async (input) => {
      const correlationId = newCorrelationId();
      /*
       * Captured now, before hashing and uploading, rather than at finalize. It is the moment the
       * analyst submitted the artifact, and taking it early leaves it slightly in the *past* by
       * the time the server sees it — on the safe side of the backend's clock-skew tolerance,
       * which rejects a `collected_at` in the future.
       */
      const collectedAt = new Date().toISOString();

      // --- A. Reserve -------------------------------------------------------
      setPhase("reserving");
      const reservation = await apiRequest<UploadReservation>(`${EVIDENCE_PATH}/uploads`, {
        method: "POST",
        body: { category: input.category, artifact_type: input.artifact_type },
        correlationId,
      });

      // --- B. Hash ----------------------------------------------------------
      setPhase("hashing");
      const integrityHash = await sha256File(input.file);

      // --- C. Upload --------------------------------------------------------
      setPhase("uploading");
      await putBytes(reservation.upload_url, input.file);

      // --- D. Finalize ------------------------------------------------------
      setPhase("finalizing");
      const payload: EvidenceCreateDTO = {
        schema_version: EVIDENCE_SCHEMA_VERSION,
        category: input.category,
        artifact_type: input.artifact_type,
        title: input.title,
        // CEM §13 rejects evidence without provenance. This is a console upload, so the system is
        // the console and the collector is the signed-in analyst, whom the server already
        // identifies from the bearer token and records as `collector_user_id`.
        source: { system: "sentinelai-web", collector_id: "user" },
        collected_at: collectedAt,
        // The registry records which triples are valid; it does not yet carry the per-triple
        // attribute *field* definitions, so there is nothing for the console to collect here. An
        // empty object is the honest value — not a placeholder standing in for fields it skipped.
        attributes: {},
        /*
         * `confidence` is required by `EvidenceCreate` and is not a question worth putting to an
         * analyst about a file they are uploading themselves: they hold the artifact, so the
         * value is 1. It becomes a real form field when evidence arrives from a connector whose
         * output warrants a judgement.
         */
        confidence: 1,
        payload_ref: payloadRefFromUploadUrl(reservation.upload_url),
        integrity_algorithm: "SHA-256",
        integrity_hash: integrityHash,
        // `exactOptionalPropertyTypes`: omitted entirely when absent, never sent as `undefined`.
        ...(input.legal_authority_ref === undefined
          ? {}
          : { legal_authority_ref: input.legal_authority_ref }),
      };

      const evidence = await apiRequest<Evidence>(EVIDENCE_PATH, {
        method: "POST",
        body: payload,
        correlationId,
      });
      setPhase("idle");
      return evidence;
    },
    /*
     * The new item belongs in every evidence list and its detail cache. Returned so React Query
     * holds the mutation pending until the refetch settles — the caller chains a link mutation on
     * success, and the picker behind the modal should already be current when it does.
     */
    onSuccess: () => queryClient.invalidateQueries({ queryKey: evidenceQueryKeys.all }),
  });

  const { reset } = mutation;
  const resetUpload = useCallback(() => {
    setPhase("idle");
    reset();
  }, [reset]);

  return { ...mutation, phase, reset: resetUpload };
}
