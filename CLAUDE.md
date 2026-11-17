# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

会議の音声をローカル(Apple Silicon)で文字起こしし、日本語⇔英語に翻訳するツール。利用者向けの説明は日本語で、全体は `README.md`、出力先プラグインごとの説明は `plugins/<名前>.md`(`relay` だけは `relay/README.md`)、プラグインの書き方は `plugins/README.md` にある。機能やオプションを変えたら、該当する説明も合わせて直す。プラグインを足したら、`plugins/<名前>.md` を書き、README の「出力先を追加する」の表に足す。

## コマンド

```
uv sync                                         # 依存の導入(Python 3.11 以上。tomllib を使う)
uv run python -u main.py                        # 起動。--list でデバイス一覧、--debug で Whisper の信頼度を表示
uv run python -u main.py --mic "Shure" --no-record
uv run python check_audio.py loop               # BlackHole の出力 → 入力のループバック判定(PASS/FAIL)
uv run python check_audio.py meter              # 入力レベルのメーター(Ctrl+C で終了)
uv run python transcript.py transcripts/xxx.jsonl   # .jsonl から .txt/.srt を作り直す
say -a "BlackHole 2ch" "テストです"             # 「相手」側にテスト音声を流す
cd relay && npx wrangler@4.156.2 dev            # 会場向けの中継を手元で動かす(relay/.dev.vars に VIEW_KEY と PUBLISH_TOKEN)
```

テストとリンターは無い。構文の確認は `uv run python -m py_compile *.py plugins/*.py` と `node --check relay/src/index.js`。

- モデル(計 約 7.2GB)は初回起動時に Hugging Face からダウンロードされる。手元に無いときは、確認のためだけに `main.py` を起動しない。
- モデル抜きで配線を確かめるときは、`main` を import する前に `sys.modules["mlx_whisper"]` と `sys.modules["mlx_lm"]` をスタブに差し替える。`main.main()` は `signal.signal` を呼ぶので、メインスレッドから呼ぶ。終了時の動き(Ctrl+C など)を確かめるときは、`import main` ではなく `runpy.run_path("main.py", run_name="__main__")` で本番と同じく起動する。import だと終了処理の順番が変わり、本番で起きる問題が再現しないことがある。発言を流すには、`say --file-format=WAVE --data-format=LEI16@16000 -o x.wav "..."` で作った音声を 30ms ずつ TCP で送り、`--remote 127.0.0.1:PORT` で受ける。マイクも BlackHole も要らない。

## 全体の流れ

1. 音声源ごとにスレッドを立てる(`main.py`)。ローカルのデバイスは `capture()`(sounddevice)、別 Mac の `sender.py` からは `capture_remote()`(TCP、切断時は自動で再接続)。
2. どちらも `segment()` に 30ms(16kHz・mono・int16 で 480 サンプル)のフレームを渡す。webrtcvad で発話に区切り、グローバルの `segments` キューに `(話者ラベル, 音声, 開始, 終了)` を積む。
3. ワーカースレッドは 1 本だけ。Whisper で文字起こしし、ハルシネーション対策のフィルタ(`no_speech_prob`、短く低信頼度の断片、定番フレーズ)を通す。そのあと `Translator` で翻訳する。直前の同じ話者の発話を `context` として渡す。末尾が文末で終わらない発話は `pending` に保留し、次の発話(同じ話者・同じ言語)と結合してから訳す(`merge_wait_s` を過ぎたらそのまま訳す。終了時は `segments` に `None` を入れて、保留分を訳させる)。
4. `emit()` でイベントを作り、`plugins.publish()`(出力先プラグイン。既定は Web/OBS の `web`)、`Recorder.write()`(`.jsonl` に追記)、標準出力に出す。終了時(Ctrl+C / SIGTERM)に、プラグインに残りを送り切らせて `.txt` と `.srt` を書き出す。

複数のファイルにまたがる約束事:

- **イベントの形** `{id, ts, speaker, lang, text, translation, start, end}` は、`main.py` の `emit()` で作り、`.jsonl`、`transcript.py` の書き出し、出力先プラグインの `write(ev)`(`web` の SSE を含む)で共通。`relay/src/index.js` の `parseEvent()` は項目と型を決め打ちで検査しているので、形を変えるときはそこも直す。`start`/`end` は起動時刻からの秒数。
- **話者ラベル**の `"自分"` と `"相手"` は文字列そのままで使っている。`plugins/web.py` の Web 画面と `relay/src/viewer.html` の JavaScript も `'自分'` で色を分けているので、変えるときは一緒に直す。
- **`sender.py` の通信**はヘッダの無い生 PCM(16kHz・mono・int16、480 サンプルずつ)。`main.py` の `SR`/`FRAME` と合わせる。`sender.py` は会議用の Mac で単体で動かすので(PEP 723 のインライン依存)、プロジェクトの他のモジュールを import しない。
- **設定**は `config.py` の `Config`(dataclass)が既定値で、`config.toml` に書いた項目だけが上書きする。型チェックは既定値の型を見ている。項目を足すときは、`Config`、`config.example.toml`、README の表の 3 か所を直す。`main.py` では、モジュールのグローバル変数 `cfg` を `main()` の中で読み込んだ値に置き換え、各関数は実行時に `cfg.xxx` を参照する。
- **終了処理**: `main()` を抜ける前に `stopping` を立て、`capture` スレッドを join して音声ストリームを閉じる。`stream.read()` の中で待っているスレッドを残したまま終了すると、sounddevice が PortAudio を外した後にそのスレッドが落ちる(SIGSEGV)。PortAudio を使うスレッドを足すときも、同じく終了前に止める。
- **出力先プラグイン**(`plugins/__init__.py`): `plugins.toml` は使うプラグインの名前の一覧だけ(`plugins = ["web", "slack"]`)。`plugins.toml` は各自の設定でコミットしない(見本は `plugins.example.toml`)。無いときは `DEFAULT_PLUGINS`(`["web"]`)を使うので、web は既定で動く。`plugins/web.toml`(`port`)は秘密の値が無いのでコミットしてある。`main.py` は個々のプラグインを知らない(`web` を特別扱いするオプションも置かない)。名前ごとに `plugins/<名前>.py` の `Output(**settings)` を作る。`settings` は `plugins/<名前>.toml` の中身(無ければ空)。インターフェースは `write(ev)` と任意の `close()`。プラグインごとに専用スレッドとキューを持つので、`write()` は待ってよい。例外は stderr に出して続ける。設定の誤りは起動時(モデルの読み込み前)に `SystemExit` で止める。読み込み役は `plugins` パッケージの `__init__.py` で、プラグインは同じ `plugins/` に置き、`importlib` でファイルから読む。`plugins.py` を別に作ると `plugins/` と名前がぶつかるので作らない。同梱の `plugins/slack.py` は見本なので、標準ライブラリだけで書き、依存を増やさない。プラグインの設定の見本は `plugins/<名前>.example.toml` にする(`plugins/*.toml` は `.gitignore` 済みで、`*.example.toml` と秘密の値の無い `web.toml` だけ例外)。
- **会場向けの中継**(`relay/`): Cloudflare Worker と Durable Object(SQLite)。`plugins/relay.py` が `POST /publish`(`Authorization: Bearer <PUBLISH_TOKEN>`)で発言を送り、DO が参加者のスマホに WebSocket(Hibernation API)で配る。閲覧ページは `/<VIEW_KEY>/`、テストの発言を消すのは `POST /clear`。`relay/` はリポジトリでただ1つの JavaScript で、Python のモジュールを import しない。wrangler は `npx wrangler@4.156.2` で版を固定し、`package.json` と lock ファイルは置かない(lock に社内の npm proxy の URL が入るため)。`VIEW_KEY` と `PUBLISH_TOKEN` は `wrangler secret`(手元では `relay/.dev.vars`)に入れ、リポジトリに書かない。`plugins/relay.py` は起動ごとの `session` を足して送り、DO は `(session, id)` で再送の重複を捨てる(`main.py` を再起動すると `id` が 0 に戻るため)。配信の番号 `seq` は AUTOINCREMENT で、`/clear` の後も使い回さない。閲覧ページは `?after=<最後の seq>` でつなぎ直し、最初に届く `hello {latest, oldest}` で、切れている間に消された表示を消す。接続が生きているかは、画面に戻ったときと、60 秒何も届かないときに `"ping"` を送って確かめる。DO は `setWebSocketAutoResponse` で、眠ったまま `"pong"` を返す。返事が無ければつなぎ直す。つなぎ直すたびに Worker と DO のリクエストを1つずつ使い、無料枠(どちらも1日 10 万)を減らすので、つなぎ直す場面を増やすときは ping で済まないか先に考える。手元で試すときは `wrangler dev`(アカウント不要、127.0.0.1:8787)を使い、WebSocket のクライアントは Node の標準の `WebSocket` で書ける。
- **翻訳**(`translate.py`)は、`translate_model` の名前でプロンプトを選ぶ。PLaMo は chat template に `source_lang`/`target_lang`/`context` を渡し、日本語にするときは `form="polite"` を付ける。名前に `translategemma` か `hy-mt` が入っていれば、それぞれの形式にする(どちらも `context` は使わない)。TranslateGemma は `<end_of_turn>` を終了トークンに足さないと、`max_tokens` まで出し続ける。`generate()` は遅かったので `stream_generate()` を使っている。

## 守ること

- 既定では音声と文字を外部サービスに送らない(README で約束している)。外部に送るのは、利用者が `plugins.toml` で有効にした出力先プラグインだけ。音声はプラグインにも渡さない。`relay` は、閲覧キーを知っている人なら誰でも見られる場所に文字を出すので、閲覧ページに話者ラベルなどの余計な情報を足さない。Web サーバー(`plugins/web.py`)と `sender.py` は `127.0.0.1` でしか待ち受けない(`web` の待ち受けアドレスは設定項目にしない)。LAN 越しは SSH トンネルを前提にしているので、待ち受けアドレスを広げない。
- このリポジトリは GitHub の公開リポジトリ。`transcripts/`(会議の全文)、`config.toml`(各自の設定)、`plugins.toml`(使うプラグインの一覧)、`plugins/*.toml`(Webhook の URL など。`*.example.toml` と `web.toml` を除く)、`uv.lock`(社内のパッケージプロキシの URL が入る)、`relay/.dev.vars`(手元で試すときの閲覧キーと送信トークン)、`relay/.wrangler/` はコミットしない(`.gitignore` 済み)。
- この Mac では、デフォルト入力が BlackHole になっていることがある。その状態で `--mic` を付けずに起動すると、「自分」も BlackHole を拾い、同じ発言が両方の話者に出る。
