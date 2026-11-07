-- Migration 0011 -- label_section gains the section order and the product.
-- ordinal: the position of the section in the document; product: the product name when a
-- document holds several SmPCs (one row per section per product). Both are NULL for DailyMed.
-- Idempotent. Apply as episteme_sys_admin (owner). One transaction (see 0005).
-- Design: docs/superpowers/specs/2026-11-03-beta-data-completion-design.md, 9 item 6.

BEGIN;
SET LOCAL lock_timeout = '60s';

ALTER TABLE episteme.label_section ADD COLUMN IF NOT EXISTS ordinal integer;
ALTER TABLE episteme.label_section ADD COLUMN IF NOT EXISTS product text;

COMMIT;
