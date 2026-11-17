"""出力先プラグインの見本: 発言ごとに Slack へ投稿する(Incoming Webhook)。

plugins.toml:        plugins = ["slack"]
plugins/slack.toml:  webhook_url = "https://hooks.slack.com/services/..."(見本は plugins/slack.example.toml)

標準ライブラリだけで動く。
"""
import json
import time
import urllib.error
import urllib.request

RETRIES = 3


def _escape(s):
    # Slack のメッセージでは & < > に意味があるので、文字として出す
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Output:
    def __init__(self, webhook_url, timeout=10):
        # 設定の誤りで Slack 以外に送らないようにする
        if not isinstance(webhook_url, str) or not webhook_url.startswith("https://hooks.slack.com/"):
            raise ValueError("webhook_url は https://hooks.slack.com/ で始まる Incoming Webhook の URL にしてください")
        self.url = webhook_url
        self.timeout = timeout

    def write(self, ev):
        text = (f"*{_escape(ev['speaker'])}* ({ev['lang']})\n"
                f"{_escape(ev['translation'])}\n"
                f"> {_escape(ev['text'])}")
        body = json.dumps({"text": text}).encode()
        for attempt in range(RETRIES + 1):
            req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as res:
                    res.read()
                return
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == RETRIES:  # 429 は投稿が速すぎるとき。待って送り直す
                    raise
                time.sleep(float(e.headers.get("Retry-After") or 1))
