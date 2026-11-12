"""設定ファイル(TOML)の読み込み。ファイルに書いた項目だけが既定値を上書きする。"""
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_PATH = Path(__file__).with_name("config.toml")
SPEAKERS = ("自分", "相手")  # main.py の話者ラベル
NON_POSITIVE = ("short_min_logprob",)  # 0 以下の値を取る項目(それ以外の数値は正の数)


@dataclass
class Config:
    silence_end_ms: int = 300  # この長さの無音で発話を区切る
    soft_segment_s: float = 3.0  # 発話がこれより長くなったら、soft_silence_ms の短い間でも区切る
    soft_silence_ms: int = 150
    max_segment_s: float = 6.0  # 1 つの発話の最長。超えたら直前 1 秒の最も静かな所で区切る
    min_segment_s: float = 0.4  # これより短い発話は捨てる
    merge_fragments: bool = True  # 文の途中で終わった発話は、次の発話と結合してから訳す
    merge_wait_s: float = 4.0  # 結合のために待つ最長の時間。次の発話が来なければ、そのまま訳す
    merge_max_s: float = 12.0  # 結合した発話がこれより長くなったら、文が終わっていなくても訳す
    short_min_logprob: float = -1.0  # 2.5 秒未満の発話の平均 logprob がこれより低ければ、ハルシネーションとして捨てる(0 以下)
    whisper_model: str = "mlx-community/whisper-large-v3-turbo"
    # Whisper が無音やノイズから作りがちな定番フレーズ
    hallucinations: list = field(default_factory=lambda: [
        "thank you", "thanks for watching", "you", "bye", "ご視聴ありがとうございました", "ありがとうございました",
    ])
    translate_model: str = "pfnet/plamo-3-translate-8b-MLX-4bit"
    pair: dict = field(default_factory=lambda: {"ja": "en", "en": "ja"})  # 判定した言語 → 翻訳先
    fixed_lang: dict = field(default_factory=dict)  # 話者ラベル("自分"/"相手") → 言語。指定した話者は言語を判定しない


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
        if key in NON_POSITIVE:
            if value > 0:
                raise SystemExit(f"{p}: '{key}' は 0 以下の数で指定してください(値: {value!r})")
        elif want in (int, float) and value <= 0:
            raise SystemExit(f"{p}: '{key}' は正の数で指定してください(値: {value!r})")
        setattr(cfg, key, value)

    for who, lang in cfg.fixed_lang.items():
        if who not in SPEAKERS:
            raise SystemExit(f"{p}: 'fixed_lang' の話者は {' / '.join(SPEAKERS)} で指定してください(値: {who!r})")
        if lang not in cfg.pair:
            raise SystemExit(f"{p}: 'fixed_lang' の言語 {lang!r} が 'pair' にありません")
    return cfg, p
