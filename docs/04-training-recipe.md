# Training Recipe Outline – Phase 0

## Base Model Candidates (Resource-Aware)
- Primary starting point: 8B-class models (Llama-3.1/3.3-8B, Qwen2.5/Qwen3-7B/14B, Gemma-2/3 9B–12B)
- Secondary: Slightly larger models if resources allow
- Fully open options (Apertus, OLMo, etc.) if auditability is critical

## Three-Stage Recipe

### Stage 1 – Continual Pre-training
- Data mix: 70–85% biomedical literature + guidelines, 5–15% general replay
- Focus on quality filtering and decontamination
- Lower learning rate + warmup

### Stage 2 – Supervised Fine-Tuning (SFT)
- Medical QA, multi-turn dialogues, reasoning chains
- Explicit grounding and citation behavior
- Safety / hedging / refusal patterns

### Stage 3 – Preference Optimization
- DPO / KTO or similar
- Reward grounding, correct use of context, conservative language

## Evaluation Focus
- Standard medical benchmarks (MedQA, MedMCQA, PubMedQA, etc.)
- Grounding / RAG-readiness tests
- Safety and version-awareness probes
- General capability retention
