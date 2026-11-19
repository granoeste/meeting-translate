"""TDT（Token-and-Duration Transducer）のgreedyデコード。バックエンド非依存。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from haas_ja_local.config import HaasConfig


@dataclass(frozen=True)
class TimedToken:
    id: int
    frame: int  # 発話内のエンコーダフレーム番号
    duration: int  # TDTが予測したduration（0〜4）


class Backend(Protocol):
    config: HaasConfig
    window_np: np.ndarray  # チェックポイントの窓（400,）float32。log_melへ渡す
    fb_np: np.ndarray  # メルフィルタバンク [128, 257] float32

    def encode(self, mel: np.ndarray, n_frames: int) -> Any:
        """バックエンド固有のエンコーダ出力。有効長に切り詰め済み。"""
        ...

    def num_frames(self, encoded: Any) -> int: ...

    def init_state(self) -> Any:
        """予測ネットワークのゼロ状態。"""
        ...

    def step(self, encoded: Any, t: int, token: int, state: Any) -> tuple[np.ndarray, Any]:
        """tokenをstateから予測ネットワークへ入力し、フレームtと結合する。

        戻り値は(logits [vocab_size + 1 + len(durations)] float32, tokenを消費した後のstate)。
        """
        ...


def greedy_tdt(backend: Backend, encoded: Any) -> list[TimedToken]:
    """NeMoのTDT label-looping greedyと同じ規則でデコードする。"""
    cfg = backend.config
    n_vocab = cfg.vocab_size + 1
    blank = cfg.blank_id
    total = backend.num_frames(encoded)
    state = backend.init_state()
    last = blank
    symbols = 0
    t = 0
    out: list[TimedToken] = []
    while t < total:
        logits, new_state = backend.step(encoded, t, last, state)
        k = int(np.argmax(logits[:n_vocab]))
        d = int(cfg.durations[int(np.argmax(logits[n_vocab:]))])
        if k == blank:
            t += max(d, 1)
            symbols = 0
            continue
        out.append(TimedToken(k, t, d))
        last, state = k, new_state
        t += d
        if d == 0:
            symbols += 1
            if symbols >= cfg.max_symbols:
                t += 1
                symbols = 0
        else:
            symbols = 0
    return out
