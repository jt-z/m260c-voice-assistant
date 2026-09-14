# -*- coding: utf-8 -*-
"""
sound_radar_ui.py —— M260C 声源定位实时界面(Tkinter, 零第三方依赖)

显示内容:
  1) 环形角度雷达: 360° 刻度 + 6 个波束扇区(高亮当前 beam) + 指针(平滑转动)
                   中心显示 角度 / beam / score
  2) 状态提示条  : 监听中 / 已唤醒 / 识别中(剩余秒) / 识别结果 / 已执行
  3) 日志面板    : 与终端一致的时间戳日志滚动
  4) 录音电平条  : 实时声音幅度(录音窗口内)

线程模型: worker 线程(串口/录音/识别) 只调用 state()/log()/angle()/level()/partial(),
          内部塞进 Queue; Tk 主线程每 50ms 取队列刷新界面(避免跨线程操作 Tk 崩溃)。
"""
import queue
import time

import tkinter as tk
from tkinter import font as tkfont

# 角度->屏幕映射(如与实际方向不符, 只改这两个常量)
ANGLE_ZERO_AT_TOP = True      # 0° 朝正上方
ANGLE_CLOCKWISE = True        # 角度顺时针增大(环形阵列常见约定)

RADAR_SIZE = 430              # 雷达画布尺寸(px)
MAX_LOG_LINES = 200


class SoundRadarUI:
    def __init__(self, title="M260C 声源定位 - 语音交互"):
        self.root = tk.Tk()
        self.root.title(title)
        self.root.configure(bg="#11151c")
        self._q = queue.Queue()
        self._on_close = None
        self._angle_now = 0.0          # 指针当前角度(动画用)
        self._angle_target = 0.0
        self._beam = None
        self._score = None
        self._level = 0.0
        self._status = "初始化中…"
        self._status_kind = "info"

        self._font = self._pick_cjk_font()
        self._build_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self._handle_close)
        self.root.after(50, self._tick)

    # ------------------------------------------------------------ 界面构建
    def _pick_cjk_font(self):
        """挑一个支持中文的字体, 找不到就用默认"""
        try:
            families = set(tkfont.families())
        except Exception:
            families = set()
        for name in ("Noto Sans CJK SC", "Noto Sans SC", "WenQuanYi Micro Hei",
                     "Microsoft YaHei", "Source Han Sans SC", "DejaVu Sans"):
            if name in families:
                return name
        return "TkDefaultFont"

    def _build_widgets(self):
        tk_ = tk
        # 顶部状态条
        top = tk_.Frame(self.root, bg="#11151c")
        top.pack(fill="x", padx=10, pady=(10, 4))
        self.lbl_status = tk_.Label(top, text="● 初始化中…", anchor="w",
                                    font=(self._font, 16, "bold"),
                                    fg="#dfe7f5", bg="#11151c")
        self.lbl_status.pack(side="left")
        self.lbl_time = tk_.Label(top, text="", font=(self._font, 11),
                                  fg="#7f8ea3", bg="#11151c")
        self.lbl_time.pack(side="right")

        # 雷达
        mid = tk_.Frame(self.root, bg="#11151c")
        mid.pack(fill="both", expand=True, padx=10)
        self.canvas = tk_.Canvas(mid, width=RADAR_SIZE, height=RADAR_SIZE,
                                 bg="#0c1016", highlightthickness=1,
                                 highlightbackground="#243044")
        self.canvas.pack(side="left")

        # 右侧: 电平条 + 唤醒历史/实时文本
        right = tk_.Frame(mid, bg="#11151c")
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))
        tk_.Label(right, text="录音电平", font=(self._font, 11),
                  fg="#7f8ea3", bg="#11151c").pack(anchor="w")
        self.canvas_level = tk_.Canvas(right, width=250, height=42, bg="#0c1016",
                                       highlightthickness=1,
                                       highlightbackground="#243044")
        self.canvas_level.pack(anchor="w", pady=(2, 10))
        tk_.Label(right, text="实时识别文本", font=(self._font, 11),
                  fg="#7f8ea3", bg="#11151c").pack(anchor="w")
        self.lbl_partial = tk_.Label(right, text="—", anchor="nw", justify="left",
                                     wraplength=260, font=(self._font, 14),
                                     fg="#ffd479", bg="#11151c")
        self.lbl_partial.pack(anchor="w", pady=(2, 10))
        tk_.Label(right, text="提示", font=(self._font, 11),
                  fg="#7f8ea3", bg="#11151c").pack(anchor="w")
        self.lbl_hint = tk_.Label(right, text="说「小宽小宽」唤醒, 再说「打开图片」",
                                  anchor="nw", justify="left", wraplength=260,
                                  font=(self._font, 12), fg="#8fd0ff", bg="#11151c")
        self.lbl_hint.pack(anchor="w")

        # 日志
        bottom = tk_.Frame(self.root, bg="#11151c")
        bottom.pack(fill="both", padx=10, pady=(6, 10))
        self.txt_log = tk_.Text(bottom, height=9, bg="#0c1016", fg="#c9d4e3",
                                font=(self._font, 11), insertbackground="#c9d4e3",
                                highlightthickness=1, highlightbackground="#243044",
                                state="disabled", wrap="none")
        self.txt_log.pack(fill="both", expand=True)

    # ------------------------------------------------------------ 线程安全 API
    def state(self, text: str, kind: str = "info"):
        self._q.put(("state", (text, kind)))

    def log(self, line: str):
        self._q.put(("log", line))

    def angle(self, deg: float, beam=None, score=None):
        self._q.put(("angle", (deg, beam, score)))

    def level(self, rms: float):
        self._q.put(("level", rms))

    def partial(self, text: str, remain: int = None):
        self._q.put(("partial", (text, remain)))

    def on_close(self, cb):
        """窗口关闭时的回调(用于通知 worker 线程退出)"""
        self._on_close = cb

    def run(self):
        self.root.mainloop()

    # ------------------------------------------------------------ 内部刷新
    def _handle_close(self):
        if self._on_close:
            try:
                self._on_close()
            except Exception:
                pass
        self.root.destroy()

    def _tick(self):
        try:
            self._drain_queue()
            self._animate()
            self._draw_radar()
            self._draw_level()
            self.lbl_time.config(text=time.strftime("%H:%M:%S"))
        finally:
            self.root.after(50, self._tick)

    def _drain_queue(self):
        while True:
            try:
                kind, payload = self._q.get_nowait()
            except queue.Empty:
                return
            if kind == "state":
                text, st = payload
                self._status, self._status_kind = text, st
                self.lbl_status.config(text="● " + text, fg=self._kind_color(st))
            elif kind == "log":
                self.txt_log.config(state="normal")
                self.txt_log.insert("end", payload + "\n")
                # 控制日志行数
                if int(self.txt_log.index("end-1c").split(".")[0]) > MAX_LOG_LINES:
                    self.txt_log.delete("1.0", "2.0")
                self.txt_log.see("end")
                self.txt_log.config(state="disabled")
            elif kind == "angle":
                deg, beam, score = payload
                self._angle_target, self._beam, self._score = float(deg), beam, score
                if not self._angle_now and deg:
                    self._angle_now = float(deg)
            elif kind == "level":
                self._level = float(payload)
            elif kind == "partial":
                text, remain = payload
                if text:
                    self.lbl_partial.config(
                        text=(f"剩余{remain}s  " if remain is not None else "") + text)
                else:
                    self.lbl_partial.config(
                        text=(f"剩余{remain}s  …" if remain is not None else "—"))

    @staticmethod
    def _kind_color(kind):
        return {"info": "#dfe7f5", "listen": "#9aa7b8", "awake": "#67c1ff",
                "recognize": "#ffd479", "result": "#7ee787",
                "done": "#7ee787", "warn": "#ff9a8f"}.get(kind, "#dfe7f5")

    def _animate(self):
        """指针平滑转到目标角度(0~360 取最短路径)"""
        diff = (self._angle_target - self._angle_now + 540) % 360 - 180
        self._angle_now = (self._angle_now + diff * 0.35) % 360

    # ------------------------------------------------------------ 绘制
    def _polar(self, deg, r):
        """角度(度) -> 画布坐标"""
        import math
        a = math.radians(deg)
        if not ANGLE_ZERO_AT_TOP:
            a += math.pi / 2
        if not ANGLE_CLOCKWISE:
            a = -a
        cx = cy = RADAR_SIZE / 2
        # 0° 朝上、顺时针: x = sin, y = -cos
        return cx + r * math.sin(a), cy - r * math.cos(a)

    def _draw_radar(self):
        c = self.canvas
        c.delete("radar")
        cx = cy = RADAR_SIZE / 2
        r_max = cx - 30

        # 同心圆 + 十字刻度
        for frac in (1.0, 0.66, 0.33):
            r = r_max * frac
            c.create_oval(cx - r, cy - r, cx + r, cy + r,
                          outline="#22304a", tags="radar")
        c.create_line(cx - r_max, cy, cx + r_max, cy, fill="#1b2740", tags="radar")
        c.create_line(cx, cy - r_max, cx, cy + r_max, fill="#1b2740", tags="radar")

        # 当前波束扇区(6 波束, 每个 60°)
        if self._beam is not None:
            start = 30 - int(self._beam) * 60
            c.create_arc(cx - r_max, cy - r_max, cx + r_max, cy + r_max,
                         start=start, extent=60, style="pieslice",
                         fill="#16324d", outline="", tags="radar")

        # 刻度与角度标注
        for deg in range(0, 360, 30):
            x1, y1 = self._polar(deg, r_max)
            x2, y2 = self._polar(deg, r_max + 8)
            c.create_line(x1, y1, x2, y2, fill="#3a4a66", tags="radar")
            tx, ty = self._polar(deg, r_max + 20)
            c.create_text(tx, ty, text=str(deg), fill="#5f708a",
                          font=(self._font, 9), tags="radar")

        # 指针
        ax, ay = self._polar(self._angle_now, r_max * 0.92)
        c.create_line(cx, cy, ax, ay, fill="#ff6b6b", width=3,
                      arrow="last", arrowshape=(14, 16, 6), tags="radar")
        c.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill="#ff6b6b",
                      outline="", tags="radar")

        # 中心信息
        c.create_text(cx, cy + 44, text=f"{self._angle_now:.0f}°",
                      fill="#eaf1ff", font=(self._font, 26, "bold"), tags="radar")
        info = []
        if self._beam is not None:
            info.append(f"beam {self._beam}")
        if self._score is not None:
            info.append(f"score {self._score}")
        if info:
            c.create_text(cx, cy + 74, text="  ".join(info), fill="#8fd0ff",
                          font=(self._font, 11), tags="radar")
        c.create_text(cx, 16, text="0°", fill="#5f708a",
                      font=(self._font, 9), tags="radar")

    def _draw_level(self):
        c = self.canvas_level
        c.delete("all")
        w = float(c["width"])
        h = float(c["height"])
        n = 32
        gap = 2
        bw = (w - gap * (n + 1)) / n
        level = max(0.0, min(1.0, self._level))
        lit = int(round(level * n))
        for i in range(n):
            x = gap + i * (bw + gap)
            if i < lit:
                color = "#41d18b" if i < n * 0.65 else ("#ffd479" if i < n * 0.85
                                                        else "#ff6b6b")
            else:
                color = "#1b2740"
            c.create_rectangle(x, 4, x + bw, h - 4, fill=color, outline="")


def demo_preview(seconds: int = 20):
    """本地预览: 不用硬件, 模拟角度/电平/状态变化, 验证界面效果"""
    import random
    ui = SoundRadarUI("声源定位界面预览(模拟数据)")
    end = time.time() + seconds
    deg = 0.0

    def fake():
        nonlocal deg
        if time.time() > end:
            ui.root.destroy()
            return
        deg = (deg + random.uniform(15, 95)) % 360
        ui.angle(deg, beam=int(deg // 60) % 6, score=random.randint(1000, 1600))
        ui.level(random.uniform(0.05, 0.9))
        ui.partial(random.choice(["", "打开", "打开图片", "关闭图片"]), remain=3)
        ui.state(random.choice(["监听中：请先说唤醒词", "已唤醒：声源角度 %.0f°" % deg,
                                "识别中：开始录音 4s", "识别结果：打开图片"]),
                 random.choice(["listen", "awake", "recognize", "result"]))
        ui.log("[%s] [状态] 模拟事件, 角度 %.0f°" % (time.strftime("%H:%M:%S"), deg))
        ui.root.after(400, fake)

    ui.root.after(300, fake)
    ui.run()


if __name__ == "__main__":
    import sys
    demo_preview(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
