# -*- coding: utf-8 -*-
"""
sound_radar_hud.py —— M260C 声源定位「科幻 HUD」实时界面(PySide6 / Qt6)

与 sound_radar_ui.py(Tk 版) 暴露完全相同的接口, 业务层(voice_command_demo.py)零改动:
    state(text, kind) / log(line) / angle(deg, beam, score) / level(rms)
    samples(chunk) / partial(text, remain) / stats(dict) / on_close(cb) / run()

Qt 相比 Tk 的优势(本次用到): QPainter 抗锯齿、径向/线性/锥形渐变、真正的 alpha 透明度、
圆角与变换矩阵 → 辉光/拖尾/半透明扇区都是原生渲染, 稳定 60fps。

线程模型: worker 线程只调用上述方法(内部入 Queue), 主线程 QTimer(16ms) 取队列重绘。
独立预览(不连硬件):  python sound_radar_hud.py 30
"""
import math
import os
import queue
import sys
import time

from PySide6.QtCore import Qt, QTimer, QPointF, QRectF
from PySide6.QtGui import (QColor, QConicalGradient, QFont, QFontDatabase,
                           QImage, QLinearGradient, QPainter, QPainterPath,
                           QPen, QRadialGradient, QTextCharFormat, QTextCursor)
from PySide6.QtWidgets import (QApplication, QFrame, QGraphicsDropShadowEffect,
                               QHBoxLayout, QLabel, QMainWindow, QSizePolicy,
                               QTextEdit, QVBoxLayout, QWidget)

try:
    import numpy as _np                      # 瀑布图用 FFT
except Exception:
    _np = None

# 角度->屏幕映射(如与实际方向不符, 只改这两个常量)
ANGLE_ZERO_AT_TOP = True
ANGLE_CLOCKWISE = True

HISTORY_LEN = 24
MAX_LOG_LINES = 500
POLL_MS = 16                     # ~60fps
WAV_RATE = 16000                 # 与采集/识别一致(16kHz 单声道 S16LE)

# ---------------------------------------------------------------- 配色
BG = "#070b12"
PANEL = "#0b1220"
GRID = "#16283e"
CYAN = "#00e5ff"
BLUE = "#3fa9ff"
RED = "#ff4d6d"
AMBER = "#ffd479"
GREEN = "#41d18b"
TEXT = "#c9d8ea"
DIM = "#5d7896"
WHITE = "#eaf6ff"


def C(hexstr, alpha=255):
    """带透明度的 QColor(alpha 是 Qt 原生能力, Tk 做不到)"""
    c = QColor(hexstr)
    c.setAlpha(alpha)
    return c


# ---------------------------------------------------------------- 雷达画布
class RadarWidget(QWidget):
    """环形角度雷达: 刻度/波束扇区/扫描线/指针辉光/历史热迹/中心读数"""

    def __init__(self, hud):
        super().__init__()
        self.hud = hud
        self.setMinimumSize(500, 500)

    def _polar(self, deg, r):
        a = math.radians(deg)
        if not ANGLE_ZERO_AT_TOP:
            a += math.pi / 2
        if not ANGLE_CLOCKWISE:
            a = -a
        cx, cy = self.width() / 2, self.height() / 2     # 画布可能非正方形, 分别取两边中点
        return QPointF(cx + r * math.sin(a), cy - r * math.cos(a))

    def paintEvent(self, _ev):
        p = QPainter(self)
        try:
            self._paint(p)
        finally:
            p.end()                  # 异常也要结束绘制, 否则 Qt 报 active painter

    def _paint(self, p):
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2            # 画布非正方形时仍保持居中
        r = min(w, h) / 2 - 30

        # 深空背景: 径向渐变(中心微亮)
        p.fillRect(0, 0, w, h, C(BG))
        gbg = QRadialGradient(QPointF(cx, cy), r * 1.35)
        gbg.setColorAt(0.0, C("#0d1a2b"))
        gbg.setColorAt(0.55, C("#0a1522"))
        gbg.setColorAt(1.0, C(BG))
        p.setBrush(gbg)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx, cy), r * 1.02, r * 1.02)

        # 空闲时扫描线: 锥形渐变拖尾(60fps 平滑转动)
        if not self.hud.active:
            self._draw_sweep(p, cx, cy, r)

        # 同心圆 + 十字线
        p.setBrush(Qt.NoBrush)
        for frac in (1.0, 0.75, 0.5, 0.25):
            rr = r * frac
            p.setPen(QPen(C(GRID, 200), 1.4))
            p.drawEllipse(QPointF(cx, cy), rr, rr)
            p.setPen(QPen(C("#0f1d2e", 160), 1.0))
            p.drawEllipse(QPointF(cx, cy), rr - 1.5, rr - 1.5)
        p.setPen(QPen(C(GRID, 220), 1.0))
        p.drawLine(QPointF(cx - r, cy), QPointF(cx + r, cy))
        p.drawLine(QPointF(cx, cy - r), QPointF(cx, cy + r))

        # 刻度: 每 10° 短线, 每 30° 长线+标注
        fm = p.fontMetrics()
        for deg in range(0, 360, 10):
            long_tick = (deg % 30 == 0)
            a1 = self._polar(deg, r)
            a2 = self._polar(deg, r + (11 if long_tick else 5))
            p.setPen(QPen(C(CYAN, 230) if long_tick else C(GRID, 220),
                          2.0 if long_tick else 1.0))
            p.drawLine(a1, a2)
            if long_tick:
                t = self._polar(deg, r + 23)
                p.setPen(QPen(C(DIM)))
                txt = str(deg)
                p.drawText(QRectF(t.x() - 16, t.y() - 8, 32, 16),
                           Qt.AlignCenter, txt)
        _ = fm

        # 6 个波束扇区(60°): 非当前微弱半透明, 当前波束青蓝高亮
        for i in range(6):
            active_beam = (self.hud.beam is not None and int(self.hud.beam) == i)
            path = QPainterPath()
            path.moveTo(cx, cy)
            path.arcTo(QRectF(cx - r, cy - r, 2 * r, 2 * r), 30 - i * 60, 60)
            path.closeSubpath()
            if active_beam:
                grad = QRadialGradient(QPointF(cx, cy), r)
                grad.setColorAt(0.0, C(CYAN, 90))
                grad.setColorAt(1.0, C(CYAN, 18))
                p.setBrush(grad)
                p.setPen(QPen(C(CYAN, 210), 2.0))
            else:
                p.setBrush(C(BLUE, 12))
                p.setPen(QPen(C(GRID, 200), 1.0))
            p.drawPath(path)

        # 角度历史热迹(按时间衰减 alpha)
        now = time.time()
        for deg, ts in self.hud.history:
            age = min(1.0, (now - ts) / 30.0)
            alpha = int(230 * (1.0 - age)) + 15
            a1 = self._polar(deg, r * 0.60)
            a2 = self._polar(deg, r * 0.98)
            p.setPen(QPen(C(BLUE, alpha), 2.0))
            p.drawLine(a1, a2)
            rr = 2.5 + 3.5 * (1 - age)
            p.setBrush(C(BLUE, alpha))
            p.setPen(Qt.NoPen)
            p.drawEllipse(a2, rr, rr)

        # 指针: 三层描边模拟辉光 + 箭头 + 轴心
        tip = self._polar(self.hud.angle_now, r * 0.95)
        for width, alpha in ((13, 30), (8, 70), (3.5, 255)):
            p.setPen(QPen(C(RED, alpha), width, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(cx, cy), tip)
        dx, dy = tip.x() - cx, tip.y() - cy
        ln = math.hypot(dx, dy) or 1
        ux, uy = dx / ln, dy / ln
        px, py = -uy, ux
        p.setBrush(C(RED))
        p.setPen(Qt.NoPen)
        arrow = QPainterPath()
        arrow.moveTo(tip.x() + ux * 9, tip.y() + uy * 9)
        arrow.lineTo(tip.x() - ux * 12 + px * 7, tip.y() - uy * 12 + py * 7)
        arrow.lineTo(tip.x() - ux * 12 - px * 7, tip.y() - uy * 12 - py * 7)
        arrow.closeSubpath()
        p.drawPath(arrow)
        p.setBrush(C(RED, 90))
        p.drawEllipse(QPointF(cx, cy), 11, 11)
        p.setBrush(C(RED))
        p.drawEllipse(QPointF(cx, cy), 6, 6)
        p.setBrush(C(WHITE))
        p.drawEllipse(QPointF(cx, cy), 2.2, 2.2)

        # 中心读数
        col = CYAN if self.hud.active else DIM
        p.setPen(QPen(C(col)))
        f = QFont(self.hud.mono_family, 30, QFont.Bold)
        p.setFont(f)
        p.drawText(QRectF(cx - 110, cy + 30, 220, 46), Qt.AlignCenter,
                   f"{self.hud.angle_now:.0f}°")
        sub = []
        if self.hud.beam is not None:
            sub.append(f"BEAM {self.hud.beam}")
        if self.hud.score is not None:
            sub.append(f"SCORE {self.hud.score}")
        p.setFont(QFont(self.hud.mono_family, 10))
        p.setPen(QPen(C(DIM)))
        p.drawText(QRectF(cx - 130, cy + 76, 260, 20), Qt.AlignCenter,
                   "   ".join(sub) or "SCANNING")

        # 底部电平条(渐变)
        bw, bh = r * 1.5, 9
        bx, by = cx - bw / 2, h - 20
        p.setPen(QPen(C(GRID, 200), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(bx, by, bw, bh))
        lvl = max(0.0, min(1.0, self.hud._level))
        if lvl > 0:
            lg = QLinearGradient(bx, by, bx + bw, by)
            lg.setColorAt(0.0, C(GREEN))
            lg.setColorAt(0.7, C(AMBER))
            lg.setColorAt(1.0, C(RED))
            p.setBrush(lg)
            p.setPen(Qt.NoPen)
            p.drawRect(QRectF(bx + 1, by + 1, (bw - 2) * lvl, bh - 2))

    def _draw_sweep(self, p, cx, cy, r):
        """扫描线: 锥形渐变 + 前沿高亮"""
        deg = self.hud.sweep
        grad = QConicalGradient(QPointF(cx, cy), 90 - deg)   # 与屏幕角度对齐
        grad.setColorAt(0.0, C(CYAN, 110))
        grad.setColorAt(0.10, C(CYAN, 40))
        grad.setColorAt(0.16, C(CYAN, 0))
        grad.setColorAt(1.0, C(CYAN, 0))
        p.setBrush(grad)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QPointF(cx, cy), r, r)
        tip = self._polar(deg, r)
        p.setPen(QPen(C(CYAN, 200), 1.6))
        p.drawLine(QPointF(cx, cy), tip)


# ---------------------------------------------------------------- 频谱瀑布图
class WaterfallWidget(QWidget):
    """实时频谱瀑布图: 横轴=时间(左旧右新), 纵轴=频率(下低上高), 颜色=能量(dB)"""

    F_MIN, F_MAX = 80.0, 8000.0
    DB_RANGE = 60.0          # 显示动态范围(dB)

    def __init__(self, hud):
        super().__init__()
        self.hud = hud
        self.setMinimumSize(340, 190)

    def paintEvent(self, _ev):
        p = QPainter(self)
        try:
            self._paint(p)
        finally:
            p.end()

    def _paint(self, p):
        p.setRenderHint(QPainter.Antialiasing, False)   # 瀑布图用硬边更清晰
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, C(BG))

        pad_l, pad_b, pad_t, pad_r = 34, 14, 16, 12
        gw, gh = w - pad_l - pad_r, h - pad_b - pad_t
        wf = self.hud._wf
        if wf is not None and _np is not None and wf.size:
            flip = _np.flipud(wf)                        # 低频放底部
            rgb = self.hud._lut[flip]                    # (bands, cols, 3) uint8
            rgb = _np.ascontiguousarray(rgb)
            img = QImage(rgb.data, rgb.shape[1], rgb.shape[0], 3 * rgb.shape[1],
                         QImage.Format_RGB888)
            p.drawImage(QRectF(pad_l, pad_t, gw, gh), img)
        else:
            p.setPen(QPen(C(DIM)))
            p.setFont(QFont(self.hud.mono_family, 10))
            p.drawText(QRectF(pad_l, pad_t, gw, gh), Qt.AlignCenter,
                       "等待音频…" if _np is not None else "需要 numpy 才能绘制瀑布图")

        # 边框 + 频率刻度
        p.setPen(QPen(C(GRID, 220), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(pad_l, pad_t, gw, gh))
        p.setFont(QFont(self.hud.mono_family, 8))
        p.setPen(QPen(C(DIM)))
        for f in (0, 2000, 4000, 6000, 8000):
            frac = (math.log10(max(f, self.F_MIN)) - math.log10(self.F_MIN)) / \
                   (math.log10(self.F_MAX) - math.log10(self.F_MIN)) if f > 0 else 0.0
            y = pad_t + gh * (1 - frac)
            p.drawText(QRectF(2, y - 6, pad_l - 6, 12), Qt.AlignRight | Qt.AlignVCenter,
                       ("%dk" % (f // 1000)) if f else "80")
            p.setPen(QPen(C(GRID, 160), 1))
            p.drawLine(QPointF(pad_l, y), QPointF(pad_l + gw, y))
            p.setPen(QPen(C(DIM)))
        # 峰值频率
        p.setFont(QFont(self.hud.mono_family, 9))
        p.setPen(QPen(C(CYAN)))
        p.drawText(QRectF(pad_l + 6, pad_t + 2, gw - 12, 14), Qt.AlignLeft,
                   "峰值 %.2f kHz" % (self.hud._peak_hz / 1000.0))
        # 色标
        lw = 8
        lx = w - pad_r - lw + 2 if w - pad_r + lw < w else w - lw - 2
        for i in range(int(gh)):
            v = 255 - int(255 * i / max(1, gh - 1))       # 顶亮底暗
            c = QColor(*[int(x) for x in self.hud._lut[v]])
            p.setPen(QPen(c))
            p.drawLine(QPointF(lx, pad_t + i), QPointF(lx + lw, pad_t + i))
        p.setPen(Qt.NoPen)


# ---------------------------------------------------------------- 主窗口
class HudWindow(QMainWindow):
    def __init__(self, hud, title):
        super().__init__()
        self.hud = hud
        self.setWindowTitle(title)
        self.resize(1200, 860)
        self.setStyleSheet(f"""
            QMainWindow, QWidget {{ background: {BG}; }}
            QFrame#panel {{ background: {PANEL}; border: 1px solid {GRID};
                            border-radius: 8px; }}
            QLabel#title {{ color: {CYAN}; }}
            QLabel#dim {{ color: {DIM}; }}
            QTextEdit {{ background: #05080e; color: {TEXT};
                         border: 1px solid {GRID}; border-radius: 8px;
                         font-family: "{hud.mono_family}"; font-size: 12px; }}
            QScrollBar:vertical {{ background: {BG}; width: 10px; }}
            QScrollBar::handle:vertical {{ background: {GRID}; border-radius: 5px; }}
        """)

        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        # 标题行 + LIVE 状态指示灯
        head = QHBoxLayout()
        t = QLabel("M260C   SOUND   LOCALIZATION")
        t.setObjectName("title")
        t.setFont(QFont(hud.mono_family, 13, QFont.Bold))
        head.addWidget(t)
        self.lbl_live = QLabel("● LIVE")
        self.lbl_live.setFont(QFont(hud.mono_family, 10, QFont.Bold))
        self.lbl_live.setStyleSheet(f"color:{GREEN};")
        head.addSpacing(10)
        head.addWidget(self.lbl_live)
        head.addStretch(1)
        self.lbl_stats = QLabel("")
        self.lbl_stats.setObjectName("dim")
        self.lbl_stats.setFont(QFont(hud.mono_family, 10))
        head.addWidget(self.lbl_stats)
        self.lbl_clock = QLabel("")
        self.lbl_clock.setObjectName("dim")
        self.lbl_clock.setFont(QFont(hud.mono_family, 10))
        head.addWidget(self.lbl_clock)
        lay.addLayout(head)

        # 状态条(左侧色带 + 大字)
        sbar = QFrame()
        sbar.setObjectName("panel")
        sbar.setFixedHeight(52)
        sl = QHBoxLayout(sbar)
        sl.setContentsMargins(0, 0, 12, 0)
        self.acc = QFrame()
        self.acc.setFixedWidth(6)
        self.acc.setStyleSheet(f"background:{DIM};")
        sl.addWidget(self.acc)
        self.lbl_status = QLabel("初始化中…")
        self.lbl_status.setFont(QFont(hud.cjk_family, 16, QFont.Bold))
        self.lbl_status.setStyleSheet(f"color:{TEXT};")
        sl.addSpacing(10)
        sl.addWidget(self.lbl_status, 1)
        lay.addWidget(sbar)
        # 状态条 + 雷达: 加青色辉光(Qt 原生阴影效果)
        for widget in (sbar,):
            eff = QGraphicsDropShadowEffect(widget)
            eff.setBlurRadius(26)
            eff.setColor(QColor(0, 229, 255, 70))
            eff.setOffset(0, 0)
            widget.setGraphicsEffect(eff)

        # 中部: 雷达 | 右侧信息
        mid = QHBoxLayout()
        self.radar = RadarWidget(hud)
        mid.addWidget(self.radar, 3)

        right = QVBoxLayout()
        right.setSpacing(8)

        def panel(title, inner, stretch=0):
            f = QFrame()
            f.setObjectName("panel")
            v = QVBoxLayout(f)
            v.setContentsMargins(10, 6, 10, 8)
            lb = QLabel(title)
            lb.setObjectName("dim")
            lb.setFont(QFont(hud.mono_family, 10))
            v.addWidget(lb)
            v.addWidget(inner)
            right.addWidget(f, stretch)
            return f

        self.lbl_partial = QLabel("—")
        self.lbl_partial.setFont(QFont(hud.cjk_family, 15, QFont.Bold))
        self.lbl_partial.setStyleSheet(f"color:{AMBER};")
        self.lbl_partial.setWordWrap(True)
        panel("实时识别文本", self.lbl_partial)

        self.wave = WaterfallWidget(hud)
        panel("实时频谱瀑布图 (80Hz–8kHz)", self.wave)

        self.lbl_hint = QLabel("说「小宽小宽」唤醒 → 说「打开图片」/「关闭图片」")
        self.lbl_hint.setWordWrap(True)
        self.lbl_hint.setStyleSheet(f"color:{BLUE};")
        self.lbl_hint.setFont(QFont(hud.cjk_family, 12))
        panel("提示", self.lbl_hint)

        mid.addLayout(right, 2)
        lay.addLayout(mid, 3)

        # 日志
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(180)
        self.log.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lay.addWidget(self.log, 2)

        # 日志配色
        self.fmt = {
            "time": _fmt(DIM), "wake": _fmt(CYAN), "res": _fmt(GREEN),
            "warn": _fmt(AMBER), "diag": _fmt(DIM), "text": _fmt(TEXT),
        }

    def closeEvent(self, ev):
        if self.hud._on_close:
            try:
                self.hud._on_close()
            except Exception:
                pass
        ev.accept()


def _fmt(color):
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    return f


# ---------------------------------------------------------------- 门面(与 Tk 版同接口)
class SoundRadarHUD:
    def __init__(self, title="M260C 声源定位 HUD"):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self.cjk_family = self._pick_font(
            ["Noto Sans CJK SC", "Noto Sans SC", "WenQuanYi Micro Hei",
             "Source Han Sans SC", "Microsoft YaHei", "DejaVu Sans"])
        self.mono_family = self._pick_font(
            ["DejaVu Sans Mono", "Noto Sans Mono CJK SC", "Liberation Mono"])

        self._q = queue.Queue()
        self._on_close = None
        # 渲染状态
        self.angle_now = 0.0
        self.angle_target = 0.0
        self.beam = None
        self.score = None
        self._level = 0.0           # 注意不要与方法 level() 同名
        # 瀑布图状态
        self.WF_BANDS, self.WF_COLS = 72, 200
        self._wf = None
        self._fft_ring = _np.zeros(1024, dtype=_np.float32) if _np is not None else None
        self._peak_hz = 0.0
        self._lut = self._build_lut()
        self._blink = 0
        self._kind = "info"
        self.sweep = 0.0
        self.active = False
        self.history = []
        self._partial = ("", None)
        self._stats = {}
        self._pending_logs = []

        self.win = HudWindow(self, title)
        self.timer = QTimer()
        self.timer.timeout.connect(self._tick)
        self.timer.start(POLL_MS)

    @staticmethod
    def _pick_font(cands):
        fams = set(QFontDatabase.families())
        for n in cands:
            if n in fams:
                return n
        return "Sans Serif"

    # ---------------- 线程安全 API ----------------
    def state(self, text, kind="info"):
        self._q.put(("state", (text, kind)))

    def log(self, line):
        self._q.put(("log", line))

    def angle(self, deg, beam=None, score=None):
        self._q.put(("angle", (deg, beam, score)))

    def level(self, rms):
        self._q.put(("level", rms))

    def samples(self, chunk):
        self._q.put(("samples", chunk))

    def partial(self, text, remain=None):
        self._q.put(("partial", (text, remain)))

    def stats(self, d):
        self._q.put(("stats", d))

    def on_close(self, cb):
        self._on_close = cb

    def run(self):
        self.win.show()
        self._app.exec()

    # ---------------- 刷新 ----------------
    def _tick(self):
        self._drain()
        # 指针平滑 + 空闲扫描
        diff = (self.angle_target - self.angle_now + 540) % 360 - 180
        self.angle_now = (self.angle_now + diff * 0.3) % 360
        if not self.active:
            self.sweep = (self.sweep + 1.8) % 360
        self.win.lbl_clock.setText(time.strftime("%H:%M:%S"))
        if self._pending_logs:
            for line, tag in self._pending_logs:
                self._append_log(line, tag)
            self._pending_logs.clear()
        self.win.radar.update()
        self.win.wave.update()
        # LIVE 指示灯: 监听时缓慢呼吸, 识别时快闪
        self._blink = (self._blink + 1) % 100
        if self._kind == "recognize":
            on = (self._blink % 12) < 8
        else:
            on = (self._blink % 60) < 44
        self.win.lbl_live.setStyleSheet(
            f"color:{self._kind_color(self._kind) if on else PANEL};")

    def _drain(self):
        while True:
            try:
                kind, p = self._q.get_nowait()
            except queue.Empty:
                return
            if kind == "state":
                text, st = p
                color = self._kind_color(st)
                self._kind = st
                self.win.lbl_status.setText(text)
                self.win.lbl_status.setStyleSheet(f"color:{color};")
                self.win.acc.setStyleSheet(f"background:{color};")
                self.active = st in ("awake", "recognize")
                self._pending_logs.append((f"[状态] {text}", "text"))
            elif kind == "log":
                self._pending_logs.append((p, self._tag(p)))
            elif kind == "angle":
                deg, beam, score = p
                self.angle_target = float(deg)
                self.beam, self.score = beam, score
                if not self.history:
                    self.angle_now = float(deg)
                self.history.append((float(deg), time.time()))
                del self.history[:-HISTORY_LEN]
            elif kind == "level":
                self._level = float(p)
            elif kind == "samples":
                self._push_samples(p)
            elif kind == "partial":
                text, remain = p
                if text:
                    self.win.lbl_partial.setText(
                        (f"[剩余{remain}s] " if remain is not None else "") + text)
                else:
                    self.win.lbl_partial.setText(
                        (f"[剩余{remain}s] 聆听中…" if remain is not None else "—"))
            elif kind == "stats":
                self._stats = p
                self.win.lbl_stats.setText(" ".join(f"{k}:{v}" for k, v in p.items()))

    @staticmethod
    def _kind_color(kind):
        return {"info": DIM, "listen": "#8fa6c0", "awake": CYAN, "recognize": AMBER,
                "result": GREEN, "done": GREEN, "warn": RED}.get(kind, DIM)

    @staticmethod
    def _tag(line):
        if "[状态] 已唤醒" in line or "[唤醒]" in line:
            return "wake"
        if "[识别] 结果片段" in line or "识别结果" in line or "[命令]" in line:
            return "res"
        if "警告" in line or "未识别" in line or "忽略" in line:
            return "warn"
        if "[诊断]" in line:
            return "diag"
        return "time"

    def _append_log(self, line, tag):
        self.win.log.moveCursor(QTextCursor.End)
        self.win.log.setTextCursor(self.win.log.textCursor())
        self.win.log.textCursor().insertText(line + "\n", self.win.fmt.get(tag, self.win.fmt["text"]))
        # 行数上限
        doc = self.win.log.document()
        while doc.blockCount() > MAX_LOG_LINES:
            cur = QTextCursor(doc)
            cur.movePosition(QTextCursor.Start)
            cur.select(QTextCursor.BlockUnderCursor)
            cur.removeSelectedText()
            cur.deleteChar()
        self.win.log.moveCursor(QTextCursor.End)

    @staticmethod
    def _build_lut():
        """瀑布图色标: 深蓝 -> 蓝 -> 青 -> 绿 -> 黄 -> 红 (256 级)"""
        stops = [(0.00, (5, 8, 16)), (0.18, (16, 42, 94)), (0.38, (14, 108, 148)),
                 (0.56, (22, 196, 176)), (0.74, (208, 226, 74)), (0.88, (255, 158, 60)),
                 (1.00, (255, 64, 84))]
        out = []
        for i in range(256):
            t = i / 255.0
            for k in range(len(stops) - 1):
                t0, c0 = stops[k]
                t1, c1 = stops[k + 1]
                if t0 <= t <= t1:
                    f = (t - t0) / (t1 - t0)
                    out.append(tuple(int(round(c0[j] + (c1[j] - c0[j]) * f))
                                     for j in range(3)))
                    break
            else:
                out.append(stops[-1][1])
        if _np is not None:
            return _np.array(out, dtype=_np.uint8)
        return out

    def _push_samples(self, chunk):
        """原始音频块 -> FFT -> 瀑布图新列(左移, 右端追加)"""
        if _np is None or self._fft_ring is None:
            return
        raw = chunk[:len(chunk) // 2 * 2]
        a = _np.frombuffer(raw, dtype="<i2").astype(_np.float32) / 32768.0
        if a.size < 64:
            return
        n = min(a.size, self._fft_ring.size)
        self._fft_ring = _np.roll(self._fft_ring, -n)
        self._fft_ring[-n:] = a[-n:]

        spec = _np.abs(_np.fft.rfft(self._fft_ring * _np.hanning(self._fft_ring.size)))
        freqs = _np.fft.rfftfreq(self._fft_ring.size, 1.0 / WAV_RATE)
        bands = self.WF_BANDS
        edges = _np.logspace(math.log10(WaterfallWidget.F_MIN),
                             math.log10(WaterfallWidget.F_MAX), bands + 1)
        col = _np.zeros(bands, dtype=_np.float32)
        for i in range(bands):
            m = (freqs >= edges[i]) & (freqs < edges[i + 1])
            col[i] = spec[m].max() if m.any() else 0.0
        db = 20.0 * _np.log10(col + 1e-6)
        val = _np.clip((db + WaterfallWidget.DB_RANGE + 10.0) /
                       WaterfallWidget.DB_RANGE, 0.0, 1.0) * 255.0
        if self._wf is None:
            self._wf = _np.zeros((bands, self.WF_COLS), dtype=_np.uint8)
        self._wf[:, :-1] = self._wf[:, 1:]
        self._wf[:, -1] = val.astype(_np.uint8)
        # 峰值频率(用于界面右上角显示)
        if col.max() > 0:
            self._peak_hz = float(freqs[int(col.argmax())])


def demo_preview(seconds: int = 30):
    """模拟数据预览(不连硬件)"""
    import random
    import struct
    hud = SoundRadarHUD("声源定位 HUD - 预览(模拟数据)")
    state = {"deg": 0.0, "phase": 0, "t0": time.time()}
    kinds = {"listen": "监听中：请先说唤醒词「小宽小宽」",
             "awake": "已唤醒：声源角度 {d:.0f}° → 请说命令词",
             "recognize": "识别中：开始录音 3s, 请说命令词…",
             "result": "识别结果：打开图片"}
    partials = ["", "打开", "打开图片", "关闭图片"]

    def tick():
        if time.time() - state["t0"] > seconds:
            hud.win.close()
            hud._app.quit()
            return
        state["deg"] = (state["deg"] + random.uniform(10, 120)) % 360
        d = state["deg"]
        hud.angle(d, int(d // 60) % 6, random.randint(1000, 1600))
        # 扫频音(300Hz~3.5kHz) + 底噪: 让瀑布图出现流动的亮线
        state["phase"] += 1
        sweep_hz = 300 + 3200 * (0.5 + 0.5 * math.sin(state["phase"] / 12.0))
        buf = b"".join(struct.pack("<h", int((7000 * math.sin(
            2 * math.pi * sweep_hz * i / 16000)) + random.uniform(-1500, 1500)))
            for i in range(800))
        hud.samples(buf)
        hud.level(random.uniform(0.1, 0.95))
        hud.partial(random.choice(partials), remain=3)
        k = random.choice(list(kinds))
        hud.state(kinds[k].format(d=d), k)
        hud.log(f"[{time.strftime('%H:%M:%S')}] [状态] 模拟事件 角度 {d:.0f}°")
        hud.stats({"握手": random.randint(200, 300), "事件": 1, "唤醒": 1,
                   "重复": 0, "忙碌": 0})
        QTimer.singleShot(350, tick)

    QTimer.singleShot(300, tick)
    hud.run()


def _ensure_xcb_cursor():
    """Qt6 xcb 插件需要 libxcb-cursor.so.0; 若系统缺但本地兜底目录存在, 带 LD_LIBRARY_PATH 重启.
    (系统执行过 sudo apt install libxcb-cursor0 后本函数自动跳过)"""
    libdir = os.path.expanduser("~/.local/lib/qt-xcb")
    if os.environ.get("_QT_XCB_FALLBACK") == "1" or not os.path.isdir(libdir):
        return
    import ctypes.util
    if ctypes.util.find_library("xcb-cursor"):
        return
    env = dict(os.environ)
    old = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = libdir + ((":" + old) if old else "")
    env["_QT_XCB_FALLBACK"] = "1"
    os.execve(sys.executable, [sys.executable] + sys.argv, env)


def _ensure_x_display():
    """GUI 需要连上 X server; 缺 XAUTHORITY 时自动补本机 gdm 的(常见坑)"""
    disp = os.environ.get("DISPLAY", "")
    if not disp:
        print("[错误] 未检测到 DISPLAY: 请在图形界面终端运行, 或 ssh -X 后再试")
        sys.exit(1)
    if not os.environ.get("XAUTHORITY"):
        cand = "/run/user/%d/gdm/Xauthority" % os.getuid()
        if os.path.exists(cand):
            os.environ["XAUTHORITY"] = cand
            print("[提示] 未设置 XAUTHORITY, 已自动使用", cand)
    sock = "/tmp/.X11-unix/X%s" % disp.split(":")[-1].split(".")[0]
    if not os.path.exists(sock):
        print(f"[错误] 找不到 X socket {sock} (DISPLAY={disp})")
        sys.exit(1)


if __name__ == "__main__":
    _ensure_x_display()
    _ensure_xcb_cursor()
    demo_preview(int(sys.argv[1]) if len(sys.argv) > 1 else 30)
