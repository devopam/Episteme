import os
import sys
import argparse
import json
import torch
from datasets import load_dataset
from transformers import AutoModelForCausalLM, AutoTokenizer, AutoConfig
from tqdm import tqdm
import re


def extract_answer_mcq(text):
    """Extract multiple-choice option (A, B, C, D) from generated response text."""
    # Look for "correct option is (A)" or "option (A)" or "answer is A" etc.
    matches = re.findall(r'(?:correct option is|option|answer is|correct choice is|choice)\s*\(?([a-d])\)?', text.lower())
    if matches:
        return matches[-1].upper()
    # Simple regex fallback to find a lone character at the end of the text
    matches = re.findall(r'\b([a-d])\b', text.lower())
    if matches:
        return matches[-1].upper()
    return "A" # Default fallback


def extract_answer_yesno(text):
    """Extract binary response (yes, no, maybe) from generated response text."""
    text_lower = text.lower()
    if "yes" in text_lower:
        return "yes"
    elif "no" in text_lower:
        return "no"
    elif "maybe" in text_lower or "uncertain" in text_lower:
        return "maybe"
    return "maybe" # Default fallback for uncertainty


def run_evaluation(model_name_or_path, output_report_path, sample_only=False):
    """Load model and run evaluation on target benchmarks."""
    print(f"=== Running Evaluation on {model_name_or_path} ===")
    
    # Initialize tokenizer and model
    tokenizer = AutoTokenizer.from_pretrained(model_name_or_path)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
        
    print("Loading model...")
    if sample_only:
        # Load tiny model config and create mock weights to avoid downloading full weights
        config = AutoConfig.from_pretrained(model_name_or_path)
        model = AutoModelForCausalLM.from_config(config)
    else:
        model = AutoModelForCausalLM.from_pretrained(
            model_name_or_path,
            torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
            device_map="auto" if torch.cuda.is_available() else None
        )
    model.eval()
    
    # Ensure pad_token_id is valid
    if model.config.pad_token_id is None or model.config.pad_token_id < 0:
        model.config.pad_token_id = tokenizer.pad_token_id
    
    # Datasets to evaluate
    results = {}
    
    # 1. MedMCQA Evaluation
    print("Evaluating on MedMCQA...")
    medmcqa_correct = 0
    medmcqa_total = 0
    
    if sample_only:
        # Generate mock test items
        medmcqa_test_set = [
            {
                "question": "Which of the following is a primary symptom of myocarditis?",
                "opa": "Chest pain", "opb": "Hair loss", "opc": "Hearing loss", "opd": "Nail peeling",
                "cop": 1 # A
            },
            {
                "question": "What is the key clinical sign of acute anemia?",
                "opa": "Sore throat", "opb": "Pallor", "opc": "Pruritus", "opd": "Fever",
                "cop": 2 # B
            }
        ]
    else:
        try:
            medmcqa_test_set = load_dataset("openlifescienceai/medmcqa", split="test")
            # Slice first 50 for quick evaluation
            medmcqa_test_set = list(medmcqa_test_set)[:50]
        except Exception as e:
            print(f"Could not load MedMCQA dataset: {e}. Skipping full dataset evaluation.")
            medmcqa_test_set = []

    for item in tqdm(medmcqa_test_set, desc="MedMCQA"):
        question = item.get("question", "")
        options = {
            "A": item.get("opa", ""),
            "B": item.get("opb", ""),
            "C": item.get("opc", ""),
            "D": item.get("opd", "")
        }
        ans_idx = item.get("cop", 1)
        ans_map = {1: "A", 2: "B", 3: "C", 4: "D"}
        correct_ans = ans_map.get(ans_idx, "A")
        
        # Build prompt
        prompt = f"Question: {question}\nOptions:\n"
        for key, val in options.items():
            if val:
                prompt += f"({key}) {val}\n"
        prompt += "\nReasoning Chain:\n"
        
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
        with torch.no_grad():
            outputs = model.generate(**inputs, max_new_tokens=100)
            
        generated_text = tokenizer.decode(outputs[0][inputs.input_ids.shape[1]:], skip_special_tokens=True)
        pred_ans = extract_answer_mcq(generated_text)
        
        medmcqa_total += 1
        if pred_ans == correct_ans:
            medmcqa_correct += 1
            
    if medmcqa_total > 0:
        medmcqa_acc = (medmcqa_correct / medmcqa_total) * 100
        results["medmcqa"] = {
            "total": medmcqa_total,
            "correct": medmcqa_correct,
            "accuracy_percent": medmcqa_acc
        }
        print(f"MedMCQA Accuracy: {medmcqa_acc:.2f}% ({medmcqa_correct}/{medmcqa_total})")

    # 2. PubMedQA Evaluation
    print("Evaluating on PubMedQA...")
    pubmedqa_correct = 0
    pubmedqa_total = 0
    
    if sample_only:
        pubmedqa_test_set = [
            {
                "question": "Does severe hyperkalemia precipitate ventricular fibrillation?",
                "context": "Severe hyperkalemia is well documented to alter cardiac electrical stability, leading to arrhythmias including ventricular fibrillation.",
                "final_decision": "yes"
            },
            {
                "question": "Is mild dehydration treated with immediate hemodialysis?",
                "context": "Mild dehydration is managed conservatively with oral rehydration therapy. Invasive hemodialysis is not indicated.",
                "final_decision": "no"
            }
        ]
    else:
        try:
            # PubMedQA pqa_labeled split
            pubmedqa_test_set = load_dataset("qiaojin/pubmedqa", "pqa_labeled", split="train")
            # Use last 50 as evaluation split
            pubmedqa_test_set = list(pubmedqa_test_set)[-50:]
        except Exception as e:
            print(f"Could not load PubMedQA dataset: {e}. Skipping full dataset evaluation.")
            pubmedqa_test_set = []

    for item in tqdm(pubmedqa_test_set, desc="PubMedQA"):
        question = item.get("question", "")
        # Context can be a list or dict of passages
        context_data = item.get("context", "")
        if isinstance(context_data, dict):
            context = " ".join(context_data.get("contexts", []))
        elif isinstance(context_data, list):
            context = " ".join(context_data)
        else:
            context = str(context_data)
            
        correct_ans = item.get("final_decision", "maybe").lower()
        
        # Build prompt
        prompt = f"Context: {context}\nQuestion: {question}\nAnswer yes, no, or maybe. Reasoning:\n"
        
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
        with torch.no_grad():
            outputs = model.generate(**inputs, max_new_tokens=100)
            
        generated_text = tokenizer.decode(outputs[0][inputs.input_ids.shape[1]:], skip_special_tokens=True)
        pred_ans = extract_answer_yesno(generated_text)
        
        pubmedqa_total += 1
        if pred_ans == correct_ans:
            pubmedqa_correct += 1
            
    if pubmedqa_total > 0:
        pubmed_acc = (pubmedqa_correct / pubmedqa_total) * 100
        results["pubmedqa"] = {
            "total": pubmedqa_total,
            "correct": pubmedqa_correct,
            "accuracy_percent": pubmed_acc
        }
        print(f"PubMedQA Accuracy: {pubmed_acc:.2f}% ({pubmedqa_correct}/{pubmedqa_total})")

    # Save output report
    os.makedirs(os.path.dirname(output_report_path), exist_ok=True)
    with open(output_report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"Evaluation report written to {output_report_path}")
    
    return results


def main():
    parser = argparse.ArgumentParser(description="Phase 0 Episteme Benchmark Evaluator CLI")
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        default="HuggingFaceM4/tiny-random-LlamaForCausalLM",
        help="Path to trained model checkpoint"
    )
    parser.add_argument(
        "--output_file",
        type=str,
        default="./data/eval_report.json",
        help="Path to write the JSON evaluation report"
    )
    parser.add_argument(
        "--sample_only",
        action="store_true",
        default=True,
        help="Run evaluation on sample/mock questions (default is True to save time)"
    )

    args = parser.parse_args()
    run_evaluation(
        args.model_name_or_path,
        args.output_file,
        args.sample_only
    )


if __name__ == "__main__":
    main()
