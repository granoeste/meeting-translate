# 出力先プラグインの書き方

出力先プラグインは、発言(文字)を受け取って、どこかに出す Python のファイルです。音声はプラグインに渡しません。使い方と同梱のプラグインの一覧は、[README の「出力先を追加する」](../README.md#出力先を追加するプラグイン)を見てください。

## 置き方

`plugins/<名前>.py` に `class Output` を置き、`plugins.toml` の `plugins` にその名前を足します。

- `plugins/<名前>.toml` があれば、その項目がそのまま `__init__` に渡ります(無ければ何も渡しません)。
- 見本の設定を付けるときは、`plugins/<名前>.example.toml` にします。
- 使い方の説明は、`plugins/<名前>.md` に書きます。

```python
class Output:
    def __init__(self, **settings):  # plugins/<名前>.toml の項目。設定の誤りはここで例外にする
        ...

    def write(self, ev):  # 発言ごとに 1 回
        # ev = {"id", "ts", "speaker", "lang", "text", "translation", "start", "end"}(.jsonl の 1 行と同じ)
        ...

    def close(self):  # 終了時に 1 回(無くてもよい)
        ...
```

見本は [`slack.py`](slack.py) です。

## 呼ばれ方

- `write()` は、プラグインごとの専用スレッドで、発言の順に呼ばれます。ネットワークを待つ処理を書いても、文字起こしと翻訳は止まりません。
- `write()` や `close()` で例外が出ると、stderr に表示して続けます。会議は止まりません。
- 終了時(Ctrl+C か `kill`)は、残りの発言を送り切ってから `close()` を呼びます。5 秒で終わらなければ、残りを送らずに終了します。
- 次のときは、起動時(モデルを読み込む前)にエラーで止まります。
  - `plugins.toml` や `plugins/<名前>.toml` の書き間違い
  - `plugins` に書いたプラグインが無い
  - `__init__` が例外を出した
- 1 つのプラグインは 1 か所にしか出せません。2 か所に出したいときは、プラグインのファイルを別の名前でもう 1 つ作ります(例: `plugins/slack_team.py` と `plugins/slack_team.toml`)。

## 注意

- 秘密の値(Webhook の URL など)を受け取るプラグインは、設定を `plugins/<名前>.toml` から読み、見本を `plugins/<名前>.example.toml` に置きます。`plugins/<名前>.toml` は Git に入らないようにしてありますが、`*.example.toml` は入るので、見本には本物の値を書かないでください。
- プラグインは `main.py` と同じ権限で動きます。中身を確認したものだけを置いてください。
