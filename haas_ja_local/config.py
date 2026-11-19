"""Haas-ja-v1.0のモデル構成。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

# HaasConfigのフィールド名 → model_config.yamlのキー名
_YAML_INT_KEYS = {
    "d_model": "d_model",
    "n_layers": "n_layers",
    "n_heads": "n_heads",
    "conv_kernel_size": "conv_kernel_size",
    "subsampling_factor": "subsampling_factor",
    "subsampling_channels": "subsampling_conv_channels",
    "ff_expansion": "ff_expansion_factor",
    "pred_hidden": "pred_hidden",
    "pred_layers": "pred_rnn_layers",
    "kda_heads": "kda_num_heads",
    "kda_head_dim": "kda_head_dim",
    "kda_conv_size": "kda_conv_size",
    "joint_hidden": "joint_hidden",
    "vocab_size": "vocab_size",
    "n_mels": "features",
    "n_fft": "n_fft",
    "max_symbols": "max_symbols",
}


# MLX量子化版で使える値（mlx.core.quantizeのaffineモードのうち、このパッケージで扱うもの）
QUANTIZATION_BITS = (4, 8)
QUANTIZATION_GROUP_SIZES = (32, 64, 128)


@dataclass(frozen=True)
class QuantizationConfig:
    """MLX量子化版の設定。線形層の重みだけをaffine量子化する（targets="linear"）。"""

    bits: int
    group_size: int = 64
    targets: str = "linear"

    def __post_init__(self) -> None:
        if self.bits not in QUANTIZATION_BITS:
            raise ValueError(f"quantization bits must be one of {QUANTIZATION_BITS}, got {self.bits}")
        if self.group_size not in QUANTIZATION_GROUP_SIZES:
            raise ValueError(
                f"quantization group_size must be one of {QUANTIZATION_GROUP_SIZES}, got {self.group_size}"
            )
        if self.targets != "linear":
            raise ValueError(f"quantization targets must be 'linear', got {self.targets!r}")


@dataclass(frozen=True)
class HaasConfig:
    sample_rate: int = 16000
    n_fft: int = 512
    win_length: int = 400
    hop_length: int = 160
    n_mels: int = 128
    pad_to: int = 16
    d_model: int = 1024
    n_layers: int = 24
    n_heads: int = 8
    ff_expansion: int = 4
    conv_kernel_size: int = 9
    subsampling_factor: int = 8
    subsampling_channels: int = 256
    pred_hidden: int = 640
    pred_layers: int = 2
    kda_heads: int = 5
    kda_head_dim: int = 64
    kda_conv_size: int = 4
    joint_hidden: int = 640
    vocab_size: int = 3072
    durations: tuple[int, ...] = (0, 1, 2, 3, 4)
    max_symbols: int = 10
    source_repo: str = "third-intelligence/Haas-ja-v1.0"
    source_sha256: str = ""
    # MLX量子化版だけが持つ。Noneならconfig.jsonに書かない（bf16版・ONNX版は従来と同じ内容）
    quantization: QuantizationConfig | None = None

    @property
    def blank_id(self) -> int:
        return self.vocab_size

    @property
    def frame_seconds(self) -> float:
        return self.hop_length * self.subsampling_factor / self.sample_rate

    @property
    def kda_state_width(self) -> int:
        """KDA層1つ分の再帰状態と畳み込み状態を平坦化したときの幅（大きい方）。"""
        value_dim = self.kda_heads * self.kda_head_dim
        return max(value_dim * self.kda_head_dim, 3 * value_dim * self.kda_conv_size)

    def to_json(self, path: Path) -> None:
        data = asdict(self)
        data["durations"] = list(self.durations)
        if self.quantization is None:
            del data["quantization"]
        Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    @classmethod
    def from_json(cls, path: Path) -> HaasConfig:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["durations"] = tuple(data["durations"])
        if data.get("quantization") is not None:
            data["quantization"] = QuantizationConfig(**data["quantization"])
        return cls(**data)

    @classmethod
    def from_model_config_yaml(cls, text: str) -> HaasConfig:
        """.nemo内のmodel_config.yamlから必要なキーだけを行単位で読む。

        同じキーが複数の節にある場合は最初の出現を使う（上流の構成ではどれも同じ値）。
        """
        values: dict[str, object] = {}
        for field, key in _YAML_INT_KEYS.items():
            m = re.search(rf"^[ ]*{key}:[ ]*(-?\d+)[ ]*$", text, re.MULTILINE)
            if m is None:
                raise ValueError(f"model_config.yaml: integer key {key!r} not found")
            values[field] = int(m.group(1))
        m = re.search(r"^([ ]*)durations:[ ]*\n((?:\1-[ ]*\d+[ ]*\n?)+)", text, re.MULTILINE)
        if m is None:
            raise ValueError("model_config.yaml: list key 'durations' not found")
        values["durations"] = tuple(int(v) for v in re.findall(r"-[ ]*(\d+)", m.group(2)))
        return cls(**values)
