# Episteme Medical LLM

Phase 0 of the Episteme Medical LLM: data gathering pipelines and model fine-tuning/evaluation loops for a generic, open medical LLM trained on stable foundational knowledge.

## Background

The Episteme Medical LLM adopts a bimodal data strategy.
Stable, foundational scientific literature and chemical/genomic databases are baked directly into the model's parametric weights (Phase 0).
Proprietary, customer-specific, or highly dynamic clinical guidelines and product labels are strictly reserved for Retrieval-Augmented Generation (RAG) at run-time (Phase 1).

This repository contains the complete pipeline for Stage 0, split into two parallel components:
1. **Data Sourcing & Curation**: Parallel downloading, natural language serialization of chemical/genomic structures, MinHash LSH deduplication, and test split decontamination.
2. **Model Fine-Tuning & Evaluation**: Continual Pre-training (CPT), Supervised Fine-Tuning (SFT) using LoRA, Direct Preference Optimization (DPO), and benchmark scoring (MedMCQA, PubMedQA).

## Install

First, set up a virtual environment and install the package in editable mode:

```bash
# Create virtual environment
python -m venv .venv

# Activate virtual environment (Windows PowerShell)
.venv\Scripts\Activate.ps1

# Upgrade pip
python -m pip install --upgrade pip

# Install in editable mode (core only: python-dotenv + tqdm; feature deps are in optional groups below)
pip install -e .
```

To install optional feature groups:
- Data pipeline: `pip install -e ".[data]"` — required by the data-acquisition scripts (e.g. `scripts/download_pmc_oa_comm.sh`), which import `requests`/`pandas`/etc.
- Model training: `pip install -e ".[model]"`
- Development & tests: `pip install -e ".[dev]"`

## Usage

### 1. Data Pipeline

The data pipeline is driven by per-source scripts under `scripts/data/`
and documented end-to-end (first-time and incremental) in
[`docs/10-data-sources-runbook.md`](docs/10-data-sources-runbook.md).
Corpus-level curation lives in `episteme.data.curate`
(`serialize_structured_sources`, `deduplicate_corpus`, `decontaminate_benchmarks`).
PostgreSQL setup (roles, schema, extensions) is `scripts/data/db/` —
run `install_extensions.sh` then `init_database.sh` against `.env`'s `PG*` vars.

### 2. Model Pipeline

The model pipeline supports model-switching (config-driven base checkpoints), parameter-efficient fine-tuning (LoRA), and standard benchmarks scoring.

```bash
# Stage 1: Continual Pre-training (CPT)
python -m episteme.model.train_continual_pretraining --model_name_or_path HuggingFaceM4/tiny-random-LlamaForCausalLM --medical_data_path ./data/pretrain_corpus_clean.jsonl --output_dir ./models/cpt_output --dry_run

# Stage 2: Supervised Fine-Tuning (SFT) with LoRA
python -m episteme.model.train_supervised_finetuning --model_name_or_path ./models/cpt_output --output_dir ./models/sft_output --dry_run

# Stage 3: Direct Preference Optimization (DPO) with LoRA
python -m episteme.model.train_preference_optimization --model_name_or_path ./models/sft_output --output_dir ./models/dpo_output --dry_run

# Evaluation: Score accuracy on MedMCQA and PubMedQA
python -m episteme.model.evaluate_benchmarks --model_name_or_path ./models/dpo_output --output_file ./data/eval_report.json --sample_only
```

### 3. Running Automated Tests

Verify that all modules and integration dry-runs compile and pass correctly:

```bash
python -m pytest
```

## Contributing

For coding conventions and style rules, please consult our `docs` and standard repository guidelines. Make sure to run the unit test suite before proposing a pull request.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
