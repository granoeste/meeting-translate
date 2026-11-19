"""Haas-ja-v1.0のMLX実装（Apple Silicon、推論専用、単一発話）。

torch_model.pyをモジュール単位で写したもの。モジュールの属性名は元チェックポイントのキー名と同じで、
convert/mlx.pyが書いたmodel.safetensorsを`load_weights(strict=True)`でそのまま読める。
畳み込みはmlx.nnの規約どおりチャンネルが最後（Conv2dは[B,T,F,C]、Conv1dは[B,T,C]）。

精度: 既定ではエンコーダの行列積・畳み込みをbf16で行い、LayerNorm・BatchNorm・softmaxはfloat32で計算する。
予測ネットワーク（KDA）とjointは重みも計算もすべてfloat32。`compute_dtype=mx.float32`で全体をfloat32にできる。

量子化版: config.jsonにquantizationがあれば、線形層をすべてnn.QuantizedLinear（affine）にしてから読む。
畳み込み・正規化・埋め込みは量子化しない。scales・biasesは上と同じ方針のdtype（エンコーダはcompute_dtype、
予測ネットワークとjointはfloat32）で持ち、行列積の出力は入力と同じdtypeになる。

環境変数: このモジュールはimport時に`MLX_ENABLE_TF32`が未設定なら"0"にする（設定済みの値は変えない）。
Neural Acceleratorを持つGPU（M5など）で、上のfloat32の計算がTF32に落ちないようにするため。
TF32を使うには、このモジュールより前に`MLX_ENABLE_TF32=1`を設定する。M5 MaxのFLEURS 100発話では、
TF32のオン・オフでトークン列も速度も変わらなかった（docs/verification/2026-11-08-mac-mlx.md）。
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np

# Neural Acceleratorを持つGPU（M5など）では、mlxはfloat32の行列積を既定でTF32（仮数10ビット）で計算する。
# 予測ネットワーク・jointとcompute_dtype=float32のエンコーダをfloat32のまま計算するため既定で切る。
# mlxはこの値を最初の行列積のときに1度だけ読むので、それより前に設定する。利用者が設定した値は変えない。
os.environ.setdefault("MLX_ENABLE_TF32", "0")

try:
    import mlx.core as mx
    import mlx.nn as nn
except ImportError as e:  # pragma: no cover - mlxはmacOSのextra
    raise ImportError("the mlx backend needs mlx (Apple Silicon macOS): pip install 'haas-ja-local[mlx]'") from e

from haas_ja_local.architecture import KDA_L2_EPS, KDA_RMS_EPS, LAYER_NORM_EPS, encoded_length, subsampling_stages
from haas_ja_local.config import HaasConfig, QuantizationConfig
from haas_ja_local.decode import Backend

# ---------------------------------------------------------------------------
# 正規化（入力をfloat32にして計算し、入力のdtypeへ戻す）


class _LayerNorm(nn.Module):
    def __init__(self, dim: int, eps: float = LAYER_NORM_EPS) -> None:
        super().__init__()
        self.eps = eps
        self.weight = mx.ones((dim,))
        self.bias = mx.zeros((dim,))

    def __call__(self, x: mx.array) -> mx.array:
        y = mx.fast.layer_norm(x.astype(mx.float32), self.weight, self.bias, self.eps)
        return y.astype(x.dtype)


class _BatchNorm(nn.Module):
    """推論時のBatchNorm1d（running統計を使う）。xはチャンネルが最後。"""

    def __init__(self, dim: int, eps: float = 1e-5) -> None:
        super().__init__()
        self.eps = eps
        self.weight = mx.ones((dim,))
        self.bias = mx.zeros((dim,))
        self.running_mean = mx.zeros((dim,))
        self.running_var = mx.ones((dim,))

    def __call__(self, x: mx.array) -> mx.array:
        y = (x.astype(mx.float32) - self.running_mean) * mx.rsqrt(self.running_var + self.eps)
        return (y * self.weight + self.bias).astype(x.dtype)


# ---------------------------------------------------------------------------
# エンコーダ（FastConformer）


class ConvSubsampling(nn.Module):
    """dw_stridingのサブサンプリング。キーはpre_encode.conv.{0,2,3,5,6}とpre_encode.out。"""

    def __init__(self, config: HaasConfig) -> None:
        super().__init__()
        ch = config.subsampling_channels
        layers: list[nn.Module] = [nn.Conv2d(1, ch, 3, stride=2, padding=1), nn.ReLU()]
        freq = (config.n_mels + 1) // 2
        for _ in range(subsampling_stages(config) - 1):
            layers += [
                nn.Conv2d(ch, ch, 3, stride=2, padding=1, groups=ch),
                nn.Conv2d(ch, ch, 1),
                nn.ReLU(),
            ]
            freq = (freq + 1) // 2
        self.conv = layers  # listの添字がtorchのnn.Sequentialと同じキー（conv.0、conv.2 …）になる
        self.out = nn.Linear(ch * freq, config.d_model)

    def __call__(self, x: mx.array, length: int) -> mx.array:
        """x [B,T,F]、有効長length → [B,ceil(T/8),d_model]。

        torch_model.ConvSubsamplingと同じく、各層の前で時間軸の有効長より後ろを0にし、
        stride 2の畳み込みごとに有効長を(L + 2p - k) // s + 1へ更新する。
        """
        x = x[..., None]  # [B,T,F,1]
        for layer in self.conv:
            x = x * _time_mask(x, length)
            x = layer(x)
            if isinstance(layer, nn.Conv2d) and layer.stride[0] != 1:
                k = layer.weight.shape[1]
                length = (length + 2 * layer.padding[0] - k) // layer.stride[0] + 1
        x = x * _time_mask(x, length)  # [B,T',F',C]
        b, t = x.shape[:2]
        x = x.transpose(0, 1, 3, 2).reshape(b, t, -1)  # チャンネルが先、周波数が後（torchと同じ平坦化）
        return self.out(x)


def _time_mask(x: mx.array, length: int) -> mx.array:
    """x [B,T,F,C]に掛ける時間軸のマスク[1,T,1,1]。"""
    return (mx.arange(x.shape[1]) < length).astype(x.dtype)[None, :, None, None]


def rel_positional_encoding(t: int, d_model: int, dtype: mx.Dtype) -> mx.array:
    """相対位置埋め込み[1,2T-1,d_model]。float32で作ってdtypeへ落とす。"""
    pos = np.arange(t - 1, -t, -1, dtype=np.float32)[:, None]
    div = np.exp(np.arange(0, d_model, 2, dtype=np.float32) * np.float32(-(math.log(10000.0) / d_model)))
    pe = np.zeros((2 * t - 1, d_model), dtype=np.float32)
    pe[:, 0::2] = np.sin(pos * div)
    pe[:, 1::2] = np.cos(pos * div)
    return mx.array(pe)[None].astype(dtype)


def rel_shift(x: mx.array) -> mx.array:
    """x [B,H,T,2T-1]の各行iを、相対位置i-jが列jに来るようずらす。"""
    b, h, qlen, pos_len = x.shape
    x = mx.pad(x, [(0, 0), (0, 0), (0, 0), (1, 0)])
    x = x.reshape(b, h, -1, qlen)
    return x[:, :, 1:].reshape(b, h, qlen, pos_len)


class RelPositionMultiHeadAttention(nn.Module):
    def __init__(self, d_model: int, n_heads: int) -> None:
        super().__init__()
        if d_model % n_heads:
            raise ValueError(f"d_model {d_model} is not divisible by n_heads {n_heads}")
        self.h = n_heads
        self.d_k = d_model // n_heads
        self.linear_q = nn.Linear(d_model, d_model, bias=False)
        self.linear_k = nn.Linear(d_model, d_model, bias=False)
        self.linear_v = nn.Linear(d_model, d_model, bias=False)
        self.linear_out = nn.Linear(d_model, d_model, bias=False)
        self.linear_pos = nn.Linear(d_model, d_model, bias=False)
        self.pos_bias_u = mx.zeros((n_heads, self.d_k))
        self.pos_bias_v = mx.zeros((n_heads, self.d_k))

    def __call__(self, x: mx.array, pos_emb: mx.array) -> mx.array:
        b, t, d = x.shape
        h, dk = self.h, self.d_k
        q = self.linear_q(x).reshape(b, t, h, dk).transpose(0, 2, 1, 3)  # [B,H,T,dk]
        k = self.linear_k(x).reshape(b, t, h, dk).transpose(0, 2, 1, 3)
        v = self.linear_v(x).reshape(b, t, h, dk).transpose(0, 2, 1, 3)
        p = self.linear_pos(pos_emb).reshape(pos_emb.shape[0], -1, h, dk).transpose(0, 2, 1, 3)  # [1,H,2T-1,dk]
        q_u = q + self.pos_bias_u[None, :, None, :]
        q_v = q + self.pos_bias_v[None, :, None, :]
        ac = q_u @ k.transpose(0, 1, 3, 2)  # [B,H,T,T]
        bd = rel_shift(q_v @ p.transpose(0, 1, 3, 2))[..., :t]  # [B,H,T,T]
        scores = (ac + bd) / math.sqrt(dk)
        attn = mx.softmax(scores.astype(mx.float32), axis=-1).astype(v.dtype)
        out = (attn @ v).transpose(0, 2, 1, 3).reshape(b, t, d)
        return self.linear_out(out)


class ConformerFeedForward(nn.Module):
    def __init__(self, d_model: int, d_ff: int) -> None:
        super().__init__()
        self.linear1 = nn.Linear(d_model, d_ff, bias=False)
        self.linear2 = nn.Linear(d_ff, d_model, bias=False)

    def __call__(self, x: mx.array) -> mx.array:
        return self.linear2(nn.silu(self.linear1(x)))


class ConformerConvolution(nn.Module):
    def __init__(self, d_model: int, kernel_size: int) -> None:
        super().__init__()
        self.pointwise_conv1 = nn.Conv1d(d_model, 2 * d_model, 1, bias=False)
        self.depthwise_conv = nn.Conv1d(
            d_model, d_model, kernel_size, padding=(kernel_size - 1) // 2, groups=d_model, bias=False
        )
        self.batch_norm = _BatchNorm(d_model, eps=1e-5)
        self.pointwise_conv2 = nn.Conv1d(d_model, d_model, 1, bias=False)

    def __call__(self, x: mx.array) -> mx.array:
        """x [B,T,d] → [B,T,d]（チャンネルが最後のまま）。"""
        a, gate = mx.split(self.pointwise_conv1(x), 2, axis=-1)
        x = a * mx.sigmoid(gate)  # GLU
        x = self.depthwise_conv(x)
        x = self.batch_norm(x)
        x = nn.silu(x)
        return self.pointwise_conv2(x)


class ConformerLayer(nn.Module):
    def __init__(self, config: HaasConfig) -> None:
        super().__init__()
        d = config.d_model
        self.norm_feed_forward1 = _LayerNorm(d)
        self.feed_forward1 = ConformerFeedForward(d, d * config.ff_expansion)
        self.norm_self_att = _LayerNorm(d)
        self.self_attn = RelPositionMultiHeadAttention(d, config.n_heads)
        self.norm_conv = _LayerNorm(d)
        self.conv = ConformerConvolution(d, config.conv_kernel_size)
        self.norm_feed_forward2 = _LayerNorm(d)
        self.feed_forward2 = ConformerFeedForward(d, d * config.ff_expansion)
        self.norm_out = _LayerNorm(d)

    def __call__(self, x: mx.array, pos_emb: mx.array) -> mx.array:
        r = x + 0.5 * self.feed_forward1(self.norm_feed_forward1(x))
        r = r + self.self_attn(self.norm_self_att(r), pos_emb)
        r = r + self.conv(self.norm_conv(r))
        r = r + 0.5 * self.feed_forward2(self.norm_feed_forward2(r))
        return self.norm_out(r)


class ConformerEncoder(nn.Module):
    def __init__(self, config: HaasConfig) -> None:
        super().__init__()
        self.config = config
        self.pre_encode = ConvSubsampling(config)
        self.layers = [ConformerLayer(config) for _ in range(config.n_layers)]

    def __call__(self, mel: mx.array, n_frames: int) -> mx.array:
        """mel [B,T,n_mels]（時間が先）、有効長n_frames → [B,T',d_model]（有効長で切り詰め済み）。"""
        x = self.pre_encode(mel, n_frames)
        x = x[:, : encoded_length(n_frames, self.config)]
        pos_emb = rel_positional_encoding(x.shape[1], self.config.d_model, x.dtype)
        x = x * math.sqrt(self.config.d_model)  # xscaling
        for layer in self.layers:
            x = layer(x, pos_emb)
        return x


# ---------------------------------------------------------------------------
# 予測ネットワーク（Kimi Delta Attention）。すべてfloat32で計算する


class _RMSNormWeight(nn.Module):
    """o_normの重みだけを持つ。正規化とゲートはKimiDeltaAttention側で行う。"""

    def __init__(self, dim: int) -> None:
        super().__init__()
        self.weight = mx.ones((dim,))


class KimiDeltaAttention(nn.Module):
    """KDA層の1ステップ再帰形。状態S [B,H,V,K]（v行・k列）と短い畳み込みのキャッシュ（直近W入力）を持ち回す。"""

    def __init__(self, config: HaasConfig) -> None:
        super().__init__()
        p, h, k, w = config.pred_hidden, config.kda_heads, config.kda_head_dim, config.kda_conv_size
        d = h * k
        self.n_heads, self.head_dim, self.conv_size = h, k, w
        self.A_log = mx.zeros((h,))
        self.dt_bias = mx.zeros((d,))
        self.q_proj = nn.Linear(p, d, bias=False)
        self.k_proj = nn.Linear(p, d, bias=False)
        self.v_proj = nn.Linear(p, d, bias=False)
        # 重みの置き場として使う（[D,W,1]）。1ステップの畳み込みは_short_convで直接計算する
        self.q_conv1d = nn.Conv1d(d, d, w, groups=d, bias=False)
        self.k_conv1d = nn.Conv1d(d, d, w, groups=d, bias=False)
        self.v_conv1d = nn.Conv1d(d, d, w, groups=d, bias=False)
        self.f_proj = [nn.Linear(p, k, bias=False), nn.Linear(k, d, bias=False)]
        self.b_proj = nn.Linear(p, h, bias=False)
        self.g_proj = [nn.Linear(p, k, bias=False), nn.Linear(k, d, bias=True)]
        self.o_norm = _RMSNormWeight(k)
        self.o_proj = nn.Linear(d, p, bias=False)

    @staticmethod
    def _short_conv(h: mx.array, conv: nn.Conv1d, cache: mx.array) -> tuple[mx.array, mx.array]:
        """因果depthwise畳み込みの1ステップ。h [B,D]、cache [B,D,W]（古い順）→ (silu(y) [B,D], 新しいcache)。"""
        seq = mx.concatenate([cache[..., 1:], h[..., None]], axis=-1)
        y = (seq * conv.weight[:, :, 0]).sum(-1)
        return nn.silu(y), seq

    def __call__(
        self, x: mx.array, rec: mx.array, conv: tuple[mx.array, mx.array, mx.array]
    ) -> tuple[mx.array, mx.array, tuple[mx.array, mx.array, mx.array]]:
        """x [B,P]、rec [B,H,V,K]、conv（q,k,v各[B,D,W]）→ (y [B,P], rec', conv')。"""
        b = x.shape[0]
        h, kd = self.n_heads, self.head_dim
        q, q_cache = self._short_conv(self.q_proj(x), self.q_conv1d, conv[0])
        k, k_cache = self._short_conv(self.k_proj(x), self.k_conv1d, conv[1])
        v, v_cache = self._short_conv(self.v_proj(x), self.v_conv1d, conv[2])
        q = q.reshape(b, h, kd)
        k = k.reshape(b, h, kd)
        v = v.reshape(b, h, kd)
        q = q * mx.rsqrt((q * q).sum(-1, keepdims=True) + KDA_L2_EPS) * kd**-0.5
        k = k * mx.rsqrt((k * k).sum(-1, keepdims=True) + KDA_L2_EPS)

        g = self.f_proj[1](self.f_proj[0](x)).reshape(b, h, kd)
        gk = -mx.exp(self.A_log)[None, :, None] * nn.softplus(g + self.dt_bias.reshape(h, kd))
        beta = mx.sigmoid(self.b_proj(x))  # [B,H]

        s = rec * mx.exp(gk)[:, :, None, :]
        v_new = (v - (s * k[:, :, None, :]).sum(-1)) * beta[..., None]
        s = s + v_new[..., None] * k[:, :, None, :]
        o = (s * q[:, :, None, :]).sum(-1)  # [B,H,V]

        gate = self.g_proj[1](self.g_proj[0](x)).reshape(b, h, kd)
        o = o * mx.rsqrt(mx.mean(o * o, axis=-1, keepdims=True) + KDA_RMS_EPS) * self.o_norm.weight
        o = o * mx.sigmoid(gate)
        y = self.o_proj(o.reshape(b, h * kd))
        return y, s, (q_cache, k_cache, v_cache)


class _KDAStack(nn.Module):
    """dec_rnn: layers、norms（pre-norm）、final_norm。"""

    def __init__(self, config: HaasConfig) -> None:
        super().__init__()
        p = config.pred_hidden
        self.layers = [KimiDeltaAttention(config) for _ in range(config.pred_layers)]
        self.norms = [_LayerNorm(p) for _ in range(config.pred_layers)]
        self.final_norm = _LayerNorm(p)


class KDAPredictionNetwork(nn.Module):
    """embed → 各層でx + layer(norms[i](x)) → final_norm。状態は(rec, conv)各[L,B,kda_state_width]に詰める。

    詰め方はtorch_model.KDAPredictionNetworkと同じ。rec行はS [H,V,K]をその順に平坦化、
    conv行はq・k・vのキャッシュ[D,W]を順に平坦化して連結し、残りは0。
    """

    def __init__(self, config: HaasConfig) -> None:
        super().__init__()
        self.config = config
        self.embed = nn.Embedding(config.vocab_size + 1, config.pred_hidden)
        self.dec_rnn = _KDAStack(config)

    def _sizes(self) -> tuple[int, int]:
        c = self.config
        d = c.kda_heads * c.kda_head_dim
        return d * c.kda_head_dim, 3 * d * c.kda_conv_size

    def _unpack(self, rec: mx.array, conv: mx.array) -> tuple[mx.array, tuple[mx.array, mx.array, mx.array]]:
        c = self.config
        b = rec.shape[0]
        n_rec, n_conv = self._sizes()
        s = rec[:, :n_rec].reshape(b, c.kda_heads, c.kda_head_dim, c.kda_head_dim)
        caches = conv[:, :n_conv].reshape(b, 3, c.kda_heads * c.kda_head_dim, c.kda_conv_size)
        return s, (caches[:, 0], caches[:, 1], caches[:, 2])

    def _pack(self, s: mx.array, caches: tuple[mx.array, mx.array, mx.array]) -> tuple[mx.array, mx.array]:
        b = s.shape[0]
        width = self.config.kda_state_width
        n_rec, n_conv = self._sizes()
        rec = mx.pad(s.reshape(b, n_rec), [(0, 0), (0, width - n_rec)])
        conv = mx.pad(mx.concatenate([t.reshape(b, -1) for t in caches], axis=-1), [(0, 0), (0, width - n_conv)])
        return rec, conv

    def __call__(self, token: mx.array, rec: mx.array, conv: mx.array) -> tuple[mx.array, mx.array, mx.array]:
        """token [B]、rec・conv [L,B,W] → (g [B,P], rec', conv')。"""
        x = self.embed(token)  # [B,P]
        new_rec, new_conv = [], []
        for i, (layer, norm) in enumerate(zip(self.dec_rnn.layers, self.dec_rnn.norms, strict=True)):
            s, caches = self._unpack(rec[i], conv[i])
            y, s, caches = layer(norm(x), s, caches)
            x = x + y
            r, c = self._pack(s, caches)
            new_rec.append(r)
            new_conv.append(c)
        return self.dec_rnn.final_norm(x), mx.stack(new_rec), mx.stack(new_conv)


class _Decoder(nn.Module):
    def __init__(self, config: HaasConfig) -> None:
        super().__init__()
        self.prediction = KDAPredictionNetwork(config)


# ---------------------------------------------------------------------------
# Jointと全体


class Joint(nn.Module):
    def __init__(self, config: HaasConfig) -> None:
        super().__init__()
        n_outputs = config.vocab_size + 1 + len(config.durations)
        self.pred = nn.Linear(config.pred_hidden, config.joint_hidden)
        self.enc = nn.Linear(config.d_model, config.joint_hidden)
        # torchのnn.Sequential(ReLU, Identity, Linear)と同じ添字（joint_net.2）
        self.joint_net = [nn.ReLU(), nn.Identity(), nn.Linear(config.joint_hidden, n_outputs)]

    def __call__(self, f: mx.array, g: mx.array) -> mx.array:
        """f [B,d_model]、g [B,P] → logits [B,vocab_size+1+len(durations)]。"""
        x = self.enc(f) + self.pred(g)
        for layer in self.joint_net:
            x = layer(x)
        return x


class _Featurizer(nn.Module):
    def __init__(self, config: HaasConfig) -> None:
        super().__init__()
        self.window = mx.zeros((config.win_length,))
        self.fb = mx.zeros((1, config.n_mels, config.n_fft // 2 + 1))


class _Preprocessor(nn.Module):
    def __init__(self, config: HaasConfig) -> None:
        super().__init__()
        self.featurizer = _Featurizer(config)


class HaasModule(nn.Module):
    """重みの木。キーは元チェックポイントと同じ（num_batches_trackedを除く）。"""

    def __init__(self, config: HaasConfig) -> None:
        super().__init__()
        self.preprocessor = _Preprocessor(config)
        self.encoder = ConformerEncoder(config)
        self.decoder = _Decoder(config)
        self.joint = Joint(config)


def quantize_linear_layers(module: nn.Module, quantization: QuantizationConfig) -> None:
    """moduleのnn.Linearをすべてnn.QuantizedLinear（affine）に置き換える。変換器と読み込みで共通に使う。"""
    if quantization.targets != "linear":  # pragma: no cover - QuantizationConfigが検査済み
        raise ValueError(f"unsupported quantization targets {quantization.targets!r}")
    nn.quantize(
        module,
        group_size=quantization.group_size,
        bits=quantization.bits,
        mode="affine",
        class_predicate=lambda _path, m: isinstance(m, nn.Linear),
    )


# エンコーダのうち、compute_dtypeに関係なくfloat32で持つ部分（正規化）
_FLOAT32_ENCODER_PARTS = frozenset(
    {"norm_feed_forward1", "norm_self_att", "norm_conv", "norm_feed_forward2", "norm_out", "batch_norm"}
)


def _weight_dtype(key: str, compute_dtype: mx.Dtype) -> mx.Dtype:
    """重みを持つdtype。エンコーダの行列積・畳み込みだけcompute_dtype、残り（正規化・予測ネットワーク・joint・前処理）はfloat32。"""
    if key.startswith("encoder.") and _FLOAT32_ENCODER_PARTS.isdisjoint(key.split(".")):
        return compute_dtype
    return mx.float32


def _cast_weight(key: str, value: mx.array, compute_dtype: mx.Dtype) -> mx.array:
    """浮動小数点の重みは_weight_dtypeへ変える。量子化した重み（uint32に詰めた値）は変えない。"""
    if not mx.issubdtype(value.dtype, mx.floating):
        return value
    return value.astype(_weight_dtype(key, compute_dtype))


# Metalバッファのキャッシュの既定の上限（1GiB）。33.8分の音声で、上限なしでは約12GBまで増えた
DEFAULT_CACHE_LIMIT = 1 << 30


def _cap_cache_limit(limit: int) -> None:
    """キャッシュの上限をlimit以下にする。mlx 0.32にはget_cache_limitが無く、set_cache_limitが前の値を返す。"""
    previous = mx.set_cache_limit(limit)
    if previous < limit:
        mx.set_cache_limit(previous)


class HaasMLX(Backend):
    """Backendの実装。encodeはmx.array [T',d_model]を返し、stepはnumpyのlogitsを返す。"""

    def __init__(self, config: HaasConfig, module: HaasModule, compute_dtype: mx.Dtype) -> None:
        self.config = config
        self.module = module
        self.compute_dtype = compute_dtype
        featurizer = module.preprocessor.featurizer
        self.window_np = np.array(featurizer.window.astype(mx.float32))
        self.fb_np = np.array(featurizer.fb.astype(mx.float32))[0]

    @classmethod
    def load(
        cls,
        model_dir: Path,
        *,
        compute_dtype: mx.Dtype = mx.bfloat16,
        cache_limit_bytes: int | None = DEFAULT_CACHE_LIMIT,
    ) -> HaasMLX:
        """convert-mlxが書いたディレクトリ（config.json・model.safetensors）を読む。

        キーの過不足・形状違いはload_weights(strict=True)がValueErrorにする。
        config.jsonにquantizationがあれば、線形層を量子化した木を作ってから読む（量子化済みの値はそのまま使う）。

        cache_limit_bytes: mlxが解放後も再利用のために持つMetalバッファのキャッシュの上限（プロセス全体の設定）。
        発話や断片ごとに長さが違うとキャッシュが再利用されずに増え続ける（mlxの既定の上限は搭載メモリ並み）ため、
        既定で1GiBに抑える。現在の上限がこれ以下なら変えない（利用者が設定した小さい値を上げない）。Noneなら触らない。
        """
        model_dir = Path(model_dir)
        config = HaasConfig.from_json(model_dir / "config.json")
        module = HaasModule(config)
        if config.quantization is not None:
            quantize_linear_layers(module, config.quantization)
        weights = mx.load(str(model_dir / "model.safetensors"))
        module.load_weights([(k, _cast_weight(k, v, compute_dtype)) for k, v in weights.items()], strict=True)
        module.eval()
        mx.eval(module.parameters())
        if cache_limit_bytes is not None:
            _cap_cache_limit(cache_limit_bytes)
        return cls(config, module, compute_dtype)

    def encode(self, mel: np.ndarray, n_frames: int) -> mx.array:
        if n_frames <= 0:
            return mx.zeros((0, self.config.d_model), dtype=self.compute_dtype)
        x = mx.array(np.ascontiguousarray(mel, dtype=np.float32)).astype(self.compute_dtype)
        enc = self.module.encoder(x.T[None], n_frames)[0]
        mx.eval(enc)
        return enc

    def num_frames(self, encoded: mx.array) -> int:
        return int(encoded.shape[0])

    def init_state(self) -> tuple[mx.array, mx.array]:
        shape = (self.config.pred_layers, 1, self.config.kda_state_width)
        return mx.zeros(shape, dtype=mx.float32), mx.zeros(shape, dtype=mx.float32)

    def step(
        self, encoded: mx.array, t: int, token: int, state: tuple[mx.array, mx.array]
    ) -> tuple[np.ndarray, tuple[mx.array, mx.array]]:
        rec, conv = state
        g, rec, conv = self.module.decoder.prediction(mx.array([token]), rec, conv)
        logits = self.module.joint(encoded[t][None].astype(mx.float32), g)[0]
        mx.eval(logits, rec, conv)
        return np.array(logits, dtype=np.float32), (rec, conv)
