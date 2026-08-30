# 8. Evidence Storage: Streaming Upload, Quarantine, WORM, Server-Side Hashing

## Status

**Accepted.** Depends on ADR-0009 (Key Management, for object encryption).

Decision items 1–3 are implemented: the `ObjectStorage` port with a MinIO adapter, the
quarantine→scan→promote flow, and — as of the ingest-time verification change — item 3's
server-computed authoritative hash. Items 4–5 (Object Lock/WORM, envelope encryption) remain
scheduled, not built; they do not alter items 1–3's design.

### Note: the §2 / §3 placement tension

Decision **§2** places server-side hashing in the background scan job ("a background job
**streams** the object to compute the server-side hash and run malware scanning"). Decision **§3**
requires the server hash to be recorded in the custody **`ingested`** event — an entry that only
exists during `POST /evidence`. Both cannot hold with one hash computation.

**Resolved in favour of §3**, because §3 is the stronger guarantee: hashing at ingest means a
mismatch is *rejected* and no evidence record is ever created for bytes the server has not
verified, whereas hashing in the scan job can only mark an already-admitted record `failed` after
the fact. `api-design.md` §13's ingest sequence and its `POST /evidence` example response (which
shows `verification_status: "verified"` on creation) already assumed this reading.

**The cost is real and is accepted deliberately.** The digest is computed inside the HTTP request,
so a multi-gigabyte forensic image is streamed and hashed synchronously — the exact artifact class
this ADR's own Context cites. It is tolerable at the file sizes the console handles today and is
**not** tolerable at disk-image scale. Revisiting it belongs to the chunked/resumable-upload
increment, where the natural answer is to hash incrementally as parts arrive, so the digest is
already known when `POST /evidence` is called and neither §2's streaming pass nor §3's rejection
guarantee has to be given up. Until then, no size-threshold bypass exists by design: two ingest
paths with different integrity guarantees would be worse than one slow one.

## Context

Object storage is unbuilt; ingestion `reserve_upload`/`get_download_url`/`verify_integrity`
and the scan job are deferred. Critically, for `payload_ref` evidence the custody chain
records the **client-declared** integrity hash and never verifies it against stored bytes
(`integrity_verification_status` stuck at `pending`). `security-architecture.md` §24–26 require
quarantine-before-scan and "never transiently reachable unscanned"; the CEM requires a
verifiable integrity hash; national-scale evidence includes multi-GB forensic images.

## Decision

1. **S3-compatible object storage** behind an `ObjectStorage` port (the `Protocol` sketched in
   backend-guide Part 9): MinIO on-prem / S3 in cloud.
2. **Quarantine → scan → promote flow.** Presigned multipart PUT into a `quarantine` bucket
   (never served); a background job **streams** the object to compute the server-side hash and
   run malware scanning (scanner behind a port; forensic categories flag-not-block per §25);
   on clean, **server-side copy** into an immutable `evidence` bucket.
3. **Server-computed integrity hash is authoritative.** Compute SHA-256 (+ agility algs) over
   stored bytes, never loading the whole object into memory; compare to any client-declared
   hash and **reject on mismatch**; set `integrity_verification_status=verified` and record the
   **server** hash in the custody `ingested` event (feeds ADR-0003).
4. **WORM immutability.** `evidence` bucket uses **Object Lock (compliance mode) + versioning +
   legal-hold**; deletion only on legal retention expiry, legal-hold-aware. This is the payload
   counterpart to ADR-0004's DB append-only and ADR-0003's anchored metadata.
5. **Encryption at rest** via envelope encryption (ADR-0009 data keys).
6. **Access** is short-TTL presigned GET that records an `accessed` custody event.

## Consequences

- Integrity becomes provable end-to-end (server-verified bytes ↔ signed custody hash ↔ WORM
  object); quarantine closes the unscanned-exposure hole.
- Requires object-store infra with Object Lock, a scanner integration (may stay behind a port
  initially), and multipart/streaming plumbing.
- Migration: implement the storage port + buckets + scan job; wires the deferred ingestion
  methods. No change to the evidence table shape beyond using the server hash.
