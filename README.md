# SentinelAI

**An AI-powered Investigation Intelligence Platform for Digital Forensics, OSINT, Threat Intelligence, Social Media, and Case Management.**

SentinelAI unifies evidence from forensic artifacts, open-source intelligence, threat feeds, and social media into a single, AI-assisted investigation workflow — helping analysts correlate signals, generate leads, and produce defensible, auditable case reports faster than manual triage allows.

> **Status: Scaffold stage.** This repository currently contains architecture, documentation, and folder structure only. No application code has been written yet. See [docs/roadmap.md](docs/roadmap.md) for what's next.

## Repository Layout

This is a monorepo. Each top-level folder — and every app/module/package within it — has its own `README.md` explaining its purpose in detail.

> **Architecture note:** with a single developer on the project, the backend is a **modular monolith** (`apps/server`), not eight separate microservices — same domain boundaries, one deployable. See [docs/architecture.md](docs/architecture.md) for why, and [apps/server/README.md](apps/server/README.md) for how each module can later be extracted into its own service without a redesign.

| Folder | Purpose |
|---|---|
| [`apps/web`](apps/web/) | Investigator-facing web console (UI) |
| [`apps/server`](apps/server/) | The backend — a modular monolith with one module per investigation domain (ingestion, OSINT, threat intel, forensics, social media, case management, AI investigation engine, notifications) |
| [`packages/`](packages/) | Shared libraries consumed by `apps/` (canonical evidence schema, types, utils, UI components, SDK) |
| [`infra/`](infra/) | Infrastructure as code (Docker, Kubernetes, Terraform) |
| [`docs/`](docs/) | Product vision, architecture, roadmap, and architecture decision records |
| [`scripts/`](scripts/) | Developer and operational tooling scripts |
| [`tests/`](tests/) | Cross-cutting end-to-end and integration test suites |
| [`.github/workflows/`](.github/workflows/) | CI/CD pipeline definitions |

## Documentation

- [Vision](docs/vision.md) — what SentinelAI is and why it exists
- [Architecture](docs/architecture.md) — system design, principles, team ownership, and tech stack
- [Roadmap](docs/roadmap.md) — phased delivery plan
- [CLAUDE.md](CLAUDE.md) — guidance for AI coding agents working in this repo
- [CONTRIBUTING.md](CONTRIBUTING.md) — branching, commit, and review process
- [SECURITY.md](SECURITY.md) — how to report a vulnerability
- [.github/CODEOWNERS](.github/CODEOWNERS) — who owns what

## Getting Started

Local development instructions will be added once Phase 1 (foundations) implementation begins. For now, `docker-compose.yml` provisions the baseline infrastructure dependencies (database, cache, event stream, object storage, vector store) that upcoming services will rely on:

```bash
docker compose up -d
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for branching, commit conventions, and review process, and [.github/CODEOWNERS](.github/CODEOWNERS) for who reviews what. Coordinate architecturally significant changes via `docs/adr/`.

## License

[MIT](LICENSE)
