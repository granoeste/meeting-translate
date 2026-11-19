"""日本語モードの文字起こし。Haas-ja-v1.0 を MLX で動かす。

公式の Haas-ja-v1.0 は NeMo と CUDA が前提で Mac では動かないので、
有志が MLX に変換した版(既定は basyauma-tech/Haas-ja-v1.0-mlx-8bit)を、
同梱の haas_ja_local(同じ作者の推論コード。haas_ja_local/README.md を参照)で動かす。
"""
import numpy as np
from huggingface_hub import snapshot_download

SR = 16000  # main.py の SR と合わせる


class Haas:
    def __init__(self, repo):
        from haas_ja_local import load_model  # 日本語モードのときだけ読み込む

        path = snapshot_download(repo)  # 初回はダウンロードする。2 回目からは手元のキャッシュを使う
        try:
            self.backend, self.tokenizer = load_model(path, backend="mlx")
        except (OSError, ValueError, KeyError) as e:
            raise SystemExit(f"{repo} を Haas の MLX のモデルとして読めません: {e}")

    def transcribe(self, audio):
        """16kHz・mono・float32 の音声を文字にする"""
        from haas_ja_local import transcribe

        return transcribe(self.backend, self.tokenizer, np.asarray(audio, np.float32)).text.strip()
