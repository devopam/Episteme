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

# Install in editable mode (installs all dependencies declared in pyproject.toml)
pip install -e .
```

## Usage

### 1. Data Pipeline

The data pipeline gathers literature, serializes structured databases into readable prose, and handles deduplication/decontamination.

```bash
# Step 1: Download raw datasets (run in sample mode to check setup)
python -m episteme.data_pipeline.download --dataset all --output_dir ./data --sample_only

# Step 2: Preprocess and serialize raw data into standard JSONL corpus
python -m episteme.data_pipeline.preprocess --input_dir ./data --output_file ./data/pretrain_corpus.jsonl

# Step 3: Run near-deduplication using MinHash LSH
python -m episteme.data_pipeline.dedup --input_file ./data/pretrain_corpus.jsonl --output_file ./data/pretrain_corpus_dedup.jsonl --threshold 0.8

# Step 4: Decontaminate training data against test benchmarks
python -m episteme.data_pipeline.decontaminate --input_file ./data/pretrain_corpus_dedup.jsonl --output_file ./data/pretrain_corpus_clean.jsonl --sample_only
```

### 2. Model Pipeline

The model pipeline supports model-switching (config-driven base checkpoints), parameter-efficient fine-tuning (LoRA), and standard benchmarks scoring.

```bash
# Stage 1: Continual Pre-training (CPT)
python -m episteme.model_pipeline.train_cpt.py --model_name_or_path HuggingFaceM4/tiny-random-LlamaForCausalLM --medical_data_path ./data/pretrain_corpus_clean.jsonl --output_dir ./models/cpt_output --dry_run

# Stage 2: Supervised Fine-Tuning (SFT) with LoRA
python -m episteme.model_pipeline.train_sft.py --model_name_or_path ./models/cpt_output --output_dir ./models/sft_output --dry_run

# Stage 3: Direct Preference Optimization (DPO) with LoRA
python -m episteme.model_pipeline.train_preference.py --model_name_or_path ./models/sft_output --output_dir ./models/dpo_output --dry_run

# Evaluation: Score accuracy on MedMCQA and PubMedQA
python -m episteme.model_pipeline.evaluate --model_name_or_path ./models/dpo_output --output_file ./data/eval_report.json --sample_only
```

### 3. Running Automated Tests

Verify that all modules and integration dry-runs compile and pass correctly:

```bash
python -m pytest
```

## Contributing

For coding conventions and style rules, please consult our `docs` and standard repository guidelines. Make sure to run the unit test suite before proposing a pull request.

## License

This project is licensed under the Apache-2.0 License.
