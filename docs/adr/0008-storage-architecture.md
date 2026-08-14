# 8. Evidence Storage: Streaming Upload, Quarantine, WORM, Server-Side Hashing

## Status

Proposed. Depends on ADR-0009 (Key Management, for object encryption).

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
