# Benchmark questions (working list)

Status: working list, started 2026-10-28 during the end-consumer brainstorm. Referenced by `docs/superpowers/specs/2026-10-28-end-consumer-programme-design.md` (Appendix A); project 1 of that design turns it into the full benchmark.

Purpose: a fixed set of real questions to score every model on the same terms: conversational generative models only: open medical instruct models (for example Meditron-class) and general open instruct models, each with and without the Episteme tool layer, and the Episteme model with it. Encoder models (for example BioBERT, PubMedBERT) cannot hold a conversation, so they are not scored here; if they are evaluated, it is under a separate protocol with its own input and output contract (programme design, section 7). The scores separate what the connected data adds from what training adds.

## How each question is scored

| Measure | Meaning |
|---|---|
| Exact facts | Every code, unit, identifier, count and date in the answer matches the reference store. |
| Invented values | Any code, identifier or citation that does not exist (target: zero). |
| Claim provenance | Each claim is supported by the exact record or query for its entity, jurisdiction and date, with a context-fit confidence. A real citation attached to the wrong claim fails. |
| Gaps reported | Each expected gap is stated, with its coverage numbers, and no conclusion is drawn past it. |
| Paths found | The answer finds the expected connections across sources and cites each one. |
| Known vs new | The answer separates what the label or reference already states from what is new. |

## Entry format

- **ID / persona / domain:** persona is `researcher` or `pv-bot`; domain is `human drug`, `device` or `animal`.
- **Question:** as the caller types it.
- **Expected answer:** the steps and the result shape, not a fixed text.
- **Must be exact (fetched, never generated):** values the post-answer check must verify.
- **Expected gaps:** what the data cannot show; the answer must say so.
- **Sources needed:** with the current data status.
- **Origin:** who proposed it.

## Questions

### Q1. Sudden co-reporting of an established drug

- **Persona / domain:** pv-bot or researcher / human drug
- **Question:** "Drug X is suddenly being reported together with many other drugs. Is this linked to a drug lot, or to combinations already listed in the warnings?"
- **Expected answer:**
  1. Resolve X to its active ingredient (UNII) and every product that contains it.
  2. Co-reported drugs in FAERS: recent window against a baseline, with a disproportionality statistic per pair. The answer states the FAERS release and data cut-off, and the exact dates of both windows.
  3. Each rising pair marked "already in the label" (DailyMed interactions and warnings) or "new". The answer states the DailyMed label set and release it checked, and the label coverage (products of the ingredient with a label checked, out of all its products) before it marks any pair "new". A pair is "new" only against the labels actually checked.
  4. Lot: reports grouped by batch number; recalls and enforcement actions for the products.
  5. Mechanism for each new pair: shared enzyme or target, only where ChEMBL or UniProt supports it. The answer states the mechanism coverage (pairs with a supported mechanism out of all new pairs) and says "no supported mechanism found" for the rest. An inferred mechanism without evidence scores as an invented claim.
  6. Recent publications and case reports on each pair, cited.
- **Must be exact:** UNII, product identifiers, report counts per window, statistic values, label section references, recall numbers, citations.
- **Expected gaps:** the batch number is absent in most reports, so the answer states the coverage (for example "lot present in n of N reports") and says a lot effect cannot be shown or ruled out. FAERS lag (about one quarter). Spontaneous reports are a signal, not proof of cause. Mechanism coverage, with pairs that have no supported mechanism named as such.
- **Sources needed:** openFDA `drug/event` and `drug/enforcement` (not downloaded); DailyMed (downloaded, not loaded); UNII via the DailyMed substance index; ChEMBL and UniProt; pubmed, PMC, europepmc.
- **Origin:** the user, 2026-10-28. Deliberately only partly answerable: it tests honest gap reporting.

### Q2. Adverse events by composition, with connections

- **Persona / domain:** researcher / human drug
- **Question:** "What adverse events are associated with ingredient Y across all products that contain it, and what connects them?"
- **Expected answer:** events reported with Y across every product with Y that the reference store holds (not one label), with the product coverage stated (products mapped to Y, and product names in the reports that could not be mapped), in "reported with" language; for each event with a strong reporting signal, the paths that connect it: Y → target → pathway → event; Y → structurally similar compounds → their events; Y → genetic variants → events in some patients; each path with its evidence.
- **Must be exact:** UNII, target and pathway identifiers, variant identifiers, event terms, counts, citations.
- **Expected gaps:** a FAERS report can list several drugs and several reactions without linking a given drug to a given reaction, and FAERS gives neither event rates nor causality, so the answer says "reported with", states how many reports list other drugs too, and makes no causal or incidence claim unless another cited source supports it; paths with no supporting evidence are not shown as findings; event terms whose hierarchy needs a MedDRA licence are matched by name only (pending the licence decision).
- **Sources needed:** the product universe and ingredient resolution: openFDA `drug/ndc` and DailyMed SPL with its substance (UNII) index; pubchem (loaded), ChEMBL, UniProt, Reactome, ClinVar, openFDA `drug/event`, literature (pubmed loaded; europepmc loaded).
- **Origin:** the user's persona 1, 2026-10-28.

### Q3. Device malfunction cluster (proposed)

- **Persona / domain:** pv-bot / device
- **Question:** "Are recent malfunction reports for device model Z linked to one production lot, and has there been a recall?"
- **Expected answer:** reports for the device by product code and model; grouping by lot where a lot identifier is present, or by serial number only through a manufacturer mapping from serial to lot. Without a lot identifier or such a mapping, the answer reports only the serial clustering and leaves the lot link unresolved (a serial number identifies one device, not a production lot); recalls and their reasons; similar devices with the same failure.
- **Must be exact:** device identifiers, product codes, report counts, recall numbers and classes.
- **Expected gaps:** lot and serial coverage in the reports; reports that do not name the model; UDI presence and validation coverage (the UDI is not in every report and is not validated), with the fallback stated when no UDI is present (match on product code, brand and model name, at a lower confidence).
- **Sources needed:** openFDA `device/event` (MAUDE), `device/recall`, `device/udi` (GUDID): none downloaded or wired yet. No manufacturer serial-to-lot mapping source is in scope: until one is added (with its own validation rule), the answer leaves the lot link unresolved for every report that has a serial number but no lot identifier.
- **Origin:** proposed by Claude, from the user's device scope, 2026-10-28. To be reviewed by the user.

### Q4. Veterinary adverse events for a shared ingredient (proposed)

- **Persona / domain:** researcher / animal
- **Question:** "Ingredient W is used in both human and veterinary products. Do the adverse events reported in animals match those in humans?"
- **Expected answer:** veterinary reports by species for products with W; human reports for the same ingredient; the events common to both and those seen only in one; label warnings on each side.
- **Must be exact:** ingredient identifier, species, counts, label references.
- **Expected gaps:** species coverage; differences in how the two systems code events; the data cut-off of each side (the veterinary data is updated quarterly and can lag receipt by 3-6 months), stated before the event sets are compared.
- **Sources needed:** openFDA `animalandveterinary/event` (not downloaded); DailyMed animal SPL release (downloaded, not loaded) for the veterinary labels; DailyMed human Rx and OTC SPL releases (downloaded, not loaded; the same source as Q1) for the human labels, with the release and label coverage of each side stated; openFDA `drug/event`.
- **Origin:** proposed by Claude, from the user's animal scope, 2026-10-28. To be reviewed by the user.
