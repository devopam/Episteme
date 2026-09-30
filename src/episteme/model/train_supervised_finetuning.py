import argparse

import torch
from datasets import Dataset, load_dataset
from peft import LoraConfig, TaskType, get_peft_model
from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer
from trl import SFTConfig, SFTTrainer

# Optional DeepSpeed import check
try:
    import deepspeed  # noqa: F401  (availability probe)

    HAS_DEEPSPEED = True
except ImportError:
    HAS_DEEPSPEED = False


def format_cot_prompt(question, options, explanation=None, answer=None):
    """Format a multiple-choice question into a Chain-of-Thought template."""
    prompt = f"Question: {question}\nOptions:\n"
    for key, val in options.items():
        if val:
            prompt += f"({key}) {val}\n"

    prompt += "\nReasoning Chain:\n"

    response = ""
    if explanation:
        response += f"{explanation}\n"
    if answer:
        response += f"Therefore, the correct option is ({answer})."

    return {"prompt": prompt, "response": response}


def load_sft_dataset(sample_only=False):
    """Load and format QA SFT datasets (MedMCQA, PubMedQA, Medprompt)."""
    print("=== Loading and Formatting SFT Datasets ===")
    formatted_data = []

    if sample_only:
        print("Sample/Mock mode enabled. Building mock SFT prompts.")
        mock_qa = [
            {
                "question": "Which of the following is a symptom of acute myocarditis?",
                "options": {
                    "A": "Chest pain",
                    "B": "Hair loss",
                    "C": "Hearing loss",
                    "D": "Nail peeling",
                },
                "explanation": (
                    "Acute myocarditis represents inflammation of the heart muscle. Typical "
                    "presentation includes acute chest pain, dyspnea, and electrocardiographic "
                    "abnormalities."
                ),
                "answer": "A",
            },
            {
                "question": "Does compound CHEMBL25 bind acetylcholinesterase?",
                "options": {"A": "Yes", "B": "No", "C": "Maybe", "D": "None of the above"},
                "explanation": (
                    "ChEMBL bioactivity measurements confirm CHEMBL25 has an IC50 of 5.4 nM, "
                    "showing potent inhibition of the Acetylcholinesterase target."
                ),
                "answer": "A",
            },
        ] * 10
        for item in mock_qa:
            formatted = format_cot_prompt(
                item["question"], item["options"], item["explanation"], item["answer"]
            )
            formatted_data.append({"text": f"{formatted['prompt']}{formatted['response']}"})
    else:
        try:
            # Try to load MedMCQA from Hugging Face
            dataset = load_dataset("openlifescienceai/medmcqa", split="train", streaming=True)
            iterator = iter(dataset)
            for _ in range(50):  # Take 50 examples
                try:
                    row = next(iterator)
                    opts = {
                        "A": row.get("opa", ""),
                        "B": row.get("opb", ""),
                        "C": row.get("opc", ""),
                        "D": row.get("opd", ""),
                    }
                    ans_idx = row.get("cop", 1)  # 1-indexed choice
                    ans_map = {1: "A", 2: "B", 3: "C", 4: "D"}
                    ans_char = ans_map.get(ans_idx, "A")

                    formatted = format_cot_prompt(
                        row.get("question", ""),
                        opts,
                        row.get("exp", "No explanation available."),
                        ans_char,
                    )
                    formatted_data.append({"text": f"{formatted['prompt']}{formatted['response']}"})
                except StopIteration:
                    break
        except Exception as e:
            print(f"Failed to load SFT datasets from HF: {e}. Falling back to mock dataset.")
            return load_sft_dataset(sample_only=True)

    print(f"Created {len(formatted_data)} formatted SFT training pairs.")
    return Dataset.from_list(formatted_data)


def main():
    parser = argparse.ArgumentParser(description="Phase 0 Episteme Supervised Fine-Tuning CLI")
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        default="HuggingFaceM4/tiny-random-LlamaForCausalLM",
        help="Path to CPT base checkpoint (supports model-switching)",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="./models/sft_output",
        help="Directory to save SFT checkpoint",
    )
    parser.add_argument(
        "--per_device_train_batch_size", type=int, default=2, help="Batch size per device"
    )
    parser.add_argument(
        "--learning_rate", type=float, default=2e-4, help="SFT learning rate (with LoRA)"
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

    # 2. Load and format dataset
    dataset = load_sft_dataset(sample_only=args.dry_run)

    # 3. Load model
    print(f"Loading causal LM model from: {args.model_name_or_path}")
    if args.dry_run:
        config = AutoConfig.from_pretrained(args.model_name_or_path)
        model = AutoModelForCausalLM.from_config(config)
    else:
        model = AutoModelForCausalLM.from_pretrained(
            args.model_name_or_path,
            torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
            device_map="auto" if torch.cuda.is_available() else None,
        )

    # Ensure pad_token_id is valid to prevent validation errors on save
    if model.config.pad_token_id is None or model.config.pad_token_id < 0:
        model.config.pad_token_id = tokenizer.pad_token_id
    if hasattr(model, "generation_config") and model.generation_config is not None:
        if model.generation_config.pad_token_id is None or model.generation_config.pad_token_id < 0:
            model.generation_config.pad_token_id = tokenizer.pad_token_id

    # 4. Configure LoRA PEFT
    if args.use_lora:
        print("Configuring LoRA PEFT adapter...")
        # Automatically target typical linear layer names for Llama/Qwen/Gemma
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

    # 5. Configure SFTConfig
    deepspeed_config_val = None
    if args.use_deepspeed and HAS_DEEPSPEED:
        print("DeepSpeed module detected! Enabling ZeRO-2 optimization.")
        # Setup basic DS config
        deepspeed_config_val = {
            "zero_optimization": {"stage": 2},
            "fp16": {"enabled": "auto"},
            "gradient_accumulation_steps": "auto",
            "train_batch_size": "auto",
        }

    max_steps = 2 if args.dry_run else -1

    training_args = SFTConfig(
        output_dir=args.output_dir,
        max_steps=max_steps,
        per_device_train_batch_size=args.per_device_train_batch_size,
        learning_rate=args.learning_rate,
        weight_decay=0.01,
        lr_scheduler_type="cosine",
        warmup_steps=0.03,
        logging_steps=1,
        save_steps=100,
        fp16=torch.cuda.is_available(),
        deepspeed=deepspeed_config_val,
        report_to="none",
        dataset_text_field="text",
        max_length=512,
        packing=False,
        use_cpu=not torch.cuda.is_available(),
        gradient_checkpointing=torch.cuda.is_available(),
    )

    # 6. SFT Trainer Setup
    trainer = SFTTrainer(model=model, args=training_args, train_dataset=dataset)

    print("Starting Supervised Fine-Tuning...")
    trainer.train()

    # Save SFT model
    if not args.dry_run or args.output_dir:
        trainer.save_model(args.output_dir)
        tokenizer.save_pretrained(args.output_dir)
        print(f"SFT model checkpoint saved to: {args.output_dir}")

    print("SFT training step complete.")


if __name__ == "__main__":
    main()
