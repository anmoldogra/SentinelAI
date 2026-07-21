# Vision

> For detailed requirements (personas, functional/non-functional/security requirements, compliance, MVP scope), see the [PRD](prd.md).

## Problem

Investigators — in security operations, fraud, compliance, and corporate investigation teams — work across a fragmented toolchain: forensic imaging tools, OSINT scraping scripts, threat intel platforms, social media monitoring dashboards, and a case management system that ties none of it together. Correlating evidence across these sources is manual, slow, and hard to audit. Analysts spend more time collecting and cross-referencing data than actually investigating.

## Solution

SentinelAI is a unified **AI Investigation Intelligence Platform** that:

1. **Ingests** evidence from digital forensics, OSINT, threat intelligence, and social media sources into a single canonical evidence model.
2. **Correlates** that evidence using AI — surfacing connections, patterns, and anomalies a human analyst would otherwise have to find by hand.
3. **Assists investigation** by generating hypotheses, suggesting next investigative steps, and drafting findings for analyst review — never acting as an unchecked black box.
4. **Manages cases** end-to-end, preserving chain of custody and an auditable trail from raw evidence to final report.

## Target Users

- **Security operations / incident response analysts** correlating logs, alerts, and threat intel during an investigation.
- **OSINT researchers** aggregating and verifying open-source findings.
- **Fraud and compliance investigators** building evidence-backed cases.
- **Digital forensics examiners** who need artifact analysis tied to a defensible chain of custody.

## Principles

- **Analyst-in-the-loop, always.** AI accelerates investigation; it does not replace analyst judgment or make unreviewed determinations.
- **Evidence integrity is non-negotiable.** Every piece of evidence is traceable to its source, timestamped, and immutable once ingested.
- **Explainability over black boxes.** Every AI-generated lead or correlation must show its reasoning and source evidence, not just a conclusion.
- **Built for audit from day one.** Every action taken on a case — human or AI — is logged. Investigations must hold up to external scrutiny (legal, regulatory, internal review).
- **Domain-separated, platform-unified.** Forensics, OSINT, threat intel, and social media each have distinct data models and workflows, but converge into one investigation and one case record.

## North Star

An analyst opens a case, and within minutes has a correlated, evidence-linked picture of "what happened" assembled from every connected data source — with every AI-suggested connection traceable back to its source evidence.
