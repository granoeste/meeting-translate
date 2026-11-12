# /// script
# dependencies = ["sounddevice", "numpy"]
# ///
"""Run on the meeting Mac: stream an input device (e.g. BlackHole) as 16 kHz mono int16 PCM.

Listens on 127.0.0.1 only. Reach it from the translating Mac through an SSH tunnel:
    ssh -N -L 9999:127.0.0.1:9999 <user>@<meeting-mac>
"""
import argparse
import queue
import socket

import numpy as np
import sounddevice as sd

SR = 16000
BLOCK = 480  # 30 ms


def find_device(name):
    if name == "default":
        return sd.default.device[0]
    for i, d in enumerate(sd.query_devices()):
        if name.lower() in d["name"].lower() and d["max_input_channels"] > 0:
            return i
    raise SystemExit(f"input device '{name}' not found:\n{sd.query_devices()}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--device", default="BlackHole", help="input device name, or 'default' for the mic")
    p.add_argument("--port", type=int, default=9999)
    a = p.parse_args()

    dev = find_device(a.device)
    channels = min(2, sd.query_devices(dev)["max_input_channels"])
    q = queue.Queue(maxsize=200)

    def callback(indata, frames, time_info, status):
        mono = indata.mean(axis=1)
        pcm = (np.clip(mono, -1, 1) * 32767).astype(np.int16)
        try:
            q.put_nowait(pcm.tobytes())
        except queue.Full:
            pass  # nobody is consuming; drop

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", a.port))
    srv.listen(1)
    print(f"capturing '{sd.query_devices(dev)['name']}', waiting on 127.0.0.1:{a.port}")

    with sd.InputStream(
        samplerate=SR, channels=channels, dtype="float32",
        blocksize=BLOCK, device=dev, callback=callback,
    ):
        while True:
            conn, _ = srv.accept()
            print("receiver connected")
            while not q.empty():
                q.get_nowait()  # don't send stale audio
            try:
                while True:
                    conn.sendall(q.get())
            except (BrokenPipeError, ConnectionResetError):
                print("receiver disconnected")
            finally:
                conn.close()


if __name__ == "__main__":
    main()
