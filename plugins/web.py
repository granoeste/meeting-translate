"""Web 画面と OBS の字幕を出すプラグイン(既定で有効)。127.0.0.1 だけで待ち受ける小さな HTTP サーバー(SSE)。

    /          話者別のログ
    /caption   OBS のブラウザソース用の字幕(背景は透過)。?hold=5 で表示秒数、?orig=0 で原文を隠す
    /events    発言の SSE

plugins/web.toml:  port = 8765(無ければ 8765)
"""
import json
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"  # LAN に公開しない。別の端末から見るときは SSH トンネルを使う
BACKLOG = 50  # 後から開いたブラウザに送る、直近の発言の数

LOG_PAGE = """<!doctype html><meta charset="utf-8"><title>Meeting Translate</title>
<style>
body{font:16px/1.5 system-ui,sans-serif;margin:0;padding:16px;background:#111;color:#eee}
.c{margin:0 0 14px;padding-left:10px;border-left:4px solid #888}
.c.me{border-color:#4aa3ff}.c.other{border-color:#ffb84a}
.s{font-size:12px;opacity:.6}.t{font-size:20px;font-weight:600}.o{opacity:.75}
</style><div id="log"></div>
<script>
const log=document.getElementById('log');
function add(e){const d=document.createElement('div');
d.className='c '+(e.speaker==='自分'?'me':'other');
for(const [cls,txt] of [['s',e.speaker+' ('+e.lang+')'],['t',e.translation],['o',e.text]]){
const x=document.createElement('div');x.className=cls;x.textContent=txt;d.append(x)}
log.append(d);scrollTo(0,document.body.scrollHeight)}
new EventSource('/events').onmessage=m=>add(JSON.parse(m.data));
</script>"""

# transparent background for OBS browser source
CAPTION_PAGE = """<!doctype html><meta charset="utf-8"><title>Caption</title>
<style>
html,body{margin:0;background:transparent;overflow:hidden}
#cap{position:fixed;left:5%;right:5%;bottom:6%;text-align:center;font:700 40px/1.35 system-ui,sans-serif;
color:#fff;text-shadow:0 0 6px #000,0 0 6px #000,2px 2px 4px #000;opacity:0;transition:opacity .2s}
#cap.on{opacity:1}#o{font-size:22px;font-weight:500;opacity:.8}
</style><div id="cap"><div id="t"></div><div id="o"></div></div>
<script>
const cap=document.getElementById('cap'),t=document.getElementById('t'),o=document.getElementById('o');
const hold=+(new URLSearchParams(location.search).get('hold')||8)*1000;
const orig=new URLSearchParams(location.search).get('orig')!=='0';
let timer;
new EventSource('/events').onmessage=m=>{const e=JSON.parse(m.data);
t.textContent=e.translation;o.textContent=orig?e.text:'';cap.classList.add('on');
clearTimeout(timer);timer=setTimeout(()=>cap.classList.remove('on'),hold)};
</script>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, ctype):
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._send(LOG_PAGE, "text/html; charset=utf-8")
        elif path == "/caption":
            self._send(CAPTION_PAGE, "text/html; charset=utf-8")
        elif path == "/events":
            self._stream()
        else:
            self.send_error(404)

    def _stream(self):
        out = self.server.output
        q = queue.Queue()
        with out.lock:
            backlog = list(out.history)
            out.subscribers.add(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            for ev in backlog:
                self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")  # detect closed connections
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with out.lock:
                out.subscribers.discard(q)


class Output:
    def __init__(self, port=8765):
        if type(port) is not int or not 0 < port < 65536:
            raise ValueError(f"port は 1〜65535 の整数にしてください(値: {port!r})")
        self.subscribers = set()
        self.history = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer((HOST, port), _Handler)  # 使用中のポートなら OSError
        self.server.daemon_threads = True
        self.server.output = self
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        print(f"Web: http://{HOST}:{port}/   OBS: http://{HOST}:{port}/caption", file=sys.stderr, flush=True)

    def write(self, ev):
        with self.lock:
            self.history.append(ev)
            del self.history[:-BACKLOG]
            for q in self.subscribers:
                q.put(ev)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
