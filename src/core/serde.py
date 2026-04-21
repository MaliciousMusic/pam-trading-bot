"""JSON serde -- msgspec-backed, mirrors the orjson surface we relied on.

Phase 0.5 pivot: orjson has no free-threaded Python wheel (maintainer
gate in build.rs that aborts the sdist compile), so the FT runtime
migration blocks on dropping it. msgspec ships prebuilt cp314t wheels
and was already on the Phase 1 roadmap, so we pull that swap forward
into Phase 0.5.

Every orjson call site in the tree routes through this module now. Keep
the API surface narrow (dumps / loads / dumps_indent / dumps_with_numpy
/ JSONDecodeError) so swapping the backend later is a one-file change.

Determinism note: msgspec's shortest-float repr differs subtly from
orjson's (rare trailing-zero cases and exponent placement). The Phase 0
baseline SHA (3665260f...) was orjson output; the Phase 0.5 re-baseline
under (3.14t + msgspec) produces a different SHA. That new SHA is the
parity target for Phase 1+.
"""
from __future__ import annotations

import json as _stdlib_json
from typing import Any

import msgspec

# Re-export so `except serde.JSONDecodeError:` matches what orjson
# callers used to catch. msgspec.DecodeError subclasses ValueError,
# same lineage as orjson.JSONDecodeError.
JSONDecodeError = msgspec.DecodeError


def _numpy_enc_hook(obj: Any) -> Any:
    """enc_hook for msgspec that mirrors orjson's OPT_SERIALIZE_NUMPY.

    ndarrays -> nested Python lists, numpy scalars -> their Python
    equivalents. Local numpy import keeps the module load cheap for
    callers that never touch numpy (ingress, ledger replay, etc.).
    """
    import numpy as np
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    raise TypeError(f"serde cannot encode {type(obj).__name__}")


# Reuse encoder/decoder objects -- msgspec docs note this is meaningfully
# faster than module-level encode()/decode() which rebuild state each call.
_encoder = msgspec.json.Encoder()
_encoder_numpy = msgspec.json.Encoder(enc_hook=_numpy_enc_hook)
_decoder = msgspec.json.Decoder()


def dumps(obj: Any) -> bytes:
    """Compact JSON encode. Drop-in for `orjson.dumps(obj)`."""
    return _encoder.encode(obj)


def dumps_with_numpy(obj: Any) -> bytes:
    """Compact JSON encode that accepts numpy scalars/arrays.

    Drop-in for `orjson.dumps(obj, option=orjson.OPT_SERIALIZE_NUMPY)`.
    Only the ledger uses this today; keeping it separate avoids paying
    the enc_hook dispatch cost on the hot decisions.jsonl path.
    """
    return _encoder_numpy.encode(obj)


def dumps_indent(obj: Any) -> bytes:
    """Pretty-printed JSON (2-space indent). Drop-in for
    `orjson.dumps(obj, option=orjson.OPT_INDENT_2)`.

    msgspec's encoder is compact-only, so we fall back to stdlib json
    for human-readable manifests/rollups/leaderboards. Separator/float
    formatting differs from orjson's pretty output; this affects only
    files a human reads (not the decisions.jsonl determinism contract).
    """
    return _stdlib_json.dumps(obj, indent=2).encode()


def loads(data: bytes | str | bytearray | memoryview) -> Any:
    """JSON decode. Drop-in for `orjson.loads(data)`.

    Accepts both str and bytes the way orjson did -- some websocket
    frames arrive as str, file reads as bytes. msgspec decoders want
    bytes; normalize up front.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    return _decoder.decode(data)
