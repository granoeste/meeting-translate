"""NeMoのFilterbankFeatures（推論時）と同じlog-mel前処理をnumpyで行う。"""

from __future__ import annotations

import numpy as np

from haas_ja_local.config import HaasConfig

PREEMPHASIS = 0.97
LOG_ZERO_GUARD = 2.0**-24
NORMALIZE_EPS = 1e-5


def mel_frames(n_samples: int, config: HaasConfig) -> int:
    """有効なmelフレーム数。NeMoのget_seq_len（(n + 2*(n_fft//2) - n_fft) // hop）と同じ。

    STFT自体はこれより1つ多いフレームを作るが、最後のフレームは有効長の外として0にする（NeMoのマスクと同じ）。
    0サンプルは0フレームとする。
    """
    if n_samples <= 0:
        return 0
    return (n_samples + 2 * (config.n_fft // 2) - config.n_fft) // config.hop_length


def log_mel(samples: np.ndarray, window: np.ndarray, fb: np.ndarray, config: HaasConfig) -> tuple[np.ndarray, int]:
    """1次元の音声からlog-melを作る。

    windowは(win_length,)、fbは(n_mels, n_fft//2+1)。どちらもcheckpointの値をfloat32にして渡す。
    戻り値は(mel [n_mels, T_pad] float32, n_frames)。T_padはSTFTのフレーム数（n_frames+1）をpad_toの倍数に
    切り上げた値で、n_frames以降は0。n_framesが0（hop_length未満の入力）なら(n_mels, 0)の空配列を返す。
    """
    n_fft, hop, win_length = config.n_fft, config.hop_length, config.win_length
    x = np.asarray(samples, dtype=np.float32)
    window = np.asarray(window, dtype=np.float32)
    fb = np.asarray(fb, dtype=np.float32)
    if x.ndim != 1:
        raise ValueError(f"samples must be 1-D, got shape {x.shape}")
    if window.shape != (win_length,):
        raise ValueError(f"window must have shape ({win_length},), got {window.shape}")
    if fb.shape != (config.n_mels, n_fft // 2 + 1):
        raise ValueError(f"fb must have shape ({config.n_mels}, {n_fft // 2 + 1}), got {fb.shape}")

    n_frames = mel_frames(x.shape[0], config)
    if n_frames == 0:
        return np.zeros((config.n_mels, 0), dtype=np.float32), 0

    # (1) pre-emphasis
    x = np.concatenate([x[:1], x[1:] - np.float32(PREEMPHASIS) * x[:-1]])

    # (2) center=True、pad_mode="constant"（NeMoのstftと同じく両端にn_fft//2の0を詰める）
    pad = n_fft // 2
    padded = np.pad(x, pad, mode="constant")

    # (3)フレーム化し、win_length点の窓をn_fft点の中央に0埋めで置いて掛ける（torch.stftと同じ配置）
    frames = np.lib.stride_tricks.sliding_window_view(padded, n_fft)[::hop]
    if frames.shape[0] != n_frames + 1:
        raise RuntimeError(f"STFT produced {frames.shape[0]} frames, expected {n_frames + 1}")
    left = (n_fft - win_length) // 2
    full_window = np.zeros(n_fft, dtype=np.float32)
    full_window[left : left + win_length] = window
    spec = np.fft.rfft(frames * full_window, n=n_fft, axis=-1)

    # (4)パワー → mel → log
    power = (spec.real**2 + spec.imag**2).astype(np.float32)
    mel = np.log(fb @ power.T + np.float32(LOG_ZERO_GUARD)).astype(np.float32)

    # (5) mel次元ごとの正規化（有効フレームのみ、不偏標準偏差）。
    # 1フレームだけでは不偏標準偏差が定義できないので、平均をそのフレーム、標準偏差を0とみなして出力を0にする
    if n_frames < 2:
        normalized = np.zeros_like(mel)
    else:
        valid = mel[:, :n_frames].astype(np.float64)
        mean = valid.mean(axis=1, keepdims=True)
        std = valid.std(axis=1, ddof=1, keepdims=True)
        normalized = ((mel - mean) / (std + NORMALIZE_EPS)).astype(np.float32)
    normalized[:, n_frames:] = 0

    # (6)時間軸をpad_toの倍数まで0で埋める
    remainder = normalized.shape[1] % config.pad_to
    if remainder:
        normalized = np.pad(normalized, ((0, 0), (0, config.pad_to - remainder)))
    return np.ascontiguousarray(normalized, dtype=np.float32), n_frames
