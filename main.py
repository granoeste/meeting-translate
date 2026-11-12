import argparse
import json
import queue
import signal
import socket
import sys
import threading
import time

import mlx_whisper
import numpy as np
import sounddevice as sd
import webrtcvad

import config
import output
from transcript import Recorder
from translate import Translator

SR = 16000
FRAME_MS = 30
FRAME = SR * FRAME_MS // 1000

segments = queue.Queue()
args_debug = False
jsonl = False
cfg = config.Config()  # main() で config.toml の値に置き換える


def find_device(name):
    for i, d in enumerate(sd.query_devices()):
        if name.lower() in d["name"].lower() and d["max_input_channels"] > 0:
            return i
    return None


def segment(label, read_frame):
    """Cut a stream of FRAME-sized int16 frames into utterances with VAD, queue them."""
    vad = webrtcvad.Vad(2)
    buf, voiced, silence = [], False, 0

    def flush():
        nonlocal buf, voiced, silence
        audio = np.concatenate(buf) if buf else np.zeros(0, np.float32)
        if voiced and len(audio) / SR >= cfg.min_segment_s:
            end = time.time()
            segments.put((label, audio, end - len(audio) / SR, end))
        buf, voiced, silence = [], False, 0

    try:
        while True:
            frame = read_frame()
            if vad.is_speech(frame.tobytes(), SR):
                voiced, silence = True, 0
            else:
                silence += FRAME_MS
            if voiced or buf:
                buf.append(frame.astype(np.float32) / 32768.0)
            if voiced and silence >= cfg.silence_end_ms:
                flush()
            elif len(buf) * FRAME_MS / 1000 >= cfg.max_segment_s:
                flush()
            elif not voiced and len(buf) > 10:
                buf = buf[-10:]  # keep a little lead-in before speech starts
    except EOFError:
        flush()  # the stream ended mid-utterance


def capture(label, device):
    with sd.InputStream(
        samplerate=SR, channels=1, dtype="int16", blocksize=FRAME, device=device
    ) as stream:
        segment(label, lambda: stream.read(FRAME)[0][:, 0])


def capture_remote(label, host, port):
    """Receive 16 kHz mono int16 PCM from sender.py; reconnect when the tunnel drops."""
    while True:
        try:
            sock = socket.create_connection((host, port), timeout=5)
        except OSError:
            time.sleep(2)
            continue
        sock.settimeout(None)
        print(f"{label}: {host}:{port} に接続しました", file=sys.stderr, flush=True)

        def read_frame():
            data = b""
            while len(data) < FRAME * 2:
                chunk = sock.recv(FRAME * 2 - len(data))
                if not chunk:
                    raise EOFError
                data += chunk
            return np.frombuffer(data, dtype=np.int16)

        try:
            segment(label, read_frame)
        except OSError:
            pass
        sock.close()
        print(f"{label}: 切断されました。再接続します", file=sys.stderr, flush=True)


def worker(translator, session_t0, recorder):
    history = {}
    hallucinations = {h.lower() for h in cfg.hallucinations}
    while True:
        label, audio, seg_start, seg_end = segments.get()
        t0 = time.time()
        fixed = cfg.fixed_lang.get(label)
        r = mlx_whisper.transcribe(
            audio, path_or_hf_repo=cfg.whisper_model, condition_on_previous_text=False,
            language=fixed,
        )
        t1 = time.time()
        text = r["text"].strip()
        lang = fixed or r.get("language")
        # 日本語の発音の英語は ja と誤判定されやすい。本文が英字だけなら en に直す
        if not fixed and lang == "ja" and "en" in cfg.pair and text.isascii() and any(c.isalpha() for c in text):
            lang = "en"
        segs = r.get("segments") or []
        if args_debug:
            print(f"  [debug] {len(audio)/SR:.1f}s audio, asr {t1-t0:.1f}s, lang={lang}, "
                  f"no_speech={[round(s['no_speech_prob'],2) for s in segs]}, "
                  f"logprob={[round(s['avg_logprob'],2) for s in segs]}", file=sys.stderr)
        if not text or lang not in cfg.pair:
            continue
        if segs and np.mean([s["no_speech_prob"] for s in segs]) > 0.6:
            continue
        # short, low-confidence fragments are almost always Whisper hallucinations
        if segs and len(audio) / SR < 2.5 and np.mean([s["avg_logprob"] for s in segs]) < -0.7:
            continue
        if len(audio) / SR < 2.0 and text.strip(" .!。！").lower() in hallucinations:
            continue
        ctx = history.get(label)
        out = translator.translate(text, lang, context=ctx)
        history[label] = text
        ev = output.publish(
            label, lang, text, out,
            start=max(0.0, seg_start - session_t0), end=max(0.0, seg_end - session_t0),
        )
        if recorder:
            recorder.write(ev)
        if jsonl:
            print(json.dumps(ev, ensure_ascii=False), flush=True)
        else:
            print(f"[{label}] ({lang}) {text}\n    -> {out}   (asr {t1 - t0:.1f}s, mt {time.time() - t1:.1f}s)\n")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mic", default="", help="mic device name (default: system default)")
    p.add_argument("--system", default="BlackHole", help="loopback device name")
    p.add_argument("--list", action="store_true")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--jsonl", action="store_true", help="print one JSON object per line to stdout")
    p.add_argument("--port", type=int, default=8765, help="web/OBS server port (127.0.0.1 only)")
    p.add_argument("--no-web", action="store_true")
    p.add_argument("--no-mic", action="store_true", help="do not use this Mac's mic")
    p.add_argument("--remote-self", default="", metavar="HOST:PORT",
                   help="receive your own voice from sender.py on the meeting Mac")
    p.add_argument("--remote", default="", metavar="HOST:PORT",
                   help="receive the other side's audio from sender.py (via an SSH tunnel)")
    p.add_argument("--out-dir", default="transcripts", help="where to save .jsonl/.txt/.srt")
    p.add_argument("--no-record", action="store_true", help="do not save a transcript")
    p.add_argument("--config", default="", metavar="PATH",
                   help="settings file (default: config.toml next to main.py, if it exists)")
    a = p.parse_args()
    global args_debug, jsonl, cfg
    args_debug, jsonl = a.debug, a.jsonl
    if a.list:
        print(sd.query_devices())
        return
    cfg, cfg_path = config.load(a.config or None)

    sources = {}
    if not a.no_mic:
        sources["自分"] = find_device(a.mic) if a.mic else sd.default.device[0]
    sys_dev = find_device(a.system)
    if sys_dev is None:
        print(f"'{a.system}' が見つからないため、マイクのみで開始します\n", file=sys.stderr)
    else:
        sources["相手"] = sys_dev

    log = (lambda *x: print(*x, file=sys.stderr, flush=True)) if jsonl else print
    if cfg_path:
        log(f"設定: {cfg_path}")
    if not a.no_web:
        output.start_server(a.port)
        log(f"Web: http://127.0.0.1:{a.port}/   OBS: http://127.0.0.1:{a.port}/caption")
    log("モデルを読み込み中...")
    translator = Translator(cfg.translate_model, cfg.pair)
    mlx_whisper.transcribe(np.zeros(SR, np.float32), path_or_hf_repo=cfg.whisper_model)  # warm up
    recorder = None if a.no_record else Recorder(a.out_dir)
    session_t0 = time.time()
    threading.Thread(
        target=worker, args=(translator, session_t0, recorder), daemon=True
    ).start()
    for label, dev in sources.items():
        threading.Thread(target=capture, args=(label, dev), daemon=True).start()
    for label, spec in (("相手", a.remote), ("自分", a.remote_self)):
        if spec:
            host, _, port = spec.rpartition(":")
            threading.Thread(
                target=capture_remote, args=(label, host or "127.0.0.1", int(port)), daemon=True
            ).start()
    log("開始しました。Ctrl+C で終了\n")
    # make `kill` (SIGTERM) save the transcript the same way Ctrl+C does
    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    if recorder:
        time.sleep(0.5)  # let an in-flight utterance finish writing
        txt, srt = recorder.close()
        log(f"\n保存しました:\n  {txt}\n  {srt}")


if __name__ == "__main__":
    main()
