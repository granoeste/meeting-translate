# meeting-translate

会議の音声をリアルタイムで文字起こしし、日本語⇔英語に翻訳するツールです。すべてローカル(Apple Silicon の GPU)で動き、音声や文字を外部サービスに送りません。

- 文字起こし: [Whisper](https://github.com/openai/whisper) large-v3-turbo(`mlx-whisper`)
- 翻訳: [PLaMo 3 Translate](https://www.preferred.jp/ja/blog/tech/plamo-3-translate-weight-release) 8B MLX-4bit(`mlx-lm`)

## 機能

- 自分の声(マイク)と相手の声(会議音声)を別々に処理し、「自分」「相手」のラベルを付ける
- 言語は自動判定し、日本語なら英語へ、英語なら日本語へ翻訳する(日本語への翻訳は敬体)
- 直前の発話を文脈として翻訳に渡す
- 出力先: 標準出力、Web 画面、OBS の字幕、会議後のテキスト・SRT
- 別の Mac で行っている会議の音声を、LAN 越し(SSH トンネル)で受け取れる

遅延は 1 発話あたり約 3 秒です(文字起こし 約2秒 + 翻訳 約1秒、M1 Max での実測)。

## 仕組み

```
マイク ────────────────┐
(自分)                  │
                        ├→ VAD で発話に区切る → Whisper → 言語判定 → PLaMo 3 Translate ─┬→ 標準出力
会議音声(BlackHole)  ┘   (webrtcvad)                                                   ├→ Web / OBS(SSE)
(相手)                                                                                   └→ .jsonl / .txt / .srt
```

| ファイル | 役割 |
|---|---|
| `main.py` | 音声の取り込み、VAD による発話の切り出し、文字起こしと翻訳の実行、出力 |
| `translate.py` | PLaMo 3 Translate による翻訳(`mlx-lm`) |
| `config.py` | 設定ファイル `config.toml` の読み込みと既定値 |
| `output.py` | Web 画面と OBS 用の小さな HTTP サーバー(SSE) |
| `transcript.py` | 会議後の `.txt` / `.srt` の書き出し |
| `sender.py` | 別の Mac で動かし、音声を LAN 越しに送る |
| `check_audio.py` | BlackHole とマイクの音声の確認(ループバック判定、レベルメーター) |

- 発話の区切りは、300ms の無音で確定します。3 秒を超えた発話は 150ms の短い間でも区切り、6 秒に達したら直前 1 秒の最も静かな所で区切ります。長く切れ目なく話す場面でも、字幕が出るまでの待ちは約 8 秒以内に収まります。
- 区切りで文の途中で終わった発話(末尾が `. ? ! 。` でないもの。`...` で終わるものも含む)は、すぐには訳さず、次の発話と結合してから訳します。次が `merge_wait_s`(既定 4 秒)以内に来なければ、そのまま訳します。訳は自然になりますが、結合した発話は、そのぶん字幕が遅れます(実測で、話し始めてからの待ちの中央値が約 5 秒から約 7 秒に増えました)。`merge_fragments = false` で無効にできます。
- Whisper が無音やノイズから作ってしまう文(「Thank you.」など)は、短くて信頼度が低い場合に捨てます。完全には防げません。
- PLaMo のモデルコード(`modeling_plamo_mlx.py`)は Hugging Face のリポジトリにあり、`trust_remote_code=True` で読み込みます。公式の手順ですが、コードを実行することになります。

## 動作環境

- Apple Silicon の Mac(動作確認は M1 Max / 64GB)
- [uv](https://docs.astral.sh/uv/)、ffmpeg
- ディスク容量 約 7.2GB(モデル。初回起動時に `~/.cache/huggingface/` へダウンロード)。翻訳を 2B にするなら約 3.7GB
- メモリの目安(起動して待機している状態のプロセスの実測。M1 Max / 64GB で測定):

  | 翻訳モデル | プロセスのメモリ | 目安 |
  |---|---|---|
  | 2B(`pfnet/plamo-3-translate-2b-MLX-4bit`) | 約 4.3GB | 8GB 以上 |
  | 8B(既定値) | 約 7.7GB | 16GB 以上 |

  翻訳や文字起こしの実行中は、もう少し増えます。会議アプリやブラウザも同時に動かすので、余裕を見てください。目安の列は実機では確かめていません(測ったのは 64GB の Mac だけです)。31B は測っていません。
- 相手の声を取るには [BlackHole](https://existential.audio/blackhole/)(仮想オーディオデバイス)

## セットアップ

```
cd meeting-translate
brew install ffmpeg blackhole-2ch   # BlackHole は管理者パスワードが必要。インストール後に再起動
uv sync
```

初回起動時にモデルをダウンロードし、読み込みに時間がかかります。

## 使い方

### 1. 同じ Mac の会議を処理する

会議アプリの音声を BlackHole にも流す設定をします。

1. 「Audio MIDI 設定」を開く
2. 「複数出力デバイス」を作り、スピーカー(またはヘッドホン)と BlackHole 2ch の両方にチェックを入れる
3. システムの出力先(または会議アプリの出力先)をそのデバイスにする
4. システムの入力はマイクのままにする(BlackHole にしない)

入力が BlackHole になっていると、`--mic` なしで起動したときに「自分」も BlackHole を拾い、同じ発言が「自分」と「相手」の両方に出ます。設定は[音声の確認](#音声の確認)の手順で確かめられます。

```
uv run python -u main.py
```

BlackHole が見つからないときは、マイクのみで起動します。デバイスの一覧は `uv run python main.py --list` で確認できます。

### 2. 別の Mac の会議を処理する(SSH トンネル)

会議 Mac で BlackHole と複数出力デバイスを設定し(上記 1 と同じ)、「システム設定 → 一般 → 共有 → リモートログイン」を有効にします。自分の声も会議 Mac 経由にする構成です。

```
会議Mac  BlackHole(相手の声) → sender.py :9999 ┐
         マイク(自分の声)    → sender.py :9998 ┤ ssh -L
                                                 ▼
この Mac main.py --no-mic --remote 127.0.0.1:9999 --remote-self 127.0.0.1:9998
```

**会議 Mac**: `sender.py` を置いて、別々のターミナルで 2 つ起動します。

```
uv run sender.py                               # 相手の声(BlackHole)、ポート 9999
uv run sender.py --device default --port 9998  # 自分の声(マイク)、ポート 9998
```

初回はマイクへのアクセス許可が必要です。`sender.py` は 127.0.0.1 のみで待ち受けます。

**この Mac**: トンネルを張り、起動します。

```
ssh -N -L 9999:127.0.0.1:9999 -L 9998:127.0.0.1:9998 <ユーザー名>@<会議Mac>.local

uv run python -u main.py --no-mic --remote 127.0.0.1:9999 --remote-self 127.0.0.1:9998
```

接続が切れても、自動で再接続します。

> 会議 Mac では、ヘッドホンかイヤホンを使ってください。スピーカーだと、会議 Mac のマイクが相手の声を拾い、同じ発言が「自分」と「相手」の両方に出ます。

## 音声の確認

`check_audio.py` で、BlackHole とマイクに音が届いているかを確かめられます。

```
uv run python check_audio.py loop     # BlackHole の出力 → 入力が通るかを判定
uv run python check_audio.py meter    # 入力レベルをリアルタイムで表示(Ctrl+C で終了)
```

- `loop`: BlackHole に左 1kHz・右 1.5kHz のテスト音を 2 秒流し、BlackHole の入力に戻ってくるかを周波数とレベルで判定して、`PASS` か `FAIL` を表示します。左右の取り違えも分かります。完全な無音のときは、ターミナルにマイクの権限がないことが多いです(システム設定 → プライバシーとセキュリティ → マイク)。
- `meter`: BlackHole とデフォルト入力のレベルを並べて表示します。相手の声がマイク側にも入っていないかを確かめられます。デバイスは名前の一部で指定できます(例: `meter BlackHole "MacBook Pro"`)。デフォルト入力が BlackHole のときは警告を出します。

BlackHole にテスト用の音声を流すには、macOS の `say` を使います。

```
say -a "BlackHole 2ch" "これはテストです"
say -v Samantha -a "BlackHole 2ch" "Hello, this is a test"
```

`main.py` を起動した状態で流し、「相手」の発言として訳文が出れば、相手側の経路は正常です。

## 出力

| 出力先 | 方法 |
|---|---|
| 標準出力 | 通常は整形表示。`--jsonl` で 1 行 1 JSON(ログ類は stderr) |
| Web 画面 | `http://127.0.0.1:8765/` に話者別のログが流れる |
| OBS | ブラウザソースに `http://127.0.0.1:8765/caption` を指定。背景は透過で、最新の字幕を下部中央に表示 |
| ファイル | `transcripts/YYYYmmdd-HHMMSS.{jsonl,txt,srt}` |

- OBS の URL パラメータ: `?hold=5` で字幕の表示秒数(デフォルト 8 秒)、`?orig=0` で原文を非表示にして訳文のみ。ブラウザソースの大きさは 1920 × 1080 程度を推奨します。
- Web サーバーは 127.0.0.1 のみで待ち受けます。ネットワーク上の他の端末からは見えません。
- `.jsonl` は発話のたびに追記します。終了時(Ctrl+C か `kill`)に `.txt` と `.srt` を書き出します。強制終了した場合は、次のコマンドで再生成できます。
  ```
  uv run python transcript.py transcripts/xxxx.jsonl
  ```
- SRT は、訳文(上段)と原文(下段)の 2 行字幕で、話者ラベルが付きます。時刻は、起動した瞬間を 00:00:00 とし、発話の区切り位置から逆算した近似値です。録画と合わせるときは、1 秒程度ずれることがあります。

## オプション

| オプション | 説明 |
|---|---|
| `--mic NAME` | 使うマイクのデバイス名(省略時はシステムのデフォルト) |
| `--no-mic` | このマシンのマイクを使わない |
| `--system NAME` | ループバックデバイス名(デフォルト `BlackHole`) |
| `--remote HOST:PORT` | `sender.py` から「相手」の声を受信する |
| `--remote-self HOST:PORT` | `sender.py` から「自分」の声を受信する |
| `--port N` | Web / OBS サーバーのポート(デフォルト 8765) |
| `--no-web` | Web / OBS サーバーを起動しない |
| `--jsonl` | 標準出力を JSON Lines にする |
| `--out-dir DIR` | 保存先(デフォルト `transcripts`) |
| `--no-record` | 保存しない |
| `--save-audio` | 入力ごとの生の音声を `--out-dir` に `.wav` で保存する(「言葉が抜ける」などの問題を、あとで再現して調べるため)。会議の音声そのものが残るので、扱いに注意 |
| `--config PATH` | 設定ファイル(省略時は `main.py` と同じ場所の `config.toml` があれば読む) |
| `--list` | 音声デバイスの一覧を表示して終了 |
| `--debug` | 文字起こしの信頼度、遅延、フィルタで捨てた発話とその理由を stderr に表示 |

`sender.py` のオプション: `--device NAME`(デフォルト `BlackHole`、`default` でマイク)、`--port N`(デフォルト 9999)。

## 設定ファイル

`config.toml` に書いた項目だけが既定値を上書きします。ファイルが無ければ既定値で動きます。見本の `config.example.toml` をコピーし、変えたい行の `#` を外して書き換えてください。

```
cp config.example.toml config.toml
```

| 項目 | 既定値 | 説明 |
|---|---|---|
| `silence_end_ms` | `300` | この長さの無音で発話を確定する(ミリ秒) |
| `soft_segment_s` | `3.0` | 発話がこの長さ(秒)を超えたら、`soft_silence_ms` の短い間でも区切る |
| `soft_silence_ms` | `150` | 上の条件で使う短い間(ミリ秒) |
| `max_segment_s` | `6.0` | 1 つの発話の最長(秒)。超えたら直前 1 秒の最も静かな所で区切る |
| `min_segment_s` | `0.4` | これより短い発話は捨てる(秒) |
| `merge_fragments` | `true` | 文の途中で終わった発話は、次の発話と結合してから訳す |
| `merge_wait_s` | `4.0` | 結合のために次の発話を待つ最長の時間(秒)。来なければ、そのまま訳す |
| `merge_max_s` | `12.0` | 結合した発話がこの長さ(秒)を超えたら、文が終わっていなくても訳す |
| `whisper_model` | `"mlx-community/whisper-large-v3-turbo"` | Whisper のモデル |
| `short_min_logprob` | `-1.0` | 2.5 秒未満の発話の平均 logprob がこれより低ければ、ハルシネーションとして捨てる(0 以下)。小さくすると本物の短い発話が残りやすく、大きくすると誤認識を捨てやすい |
| `hallucinations` | `["thank you", ...]` | 2 秒未満の発話がこれと一致したら捨てる定番フレーズ |
| `translate_model` | `"pfnet/plamo-3-translate-8b-MLX-4bit"` | 翻訳モデル。遅い場合は `...-2b-MLX-4bit`、品質重視なら `...-31b-MLX-4bit` |
| `pair` | `{ ja = "en", en = "ja" }` | 判定した言語 → 翻訳先の言語。ここに無い言語の発話は無視する |
| `fixed_lang` | `{}` | 話者(`自分` / `相手`)ごとに言語を固定する。例: `{ "相手" = "en" }`。固定した話者は言語の自動判定をしない |

- 日本語の発音の英語が `ja` と誤判定されたとき、本文が英字だけなら `en` に直します(`fixed_lang` で固定した話者は対象外)。
- 起動時に、読み込んだファイルのパスを表示します。
- 項目名の書き間違いや型の違い(数値の欄に文字列など)は、起動時にエラーになります。
- 別のファイルを使うときは `--config PATH` で指定します。
- `config.toml` は `.gitignore` に入っているので、コミットされません。

## 注意点

- **ライセンス**: PLaMo 3 Translate は PLaMo Community License です。業務での利用は商用利用にあたる可能性が高く、その場合は専用フォームでの事前登録が必要です。社内の担当に確認してください。
- **会議の周知**: 他の参加者の発言を文字起こしするので、周知や同意が必要な場合があります。
- **機密性**: `transcripts/` には会議の全文が平文で残ります。共有ストレージや Git、社外のリポジトリに入れないでください。
- **SSH トンネル**: 音声は暗号化されていない生の PCM なので、必ず SSH トンネル経由にしてください。`sender.py` を 127.0.0.1 以外で待ち受けさせないでください。
- **話者分離**: 「相手」は 1 つにまとまります。複数の参加者は区別しません。
- **ハルシネーション**: 無音やノイズで、Whisper が「You」などの文を作ることがあります。短い断片には除外ルールがありますが、長い無音区間では出る場合があります。
- **ヘッドホン**: スピーカーの音をマイクが拾うと、同じ発言が重複して出ます。
- **会議 Mac のマイク**: 会議 Mac のスピーカーから音を出したままマイクを使うと、相手の声がマイクに入り、9998 で「自分」として転送されます。この Mac で `--no-mic` を付けても防げません(拾っているのは会議 Mac のマイクです)。会議 Mac ではヘッドホンを使うか、自分が話さない間はマイク側(9998)の `sender.py` を止めてください。

## モデルの保存場所

どちらも Hugging Face の標準キャッシュ(`~/.cache/huggingface/hub/`)にあります。

| モデル | サイズ |
|---|---|
| `pfnet/plamo-3-translate-8b-MLX-4bit` | 約 5.7GB |
| `pfnet/plamo-3-translate-2b-MLX-4bit`(2B に切り替えたとき) | 約 2.2GB |
| `mlx-community/whisper-large-v3-turbo` | 約 1.5GB |

削除するときは、`models--pfnet--plamo-3-translate-8b-MLX-4bit` と `models--mlx-community--whisper-large-v3-turbo` のディレクトリを消してください。次回起動時に再ダウンロードされます。
