# relay: 会場の参加者に公開する

カンファレンスなどで、会場の参加者が自分のスマホで翻訳を見られるようにします。手元の Mac は、発言(文字だけ)を Cloudflare Workers の中継に送ります。参加者は、限定公開の URL を開きます。手元の Mac はインターネットに公開しません。

```
main.py → plugins/relay.py ──HTTPS──▶ Cloudflare Worker(relay/)──WebSocket──▶ 参加者のスマホ
                                       https://meeting-relay.<サブドメイン>.workers.dev/<閲覧キー>/
```

- URL に入っている閲覧キーを知っている人だけが見られます。検索エンジンには載らないようにしてあります。
- 発言を書き込むには、閲覧キーとは別の送信トークンが要ります。
- 履歴は中継に残ります。途中から開いた人にも、直近 250 件を表示します。スマホの回線が切れても、つながり直すと続きから表示します。
- ページを表示している間は、スマホの画面が消えないようにしています(そのぶん電池を使います)。対応していないブラウザや省電力モードでは、いつもどおり消えます。
- Cloudflare の無料プランで動く見込みです(Durable Objects も含めて、1 日 10 万リクエストまで)。参加者 300 人が 8 時間見る場合で、枠の 2 割ほどと見積もっています。超えると、その日はそれ以上の接続や送信が失敗します。枠は毎日 0:00 UTC(日本時間の 9:00)に戻ります。

| ファイル | 役割 |
|---|---|
| `../plugins/relay.py` | 手元の Mac で動く出力先プラグイン。発言を中継に送る |
| `wrangler.toml` | Worker の設定(秘密の値は書かない) |
| `src/index.js` | Worker と Durable Object。発言を受け取り、履歴に残して、参加者に配る |
| `src/viewer.html` | 参加者がスマホで開く閲覧ページ |

## 置き方

用意するもの: Cloudflare のアカウント(無料)と Node.js(`npx` を使います)。コマンドは、リポジトリの一番上(`meeting-translate/`)から始めます。

1. Cloudflare にログインする(ブラウザが開きます)。
   ```
   cd relay
   npx wrangler@4.156.2 login
   ```
2. 中継を置く。表示された `https://meeting-relay.<サブドメイン>.workers.dev` を控えます。
   ```
   npx wrangler@4.156.2 deploy
   ```
   初めてのときは、workers.dev のサブドメインを決めます。サブドメインは参加者全員に見えるので、個人名などを含まない名前にします。
3. 閲覧キーと送信トークンを作って登録する。次のコマンドを 2 回実行し、別々の値を使います。
   ```
   python3 -c "import secrets; print(secrets.token_urlsafe(24))"
   ```
   ```
   npx wrangler@4.156.2 secret put VIEW_KEY
   npx wrangler@4.156.2 secret put PUBLISH_TOKEN
   ```
   値は、聞かれたときに貼り付けます(シェルの履歴に残りません)。両方を登録するまで、中継は何も表示しません(503)。
4. 手元の設定を書く。
   ```
   cd ..
   cp plugins.example.toml plugins.toml        # まだ無ければ
   cp plugins/relay.example.toml plugins/relay.toml
   ```
   `plugins.toml` を `plugins = ["web", "relay"]` にします。`plugins/relay.toml` の `url` に 2 の URL を、`token` に送信トークンを書きます。
5. `main.py` を起動し、`https://meeting-relay.<サブドメイン>.workers.dev/<閲覧キー>/` をスマホで開いて、発言が流れることを確かめる。
6. 本番の前に、テストの発言を消す。開いているページからも消えます。
   ```
   curl -X POST -H "Authorization: Bearer <送信トークン>" https://meeting-relay.<サブドメイン>.workers.dev/clear
   ```
7. 閲覧用の URL を QR コードにして、スライドなどに載せる。QR コードは、Chrome の「QR コードを作成」(ページを右クリック)などで手元で作ってください。オンラインの生成サービスに URL を渡すと、閲覧キーが外に出ます。

当日は、Cloudflare のダッシュボードで Worker のリクエスト数を確かめられます。リハーサルで数を見ておくと安心です。

## 設定(`plugins/relay.toml`)

見本は [`plugins/relay.example.toml`](../plugins/relay.example.toml) です。送信トークンを知っていれば、誰でも中継に書き込めます。`plugins/relay.toml` を人に渡したり、公開したりしないでください。

| 項目 | 既定値 | 説明 |
|---|---|---|
| `url` | (必須) | `npx wrangler deploy` で表示された Worker の URL。パス(閲覧キー)は付けない |
| `token` | (必須) | `secret put PUBLISH_TOKEN` で登録した送信トークン(16 文字以上) |
| `timeout` | `10` | 1 回の送信を待つ最長の秒数 |

## 終わったら

- `/clear` で履歴を消してから、`npx wrangler@4.156.2 delete` で中継を消します。
- 次の会場でも使うときは、`secret put VIEW_KEY` で閲覧キーを入れ替えます。前の URL では見られなくなります。

## 注意

- マイクが拾った話は、質疑や休憩中の会話も含めて、全部文字にして公開されます。休憩中は `main.py` を止めてください(Ctrl+C)。
- 他の人の講演を翻訳して出すときは、登壇者や運営の了承を取ってください。
- ページには話者ラベル(「自分」「相手」)を出しません。言語の向き(例: `English → 日本語`)と時刻を出し、話者は色で分けます。
- 中継に届かないときは、1 件ごとに最長 60 秒送り直します。それでも届かない発言は送らず、stderr に表示します。手元の表示や保存は止まりません。
- Cloudflare を使う前に、社内の手続き(External Service Review など)が要るか確認してください。

## 手元で試す

アカウントは要りません。`relay/.dev.vars` に、試すための閲覧キーと送信トークンを書きます。

```
VIEW_KEY=<閲覧キー>
PUBLISH_TOKEN=<送信トークン>
```

中継を手元で動かします。

```
cd relay
npx wrangler@4.156.2 dev
```

`plugins/relay.toml` の `url` を `http://127.0.0.1:8787` にして `main.py` を起動し、`http://127.0.0.1:8787/<閲覧キー>/` を開きます。
