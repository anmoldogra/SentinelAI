# PostgreSQL bootstrap — role provisioning (ADR-0004 Part 1)

This directory holds **cluster-administration** SQL that must run **before** the application or its
migrations. It provisions the database *privilege roles* SentinelAI depends on.

```
infra/postgres/bootstrap/
  001_roles.sql   # creates sentinel_migrator / sentinel_app / sentinel_append (NOLOGIN)
```

## Why roles are infrastructure, not application concern

`CREATE ROLE` is a **cluster-global** operation: a role is not scoped to a database or a schema, it
is a property of the whole PostgreSQL *cluster*, and creating one requires a privileged role
(`CREATEROLE`/superuser). That places it firmly in the **cluster-administration** layer — the same
layer that provisions the cluster itself — not in the application or its schema migrations.

## Why migrations never `CREATE ROLE`

The migration job runs as **`sentinel_migrator`**, which is deliberately **`NOCREATEROLE`**
(least privilege). It therefore *cannot* create roles — and must not. If migrations could mint
roles, a compromised migration or migrator credential could escalate privileges by creating new
roles. Keeping role administration with the cluster operator is a security boundary, not an
inconvenience. Consequently the roles **must already exist before the Part 2 privilege migrations
run**, because those migrations `GRANT`/`REVOKE` table privileges *to* these roles.

## Why the roles are `NOLOGIN`

`sentinel_migrator`, `sentinel_app`, and `sentinel_append` are **pure privilege sets, not
authentication identities**. None of them can log in, has a password, or is a "user". This is
intentional:

- **No secrets in the repository.** There are no passwords to commit, rotate, or leak.
- **Authentication is externalised** to Vault / IAM (see below). A short-lived login identity is
  issued at runtime and *inherits* one of these roles; the privilege set and the credential
  lifecycle are cleanly separated.
- **Least privilege by construction.** A privilege set that cannot authenticate cannot itself be a
  breach vector; only the ephemeral, expiring member credential can, and it grants exactly the
  role's privileges for at most its lease lifetime.

Attributes enforced on all three (idempotent `ALTER ROLE`): `NOLOGIN NOSUPERUSER NOCREATEDB
NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT`.

## Deployment sequence

1. **Cluster + database created** (managed Postgres / CloudNativePG / `docker compose`).
2. **`001_roles.sql` runs** — once, by a privileged operator/DBA or an automated privileged step:
   - Production: CloudNativePG `postInitSQL`/bootstrap, or a one-shot privileged `Job`.
   - Local dev: mount into the Postgres container's `docker-entrypoint-initdb.d/` (runs on first
     init of an empty data volume), or run it by hand as a superuser. It is idempotent, so
     re-running on every reconcile is safe.
3. **Vault database secrets engine configured** to issue dynamic users as members of these roles
   (see below). *(Deployment concern — not in this repo.)*
4. **Alembic PreSync migration job runs** under a `sentinel_migrator` member identity — creates
   schemas/tables (Part 2 grants + Part 3 append-only triggers).
5. **Application starts**, connecting via a `sentinel_app` member identity.

## Ownership model

- **`sentinel_migrator`** owns DDL: a member identity runs migrations, so it *owns* the schemas and
  tables it creates. It has `CREATE ON DATABASE` (to create schemas) but is `NOCREATEDB` (cannot
  create databases) and `NOCREATEROLE`.
- **`sentinel_app`** is the application's runtime privilege set: DML on mutable module tables (grants
  applied by migrations) and, via membership in `sentinel_append`, evidentiary `INSERT`/`SELECT`.
- **`sentinel_append`** holds `INSERT`/`SELECT` only on evidentiary tables — never
  `UPDATE`/`DELETE`/`TRUNCATE` (Part 2 revokes those from both roles; Part 3 triggers are the
  backstop). Because `sentinel_app` inherits it, the app reads/appends evidence through **one**
  connection with **no runtime `SET ROLE`** switching.

Cluster-admin (superuser) owns role administration and this script; it is the only actor that may
create/alter roles.

## Vault integration

Per `docs/deployment-architecture.md` §11, database credentials are **short-lived and dynamic**:
Vault's database secrets engine (fronted by the External Secrets Operator) creates an ephemeral
Postgres login user, grants it membership in the appropriate privilege role
(`sentinel_migrator` for the migration job, `sentinel_app` for the application), and leases it for
~1 hour. The pod only ever sees the resulting `DATABASE_URL`. When the lease expires the login user
is dropped; the **privilege roles here are permanent, the login identities are ephemeral**. If a pod
is compromised, the leaked credential expires quickly and carries only its role's privileges.

## Local development guidance

The dev `docker-compose` runs Postgres as the superuser `sentinelai`, and the app's default
`DATABASE_URL` uses that superuser — so **local dev does not require these roles to function**. Run
`001_roles.sql` locally when you want to exercise the ADR-0004 privilege model (e.g. to let
`tests/integration/test_privileges_db.py` run instead of skip):

```bash
# against the running dev Postgres container, as the DBA (superuser):
docker exec -i sentinelai-postgres \
  psql -U sentinelai -d sentinelai -v ON_ERROR_STOP=1 \
  < infra/postgres/bootstrap/001_roles.sql
```

Because the roles are `NOLOGIN`, nothing logs in *as* them directly. The integration test assumes a
role with `SET LOCAL ROLE sentinel_append` (which works on a `NOLOGIN` role) while connected as the
superuser — no login identity for the privilege roles is needed locally.
