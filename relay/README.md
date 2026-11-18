# relay: 会場の参加者に公開する

カンファレンスなどで、会場の参加者が自分のスマホで翻訳を見られるようにします。手元の Mac は、発言(文字だけ)を Cloudflare Workers の中継に送ります。参加者は、限定公開の URL を開きます。手元の Mac はインターネットに公開しません。

```
会場 A の Mac: main.py → plugins/relay.py ─┐
会場 B の Mac: main.py → plugins/relay.py ─┼─HTTPS─▶ Cloudflare Worker(relay/)──WebSocket──▶ 参加者のスマホ
                                          ┘          https://meeting-relay.<サブドメイン>.workers.dev/<閲覧キー>/<会場>/
```

- 会場(チャンネル)を複数にできます。会場ごとに履歴が分かれ、参加者は閲覧ページの上のメニューで会場を切り替えられます。会場が 1 つのときは、メニューは出ません。
- URL に入っている閲覧キーを知っている人だけが見られます。検索エンジンには載らないようにしてあります。閲覧キーは全会場で共通です。
- 発言を書き込むには、閲覧キーとは別の送信トークンが要ります。送信トークンは全会場で共通なので、どの会場の Mac からでも、どの会場にも書き込めます(`/clear` も同じ)。各 Mac の `plugins/relay.toml` の `channel` を間違えないようにしてください。
- 履歴は中継に残ります。途中から開いた人にも、直近 250 件を表示します。スマホの回線が切れても、つながり直すと続きから表示します。
- ページを表示している間は、スマホの画面が消えないようにしています(そのぶん電池を使います)。対応していないブラウザや省電力モードでは、いつもどおり消えます。
- Cloudflare の無料プランで動く見込みです(Durable Objects も含めて、1 日 10 万リクエストまで)。参加者 300 人が 8 時間見る場合で、枠の 2 割ほどと見積もっています。枠はアカウント全体で 1 つなので、会場を増やしたときは全会場の参加者の合計で考えてください。超えると、その日はそれ以上の接続や送信が失敗します。枠は毎日 0:00 UTC(日本時間の 9:00)に戻ります。

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
2. 会場が複数あるときは、`relay/wrangler.toml` の `CHANNELS` に会場を並べる(1 会場なら既定の `"main:Main"` のままでかまいません)。
   ```toml
   CHANNELS = "hall-a:Room A,hall-b:Room B"
   ```
   「ID:表示名」をカンマで区切ります。ID は英小文字・数字・`-`(32 文字まで。`ws` は中継が URL に使うので使えません)で、URL と各 Mac の設定に使います。表示名は参加者に見えます。1 つ目の会場が、`/<閲覧キー>/` を開いたときの既定です。会場を変えたら、次の手順の deploy をし直します。
   - ID `main` は、会場が 1 つだったころの履歴をそのまま使います。`channel` を書いていない Mac は `main` に送るので、一覧から `main` を外すときは、全部の Mac に `channel` を書いてください。
   - 書き方が誤っている会場(使えない文字や、2 回目の同じ ID など)は、中継が飛ばします。その会場だけ 404 になり、その会場の Mac の `main.py` は起動時の確認でエラーになって止まります。ほかの会場はそのまま動きます。正しい会場が 1 つも無いときは、全会場で 503 を返します。deploy したら閲覧ページを開いて確かめてください。原因は `npx wrangler@4.156.2 tail` で見られます。
   - 一覧から外した会場の履歴は、中継に残ります。同じ ID を足し直すと、前の発言がまた見えます。外す前でも後でも、手順 7 の `/clear/<会場の ID>` で消せます。
3. 中継を置く。表示された `https://meeting-relay.<サブドメイン>.workers.dev` を控えます。
   ```
   npx wrangler@4.156.2 deploy
   ```
   初めてのときは、workers.dev のサブドメインを決めます。サブドメインは参加者全員に見えるので、個人名などを含まない名前にします。
4. 閲覧キーと送信トークンを作って登録する。次のコマンドを 2 回実行し、別々の値を使います。
   ```
   python3 -c "import secrets; print(secrets.token_urlsafe(24))"
   ```
   ```
   npx wrangler@4.156.2 secret put VIEW_KEY
   npx wrangler@4.156.2 secret put PUBLISH_TOKEN
   ```
   値は、聞かれたときに貼り付けます(シェルの履歴に残りません)。両方を登録するまで、中継は何も表示しません(503)。
5. 各会場の Mac で、手元の設定を書く。
   ```
   cd ..
   cp plugins.example.toml plugins.toml        # まだ無ければ
   cp plugins/relay.example.toml plugins/relay.toml
   ```
   `plugins.toml` を `plugins = ["web", "relay"]` にします。`plugins/relay.toml` の `url` に 3 の URL を、`token` に送信トークンを、`channel` にその Mac の会場の ID を書きます。
6. `main.py` を起動し、`https://meeting-relay.<サブドメイン>.workers.dev/<閲覧キー>/<会場の ID>/` をスマホで開いて、発言が流れることを確かめる。
7. 本番の前に、テストの発言を会場ごとに消す。開いているページからも消えます。
   ```
   curl -X POST -H "Authorization: Bearer <送信トークン>" https://meeting-relay.<サブドメイン>.workers.dev/clear/<会場の ID>
   ```
8. 閲覧用の URL を QR コードにして、スライドなどに載せる。会場ごとに `/<閲覧キー>/<会場の ID>/` の QR コードを作れば、その会場が最初から開きます。QR コードは、Chrome の「QR コードを作成」(ページを右クリック)などで手元で作ってください。オンラインの生成サービスに URL を渡すと、閲覧キーが外に出ます。

当日は、Cloudflare のダッシュボードで Worker のリクエスト数を確かめられます。リハーサルで数を見ておくと安心です。

## 設定(`plugins/relay.toml`)

見本は [`plugins/relay.example.toml`](../plugins/relay.example.toml) です。送信トークンを知っていれば、誰でも中継に書き込めます。`plugins/relay.toml` を人に渡したり、公開したりしないでください。

| 項目 | 既定値 | 説明 |
|---|---|---|
| `url` | (必須) | `npx wrangler deploy` で表示された Worker の URL。パス(閲覧キー)は付けない |
| `token` | (必須) | `secret put PUBLISH_TOKEN` で登録した送信トークン(16 文字以上) |
| `channel` | `"main"` | この Mac の会場の ID。`relay/wrangler.toml` の `CHANNELS` に書いたもののうち、`:` の前の ID だけ |
| `timeout` | `10` | 1 回の送信を待つ最長の秒数 |

`main.py` は起動時(モデルを読み込む前)に中継へ問い合わせ、送信トークンが違うときや、`channel` が中継の会場の一覧に無いときは、エラーで止まります。何も書き込みません。中継にまだつながらないとき(中継を起動する前など)は、警告だけ出して起動を続けます。

## 終わったら

- 会場ごとに `/clear/<会場の ID>` で履歴を消してから、`npx wrangler@4.156.2 delete` で中継を消します。`CHANNELS` から外した会場も、同じように消せます。
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

複数の会場を試すときは、`wrangler.toml` を書き換えずに、起動するときに会場の一覧を渡せます。

```
npx wrangler@4.156.2 dev --var "CHANNELS:hall-a:Room A,hall-b:Room B"
```
