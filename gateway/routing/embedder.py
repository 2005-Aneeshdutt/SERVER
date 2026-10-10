"""Prompt embeddings for the router (BAAI/bge-small-en-v1.5 on CPU).

The *same* class embeds prompts for training (eval/) and at request time (gateway), so there is
no train/serve skew: identical truncation, int8 quantization and normalization.

Measured on the dev laptop (Linux container, 4 threads): fp32 ~31 ms short / ~72 ms long;
int8 dynamic quantization ~18 / ~48 ms with cosine 0.98 to fp32. int8 is what we serve.
"""

import threading
from collections.abc import Sequence
from typing import Any, Protocol

MODEL_NAME = "BAAI/bge-small-en-v1.5"
MAX_TOKENS = 128  # routing signal is in the start of the prompt; caps latency on long inputs


class Embedder(Protocol):
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Blocking (CPU-bound). Call from a worker thread, never the event loop."""
        ...


class BgeEmbedder:
    def __init__(
        self, model_name: str = MODEL_NAME, max_tokens: int = MAX_TOKENS, threads: int = 2
    ) -> None:
        # Imported lazily: torch/sentence-transformers are an optional extra ("router").
        import torch as torch_module
        from sentence_transformers import SentenceTransformer

        torch: Any = torch_module  # torch's stubs are partial; treat it as dynamic here
        torch.set_num_threads(threads)
        st: Any = SentenceTransformer(model_name, device="cpu")
        self._torch = torch
        self._tokenizer = st.tokenizer
        self._model = torch.ao.quantization.quantize_dynamic(
            st[0].auto_model.eval(), {torch.nn.Linear}, dtype=torch.qint8
        )
        self._max_tokens = max_tokens
        self._lock = threading.Lock()  # one forward pass at a time; torch threads do the rest
        self.dim = int(st.get_sentence_embedding_dimension() or 384)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        torch = self._torch
        with self._lock, torch.inference_mode():
            batch = self._tokenizer(
                list(texts),
                truncation=True,
                max_length=self._max_tokens,
                padding=True,
                return_tensors="pt",
            )
            cls = self._model(**batch).last_hidden_state[:, 0]  # bge uses the [CLS] vector
            vectors = torch.nn.functional.normalize(cls, dim=-1)
            return [list(map(float, row)) for row in vectors.tolist()]
