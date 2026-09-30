import json
import os
import tempfile
from unittest.mock import patch

from episteme.model import (
    evaluate_benchmarks as evaluate,
)
from episteme.model import (
    train_continual_pretraining as train_cpt,
)
from episteme.model import (
    train_preference_optimization as train_preference,
)
from episteme.model import (
    train_supervised_finetuning as train_sft,
)


def test_cpt_dry_run():
    """Test CPT pipeline dry-run compilation and execution."""
    with tempfile.TemporaryDirectory() as temp_dir:
        test_args = [
            "train_cpt.py",
            "--model_name_or_path",
            "HuggingFaceM4/tiny-random-LlamaForCausalLM",
            "--output_dir",
            temp_dir,
            "--per_device_train_batch_size",
            "1",
            "--dry_run",
        ]
        with patch("sys.argv", test_args):
            train_cpt.main()

        # Verify output config and checkpoint files are generated
        assert os.path.exists(os.path.join(temp_dir, "config.json"))
        assert os.path.exists(os.path.join(temp_dir, "tokenizer.json"))


def test_sft_dry_run():
    """Test SFT pipeline dry-run compilation and execution."""
    with tempfile.TemporaryDirectory() as temp_dir:
        test_args = [
            "train_sft.py",
            "--model_name_or_path",
            "HuggingFaceM4/tiny-random-LlamaForCausalLM",
            "--output_dir",
            temp_dir,
            "--per_device_train_batch_size",
            "1",
            "--dry_run",
        ]
        with patch("sys.argv", test_args):
            train_sft.main()

        assert os.path.exists(os.path.join(temp_dir, "adapter_config.json"))


def test_preference_dry_run():
    """Test Preference DPO pipeline dry-run compilation and execution."""
    with tempfile.TemporaryDirectory() as temp_dir:
        test_args = [
            "train_preference.py",
            "--model_name_or_path",
            "HuggingFaceM4/tiny-random-LlamaForCausalLM",
            "--output_dir",
            temp_dir,
            "--per_device_train_batch_size",
            "1",
            "--dry_run",
        ]
        with patch("sys.argv", test_args):
            train_preference.main()

        assert os.path.exists(os.path.join(temp_dir, "adapter_config.json"))


def test_evaluate_dry_run():
    """Test evaluation report generator dry-run."""
    with tempfile.TemporaryDirectory() as temp_dir:
        report_path = os.path.join(temp_dir, "eval_report.json")
        test_args = [
            "evaluate.py",
            "--model_name_or_path",
            "HuggingFaceM4/tiny-random-LlamaForCausalLM",
            "--output_file",
            report_path,
            "--sample_only",
        ]
        with patch("sys.argv", test_args):
            evaluate.main()

        assert os.path.exists(report_path)
        with open(report_path) as f:
            report_data = json.load(f)

        assert "medmcqa" in report_data
        assert "pubmedqa" in report_data
        assert report_data["medmcqa"]["total"] == 2
        assert report_data["pubmedqa"]["total"] == 2
