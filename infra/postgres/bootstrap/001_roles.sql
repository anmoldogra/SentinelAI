-- =============================================================================
-- ADR-0004 Part 1 — PostgreSQL role provisioning for SentinelAI
-- =============================================================================
--
-- CLUSTER ADMINISTRATION ONLY.
--
-- This script is run ONCE per cluster by a privileged operator/DBA (or by the
-- Postgres container's `docker-entrypoint-initdb.d` on first initialisation).
-- It is NEVER run by the application, NEVER by Alembic, and NEVER from Python.
--
-- These are PRIVILEGE ROLES ONLY — pure, NOLOGIN privilege sets. They are NOT
-- authentication identities: none of them can log in, has a password, or is a
-- user. Authentication identities are issued OUTSIDE this repository by
-- Vault's database secrets engine (or the platform IAM) as short-lived login
-- users that are GRANTed membership in these roles and, INHERITing them,
-- acquire exactly these privileges for the lifetime of the credential. This is
-- why there are no passwords here and no secrets in the repository.
--
-- Why role creation lives here and not in a migration:
--   * CREATE ROLE is a CLUSTER-GLOBAL operation requiring a privileged role.
--   * Migrations run under the deliberately NON-privileged `sentinel_migrator`
--     (NOCREATEROLE below), so a migration cannot — and must not — create roles.
--   * The roles must already exist BEFORE the Part 2 privilege migrations run,
--     because those migrations GRANT/REVOKE table privileges TO these roles.
--
-- Idempotent: safe to run repeatedly. CREATE is guarded by `pg_roles`; the
-- ALTER/GRANT statements are no-ops when already applied and also self-heal a
-- role that pre-existed with different attributes (e.g. LOGIN → NOLOGIN).
--
-- Roles (ADR-0004 §Decision.1):
--   sentinel_migrator  Owns DDL. A login identity that is a member of this role
--                      runs the Alembic PreSync migration job.
--   sentinel_app       DML on mutable module tables. Member of sentinel_append.
--                      A login identity that is a member of this role is the
--                      application's runtime credential.
--   sentinel_append    INSERT + SELECT only on evidentiary tables. Granted to
--                      sentinel_app via membership, so the app inherits
--                      evidentiary read/append WITHOUT ever being able to
--                      UPDATE/DELETE/TRUNCATE them (Part 2 revokes those).
--
-- Least privilege: NONE of these roles is LOGIN / SUPERUSER / CREATEDB /
-- CREATEROLE / REPLICATION / BYPASSRLS.
-- =============================================================================

-- --- create the roles if they do not already exist (idempotent) --------------
-- All three are NOLOGIN: privilege sets, never login identities.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sentinel_migrator') THEN
        CREATE ROLE sentinel_migrator NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sentinel_app') THEN
        CREATE ROLE sentinel_app NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sentinel_append') THEN
        CREATE ROLE sentinel_append NOLOGIN;
    END IF;
END $$;

-- --- enforce least-privilege attributes (idempotent, self-healing) -----------
-- Each attribute is denied deliberately:
--   NOLOGIN       cannot authenticate — a privilege set, not a login identity.
--                 Login identities come from Vault / IAM and INHERIT these roles.
--   NOSUPERUSER   never bypass all permission checks.
--   NOCREATEDB    cannot create databases.
--   NOCREATEROLE  cannot create/alter roles — role administration stays with the
--                 cluster operator, so a compromised member credential cannot
--                 escalate by minting new roles.
--   NOREPLICATION cannot start replication or create replication slots.
--   NOBYPASSRLS   cannot bypass row-level security (relevant to ADR-0014).
--   INHERIT       a member automatically wields this role's privileges — this is
--                 how a Vault-issued login user acquires them, and how sentinel_app
--                 acquires sentinel_append's privileges (membership below).
ALTER ROLE sentinel_migrator
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT;
ALTER ROLE sentinel_app
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT;
ALTER ROLE sentinel_append
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT;

-- --- membership --------------------------------------------------------------
-- sentinel_app is a member of sentinel_append and (INHERIT above) wields its
-- evidentiary INSERT+SELECT. So a login identity that is a member of sentinel_app
-- holds, through one connection: mutable-table DML (sentinel_app's own grants,
-- applied by migrations) PLUS evidentiary INSERT/SELECT (via sentinel_append) —
-- but NEVER UPDATE/DELETE/TRUNCATE on evidentiary tables (Part 2 revokes those
-- from BOTH roles). No runtime SET ROLE switching is required.
GRANT sentinel_append TO sentinel_app;

-- --- connect + schema-creation privileges (scoped to the current database) ----
-- Granted to the privilege roles; member login identities inherit them. Uses
-- current_database() so the script is database-name-agnostic across profiles.
DO $$
BEGIN
    -- Members of these roles may connect to this database (explicit, even though
    -- CONNECT is granted to PUBLIC by default — robust if PUBLIC's CONNECT is revoked).
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO sentinel_migrator, sentinel_app',
        current_database()
    );
    -- Only the migrator may create schemas (Alembic env.py issues CREATE SCHEMA).
    -- NOTE: CREATE ON DATABASE permits creating SCHEMAS; it is NOT CREATEDB (denied
    -- above) and does not permit creating databases.
    EXECUTE format(
        'GRANT CREATE ON DATABASE %I TO sentinel_migrator',
        current_database()
    );
END $$;

-- Table-level privileges (GRANT INSERT,SELECT / REVOKE UPDATE,DELETE,TRUNCATE on
-- the evidentiary tables) are NOT set here — they are applied by the Part 2
-- Alembic migrations, which run under sentinel_migrator after these roles exist.
