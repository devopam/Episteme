# Reference code lists

Date: 2026-11-15. Status: design for the implementation in this change.

## Goal

Load seven lookup lists into PostgreSQL and read one code at a time, with its version and its licence notice. The lists are the E2B OID catalogue, the E2B code lists v2.11, LOINC, ISO 3166 code elements, ISO 639-2, UCUM, and EDQM Standard Terms.

## Decision

These lists are not `episteme.articles` and they are not part of the training corpus. They do not join the PBRER tool list. That list follows the section map. MCPg already reads PostgreSQL, so it reads the view `episteme.code_current`. `lookup_code` returns the same claim shape as the other lookups. The ICSR checker keeps reading its JSON rule file.

## What the guide actually contains

Appendix I (F) of ICH E2B(R3) IG v5.03 (18 July 2025, package `e2/ig_v1_11`) is one sentence. It says the code lists are published as XML beside the guide. It is not a table of OIDs.

The OID catalogue is section 3.2, tables 1 to 8, of the same PDF (pages 26–32). Table 1 has ten IDMP rows and no numeric OID yet: the guide says the registered OID will replace the ISO identifier standard when it exists. Tables 2 to 8 assign OIDs. Many elements share one OID, so the primary key cannot be the OID. The stored code is `t<n>:<element id>` (table 7 uses the technical name, because those rows have no element id). The OID is stored beside the row.

The 2013 file `2013-04-17 ICH E2B(R3) OID Listing.pdf` is history. The duplicate package folder `page_e2br3-.../IG_Complete_Package_v1_11_1` is ignored. The pinned package is `e2/ig_v1_11`.

Third-party term content inside the guide (MedDRA terms, ISO term lists, UCUM codes, EDQM terms) is not stored. The MedDRA OID `2.16.840.1.113883.6.163` is stored only as an identifier ICH publishes.

## Lists

| system | code | release | fetch |
|--------|------|---------|-------|
| `e2b_oid` | `t<n>:<element or name>` | `ig-v5.03-<sha12 of the PDF>` | already on disk |
| `e2b_cl` | `CLn:<code>` | `v2.11-<sha12 of the chosen XML>` | already on disk. Prefer `.gc.xml`. A revised duplicate must list the same codes |
| `loinc` | `LOINC_NUM` | the owner’s folder name | owner places the zip and `NOTICE.txt` |
| `iso3166` | alpha-2 | the owner’s folder name | owner places `codes.csv` with the header `alpha_2,alpha_3,numeric,short_name_en`, plus `NOTICE.txt` |
| `iso639` | bibliographic alpha-3 | sha12 of the file | `ISO639_2_URL` |
| `ucum` | `<kind>:<printSymbol or name>` | sha12 of the essence file | `UCUM_ESSENCE_URL` and `UCUM_LICENSE_URL`. The XML is stored verbatim |
| `edqm` | `<class>:<term code>` | version label, or a content hash | `EDQM_STAPI_BASE`. Every class from `/classes`, full data, `tags=1`. Only when both keys are set |

Licence and cadence for each list are in `docs/02` section 2.3.1. SIL ISO 639-3 is out of scope: its terms forbid a product that redistributes the code set, and a table MCPg can read would do that.

## Database

Migration `0013_reference_codes.sql`:

- `episteme.code_system` — one row per system and release. `licence_notice` is non-empty. At most one `is_latest` per system.
- `episteme.code` — primary key `(system_key, release_id, code)`. `extra` is jsonb.
- `episteme.code_current` — the codes of the latest release, with the notice.

The load deletes that release, inserts it, and then marks it latest, in one transaction. It records `load_commit` the first time a system is loaded and `load_replace` after that. No new audit event type. A success marker is `refmeta/<system>__<release>`. A second run skips. `--force` reloads.

A missing optional list is skipped. A file that is present but invalid fails. Zero codes, a duplicate code, or an empty notice fails before any insert.

## Download

`run_pipeline.sh refmeta download` fetches UCUM and ISO 639-2. EDQM is included when `EDQM_STAPI_LOGIN` and `EDQM_STAPI_SECRET_KEY` are set. Combined download with the keys unset logs a skip and exits 0. An explicit EDQM request with the keys unset exits 3.

The signature is the one the owner recorded: header `X-STAPI-KEY` = `login|signature`, where the signature is the last 22 characters of Base64(HMAC-SHA512(secret, `VERB&URI&HOST&Date`)), plus a `Date` header.

Endpoint URLs live only in `scripts/data/_lib/sources.env`. The EDQM secret is read only by `config.py` and is excluded from `repr`.

The loader refuses an EDQM release until the owner places `NOTICE.txt` or `CONDITIONS.txt` beside it. The API conditions of use were not read.

## Out of scope

EDQM CEP, SPOR RMS, MedDRA terms, SIL ISO 639-3, ISO standard PDFs, a PBRER chat tool, and any `articles` or corpus row. A production load is a runbook step, not part of this change.

## Tests

Parsers are tested on synthetic fixtures, with no network. The EDQM signature has one frozen vector. A local test reads the real IG PDF when `D:\episteme` has the package and is skipped otherwise. The migration test is `pg`-marked and runs only against `episteme_test`.
