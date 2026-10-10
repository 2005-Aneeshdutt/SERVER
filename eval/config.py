"""Routing evaluation settings. Changing these invalidates cached generations/judgments."""

from decimal import Decimal
from pathlib import Path

from gateway.routing.registry import ModelRegistry, ModelSpec

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(__file__).parent / "data"
PROMPTS_FILE = DATA_DIR / "prompts.jsonl"
GENERATIONS_FILE = DATA_DIR / "generations.jsonl"
JUDGMENTS_FILE = DATA_DIR / "judgments.jsonl"
EMBEDDINGS_FILE = DATA_DIR / "embeddings.jsonl"
RESULTS_FILE = Path(__file__).parent / "RESULTS.md"
CHART_FILE = Path(__file__).parent / "cost_quality.svg"
ROUTER_ARTIFACT = ROOT / "gateway" / "routing" / "router_model.json"

# Apache-2.0 dataset of real user prompts from Chatbot Arena.
DATASET = "lmarena-ai/arena-human-preference-55k"
N_PROMPTS = 2000
SEED = 7
MIN_CHARS, MAX_CHARS = 15, 2000
MAX_ANSWER_TOKENS = 512

# Same model family, two sizes: the comparison isolates "does this prompt need the big model".
CHEAP_ALIAS = "or-llama-8b"
STRONG_ALIAS = "or-llama-70b"
# A different model family judges, which reduces self-preference bias.
JUDGE = ModelSpec(
    alias="judge",
    provider="openrouter",
    upstream_model="google/gemini-2.5-flash",
    input_price_per_mtok=Decimal("0.30"),
    output_price_per_mtok=Decimal("2.50"),
    context_window=1_048_576,
    tier="strong",
)
# Prompts judged twice with the answer order swapped, to measure position bias.
CONSISTENCY_SAMPLE = 200

TEST_FRACTION = 0.2


def model_pair() -> tuple[ModelSpec, ModelSpec]:
    registry = ModelRegistry.from_yaml(ROOT / "models.yaml")
    return registry.get(CHEAP_ALIAS), registry.get(STRONG_ALIAS)
