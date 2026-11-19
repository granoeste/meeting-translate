"""モデルの読み込みと音声1本の書き起こし。"""

from __future__ import annotations

import importlib
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import numpy as np

from haas_ja_local.decode import Backend, TimedToken, greedy_tdt
from haas_ja_local.features import log_mel
from haas_ja_local.longform import split_at_quiet_points
from haas_ja_local.tokenizer import Tokenizer

BackendName = Literal["mlx", "onnx", "torch"]

# バックエンド名 → (実装モジュール, 必要な外部パッケージ, extra名)
_BACKENDS: dict[str, tuple[str, str, str]] = {
    "torch": ("haas_ja_local.torch_model", "torch", "convert"),
    "mlx": ("haas_ja_local.mlx_model", "mlx", "mlx"),
    "onnx": ("haas_ja_local.onnx_model", "onnxruntime", "onnx"),
}


@dataclass(frozen=True)
class Transcript:
    text: str
    tokens: tuple[TimedToken, ...]
    offsets: tuple[float, ...]  # tokensと同じ長さ。各トークンが属する断片の開始秒


def _detect_backend(path: Path) -> BackendName:
    if not path.exists():
        raise FileNotFoundError(f"model not found: {path}")
    if path.is_file() and path.suffix == ".nemo":
        return "torch"
    if path.is_dir():
        if (path / "model.safetensors").is_file():
            return "mlx"
        if (path / "fp32").is_dir() or (path / "int8").is_dir():
            return "onnx"
    raise FileNotFoundError(
        f"not a model: {path} (expected a .nemo file, or a directory with model.safetensors, fp32/ or int8/)"
    )


def _import_backend(name: str):
    """バックエンドの実装モジュールを読む。外部パッケージが無ければ入れ方を示すImportErrorにする。"""
    module, dependency, extra = _BACKENDS[name]
    try:
        importlib.import_module(dependency)
    except ImportError as e:
        if name == "mlx" and sys.platform != "darwin":
            raise ImportError(
                "the mlx backend requires macOS on Apple Silicon (the [mlx] extra installs nothing on "
                f"{sys.platform}); use the onnx backend instead"
            ) from e
        raise ImportError(f"the {name} backend needs {dependency}: pip install 'haas-ja-local[{extra}]'") from e
    return importlib.import_module(module)


def load_model(
    model_dir_or_nemo: str | Path,
    *,
    backend: BackendName | None = None,
    threads: int | None = None,
    variant: str | None = None,
) -> tuple[Backend, Tokenizer]:
    """モデルを読み、(バックエンド, トークナイザ)を返す。

    backendを省略すると、.nemoならtorch、model.safetensorsを含むディレクトリならmlx、
    fp32/かint8/を含むディレクトリならonnxを選ぶ。threadsはonnxでだけ使い、他では無視する。
    variant（"fp32"か"int8"）はonnxでだけ指定でき、他のバックエンドで指定するとValueError。
    省略するとfp32/があればfp32を使う。
    """
    path = Path(model_dir_or_nemo)
    if backend is None:
        backend = _detect_backend(path)
    elif backend not in _BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; choose from {sorted(_BACKENDS)}")
    elif not path.exists():
        raise FileNotFoundError(f"model not found: {path}")

    if variant is not None and backend != "onnx":
        raise ValueError(f"variant {variant!r} is only for the onnx backend (this model uses {backend})")

    impl = _import_backend(backend)
    if backend == "torch":
        return impl.load_torch_model(path)
    if backend == "mlx":
        return impl.HaasMLX.load(path), Tokenizer(path / "tokenizer.model")
    return impl.HaasONNX.load(path, variant=variant, threads=threads), Tokenizer(path / "tokenizer.model")


def _decode_chunk(backend: Backend, audio: np.ndarray) -> list[TimedToken]:
    if len(audio) < backend.config.hop_length:
        return []
    mel, n_frames = log_mel(audio, backend.window_np, backend.fb_np, backend.config)
    if n_frames == 0:
        return []
    return greedy_tdt(backend, backend.encode(mel, n_frames))


def transcribe(
    backend: Backend, tokenizer: Tokenizer, audio: np.ndarray, *, longform_threshold_seconds: float = 30.0
) -> Transcript:
    """16kHzモノラルの音声を書き起こす。

    longform_threshold_seconds以下なら1回で認識する。超える場合はsplit_at_quiet_pointsで同じ長さを目安に
    分け、断片ごとに認識する。textは断片ごとにdecodeしてstripし、空文字で連結する。
    """
    sample_rate = backend.config.sample_rate
    if len(audio) <= longform_threshold_seconds * sample_rate:
        spans = [(0, len(audio))]
    else:
        spans = split_at_quiet_points(audio, sample_rate=sample_rate, chunk_seconds=longform_threshold_seconds)

    texts: list[str] = []
    tokens: list[TimedToken] = []
    offsets: list[float] = []
    for start, end in spans:
        chunk_tokens = _decode_chunk(backend, audio[start:end])
        texts.append(tokenizer.decode([t.id for t in chunk_tokens]).strip())
        tokens.extend(chunk_tokens)
        offsets.extend([start / sample_rate] * len(chunk_tokens))
    return Transcript("".join(texts), tuple(tokens), tuple(offsets))


_BYTE_PIECE = re.compile(r"<0x[0-9A-Fa-f]{2}>")


class _PieceTokenizer(Protocol):
    def pieces(self, ids: Sequence[int]) -> list[str]: ...

    def decode(self, ids: Sequence[int]) -> str: ...


def token_records(
    tokens: Sequence[TimedToken], offsets: Sequence[float], tokenizer: _PieceTokenizer, frame_seconds: float
) -> list[dict]:
    """トークン列を{"text", "start", "end"}（秒、ミリ秒に丸め）の列にする。CLIの--timestampsで使う。

    続くバイトフォールバックのpiece（`<0xE9>`など）は同じ断片の中で1つにまとめ、tokenizer.decodeで文字に戻す。
    通常のpieceは先頭の"▁"を消し（日本語には語の区切りの空白が無い）、途中の"▁"は空白にする。
    文字列が空になったものは捨てる。startは先頭トークンの開始、endは末尾トークンの終了（duration 0は1フレーム）。
    """
    if len(tokens) != len(offsets):
        raise ValueError(f"tokens ({len(tokens)}) and offsets ({len(offsets)}) differ in length")
    pieces = tokenizer.pieces([t.id for t in tokens])

    def start_of(i: int) -> float:
        return offsets[i] + tokens[i].frame * frame_seconds

    def end_of(i: int) -> float:
        return start_of(i) + max(tokens[i].duration, 1) * frame_seconds

    records: list[dict] = []
    i = 0
    while i < len(tokens):
        j = i + 1
        if _BYTE_PIECE.fullmatch(pieces[i]):
            while j < len(tokens) and _BYTE_PIECE.fullmatch(pieces[j]) and offsets[j] == offsets[i]:
                j += 1
            text = tokenizer.decode([t.id for t in tokens[i:j]])
        else:
            text = pieces[i].removeprefix("▁").replace("▁", " ")
        if text:
            records.append({"text": text, "start": round(start_of(i), 3), "end": round(end_of(j - 1), 3)})
        i = j
    return records
