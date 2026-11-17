# slack: Slack に流す(見本)

`plugins/slack.py` は、発言ごとに、話者・訳・原文を 1 つのメッセージで Slack に投稿します。プラグインの見本として、標準ライブラリだけで書いてあります。

## 使い方

コマンドは、リポジトリの一番上(`meeting-translate/`)で実行します。

1. Slack で、投稿先のチャンネルの Incoming Webhook を作り、URL を控える。
2. 使うプラグインを書く。
   ```
   cp plugins.example.toml plugins.toml
   ```
   `plugins.toml` を `plugins = ["web", "slack"]` に書き換える(Web/OBS が要らなければ `["slack"]`)。
3. Slack の設定を書く。
   ```
   cp plugins/slack.example.toml plugins/slack.toml
   ```
   `plugins/slack.toml` の `webhook_url` を、控えた URL に書き換える。
4. いつもどおり `main.py` を起動する。起動時に `プラグイン: web, slack (…/plugins.toml)` と表示されます。

## 設定(`plugins/slack.toml`)

見本は [`slack.example.toml`](slack.example.toml) です。

| 項目 | 既定値 | 説明 |
|---|---|---|
| `webhook_url` | (必須) | Incoming Webhook の URL(`https://hooks.slack.com/` で始まる) |
| `timeout` | `10` | 1 回の投稿を待つ最長の秒数 |

## 注意

- 投稿が速すぎて Slack に断られた(HTTP 429)ときは、待ってから送り直します。
- Webhook の URL を知っていれば、誰でもそのチャンネルに投稿できます。`plugins/slack.toml` を人に渡したり、公開したりしないでください。
- 投稿先のチャンネルに誰がいるかを確認してください。会議の発言がそのまま流れます。社外の人が入っているチャンネル(Slack コネクト)にも注意してください。
- 2 つのチャンネルに流したいときは、プラグインのファイルを別の名前でもう 1 つ作ります(例: `plugins/slack_team.py` と `plugins/slack_team.toml`)。1 つのプラグインは 1 か所にしか出せません。
