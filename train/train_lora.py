"""M7: LoRA fine-tune a small model on one distillation dataset. One command, CPU or GPU.

    uv run --group train --extra router python train/train_lora.py \\
        --data data/distill/banking77-intent --out <models dir>/banking77-intent/v1

On a free Kaggle/Colab GPU the same command uses CUDA automatically. Then convert to GGUF:

    bash scripts/to_gguf.sh <models dir>/banking77-intent/v1

What it does:
- reads train.jsonl from the dataset's LATEST build, in the exact serving format
  (gateway/distill/prompt.py: a short task tag instead of the long system prompt);
- LoRA (r=16) on the attention and MLP projections of the top half of Qwen2.5-0.5B-Instruct's
  layers (`--top-layers`): backpropagation then stops halfway down, which roughly halves the
  backward pass on CPU. Loss only on the answer tokens, fixed seeds;
- merges the adapter into the base weights (plain HF model, ready for GGUF conversion);
- writes train_config.json: base model, dataset hash, hyperparameters, library versions,
  device, wall time and the loss curve, so the run can be reproduced and audited.
"""

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

import peft
import torch
import transformers
from peft import LoraConfig, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer

TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]


def seed_everything(seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def load_dataset(data_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    build = data_dir / (data_dir / "LATEST").read_text(encoding="utf-8").strip()
    manifest = json.loads((build / "manifest.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (build / "train.jsonl").read_text("utf-8").splitlines()]
    return rows, manifest


def encode(tokenizer: Any, row: dict[str, Any], max_len: int) -> tuple[list[int], list[int]]:
    """Token ids and labels; labels are -100 (ignored) everywhere except the answer."""
    messages = [
        {"role": "system", "content": row["system"]},
        {"role": "user", "content": row["input"]},
    ]
    prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
    prompt_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
    answer_ids = tokenizer(row["target"] + "<|im_end|>", add_special_tokens=False)["input_ids"]
    ids = (prompt_ids + answer_ids)[:max_len]
    labels = ([-100] * len(prompt_ids) + answer_ids)[:max_len]
    return ids, labels


def batches(
    encoded: list[tuple[list[int], list[int]]], size: int, pad_id: int, rng: random.Random
) -> list[dict[str, torch.Tensor]]:
    order = list(range(len(encoded)))
    rng.shuffle(order)
    out = []
    for start in range(0, len(order), size):
        chunk = [encoded[i] for i in order[start : start + size]]
        width = max(len(ids) for ids, _ in chunk)
        ids = [ids + [pad_id] * (width - len(ids)) for ids, _ in chunk]
        labels = [lab + [-100] * (width - len(lab)) for _, lab in chunk]
        mask = [[1] * len(i) + [0] * (width - len(i)) for i, _ in chunk]
        out.append(
            {
                "input_ids": torch.tensor(ids),
                "labels": torch.tensor(labels),
                "attention_mask": torch.tensor(mask),
            }
        )
    return out


def portable_tokenizer_config(hf_dir: Path) -> None:
    """transformers 5 writes `extra_special_tokens` as a list; llama.cpp's GGUF converter (on
    transformers 4) expects a dict and crashes. The special tokens themselves live in
    tokenizer.json, so the field can be dropped."""
    path = hf_dir / "tokenizer_config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(config.get("extra_special_tokens"), list):
        del config["extra_special_tokens"]
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True, help="data/distill/<task>")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--base", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--rank", type=int, default=16)
    parser.add_argument("--max-len", type=int, default=128)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--top-layers", type=float, default=0.5, help="fraction of layers (from the top) to adapt"
    )
    args = parser.parse_args()

    seed_everything(args.seed)
    rows, manifest = load_dataset(args.data)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    print(f"{len(rows)} training rows, dataset {manifest['data_hash'][:12]}, device {device}")

    tokenizer = AutoTokenizer.from_pretrained(args.base)
    model = AutoModelForCausalLM.from_pretrained(args.base, dtype=dtype).to(device)
    n_layers = int(model.config.num_hidden_layers)
    adapted = list(range(n_layers - max(1, round(n_layers * args.top_layers)), n_layers))
    lora = LoraConfig(
        r=args.rank,
        lora_alpha=2 * args.rank,
        lora_dropout=0.05,
        target_modules=TARGETS,
        layers_to_transform=adapted,
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    encoded = [encode(tokenizer, r, args.max_len) for r in rows]
    pad_id = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else 0
    rng = random.Random(args.seed)
    steps_per_epoch = (len(encoded) + args.batch - 1) // args.batch
    total_steps = steps_per_epoch * args.epochs
    optimizer = torch.optim.AdamW((p for p in model.parameters() if p.requires_grad), lr=args.lr)
    schedule = torch.optim.lr_scheduler.LambdaLR(
        optimizer, lambda step: max(0.0, 1 - step / total_steps)
    )

    model.train()
    losses: list[dict[str, float]] = []
    started = time.time()
    step = 0
    for epoch in range(args.epochs):
        for batch in batches(encoded, args.batch, pad_id, rng):
            batch = {k: v.to(device) for k, v in batch.items()}
            loss = model(**batch).loss
            loss.backward()
            optimizer.step()
            schedule.step()
            optimizer.zero_grad()
            step += 1
            if step % 10 == 0 or step == total_steps:
                losses.append({"step": step, "epoch": epoch, "loss": round(loss.item(), 4)})
                rate = step / (time.time() - started)
                print(f"step {step}/{total_steps} loss {loss.item():.4f} ({rate:.2f} steps/s)")
                sys.stdout.flush()

    merged = model.merge_and_unload()
    hf_dir = args.out / "hf"
    merged.save_pretrained(hf_dir)
    tokenizer.save_pretrained(hf_dir)
    portable_tokenizer_config(hf_dir)
    config = {
        "task": manifest["task"],
        "dataset_hash": manifest["data_hash"],
        "dataset_counts": manifest["counts"],
        "simulated": manifest["simulated"],
        "base_model": args.base,
        "method": "LoRA, merged",
        "lora": {
            "r": args.rank,
            "alpha": 2 * args.rank,
            "dropout": 0.05,
            "targets": TARGETS,
            "layers": [adapted[0], adapted[-1]],
        },
        "epochs": args.epochs,
        "batch_size": args.batch,
        "learning_rate": args.lr,
        "max_len": args.max_len,
        "seed": args.seed,
        "device": device,
        "train_rows": len(rows),
        "steps": total_steps,
        "wall_seconds": round(time.time() - started, 1),
        "loss_curve": losses,
        "versions": {
            "torch": torch.__version__,
            "transformers": transformers.__version__,
            "peft": peft.__version__,
        },
    }
    (args.out / "train_config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"saved merged model to {hf_dir} in {config['wall_seconds']} s")


if __name__ == "__main__":
    main()
