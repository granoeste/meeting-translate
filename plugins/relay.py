"""出力先プラグイン: 発言を Cloudflare の中継(relay/)に送り、会場の参加者が自分のスマホで見られるようにする。

plugins.toml:        plugins = ["web", "relay"]
plugins/relay.toml:  url と token(見本は plugins/relay.example.toml)。中継の置き方は relay/README.md

送るのはイベント(文字)だけで、音声は送らない。標準ライブラリだけで動く。
"""
import http.client
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

RETRY_FOR = 60  # 中継に届かないときに、1 件を送り直し続ける最長の秒数


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


class Output:
    def __init__(self, url, token, timeout=10):
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
        self.endpoint = url.rstrip("/") + "/publish"
        self.token = token
        self.timeout = timeout
        # main.py を起動し直すと id が 0 に戻るので、起動ごとの印を付けて中継側で区別する
        self.session = secrets.token_hex(8)
        self.failing = False

    def write(self, ev):
        body = json.dumps({**ev, "session": self.session}, ensure_ascii=False).encode()
        deadline = time.monotonic() + RETRY_FOR
        wait = 1
        while True:
            req = urllib.request.Request(self.endpoint, data=body, method="POST", headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "meeting-translate-relay",
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as res:
                    res.read()
                if self.failing:
                    self.failing = False
                    _log("プラグイン relay: 中継に届くようになりました")
                return
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise RuntimeError("送信トークンが中継と合いません。plugins/relay.toml の token と、"
                                       "wrangler secret の PUBLISH_TOKEN が同じか確かめてください") from None
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
