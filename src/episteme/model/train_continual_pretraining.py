import os
import sys
import argparse
import json
import torch
from datasets import Dataset, load_dataset
from transformers import (
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
    DataCollatorForLanguageModeling
)

# Optional DeepSpeed import check
try:
    import deepspeed
    HAS_DEEPSPEED = True
except ImportError:
    HAS_DEEPSPEED = False


def load_mixed_data(medical_jsonl, replay_dataset_name=None, medical_ratio=0.8, sample_only=False):
    """Load and mix medical data with general replay data."""
    print(f"=== Loading and mixing datasets (Medical ratio: {medical_ratio}) ===")
    
    # 1. Load medical data
    medical_texts = []
    if os.path.exists(medical_jsonl):
        with open(medical_jsonl, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    medical_texts.append(rec.get("text", ""))
    
    # If no data exists or sample mode is active, generate some mock data
    if not medical_texts or sample_only:
        medical_texts = [
            "Cardiovascular disease accounts for a large percentage of clinical outcomes globally.",
            "The chemical compound CHEMBL25 is a known inhibitor of target Acetylcholinesterase.",
            "Protein HBB_HUMAN sequence participates in oxygen transport within erythrocytes."
        ] * 10
        
    print(f"Loaded {len(medical_texts)} medical texts.")
    
    # 2. Load general replay data
    replay_texts = []
    if replay_dataset_name and not sample_only:
        try:
            # Try to load a tiny slice of an open general replay corpus like fineweb-edu
            dataset = load_dataset(replay_dataset_name, split="train", streaming=True)
            iterator = iter(dataset)
            for _ in range(int(len(medical_texts) * (1 - medical_ratio) / medical_ratio) + 1):
                try:
                    row = next(iterator)
                    replay_texts.append(row.get("text", ""))
                except StopIteration:
                    break
        except Exception as e:
            print(f"Could not load general replay dataset {replay_dataset_name}: {e}. Using mock replay data.")
            
    if not replay_texts:
        # Mock replay data
        replay_texts = [
            "The history of science dates back to ancient civilizations.",
            "Mathematics is the study of numbers, structures, and spaces.",
            "Photosynthesis is a process used by plants to convert light energy into chemical energy."
        ] * int(len(medical_texts) * (1 - medical_ratio) / medical_ratio + 1)
        
    print(f"Generated {len(replay_texts)} general replay texts.")
    
    # Mix datasets according to ratio
    combined = []
    med_idx = 0
    rep_idx = 0
    
    # Mix
    while med_idx < len(medical_texts) or rep_idx < len(replay_texts):
        # We try to keep the ratio stable
        current_ratio = len([x for x in combined if x["source"] == "medical"]) / max(1, len(combined))
        if current_ratio < medical_ratio and med_idx < len(medical_texts):
            combined.append({"text": medical_texts[med_idx], "source": "medical"})
            med_idx += 1
        elif rep_idx < len(replay_texts):
            combined.append({"text": replay_texts[rep_idx], "source": "general_replay"})
            rep_idx += 1
        else:
            combined.append({"text": medical_texts[med_idx], "source": "medical"})
            med_idx += 1
            
    print(f"Mixed dataset contains {len(combined)} samples.")
    return Dataset.from_list(combined)


def tokenize_function(examples, tokenizer, max_length=512):
    """Tokenize documents and chunk them into fixed-size blocks."""
    # Tokenize texts
    tokenized = tokenizer(examples["text"], truncation=True, max_length=max_length)
    return tokenized


def get_deepspeed_config():
    """Return a standard DeepSpeed ZeRO-2 configuration."""
    return {
        "fp16": {
            "enabled": "auto",
            "loss_scale": 0,
            "loss_scale_window": 1000,
            "initial_scale_power": 16,
            "hysteresis": 2,
            "min_loss_scale": 1
        },
        "zero_optimization": {
            "stage": 2,
            "allgather_partitions": True,
            "allgather_bucket_size": 2e8,
            "overlap_comm": True,
            "reduce_scatter": True,
            "reduce_bucket_size": 2e8,
            "contiguous_gradients": True
        },
        "gradient_accumulation_steps": "auto",
        "gradient_clipping": "auto",
        "train_batch_size": "auto",
        "train_micro_batch_size_per_gpu": "auto"
    }


def main():
    parser = argparse.ArgumentParser(description="Phase 0 Episteme Continual Pre-training Script")
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        default="HuggingFaceM4/tiny-random-LlamaForCausalLM",
        help="Model path or Hugging Face model identifier (supports model-switching)"
    )
    parser.add_argument(
        "--medical_data_path",
        type=str,
        default="./data/pretrain_corpus_clean.jsonl",
        help="Path to clean decontaminated medical JSONL file"
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="./models/cpt_output",
        help="Directory to save checkpoints"
    )
    parser.add_argument(
        "--medical_ratio",
        type=float,
        default=0.8,
        help="Ratio of medical tokens to general replay tokens"
    )
    parser.add_argument(
        "--max_length",
        type=int,
        default=512,
        help="Maximum sequence length"
    )
    parser.add_argument(
        "--num_train_epochs",
        type=int,
        default=1,
        help="Number of training epochs"
    )
    parser.add_argument(
        "--per_device_train_batch_size",
        type=int,
        default=2,
        help="Batch size per device"
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=2e-5,
        help="CPT learning rate"
    )
    parser.add_argument(
        "--use_deepspeed",
        action="store_true",
        help="Use DeepSpeed optimization if available"
    )
    parser.add_argument(
        "--dry_run",
        action="store_true",
        default=True,
        help="Run a single training step on a random tiny model to verify execution (default is True)"
    )

    args = parser.parse_args()
    
    # 1. Load tokenizer and configuration
    print(f"Loading model config and tokenizer for: {args.model_name_or_path}")
    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
        
    # 2. Load dataset
    dataset = load_mixed_data(
        args.medical_data_path,
        replay_dataset_name="HuggingFaceFW/fineweb-edu",
        medical_ratio=args.medical_ratio,
        sample_only=args.dry_run
    )
    
    # Tokenize
    print("Tokenizing dataset...")
    tokenized_dataset = dataset.map(
        lambda x: tokenize_function(x, tokenizer, args.max_length),
        batched=True,
        remove_columns=dataset.column_names
    )
    
    # 3. Load model
    print(f"Loading causal LM model from: {args.model_name_or_path}")
    if args.dry_run:
        # Load tiny model configuration for quick verify
        config = AutoConfig.from_pretrained(args.model_name_or_path)
        model = AutoModelForCausalLM.from_config(config)
    else:
        model = AutoModelForCausalLM.from_pretrained(args.model_name_or_path)
        
    # Ensure pad_token_id is valid to prevent validation errors on save
    if model.config.pad_token_id is None or model.config.pad_token_id < 0:
        model.config.pad_token_id = tokenizer.pad_token_id
    if hasattr(model, "generation_config") and model.generation_config is not None:
        if model.generation_config.pad_token_id is None or model.generation_config.pad_token_id < 0:
            model.generation_config.pad_token_id = tokenizer.pad_token_id
        
    # 4. Configure TrainingArguments
    deepspeed_config_val = None
    if args.use_deepspeed:
        if HAS_DEEPSPEED:
            print("DeepSpeed module detected! Enabling ZeRO-2 optimization.")
            deepspeed_config_val = get_deepspeed_config()
        else:
            print("WARNING: DeepSpeed is not installed on this system. Falling back to standard PyTorch training.")
            
    # Set step sizes for dry run
    max_steps = 2 if args.dry_run else -1
    epochs = 1 if args.dry_run else args.num_train_epochs
    
    training_args = TrainingArguments(
        output_dir=args.output_dir,
        num_train_epochs=epochs,
        max_steps=max_steps,
        per_device_train_batch_size=args.per_device_train_batch_size,
        learning_rate=args.learning_rate,
        weight_decay=0.01,
        lr_scheduler_type="cosine",
        warmup_steps=0.1,
        logging_steps=1,
        save_steps=100,
        fp16=torch.cuda.is_available(), # Use FP16 if CUDA is available
        deepspeed=deepspeed_config_val,
        report_to="none" # Disable logging callbacks for setup simplicity
    )
    
    data_collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False)
    
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized_dataset,
        data_collator=data_collator,
    )
    
    print("Starting Continual Pre-training...")
    trainer.train()
    
    # Save base model
    if not args.dry_run or args.output_dir:
        trainer.save_model(args.output_dir)
        tokenizer.save_pretrained(args.output_dir)
        print(f"CPT model checkpoint saved to: {args.output_dir}")
        
    print("CPT training step complete.")


if __name__ == "__main__":
    main()
