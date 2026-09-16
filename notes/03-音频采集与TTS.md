# 03 - 音频采集与 TTS

## 1. 录音链路：常开录音 + 环形预滚

### 问题：命令第一个字总被切掉

唤醒事件要走「降噪板检测 → 串口上报 → PC 解析入队 → 启动 arecord」，这中间有 **100~300ms**。
用户唤醒后紧接着说命令，第一个字正好落在这个空隙里。

实测被切掉的例子：

| 想说 | 识别成 |
|---|---|
| 打开图片 | **开**图片 |
| 关闭图片 | **闭**图片 |
| 看一下图片 | **下**图片 |

### 方案：进程启动就持续录音，维护一个环形缓冲

```
进程启动
   ↓
AudioCapture.start()  →  arecord 常开（不加 -d，持续输出 raw S16LE 16k 单声道）
   ↓
采集线程：每次读 4000 字节 → 追加到环形缓冲 → 只保留最近 PRE_ROLL(1.2s) 秒
   ↓
唤醒到来时 grab_stream(3) 被调用
   ↓ 先把环形缓冲里的 1.2s「预滚」吐出来
   ↓ 再接着吐实时音频，直到累计 (3 + 1.2) 秒
   ↓
得到一段"含唤醒前声音"的完整音频
```

**关键实现细节**（[`audio_capture.py`](../audio_capture.py)）：

```python
chunk = self._proc.stdout.read(4000)          # 阻塞读 4000 字节 ≈ 0.125s
with self._cond:
    self._chunks.append((self._seq, chunk))   # 带序号入环形缓冲
    self._seq += 1
    if not self._grabbing:                    # ★ 抓取期间不裁剪
        self._trim_locked()
    self._cond.notify_all()
```

- **为什么带序号**：`grab_stream()` 用 `next_seq` 游标取块，保证不重不漏
- **为什么抓取期间暂停裁剪**：否则一边吐一边裁，会丢块
- **抓取结束**：`self._chunks = self._chunks[-1:]` 只留最后一块，避免下一次抓取吐出陈旧音频
- **看门狗**：`deadline = time.time() + seconds + pre_roll + 3.0`，防止设备卡死时无限等待
- **自动重启**：`arecord` 退出或设备被占用时，2 秒后重试（拔插音箱不影响程序）

### 音频格式约定

| 项 | 值 |
|---|---|
| 采样率 | 16000 Hz |
| 声道 | 单声道 |
| 采样格式 | S16LE（16bit 小端） |
| 每秒字节数 | 32000（`BYTES_PER_SEC = 16000 * 2`） |

识别、录音、SenseVoice 全部统一用这个格式，避免重采样。

### 录音设备选择（`build_arecord_cmd`）

```
1) PulseAudio 源：pactl 里找含 "XFM-DP" 的 source
   → arecord -D default -f S16_LE -r 16000 -c 1  （等价于设 PULSE_SOURCE 环境变量）
2) 回退 ALSA 直连：arecord -l 里找卡名含 "XFM" 的 → plughw:CARD=X,DEV=0
3) 再回退：系统默认设备
```

**为什么优先走 PulseAudio**：PA 会独占 ALSA 硬件，直连 `plughw` 通常报 `Device busy`。

---

## 2. 播放路由

```python
# voice_interact_test.py: playback_cmds()
1) paplay --device=<C-Media 的 PA sink>        ← 优先
2) aplay -D plughw:CARD=<USB Audio>,DEV=0     ← 回退
3) aplay <wav>                                 ← 最后兜底
```

sink 的查找逻辑：`pactl list sinks short` 里找含 `C-Media` 或 `USB_Audio_Device`
且**不含** `XFM`（避免误选录音设备）的那一项。

本机实际 sink：

```
alsa_output.usb-C-Media_Electronics_Inc._USB_Audio_Device-00.iec958-stereo   s16le 2ch 48000Hz
```

> 注意 sink 是 48000Hz 立体声，而 TTS 合成的是 44100Hz 立体声 ——
> paplay 走 PulseAudio 会自动重采样，不需要额外处理。

---

## 3. TTS 链路（[`tts.py`](../tts.py)）

### 三级设计

```
speak(text) 
   │
   ├─① 缓存命中？ audio/tts_cache/<sha1(voice|text)[:16]>.wav
   │     └ 命中 → 直接播放（离线也能用）
   │
   ├─② 未命中 → edge-tts 在线合成 mp3
   │     └─ 失败重试 3 次（间隔 0.8s / 1.6s）
   │         ↓ 成功
   │     ffmpeg 转码 → 44100Hz / 2ch WAV → 写入缓存
   │
   └─③ 合成最终失败 → 打印日志、跳过本次播报（不再退 spd-say，见第 4 节）
         ↓
   播放：paplay（带 _play_lock 互斥）
```

### 关键参数

| 常量 | 值 | 说明 |
|---|---|---|
| `DEFAULT_VOICE` | `zh-CN-XiaoxiaoNeural` | 微软中文女声 |
| `SYNTH_ATTEMPTS` | 3 | 合成尝试次数 |
| `SYNTH_TIMEOUT` | 15.0s | 单次合成超时（`asyncio.wait_for`） |
| `MAX_SPEAK_CHARS`（在 llm.py） | 120 | 播报长度上限 |

### 缓存

```python
key = sha1((voice + "|" + text).encode()).hexdigest()[:16]
path = audio/tts_cache/<key>.wav          # 一个文件同时是 mp3 的临时源和最终产物
tmp_mp3 = path + ".mp3"                   # 转码完即删
```

**预热**：`main()` 启动时后台线程调用 `prewarm()`，把常用提示音先合成好：

| 预热文本 | 用途 |
|---|---|
| `暂时还处理不了` | 兜底播报 |
| `我想想` | 交给大模型前的等待反馈 |

预热后是**秒出声**（省掉 1~2s 合成）。

### 播放互斥为什么只锁播放

```python
# tts.py
_play_lock = threading.Lock()

def speak(...):
    if not os.path.exists(wav):
        _synth_edge(...)              # ★ 合成不加锁 → 可并行
    with _play_lock:                  # ★ 只有播放互斥
        ok = _play(wav)
```

场景：说「你好宽宽你可以做什么」时，会先播 `我想想`（异步线程），主线程同时在请求 DeepSeek。
如果锁包住"合成+播放"，必须等 `我想想` 整段播完才能开始合成回复，白等 2 秒多。

实测两段播报总耗时：**4.0s → 3.0s**。

---

## 4. 踩过的坑（音频/TTS 部分）

### 4.1 中文被念成一串怪音（"tried saver"）

**现象**：偶尔听到 7~8 秒的怪音，像英文又不是。

**排查过程**：

1. 抓音频流：`pactl list sink-inputs` 显示 `speech-dispatcher-espeak-ng`
2. 定位到 `tts.py` 的兜底分支：`[TTS] edge-tts 不可用, 退回 spd-say`
3. 实测耗时对比：

   | 命令 | 耗时 |
   |---|---|
   | `spd-say -l zh -w "好的，开始为你制作咖啡"`（10 字） | **6.98s** |
   | `spd-say -l en -w "Recording episode one"`（4 词） | 1.61s |

**根因**：edge-tts 联网偶发失败（实测 RTT 1.36~3.16s 波动很大），
而旧代码一失败就退到 `spd-say`，espeak-ng 的中文是**拼音+音素硬拼、无声调建模**，音调全散且极慢。

**修法**：
- 合成加超时 + 重试 3 次
- **去掉 `spd-say` 兜底**，失败就静默跳过（宁可没有声音，也不要 7 秒怪音）
- 失败原因打到日志（旧代码把异常吞了，什么都看不到）

### 4.2 所有新合成都失败：`ffmpeg 转码失败 rc=183`

**现象**：缓存里已有的提示音能播，**任何新文本都合成失败**，报 `rc=183`。

**排查**（走了很多弯路）：

| 假设 | 结论 |
|---|---|
| edge-tts 网络问题 | 排除，多次合成成功 |
| mp3 数据损坏 | 排除，把 mp3 截断一半 ffmpeg 照样转成功 |
| 并发合成冲突 | 排除，4 线程并发全成功 |
| Qt 环境干扰 | 排除，在 Qt 进程内子线程合成成功 |
| `LD_LIBRARY_PATH` 污染 | 排除（试了 `~/.local/lib/qt-xcb`、conda lib） |
| conda 里有另一个 ffmpeg | 排除，没有 |
| ffmpeg 各种错误场景 | 全部返回 `rc=1`，没有 183 |

**真凶**：先让错误信息带上 ffmpeg 的 stderr，才拿到关键线索：

```
Unknown input format: 'mp3'
Error opening input file .../89b2131f001d9d16.wav.mp3
```

再 `which -a ffmpeg`：

```
/usr/share/trae-cn/resources/app/bin/ffmpeg    ← Trae IDE 自带，PATH 第一位
/usr/bin/ffmpeg
/bin/ffmpeg
```

```bash
$ /usr/share/trae-cn/resources/app/bin/ffmpeg -version
ffmpeg version 6.1.1
$ 支持的 demuxer 数量: 8            ← 极简构建
$ 有 mp3 吗: ✗ 没有

$ /usr/bin/ffmpeg
支持 341 个 demuxer
```

**Trae IDE 自带了一个只支持 8 种格式的 ffmpeg 塞在 PATH 最前**，遮蔽了系统完整版。

**修法（两层）**：

```python
# ① tts.py：不依赖 PATH，用绝对路径 + 清理可能劫持库的环境变量
def _ffmpeg_exe():
    for p in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return "ffmpeg"

def _ffmpeg_env():
    env = dict(os.environ)
    for k in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
        env.pop(k, None)
    return env
```

```bash
# ② ~/.bashrc 末尾（治本，让所有程序受益）
PATH=$(printf '%s' "$PATH" | tr ':' '\n' | grep -vx '/usr/share/trae-cn/resources/app/bin' | paste -sd: -)
export PATH
```

> 这个坑的教训：**错误信息一定要保留原始 stderr**。
> 旧代码 `print("ffmpeg 转码失败 rc=%s" % r.returncode)` 把 stderr 吞了，
> 导致只能对着 `183` 这个无意义的数字瞎猜。

### 4.3 播报成功但没声音（返回值没检查）

`tts.speak()` 返回 `(是否成功, 方式)`，且**合成失败时不抛异常**。
但调用方写的是 `tts_speak(reply)` 不看返回值 → 一路判定"已播报" →

**日志说 DeepSeek 答了，实际一声没出**。

```python
# 修法
ok, how = tts_speak(reply)
if not ok:
    log_state(f"回复合成/播放失败({how})，只显示文字没出声", "warn")
    return False          # 回退到兜底语
```

### 4.4 一次问答要 10 秒

实测耗时构成：

| 环节 | 耗时 |
|---|---|
| "我想想" 播报 | 1.5s |
| DeepSeek 请求 | 2~2.5s |
| 回复**合成** | ~2s |
| 回复**播放** | 4~5s ← 大头，由回答长度决定 |

优化手段：合成并行（4.0→3.0s）、预热"我想想"（省 1~2s）。
进一步压缩要靠**缩短回答长度**（`llm.py` 的 system prompt 字数上限）。

---

## 5. 自测命令

```bash
# 直接播一句话（验证 TTS 全链路）
tts.py "这是一次测试"

# 看当前音频路由
pactl list short sources | grep -i xfm
pactl list short sinks  | grep -i cmedia

# 看谁在占着音频（排查"没声音"）
while :; do pactl list sink-inputs 2>/dev/null | grep -aE "Sink Input|application.name"; sleep 0.2; done | awk '!a[$0]++'

# 看 ffmpeg 是不是被劫持了
which -a ffmpeg
ffmpeg -hide_banner -demuxers | grep -c ""     # 341 行左右才是完整版
```
