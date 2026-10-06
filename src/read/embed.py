"""Bedrock Titan Text Embeddings V2. See docs/design.md, "Embed and index"."""

import json

EMBED_MODEL = "amazon.titan-embed-text-v2:0"
DIMENSIONS = 1024


def embed(text: str, client) -> list[float]:
    """1024-dim normalized vector for text; client is a boto3 bedrock-runtime client."""
    r = client.invoke_model(
        modelId=EMBED_MODEL,
        body=json.dumps({"inputText": text, "dimensions": DIMENSIONS, "normalize": True}),
    )
    vec = json.loads(r["body"].read())["embedding"]
    if len(vec) != DIMENSIONS:
        raise ValueError(f"expected {DIMENSIONS} dims, got {len(vec)}")
    return vec
