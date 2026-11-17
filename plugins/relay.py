"""出力先プラグイン: 発言を Cloudflare の中継(relay/)に送り、会場の参加者が自分のスマホで見られるようにする。

plugins.toml:        plugins = ["web", "relay"]
plugins/relay.toml:  url と token と channel(見本は plugins/relay.example.toml)。中継の置き方は relay/README.md

送るのはイベント(文字)だけで、音声は送らない。標準ライブラリだけで動く。
"""
import http.client
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

RETRY_FOR = 60  # 中継に届かないときに、1 件を送り直し続ける最長の秒数
CHANNEL_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")  # relay/src/index.js の CHANNEL_ID と合わせる


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


class Output:
    def __init__(self, url, token, channel="main", timeout=10):
        u = urllib.parse.urlsplit(url) if isinstance(url, str) else None
        local = u is not None and u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")  # wrangler dev で試すとき
        if u is None or not ((u.scheme == "https" and u.hostname) or local):
            raise ValueError("url は https:// で始まる中継の URL にしてください(手元で試すときだけ http://127.0.0.1:8787 も使えます)")
        if u.path not in ("", "/") or u.query or u.fragment:
            raise ValueError("url は https://meeting-relay.xxxx.workers.dev のように、パスを付けずに書いてください(閲覧キーは書きません)")
        if not isinstance(token, str) or len(token) < 16:
            raise ValueError("token には、wrangler secret put PUBLISH_TOKEN で登録した送信トークン(16 文字以上)を書いてください")
        if type(timeout) not in (int, float) or timeout <= 0:
            raise ValueError(f"timeout は正の秒数にしてください(値: {timeout!r})")
        if not isinstance(channel, str) or not CHANNEL_RE.fullmatch(channel):
            hint = ""
            if isinstance(channel, str) and ":" in channel:  # CHANNELS の「ID:表示名」をそのまま書いたとき
                hint = f"。表示名は付けず、ID だけを書きます(例: channel = {channel.split(':', 1)[0].strip()!r})"
            raise ValueError("channel には、relay/wrangler.toml の CHANNELS に書いた会場の ID"
                             f"(英小文字・数字・-、32 文字まで)を書いてください(値: {channel!r}){hint}")
        self.channel = channel
        self.endpoint = url.rstrip("/") + "/publish/" + channel
        self.token = token
        self.timeout = timeout
        # main.py を起動し直すと id が 0 に戻るので、起動ごとの印を付けて中継側で区別する
        self.session = secrets.token_hex(8)
        self.failing = False
        self._check()

    def _post(self, body, timeout):
        req = urllib.request.Request(self.endpoint, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "meeting-translate-relay",
        })
        with urllib.request.urlopen(req, timeout=timeout) as res:
            res.read()

    def _errors(self, code):
        if code == 401:
            return ("送信トークンが中継と合いません。plugins/relay.toml の token と、"
                    "wrangler secret の PUBLISH_TOKEN が同じか確かめてください")
        if code == 404:
            return (f"中継に会場 '{self.channel}' がありません。plugins/relay.toml の channel が "
                    "relay/wrangler.toml の CHANNELS にあるか、中継を deploy し直したかを確かめてください")
        return None

    def _check(self):
        """起動時(モデルを読み込む前)に、トークンと会場が中継と合っているかを確かめる。

        空の本文を送ると、中継は 401(トークン違い)/ 404(会場が無い)/ 400(設定は正しい)で答え、何も書き込まない。
        設定の誤りは ValueError にして起動を止める。中継にまだ届かない(起動前・回線断など)ときは、警告だけにして続ける。
        """
        try:
            self._post(b"", min(self.timeout, 5))
        except urllib.error.HTTPError as e:
            msg = self._errors(e.code)
            if msg:
                raise ValueError(msg) from None
            if e.code != 400:
                _log(f"プラグイン relay: 起動時の確認で、中継が {e.code} を返しました。発言は送ってみます")
        except (OSError, http.client.HTTPException) as e:
            _log(f"プラグイン relay: 中継に届かないため、会場とトークンを確かめられませんでした({e})。発言は送ってみます")

    def write(self, ev):
        body = json.dumps({**ev, "session": self.session}, ensure_ascii=False).encode()
        deadline = time.monotonic() + RETRY_FOR
        wait = 1
        while True:
            try:
                self._post(body, self.timeout)
                if self.failing:
                    self.failing = False
                    _log("プラグイン relay: 中継に届くようになりました")
                return
            except urllib.error.HTTPError as e:
                msg = self._errors(e.code)  # 起動後に中継の設定が変わったとき
                if msg:
                    raise RuntimeError(msg) from None
                if e.code != 429 and e.code < 500:
                    raise
                err = e
            except (OSError, http.client.HTTPException) as e:  # 回線断、タイムアウト、名前解決の失敗など
                err = e
            if not self.failing:
                self.failing = True
                _log(f"プラグイン relay: 中継に届かないので、送り直しています: {err}")
            if time.monotonic() + wait > deadline:
                raise RuntimeError(f"{RETRY_FOR} 秒送り直しても届かなかったため、この発言(id={ev['id']})は送りません") from None
            time.sleep(wait)
            wait = min(wait * 2, 10)
