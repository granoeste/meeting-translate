"""Haas-ja-v1.0をNeMoなしでローカル実行するための推論パッケージ。

torch・mlx・onnxruntimeはload_modelで選んだバックエンドだけを遅延して読む。

meeting-translate での変更: haas-ja-local の推論のうち MLX で使う部分だけを同梱している。
音声ファイルの読み込み(audio.load_audio。soundfile と soxr を使う)は使わないので、ここでは読み込まない。
"""

from haas_ja_local.config import HaasConfig
from haas_ja_local.decode import TimedToken
from haas_ja_local.pipeline import Transcript, load_model, transcribe

__version__ = "0.1.0"

__all__ = ["HaasConfig", "TimedToken", "Transcript", "__version__", "load_model", "transcribe"]
