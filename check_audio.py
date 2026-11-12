"""BlackHole の入出力チェック。

  uv run python check_audio.py loop    # BlackHole に L=1kHz / R=1.5kHz を流し、BlackHole の入力に戻ってくるか判定
  uv run python check_audio.py meter   # BlackHole とデフォルト入力(マイク)のレベルをリアルタイム表示(Ctrl+C で終了)

meter はデバイスを指定できる:  uv run python check_audio.py meter BlackHole "MacBook Pro" default
"""
import argparse
import sys
import time

import numpy as np
import sounddevice as sd


def find(name, kind):
    if name == "default":
        return sd.default.device[0 if kind == "input" else 1]
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    for i, d in enumerate(sd.query_devices()):
        if name.lower() in d["name"].lower() and d[key] > 0:
            return i
    raise SystemExit(f"{kind} device '{name}' が見つかりません:\n{sd.query_devices()}")


def db(x):
    return 20 * np.log10(np.maximum(x, 1e-7))


def loop(name, secs=2.0):
    dev_in, dev_out = find(name, "input"), find(name, "output")
    sr = int(sd.query_devices(dev_out)["default_samplerate"])
    freqs = (1000.0, 1500.0)  # L, R を別の周波数にして、チャンネルの取り違えも検出する
    t = np.arange(int(sr * secs)) / sr
    tone = np.column_stack([0.25 * np.sin(2 * np.pi * f * t) for f in freqs]).astype(np.float32)

    print(f"再生: {sd.query_devices(dev_out)['name']} / 録音: {sd.query_devices(dev_in)['name']} / {sr} Hz")
    rec = sd.playrec(tone, samplerate=sr, channels=2, device=(dev_in, dev_out), blocking=True)

    ok = True
    for ch, (label, f) in enumerate(zip("LR", freqs)):
        x = rec[:, ch]
        onset = np.argmax(np.abs(x) > 0.05) if np.abs(x).max() > 0.05 else None
        body = x[int(0.5 * sr):]  # 立ち上がりを除いた区間で測る
        spec = np.abs(np.fft.rfft(body * np.hanning(len(body))))
        peak_f = np.fft.rfftfreq(len(body), 1 / sr)[spec.argmax()]
        rms = db(np.sqrt(np.mean(body**2)))
        passed = rms > -30 and abs(peak_f - f) < 5
        ok &= passed
        lat = f"{onset / sr * 1000:.0f} ms" if onset is not None else "-"
        print(f"  {label}: 期待 {f:.0f} Hz → 検出 {peak_f:.0f} Hz, {rms:.1f} dBFS, 遅延 {lat}  {'OK' if passed else 'NG'}")

    if ok:
        print("PASS: BlackHole の出力 → 入力のループバックは正常です")
    elif np.abs(rec).max() == 0:
        print("FAIL: 完全な無音です。ターミナルにマイクの権限があるか確認してください"
              "(システム設定 → プライバシーとセキュリティ → マイク)")
    else:
        print("FAIL: 音は届いていますが、周波数かレベルが想定と違います(他の音が混ざっていないか確認)")
    return 0 if ok else 1


def meter(names, duration):
    width = 40
    entries = []
    for name in names:
        idx = find(name, "input")
        info = sd.query_devices(idx)
        e = {"name": f"{info['name']}" + ("  (デフォルト入力)" if name == "default" else ""),
             "ch": min(2, info["max_input_channels"]), "idx": idx}
        e["level"] = np.full(e["ch"], -120.0)
        e["hold"] = np.full(e["ch"], -120.0)

        def cb(indata, frames, t, status, e=e):
            e["level"] = db(np.abs(indata).max(axis=0))

        e["stream"] = sd.InputStream(device=idx, channels=e["ch"], blocksize=1024, callback=cb)
        entries.append(e)

    if sd.default.device[0] == find("BlackHole", "input"):
        print("注意: デフォルト入力が BlackHole です。main.py の「自分」(マイク)も BlackHole を拾います。\n")

    lines = sum(1 + e["ch"] for e in entries)
    for e in entries:
        e["stream"].start()
    start, first = time.time(), True
    try:
        while not duration or time.time() - start < duration:
            if not first:
                sys.stdout.write(f"\x1b[{lines}F")  # 描いた行数ぶん戻って上書き
            first = False
            for e in entries:
                sys.stdout.write(f"\x1b[2K[{e['name']}]\n")
                e["hold"] = np.maximum(e["level"], e["hold"] - 1.5)  # ピークホールドを徐々に下げる
                for c in range(e["ch"]):
                    lv, hd = e["level"][c], e["hold"][c]
                    n = int(np.clip((lv + 60) / 60, 0, 1) * width)  # -60〜0 dBFS
                    h = int(np.clip((hd + 60) / 60, 0, 1) * width)
                    bar = ["█" if i < n else "·" for i in range(width)]
                    if 0 < h <= width:
                        bar[h - 1] = "|"
                    lab = "LR"[c] if e["ch"] == 2 else "M"
                    sys.stdout.write(f"\x1b[2K  {lab} {''.join(bar)} {lv:6.1f} dBFS\n")
            sys.stdout.flush()
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        for e in entries:
            e["stream"].close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["loop", "meter"])
    p.add_argument("devices", nargs="*", help="meter で表示する入力デバイス(部分一致、default でデフォルト入力)")
    p.add_argument("--device", default="BlackHole", help="loop で使うデバイス")
    p.add_argument("--duration", type=float, default=0, help="meter を自動で止める秒数(0 で無制限)")
    a = p.parse_args()
    if a.mode == "loop":
        sys.exit(loop(a.device))
    meter(a.devices or ["BlackHole", "default"], a.duration)


if __name__ == "__main__":
    main()
