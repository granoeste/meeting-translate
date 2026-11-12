"""設定ファイル(TOML)の読み込み。ファイルに書いた項目だけが既定値を上書きする。"""
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_PATH = Path(__file__).with_name("config.toml")


@dataclass
class Config:
    silence_end_ms: int = 500  # この長さの無音で発話を区切る
    max_segment_s: float = 12.0  # 1 つの発話の最長
    min_segment_s: float = 0.4  # これより短い発話は捨てる
    whisper_model: str = "mlx-community/whisper-large-v3-turbo"
    # Whisper が無音やノイズから作りがちな定番フレーズ
    hallucinations: list = field(default_factory=lambda: [
        "thank you", "thanks for watching", "you", "bye", "ご視聴ありがとうございました", "ありがとうございました",
    ])
    translate_model: str = "pfnet/plamo-3-translate-8b-MLX-4bit"
    pair: dict = field(default_factory=lambda: {"ja": "en", "en": "ja"})  # 判定した言語 → 翻訳先


def load(path=None):
    """(Config, 読んだファイルのパス) を返す。

    path を省略したときは config.toml があれば読み、無ければ既定値のまま。
    path を指定したのにファイルが無いときはエラーにする。
    """
    cfg = Config()
    p = Path(path) if path else DEFAULT_PATH
    if not p.is_file():
        if path:
            raise SystemExit(f"設定ファイルが見つかりません: {p}")
        return cfg, None
    try:
        with p.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"{p}: TOML の書式が正しくありません: {e}")

    names = [f.name for f in fields(Config)]
    for key, value in data.items():
        if key not in names:
            raise SystemExit(f"{p}: 不明な設定 '{key}'(使える項目: {', '.join(names)})")
        want = type(getattr(cfg, key))
        if want is float and type(value) is int:
            value = float(value)
        if type(value) is not want:
            raise SystemExit(f"{p}: '{key}' は {want.__name__} で指定してください(値: {value!r})")
        if want in (int, float) and value <= 0:
            raise SystemExit(f"{p}: '{key}' は正の数で指定してください(値: {value!r})")
        setattr(cfg, key, value)
    return cfg, p
