# Healthcare / Pharma Domain LLM – Strategy Summary

**Last updated:** August 2026

## Core Strategy

- Build a **generic open LLM** trained only on open, relatively stable information.
- Keep all proprietary, company-specific, customer-specific, and frequently changing information (MedDRA, UCUM, labels, SOPs, etc.) behind a **RAG layer**.
- Support multi-tenancy via RBAC on the RAG / knowledge layer.
- Use **Model Context Protocol (MCP)** as the preferred way to expose tools and knowledge sources.

## Two Buckets

### Bucket 1 – LLM Training Data (Open / Generic)
- Stable biomedical & clinical foundations
- Domain language, style, and terminology awareness
- Reasoning patterns
- Open literature (PubMed, PMC Commercial OA, etc.)
- Clinical guidelines (open/redistributable)
- Safety, grounding, and refusal behavior

### Bucket 2 – RAG Layer (Dynamic / Proprietary / Versioned)
- MedDRA, UCUM, ICD, SNOMED, etc. (versioned)
- Product labels / SmPCs
- Company SOPs and internal documents
- Customer-specific data
- Latest literature and safety signals

## Phase 0 Focus
Build the generic open medical LLM only.
