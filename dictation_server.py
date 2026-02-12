#!/usr/bin/env python3
"""Persistent dictation daemon. Holds the Whisper model in memory and
transcribes audio received over a Unix socket.

One-shot transcription: collects all audio until the client signals done,
then transcribes the full recording and sends the result back.
"""

import os
import socket
import sys
import time
import numpy as np
from faster_whisper import WhisperModel

# =========================
# Configuration
# =========================

WHISPER_RATE = 16000
LANGUAGE = None  # auto-detect (supports en, de, hu, etc.)
MODEL_SIZE = "large-v3-turbo"
SOCKET_PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "dictation.sock")
LOG_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "dictation")
LOG_PATH = os.path.join(LOG_DIR, "timing.csv")


def log_timing(audio_duration, transcribe_duration):
    """Append a timing entry to the CSV log."""
    os.makedirs(LOG_DIR, exist_ok=True)
    write_header = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a") as f:
        if write_header:
            f.write("audio_s,transcribe_s\n")
        f.write(f"{audio_duration:.2f},{transcribe_duration:.2f}\n")


def load_model():
    print(f"Loading model '{MODEL_SIZE}'...", file=sys.stderr, flush=True)
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    print("Model loaded.", file=sys.stderr, flush=True)
    return model


def send_message(conn, msg_type, text):
    """Send a message to the client (newline-delimited)."""
    line = f"{msg_type} {text}\n"
    try:
        conn.sendall(line.encode("utf-8"))
    except (BrokenPipeError, ConnectionResetError):
        pass


def handle_connection(conn, model):
    """Read all audio until EOF, then transcribe and send the result."""
    audio_buf = np.array([], dtype=np.float32)
    byte_buf = b""

    while True:
        try:
            data = conn.recv(65536)
        except (ConnectionResetError, BrokenPipeError):
            break

        if len(data) == 0:
            break

        byte_buf += data
        n_complete = (len(byte_buf) // 4) * 4
        if n_complete > 0:
            chunk = np.frombuffer(byte_buf[:n_complete], dtype=np.float32)
            audio_buf = np.concatenate([audio_buf, chunk])
            byte_buf = byte_buf[n_complete:]

    if len(audio_buf) == 0:
        return

    audio_duration = len(audio_buf) / WHISPER_RATE
    send_message(conn, "L", f"{audio_duration:.1f}")
    print(f"Transcribing ({audio_duration:.1f}s)...", file=sys.stderr, flush=True)

    t0 = time.monotonic()
    segments, _ = model.transcribe(
        audio_buf,
        language=LANGUAGE,
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=500,
            speech_pad_ms=300,
        ),
    )
    text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
    transcribe_duration = time.monotonic() - t0

    if text:
        send_message(conn, "F", text)

    log_timing(audio_duration, transcribe_duration)
    print(f"Done in {transcribe_duration:.1f}s.", file=sys.stderr, flush=True)


def main():
    model = load_model()

    # Clean up stale socket
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(1)
    print(f"Listening on {SOCKET_PATH}", file=sys.stderr, flush=True)

    try:
        while True:
            conn, _ = server.accept()
            try:
                handle_connection(conn, model)
            except Exception as e:
                print(f"Error handling request: {e}", file=sys.stderr, flush=True)
            finally:
                conn.close()
    except KeyboardInterrupt:
        print("Shutting down.", file=sys.stderr, flush=True)
    finally:
        server.close()
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
