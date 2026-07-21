# SentinelAI — Canonical Evidence Model (CEM)

**Status:** Draft — engineering & data-model reference
**Last updated:** 2026-07-19
**Related documents:** [PRD](prd.md) · [Architecture](architecture.md) · [System Design](system-design.md) · [`packages/evidence-schema`](../packages/evidence-schema/README.md)

This document is the design specification for the Canonical Evidence Model — the single data shape every evidence source ingested into SentinelAI is normalized into. `packages/evidence-schema` is its implementation home; this document is the design that implementation must conform to. No implementation code below — object shapes, rules, and examples are described conceptually, not as schema-as-code.

**Scope note:** evidence *categories* (Section 5) are a data-model concept, independent of which `apps/server` module or future service ingests them. Mobile forensics and cloud evidence may be ingested by the existing `forensics` module as sub-categories; blockchain intelligence and drone/IoT may warrant new modules as `docs/roadmap.md` Phase 2 connector prioritization is revisited. That module-boundary decision belongs in `docs/architecture.md`, not here — this document guarantees the model supports all eight categories regardless of how ingestion is eventually organized.

---

## 1. Purpose and Design Principles

**Purpose:** every evidence source — a forensic disk image, a scraped OSINT record, a threat feed indicator, a social media post, an on-chain transaction, a drone telemetry log, a cloud audit event — must become the *same shape* the moment it enters SentinelAI, so that chain-of-custody, case linking, AI correlation, and reporting all operate on one consistent model instead of nine different ones. The CEM is that shape. It is the single source of truth PRD FR-1.1 requires and the contract `investigation` (system-design.md §4) depends on to reason across domains it doesn't otherwise understand.

**Design principles:**

1. **Source-agnostic core, source-specific extension.** A stable envelope (identity, provenance, timestamps, integrity, custody, classification) is common to every evidence object; category- and artifact-type-specific detail lives in a typed `attributes` extension (Section 2). Adding a new source never changes the core.
2. **Immutability by construction.** An evidence object, once committed, is never edited in place (PRD FR-1.4). Corrections produce a new, versioned object linked to the original (Section 12).
3. **Provenance is mandatory, not optional.** No object is admitted without source system, collector identity, collection method, and collection timestamp (Section 3).
4. **Integrity is verifiable, not asserted.** Every payload-bearing object carries a cryptographic hash computed at ingestion and re-checkable at any later access (Section 4).
5. **Evidence, Entity, and Relationship are distinct layers.** Evidence objects are the raw/normalized facts; Entities and Relationships (Sections 7–8) are *derived* from evidence and form a knowledge graph layer (Section 11), kept separate so the same real-world entity can be corroborated by many evidence items without duplicating entity data.
6. **Every graph assertion is grounded in evidence.** No Entity or Relationship may exist without at least one supporting evidence reference — this is what makes AI-generated findings explainable (PRD FR-7.2) rather than a black box, enforced at the data-model level (Section 13), not just in application logic.
7. **Confidence and reliability are explicit fields**, not implied — OSINT, social media, and blockchain-attribution evidence carry inherently variable reliability, and the model must say so rather than presenting everything as equally authoritative.
8. **Chain of custody is a first-class, append-only ledger**, not a metadata afterthought (Section 4).
9. **Legal-authority-aware.** Every evidence object can (and for most categories, must) reference the legal authority under which it was collected — directly implementing the PRD's civil-liberties/lawful-use governance requirement.
10. **Extensible without breaking consumers.** New categories, artifact types, and attributes are additive; the core envelope changes rarely and only via a versioned, coordinated process (Section 12).

## 2. Core Evidence Object

The envelope common to every evidence object, regardless of category:

| Field | Type | Required | Description |
|---|---|---|---|
| `evidence_id` | UUID | yes | Globally unique, immutable identifier assigned at ingestion |
| `schema_version` | semver string | yes | CEM schema version this object conforms to (Section 12) |
| `category` | enum (Section 5) | yes | Top-level evidence category |
| `artifact_type` | enum, scoped to `category` (Section 6) | yes | Specific artifact type within the category |
| `title` | string | yes | Short human-readable label |
| `description` | string | no | Free-text narrative/context |
| `source` | object (Section 3) | yes | Provenance: system, connector, collector, method |
| `collected_at` | ISO 8601 timestamp | yes | When the underlying fact was originally observed/collected |
| `ingested_at` | ISO 8601 timestamp | yes | When this CEM object was created (may lag `collected_at`) |
| `integrity` | object (Section 4) | if payload-bearing | Hash, algorithm, verification status of raw payload |
| `payload_ref` | URI | conditional | Pointer to raw binary/large content in object storage |
| `inline_payload` | structured data | conditional | Small normalized content stored directly |
| `attributes` | structured data, typed by `category`+`artifact_type` | yes | Category/artifact-specific detail (Section 6) |
| `technical` | object | if payload-bearing | `mime_type`, `size_bytes`, `encoding`, `original_filename` |
| `confidence` | float, 0.0–1.0 | yes | Confidence in the evidence's accuracy |
| `reliability_rating` | enum | no | Source reliability — recommended vocabulary: Admiralty/NATO source-reliability code (A–F) × information-credibility code (1–6), standard OSINT/intel tradecraft |
| `classification` | object | yes | `sensitivity`, `legal_authority_ref`, `access_restriction_tags[]` |
| `geo` | object | no | `lat`, `long`, `precision_meters`, `geolocation_source` |
| `language` | BCP 47 code[] | no | Detected content language(s) |
| `tags` | string[] | no | Free-form and controlled-vocabulary labels |
| `status` | enum: `pending_validation`, `validated`, `quarantined`, `superseded`, `tombstoned` | yes | Lifecycle status (Section 12) |
| `supersedes_evidence_id` | UUID | no | Prior version this object replaces |
| `retention` | object | yes | `policy_ref`, `legal_hold` (boolean) |

**Not part of the core object:** case linkage. Which case(s) an evidence item is attached to is owned and recorded by the `case-management` domain (system-design.md §4), not embedded here — this document defines the evidence object itself, referenced by, not owning, that relationship.

`source` sub-fields: `system` (connector/tool name), `connector_version`, `collector_id` (user or system identity), `collection_method` (e.g. `forensic_extraction`, `api_pull`, `manual_entry`, `web_capture`, `sensor_telemetry`), `collection_location` (optional).

## 3. Metadata Model

Core fields fall into four metadata classes; every new field proposed for the core envelope should be justified against one of them, not added ad hoc:

| Class | Answers | Core fields |
|---|---|---|
| **Descriptive** | What is this? | `title`, `description`, `tags`, `language` |
| **Technical** | What format/shape is the payload? | `technical.mime_type`, `technical.size_bytes`, `technical.encoding`, `integrity` |
| **Administrative** | Who may see it, how long is it kept? | `classification`, `retention` |
| **Provenance** | Where did it come from, how was it obtained? | `source`, `collected_at`, `ingested_at` |

A fifth, implicit class — **custody** (who has touched it since) — is deliberately *not* a field on the core object at all; it's an append-only related ledger (Section 4), because unlike the classes above it keeps growing after the evidence object is created, and the object itself must stay immutable.

## 4. Chain of Custody Model

Custody is an **append-only, hash-chained ledger** of events per `evidence_id` — not a field on the evidence object, and not editable once written (directly implementing PRD FR-2.3 and SR-4: tamper-evident even to an administrator with direct database access).

| Field | Type | Description |
|---|---|---|
| `custody_event_id` | UUID | Unique identifier for this ledger entry |
| `evidence_id` | UUID | The evidence this event concerns |
| `sequence_number` | integer | Monotonically increasing per `evidence_id` |
| `event_type` | enum | `collected`, `ingested`, `accessed`, `exported`, `transferred`, `analyzed`, `integrity_reverified`, `linked_to_case`, `unlinked_from_case`, `legal_hold_applied`, `legal_hold_released`, `disposed` |
| `occurred_at` | ISO 8601 timestamp | When the event happened |
| `actor_id` | UUID | User or system identity that performed the action |
| `actor_role` | string | The actor's role *at the time of the action* (kept even if the role later changes) |
| `authority_ref` | string, optional | Legal authority relevant to this specific action, if distinct from the evidence's own `classification.legal_authority_ref` |
| `integrity_hash_at_event` | hash | Payload hash recomputed at this event — proves what was accessed/exported/analyzed matched the original |
| `prev_event_hash` | hash, nullable | The `entry_hash` of the previous event for this `evidence_id` (null only for the genesis `collected`/`ingested` event) |
| `entry_hash` | hash | Computed over this entry's own fields plus `prev_event_hash` |
| `notes` | string, optional | Free text (e.g. reason for access) |

**Why hash-chained:** each entry's `entry_hash` depends on the previous entry's hash, so altering or deleting any historical entry breaks every subsequent hash in the chain — detectable by recomputing forward from the genesis event. This is the same tamper-evidence technique used in blockchain ledgers, which is fitting given blockchain intelligence is itself one of the supported evidence categories.

## 5. Evidence Categories

| Category | Definition | Notes |
|---|---|---|
| `digital_forensics` | Artifacts recovered from computers, storage media, and network captures | Traditional forensic imaging/analysis output |
| `mobile_forensics` | Artifacts recovered from mobile devices | Distinct from `digital_forensics` — different extraction tools, different artifact shapes (SMS, app data, device identifiers) |
| `osint` | Open-source intelligence findings | Public records, breach data, domain intelligence, web content |
| `threat_intelligence` | Indicators and context from threat feeds | IOCs, threat actor/campaign profiles, TTPs |
| `social_media_intelligence` | Content and network data from social platforms | Posts, profiles, connections, media |
| `blockchain_intelligence` | On-chain activity and attribution | Wallets, transactions, smart contracts, exchange attribution |
| `drone_iot` | Telemetry and sensor data from drones and IoT devices | Flight logs, sensor streams, captured media, geofence events |
| `cloud_evidence` | Cloud provider and SaaS activity/configuration data | Access logs, API call logs, storage objects, configuration snapshots |
| `manual` | Analyst-entered evidence not sourced from an automated connector | Notes, uploaded external documents, logged physical evidence |

## 6. Artifact Types

Representative `artifact_type` values per category (additive — new types are a minor schema version bump, Section 12):

| Category | Artifact types |
|---|---|
| `digital_forensics` | `disk_image`, `memory_dump`, `file_artifact`, `registry_hive`, `event_log`, `network_capture`, `email_archive` |
| `mobile_forensics` | `full_extraction`, `file_system_extraction`, `call_log`, `sms_mms_message`, `app_data_artifact`, `device_metadata`, `location_history` |
| `osint` | `public_record`, `domain_whois`, `breach_data_record`, `web_page_snapshot`, `image_metadata`, `people_search_result` |
| `threat_intelligence` | `ioc`, `threat_actor_profile`, `malware_sample_metadata`, `ttp_report`, `vulnerability_reference` |
| `social_media_intelligence` | `post`, `profile_snapshot`, `comment`, `direct_message`, `network_connection_snapshot`, `media_upload` |
| `blockchain_intelligence` | `wallet_address`, `transaction`, `smart_contract`, `exchange_attribution`, `token_transfer`, `cluster_attribution` |
| `drone_iot` | `flight_log`, `telemetry_stream`, `sensor_reading`, `captured_media`, `device_registration`, `geofence_event` |
| `cloud_evidence` | `storage_object`, `access_log`, `api_call_log`, `container_image_snapshot`, `saas_audit_log`, `configuration_snapshot` |
| `manual` | `analyst_note`, `external_document`, `physical_evidence_record` |

Each `(category, artifact_type)` pair has a registered `attributes` schema (Section 12's schema registry) — e.g. an `sms_mms_message` requires `sender`, `recipient`, `body`, `direction`; a `wallet_address` requires `chain`, `address`, `attribution_confidence`.

## 7. Entity Types

Entities are the "things" the investigation reasons about, extracted or referenced from evidence — the nodes of the knowledge graph (Section 11):

| Entity type | Description | Example identifying attributes |
|---|---|---|
| `person` | An individual, real or aliased | Name(s), alias(es), date of birth |
| `device` | A phone, computer, drone, or IoT device | Serial/IMEI/IMSI, MAC address, device model |
| `account` | A social media, email, or cloud account | Platform, handle/username, account ID |
| `organization` | A company, group, or entity | Name, registration ID, jurisdiction |
| `location` | A place or geofence | Coordinates, address, precision |
| `digital_asset` | A file, domain, IP, URL, or indicator | Hash, domain name, IP, URL |
| `financial_instrument` | A wallet or exchange account | Chain, address, exchange, account ID |
| `event` | An occurrence in time (meeting, transaction, flight) | Timestamp, event type, participants |

**Note:** communications (messages, calls) are modeled as *evidence* (Section 6) referencing sender/recipient entities via relationships, not as a first-class entity type — this keeps the entity taxonomy to "durable things" and avoids duplicating what evidence objects already represent.

## 8. Relationship Types

Relationships connect entities and are the edges of the knowledge graph. Every relationship carries the provenance that grounds it in evidence (Section 1, principle 6):

| Relationship type | From → To | Description |
|---|---|---|
| `owns` / `controls` | Person → Device / Account / Financial Instrument | Ownership or operational control |
| `communicated_with` | Person/Account ↔ Person/Account | Message, call, or contact evidence |
| `associated_with` | Any ↔ Any | Generic, weighted association where a more specific type doesn't apply |
| `located_at` | Entity → Location | Time-bounded presence |
| `transacted_with` | Financial Instrument ↔ Financial Instrument | On-chain or financial transaction |
| `member_of` | Person → Organization | Affiliation |
| `accessed` | Person/Account → Digital Asset / Cloud Resource | Access event (from cloud/forensic logs) |
| `present_at` | Entity → Event | Participation |
| `derived_from` | Entity/Relationship → Evidence | The mandatory provenance edge — see Section 11 |

| Field | Type | Description |
|---|---|---|
| `relationship_id` | UUID | Unique identifier |
| `type` | enum (above) | Relationship type |
| `from_entity_id`, `to_entity_id` | UUID | Endpoints |
| `directional` | boolean | Whether `type` implies direction |
| `confidence` | float, 0.0–1.0 | Confidence in the relationship's accuracy |
| `valid_from`, `valid_to` | ISO 8601 timestamp, nullable | Temporal validity (many relationships are not permanent) |
| `supporting_evidence_ids` | UUID[], min length 1 | Every relationship must be grounded (Section 13) |
| `status` | enum: `proposed`, `confirmed`, `rejected` | Analyst disposition (Section 10) |
| `created_by` | actor reference | Analyst, or AI + model/version if AI-generated |

## 9. Source Connectors and Mapping Strategy

**Representative connectors per category** (illustrative, not exhaustive — connectors are added over time per `docs/roadmap.md`):

| Category | Representative connector sources |
|---|---|
| `digital_forensics` | EnCase, FTK, Autopsy/Sleuth Kit, X-Ways exports |
| `mobile_forensics` | Cellebrite UFED, Magnet AXIOM, GrayKey, Oxygen Forensic Detective exports |
| `osint` | WHOIS registries, Shodan-style scanning services, breach-data aggregators, web capture tools |
| `threat_intelligence` | STIX/TAXII feeds, MISP, commercial threat-intel APIs |
| `social_media_intelligence` | Platform-provided APIs where available; lawful-access data requests otherwise; web archival tools |
| `blockchain_intelligence` | Node/RPC or indexer APIs, chain-analysis attribution feeds |
| `drone_iot` | Flight-controller telemetry exports, MQTT/IoT gateway streams, sensor exports |
| `cloud_evidence` | Cloud provider audit/activity log exports, SaaS admin audit logs, registry/image metadata |

**Mapping pipeline** (generic across every connector, per PRD FR-3.1's "pluggable connector" requirement):

```
Extract → Map → Enrich → Validate → Commit
```

1. **Extract** — raw connector output, unmodified, retained as `payload_ref` for audit purposes.
2. **Map** — a **connector mapping profile** (a versioned, declarative field-mapping definition, not per-connector business logic embedded in the ingestion path) translates raw fields into `attributes` for the target `(category, artifact_type)`.
3. **Enrich** — derived fields computed: integrity hash, geo resolution, language detection.
4. **Validate** — Section 13's rules; failure routes to rejection with a clear error (FR-1.3), never silent partial ingestion.
5. **Commit** — the immutable CEM object is written, and its genesis custody entry (`collected` or `ingested`) is appended.

Treating mapping profiles as **versioned configuration, not code forks per connector** is the design choice that keeps ingestion extensible — a new source is a new profile, not a new code path, and profiles version independently of the CEM schema itself (Section 12).

## 10. AI Extraction Targets

What the AI investigation layer is expected to extract *from* evidence *into* the entity/relationship graph — every output below is written as `status: proposed` with a `confidence` score and `derived_from` evidence link, never as a confirmed fact (PRD FR-7.3):

- **Named entities** (people, organizations, locations) from unstructured text — OSINT reports, social posts, forensic document/chat content.
- **Identifiers** (emails, phone numbers, wallet addresses, device identifiers, IPs, domains) via pattern/NER extraction from any payload type.
- **Temporal expressions**, normalized into event timestamps for timeline reconstruction.
- **Geolocation** — from EXIF/geotags, IP geolocation, or mentioned place names.
- **Relationship inference** — communication patterns, transaction graphs, co-occurrence within the same evidence item.
- **Sentiment / threat-intent signals** — from social or forensic chat content; always low-confidence-by-default and human-reviewed, never auto-escalated.
- **Cross-source correlation candidates** — the same identifier appearing in two different categories' evidence (e.g. a wallet address in `blockchain_intelligence` matching one mentioned in an `osint` breach record).
- **Entity-resolution candidates** — the same real-world entity referenced differently across sources (aliases, handle variants).

## 11. Knowledge Graph Mapping

The Entity/Relationship layer (Sections 7–8) is designed to be projected into a **property graph**:

- **Node type:** `Entity`, typed by `entity_type` (Section 7).
- **Edge type:** typed relationships between `Entity` nodes (Section 8), carrying `confidence`, `valid_from`/`valid_to`, `supporting_evidence_ids`, and `status` as edge properties — sufficient for FR-7.2 traceability without needing to reify relationships as nodes.
- **`MENTIONS` edges:** from a lightweight `Evidence` reference node (id + category, not the full payload — the full object stays in the relational store) to `Entity` nodes it references, independent of whether a `Relationship` has been established yet — supports "show all evidence mentioning this person" queries even before correlation runs.

**The graph is a derived, queryable projection — not the system of record.** The relational store (Postgres, per system-design.md §9) remains the durable, transactional, auditable source of truth for evidence, entities, and relationships, because chain-of-custody and evidentiary integrity require ACID guarantees the CEM depends on throughout. At Phase 1 scale, the "graph" can simply be relational tables queried via joins/recursive CTEs; a dedicated graph database (e.g. for traversal-heavy queries at scale) is a candidate future addition, not a Phase 1 requirement — **the graph database technology choice is intentionally left open here**, consistent with the other open infrastructure questions in `docs/architecture.md`, since it's a consequential addition to the stack that shouldn't be decided inside a data-model document.

## 12. Versioning Strategy

- **`schema_version` (semver)** on every object. **MAJOR** = a breaking change to the core envelope (rare, coordinated, requires an ADR per `CLAUDE.md`'s "no structural choice without recording why"). **MINOR** = additive — a new category, artifact type, or optional attribute (common, always backward-compatible; old objects remain valid without migration).
- **Evidence objects are immutable; corrections supersede.** "Editing" evidence produces a new object with `supersedes_evidence_id` pointing at the prior one; the prior object is never deleted, only marked `status: superseded` — full history survives for audit and legal hold (implements FR-1.4).
- **Entities and Relationships version by append, not overwrite.** Confidence updates or status transitions (`proposed → confirmed`/`rejected`) are recorded as new entries in an append-only revision log per entity/relationship, mirroring the custody-ledger pattern (Section 4) — so "why did the AI's confidence change" is itself auditable.
- **Schema registry.** A versioned registry defines which `attributes` fields are valid/required for each `(category, artifact_type, schema_version)` combination. Connector mapping profiles (Section 9) declare which registry version they target, so data ingested under an older schema version remains valid and readable after newer categories/types are added.
- **Migration policy.** MINOR bumps require no migration. MAJOR bumps require an explicit, logged migration path recorded as an ADR before adoption.

## 13. Validation Rules

Every rule below is enforced before an object transitions from `pending_validation` to `validated`; failure returns a clear rejection per PRD FR-1.3 rather than a silent partial write.

| Rule | Applies to | On failure |
|---|---|---|
| `evidence_id` present, UUID v4, globally unique | All evidence | Reject |
| `schema_version` present and registered | All evidence | Reject |
| `category` is one of Section 5's registered values | All evidence | Reject |
| `artifact_type` valid for the declared `category` (Section 6) | All evidence | Reject |
| `source.system` and `source.collector_id` present | All evidence | Reject — no evidence without provenance |
| `collected_at` is valid ISO 8601 and not after `ingested_at` beyond a documented clock-skew tolerance | All evidence | Reject |
| `integrity.hash` + `integrity.algorithm` present, algorithm ∈ {SHA-256, SHA-3-256, SHA-512} | Payload-bearing evidence | Reject (weaker legacy hashes may be retained as a *secondary* field for forensic-tool compatibility, never as the primary integrity hash) |
| `attributes` conforms to the registered schema for `(category, artifact_type, schema_version)` | All evidence | Reject |
| `classification.legal_authority_ref` present (or explicitly `public_source_no_authority_required`) | `digital_forensics`, `mobile_forensics`, `social_media_intelligence`, `cloud_evidence` at minimum | Reject |
| Core fields are write-once | All evidence | Any attempted mutation of a `validated`/`superseded` object's core fields is rejected — must go through supersession (Section 12) |
| `supporting_evidence_ids` has ≥1 entry, each referencing an existing, non-`tombstoned` evidence object | Relationships | Reject |
| Has ≥1 `MENTIONS` edge from a valid evidence object, **or** `created_by` is an analyst | Entities | Reject (analyst-pre-registered entities, e.g. a known subject of investigation, are an accepted exception) |
| First custody event for an `evidence_id` is `collected` or `ingested`; events are strictly time-ordered and hash-chained with no gaps | Custody ledger | Any detected gap sets the evidence `status` to `quarantined` pending investigation |

## 14. Example Evidence Objects

Illustrative examples — abbreviated to the fields most relevant to each category; every real object also carries the full core envelope from Section 2.

**Mobile forensics — SMS message:**
```json
{
  "evidence_id": "1f3b2e2a-0a3a-4d3e-9c3a-7a6b1f2c8d10",
  "schema_version": "1.2.0",
  "category": "mobile_forensics",
  "artifact_type": "sms_mms_message",
  "title": "SMS extracted from device DEV-4471",
  "source": {
    "system": "Cellebrite UFED",
    "connector_version": "7.x",
    "collector_id": "examiner:priya.n",
    "collection_method": "forensic_extraction"
  },
  "collected_at": "2026-06-02T14:03:00Z",
  "ingested_at": "2026-06-02T18:41:12Z",
  "integrity": { "algorithm": "SHA-256", "hash": "9f3a...c21", "verification_status": "verified" },
  "attributes": {
    "sender": "+1-555-0134",
    "recipient": "+1-555-0199",
    "direction": "outgoing",
    "body": "meet at the usual spot 9pm",
    "device_id": "DEV-4471"
  },
  "confidence": 1.0,
  "classification": { "sensitivity": "restricted", "legal_authority_ref": "WARRANT-2026-0417" },
  "status": "validated"
}
```

**OSINT — domain WHOIS record:**
```json
{
  "evidence_id": "2a9c7d41-...",
  "category": "osint",
  "artifact_type": "domain_whois",
  "title": "WHOIS for suspect-domain.example",
  "source": { "system": "whois-connector", "collection_method": "api_pull" },
  "attributes": {
    "domain": "suspect-domain.example",
    "registrar": "Example Registrar Inc.",
    "registrant_org": "Redacted",
    "created_date": "2024-11-03",
    "name_servers": ["ns1.example.com", "ns2.example.com"]
  },
  "confidence": 0.9,
  "reliability_rating": "B2",
  "classification": { "sensitivity": "public", "legal_authority_ref": "public_source_no_authority_required" }
}
```

**Threat intelligence — malicious IP indicator:**
```json
{
  "evidence_id": "3c8e1b09-...",
  "category": "threat_intelligence",
  "artifact_type": "ioc",
  "title": "Malicious IP linked to campaign APT-EXAMPLE",
  "source": { "system": "MISP", "collection_method": "api_pull" },
  "attributes": {
    "indicator_type": "ipv4",
    "value": "203.0.113.77",
    "threat_actor_ref": "APT-EXAMPLE",
    "first_seen": "2026-05-11T00:00:00Z",
    "mitre_attack_ids": ["T1071.001"]
  },
  "confidence": 0.85,
  "classification": { "sensitivity": "restricted", "legal_authority_ref": "public_source_no_authority_required" }
}
```

**Social media intelligence — public post:**
```json
{
  "evidence_id": "4d1f9a22-...",
  "category": "social_media_intelligence",
  "artifact_type": "post",
  "title": "Public post referencing suspect location",
  "source": { "system": "platform-connector-x", "collection_method": "api_pull" },
  "attributes": {
    "platform": "example-platform",
    "account_handle": "@example_user",
    "post_id": "1234567890",
    "text": "at the warehouse on 5th, see you all there",
    "posted_at": "2026-06-10T21:15:00Z"
  },
  "confidence": 0.7,
  "reliability_rating": "C3",
  "geo": { "lat": 40.7128, "long": -74.0060, "precision_meters": 500, "geolocation_source": "post_geotag" }
}
```

**Blockchain intelligence — transaction:**
```json
{
  "evidence_id": "5e2a8c37-...",
  "category": "blockchain_intelligence",
  "artifact_type": "transaction",
  "title": "On-chain transfer from wallet A to wallet B",
  "source": { "system": "chain-indexer-connector", "collection_method": "api_pull" },
  "attributes": {
    "chain": "example-chain",
    "tx_hash": "0xabc123...",
    "from_address": "0x1111...",
    "to_address": "0x2222...",
    "amount": "2.35",
    "asset": "EXC",
    "block_height": 19234871
  },
  "confidence": 1.0,
  "integrity": { "algorithm": "SHA-256", "hash": "on-chain-verifiable", "verification_status": "verified" }
}
```

**Drone/IoT — flight telemetry entry:**
```json
{
  "evidence_id": "6f3b7d48-...",
  "category": "drone_iot",
  "artifact_type": "telemetry_stream",
  "title": "Flight telemetry, mission MSN-0092",
  "source": { "system": "flight-controller-export", "collection_method": "sensor_telemetry" },
  "attributes": {
    "device_id": "DRONE-08",
    "timestamp": "2026-06-15T13:22:04Z",
    "lat": 34.0522, "long": -118.2437, "altitude_m": 62.4,
    "heading_deg": 187,
    "battery_pct": 61
  },
  "confidence": 1.0,
  "geo": { "lat": 34.0522, "long": -118.2437, "precision_meters": 3, "geolocation_source": "onboard_gps" }
}
```

**Cloud evidence — access log entry:**
```json
{
  "evidence_id": "7a4c9e51-...",
  "category": "cloud_evidence",
  "artifact_type": "access_log",
  "title": "Cloud storage object access, bucket case-archive",
  "source": { "system": "cloud-provider-audit-export", "collection_method": "api_pull" },
  "attributes": {
    "principal": "user:analyst@example.org",
    "action": "GetObject",
    "resource": "s3://case-archive/evidence/1f3b2e2a.bin",
    "source_ip": "198.51.100.23",
    "result": "success"
  },
  "confidence": 1.0,
  "classification": { "sensitivity": "confidential", "legal_authority_ref": "internal_policy_ref:IT-SEC-04" }
}
```

**Derived entity — Person (proposed by AI):**
```json
{
  "entity_id": "8b5d0f62-...",
  "entity_type": "person",
  "canonical_name": "Unknown Subject 1",
  "aliases": ["@example_user"],
  "confidence": 0.7,
  "status": "proposed",
  "created_by": { "type": "ai", "model_ref": "investigation-correlator-v0" }
}
```

**Derived relationship, grounded in evidence:**
```json
{
  "relationship_id": "9c6e1073-...",
  "type": "located_at",
  "from_entity_id": "8b5d0f62-...",
  "to_entity_id": "location:5th-street-warehouse",
  "confidence": 0.65,
  "valid_from": "2026-06-10T21:15:00Z",
  "supporting_evidence_ids": ["4d1f9a22-..."],
  "status": "proposed",
  "created_by": { "type": "ai", "model_ref": "investigation-correlator-v0" }
}
```

**Custody ledger — first two entries for the SMS example above:**
```json
[
  {
    "custody_event_id": "a1...",
    "evidence_id": "1f3b2e2a-...",
    "sequence_number": 1,
    "event_type": "collected",
    "occurred_at": "2026-06-02T14:03:00Z",
    "actor_id": "examiner:priya.n",
    "actor_role": "forensic_examiner",
    "prev_event_hash": null,
    "entry_hash": "h1..."
  },
  {
    "custody_event_id": "a2...",
    "evidence_id": "1f3b2e2a-...",
    "sequence_number": 2,
    "event_type": "ingested",
    "occurred_at": "2026-06-02T18:41:12Z",
    "actor_id": "system:ingestion-connector",
    "actor_role": "system",
    "prev_event_hash": "h1...",
    "entry_hash": "h2..."
  }
]
```

---

*Keep this document synchronized with `packages/evidence-schema`'s implementation and with [System Design](system-design.md) §4 (which module owns which category's ingestion) and §11's event catalog (evidence lifecycle events reference these object shapes). Any MAJOR schema version bump (Section 12) should be recorded as an ADR.*
