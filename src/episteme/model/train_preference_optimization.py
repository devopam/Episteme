import argparse

import torch
from datasets import Dataset
from peft import LoraConfig, TaskType, get_peft_model
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer
from trl import DPOConfig, DPOTrainer

# Optional DeepSpeed import check
try:
    import deepspeed  # noqa: F401  (availability probe)

    HAS_DEEPSPEED = True
except ImportError:
    HAS_DEEPSPEED = False


def load_preference_dataset(sample_only=False):
    """Load and format preference dataset containing prompt, chosen, and rejected answers."""
    print("=== Loading and Formatting Preference Dataset ===")

    # We define standard DPO formatted pairs
    # Chosen answers are conservative, well-grounded and explain reasoning.
    # Rejected answers are overconfident, incorrect, or hallucinate details.
    mock_data = [
        {
            "prompt": (
                "Question: What is the primary adverse reaction associated with penicillin "
                "use?\nOptions:\n(A) Anaphylaxis\n(B) Ototoxicity\n(C) Nephrotoxicity\n"
                "(D) Neuropathy\n\nReasoning Chain:\n"
            ),
            "chosen": (
                "Penicillin is known to trigger hypersensitivity reactions. The most severe "
                "reaction is Type I IgE-mediated anaphylaxis, which can be life-threatening.\n"
                "Therefore, the correct option is (A)."
            ),
            "rejected": (
                "Penicillin causes severe kidney damage (nephrotoxicity) in almost all "
                "patients and can lead to immediate neuropathy.\n"
                "Therefore, the correct option is (C)."
            ),
        },
        {
            "prompt": (
                "Question: Is compound CHEMBL25 a known inhibitor of target "
                "Acetylcholinesterase?\nOptions:\n(A) Yes\n(B) No\n(C) Maybe\n"
                "(D) None of the above\n\nReasoning Chain:\n"
            ),
            "chosen": (
                "According to bioactivity screens, CHEMBL25 exhibits high binding affinity "
                "with a measured IC50 of 5.4 nM, indicating it functions as an inhibitor.\n"
                "Therefore, the correct option is (A)."
            ),
            "rejected": (
                "There is absolutely no biological evidence connecting CHEMBL25 to "
                "Acetylcholinesterase. It is a non-reactive compound.\n"
                "Therefore, the correct option is (B)."
            ),
        },
    ] * 10

    # In a full run, we would load datasets like ultrafeedback or domain-specific preference pairs.
    # For Phase 0, we can use these structured mock pairs as default.
    return Dataset.from_list(mock_data)


def main():
    parser = argparse.ArgumentParser(
        description="Phase 0 Episteme Preference Optimization (DPO) CLI"
    )
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        default="HuggingFaceM4/tiny-random-LlamaForCausalLM",
        help="Path to SFT model checkpoint (supports model-switching)",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="./models/dpo_output",
        help="Directory to save DPO checkpoint",
    )
    parser.add_argument(
        "--per_device_train_batch_size", type=int, default=2, help="Batch size per device"
    )
    parser.add_argument("--learning_rate", type=float, default=5e-6, help="DPO learning rate")
    parser.add_argument(
        "--beta",
        type=float,
        default=0.1,
        help="DPO beta parameter (temperature for preference margin)",
    )
    parser.add_argument(
        "--use_lora",
        action="store_true",
        default=True,
        help="Enable LoRA adapter fine-tuning (default is True)",
    )
    parser.add_argument(
        "--use_deepspeed", action="store_true", help="Use DeepSpeed configuration if available"
    )
    parser.add_argument(
        "--dry_run",
        action="store_true",
        default=True,
        help=(
            "Run a single training step on a random tiny model to verify execution "
            "(default is True)"
        ),
    )

    args = parser.parse_args()

    # 1. Load tokenizer
    print(f"Loading tokenizer for: {args.model_name_or_path}")
    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # 2. Load dataset
    dataset = load_preference_dataset(sample_only=args.dry_run)

    # 3. Load model and reference model
    print(f"Loading causal LM model from: {args.model_name_or_path}")
    if args.dry_run:
        config = AutoConfig.from_pretrained(args.model_name_or_path)
        model = AutoModelForCausalLM.from_config(config)
        ref_model = None
    else:
        model = AutoModelForCausalLM.from_pretrained(
            args.model_name_or_path,
            torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
            device_map="auto" if torch.cuda.is_available() else None,
        )
        ref_model = None  # DPOTrainer will clone or disable adapters automatically if lora is used

    # Ensure pad_token_id is valid to prevent validation errors on save
    if model.config.pad_token_id is None or model.config.pad_token_id < 0:
        model.config.pad_token_id = tokenizer.pad_token_id
    if hasattr(model, "generation_config") and model.generation_config is not None:
        if model.generation_config.pad_token_id is None or model.generation_config.pad_token_id < 0:
            model.generation_config.pad_token_id = tokenizer.pad_token_id

    # 4. Configure LoRA PEFT
    if args.use_lora:
        print("Configuring LoRA PEFT adapter for DPO...")
        target_modules = ["q_proj", "v_proj", "k_proj", "o_proj"]
        lora_config = LoraConfig(
            r=8,
            lora_alpha=16,
            target_modules=target_modules,
            lora_dropout=0.05,
            bias="none",
            task_type=TaskType.CAUSAL_LM,
        )
        model = get_peft_model(model, lora_config)
        model.print_trainable_parameters()

    # 5. Configure DPOConfig
    deepspeed_config_val = None
    if args.use_deepspeed and HAS_DEEPSPEED:
        print("DeepSpeed module detected! Enabling ZeRO-2 optimization.")
        deepspeed_config_val = {
            "zero_optimization": {"stage": 2},
            "fp16": {"enabled": "auto"},
            "gradient_accumulation_steps": "auto",
            "train_batch_size": "auto",
        }

    max_steps = 2 if args.dry_run else -1

    training_args = DPOConfig(
        output_dir=args.output_dir,
        max_steps=max_steps,
        per_device_train_batch_size=args.per_device_train_batch_size,
        learning_rate=args.learning_rate,
        weight_decay=0.01,
        lr_scheduler_type="cosine",
        warmup_steps=0.1,
        logging_steps=1,
        save_steps=100,
        fp16=torch.cuda.is_available(),
        deepspeed=deepspeed_config_val,
        report_to="none",
        beta=args.beta,
        max_length=512,
        use_cpu=not torch.cuda.is_available(),
        gradient_checkpointing=torch.cuda.is_available(),
    )

    # 6. DPO Trainer Setup
    trainer = DPOTrainer(
        model=model,
        ref_model=ref_model,
        args=training_args,
        train_dataset=dataset,
        processing_class=tokenizer,
    )

    print("Starting Direct Preference Optimization (DPO)...")
    trainer.train()

    # Save DPO model
    if not args.dry_run or args.output_dir:
        trainer.save_model(args.output_dir)
        tokenizer.save_pretrained(args.output_dir)
        print(f"DPO model checkpoint saved to: {args.output_dir}")

    print("DPO training step complete.")


if __name__ == "__main__":
    main()
