# haas_ja_local(同梱)

日本語モード(`asr = "haas"`)で使う、Haas-ja-v1.0 の MLX 推論コードです。[basyauma-tech/haas-ja-local](https://github.com/basyauma-tech/haas-ja-local) の commit `c56919f0da98ee0044ba64279d54b495f8bc5eaa` から、MLX の推論に要るファイルだけを取り込みました。ライセンスは Apache-2.0 です(`LICENSE` と `NOTICE` を参照)。Third Intelligence の公式のコードではありません。

- そのまま取り込んだファイル: `architecture.py`、`config.py`、`decode.py`、`features.py`、`longform.py`、`mlx_model.py`、`pipeline.py`、`tokenizer.py`
- 変えたファイル: `__init__.py`(使わない `audio.load_audio` の import を外した)
- 取り込んでいないもの: 音声ファイルの読み込み(`audio.py`)、ONNX と PyTorch のバックエンド、変換スクリプト、CLI、テスト

元のパッケージは `requires-python = ">=3.12,<3.13"` ですが、Python 3.13 でも同じ結果になることを確かめています。更新するときは、元のリポジトリから同じファイルを取り直し、`__init__.py` の変更を当て直してください。
