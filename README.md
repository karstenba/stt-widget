# SST Widget

Local speech-to-text dictation tool using [faster-whisper](https://github.com/SYSTRAN/faster-whisper). A persistent daemon holds the Whisper model in memory, and a GTK popup (triggered by an i3 keybinding) captures audio, sends it to the daemon for transcription, copies the result to clipboard, and auto-pastes into the previously focused application.

## How it works

Bind `dictation_gui.py` to a keyboard shortcut. When triggered:

1. A small floating window appears and starts recording
2. Speak — the window shows a recording timer
3. Press `ESC` — recording stops, audio is transcribed
4. The transcribed text is copied to clipboard and pasted into the previously focused window
5. Press `Ctrl+C` to cancel without pasting

Language is auto-detected per utterance (English, German, Hungarian, etc.).

## Setup

### Dependencies

```bash
# System packages (Arch Linux)
sudo pacman -S python-gobject gtk3 xdotool xclip

# Python venv
python -m venv --system-site-packages venv
venv/bin/pip install faster-whisper sounddevice numpy pyperclip
```

### Daemon

```bash
# Start manually
venv/bin/python dictation_server.py

# Or via systemd
systemctl --user enable --now dictation
```

### i3 config

```
for_window [title="^Dictation$"] floating enable
bindsym $mod+Shift+d exec /path/to/venv/bin/python /path/to/dictation_gui.py
```

## Tested environment

This has only been tested on:

- **OS**: Arch Linux (x86_64)
- **Laptop**: Framework 13 (AMD Ryzen, 32 GB RAM, no discrete GPU)
- **Headset**: Sony WH-1000XM5 (Bluetooth, mSBC/HFP for mic input)
- **Audio**: PipeWire + WirePlumber
- **Display**: X11 with i3 window manager

Nothing else is intended to be supported.

## Disclaimer

This code has been entirely AI-generated and has not been meaningfully reviewed. Use at your own risk.
