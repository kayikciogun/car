"""
Generate engine.pd — phasor-derived clock + state-machine stems.

Design:
  - Global clock counts beats 1-2-3-4 from phasor (sample-accurate).
  - When RPM crosses threshold, stem is ARMED.
  - On the NEXT beat 1 (downbeat), stem starts PLAYING.
  - When RPM drops below threshold, stem goes PENDING_OFF.
  - On the NEXT beat 1, stem stops.
  - All stems share the same global clock → always bar-synchronized.

States:  0=IDLE  1=ARMED  2=PLAYING  3=PENDING_OFF
"""

from pathlib import Path

BPM = 125
LOOP_SAMPLES = 184320
READ_INDEX = LOOP_SAMPLES - 1
STEMS = ["pad", "kick", "perc", "bass", "lead"]
BEAT_MS = int(60000 / BPM)   # 480

STEM_RPM = {
    "kick": (1200, 1000, 0.8),
    "perc": (2200, 1900, 0.7),
    "bass": (3000, 2700, 0.85),
    "lead": (4000, 3700, 0.7),
}


class PdCanvas:
    def __init__(self):
        self.lines: list[str] = []
        self.items: list[tuple[str, str]] = []
        self.connects: list[tuple[int, int, int, int]] = []

    def text(self, x, y, s):
        self.lines.append(f"#X text {x} {y} {s};")
        self.items.append(("text", s[:40]))

    def obj(self, x, y, spec):
        idx = len(self.items)
        self.lines.append(f"#X obj {x} {y} {spec};")
        self.items.append(("obj", spec))
        return idx

    def msg(self, x, y, spec):
        idx = len(self.items)
        self.lines.append(f"#X msg {x} {y} {spec};")
        self.items.append(("msg", spec))
        return idx

    def connect(self, a, ao, b, bi):
        self.connects.append((a, ao, b, bi))

    def nbx(self, x, y, label, vmin, vmax, init):
        idx = len(self.items)
        self.lines.append(
            f"#X obj {x} {y} nbx 5 14 {vmin} {vmax} {init} 0 1 empty empty {label} "
            f"0 -8 0 10 -262144 -1 -1 0 256;"
        )
        self.items.append(("nbx", label))
        return idx

    def build(self, w=2000, h=1500):
        out = [f"#N canvas 0 0 {w} {h} 12;"]
        out.extend(self.lines)
        for a, ao, b, bi in self.connects:
            out.append(f"#X connect {a} {ao} {b} {bi};")
        return "\n".join(out) + "\n"


# ── state-machine stem ──────────────────────────────────────────
def add_state_machine_stem(c, x, y, name, rpm_on, rpm_off, peak,
                           i_r_rpm, i_r_downbeat):
    """
    State stored in [value {name}St].  Each handler has its own read
    instance → zero crosstalk.

    States:
      0 IDLE
      1 ARMED       (RPM crossed threshold, waiting for next beat-1)
      2 PLAYING     (gain up, sound audible)
      3 PENDING_OFF (RPM dropped, waiting for next beat-1 to stop)

    Transitions:
      ARM   (RPM >= on):   0 → 1
      DISARM (RPM < off):  1 → 0  (immediate, gain is still 0)
                           2 → 3  (defer stop to next downbeat)
      DOWNBEAT (beat 1):   1 → 2  (gain up)
                           3 → 0  (gain off)
    """
    stVar = f"{name}St"

    c.text(x, y,
           f"{name}: arm>={rpm_on} disarm<{rpm_off} | play on next beat-1")
    y += 25

    # ── RPM comparators + edge detect ──
    i_ge = c.obj(x, y, f">= {rpm_on}")
    i_lt = c.obj(x, y + 30, f"< {rpm_off}")
    c.connect(i_r_rpm, 0, i_ge, 0)
    c.connect(i_r_rpm, 0, i_lt, 0)

    i_ch_arm = c.obj(x + 100, y, "change")
    i_s1_arm = c.obj(x + 170, y, "select 1")
    c.connect(i_ge, 0, i_ch_arm, 0)
    c.connect(i_ch_arm, 0, i_s1_arm, 0)

    i_ch_dis = c.obj(x + 100, y + 30, "change")
    i_s1_dis = c.obj(x + 170, y + 30, "select 1")
    c.connect(i_lt, 0, i_ch_dis, 0)
    c.connect(i_ch_dis, 0, i_s1_dis, 0)

    # ── ARM handler ──
    # Read state; if IDLE(0) → set ARMED(1); if PENDING_OFF(3) → set PLAYING(2)
    arm_y = y + 70
    i_v_arm = c.obj(x, arm_y, f"value {stVar}")
    i_sel_arm = c.obj(x + 100, arm_y, "select 0 3")
    i_msg_arm1 = c.msg(x + 200, arm_y, "1")
    i_v_arm_w1 = c.obj(x + 240, arm_y, f"value {stVar}")
    i_msg_arm2 = c.msg(x + 300, arm_y, "2")
    i_v_arm_w2 = c.obj(x + 340, arm_y, f"value {stVar}")
    
    i_pr_arm = c.obj(x + 200, arm_y + 30, f"print {name}-arm")
    i_pr_cancel = c.obj(x + 300, arm_y + 30, f"print {name}-cancel-off")

    c.connect(i_s1_arm, 0, i_v_arm, 0)
    c.connect(i_v_arm, 0, i_sel_arm, 0)
    # 0 -> 1
    c.connect(i_sel_arm, 0, i_msg_arm1, 0)
    c.connect(i_msg_arm1, 0, i_v_arm_w1, 0)
    c.connect(i_sel_arm, 0, i_pr_arm, 0)
    # 3 -> 2
    c.connect(i_sel_arm, 1, i_msg_arm2, 0)
    c.connect(i_msg_arm2, 0, i_v_arm_w2, 0)
    c.connect(i_sel_arm, 1, i_pr_cancel, 0)

    # ── DISARM handler ──
    # Read state; if 1 → 0 (immediate), if 2 → 3 (pending)
    dis_y = arm_y + 60
    i_v_dis = c.obj(x, dis_y, f"value {stVar}")
    i_sel_dis = c.obj(x + 100, dis_y, "select 1 2")
    # outlets: 0=match1(ARMED)  1=match2(PLAYING)  2=reject
    i_msg_dis0 = c.msg(x + 200, dis_y, "0")
    i_v_dis_w0 = c.obj(x + 240, dis_y, f"value {stVar}")
    i_msg_dis3 = c.msg(x + 300, dis_y, "3")
    i_v_dis_w3 = c.obj(x + 340, dis_y, f"value {stVar}")
    i_pr_pending = c.obj(x + 300, dis_y + 30, f"print {name}-pending")

    c.connect(i_s1_dis, 0, i_v_dis, 0)
    c.connect(i_v_dis, 0, i_sel_dis, 0)
    # state 1 (ARMED) → 0 (immediate cancel, no audible change)
    c.connect(i_sel_dis, 0, i_msg_dis0, 0)
    c.connect(i_msg_dis0, 0, i_v_dis_w0, 0)
    # state 2 (PLAYING) → 3 (PENDING_OFF, defer to downbeat)
    c.connect(i_sel_dis, 1, i_msg_dis3, 0)
    c.connect(i_msg_dis3, 0, i_v_dis_w3, 0)
    c.connect(i_sel_dis, 1, i_pr_pending, 0)

    # ── DOWNBEAT handler ──
    # Read state; 1→2 (+gain on), 3→0 (+gain off)
    db_y = dis_y + 60
    i_v_db = c.obj(x, db_y, f"value {stVar}")
    i_sel_db = c.obj(x + 100, db_y, "select 1 3")
    # outlets: 0=match1(ARMED)  1=match3(PENDING_OFF)  2=reject
    i_msg_db2 = c.msg(x + 200, db_y, "2")
    i_v_db_w2 = c.obj(x + 240, db_y, f"value {stVar}")
    i_msg_db0 = c.msg(x + 300, db_y, "0")
    i_v_db_w0 = c.obj(x + 340, db_y, f"value {stVar}")
    i_pr_play = c.obj(x + 200, db_y + 30, f"print {name}-play")
    i_pr_idle = c.obj(x + 300, db_y + 30, f"print {name}-idle")

    c.connect(i_r_downbeat, 0, i_v_db, 0)
    c.connect(i_v_db, 0, i_sel_db, 0)
    # state 1 → 2 (ARMED → PLAYING)
    c.connect(i_sel_db, 0, i_msg_db2, 0)
    c.connect(i_msg_db2, 0, i_v_db_w2, 0)
    c.connect(i_sel_db, 0, i_pr_play, 0)
    # state 3 → 0 (PENDING_OFF → IDLE)
    c.connect(i_sel_db, 1, i_msg_db0, 0)
    c.connect(i_msg_db0, 0, i_v_db_w0, 0)
    c.connect(i_sel_db, 1, i_pr_idle, 0)

    # ── gain envelope ──
    gain_y = db_y + 60
    i_on_msg = c.msg(x, gain_y, f"{peak} 5")
    i_off_msg = c.msg(x + 100, gain_y, "0 5")
    i_ln = c.obj(x, gain_y + 30, "line~")
    i_sg = c.obj(x, gain_y + 60, f"s~ gain_{name}")
    i_lb = c.obj(x + 200, gain_y + 30, "loadbang")
    i_init = c.msg(x + 200, gain_y + 60, "0 10")

    # ARMED → PLAYING: gain on
    c.connect(i_sel_db, 0, i_on_msg, 0)
    c.connect(i_on_msg, 0, i_ln, 0)
    # PENDING_OFF → IDLE: gain off
    c.connect(i_sel_db, 1, i_off_msg, 0)
    c.connect(i_off_msg, 0, i_ln, 0)
    # Immediate disarm from ARMED: safety gain off
    c.connect(i_sel_dis, 0, i_off_msg, 0)

    c.connect(i_lb, 0, i_init, 0)
    c.connect(i_init, 0, i_ln, 0)
    c.connect(i_ln, 0, i_sg, 0)

    return gain_y + 100


# ── main generator ───────────────────────────────────────────────
def generate():
    c = PdCanvas()
    y = 10
    c.text(20, y,
           f"engine.pd -- phasor clock @ {BPM} BPM \\, "
           f"stems play on next beat-1 \\, quantized exit")
    y += 18
    c.text(20, y,
           f"Loop: {LOOP_SAMPLES} samples. OSC: python scripts/mock_osc_ui.py")
    y += 30

    osc_y = y
    # ── OSC input ───
    c.text(20, osc_y,
           "--- OSC: netreceive -> oscparse -> list trim -> route car ---")
    i_net   = c.obj(20, osc_y + 20,  "netreceive -u -b 9000")
    i_parse = c.obj(20, osc_y + 50,  "oscparse")
    i_trim  = c.obj(20, osc_y + 80,  "list trim")
    i_rcar  = c.obj(20, osc_y + 110, "route car")
    i_rvals = c.obj(20, osc_y + 140, "route rpm speed throttle brake gear")
    i_frpm  = c.obj(20, osc_y + 170, "float")
    i_srpm  = c.obj(20, osc_y + 200, "s rpm")
    i_sspd  = c.obj(120, osc_y + 200, "s speed")
    i_sthr  = c.obj(220, osc_y + 200, "s throttle")
    i_sbrk  = c.obj(320, osc_y + 200, "s brake")
    i_sgear = c.obj(420, osc_y + 200, "s gear")
    i_prrpm = c.obj(20, osc_y + 230, "print osc-rpm")

    c.connect(i_net, 0, i_parse, 0)
    c.connect(i_parse, 0, i_trim, 0)
    c.connect(i_trim, 0, i_rcar, 0)
    c.connect(i_rcar, 0, i_rvals, 0)
    c.connect(i_rvals, 0, i_frpm, 0)
    c.connect(i_frpm, 0, i_srpm, 0)
    c.connect(i_rvals, 0, i_prrpm, 0)
    c.connect(i_rvals, 1, i_sspd, 0)
    c.connect(i_rvals, 2, i_sthr, 0)
    c.connect(i_rvals, 3, i_sbrk, 0)
    c.connect(i_rvals, 4, i_sgear, 0)

    i_nbx_rpm = c.nbx(20, osc_y + 240, "RPM", 800, 7000, 800)
    c.connect(i_nbx_rpm, 0, i_srpm, 0)
    i_lb_init = c.obj(20, osc_y + 280, "loadbang")
    i_msg_init = c.msg(20, osc_y + 310, "800")
    c.connect(i_lb_init, 0, i_msg_init, 0)
    c.connect(i_msg_init, 0, i_srpm, 0)

    i_r_rpm = c.obj(400, osc_y + 200, "r rpm")
    i_nbx_rx = c.nbx(400, osc_y + 240, "rx", 800, 7000, 800)
    c.connect(i_r_rpm, 0, i_nbx_rx, 0)

    # ── MASTER CLOCK ───
    clk_x = 620
    c.text(clk_x, osc_y,
           "--- MASTER CLOCK (phasor \\, 2-bar cycle = 3.84s) ---")
    i_lb_clk = c.obj(clk_x, osc_y + 20, "loadbang")
    i_msg_bpm = c.msg(clk_x, osc_y + 50, str(BPM))
    i_div     = c.obj(clk_x, osc_y + 80, "/ 480")
    i_sig     = c.obj(clk_x, osc_y + 110, "sig~")
    i_phasor  = c.obj(clk_x, osc_y + 140, "phasor~")
    i_sphase  = c.obj(clk_x, osc_y + 170, "s~ barPhase")

    c.connect(i_lb_clk, 0, i_msg_bpm, 0)
    c.connect(i_msg_bpm, 0, i_div, 0)
    c.connect(i_div, 0, i_sig, 0)
    c.connect(i_sig, 0, i_phasor, 0)
    c.connect(i_phasor, 0, i_sphase, 0)

    # ── PHASOR-DERIVED BEAT GRID ───
    # barPhase: 0→1 over 2 bars (8 beats @ 125 BPM)
    #   *~ 8 → wrap~ → threshold~ = beat tick  (every 480ms)
    #   *~ 2 → wrap~ → threshold~ = downbeat   (every 1920ms = beat 1)
    beat_y = osc_y + 200
    c.text(clk_x, beat_y,
           f"--- BEAT GRID {BPM} BPM (phasor-derived) ---")

    # beat tick (for display only)
    i_rp_bt  = c.obj(clk_x, beat_y + 25, "r~ barPhase")
    i_mul8   = c.obj(clk_x, beat_y + 55, "*~ 8")
    i_wr8    = c.obj(clk_x, beat_y + 85, "wrap~")
    i_thr_bt = c.obj(clk_x, beat_y + 115, "threshold~ 0.001 0 0.0005 0")
    i_s_bt   = c.obj(clk_x, beat_y + 145, "s beatTick")

    c.connect(i_rp_bt, 0, i_mul8, 0)
    c.connect(i_mul8, 0, i_wr8, 0)
    c.connect(i_wr8, 0, i_thr_bt, 0)
    c.connect(i_thr_bt, 0, i_s_bt, 0)

    # downbeat bang (beat 1): *~ 2 → wrap~ → threshold~
    # 2 wraps per 2-bar phasor cycle = every bar = every 4 beats
    i_rp_db  = c.obj(clk_x, beat_y + 185, "r~ barPhase")
    i_mul2   = c.obj(clk_x, beat_y + 215, "*~ 2")
    i_wr2    = c.obj(clk_x, beat_y + 245, "wrap~")
    i_thr_db = c.obj(clk_x, beat_y + 275, "threshold~ 0.001 0 0.0005 0")
    i_s_db   = c.obj(clk_x, beat_y + 305, "s downbeat")

    c.connect(i_rp_db, 0, i_mul2, 0)
    c.connect(i_mul2, 0, i_wr2, 0)
    c.connect(i_wr2, 0, i_thr_db, 0)
    c.connect(i_thr_db, 0, i_s_db, 0)

    # bar counter (GUI display)
    i_r_db_cnt = c.obj(clk_x, beat_y + 335, "r downbeat")
    i_cnt_bar  = c.obj(clk_x, beat_y + 365, "counter 1 9999")
    i_s_barnum = c.obj(clk_x, beat_y + 395, "s barNum")
    c.connect(i_r_db_cnt, 0, i_cnt_bar, 0)
    c.connect(i_cnt_bar, 0, i_s_barnum, 0)

    # GUI
    i_r_bn_gui = c.obj(clk_x + 200, beat_y + 25, "r barNum")
    i_nbx_bar  = c.nbx(clk_x + 280, beat_y + 25, "bar", 1, 9999, 1)
    c.connect(i_r_bn_gui, 0, i_nbx_bar, 0)

    # r downbeat for horizontal stem control
    i_r_downbeat = c.obj(860, beat_y + 305, "r downbeat")

    # ── PAD (always on) ───
    gain_x = 860
    c.text(gain_x, osc_y, "--- PAD (always on) ---")
    i_lb_pad = c.obj(gain_x, osc_y + 25, "loadbang")
    i_m_pad  = c.msg(gain_x, osc_y + 50, "0.5 50")
    i_ln_pad = c.obj(gain_x, osc_y + 80, "line~")
    i_sg_pad = c.obj(gain_x, osc_y + 110, "s~ gain_pad")
    c.connect(i_lb_pad, 0, i_m_pad, 0)
    c.connect(i_m_pad, 0, i_ln_pad, 0)
    c.connect(i_ln_pad, 0, i_sg_pad, 0)

    # ── QUANTIZED STEMS (state machine) ───
    qx = 860
    qy = osc_y + 150
    c.text(qx, qy,
           "--- STEMS: arm -> next beat-1 -> play | quantized exit ---")
    qy += 25
    for name in ["kick", "perc", "bass", "lead"]:
        rpm_on, rpm_off, peak = STEM_RPM[name]
        qy = add_state_machine_stem(
            c, qx, qy, name, rpm_on, rpm_off, peak,
            i_r_rpm, i_r_downbeat,
        )

    # ── STEMS playback ───
    stem_base_y = 420
    c.text(20, stem_base_y, "--- STEMS (phase-locked stereo) ---")
    stem_base_y += 25

    mix_x = 20
    fx_ctrl_y = stem_base_y + 480
    c.text(mix_x, fx_ctrl_y, "--- MASTER FX CONTROL (RPM -> Drive 0..1) ---")
    i_r_rpm_fx = c.obj(mix_x, fx_ctrl_y + 30, "r rpm")
    i_clip_rpm = c.obj(mix_x, fx_ctrl_y + 60, "clip 800 6000")
    i_sub_rpm = c.obj(mix_x, fx_ctrl_y + 90, "- 800")
    i_div_rpm = c.obj(mix_x, fx_ctrl_y + 120, "/ 5200")
    i_s_drive = c.obj(mix_x, fx_ctrl_y + 150, "s drive_amt")
    c.connect(i_r_rpm_fx, 0, i_clip_rpm, 0)
    c.connect(i_clip_rpm, 0, i_sub_rpm, 0)
    c.connect(i_sub_rpm, 0, i_div_rpm, 0)
    c.connect(i_div_rpm, 0, i_s_drive, 0)

    i_r_drive1 = c.obj(mix_x + 200, fx_ctrl_y + 30, "r drive_amt")
    i_t_f_f = c.obj(mix_x + 200, fx_ctrl_y + 60, "t f f")
    i_mul_sq = c.obj(mix_x + 200, fx_ctrl_y + 90, "*")
    i_mul_range = c.obj(mix_x + 200, fx_ctrl_y + 120, "* 14750")
    i_add_base = c.obj(mix_x + 200, fx_ctrl_y + 150, "+ 250")
    i_pack_lp = c.obj(mix_x + 200, fx_ctrl_y + 180, "pack f 500")
    i_line_lp = c.obj(mix_x + 200, fx_ctrl_y + 210, "line")
    i_s_lp = c.obj(mix_x + 200, fx_ctrl_y + 240, "s lp_freq")
    c.connect(i_r_drive1, 0, i_t_f_f, 0)
    c.connect(i_t_f_f, 0, i_mul_sq, 0)
    c.connect(i_t_f_f, 1, i_mul_sq, 1)
    c.connect(i_mul_sq, 0, i_mul_range, 0)
    c.connect(i_mul_range, 0, i_add_base, 0)
    c.connect(i_add_base, 0, i_pack_lp, 0)
    c.connect(i_pack_lp, 0, i_line_lp, 0)
    c.connect(i_line_lp, 0, i_s_lp, 0)

    i_r_drive2 = c.obj(mix_x + 400, fx_ctrl_y + 30, "r drive_amt")
    i_sub_inv = c.obj(mix_x + 400, fx_ctrl_y + 60, "expr (1 - $f1) * 0.5")
    i_pack_rev = c.obj(mix_x + 400, fx_ctrl_y + 90, "pack f 500")
    i_line_rev = c.obj(mix_x + 400, fx_ctrl_y + 120, "line~")
    i_s_rev = c.obj(mix_x + 400, fx_ctrl_y + 150, "s~ rev_wet")
    c.connect(i_r_drive2, 0, i_sub_inv, 0)
    c.connect(i_sub_inv, 0, i_pack_rev, 0)
    c.connect(i_pack_rev, 0, i_line_rev, 0)
    c.connect(i_line_rev, 0, i_s_rev, 0)

    for si, name in enumerate(STEMS):
        sx = 20 + si * 160
        sy = stem_base_y
        i_lb = c.obj(sx, sy, "loadbang")
        i_rd = c.msg(sx, sy + 25,
                     f"read -resize samples/{name}.wav {name}L {name}R")
        i_sf = c.obj(sx, sy + 50, "soundfiler")
        c.connect(i_lb, 0, i_rd, 0)
        c.connect(i_rd, 0, i_sf, 0)
        c.obj(sx + 700, sy, f"table {name}L {LOOP_SAMPLES}")
        c.obj(sx + 700, sy + 25, f"table {name}R {LOOP_SAMPLES}")

        i_rp_l  = c.obj(sx, sy + 100, "receive~ barPhase")
        i_mul_l = c.obj(sx, sy + 130, f"*~ {READ_INDEX}")
        i_tr_l  = c.obj(sx, sy + 160, f"tabread4~ {name}L")
        i_rg_l  = c.obj(sx, sy + 190, f"receive~ gain_{name}")
        i_g_l   = c.obj(sx, sy + 220, "*~")
        i_th_l  = c.obj(sx, sy + 250, "throw~ mixL")
        c.connect(i_rp_l, 0, i_mul_l, 0)
        c.connect(i_mul_l, 0, i_tr_l, 0)
        c.connect(i_tr_l, 0, i_g_l, 0)
        c.connect(i_rg_l, 0, i_g_l, 1)
        c.connect(i_g_l, 0, i_th_l, 0)

        i_rp_r  = c.obj(sx, sy + 290, "receive~ barPhase")
        i_mul_r = c.obj(sx, sy + 320, f"*~ {READ_INDEX}")
        i_tr_r  = c.obj(sx, sy + 350, f"tabread4~ {name}R")
        i_rg_r  = c.obj(sx, sy + 380, f"receive~ gain_{name}")
        i_g_r   = c.obj(sx, sy + 410, "*~")
        i_th_r  = c.obj(sx, sy + 440, "throw~ mixR")
        c.connect(i_rp_r, 0, i_mul_r, 0)
        c.connect(i_mul_r, 0, i_tr_r, 0)
        c.connect(i_tr_r, 0, i_g_r, 0)
        c.connect(i_rg_r, 0, i_g_r, 1)
        c.connect(i_g_r, 0, i_th_r, 0)

    mix_y = fx_ctrl_y + 300
    c.text(mix_x, mix_y, "--- MASTER MIX & FX ---")
    i_catch_l = c.obj(mix_x, mix_y + 30, "catch~ mixL")
    i_catch_r = c.obj(mix_x + 80, mix_y + 30, "catch~ mixR")
    
    i_r_lp = c.obj(mix_x + 160, mix_y + 30, "r lp_freq")
    i_lop_l1 = c.obj(mix_x, mix_y + 70, "lop~")
    i_lop_l2 = c.obj(mix_x, mix_y + 110, "lop~")
    i_lop_r1 = c.obj(mix_x + 80, mix_y + 70, "lop~")
    i_lop_r2 = c.obj(mix_x + 80, mix_y + 110, "lop~")
    c.connect(i_catch_l, 0, i_lop_l1, 0)
    c.connect(i_r_lp, 0, i_lop_l1, 1)
    c.connect(i_lop_l1, 0, i_lop_l2, 0)
    c.connect(i_r_lp, 0, i_lop_l2, 1)
    c.connect(i_catch_r, 0, i_lop_r1, 0)
    c.connect(i_r_lp, 0, i_lop_r1, 1)
    c.connect(i_lop_r1, 0, i_lop_r2, 0)
    c.connect(i_r_lp, 0, i_lop_r2, 1)
    
    i_dry_l = c.obj(mix_x, mix_y + 150, "*~ 0.8")
    i_dry_r = c.obj(mix_x + 80, mix_y + 150, "*~ 0.8")
    c.connect(i_lop_l2, 0, i_dry_l, 0)
    c.connect(i_lop_r2, 0, i_dry_r, 0)
    
    i_freeverb = c.obj(mix_x + 200, mix_y + 150, "freeverb~")
    c.connect(i_lop_l2, 0, i_freeverb, 0)
    c.connect(i_lop_r2, 0, i_freeverb, 1)
    
    i_r_rev = c.obj(mix_x + 300, mix_y + 150, "r~ rev_wet")
    i_wet_l = c.obj(mix_x + 200, mix_y + 190, "*~")
    i_wet_r = c.obj(mix_x + 240, mix_y + 190, "*~")
    c.connect(i_freeverb, 0, i_wet_l, 0)
    c.connect(i_r_rev, 0, i_wet_l, 1)
    c.connect(i_freeverb, 1, i_wet_r, 0)
    c.connect(i_r_rev, 0, i_wet_r, 1)
    
    i_dac = c.obj(mix_x, mix_y + 240, "dac~")
    c.connect(i_dry_l, 0, i_dac, 0)
    c.connect(i_dry_r, 0, i_dac, 1)
    c.connect(i_wet_l, 0, i_dac, 0)
    c.connect(i_wet_r, 0, i_dac, 1)

    return c.build()


def main():
    root = Path(__file__).resolve().parent.parent
    pd = generate()
    path = root / "engine.pd"
    path.write_text(pd, encoding="ascii", newline="\n")
    print(f"wrote {path} ({len(pd.splitlines())} lines)")


if __name__ == "__main__":
    main()
