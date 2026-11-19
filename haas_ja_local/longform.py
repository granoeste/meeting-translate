"""長い音声を無音に近い位置で断片に分ける。"""

from __future__ import annotations

import numpy as np


def split_at_quiet_points(
    audio: np.ndarray,
    *,
    sample_rate: int = 16000,
    chunk_seconds: float = 30.0,
    search_seconds: float = 2.0,
    window_seconds: float = 0.1,
) -> list[tuple[int, int]]:
    """音声を連続する断片[(start, end), ...]（サンプル番号、endは含まない）に分ける。

    startからtarget=start+chunk_secondsを狙う。残りがchunk_seconds以下なら最後まで1断片にする。
    それ以外は窓の開始位置をmax(start+chunk_seconds/2, target-search_seconds)〜
    min(len-窓長, target+search_seconds)の範囲で動かし、窓内の二乗和が最小の窓（同値なら最も前）の中心で切る。
    範囲が成立しなければtargetで切る。各断片は最長でchunk_seconds+search_seconds+window_seconds/2になる。
    """
    n = len(audio)
    chunk = round(chunk_seconds * sample_rate)
    search = round(search_seconds * sample_rate)
    window = round(window_seconds * sample_rate)
    if chunk <= 0 or window <= 0:
        raise ValueError("chunk_seconds and window_seconds must be positive")

    chunks: list[tuple[int, int]] = []
    start = 0
    while start < n:
        if n - start <= chunk:
            chunks.append((start, n))
            break
        target = start + chunk
        low = max(start + chunk // 2, target - search)
        high = min(n - window, target + search)
        if low > high:
            cut = target
        else:
            # 範囲内の窓エネルギーを累積和で求める。累積は探索範囲だけで取り、長い音声でも桁落ちさせない
            segment = np.asarray(audio[low : high + window], dtype=np.float64)
            cumsum = np.concatenate([[0.0], np.cumsum(segment * segment)])
            energy = cumsum[window:] - cumsum[:-window]
            cut = low + int(np.argmin(energy)) + window // 2
        chunks.append((start, cut))
        start = cut
    return chunks
