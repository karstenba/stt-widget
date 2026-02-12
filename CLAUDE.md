# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Local speech-to-text dictation tool using a daemon + thin GTK client architecture. A persistent daemon holds the Whisper model in memory, and a GTK popup (triggered by i3 keybinding `$mod+Shift+d`) captures audio, sends it to the daemon for transcription, copies the result to clipboard, and auto-pastes into the previously focused application.

## Running

### Daemon (transcription server)

```bash
systemctl --user start dictation
```

The daemon loads the Whisper model once and listens on a Unix socket at `$XDG_RUNTIME_DIR/dictation.sock`. Enable for auto-start on login:

```bash
systemctl --user enable dictation
```

Check logs:

```bash
journalctl --user -u dictation -f
```

### GUI (GTK popup client)

```bash
venv/bin/python dictation_gui.py
```

A small floating window appears, records audio, and transcribes when you press ESC. The result is copied to clipboard and the window closes. Designed for use with an i3 keybinding:

```
for_window [title="^Dictation$"] floating enable
bindsym $mod+Shift+d exec /path/to/stt/venv/bin/python /path/to/stt/dictation_gui.py
```

The i3 title match uses `^Dictation$` (exact) to avoid accidentally floating other windows that contain the word "Dictation" in their title.

## Dependencies

Python 3.14 venv with key packages: `faster-whisper`, `sounddevice`, `numpy`, `webrtcvad`, `pyperclip`. No requirements.txt or pyproject.toml exists — packages are installed directly into `venv/`.

The venv has `include-system-site-packages = true` to access `PyGObject` (GTK3 bindings), installed via `sudo pacman -S python-gobject gtk3`.

The Whisper model (`large-v3-turbo`, ~1.5 GB int8) is cached in `~/.cache/huggingface/hub/`.

## Architecture

Two components communicating over a Unix socket:

- **`dictation_server.py`** — persistent daemon holding the Whisper model in memory
- **`dictation_gui.py`** — thin GTK3 popup client (audio capture + paste)

### Socket protocol (bidirectional, newline-delimited)

Client → Server: raw float32 audio bytes (continuous stream), then `shutdown(SHUT_WR)` to signal done

Server → Client: newline-delimited messages:
- `F <text>` — final transcription result
- `L <message>` — log/status message (e.g. "Transcribing (3.2s)...")

### Daemon (`dictation_server.py`)

1. Loads `large-v3-turbo` model at startup (~1.5 GB int8, faster and more accurate than `medium`)
2. Listens on `$XDG_RUNTIME_DIR/dictation.sock`
3. Accepts connections: reads all audio until EOF (client shuts down write)
4. Transcribes the full recording with VAD filtering, sends result as `F` message
5. Handles one request at a time (serialized)
6. Managed by systemd user service (`~/.config/systemd/user/dictation.service`)

### GUI flow (`dictation_gui.py`)

1. GTK window (1200px wide, undecorated) appears immediately showing "Starting..."
2. Worker thread: manages BT codec, finds audio device, starts capture, connects to daemon
3. Writer thread streams audio chunks to daemon continuously
4. Reader thread receives `F`/`L` messages and updates GUI
5. Shows live recording timer while recording
6. ESC stops recording → writer drains queue → daemon transcribes and sends `F` result
7. Copies to clipboard, refocuses the previous window, and auto-pastes
8. Ctrl+C cancels — closes everything without copying or pasting

### GUI layout

Minimal window (1200px wide, Catppuccin Mocha dark theme) with a single status label (Pango-styled 20pt):
- While recording: "Recording... MM:SS"
- After ESC: "Transcribing... MM:SS" (with running timer)
- Errors shown in the same label, window auto-closes after 3s

### Bluetooth codec management

If a Bluetooth headset is connected (detected via `pactl list cards`), the GUI:
1. Disables WirePlumber auto-profile-switching (`wpctl settings bluetooth.autoswitch-to-headset-profile false`) to prevent WirePlumber's 2s restore timeout from fighting manual profile changes
2. Saves the current BT profile (e.g. `a2dp-sink` / LDAC)
3. Switches to HFP/mSBC (`headset-head-unit`) for mic access
4. Finds the BT input device in sounddevice by matching `device.description` from pactl (e.g. "WH-1000XM5" — sounddevice names don't contain "bluez")
5. After finish or cancel, restores the original profile and re-enables WirePlumber autoswitch

### Audio capture

Audio is captured at the device's native sample rate (e.g. 48kHz) and resampled to 16kHz for Whisper in the audio callback.

### Auto-paste

The GUI captures the focused window ID (via `xdotool`) before the popup appears. After transcription it refocuses that window and pastes. For XTerm windows it copies to the PRIMARY selection (via `xclip`) and sends Shift+Insert; for everything else it sends Ctrl+V.

### Language

Uses `LANGUAGE = None` so Whisper auto-detects per utterance (English, German, Hungarian all work). Mixing languages mid-sentence is unreliable — Whisper picks one language per segment.

## Hardware

- **CPU**: AMD Ryzen (Framework AMD laptop)
- **RAM**: 32 GB (plenty for any Whisper model in int8)
- **GPU**: AMD Radeon 780M (Phoenix1) iGPU, 512 MB VRAM (shares system RAM). ROCm does NOT support this iGPU — CPU inference only.

## System tools

- `xdotool` — window focus management and key simulation
- `xclip` — PRIMARY selection for xterm paste
- `pyperclip` — CLIPBOARD selection
- `pactl` — PulseAudio/PipeWire card and profile management
- `wpctl` — WirePlumber settings control

## Session log

### 2025-02-12
1. Built initial streaming transcription with dual-model (`small` + `medium`)
2. Added BT codec management, WirePlumber fix, GTK improvements

### 2026-02-12
1. Simplified to one-shot transcription with `large-v3-turbo` (faster than `medium`, more accurate, ~1.5 GB int8)
2. Removed streaming VAD logic and `small` model — single model, single transcription pass
3. Removed `T` message type from protocol (no more streaming results)

### Key technical learnings
- GTK3 CSS unreliable for labels in ScrolledWindow → use Pango attributes or GtkTextView
- sounddevice lists BT devices by description ("WH-1000XM5"), not by bluez name
- WirePlumber `autoswitch-bluetooth-profile.lua` has 2000ms restore timeout that fights manual `pactl set-card-profile` → must disable via `wpctl settings`
- Unix stream sockets don't preserve message boundaries — buffer for 4-byte float32 alignment
