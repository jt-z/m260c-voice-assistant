# -*- coding: utf-8 -*-
"""
sound_radar_ui.py —— M260C 声源定位「科技风 HUD」实时界面(Tkinter, 零第三方依赖)

视觉: 深空底色 + 青蓝辉光 + 雷达扫描线 + 波束能量扇形 + 指针拖尾 + 角度历史热迹
内容: 1) 环形角度雷达(波束高亮/指针/拖尾/历史热迹)  2) 状态提示条(按状态变色)
      3) 实时波形 + 频谱柱  4) 可滚动/可复制的日志面板  5) 收帧诊断计数

线程模型: worker 线程只调用 state()/log()/angle()/level()/samples()/partial()/stats(),
          内部入 Queue; Tk 主线程每 40ms 取队列刷新(避免跨线程操作 Tk 崩溃)。

独立预览(模拟数据, 不连硬件):  python sound_radar_ui.py 30
"""
import math
import queue
import time

import tkinter as tk
from tkinter import font as tkfont

try:                                   # FFT 频谱(可选, 没有 numpy 时退化为幅度近似)
    import numpy as _np
except Exception:
    _np = None

# 角度->屏幕映射(如与实际方向不符, 只改这两个常量)
ANGLE_ZERO_AT_TOP = True      # 0° 朝正上方
ANGLE_CLOCKWISE = True        # 角度顺时针增大

RADAR_SIZE = 470              # 雷达画布边长(px)
WAVE_W, WAVE_H = 320, 118     # 波形/频谱画布尺寸
MAX_LOG_LINES = 500
HISTORY_LEN = 24              # 角度历史拖尾条数

# ---------------------------------------------------------------- 配色(科技风)
BG = "#070b12"
PANEL = "#0b1220"
GRID = "#132133"
GRID_SOFT = "#0f1a2a"
CYAN = "#00e5ff"
BLUE = "#3fa9ff"
RED = "#ff4d6d"
AMBER = "#ffd479"
GREEN = "#41d18b"
TEXT = "#c9d8ea"
DIM = "#5d7896"
WHITE = "#eaf6ff"


def _hex2rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(c1, c2, t):
    """颜色线性插值(用于辉光/拖尾的渐变近似)"""
    t = max(0.0, min(1.0, t))
    a, b = _hex2rgb(c1), _hex2rgb(c2)
    return "#%02x%02x%02x" % tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


class SoundRadarUI:
    def __init__(self, title="M260C 声源定位 HUD"):
        self.root = tk.Tk()
        self.root.title(title)
        self.root.configure(bg=BG)
        self._q = queue.Queue()
        self._on_close = None
        self._font = self._pick_cjk_font()
        self._mono = self._pick_mono_font()

        # 状态
        self._angle_now = 0.0
        self._angle_target = 0.0
        self._beam = None
        self._score = None
        self._level = 0.0
        self._samples = []              # 滚动音频缓冲(峰值抽样)
        self._status = "初始化中…"
        self._status_kind = "info"
        self._stats = {}
        self._sweep = 0.0               # 空闲时的扫描线角度
        self._active = False            # 是否已唤醒(有目标角度)
        self._history = []              # [(angle, ts)] 角度历史
        self._last_log_autoscroll = True

        self._build_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self._handle_close)
        self.root.after(40, self._tick)

    # ------------------------------------------------------------ 字体
    def _pick_cjk_font(self):
        try:
            fams = set(tkfont.families())
        except Exception:
            fams = set()
        for n in ("Noto Sans CJK SC", "Noto Sans SC", "WenQuanYi Micro Hei",
                  "Source Han Sans SC", "Microsoft YaHei", "DejaVu Sans"):
            if n in fams:
                return n
        return "TkDefaultFont"

    def _pick_mono_font(self):
        try:
            fams = set(tkfont.families())
        except Exception:
            fams = set()
        for n in ("DejaVu Sans Mono", "Noto Sans Mono CJK SC", "Liberation Mono"):
            if n in fams:
                return n
        return self._font

    # ------------------------------------------------------------ 界面
    def _build_widgets(self):
        tk_ = tk
        # ===== 顶部: 标题 + 状态条 + 时钟 =====
        head = tk_.Frame(self.root, bg=BG)
        head.pack(fill="x", padx=12, pady=(10, 2))
        tk_.Label(head, text="M260C  SOUND  LOCALIZATION", font=(self._mono, 13, "bold"),
                  fg=CYAN, bg=BG).pack(side="left")
        self.lbl_clock = tk_.Label(head, text="", font=(self._mono, 11), fg=DIM, bg=BG)
        self.lbl_clock.pack(side="right")

        bar = tk_.Frame(self.root, bg=PANEL, height=46)
        bar.pack(fill="x", padx=12, pady=(2, 6))
        self.acc = tk_.Frame(bar, bg=DIM, width=5)          # 状态色带
        self.acc.pack(side="left", fill="y")
        self.lbl_status = tk_.Label(bar, text="初始化中…", anchor="w",
                                    font=(self._font, 16, "bold"), fg=TEXT, bg=PANEL)
        self.lbl_status.pack(side="left", padx=10)
        self.lbl_stats = tk_.Label(bar, text="", anchor="e", font=(self._mono, 10),
                                   fg=DIM, bg=PANEL)
        self.lbl_stats.pack(side="right", padx=10)

        # ===== 中部: 左雷达 / 右信息 =====
        mid = tk_.Frame(self.root, bg=BG)
        mid.pack(fill="both", expand=True, padx=12)
        self.canvas = tk_.Canvas(mid, width=RADAR_SIZE, height=RADAR_SIZE,
                                 bg=BG, highlightthickness=1,
                                 highlightbackground=GRID)
        self.canvas.pack(side="left")

        right = tk_.Frame(mid, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        def card(title):
            f = tk_.Frame(right, bg=PANEL)
            f.pack(fill="x", pady=4)
            tk_.Label(f, text=title, font=(self._mono, 10), fg=DIM, bg=PANEL).pack(
                anchor="w", padx=8, pady=(4, 0))
            return f

        c1 = card("实时识别文本")
        self.lbl_partial = tk_.Label(c1, text="—", anchor="nw", justify="left",
                                     wraplength=300, font=(self._font, 15, "bold"),
                                     fg=AMBER, bg=PANEL)
        self.lbl_partial.pack(anchor="w", padx=8, pady=(0, 8))

        c2 = card("波形 / 频谱 (16kHz)")
        self.canvas_wave = tk_.Canvas(c2, width=WAVE_W, height=WAVE_H, bg=BG,
                                      highlightthickness=0)
        self.canvas_wave.pack(padx=8, pady=(2, 8))

        c3 = card("提示")
        self.lbl_hint = tk_.Label(c3, text="说「小宽小宽」唤醒 → 说「打开图片」/「关闭图片」",
                                  anchor="nw", justify="left", wraplength=300,
                                  font=(self._font, 12), fg=BLUE, bg=PANEL)
        self.lbl_hint.pack(anchor="w", padx=8, pady=(0, 8))

        # ===== 底部: 日志(可滚动/可复制) =====
        bot = tk_.Frame(self.root, bg=BG)
        bot.pack(fill="both", padx=12, pady=(4, 12))
        tk_.Label(bot, text="LOG", font=(self._mono, 10), fg=DIM, bg=BG).pack(anchor="w")
        wrap = tk_.Frame(bot, bg=GRID)
        wrap.pack(fill="both", expand=True)
        self.txt_log = tk_.Text(wrap, height=9, bg=BG, fg=TEXT,
                                font=(self._mono, 11), insertbackground=TEXT,
                                highlightthickness=0, wrap="none", undo=False)
        self.txt_log.pack(side="left", fill="both", expand=True)
        sb = tk_.Scrollbar(wrap, command=self.txt_log.yview)
        sb.pack(side="right", fill="y")
        self.txt_log.config(yscrollcommand=sb.set)
        # 日志配色 + 只允许选择/复制(Ctrl+C/Ctrl+A), 禁止编辑
        self.txt_log.tag_config("time", foreground=DIM)
        self.txt_log.tag_config("wake", foreground=CYAN)
        self.txt_log.tag_config("res", foreground=GREEN)
        self.txt_log.tag_config("warn", foreground=AMBER)
        self.txt_log.tag_config("diag", foreground=DIM)
        self.txt_log.bind("<Key>", self._log_key)
        self.txt_log.bind("<MouseWheel>", self._log_scroll)
        self.txt_log.bind("<Button-4>", self._log_scroll)
        self.txt_log.bind("<Button-5>", self._log_scroll)

    def _log_key(self, ev):
        """只放行复制/全选/上下滚动, 其它按键不生效(日志只读)"""
        if ev.state & 0x4 and ev.keysym.lower() in ("c", "a"):   # Ctrl+C / Ctrl+A
            return None
        if ev.keysym in ("Up", "Down", "Left", "Right", "Prior", "Next",
                         "Home", "End"):
            return None
        return "break"

    def _log_scroll(self, ev):
        delta = -3 if getattr(ev, "delta", 0) > 0 or ev.num == 4 else 3
        self.txt_log.yview_scroll(delta, "units")
        # 用户上滚后不强制回到末尾
        self._last_log_autoscroll = self.txt_log.yview()[1] > 0.999
        return "break"

    # ------------------------------------------------------------ 线程安全 API
    def state(self, text, kind="info"):
        self._q.put(("state", (text, kind)))

    def log(self, line):
        self._q.put(("log", line))

    def angle(self, deg, beam=None, score=None):
        self._q.put(("angle", (deg, beam, score)))

    def level(self, rms):
        self._q.put(("level", rms))

    def samples(self, chunk):
        """推送原始 S16LE 音频块, 用于波形/频谱"""
        self._q.put(("samples", chunk))

    def partial(self, text, remain=None):
        self._q.put(("partial", (text, remain)))

    def stats(self, d):
        self._q.put(("stats", d))

    def on_close(self, cb):
        self._on_close = cb

    def run(self):
        self.root.mainloop()

    # ------------------------------------------------------------ 刷新
    def _handle_close(self):
        if self._on_close:
            try:
                self._on_close()
            except Exception:
                pass
        self.root.destroy()

    def _tick(self):
        try:
            self._drain()
            self._animate()
            self._draw_radar()
            self._draw_wave()
            self.lbl_clock.config(text=time.strftime("%H:%M:%S"))
        finally:
            self.root.after(40, self._tick)

    def _drain(self):
        while True:
            try:
                kind, p = self._q.get_nowait()
            except queue.Empty:
                return
            if kind == "state":
                text, st = p
                self._status, self._status_kind = text, st
                color = self._kind_color(st)
                self.lbl_status.config(text=text, fg=color)
                self.acc.config(bg=color)
                self._active = st in ("awake", "recognize")
            elif kind == "log":
                self._append_log(p)
            elif kind == "angle":
                deg, beam, score = p
                self._angle_target = float(deg)
                self._beam, self._score = beam, score
                if not self._history:
                    self._angle_now = float(deg)
                self._history.append((float(deg), time.time()))
                del self._history[:-HISTORY_LEN]
            elif kind == "level":
                self._level = float(p)
            elif kind == "samples":
                self._push_samples(p)
            elif kind == "partial":
                text, remain = p
                if text:
                    self.lbl_partial.config(
                        text=(f"[剩余{remain}s] " if remain is not None else "") + text)
                else:
                    self.lbl_partial.config(
                        text=(f"[剩余{remain}s] 聆听中…" if remain is not None else "—"))
            elif kind == "stats":
                self._stats = p
                self.lbl_stats.config(text=" ".join(
                    f"{k}:{v}" for k, v in p.items()))

    @staticmethod
    def _kind_color(kind):
        return {"info": DIM, "listen": "#8fa6c0", "awake": CYAN,
                "recognize": AMBER, "result": GREEN, "done": GREEN,
                "warn": RED}.get(kind, DIM)

    def _append_log(self, line):
        tag = "time"
        if "[状态] 已唤醒" in line or "[唤醒]" in line:
            tag = "wake"
        elif "[识别] 结果片段" in line or "识别结果" in line or "[命令]" in line:
            tag = "res"
        elif "警告" in line or "未识别" in line or "忽略" in line:
            tag = "warn"
        elif "[诊断]" in line:
            tag = "diag"
        self.txt_log.insert("end", line + "\n", tag)
        if int(self.txt_log.index("end-1c").split(".")[0]) > MAX_LOG_LINES:
            self.txt_log.delete("1.0", "2.0")
        if self._last_log_autoscroll:
            self.txt_log.see("end")

    def _push_samples(self, chunk):
        from array import array
        a = array("h")
        a.frombytes(chunk[:len(chunk) // 2 * 2])
        if not a:
            return
        step = max(1, len(a) // 400)          # 抽样, 控制内存/CPU
        self._samples.extend(a[i] for i in range(0, len(a), step))
        keep = 4000
        if len(self._samples) > keep:
            del self._samples[:-keep]

    def _animate(self):
        # 指针平滑转最短路径
        diff = (self._angle_target - self._angle_now + 540) % 360 - 180
        self._angle_now = (self._angle_now + diff * 0.3) % 360
        # 空闲时雷达扫描线转动
        if not self._active:
            self._sweep = (self._sweep + 2.2) % 360

    # ------------------------------------------------------------ 雷达绘制
    def _polar(self, deg, r):
        a = math.radians(deg)
        if not ANGLE_ZERO_AT_TOP:
            a += math.pi / 2
        if not ANGLE_CLOCKWISE:
            a = -a
        cx = cy = RADAR_SIZE / 2
        return cx + r * math.sin(a), cy - r * math.cos(a)

    def _draw_radar(self):
        c = self.canvas
        c.delete("all")
        cx = cy = RADAR_SIZE / 2
        r = cx - 34

        # 背景网格: 同心圆(双层近似辉光) + 十字线
        for frac in (1.0, 0.75, 0.5, 0.25):
            rr = r * frac
            c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline=GRID, width=2)
            c.create_oval(cx - rr + 1, cy - rr + 1, cx + rr - 1, cy + rr - 1,
                          outline=GRID_SOFT, width=1)
        c.create_line(cx - r, cy, cx + r, cy, fill=GRID)
        c.create_line(cx, cy - r, cx, cy + r, fill=GRID)

        # 空闲时扫描线(带拖尾渐变)
        if not self._active:
            for i in range(26):
                a = self._sweep - i * 2.2
                col = _mix(BG, CYAN, (26 - i) / 26.0 * 0.85)
                x, y = self._polar(a, r)
                c.create_line(cx, cy, x, y, fill=col)

        # 刻度: 每 10° 短线, 每 30° 长线+标注
        for deg in range(0, 360, 10):
            long_tick = (deg % 30 == 0)
            x1, y1 = self._polar(deg, r)
            x2, y2 = self._polar(deg, r + (10 if long_tick else 5))
            c.create_line(x1, y1, x2, y2, fill=CYAN if long_tick else GRID,
                          width=2 if long_tick else 1)
            if long_tick:
                tx, ty = self._polar(deg, r + 21)
                c.create_text(tx, ty, text=str(deg), fill=DIM,
                              font=(self._mono, 9))

        # 波束能量扇形(6 波束, 每 60°): 非当前波束微弱, 当前波束高亮
        for i in range(6):
            active = (self._beam is not None and int(self._beam) == i)
            start = 30 - i * 60
            c.create_arc(cx - r, cy - r, cx + r, cy + r, start=start, extent=60,
                         style="pieslice",
                         fill=_mix(PANEL, CYAN, 0.55) if active else PANEL,
                         stipple="gray50" if active else "gray12",
                         outline=CYAN if active else GRID,
                         width=2 if active else 1)

        # 角度历史热迹(越新越亮越大)
        now = time.time()
        for deg, ts in self._history:
            age = min(1.0, (now - ts) / 30.0)
            col = _mix(BG, BLUE, 1.0 - age)
            x1, y1 = self._polar(deg, r * 0.62)
            x2, y2 = self._polar(deg, r * 0.98)
            c.create_line(x1, y1, x2, y2, fill=col, width=2)
            x3, y3 = self._polar(deg, r * 0.98)
            rr = 3 + 3 * (1 - age)
            c.create_oval(x3 - rr, y3 - rr, x3 + rr, y3 + rr, fill=col, outline="")

        # 目标指针(三层近似辉光 + 箭头 + 轴心)
        ax, ay = self._polar(self._angle_now, r * 0.95)
        c.create_line(cx, cy, ax, ay, fill=_mix(BG, RED, 0.22), width=11)
        c.create_line(cx, cy, ax, ay, fill=_mix(BG, RED, 0.5), width=6)
        c.create_line(cx, cy, ax, ay, fill=RED, width=2)
        # 箭头
        dx, dy = (ax - cx), (ay - cy)
        ln = math.hypot(dx, dy) or 1
        ux, uy = dx / ln, dy / ln
        px, py = -uy, ux
        c.create_polygon(ax + ux * 8, ay + uy * 8,
                         ax - ux * 12 + px * 7, ay - uy * 12 + py * 7,
                         ax - ux * 12 - px * 7, ay - uy * 12 - py * 7,
                         fill=RED, outline="")
        for rr, col in ((10, _mix(BG, RED, 0.35)), (6, RED), (2, WHITE)):
            c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, fill=col, outline="")

        # 中心读数
        active_col = CYAN if self._active else DIM
        c.create_text(cx, cy + 52, text=f"{self._angle_now:.0f}°",
                      fill=active_col, font=(self._mono, 30, "bold"))
        sub = []
        if self._beam is not None:
            sub.append(f"BEAM {self._beam}")
        if self._score is not None:
            sub.append(f"SCORE {self._score}")
        c.create_text(cx, cy + 84, text="  ".join(sub) or "SCANNING",
                      fill=DIM, font=(self._mono, 11))
        # 电平条(小, 底部)
        lw = int(r * 1.4)
        x0 = cx - lw / 2
        c.create_rectangle(x0, RADAR_SIZE - 22, x0 + lw, RADAR_SIZE - 14,
                           outline=GRID, fill=BG)
        c.create_rectangle(x0 + 1, RADAR_SIZE - 21,
                           x0 + 1 + (lw - 2) * min(1.0, self._level), RADAR_SIZE - 15,
                           outline="", fill=_mix(GREEN, RED, min(1.0, self._level)))

    # ------------------------------------------------------------ 波形/频谱
    def _draw_wave(self):
        c = self.canvas_wave
        c.delete("all")
        w, h = WAVE_W, WAVE_H
        hw = int(h * 0.55)                     # 上半: 波形
        c.create_line(0, hw, w, hw, fill=GRID)
        c.create_line(0, hw + 4, w, hw + 4, fill=GRID_SOFT)
        c.create_text(4, 8, text="WAVE", anchor="w", fill=DIM, font=(self._mono, 8))
        c.create_text(4, hw + 14, text="SPECTRUM", anchor="w", fill=DIM,
                      font=(self._mono, 8))

        if self._samples:
            n = len(self._samples)
            step = max(1, n // w)
            pts = []
            for x in range(w):
                i = min(n - 1, x * step)
                v = self._samples[i] / 32768.0
                pts.extend((x, hw / 2 - v * (hw / 2 - 8)))
            if len(pts) >= 4:
                c.create_line(*pts, fill=_mix(BG, CYAN, 0.35), width=3)
                c.create_line(*pts, fill=CYAN, width=1)

        # 频谱: 有 numpy 用 FFT, 否则用分块幅度近似
        bars = 32
        mags = self._spectrum(bars)
        bw = w / bars
        base = h - 6
        for i, m in enumerate(mags):
            bh = int((h - hw - 16) * m)
            x = i * bw
            col = _mix(BLUE, RED, m)
            c.create_rectangle(x + 1, base - bh, x + bw - 2, base,
                               fill=_mix(BG, col, 0.85), outline=col)

    def _spectrum(self, bars):
        """返回 bars 个 0~1 的幅度(优先 numpy FFT)"""
        if len(self._samples) < 512:
            return [0.0] * bars
        if _np is not None:
            x = _np.asarray(self._samples[-4096:], dtype=_np.float32) / 32768.0
            spec = _np.abs(_np.fft.rfft(x * _np.hanning(len(x))))
            if len(spec) >= bars:
                idx = _np.linspace(0, min(len(spec) - 1, 900), bars + 1).astype(int)
                out = []
                for i in range(bars):
                    seg = spec[idx[i]:max(idx[i] + 1, idx[i + 1])]
                    out.append(float(seg.max()))
                mx = max(out) or 1.0
                return [min(1.0, v / mx) for v in out]
        # 退化: 按块求平均幅度
        n = len(self._samples)
        blk = max(1, n // bars)
        out = []
        for i in range(bars):
            seg = self._samples[i * blk:(i + 1) * blk] or [0]
            out.append(min(1.0, (sum(abs(v) for v in seg) / len(seg)) / 9000.0))
        return out


def demo_preview(seconds: int = 30):
    """本地预览(模拟角度/波形/状态), 不连硬件"""
    import random
    ui = SoundRadarUI("声源定位 HUD - 预览(模拟数据)")
    end = time.time() + seconds
    deg = 0.0
    phase = 0

    def fake():
        nonlocal deg, phase
        if time.time() > end:
            ui.root.destroy()
            return
        deg = (deg + random.uniform(10, 120)) % 360
        ui.angle(deg, beam=int(deg // 60) % 6, score=random.randint(1000, 1600))
        # 造一段正弦"音频"给波形/频谱
        import struct
        import math as _m
        phase += 1
        buf = b"".join(struct.pack("<h", int(9000 * _m.sin(
            2 * _m.pi * (4 + phase % 5) * i / 160) * random.uniform(0.6, 1.0)))
            for i in range(1000))
        ui.samples(buf)
        ui.level(random.uniform(0.1, 0.95))
        ui.partial(random.choice(["", "打开", "打开图片", "关闭图片"]), remain=3)
        kind = random.choice(["listen", "awake", "recognize", "result"])
        ui.state({"listen": "监听中：请先说唤醒词「小宽小宽」",
                  "awake": "已唤醒：声源角度 %.0f°" % deg,
                  "recognize": "识别中：开始录音 3s, 请说命令词…",
                  "result": "识别结果：打开图片"}[kind], kind)
        ui.log("[%s] [状态] 模拟事件 角度 %.0f°" % (time.strftime("%H:%M:%S"), deg))
        ui.stats({"握手帧": random.randint(200, 300), "设备事件": 1,
                  "唤醒": 1, "重复忽略": 0, "忙碌忽略": 0})
        ui.root.after(350, fake)

    ui.root.after(200, fake)
    ui.run()


if __name__ == "__main__":
    import sys
    demo_preview(int(sys.argv[1]) if len(sys.argv) > 1 else 30)
