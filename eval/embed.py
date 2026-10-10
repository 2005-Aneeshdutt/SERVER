"""Embed prompts with the gateway's own embedder (no train/serve skew). Cached on disk."""

from collections.abc import Sequence

from eval import jsonl
from eval.config import EMBEDDINGS_FILE
from eval.dataset import Example
from gateway.routing.embedder import MAX_TOKENS, MODEL_NAME, BgeEmbedder

BATCH = 32


def embed_examples(examples: Sequence[Example]) -> dict[str, list[float]]:
    cache_tag = f"{MODEL_NAME}|{MAX_TOKENS}|int8"
    cached = {r["id"]: r["v"] for r in jsonl.read(EMBEDDINGS_FILE) if r.get("tag") == cache_tag}
    todo = [e for e in examples if e.id not in cached]
    if todo:
        print(f"embedding {len(todo)} prompts ({len(cached)} cached)", flush=True)
        embedder = BgeEmbedder(threads=4)
        for start in range(0, len(todo), BATCH):
            batch = todo[start : start + BATCH]
            for example, vector in zip(
                batch, embedder.embed([e.prompt for e in batch]), strict=True
            ):
                cached[example.id] = vector
        jsonl.write(
            EMBEDDINGS_FILE,
            ({"id": k, "tag": cache_tag, "v": v} for k, v in sorted(cached.items())),
        )
    return cached
