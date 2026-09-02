# Episteme — Data Sources Runbook

**Status:** Living document  
**Purpose:** Operational memory for each open-data source: what it is, how to acquire it, update cadence, scripts, known failures, and license posture.  
**Related:** `docs/09-extraction-contract.md`, `docs/08-data-storage-principles.md`

---

## 0. Quick status board (as of 2026-08-31)

| Source | Raw on disk | Script(s) | Notes |
|--------|-------------|-----------|-------|
| PubMed baseline + daily | Yes | PubMed aria2 / MD5 scripts | Primary abstract corpus |
| ApolloCorpus | Yes | `download_apollo_corpus.sh` | Multilingual; license diligence open |
| EPMC preprints | Yes | `download_europepmc.sh` | 5 range archives |
| EPMC preprint abstracts | Deferred | same | Upstream zip often unlisted/unfetchable |
| EPMC ID mappings | **Corrupt file** | `download_epmc_id_mappings.sh` | **Re-download required** |
| Author manuscripts | Partial / retry | `download_author_manuscripts.sh` | EPMC 503 intermittent |
| EPMC lite metadata | Incomplete | `download_epmc_lite_metadata.sh` | Resume `PMCLiteMetadata.tgz` |
| PMC commercial OA | Sample only | `download_pmc_oa_comm.sh` | Full pull on SSD |

Storage root (typical): `EpistemeData/01_raw/...`

---

## 1. PubMed (NLM)

### What
Citation/abstract XML for the MEDLINE/PubMed corpus. Baseline yearly + **daily** update files. Not full text.

### Location (raw)
```text
01_raw/pubmed/baseline/     # pubmedNNNN.xml.gz + .md5
01_raw/pubmed/updatefiles/  # daily increments + .md5
```

### Official endpoints
- Baseline: `ftp://ftp.ncbi.nlm.nih.gov/pubmed/baseline/`
- Updates: `ftp://ftp.ncbi.nlm.nih.gov/pubmed/updatefiles/`
- Prefer listing exact filenames (no shell globs on FTP).

### Scripts
- Baseline / update download with aria2c + resume
- Separate MD5 download + `verify` / repair helpers (as built earlier in the project)

### Frequency
| Artifact | Cadence |
|----------|---------|
| Baseline | Annual (new year set) |
| Update files | **Daily** |

### How to run (pattern)
```bash
# Baseline (example pattern — use project scripts)
# Download listed pubmed*.xml.gz + matching .md5
# Verify MD5; repair mismatches

# Daily updates: resume from last successful date / file list
```

### Extraction posture
- `source = pubmed`
- `subset = open_metadata`
- `license = unknown` (unless policy maps NLM terms more finely)
- Expect many **title-only** historical records → `extract_status=partial` when short

### Known issues
| Issue | Mitigation |
|-------|------------|
| FTP glob `pubmed26n*.xml.gz` fails | List remote names; feed explicit URL list to aria2c |
| MD5 mismatches | Repair script; re-download failed files only |
| Early PMIDs lack abstracts | Normal; status rules treat short title-only as `partial` |

### Sample audit notes
- MeSH, authors, journal, language, publication types populate well.
- Validate abstract extraction on a **recent** baseline file, not only pmid 1–5.

---

## 2. PMC Commercial OA (`oa_comm`) — AWS Open Data

### What
Full-text open-access articles under **commercial-friendly** licenses (CC0, CC BY, CC BY-SA, CC BY-ND). Post–August 2026 layout is **per-article** objects + `metadata/PMC{id}.{ver}.json` (legacy `oa_comm/xml/all/` bulk prefixes removed/empty).

### Location (raw)
```text
01_raw/pmc/oa_comm/
  metadata/     # PMC*.json
  xml/          # PMC*.xml when downloaded
```

### Official endpoints
- Bucket: `s3://pmc-oa-opendata` (public, `--no-sign-request`)
- HTTPS: `https://pmc-oa-opendata.s3.amazonaws.com/`
- Metadata: `metadata/PMC{id}.{version}.json` with `xml_url`, `text_url`, `license_code`, …
- Docs: NCBI “Accessing PMC Article Datasets Using Amazon Web Services”

### Scripts
```bash
./download_pmc_oa_comm.sh ./01_raw/pmc/oa_comm xml false <LIMIT>
# LIMIT=20 sample; LIMIT=0 full commercial set (huge — use SSD)
```
Python: ESearch commercial filters **or** metadata-driven download; verifies `license_code`.

### Frequency
| Artifact | Cadence |
|----------|---------|
| Article objects / metadata | Continuous / daily |
| Inventory reports under bucket | Daily (S3 inventory) |

### How to run
```bash
# Sample path test
./download_pmc_oa_comm.sh ./01_raw/pmc/oa_comm xml false 20

# Full (SSD)
./download_pmc_oa_comm.sh /Volumes/SSD/.../01_raw/pmc/oa_comm xml false 0
```
Optional: `export NCBI_API_KEY=...` for ESearch rate limits.

### Extraction posture
- `source = pmc_oa_comm`
- `subset = commercial` (after license check)
- Populate `license` from `license_code`; `pmc_version`, `is_manuscript`, `is_historical_ocr`, `pdf_url` from metadata
- Prefer **XML** for authors/journal/abstract/body; JSON alone is incomplete for bibliography

### Known issues
| Issue | Mitigation |
|-------|------------|
| Legacy path `oa_comm/xml/` empty after Aug 2026 | Use per-article + metadata JSON layout |
| Full set ~millions of articles | SSD; staged `LIMIT`; long-running `tmux` |
| Authors missing if only JSON used | Parse JATS XML |

### Sample audit notes
- CC BY commercial subset confirmed on samples.
- Body text recoverable from XML; extend parser for authors/journal.

---

## 3. Europe PMC — Preprints (full text)

### What
Open preprint full text packaged in PMCID/PPR range `.xml.gz` files on EBI FTP.

### Location (raw)
```text
01_raw/europepmc/preprints/
  PPR*_*.xml.gz
  remote_manifest.txt
```

### Official endpoints
```text
ftp://ftp.ebi.ac.uk/pub/databases/pmc/preprints/
```

### Scripts
```bash
./download_europepmc.sh ./01_raw/europepmc preprints
# or: both  (preprints + abstracts)
```

### Frequency
Incremental range files as EPMC publishes; not a simple daily single file.

### How to run
```bash
./download_europepmc.sh ./01_raw/europepmc preprints
# Restartable: size-matched files skipped
```

### Extraction posture
- `source = epmc_preprint`
- License from JATS → normalised `license` + `license_url`; NC licenses → `subset=text_mining`
- Strong `title` / `abstract` / `body_text` in samples

### Known issues
| Issue | Mitigation |
|-------|------------|
| FTP listing flakiness | Dual `--list-only` + LIST parse |
| Authors under-parsed in audit | Improve JATS contrib mapping |

### Sample audit notes
- License differentiation (CC BY vs BY-NC) works for subset routing.

---

## 4. Europe PMC — Preprint abstracts

### What
Bulk abstract packages (zip). Often only a **current-month** name appears; file may be listed but not fetchable.

### Location (raw)
```text
01_raw/europepmc/preprint_abstracts/
```

### Official endpoints
```text
ftp://ftp.ebi.ac.uk/pub/databases/pmc/preprint_abstracts/
```

### Scripts
```bash
./download_europepmc.sh ./01_raw/europepmc abstracts
```

### Frequency
Monthly-style packages when published.

### Known issues
| Issue | Mitigation |
|-------|------------|
| `Resource not found` for listed zip | Probe/fetchable check; **defer current month** |
| Empty discovery | Non-fatal under `both` mode |

### Status
**Deferred** until upstream publishes stable files. Preprint **full text** is the valuable path.

---

## 5. Europe PMC — PMID–PMCID–DOI mappings

### What
Single mapping table for joins across PubMed / PMC / DOI.

### Location (raw)
```text
01_raw/europepmc/id_mappings/
  PMID_PMCID_DOI.csv.gz    # expected
  remote_manifest.txt
```

### Official endpoints
```text
https://europepmc.org/ftp/DOI_mappings/
# file: PMID_PMCID_DOI.csv.gz (~monthly overwrite on 1st)
```

### Scripts
```bash
./download_epmc_id_mappings.sh ./01_raw/europepmc/id_mappings
```

### Frequency
**Monthly** (file overwritten on the first of the month per EPMC docs).

### Integrity check (mandatory)
```bash
gzip -dc ./01_raw/europepmc/id_mappings/PMID_PMCID_DOI.csv.gz | head -5
```
Expect real headers/rows.  

**Audit finding (2026-08-31):** local file contained Oracle errors (`ORA-12537`, `SP2-0751`) — **not** mapping data. Treat as `corrupt_source`; delete and re-download.

### Extraction posture
- **Not** loaded into `episteme.articles`
- Target table: `episteme.id_map`
- Only after integrity check passes

### Known issues
| Issue | Mitigation |
|-------|------------|
| HTTP **503** from Cloudflare | Retry later; path is correct |
| Corrupt payload | Validate with `head` after every download |
| macOS Bash `mapfile` | Use portable script (no `mapfile`) |

---

## 6. Europe PMC — Author manuscripts

### What
Author-accepted manuscripts (XML/TXT tarballs by PMCID range) for text mining / funder OA policies. **Not** default commercial training material.

### Location (raw)
```text
01_raw/europepmc/author_manuscripts/xml/
  author_manuscript_xml.PMC00*.baseline.*.tar.gz
  author_manuscript_xml.incr.*.tar.gz
  *.filelist.csv / *.filelist.txt
```

### Official endpoints
```text
https://europepmc.org/ftp/manuscripts/
```

### Scripts
```bash
./download_author_manuscripts.sh ./01_raw/europepmc/author_manuscripts xml all
# modes: all | baseline | incr
```

### Frequency
| Artifact | Cadence |
|----------|---------|
| Baseline packages | Periodic refresh (dated in filename) |
| Incrementals | **Daily** when published |

### How to run
```bash
# Prefer SSD for full baseline set (multi-GB tars)
./download_author_manuscripts.sh ./01_raw/europepmc/author_manuscripts xml all
```

### Extraction posture
- `source = epmc_manuscript`
- `subset = text_mining`
- `license` often free-text fair-use / text-mining notice → normalise to `text_mining`

### Known issues
| Issue | Mitigation |
|-------|------------|
| HTTP **503** (Cloudflare) | Retry; path verified when origin up |
| Large baselines (1–3GB+) | Disk check; `tmux`; restartable skips |
| Authors under-parsed | JATS mapping improvement |

### Sample audit notes
- Full text quality good; commercial mix **forbidden** by default policy.

---

## 7. Europe PMC — Lite full-text metadata

### What
Weekly bulk of **key metadata** (lite API shape) for full-text articles — enrichment, not primary body text.

### Location (raw)
```text
01_raw/europepmc/metadata_lite/
  PMCLiteMetadata.tgz
```

### Official endpoints
```text
https://europepmc.org/ftp/pmclitemetadata/
```

### Scripts
```bash
./download_epmc_lite_metadata.sh ./01_raw/europepmc/metadata_lite
```

### Frequency
**Weekly**

### How to run
```bash
# Resume partial tgz
./download_epmc_lite_metadata.sh ./01_raw/europepmc/metadata_lite
```

### Extraction posture
- Enrichment / join support; not primary `text` for training
- `subset = open_metadata`

### Known issues
| Issue | Mitigation |
|-------|------------|
| ~2GB tgz long download | aria2c `-c` resume |
| Audit parse failed on incomplete archive | Finish download; re-audit |
| Intermittent 503 | Retry |

---

## 8. ApolloCorpus (FreedomIntelligence)

### What
Multilingual medical dialogue / text corpus used for broad language coverage. Packaged via Hugging Face.

### Location (raw)
```text
01_raw/multilingual/apollo/raw/
```

### Official endpoints
- Hugging Face: `FreedomIntelligence/ApolloCorpus`
- Use `hf download` (CLI); `huggingface-cli` deprecated

### Scripts
```bash
./download_apollo_corpus.sh
# extract helper auto-unzips when needed
```

### Frequency
Static corpus releases (not daily). Re-pull on new upstream version if announced.

### Extraction posture
- `source = apollo`
- Sparse bibliographic fields expected
- `language` should be populated when present in files
- `subset = other` until commercial-use diligence on corpus license is finished
- `license = corpus_declared` (refine after reading upstream terms)

### Known issues
| Issue | Mitigation |
|-------|------------|
| Sample audit saw text-only hashes | Inspect actual `*_text.json` schema; improve parser |
| License for commercial LLM training | Explicit diligence before `subset=commercial` |

### Brew note (macOS)
```bash
brew install huggingface-cli   # provides `hf`
```

---

## 9. Cross-cutting operations

### Sample field audit
```bash
python3 analyze_source_samples.py \
  --data-root ./01_raw \
  --n 5 \
  -o sample_audit_report.md \
  --json sample_audit_report.json
```
Re-run after fixing corrupt ID mappings, completing lite metadata, or parser improvements.

### Europe PMC 503 pattern
- Symptom: `HTTP/2 503`, `server: cloudflare`
- Paths are still correct; wait and retry
- Probe: `curl -I https://europepmc.org/ftp/manuscripts/`

### Restartability standard
All bulk downloaders should:
1. Discover remote file list → `remote_manifest.txt`
2. Skip size-matched local files
3. Use aria2c `-c` or equivalent
4. Write `last_sync_utc.txt` when applicable

### License routing (training)
| `subset` | Use |
|----------|-----|
| `commercial` | Parametric LLM commercial path |
| `text_mining` | Research / non-commercial product policy review |
| `open_metadata` | Metadata/abstract features; not OA full-text commercial claim |
| `other` | Hold until diligence clears |

---

## 10. Suggested ops cadence

| When | Action |
|------|--------|
| Daily | PubMed updatefiles; PMC OA delta if running continuous sync; author manuscript incr when EPMC up |
| Weekly | EPMC lite metadata; review failure markers |
| Monthly | EPMC ID mappings (after integrity check) |
| On SSD ready | Full PMC `oa_comm`; consolidate `01_raw` |
| After each major pull | `analyze_source_samples.py` smoke audit |

---

## 11. Document control

| Version | Date | Notes |
|---------|------|-------|
| v1 | 2026-08-31 | Initial runbook from acquisition phase + sample audit |
