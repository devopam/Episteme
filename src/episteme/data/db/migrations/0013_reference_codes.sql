-- Migration 0013 -- reference code lists (OIDs, E2B code lists, LOINC,
-- ISO 3166, ISO 639-2, UCUM, EDQM dose forms and routes).
-- Idempotent. Apply as episteme_sys_admin (owner). One transaction.
-- Design: docs/superpowers/specs/2026-11-15-reference-metadata-design.md
--
-- These rows are lookup data. They are not articles and they are not part of
-- the training corpus. One release of a system is current (is_latest).
-- A licence notice is required: an empty notice cannot be loaded.
--
-- code.code is the identity inside one release. For the OID catalogue it is
-- t<n>:<element id>, not the OID: many elements share one OID. The OID, when
-- the guide has assigned one, is in code.extra.

BEGIN;

SET LOCAL lock_timeout = '60s';

CREATE TABLE IF NOT EXISTS episteme.code_system (
    system_key text NOT NULL,
    release_id text NOT NULL,
    version text,
    oid text,
    licence_notice text NOT NULL,
    source_file text NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    is_latest boolean NOT NULL DEFAULT false,
    PRIMARY KEY (system_key, release_id),
    CONSTRAINT code_system_notice_nonempty CHECK (length(btrim(licence_notice)) > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS code_system_one_latest_idx
    ON episteme.code_system (system_key) WHERE is_latest;

CREATE TABLE IF NOT EXISTS episteme.code (
    system_key text NOT NULL,
    release_id text NOT NULL,
    code text NOT NULL,
    display text,
    status text,
    extra jsonb NOT NULL DEFAULT '{}',
    source_file text NOT NULL,
    PRIMARY KEY (system_key, release_id, code),
    FOREIGN KEY (system_key, release_id)
        REFERENCES episteme.code_system (system_key, release_id)
);

CREATE OR REPLACE VIEW episteme.code_current AS
SELECT c.system_key,
       c.release_id,
       s.version,
       s.oid,
       s.licence_notice,
       c.code,
       c.display,
       c.status,
       c.extra,
       c.source_file
FROM episteme.code c
JOIN episteme.code_system s
  ON s.system_key = c.system_key
 AND s.release_id = c.release_id
WHERE s.is_latest;

COMMIT;
