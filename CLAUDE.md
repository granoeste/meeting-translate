# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

会議の音声をローカル(Apple Silicon)で文字起こしし、日本語⇔英語に翻訳するツール。利用者向けの説明は `README.md`(日本語)にあり、機能やオプションを変えたら README も合わせて直す。

## コマンド

```
uv sync                                         # 依存の導入(Python 3.11 以上。tomllib を使う)
uv run python -u main.py                        # 起動。--list でデバイス一覧、--debug で Whisper の信頼度を表示
uv run python -u main.py --mic "Shure" --no-web --no-record
uv run python check_audio.py loop               # BlackHole の出力 → 入力のループバック判定(PASS/FAIL)
uv run python check_audio.py meter              # 入力レベルのメーター(Ctrl+C で終了)
uv run python transcript.py transcripts/xxx.jsonl   # .jsonl から .txt/.srt を作り直す
say -a "BlackHole 2ch" "テストです"             # 「相手」側にテスト音声を流す
```

テストとリンターは無い。構文の確認は `uv run python -m py_compile *.py`。

- モデル(計 約 7.2GB)は初回起動時に Hugging Face からダウンロードされる。手元に無いときは、確認のためだけに `main.py` を起動しない。
- モデル抜きで配線を確かめるときは、`main` を import する前に `sys.modules["mlx_whisper"]` と `sys.modules["mlx_lm"]` をスタブに差し替える。`main.main()` は `signal.signal` を呼ぶので、メインスレッドから呼ぶ。終了時の動き(Ctrl+C など)を確かめるときは、`import main` ではなく `runpy.run_path("main.py", run_name="__main__")` で本番と同じく起動する。import だと終了処理の順番が変わり、本番で起きる問題が再現しないことがある。

## 全体の流れ

1. 音声源ごとにスレッドを立てる(`main.py`)。ローカルのデバイスは `capture()`(sounddevice)、別 Mac の `sender.py` からは `capture_remote()`(TCP、切断時は自動で再接続)。
2. どちらも `segment()` に 30ms(16kHz・mono・int16 で 480 サンプル)のフレームを渡す。webrtcvad で発話に区切り、グローバルの `segments` キューに `(話者ラベル, 音声, 開始, 終了)` を積む。
3. ワーカースレッドは 1 本だけ。Whisper で文字起こしし、ハルシネーション対策のフィルタ(`no_speech_prob`、短く低信頼度の断片、定番フレーズ)を通す。そのあと `Translator` で翻訳する。直前の同じ話者の発話を `context` として渡す。末尾が文末で終わらない発話は `pending` に保留し、次の発話(同じ話者・同じ言語)と結合してから訳す(`merge_wait_s` を過ぎたらそのまま訳す。終了時は `segments` に `None` を入れて、保留分を訳させる)。
4. 結果のイベントを `output.publish()`(Web/OBS への SSE 配信)と `Recorder.write()`(`.jsonl` に追記)と標準出力に出す。終了時(Ctrl+C / SIGTERM)に `.txt` と `.srt` を書き出す。

複数のファイルにまたがる約束事:

- **イベントの形** `{id, ts, speaker, lang, text, translation, start, end}` は、SSE、`.jsonl`、`transcript.py` の書き出しで共通。`start`/`end` は起動時刻からの秒数。
- **話者ラベル**の `"自分"` と `"相手"` は文字列そのままで使っている。`output.py` の Web 画面の JavaScript も `'自分'` で色を分けているので、変えるときは両方直す。
- **`sender.py` の通信**はヘッダの無い生 PCM(16kHz・mono・int16、480 サンプルずつ)。`main.py` の `SR`/`FRAME` と合わせる。`sender.py` は会議用の Mac で単体で動かすので(PEP 723 のインライン依存)、プロジェクトの他のモジュールを import しない。
- **設定**は `config.py` の `Config`(dataclass)が既定値で、`config.toml` に書いた項目だけが上書きする。型チェックは既定値の型を見ている。項目を足すときは、`Config`、`config.example.toml`、README の表の 3 か所を直す。`main.py` では、モジュールのグローバル変数 `cfg` を `main()` の中で読み込んだ値に置き換え、各関数は実行時に `cfg.xxx` を参照する。
- **終了処理**: `main()` を抜ける前に `stopping` を立て、`capture` スレッドを join して音声ストリームを閉じる。`stream.read()` の中で待っているスレッドを残したまま終了すると、sounddevice が PortAudio を外した後にそのスレッドが落ちる(SIGSEGV)。PortAudio を使うスレッドを足すときも、同じく終了前に止める。
- **翻訳**(`translate.py`)は、`translate_model` の名前でプロンプトを選ぶ。PLaMo は chat template に `source_lang`/`target_lang`/`context` を渡し、日本語にするときは `form="polite"` を付ける。名前に `translategemma` か `hy-mt` が入っていれば、それぞれの形式にする(どちらも `context` は使わない)。TranslateGemma は `<end_of_turn>` を終了トークンに足さないと、`max_tokens` まで出し続ける。`generate()` は遅かったので `stream_generate()` を使っている。

## 守ること

- 音声と文字を外部サービスに送らない(README で約束している)。Web サーバーと `sender.py` は `127.0.0.1` でしか待ち受けない。LAN 越しは SSH トンネルを前提にしているので、待ち受けアドレスを広げない。
- このリポジトリは GitHub の公開リポジトリ。`transcripts/`(会議の全文)、`config.toml`(各自の設定)、`uv.lock`(社内のパッケージプロキシの URL が入る)はコミットしない(`.gitignore` 済み)。
- この Mac では、デフォルト入力が BlackHole になっていることがある。その状態で `--mic` を付けずに起動すると、「自分」も BlackHole を拾い、同じ発言が両方の話者に出る。
