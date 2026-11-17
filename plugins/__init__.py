"""出力先プラグイン。plugins.toml に名前を書いたものだけを plugins/<名前>.py から読み込み、発言を配る。

    plugins.toml           使うプラグインの一覧だけ。例: plugins = ["web", "slack"]。無ければ ["web"]
    plugins/<名前>.py      class Output を置く
    plugins/<名前>.toml    そのプラグインの設定(任意)。中身をそのまま Output(**settings) に渡す

class Output:
    __init__(self, **settings)  plugins/<名前>.toml の項目を受け取る。設定の誤りはここで例外にする
    write(self, ev)             発言ごとに 1 回。ev は main.py が作るイベント {id, ts, speaker, lang, text, translation, start, end}
    close(self)                 任意。終了時に 1 回
write() と close() はプラグインごとの専用スレッドで順番に呼ぶので、ネットワークで待ってもよい。
"""
import importlib.util
import queue
import re
import sys
import threading
import time
import tomllib
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent  # このパッケージ(plugins/)にプラグインを置く
DEFAULT_PATH = PLUGIN_DIR.parent / "plugins.toml"
DEFAULT_PLUGINS = ["web"]  # plugins.toml が無いときに使う(plugins.toml は各自の設定で、コミットしない)

_runners = []


def _log(msg):
    print(msg, file=sys.stderr, flush=True)


class _Runner:
    def __init__(self, name, plugin):
        self.name, self.plugin = name, plugin
        self.q = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while (ev := self.q.get()) is not None:
            try:
                self.plugin.write(ev)
            except Exception as e:  # プラグインの失敗で会議を止めない
                _log(f"プラグイン {self.name}: {type(e).__name__}: {e}")
        close = getattr(self.plugin, "close", None)
        if close:
            try:
                close()
            except Exception as e:
                _log(f"プラグイン {self.name}: 終了処理で {type(e).__name__}: {e}")


def _read_toml(p):
    try:
        with p.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"{p}: TOML の書式が正しくありません: {e}")


def _load_plugin(name):
    file = PLUGIN_DIR / f"{name}.py"
    if not file.is_file():
        raise SystemExit(f"プラグイン {name}: {file} がありません")
    spec = importlib.util.spec_from_file_location(f"plugins.{name}", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls = getattr(module, "Output", None)
    if cls is None:
        raise SystemExit(f"プラグイン {name}: {file} に class Output がありません")
    settings_file = PLUGIN_DIR / f"{name}.toml"
    settings = _read_toml(settings_file) if settings_file.is_file() else {}
    try:
        return cls(**settings)
    except Exception as e:
        where = f"{name}({settings_file})" if settings_file.is_file() else name
        msg = f"プラグイン {where}: 起動できません: {type(e).__name__}: {e}"
        if not settings_file.is_file():
            example = PLUGIN_DIR / f"{name}.example.toml"
            msg += f"\n  設定ファイル {settings_file} がありません"
            if example.is_file():
                msg += f"(見本: {example})"
        raise SystemExit(msg)


def start(path=None):
    """plugins.toml を読み、プラグインを動かし始める。(読み込んだ名前の一覧, 読んだパス) を返す。

    path を省略したときは plugins.toml があれば読み、無ければ DEFAULT_PLUGINS を使う。
    path を指定したのにファイルが無いときはエラーにする。
    """
    p = Path(path) if path else DEFAULT_PATH
    if not p.is_file():
        if path:
            raise SystemExit(f"プラグインの一覧のファイルが見つかりません: {p}")
        names, p = list(DEFAULT_PLUGINS), None
    else:
        names = _read_plugin_list(p)
    loaded = [(name, _load_plugin(name)) for name in names]
    _runners.extend(_Runner(name, plugin) for name, plugin in loaded)
    return names, p


def _read_plugin_list(p):
    data = _read_toml(p)
    unknown = set(data) - {"plugins"}
    if unknown:
        raise SystemExit(f"{p}: 不明な設定 {', '.join(sorted(unknown))}"
                         f"(書けるのは plugins = [...] だけです。プラグインの設定は plugins/<名前>.toml に書きます)")
    names = data.get("plugins", [])
    if not isinstance(names, list):
        raise SystemExit(f"{p}: 'plugins' は名前の配列で書いてください。例: plugins = [\"slack\"]")
    for name in names:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]+", name):
            raise SystemExit(f"{p}: 'plugins' にはプラグインの名前(英数字と _)を書いてください(値: {name!r})")
        if names.count(name) > 1:
            raise SystemExit(f"{p}: 'plugins' に {name!r} が 2 回以上あります")
    return names


def publish(ev):
    for r in _runners:
        r.q.put(dict(ev))


def close(timeout=5.0):
    """残りの発言を送り切ってから close() を呼ぶ。timeout 秒で打ち切る。"""
    for r in _runners:
        r.q.put(None)
    deadline = time.monotonic() + timeout
    for r in _runners:
        r.thread.join(max(0.0, deadline - time.monotonic()))
        if r.thread.is_alive():
            _log(f"プラグイン {r.name}: {timeout:g} 秒で送り切れなかったため、残りを送らずに終了します")
