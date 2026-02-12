#!/usr/bin/env python3
"""GTK3 popup dictation UI. Thin client that captures audio, sends it to
the dictation daemon for transcription, and pastes the result."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango

import os
import socket
import sounddevice as sd
import numpy as np
import pyperclip
import queue
import subprocess
import threading
import time

# =========================
# Configuration
# =========================

WHISPER_RATE = 16000
CHANNELS = 1
BLOCK_MS = 30
SOCKET_PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "dictation.sock")

# =========================
# Audio capture
# =========================

audio_queue = queue.Queue()
capture_rate = WHISPER_RATE


def audio_callback(indata, frames, time_info, status):
    mono = indata[:, 0].copy()
    if capture_rate != WHISPER_RATE:
        ratio = WHISPER_RATE / capture_rate
        n_out = int(len(mono) * ratio)
        indices = np.minimum(
            (np.arange(n_out) / ratio).astype(int), len(mono) - 1
        )
        mono = mono[indices]
    audio_queue.put(mono)


def find_input_device(bt_device_name=None):
    """Find input device index. Skips test-stream opens for fast startup.
    If bt_device_name is set, prefer a device matching that name."""
    devices = sd.query_devices()

    if bt_device_name:
        bt_lower = bt_device_name.lower()
        for i, dev in enumerate(devices):
            if dev["max_input_channels"] > 0 and bt_lower in dev["name"].lower():
                return i

    # Try system default first
    try:
        dev_info = sd.query_devices(kind="input")
        return next(i for i, d in enumerate(devices) if d["name"] == dev_info["name"])
    except Exception:
        pass

    # Fallback: any device with input channels
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            return i

    raise RuntimeError("No input devices found. Is a microphone connected?")


# =========================
# Bluetooth codec management
# =========================


def get_bt_card():
    """Find the active Bluetooth audio card, its current profile, and device description.
    Returns (card_name, active_profile, device_description) or (None, None, None)."""
    try:
        result = subprocess.run(
            ["pactl", "list", "cards"], capture_output=True, text=True, timeout=5
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, None, None

    card_name = None
    active_profile = None
    device_desc = None
    in_bluez_card = False

    for line in result.stdout.split("\n"):
        stripped = line.strip()
        if stripped.startswith("Name:") and "bluez" in stripped.lower():
            card_name = stripped.split("Name:", 1)[1].strip()
            in_bluez_card = True
        elif stripped.startswith("Name:"):
            in_bluez_card = False
        elif in_bluez_card and stripped.startswith("device.description ="):
            # e.g. device.description = "WH-1000XM5"
            device_desc = stripped.split("=", 1)[1].strip().strip('"')
        elif in_bluez_card and stripped.startswith("Active Profile:"):
            active_profile = stripped.split("Active Profile:", 1)[1].strip()
            break

    return card_name, active_profile, device_desc


def find_hfp_profile(card_name):
    """Find an HFP profile (mSBC preferred) for mic input."""
    try:
        result = subprocess.run(
            ["pactl", "list", "cards"], capture_output=True, text=True, timeout=5
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    in_card = False
    in_profiles = False
    profiles = []  # list of (profile_name, full_description_line)

    for line in result.stdout.split("\n"):
        stripped = line.strip()
        if stripped.startswith("Name:") and card_name in stripped:
            in_card = True
        elif stripped.startswith("Name:"):
            in_card = False
        elif in_card and stripped.startswith("Profiles:"):
            in_profiles = True
        elif in_card and in_profiles:
            # Profile lines contain "(sinks: N, sources: N, ...)"
            if "sinks:" in stripped and ":" in stripped:
                profile = stripped.split(":")[0].strip()
                profiles.append((profile, stripped))
            else:
                in_profiles = False

    # Prefer mSBC over CVSD — check full description line for codec name
    for p, desc in profiles:
        if "headset" in p.lower() and "msbc" in desc.lower():
            return p
    for p, desc in profiles:
        if "headset" in p.lower():
            return p
    return None


def set_bt_profile(card_name, profile):
    """Switch Bluetooth card to a given profile."""
    if not card_name or not profile:
        return
    try:
        subprocess.run(
            ["pactl", "set-card-profile", card_name, profile],
            capture_output=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def set_wp_autoswitch(enabled):
    """Enable or disable WirePlumber's BT auto-profile-switching."""
    try:
        subprocess.run(
            ["wpctl", "settings", "bluetooth.autoswitch-to-headset-profile",
             "true" if enabled else "false"],
            capture_output=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


# =========================
# Helpers
# =========================


FONT_SIZE_PT = 20  # Consistent font size in points for all text


def _set_label_font(label, size_pt, color_hex="#cdd6f4"):
    """Set font size and color on a GtkLabel using Pango attributes."""
    attrs = Pango.AttrList()
    attrs.insert(Pango.attr_size_new(size_pt * Pango.SCALE))
    r, g, b = int(color_hex[1:3], 16), int(color_hex[3:5], 16), int(color_hex[5:7], 16)
    attrs.insert(Pango.attr_foreground_new(r * 257, g * 257, b * 257))
    label.set_attributes(attrs)


# =========================
# GTK Window
# =========================


class DictationWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Dictation")
        self.set_decorated(False)
        self.set_default_size(1200, -1)
        self.set_position(Gtk.WindowPosition.CENTER)

        # Screen-level CSS for window background and border
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            window {
                background-color: #1e1e2e;
                border: 2px solid #585b70;
            }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # Layout: just the status label
        self.timer_label = Gtk.Label(label="Starting...")
        _set_label_font(self.timer_label, FONT_SIZE_PT)
        self.timer_label.set_margin_top(28)
        self.timer_label.set_margin_start(32)
        self.timer_label.set_margin_end(32)
        self.timer_label.set_margin_bottom(28)
        self.add(self.timer_label)

        self.connect("key-press-event", self.on_key_press)
        self.connect("destroy", Gtk.main_quit)

        self.recording = False
        self.cancelled = False
        self.record_start = None
        self.timer_id = None
        self.stream = None
        self.stop_event = threading.Event()
        self.sock = None
        self.final_text = ""
        self.bt_card = None
        self.bt_original_profile = None
        self.wp_autoswitch_disabled = False

        # Start audio capture in a worker thread
        threading.Thread(target=self.worker_init, daemon=True).start()

    def worker_init(self):
        global capture_rate

        # Switch Bluetooth to HFP (mSBC) for headset mic access.
        # Disable WirePlumber autoswitch to prevent it from fighting our
        # manual profile change (it has a 2s timeout that restores A2DP).
        bt_device_name = None
        self.bt_card, self.bt_original_profile, bt_desc = get_bt_card()
        if self.bt_card:
            hfp = find_hfp_profile(self.bt_card)
            if hfp:
                bt_device_name = bt_desc
                set_wp_autoswitch(False)
                self.wp_autoswitch_disabled = True
                if self.bt_original_profile and hfp != self.bt_original_profile:
                    set_bt_profile(self.bt_card, hfp)
                    time.sleep(0.5)  # let PipeWire settle after profile switch

        try:
            device = find_input_device(bt_device_name=bt_device_name)
        except RuntimeError as e:
            GLib.idle_add(self.show_error, str(e))
            return

        dev_info = sd.query_devices(device)
        capture_rate = int(dev_info["default_samplerate"])
        block_size = int(capture_rate * BLOCK_MS / 1000)

        try:
            self.stream = sd.InputStream(
                device=device,
                samplerate=capture_rate,
                channels=CHANNELS,
                dtype=np.float32,
                blocksize=block_size,
                callback=audio_callback,
            )
            self.stream.start()
        except Exception as e:
            GLib.idle_add(self.show_error, f"Audio error: {e}")
            return

        # Connect to daemon
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.connect(SOCKET_PATH)
        except (ConnectionRefusedError, FileNotFoundError):
            GLib.idle_add(self.show_error, "Daemon not running")
            return

        # Start reader thread to receive messages from daemon
        threading.Thread(target=self.read_daemon_messages, daemon=True).start()

        # Signal the GUI to start recording
        GLib.idle_add(self.start_recording)

        # Writer loop: stream audio to daemon
        self.stream_audio_to_daemon()

    def stream_audio_to_daemon(self):
        """Send audio chunks to daemon until stop_event is set."""
        while not self.stop_event.is_set():
            try:
                chunk = audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.sock.sendall(chunk.tobytes())
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        # Drain remaining audio from queue
        while not audio_queue.empty():
            try:
                chunk = audio_queue.get_nowait()
                self.sock.sendall(chunk.tobytes())
            except (queue.Empty, BrokenPipeError, ConnectionResetError, OSError):
                break

        # Signal end of audio
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def read_daemon_messages(self):
        """Read newline-delimited messages from daemon."""
        buf = b""
        try:
            while True:
                data = self.sock.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self.handle_daemon_line(line.decode("utf-8", errors="replace"))
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            if not self.cancelled:
                GLib.idle_add(self.finish, self.final_text)

    def handle_daemon_line(self, line):
        """Parse an F or L line from the daemon."""
        if line.startswith("F "):
            self.final_text = line[2:]
        elif line.startswith("L "):
            try:
                dur = float(line[2:])
                GLib.idle_add(self.timer_label.set_text,
                              f"Transcribing {dur:.1f}s...")
            except ValueError:
                pass

    def show_error(self, message):
        self.timer_label.set_text(message)
        GLib.timeout_add(3000, Gtk.main_quit)
        return False

    def start_recording(self):
        self.recording = True
        self.record_start = time.time()
        self.update_timer()
        self.timer_id = GLib.timeout_add(500, self.update_timer)
        return False

    def update_timer(self):
        if not self.recording:
            return False
        elapsed = time.time() - self.record_start
        mins, secs = divmod(int(elapsed), 60)
        self.timer_label.set_text(f"Recording...  {mins:02d}:{secs:02d}")
        return True

    def on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.stop_and_transcribe()
            return True
        if event.keyval == Gdk.KEY_c and event.state & Gdk.ModifierType.CONTROL_MASK:
            self.cancel()
            return True
        return False

    def stop_and_transcribe(self):
        if not self.recording:
            return
        self.recording = False
        self.timer_label.set_text("Transcribing...")

        # Stop audio capture
        if self.stream:
            self.stream.stop()

        # Signal writer thread to drain and shut down
        self.stop_event.set()

    def cancel(self):
        """Cancel — close everything without pasting."""
        self.cancelled = True
        self.recording = False
        if self.timer_id:
            GLib.source_remove(self.timer_id)
            self.timer_id = None
        self.stop_event.set()
        if self.stream:
            self.stream.close()
            self.stream = None
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self._restore_bt_profile()
        Gtk.main_quit()

    def _restore_bt_profile(self):
        """Restore the original Bluetooth profile and re-enable WirePlumber autoswitch."""
        if self.bt_card and self.bt_original_profile:
            set_bt_profile(self.bt_card, self.bt_original_profile)
        if self.wp_autoswitch_disabled:
            set_wp_autoswitch(True)
            self.wp_autoswitch_disabled = False

    def finish(self, text):
        if self.cancelled:
            return False

        if self.stream:
            self.stream.close()
            self.stream = None
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

        self._restore_bt_profile()

        if text:
            pyperclip.copy(text)
        Gtk.main_quit()

        # Refocus the previous window and paste
        if text and getattr(self, "prev_window", None):
            paste_to_window(self.prev_window, text)

        return False


def get_active_window_id():
    """Get the currently focused window ID before our popup appears."""
    try:
        result = subprocess.run(
            ["xdotool", "getactivewindow"], capture_output=True, text=True
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except FileNotFoundError:
        return None


def get_window_class(window_id):
    """Get the WM_CLASS of a window."""
    try:
        result = subprocess.run(
            ["xdotool", "getwindowclassname", window_id],
            capture_output=True, text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except FileNotFoundError:
        return ""


def paste_to_window(window_id, text):
    """Refocus the previous window and paste using the appropriate method."""
    # Also copy to PRIMARY selection so xterm's Shift+Insert works
    subprocess.run(["xclip", "-selection", "primary"], input=text.encode())

    subprocess.run(["xdotool", "windowactivate", "--sync", window_id])

    wm_class = get_window_class(window_id)
    if wm_class.lower() in ("xterm", "uxterm"):
        subprocess.run(["xdotool", "key", "--clearmodifiers", "shift+Insert"])
    else:
        subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v"])


def main():
    prev_window = get_active_window_id()
    win = DictationWindow()
    win.prev_window = prev_window
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
