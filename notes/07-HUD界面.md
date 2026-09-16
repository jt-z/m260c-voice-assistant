# 07 - HUD 界面（PySide6）

## 1. 技术选型

| 方案 | 结论 |
|---|---|
| Tkinter | 能做，但画渐变/发光/半透明很吃力，视觉效果差 |
| Streamlit | 是 web 应用，做实时 60fps 雷达不现实，且要额外起服务 |
| matplotlib | 绘图库，不是 UI 框架，做仪表盘很别扭 |
| **PySide6 / Qt6** | **选定**：QPainter 直接画，抗锯齿/渐变/alpha 都有，60fps 无压力 |

界面只做**展示与反馈**，不参与任何控制逻辑 —— 关掉界面程序照常跑（自动退回无界面模式）。

---

## 2. 组成

```
SoundRadarHUD            ← 门面（业务代码只跟它打交道）
  ├─ _app: QApplication
  ├─ win: HudWindow(QMainWindow)
  │    ├─ RadarWidget        环形声源雷达
  │    └─ WaterfallWidget    实时频谱瀑布图
  └─ 队列：业务线程 → UI 线程 单向投递数据
```

| 类/函数 | 文件位置 | 职责 |
|---|---|---|
| `SoundRadarHUD` | 主门面 | 提供全部对外接口、维护队列、驱动 `_tick` |
| `RadarWidget` | 自定义 QWidget | 极坐标雷达、波束高亮、扫描线、历史轨迹 |
| `WaterfallWidget` | 自定义 QWidget | 频谱瀑布图（72 bands × 200 列，对数 80Hz–8kHz） |
| `HudWindow` | QMainWindow | 整体布局、状态条、日志面板、统计面板 |
| `demo_preview` | 函数 | 模拟数据预览（不连硬件） |

### 对外接口（业务侧只用这些）

```python
ui = SoundRadarHUD(title)
ui.state(text, kind="info")     # 状态提示（配色按 kind: listen/awake/recognize/result/done/warn）
ui.log(line)                    # 追加一行日志
ui.angle(deg, beam=None, score=None)   # 声源角度 + 波束 + 置信度
ui.level(rms)                   # 电平条 0~1
ui.samples(chunk)               # 原始 PCM 块（推给瀑布图）
ui.partial(text, remain=None)   # 实时识别文本 + 剩余秒数
ui.stats(dict)                  # 右上角诊断计数
ui.on_close(callback)           # 关窗回调（→ _stop.set()）
ui.run()                        # 进入 Qt 事件循环（阻塞）
```

---

## 3. 渲染与视觉

```python
ANGLE_ZERO_AT_TOP = True      # 0° 在正上方（而不是数学上的右侧）
ANGLE_CLOCKWISE   = True      # 角度顺时针增大
HISTORY_LEN       = 24        # 雷达上保留最近 24 个测向点
MAX_LOG_LINES     = 500       # 日志面板最多留 500 行
POLL_MS           = 16        # ≈60fps
```

配色（`#RRGGBB` 常量 + `C(hex, alpha)` 生成带透明度）：

```python
BG="#070b12"  PANEL="#0b1220"  GRID="#16283e"
CYAN="#00e5ff" BLUE="#3fa9ff"  RED="#ff4d6d"
AMBER="#ffd479" GREEN="#41d18b" TEXT="#c9d8ea" DIM="#5d7896"
```

绘制要点：

- `_polar(deg, r)` 做极坐标 → 屏幕坐标转换，配合 `ANGLE_ZERO_AT_TOP/CLOCKWISE`
- 渐变、半透明、`QPainter.Antialiasing`
- 分离 `paintEvent` 与 `_paint(p)`，并用 `try/finally: p.end()` 保证画笔一定收尾
- 瀑布图用查表（`_build_lut`）把 FFT 幅度映射成颜色，避免每帧重复算色

> **画布居中坑**：雷达圆心必须用 `cx, cy = w/2, h/2`（按各自宽高），
> 曾经写成取正方形边长导致非正方形窗口里雷达不居中 —— 是靠**离屏渲染 + 像素检查**发现的。

---

## 4. 线程 / 刷新模型

Qt 组件只能在主线程更新，所以：

```
业务线程（run_live）            UI 线程
   ui.state(...)  ─┐
   ui.angle(...)   │  统一 self._q.put((kind, payload))   ← queue.Queue
   ui.log(...)     │      （日志另存 _pending_logs 待批量刷）
   ui.samples(...) ─┘
                              self.timer: QTimer
                                 └─ _tick() 每 POLL_MS(16ms) 触发
                                       └─ _drain() 批量 get_nowait 并更新控件
```

- 对外方法一律只做 `self._q.put(...)`，不直接碰控件
- `_tick()` 里还负责：指针平滑（`angle_now` 向 `angle_target` 靠拢）、空闲扫描线、
  时钟文字、LIVE 指示灯呼吸、最后 `radar.update()` / `wave.update()` 触发重绘
- `_push_samples()` 把原始 PCM 送进瀑布图缓冲
- 这样业务线程永不触碰 Qt 对象，避免跨线程崩溃

运行方式（`--ui`）：

```python
# voice_command_demo.py: run_with_ui()
worker = threading.Thread(target=run_live, args=(args, rec), daemon=True)
worker.start()
_ui.on_close(lambda: _stop.set())
_ui.run()             # 主线程进入 Qt 事件循环
```

---

## 5. 环境坑（X11 / Qt 插件）

### 5.1 `could not connect to display :0`

GUI 需要连上 X server。两类常见原因：

| 原因 | 表现 | 处理 |
|---|---|---|
| 没有 `DISPLAY` | 纯字符终端 / ssh 未 `-X` | `_ensure_x_display()` 明确报错并退出 |
| 有 `DISPLAY` 但没 `XAUTHORITY` | 连不上 X | 自动补 `/run/user/<uid>/gdm/Xauthority` |
| X socket 不存在 | `DISPLAY` 值无效 | 检查 `/tmp/.X11-unix/X<n>` |

### 5.2 Qt6 xcb 插件缺 `libxcb-cursor.so.0`

```
qt.qpa.plugin: Could not load the Qt platform plugin "xcb"
```

`_ensure_xcb_cursor()` 的处理：本地兜底目录 `~/.local/lib/qt-xcb/` 里放了
`libxcb-cursor.so.0`，检测到系统缺这个库就带 `LD_LIBRARY_PATH` **重启自身**
（`os.execve`，因为 `LD_LIBRARY_PATH` 必须在进程启动前生效，运行中改 `os.environ` 无效）。

用 `_QT_XCB_FALLBACK=1` 环境变量防止无限重启。

装好系统包后自动跳过：

```bash
sudo apt install libxcb-cursor0
```

> ⚠️ 这个函数会给进程设 `LD_LIBRARY_PATH`，**子进程会继承**。
> 曾经怀疑它是 ffmpeg 失败的元凶，后来排除（真正原因是 Trae 的 ffmpeg 劫持 PATH，见 03 篇）。

---

## 6. 预览与验证

```bash
# 模拟数据预览（不连硬件），默认 30 秒
sound_radar_hud.py 30

# 主程序带界面
voice_command_demo.py --ui
```

**如何在没有显示器时验证渲染**（避免"没测就说好了"）：

```bash
QT_QPA_PLATFORM=offscreen python -c "
# 离屏渲染到 QImage，再检查像素
"
```

用离屏渲染 + 像素级检查发现过：
- 雷达未水平居中（圆心算法按正方形边长算的）
- `_samples`/`_level` 用了与方法同名的属性，导致 `TypeError: 'list' object is not callable`

后者的修法是给内部字段加下划线前缀（`self._samples` / `self._level`）。
