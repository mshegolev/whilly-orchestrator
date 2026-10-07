"""Standalone child performing non-generative Cognee indexing and recall."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

MAX_RECORDS, MAX_QUERY_CHARS = 32, 4096


def _record_stream(body: str) -> SimpleNamespace:
    """Prevent SDK path/URL interpretation of canonical record text."""
    stream = io.BytesIO(body.encode("utf-8"))
    return SimpleNamespace(file=stream, filename="record.txt")


async def _rank_with_cognee(payload: dict[str, object]) -> list[str]:
    try:
        with contextlib.redirect_stdout(sys.stderr):
            import cognee
            from cognee.modules.search.types import SearchType
            from cognee.tasks.ingestion.data_item import DataItem
    except ImportError as exc:
        raise RuntimeError("cognee_unavailable") from exc
    records = payload["records"]
    query = payload["query"]
    if not isinstance(records, list) or not isinstance(query, str):
        raise RuntimeError("invalid_payload")
    dataset = "whilly_" + Path(os.environ["WHILLY_REQUEST_DIR"]).name
    data_id_to_record_id: dict[str, str] = {}
    items = []
    for item in records:
        record_id = item["id"]
        data_id = uuid5(NAMESPACE_URL, f"whilly:{dataset}:{record_id}")
        data_id_to_record_id[str(data_id)] = record_id
        items.append(
            DataItem(
                data=_record_stream(item["body"]),
                data_id=data_id,
                external_metadata={"whilly_record_id": record_id},
            )
        )
    with contextlib.redirect_stdout(sys.stderr):
        await cognee.remember(items, dataset_name=dataset, self_improvement=False)
        found = await cognee.recall(
            query,
            query_type=SearchType.CHUNKS,
            datasets=[dataset],
            top_k=len(records),
            retriever_specific_config={
                "include_external_metadata": True,
                "external_metadata_keys": ["whilly_record_id"],
            },
        )
    return _map_hits(found, data_id_to_record_id)


def _map_hits(found, data_id_to_record_id: dict[str, str]) -> list[str]:
    """Resolve SDK chunk provenance; neither generated text nor body equality is identity."""
    ids: list[str] = []
    for result in found:
        if getattr(result, "source", None) != "graph" or getattr(result, "kind", None) != "chunk":
            raise RuntimeError("unexpected_sdk_result")
        metadata = getattr(result, "metadata", {})
        mapped = data_id_to_record_id.get(str(metadata.get("data_id")))
        if mapped is None:
            raise RuntimeError("unknown_chunk_provenance")
        if mapped not in ids:
            ids.append(mapped)
    return ids


def main() -> int:
    line = sys.stdin.buffer.readline(128 * 1024 + 1)
    if len(line.rstrip(b"\n")) > 128 * 1024:
        return 2
    try:
        payload = json.loads(line)
        if not isinstance(payload, dict) or set(payload) != {"version", "query", "records"}:
            return 2
        if (
            type(payload["version"]) is not int
            or payload["version"] != 1
            or not isinstance(payload["query"], str)
            or not isinstance(payload["records"], list)
        ):
            return 2
        if len(payload["records"]) > MAX_RECORDS or len(payload["query"]) > MAX_QUERY_CHARS:
            return 2
        if any(
            not isinstance(item, dict)
            or set(item) != {"id", "body"}
            or not isinstance(item["id"], str)
            or not isinstance(item["body"], str)
            for item in payload["records"]
        ):
            return 2
        if len({item["id"] for item in payload["records"]}) != len(payload["records"]):
            return 2
        started = time.monotonic()
        ids = asyncio.run(_rank_with_cognee(payload))
        print(
            json.dumps(
                {"version": 1, "ids": ids, "elapsed_ms": int((time.monotonic() - started) * 1000)},
                separators=(",", ":"),
            )
        )
        return 0
    except (json.JSONDecodeError, KeyError, TypeError, RuntimeError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
