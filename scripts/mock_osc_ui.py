"""
Easy test UI for Adaptive Drive Music System.

Usage:
    python scripts/mock_osc_ui.py
    python scripts/mock_osc_ui.py --hz 50
"""

from __future__ import annotations

import argparse
import math
import random
import socket
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

from pythonosc.osc_message_builder import OscMessageBuilder

HOST = "127.0.0.1"
PORT = 9000
OSC_HZ = 50

ADDR_RPM = "/car/rpm"
ADDR_SPEED = "/car/speed"
ADDR_THROTTLE = "/car/throttle"
ADDR_BRAKE = "/car/brake"
ADDR_GEAR = "/car/gear"

RPM_MIN, RPM_MAX = 800.0, 7000.0
SPEED_MAX = 220.0


@dataclass
class Phase:
    dur: float
    label: str
    rpm_start: float
    rpm_end: float
    throttle_start: float
    throttle_end: float
    brake: float
    gear: int


@dataclass
class CarState:
    rpm: float = 800.0
    throttle: float = 0.0
    brake: float = 0.0
    gear: int = 0

    def same_as(self, other: CarState, rpm_eps: float = 0.5) -> bool:
        return (
            abs(self.rpm - other.rpm) <= rpm_eps
            and abs(self.throttle - other.throttle) < 0.05
            and abs(self.brake - other.brake) < 0.001
            and self.gear == other.gear
        )


CYCLE: list[Phase] = [
    Phase(4.0, "idle", 800, 900, 0, 0, 0.0, 0),
    Phase(4.0, "accel-1", 900, 3500, 60, 80, 0.0, 2),
    Phase(5.0, "cruise-1", 3500, 3400, 65, 55, 0.0, 4),
    Phase(3.0, "hard-accel", 3400, 6500, 95, 100, 0.0, 3),
    Phase(4.0, "cruise-2", 6500, 3200, 60, 55, 0.0, 5),
    Phase(3.5, "brake", 3200, 1200, 10, 0, 0.6, 3),
    Phase(3.0, "idle-end", 1200, 800, 0, 0, 0.0, 0),
]


def rpm_noise(base: float, amount: float = 40.0) -> float:
    return max(RPM_MIN, min(RPM_MAX, base + random.uniform(-amount, amount)))


def rpm_to_speed(rpm: float) -> float:
    return max(0.0, min(SPEED_MAX, (rpm - RPM_MIN) / (RPM_MAX - RPM_MIN) * 180.0))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def intensity_from_rpm(rpm: float) -> float:
    return max(0.0, min(1.0, (rpm - 800) / 6200))


def gain_from_intensity(intensity: float, lo: float, hi: float, peak: float) -> float:
    if intensity < lo:
        return 0.0
    normalized = min(1.0, (intensity - lo) / (hi - lo))
    return normalized * peak


class FastOscSender:
    """Single UDP socket, minimal overhead."""

    def __init__(self, host: str, port: int):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._addr = (host, port)

    def send(self, address: str, *values) -> None:
        msg = OscMessageBuilder(address=address)
        for v in values:
            msg.add_arg(v)
        self._sock.sendto(msg.build().dgram, self._addr)

    def send_car(self, state: CarState) -> None:
        speed = rpm_to_speed(state.rpm)
        self.send(ADDR_RPM, state.rpm)
        self.send(ADDR_SPEED, speed)
        self.send(ADDR_THROTTLE, state.throttle)
        self.send(ADDR_BRAKE, state.brake)
        self.send(ADDR_GEAR, state.gear)

    def close(self) -> None:
        self._sock.close()


class DriveMockUI:
    def __init__(self, root: tk.Tk, osc_hz: int):
        self.root = root
        self.osc_hz = max(10, min(osc_hz, 100))
        self.tick_ms = max(10, int(1000 / self.osc_hz))
        root.title("Adaptive Drive Music - Test UI")
        root.geometry("720x580")
        root.resizable(False, False)

        self.sender = FastOscSender(HOST, PORT)
        self.mode = "manual"
        self._last_sent = CarState()
        self._last_send_mono = 0.0
        self._script_t0 = time.perf_counter()
        self._script_phase_idx = 0
        self._script_phase_t0 = time.perf_counter()
        self._cycle_text = "Stopped"
        self._ui_dirty = True

        self.rpm = tk.DoubleVar(value=800.0)
        self.throttle = tk.DoubleVar(value=0.0)
        self.brake = tk.DoubleVar(value=0.0)
        self.gear = tk.IntVar(value=0)

        self.create_widgets()
        self.update_status(f"OSC {HOST}:{PORT} @ {self.osc_hz} Hz — Manual")

        self.rpm.trace_add("write", self._mark_dirty)
        self.throttle.trace_add("write", self._mark_dirty)
        self.brake.trace_add("write", self._mark_dirty)

        self._tick()

        root.bind("<KeyPress-w>", self.key_press_w)
        root.bind("<KeyPress-W>", self.key_press_w)
        root.bind("<KeyPress-s>", self.key_press_s)
        root.bind("<KeyPress-S>", self.key_press_s)

    def _mark_dirty(self, *_):
        self._ui_dirty = True

    def _read_manual_state(self) -> CarState:
        thr = float(self.throttle.get())
        gear = min(6, max(0, int(thr / 18)))
        return CarState(
            rpm=float(self.rpm.get()),
            throttle=thr,
            brake=float(self.brake.get()),
            gear=gear,
        )

    def _scripted_state(self) -> CarState:
        phase = CYCLE[self._script_phase_idx]
        elapsed = time.perf_counter() - self._script_phase_t0
        if elapsed >= phase.dur:
            self._script_phase_idx = (self._script_phase_idx + 1) % len(CYCLE)
            self._script_phase_t0 = time.perf_counter()
            phase = CYCLE[self._script_phase_idx]
            elapsed = 0.0

        p = elapsed / phase.dur
        ease = 0.5 - 0.5 * math.cos(math.pi * max(0.0, min(1.0, p)))
        rpm = rpm_noise(lerp(phase.rpm_start, phase.rpm_end, ease), 40)
        thr = max(0.0, min(100.0, lerp(phase.throttle_start, phase.throttle_end, ease)))
        self._cycle_text = f"Phase: {phase.label} ({elapsed:.1f}s / {phase.dur}s)"
        return CarState(rpm=rpm, throttle=thr, brake=phase.brake, gear=phase.gear)

    def _current_state(self) -> CarState:
        if self.mode == "scripted":
            return self._scripted_state()
        return self._read_manual_state()

    def _send_now(self, state: CarState) -> None:
        self.sender.send_car(state)
        self._last_sent = state
        self._last_send_mono = time.perf_counter()

    def _maybe_send(self, state: CarState, force: bool = False) -> None:
        min_interval = 1.0 / self.osc_hz
        due = (time.perf_counter() - self._last_send_mono) >= min_interval
        if force or (due and not state.same_as(self._last_sent)):
            self._send_now(state)

    def create_widgets(self):
        status_frame = ttk.LabelFrame(self.root, text="OSC Status", padding=10)
        status_frame.grid(row=0, column=0, padx=10, pady=5, sticky="ew")
        self.status_label = ttk.Label(status_frame, text="Connecting...")
        self.status_label.pack()

        mode_frame = ttk.LabelFrame(self.root, text="Control Mode", padding=10)
        mode_frame.grid(row=1, column=0, padx=10, pady=5, sticky="ew")

        self.mode_var = tk.StringVar(value="manual")
        ttk.Radiobutton(
            mode_frame, text="Manual (sliders)", variable=self.mode_var,
            value="manual", command=self.set_mode,
        ).pack(side="left", padx=5)
        ttk.Radiobutton(
            mode_frame, text="Scripted Cycle", variable=self.mode_var,
            value="scripted", command=self.set_mode,
        ).pack(side="left", padx=5)
        ttk.Radiobutton(
            mode_frame, text="Interactive (W/S keys)", variable=self.mode_var,
            value="interactive", command=self.set_mode,
        ).pack(side="left", padx=5)

        manual_frame = ttk.LabelFrame(self.root, text="Manual Control", padding=10)
        manual_frame.grid(row=2, column=0, padx=10, pady=5, sticky="ew")

        ttk.Label(manual_frame, text="RPM (800-7000):").grid(row=0, column=0, sticky="w", pady=2)
        self.rpm_scale = ttk.Scale(
            manual_frame, from_=RPM_MIN, to=RPM_MAX, variable=self.rpm,
            orient="horizontal", length=500,
        )
        self.rpm_scale.grid(row=0, column=1, sticky="ew", padx=5)
        self.rpm_label = ttk.Label(manual_frame, text="800", width=8)
        self.rpm_label.grid(row=0, column=2, sticky="w")

        ttk.Label(manual_frame, text="Throttle (0-100):").grid(row=1, column=0, sticky="w", pady=2)
        self.throttle_scale = ttk.Scale(
            manual_frame, from_=0, to=100, variable=self.throttle,
            orient="horizontal", length=500,
        )
        self.throttle_scale.grid(row=1, column=1, sticky="ew", padx=5)
        self.throttle_label = ttk.Label(manual_frame, text="0", width=8)
        self.throttle_label.grid(row=1, column=2, sticky="w")

        ttk.Label(manual_frame, text="Brake (0-1):").grid(row=2, column=0, sticky="w", pady=2)
        self.brake_scale = ttk.Scale(
            manual_frame, from_=0, to=1, variable=self.brake,
            orient="horizontal", length=500,
        )
        self.brake_scale.grid(row=2, column=1, sticky="ew", padx=5)
        self.brake_label = ttk.Label(manual_frame, text="0.0", width=8)
        self.brake_label.grid(row=2, column=2, sticky="w")

        manual_frame.columnconfigure(1, weight=1)

        preset_frame = ttk.Frame(manual_frame)
        preset_frame.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(preset_frame, text="Presets:").pack(side="left", padx=(0, 6))
        for rpm in (800, 1500, 3500, 7000):
            ttk.Button(
                preset_frame, text=str(rpm),
                command=lambda v=rpm: self.set_rpm_preset(v),
            ).pack(side="left", padx=3)

        readout_frame = ttk.LabelFrame(self.root, text="Live Values (sent to Pd)", padding=10)
        readout_frame.grid(row=3, column=0, padx=10, pady=5, sticky="ew")

        ttk.Label(readout_frame, text="Intensity:").grid(row=0, column=0, sticky="w", pady=2)
        self.intensity_label = ttk.Label(
            readout_frame, text="0.00", font=("Consolas", 12, "bold"), foreground="blue",
        )
        self.intensity_label.grid(row=0, column=1, sticky="w", padx=5)

        ttk.Label(readout_frame, text="Gear:").grid(row=0, column=2, sticky="w", pady=2, padx=10)
        self.gear_label = ttk.Label(readout_frame, text="0", font=("Consolas", 12, "bold"))
        self.gear_label.grid(row=0, column=3, sticky="w", padx=5)

        ttk.Label(readout_frame, text="Speed:").grid(row=1, column=0, sticky="w", pady=2)
        self.speed_label = ttk.Label(readout_frame, text="0 km/h", font=("Consolas", 12))
        self.speed_label.grid(row=1, column=1, sticky="w", padx=5)

        gains_frame = ttk.LabelFrame(self.root, text="Stem Gains (estimated from intensity)", padding=10)
        gains_frame.grid(row=4, column=0, padx=10, pady=5, sticky="ew")

        self.gain_labels = {}
        stems = [
            ("pad", 0.0, 1.0, 0.5),
            ("kick", 0.06, 0.20, 0.8),
            ("perc", 0.22, 0.33, 0.7),
            ("bass", 0.35, 0.48, 0.85),
            ("lead", 0.51, 0.70, 0.7),
        ]
        for i, (name, lo, hi, peak) in enumerate(stems):
            ttk.Label(gains_frame, text=f"{name}:").grid(row=i, column=0, sticky="w", pady=2)
            lbl = ttk.Label(gains_frame, text="0.00", font=("Consolas", 11), width=8)
            lbl.grid(row=i, column=1, sticky="w", padx=5)
            self.gain_labels[name] = (lbl, lo, hi, peak)

        cycle_frame = ttk.LabelFrame(self.root, text="Scripted Cycle Status", padding=10)
        cycle_frame.grid(row=5, column=0, padx=10, pady=5, sticky="ew")
        self.cycle_label = ttk.Label(cycle_frame, text="Stopped", font=("Consolas", 11))
        self.cycle_label.pack()

        instr_frame = ttk.LabelFrame(self.root, text="Instructions", padding=10)
        instr_frame.grid(row=6, column=0, padx=10, pady=5, sticky="ew")
        ttk.Label(
            instr_frame,
            text="1. Start Pd: open engine.pd from project root (not patches/)",
            font=("Consolas", 9),
        ).pack(anchor="w")
        ttk.Label(
            instr_frame, text="2. Adjust sliders or select a mode above", font=("Consolas", 9),
        ).pack(anchor="w")
        ttk.Label(
            instr_frame, text="3. Interactive mode: press W to accelerate, S to brake",
            font=("Consolas", 9),
        ).pack(anchor="w")

    def set_rpm_preset(self, rpm: float):
        self.mode_var.set("manual")
        self.set_mode()
        self.rpm.set(float(rpm))
        self.throttle.set(min(100.0, max(0.0, (rpm - RPM_MIN) / (RPM_MAX - RPM_MIN) * 100.0)))
        state = self._read_manual_state()
        self._send_now(state)
        self._paint_readouts(state)

    def update_status(self, text: str):
        self.status_label.config(text=text)

    def set_mode(self):
        mode = self.mode_var.get()
        if mode == self.mode:
            return
        self.mode = mode
        self._script_phase_idx = 0
        self._script_phase_t0 = time.perf_counter()
        if mode == "manual":
            self.update_status(f"Manual @ {self.osc_hz} Hz")
        elif mode == "scripted":
            self.update_status(f"Scripted cycle @ {self.osc_hz} Hz")
        elif mode == "interactive":
            self.update_status(f"Interactive @ {self.osc_hz} Hz — W/S keys")

    def _paint_readouts(self, state: CarState) -> None:
        self.rpm_label.config(text=f"{int(state.rpm)}")
        self.throttle_label.config(text=f"{int(state.throttle)}")
        self.brake_label.config(text=f"{state.brake:.2f}")
        self.gear.set(state.gear)
        self.gear_label.config(text=str(state.gear))

        intensity = intensity_from_rpm(state.rpm)
        self.intensity_label.config(text=f"{intensity:.2f}")
        speed = rpm_to_speed(state.rpm)
        self.speed_label.config(text=f"{int(speed)} km/h")

        for name, (lbl, lo, hi, peak) in self.gain_labels.items():
            lbl.config(text=f"{gain_from_intensity(intensity, lo, hi, peak):.2f}")

        if self.mode == "scripted":
            self.cycle_label.config(text=self._cycle_text)

    def key_press_w(self, _):
        if self.mode != "interactive":
            return
        thr = min(100, self.throttle.get() + 8)
        self.throttle.set(thr)
        rpm_target = RPM_MIN + (RPM_MAX - RPM_MIN) * (thr / 100.0)
        self.rpm.set(self.rpm.get() * 0.85 + rpm_noise(rpm_target, 30) * 0.15)

    def key_press_s(self, _):
        if self.mode != "interactive":
            return
        thr = max(0, self.throttle.get() - 8)
        self.throttle.set(thr)
        rpm_target = RPM_MIN + (RPM_MAX - RPM_MIN) * (thr / 100.0)
        self.rpm.set(self.rpm.get() * 0.85 + rpm_noise(rpm_target, 30) * 0.15)

    def _tick(self):
        state = self._current_state()
        self._maybe_send(state)

        if self._ui_dirty or self.mode == "scripted":
            self._paint_readouts(state)
            self._ui_dirty = False

        self.root.after(self.tick_ms, self._tick)

    def on_closing(self):
        self.sender.close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="Adaptive Drive Music OSC mock UI")
    parser.add_argument("--hz", type=int, default=OSC_HZ, help=f"Max OSC rate (default {OSC_HZ})")
    args = parser.parse_args()

    root = tk.Tk()
    app = DriveMockUI(root, osc_hz=args.hz)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()
