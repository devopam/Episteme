-- Migration 0014 -- article_body.text becomes a VIRTUAL generated column.
-- Idempotent. Apply as episteme_sys_admin (owner). One transaction.
-- Design: docs/superpowers/specs/2026-11-16-article-text-virtual-design.md
--
-- The stored column is renamed to text_override. The new virtual column text
-- reads text_override where it is set, else the join of the non-blank parts
-- (title, abstract, body_text) with a blank line between them. Every reader of
-- text sees the same value as before. text_override = '' means "text is NULL"
-- (the sentinel): coalesce alone cannot tell "use the join" from "text was NULL
-- while a part is non-blank".
--
-- The join expression is article_schema.TEXT_JOIN_SQL, copied verbatim (a test
-- checks it). It must be IMMUTABLE: || and CASE only (concat_ws is refused).
--
-- The RENAME and ADD COLUMN are metadata only. The sentinel UPDATE scans every
-- article_body partition under the ACCESS EXCLUSIVE lock of the RENAME: apply
-- with no load and no corpus job running.
--
-- The whole body is guarded: when text_override exists, nothing runs. This keeps
-- the sentinel UPDATE from running again after the null-out (it would turn
-- "use the join" rows into NULL text). A database built from schema.sql already
-- has the final shape and skips. The table is named only as episteme.article_body
-- (the pg test substitutes it).

BEGIN;

SET LOCAL lock_timeout = '60s';

DO $mig$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_attribute
         WHERE attrelid = 'episteme.article_body'::regclass
           AND attname = 'text_override'
           AND NOT attisdropped
    ) THEN
        RAISE NOTICE '0014: article_body.text_override exists, nothing to do';
        RETURN;
    END IF;

    EXECUTE $q$ALTER TABLE episteme.article_body RENAME COLUMN text TO text_override$q$;

    EXECUTE $q$UPDATE episteme.article_body SET text_override = '' WHERE text_override IS NULL AND (CASE WHEN coalesce(btrim(title, E' \t\r\n'), '') = '' THEN '' ELSE title END || CASE WHEN coalesce(btrim(abstract, E' \t\r\n'), '') = '' THEN '' ELSE CASE WHEN coalesce(btrim(title, E' \t\r\n'), '') = '' THEN '' ELSE E'\n\n' END || abstract END || CASE WHEN coalesce(btrim(body_text, E' \t\r\n'), '') = '' THEN '' ELSE CASE WHEN coalesce(btrim(title, E' \t\r\n'), '') = '' AND coalesce(btrim(abstract, E' \t\r\n'), '') = '' THEN '' ELSE E'\n\n' END || body_text END) <> ''$q$;

    EXECUTE $q$ALTER TABLE episteme.article_body ADD COLUMN text text GENERATED ALWAYS AS (CASE WHEN text_override = '' THEN NULL ELSE coalesce(text_override, nullif((CASE WHEN coalesce(btrim(title, E' \t\r\n'), '') = '' THEN '' ELSE title END || CASE WHEN coalesce(btrim(abstract, E' \t\r\n'), '') = '' THEN '' ELSE CASE WHEN coalesce(btrim(title, E' \t\r\n'), '') = '' THEN '' ELSE E'\n\n' END || abstract END || CASE WHEN coalesce(btrim(body_text, E' \t\r\n'), '') = '' THEN '' ELSE CASE WHEN coalesce(btrim(title, E' \t\r\n'), '') = '' AND coalesce(btrim(abstract, E' \t\r\n'), '') = '' THEN '' ELSE E'\n\n' END || body_text END), '')) END) VIRTUAL$q$;
END
$mig$;

COMMIT;
