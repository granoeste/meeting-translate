"""バックエンド共通のモデル定数と長さの計算。torch・mlxに依存しない。"""

from __future__ import annotations

import math

from haas_ja_local.config import HaasConfig

LAYER_NORM_EPS = 1e-5
KDA_L2_EPS = 1e-6
KDA_RMS_EPS = 1e-5


def subsampling_stages(config: HaasConfig) -> int:
    stages = int(math.log2(config.subsampling_factor))
    if 2**stages != config.subsampling_factor:
        raise ValueError(f"subsampling_factor must be a power of 2, got {config.subsampling_factor}")
    return stages


def encoded_length(n_frames: int, config: HaasConfig) -> int:
    """melフレーム数からエンコーダ出力の有効長を求める。stride 2・kernel 3・pad 1の各段でceil(L/2)。"""
    n = int(n_frames)
    for _ in range(subsampling_stages(config)):
        n = (n + 1) // 2
    return n
