"""Constrained decoding for distilled models: a GBNF grammar that only admits valid outputs.

    uv run --group distill python -m distill.grammar banking77-intent <models>/banking77-intent/v2

For a `label_set` task the grammar is an alternation of the exact labels, so llama.cpp can only
emit one of them: invalid labels become impossible instead of being caught afterwards and
re-served by the strong model. Tasks with other validators get a permissive grammar (any text).
llama-server loads it with `--grammar-file` (docker-compose.yml), as a default for every request.
"""

import json
import sys
from pathlib import Path
from typing import Any

from distill.discover import OUT


def gbnf(validator: dict[str, Any]) -> str:
    if validator.get("type") == "label_set":
        alternatives = " | ".join(json.dumps(label) for label in validator["labels"])
        return f"root ::= {alternatives}\n"
    return "root ::= [^\\x00]*\n"


def main() -> None:
    task, model_dir = sys.argv[1], Path(sys.argv[2])
    build = OUT / task / (OUT / task / "LATEST").read_text(encoding="utf-8").strip()
    manifest = json.loads((build / "manifest.json").read_text(encoding="utf-8"))
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "grammar.gbnf").write_text(gbnf(manifest["validator"]), encoding="utf-8")
    print(f"wrote {model_dir / 'grammar.gbnf'} ({manifest['validator']['type']})")


if __name__ == "__main__":
    main()
