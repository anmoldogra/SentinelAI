# SentinelAI — Product Requirements Document

**Version:** 1.0 (Draft)
**Status:** Draft — for executive and engineering review
**Last updated:** 2026-07-19
**Related documents:** [Vision](vision.md) · [Architecture](architecture.md) · [Roadmap](roadmap.md) · [ADRs](adr/)

This document defines *what* SentinelAI must do and *why*, for both executive stakeholders (market, risk, compliance) and engineering (requirements traceable to implementation and test). It is the canonical requirements source and complements — rather than replaces — [Vision](vision.md) (why the product exists), [Architecture](architecture.md) (how it's built), and [Roadmap](roadmap.md) (delivery sequencing). Those documents should stay consistent with this one; when a phase completes or a compliance decision is made, revisit this PRD.

---

## 1. Vision

SentinelAI is the AI-native system of record for investigations — the platform where law enforcement, intelligence, and enterprise security professionals bring fragmented evidence (forensic artifacts, open-source intelligence, threat feeds, social media activity) and leave with a correlated, defensible, court- and audit-ready case.

The next decade of investigative work will be defined not by faster manual triage, but by AI that surfaces the connection a human would eventually find — in minutes rather than weeks — while keeping a human accountable for every conclusion. SentinelAI exists to be that platform: the place where evidence becomes insight, and insight becomes an admissible, auditable case.

## 2. Mission

To give every investigator — regardless of agency size, budget, or technical sophistication — access to AI-assisted cross-domain correlation that today only the best-resourced units can approximate manually, without ever compromising evidentiary integrity, individual rights, or auditability.

Concretely:
- Unify the investigator's toolchain (forensics, OSINT, threat intel, social media, case management) into one system.
- Use AI to reduce the time between "evidence exists" and "an investigator has an actionable, explainable lead."
- Make every AI-assisted conclusion traceable to source evidence and reviewable by a human, by design — not as an afterthought.

## 3. Problem Statement

Investigators today operate across a fragmented toolchain: forensic imaging/analysis tools, OSINT scraping scripts and browser tabs, a threat intelligence platform, social media monitoring dashboards, and a case management system (often a shared drive or legacy RMS) that connects none of it. This fragmentation compounds into four problems:

1. **Correlation is manual and doesn't scale.** Connecting a threat actor's infrastructure to a social media alias to a piece of forensic evidence requires an analyst to hold all of it in their head, or build it by hand in a spreadsheet. Backlogs grow faster than headcount.
2. **Time-to-insight is measured in days or weeks**, not minutes — most of it spent collecting and reformatting data rather than analyzing it.
3. **Investigative work is hard to audit.** When evidence handling and analytical reasoning live across five disconnected tools, reconstructing "how did we get to this conclusion" for a court, oversight body, or internal review often isn't possible with confidence.
4. **AI adoption in this space is already happening, ungoverned.** Analysts use general-purpose AI tools informally today, with no audit trail, no evidentiary chain of custody, and no organizational visibility into what was AI-assisted versus human-verified. That's a liability, not a capability.

SentinelAI's premise: the fix isn't more tools — it's one platform that ingests everything into a common evidentiary model, uses AI to do the correlation work at machine speed, and makes every step of that process auditable and explainable by construction.

## 4. Target Users

SentinelAI serves three primary customer segments, each with distinct procurement, compliance, and deployment requirements (see [Section 10](#10-compliance-considerations)):

| Segment | Representative organizations | Primary need |
|---|---|---|
| **Law enforcement** | Municipal, state, and federal police; fusion centers; digital forensics units | Case-ready evidence correlation and chain of custody that holds up in court |
| **Intelligence & national security** | Federal intelligence agencies, counter-terrorism units, defense/homeland security | Cross-source correlation at scale, often air-gapped/classified deployment |
| **Enterprise security & trust & safety** | Corporate SOC, fraud/insider-threat teams, trust & safety orgs, MSSPs | Faster incident-to-case handoff, defensible internal investigation records |

Secondary users who consume SentinelAI outputs without operating the platform directly: prosecutors and legal counsel (case reports), compliance/audit reviewers, executive stakeholders (dashboards/metrics).

**Explicitly out of scope:** SentinelAI is an investigative tool for use under existing legal authority (warrant, subpoena, employer policy, or equivalent) — it is not a bulk/mass surveillance system and does not perform autonomous action against individuals. See [Section 9](#9-security-requirements) and [Section 10](#10-compliance-considerations) for the controls that enforce this.

## 5. User Personas

### 5.1 Detective / Law Enforcement Investigator — "Maria"
- **Role:** Case-carrying investigator at a metro police department, 8+ years experience.
- **Goals:** Build a case fast enough to keep up with caseload; produce evidence packages that survive cross-examination.
- **Pain points:** Spends more time reformatting evidence between tools than investigating; can't easily show a supervisor *why* she connected two pieces of evidence months later.
- **How SentinelAI helps:** One case view with every linked piece of evidence, an audit trail she didn't have to build by hand, and AI-surfaced connections she can accept, reject, or annotate — each with a visible "why."

### 5.2 Intelligence Analyst — "Daniel"
- **Role:** Analyst at a regional fusion center correlating threat reporting across agencies.
- **Goals:** Surface non-obvious connections across large volumes of OSINT and threat intelligence before an incident, not after.
- **Pain points:** Data volume outpaces manual review; existing tools don't talk to each other; classification/handling requirements often force air-gapped, disconnected workflows.
- **How SentinelAI helps:** Cross-domain AI correlation at a volume no manual process can match, deployable in an isolated/air-gapped environment when required.

### 5.3 Digital Forensics Examiner — "Priya"
- **Role:** Certified forensic examiner processing seized devices for multiple case teams.
- **Goals:** Process artifacts quickly without ever weakening the chain of custody that makes her findings admissible.
- **Pain points:** Chain-of-custody documentation is manual and error-prone; her findings often sit disconnected from the rest of the case until someone manually links them.
- **How SentinelAI helps:** Automatic, immutable custody logging on ingestion; forensic findings become first-class evidence linked into the case the moment they're processed.

### 5.4 OSINT Researcher — "Tomas"
- **Role:** Open-source researcher supporting both law enforcement and corporate investigations.
- **Goals:** Verify and corroborate findings quickly; avoid citing unreliable sources.
- **Pain points:** No systematic way to score source reliability; findings live in browser bookmarks and personal notes, disconnected from the case.
- **How SentinelAI helps:** Structured OSINT capture with source reliability scoring, feeding directly into the same evidentiary model as every other domain.

### 5.5 SOC / Enterprise Security Analyst — "Aisha"
- **Role:** Analyst on a corporate insider-threat and fraud investigations team.
- **Goals:** Move from alert to a documented, HR/legal-defensible internal case quickly.
- **Pain points:** Security tooling (SIEM/EDR) and case management are disconnected; every investigation requires manually assembling a timeline for legal/HR review.
- **How SentinelAI helps:** Same cross-domain correlation and case-building workflow as law enforcement, adapted to enterprise evidence sources and internal review/disclosure requirements.

### 5.6 Case Supervisor / Unit Commander — "Robert"
- **Role:** Oversees a team of investigators; accountable for case quality, resourcing, and compliance.
- **Goals:** Visibility into case status and team workload; confidence that AI use is governed, not ad hoc.
- **Pain points:** No aggregate view across cases; no way to audit *how* AI was used in a given case after the fact.
- **How SentinelAI helps:** Portfolio-level dashboards, full audit trail of AI-assisted findings and human review decisions, exportable for oversight.

## 6. Core Capabilities

1. **Unified Evidence Ingestion** — a single, canonical evidence model that every domain normalizes into, with source, timestamp, and integrity metadata preserved from the moment of intake.
2. **Digital Forensics Processing** — artifact parsing and chain-of-custody logging that meets evidentiary standards for court admissibility.
3. **OSINT Collection** — source connectors with reliability scoring, feeding structured findings into the case.
4. **Threat Intelligence Correlation** — IOC management and feed correlation, connecting external threat context to case evidence.
5. **Social Media Monitoring** — content and network capture relevant to an investigation, with the same evidentiary rigor as any other source.
6. **AI-Assisted Cross-Domain Correlation** — the platform's core differentiator: surfacing connections across domains that would otherwise require a human to manually cross-reference everything, with every suggestion attributed to its source evidence.
7. **Case Management & Chain of Custody** — case lifecycle, evidence linking, immutable audit trail, and report/disclosure generation.
8. **Human-in-the-Loop Review** — every AI-generated finding is a proposal, not a conclusion, until an analyst accepts, rejects, or annotates it.
9. **Alerting & Notification** — proactive alerts on new correlations, case status changes, and SLA/compliance breaches.
10. **Access Control & Audit** — role-based access, and a complete, immutable record of every action taken on a case by a human or by AI.

## 7. Functional Requirements

Requirements are grouped by capability area and numbered for traceability into implementation and test plans. "Shall" denotes a mandatory requirement; "should" denotes a strong preference deferrable with justification.

### 7.1 Evidence Ingestion (FR-1.x)
- **FR-1.1** The system shall accept evidence from forensics, OSINT, threat intelligence, social media, and manual-upload sources into a single canonical evidence model.
- **FR-1.2** Every ingested item shall be recorded with source, ingestion timestamp, ingesting user/system, and an integrity checksum.
- **FR-1.3** The system shall reject ingestion of an item that fails schema validation, with a clear error rather than silent partial ingestion.
- **FR-1.4** Once ingested, evidence records shall be immutable; corrections shall be recorded as new, linked entries, never as edits that overwrite history.

### 7.2 Case Management (FR-2.x)
- **FR-2.1** Users shall be able to create, update status, and close a case, with all status transitions logged.
- **FR-2.2** Users shall be able to link any evidence item, from any domain, to one or more cases.
- **FR-2.3** The system shall maintain a complete, immutable chain-of-custody log per evidence item, viewable per case.
- **FR-2.4** The system shall support generating a case report/disclosure package summarizing evidence, findings, and custody history for external (legal/court) use.
- **FR-2.5** The system shall support role-based access restricting case visibility to assigned investigators, supervisors, and explicitly granted reviewers.

### 7.3 OSINT (FR-3.x)
- **FR-3.1** The system shall support pluggable OSINT source connectors, each producing findings normalized into the canonical evidence model.
- **FR-3.2** Each OSINT finding shall carry a source-reliability score, editable by the analyst.
- **FR-3.3** The system shall record the query/method used to obtain each OSINT finding, for reproducibility.

### 7.4 Threat Intelligence (FR-4.x)
- **FR-4.1** The system shall ingest indicators of compromise (IOCs) from configured threat feeds.
- **FR-4.2** The system shall correlate case evidence against known IOCs and threat actor/campaign context automatically.
- **FR-4.3** The system should support standard threat intel interchange formats (e.g., STIX/TAXII) for feed ingestion and export.

### 7.5 Digital Forensics (FR-5.x)
- **FR-5.1** The system shall parse supported forensic artifact types (disk/memory images, file system metadata, extracted documents) into the canonical evidence model.
- **FR-5.2** The system shall log every access to a forensic artifact (view, export, re-analysis) as part of its chain-of-custody record.
- **FR-5.3** The system shall support hash-based integrity verification of forensic artifacts at ingestion and on demand thereafter.

### 7.6 Social Media (FR-6.x)
- **FR-6.1** The system shall capture social media content (posts, profile metadata, network/connection data) relevant to an investigation into the canonical evidence model, preserving capture timestamp and original source URL/identifier.
- **FR-6.2** The system shall support network/relationship analysis (e.g., account-to-account connections) as a queryable structure, not just flat records.

### 7.7 AI Investigation Engine (FR-7.x)
- **FR-7.1** The system shall generate candidate correlations and hypotheses across evidence from two or more domains within a case.
- **FR-7.2** Every AI-generated finding shall be traceable to the specific source evidence it was derived from, and display a confidence indicator.
- **FR-7.3** No AI-generated finding shall be presented as a confirmed conclusion; it shall require explicit analyst accept/reject/annotate action before it can appear in a case report.
- **FR-7.4** The system shall log every AI-generated finding and the analyst's disposition of it (accepted/rejected/annotated, by whom, when) as part of the case's audit trail.
- **FR-7.5** The system shall allow an analyst to ask the AI to explain the reasoning behind a specific correlation in terms of the underlying evidence.

### 7.8 Notifications (FR-8.x)
- **FR-8.1** The system shall notify relevant analysts when a new AI correlation is generated for a case assigned to them.
- **FR-8.2** The system shall support configurable notification channels (in-app, email, and others as adopted).
- **FR-8.3** The system shall alert supervisors on defined SLA breaches (e.g., case inactivity beyond a threshold).

### 7.9 Access Control & Audit (FR-9.x)
- **FR-9.1** The system shall enforce role-based access control at the case and evidence level.
- **FR-9.2** Every read and write action on evidence or case data shall be attributable to an authenticated user or system identity and recorded in an immutable audit log.
- **FR-9.3** The system shall support audit log export for external compliance/oversight review.
- **FR-9.4** The system shall support administrator-defined data retention and deletion policies per case/evidence category, consistent with applicable legal requirements.

## 8. Non-Functional Requirements

| Category | Requirement |
|---|---|
| Performance | Common case/evidence views shall render in under 2 seconds under normal load; AI correlation runs shall complete asynchronously with progress visibility, not block the UI. |
| Availability | Production deployments shall target 99.9% uptime for the case management and evidence access path (excludes scheduled maintenance windows). |
| Scalability | Ingestion and correlation workloads shall scale independently of the case management/UI layer as volume grows (see `docs/architecture.md` extraction path). |
| Deployment flexibility | The system shall support cloud, on-premises, and air-gapped/disconnected deployment models — the last is a hard requirement for a meaningful share of the intelligence/national-security segment. |
| Usability | Core investigator workflows (create case, ingest evidence, review an AI finding) shall be usable without vendor training beyond onboarding documentation. |
| Interoperability | The system shall expose an API (`packages/sdk`) and support standard interchange formats where they exist for a domain (e.g., STIX/TAXII for threat intel). |
| Internationalization | OSINT and social media content shall be handled without assuming English-only text; UI localization is a future-roadmap item, not MVP. |
| Observability | Every module/service shall emit structured logs and metrics sufficient to reconstruct "what happened" independent of the audit log (operational, not evidentiary, telemetry). |
| Data retention | The system shall support configurable, per-category retention and legal-hold policies, since retention requirements vary sharply by jurisdiction and customer type. |
| Maintainability | Domain boundaries shall remain independently testable and extractable into separate services without cross-cutting rewrites (see `docs/architecture.md`). |

## 9. Security Requirements

Security is foundational, not additive, given the sensitivity of the data this platform aggregates — a successful breach of SentinelAI would expose the correlated output of many investigations at once, making it an unusually high-value target.

- **SR-1** All data shall be encrypted at rest and in transit; encryption keys shall be managed via a dedicated key management service, never embedded in application config.
- **SR-2** Authentication shall support multi-factor authentication; the system shall support integration with an organization's existing identity provider (SSO/SAML/OIDC) rather than only local accounts.
- **SR-3** Authorization shall follow least privilege by default — role and case-level access, not blanket platform-wide access, for every non-administrator user.
- **SR-4** All administrative and evidence-affecting actions shall be logged to an append-only, tamper-evident audit store, separate from application data, that even administrators cannot silently edit.
- **SR-5** The platform shall undergo regular third-party penetration testing and vulnerability scanning prior to each major release; findings above a defined severity threshold shall block release.
- **SR-6** Secrets and credentials shall never be stored in source control; production secrets shall be managed via a dedicated secrets manager (see `docs/architecture.md` Open Questions).
- **SR-7** Multi-tenant deployments (where offered) shall enforce cryptographic and logical isolation between tenants sufficient to prevent any cross-tenant data access, including via AI model context.
- **SR-8** AI components shall not use customer/case data to train shared models across tenants without explicit, revocable customer consent.
- **SR-9** AI components shall be evaluated for prompt-injection and data-exfiltration risk where they process untrusted external content (e.g., scraped OSINT/social media text) before that content can influence system behavior.
- **SR-10** The system shall maintain a software bill of materials (SBOM) and a defined process for tracking and remediating vulnerable dependencies.
- **SR-11** The platform itself shall be subject to internal-misuse controls (e.g., anomaly detection on investigator access patterns) — a tool built to investigate must also resist being used to investigate without authorization.

## 10. Compliance Considerations

Compliance requirements vary significantly by customer segment; SentinelAI must be architected so that meeting the strictest applicable requirement for a given deployment doesn't require a different product.

| Area | Considerations |
|---|---|
| Law enforcement data handling | CJIS Security Policy (US) for any deployment touching criminal justice information; equivalent regional standards elsewhere. |
| Government cloud authorization | FedRAMP / StateRAMP (or regional equivalents) required for cloud deployments serving US government customers; on-prem/air-gapped deployment is the near-term alternative while authorization is pursued. |
| Digital evidence standards | Chain-of-custody and evidence-handling practices should align with recognized standards (e.g., ISO/IEC 27037) so forensic output is defensible under evidentiary rules (e.g., US Federal Rules of Evidence 901/902) and admissibility standards for expert/technical evidence (e.g., Daubert/Frye) — which is why AI findings must remain explainable and human-reviewed (FR-7.2–7.4), not black-box. |
| Data protection | GDPR/UK GDPR and equivalent regimes apply to any personal data collected via OSINT or social media monitoring, regardless of customer location, if data subjects are in-scope jurisdictions — purpose limitation and lawful basis must be enforceable per case. |
| Export control | ITAR/EAR (or equivalent) review required before offering the platform to certain intelligence/defense customers or jurisdictions; affects hosting, support-staff access, and even which engineers can work on certain deployments. |
| Enterprise security attestation | SOC 2 Type II and/or ISO 27001 are baseline expectations for enterprise security customers and should be pursued in parallel with, not after, initial enterprise sales. |
| Accessibility | Section 508 / WCAG 2.1 AA conformance is frequently a hard procurement requirement for government customers. |
| Civil liberties / lawful-use governance | The platform shall provide a mechanism to record the legal authority (warrant, subpoena, internal policy reference) under which a case/evidence item was collected, and shall not include any capability for autonomous action against an individual — every consequential action requires human sign-off, by design (FR-7.3). |

None of the above is fully resolved; each should be tracked as it's addressed (target certification, target date, owner) and elevated to an ADR where it drives an architectural decision (e.g., air-gapped deployment support, data residency).

## 11. MVP Scope

The MVP is the smallest version of the product that proves the core thesis — AI-assisted cross-domain correlation with full evidentiary integrity — not just a case-tracking tool. It corresponds to **Roadmap Phases 1–3** (`docs/roadmap.md`).

**In scope:**
- Authentication, authorization, and audit logging baseline
- Canonical evidence and case data model
- Case creation, evidence linking, and chain-of-custody logging
- Manual evidence ingestion plus one automated connector
- At least one non-forensic domain connector (OSINT, threat intel, or social media — whichever is prioritized once the initial customer segment is chosen; see [Section 14](#14-risks-and-assumptions))
- A first, narrow AI correlation capability: surfacing at least one class of cross-domain connection, with full attribution and mandatory analyst review
- Investigator web console covering the above workflows
- Basic notification on new AI-surfaced findings

**Explicitly out of scope for MVP:**
- All five domain connectors simultaneously
- Multi-tenancy
- Air-gapped/on-prem packaging (targeted for post-MVP hardening — see Future Roadmap)
- Advanced agentic/autonomous investigation workflows
- External SDK/API for third-party integration
- Formal compliance certifications (SOC 2, FedRAMP) — pursued in parallel, not gating MVP

**MVP exit criteria:** an analyst can create a case, ingest evidence from at least two distinct domains, receive at least one AI-surfaced cross-domain correlation attributed to its source evidence, and export a case report — with a complete, auditable chain of custody throughout.

## 12. Future Roadmap

Beyond MVP, aligned with `docs/roadmap.md` Phases 4–5 and beyond:

- **Enterprise hardening** — multi-tenancy, fine-grained RBAC, full audit trail export, formal disclosure/report generation, external SDK.
- **Deployment expansion** — on-premises and air-gapped packaging; pursuit of FedRAMP/StateRAMP and equivalent government authorizations.
- **Service extraction** — individual domain modules split into independently scaled services as team size and load justify it (see `apps/server/README.md`) — a technical evolution, not a product one; invisible to users.
- **Expanded domain coverage** — additional OSINT/threat-intel/social connectors as new evidence sources are prioritized by customer demand.
- **Advanced AI capabilities** — multi-step agentic investigation workflows with analyst checkpoints, predictive/proactive correlation (surfacing risk before an incident rather than after), natural-language case querying.
- **Ecosystem** — integrations marketplace/plugin model for third-party data sources and downstream systems (RMS/CAD integration for law enforcement, SIEM/ticketing integration for enterprise).
- **Collaboration** — controlled, auditable case/evidence sharing across teams or agencies where legally permitted, with the same chain-of-custody guarantees as single-agency use.

## 13. Success Metrics

| Metric | What it measures | Why it matters |
|---|---|---|
| Time-to-first-correlation | Median time from case creation to first AI-surfaced, analyst-confirmed correlation | Directly measures the core value proposition (speed) |
| Correlation acceptance rate | % of AI-surfaced findings an analyst accepts or annotates as useful (vs. rejects) | Measures AI output quality/precision, not just volume |
| Analyst hours saved per case | Self-reported/estimated reduction vs. pre-SentinelAI baseline workflow | Ties product usage to the efficiency thesis |
| Case cycle time | Time from case open to case close, before/after adoption | Downstream business outcome the platform should move |
| Audit completeness | % of evidence/case actions with a complete, unbroken chain-of-custody record (target: 100%) | Non-negotiable trust metric — any gap is a defect, not a KPI miss |
| Platform uptime | Against the 99.9% NFR target | Operational reliability |
| Customer/agency retention & expansion | Renewal rate, seats/cases per account over time | Commercial viability |
| Time-to-security-authorization | Days from customer request to completed security review/ATO for a given deployment | Leading indicator of enterprise/government sales cycle friction |

**Proposed north-star metric:** *median time from evidence ingestion to an analyst-confirmed, actionable lead* — it captures speed, AI quality, and human trust in one number, and directly reflects the mission statement.

## 14. Risks and Assumptions

### Risks

| Risk | Impact | Mitigation |
|---|---|---|
| AI false correlation leads to wrongful suspicion or wasted investigative effort | High — reputational, legal, and human harm | Mandatory human review before any AI finding is actionable (FR-7.3); confidence scoring; explainability (FR-7.5); no autonomous action capability |
| Civil liberties / public trust backlash, given the customer base (law enforcement, intelligence) | High — reputational, regulatory | Lawful-authority tracking per case, human-in-the-loop by design, no bulk-surveillance capability, transparent audit trail (Section 10) |
| Long, security-authorization-gated government sales cycles | Medium-high — revenue timing | Pursue authorizations (FedRAMP/CJIS alignment) in parallel with product development, not after; offer on-prem/air-gapped path as an interim option |
| Data quality/reliability of OSINT and social media sources (noisy, adversarial, subject to disinformation) | Medium — undermines correlation quality and trust | Source reliability scoring (FR-3.2), confidence indicators on AI output, analyst review as a mandatory gate |
| Single high-value target for attackers (aggregated investigation data) | High — breach impact | Security requirements in Section 9 treated as MVP-blocking, not post-launch hardening |
| Team scale: current delivery capacity is a single developer | Medium — velocity, support capacity, and ability to pursue enterprise/government certifications simultaneously | Roadmap sequencing (modular monolith, deferred infra) is deliberately chosen to maximize solo velocity; team growth is an assumption underlying Phases 4–5, not yet resourced |
| Legacy system integration friction (RMS/CAD for law enforcement, SIEM/ticketing for enterprise) | Medium — adoption friction | SDK and API-first design from Phase 4 onward; integrations prioritized by customer demand |
| Model/vendor dependency for AI capability | Medium — cost, availability, data-handling exposure | Hosted vs. self-hosted AI strategy remains an open architectural question (`docs/architecture.md`); resolve before Phase 3 begins in earnest |

### Assumptions

- **Primary initial customer segment is not yet decided** (law enforcement vs. enterprise security vs. intelligence) — this materially affects which compliance track (CJIS vs. SOC 2 vs. FedRAMP) and which domain connector to build first for MVP. This should be resolved as an explicit go-to-market decision, not inferred from this document, before Phase 2 connector prioritization begins.
- Customers will accept a phased rollout (not all five domains at once) in exchange for earlier access to AI correlation.
- At least a subset of target customers can operate in a cloud-hosted deployment; a subset (particularly intelligence/national security) will require on-prem/air-gapped — both are assumed necessary, not optional, long-term.
- Legal authority for evidence collection (warrant, subpoena, internal policy) exists upstream of the platform in every real deployment; SentinelAI records and respects that authority but is not the system that grants it.
- AI model quality (whichever provider/approach is chosen) is sufficient to produce correlations with a false-positive rate low enough that mandatory human review remains a check, not a bottleneck that negates the speed benefit.
