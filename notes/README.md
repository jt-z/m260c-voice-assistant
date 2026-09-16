# voice_operate —— M260C 语音交互与命令控制

Linux 下通过语音控制 WHEELTEC M260C 智能音箱（讯飞 XFM-DP 环形六麦阵列 + M2 系列降噪板），
实现「唤醒 → 识别 → 执行命令 / 大模型问答 → 语音播报」的完整闭环，并可驱动机器人臂完成任务。

---

## 快速开始

```bash
# 主程序（推荐用 lerobot conda 环境，里面装好了 funasr / edge-tts / PySide6）
/home/kf/miniconda3/envs/lerobot/bin/python voice_command_demo.py --ui

# 不带界面
/home/kf/miniconda3/envs/lerobot/bin/python voice_command_demo.py

# 同时落盘日志
/home/kf/miniconda3/envs/lerobot/bin/python voice_command_demo.py --ui --log-file run.log
```

使用流程：**先说唤醒词「你好宽宽」，再说命令**。

| 你说 | 效果 |
|---|---|
| 你好宽宽 → 打开图片 | 打开最新的图片文件 |
| 你好宽宽 → 关闭图片 | 关闭之前打开的那张图 |
| 你好宽宽 → 给我倒杯咖啡 | 新开终端窗口跑机械臂推理脚本 |
| 你好宽宽 → 其它任何话 | 交给 DeepSeek 回答并播报 |

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [01-架构与线程模型.md](01-架构与线程模型.md) | 整体链路、模块职责、多线程/队列设计、数据流 |
| [02-硬件与串口协议.md](02-硬件与串口协议.md) | M260C/XFM-DP 硬件、串口帧协议、消息类型、下发命令、音频设备 |
| [03-音频采集与TTS.md](03-音频采集与TTS.md) | 常开录音+环形预滚、播放路由、TTS 三级链路、缓存 |
| [04-语音识别.md](04-语音识别.md) | Vosk vs SenseVoice、滚动重解码、首字丢失问题 |
| [05-命令与咖啡任务.md](05-命令与咖啡任务.md) | 命令词表、图片功能、咖啡任务与 lerobot 按键 |
| [06-大模型问答.md](06-大模型问答.md) | DeepSeek 接入、推理模型的坑、配置项 |
| [07-HUD界面.md](07-HUD界面.md) | PySide6 实时界面：声源雷达 + 频谱瀑布图 |
| [08-踩坑与排错.md](08-踩坑与排错.md) | **所有踩过的坑与修法 + 排错手册（最有价值）** |

---

## 模块一览

| 文件 | 职责 | 依赖 |
|---|---|---|
| `mic_serial.py` | 讯飞 XFM-DP 串口协议实现（组帧/解析/握手/命令） | 纯标准库 `termios` |
| `audio_capture.py` | 常开录音 + 1.2s 环形预滚缓冲 | 标准库 + `arecord` |
| `asr_sensevoice.py` | SenseVoice-Small 离线识别封装 | `funasr` + ModelScope |
| `tts.py` | edge-tts → ffmpeg → paplay，带缓存与重试 | `edge-tts` + `ffmpeg` + `paplay` |
| `llm.py` | DeepSeek 问答（OpenAI 兼容 HTTP API） | `requests` |
| `sound_radar_hud.py` | PySide6 HUD：雷达 + 瀑布图 + 状态 + 日志 | `PySide6` |
| `voice_command_demo.py` | **主程序**：串起上面所有模块 | 全部 |
| `voice_interact_test.py` | 硬件测试工具（改唤醒词、查音频路由、录音回放） | 标准库 |
| `bench_asr.py` | Vosk vs SenseVoice 识别基准对比 | 两者 |

---

## 环境依赖

**必须**：
- Python 3.12（`/home/kf/miniconda3/envs/lerobot/bin/python`）
- 系统命令：`ffmpeg`、`paplay`（PulseAudio）、`arecord`（ALSA）
- Python 包：`funasr`、`modelscope`、`edge-tts`、`PySide6`、`requests`

**可选**：
- `pynput`（让脚本内的 n/q 按键在管道 stdin 下也生效）

**模型**（首次自动下载）：
- SenseVoice：`iic/SenseVoiceSmall`（约 936MB，缓存在 `~/.cache/modelscope`）
- Vosk（可选回退）：`models/vosk-model-small-cn-0.22`

---

## 常用命令

```bash
# —— 主程序 ——
voice_command_demo.py --ui                 # 带 HUD 界面
voice_command_demo.py --no-llm             # 关掉大模型兜底（只播"暂时还处理不了"）
voice_command_demo.py --asr vosk           # 退回 Vosk 识别
voice_command_demo.py --duration 5         # 唤醒后录音 5 秒（默认 3）
voice_command_demo.py --wav a.wav          # 直接识别已有录音（不连硬件）
voice_command_demo.py --coffee-script /path/x.sh   # 换咖啡任务脚本

# —— 硬件与协议 ——
voice_interact_test.py --version           # 查降噪板固件版本
voice_interact_test.py --manual-wake       # 手动触发唤醒（不用喊）
voice_interact_test.py --set-wakeword "ni2 hao3 kuan1 kuan1"   # 改唤醒词（改完需拔插音箱）
voice_interact_test.py --threshold 900     # 唤醒阈值，越大越难唤醒

# —— 识别基准 ——
bench_asr.py                               # Vosk vs SenseVoice 对比

# —— 只预览界面（模拟数据，不连硬件）——
sound_radar_hud.py 30

# —— TTS 自测 ——
tts.py "测试一句话"

# —— 大模型自测 ——
llm.py "你好"
```

---

## 关键配置速查

| 配置 | 位置 | 当前值 |
|---|---|---|
| 唤醒词 | `voice_interact_test.py: WAKE_WORD_TEXT/PINYIN` | 「你好宽宽」/ `ni2 hao3 kuan1 kuan1` |
| 唤醒后录音时长 | `voice_command_demo.py: --duration` | 3s |
| 预滚时长 | `voice_command_demo.py: PRE_ROLL_SEC` | 1.2s |
| 识别引擎 | `voice_command_demo.py: --asr` | SenseVoice (`sv`) |
| 咖啡脚本 | `voice_command_demo.py: COFFEE_SCRIPT` | `/home/kf/LX/pai0/run_inference_b601_make_coffee_ACT_50k.sh` |
| 兜底提示语 | `voice_command_demo.py: --fallback-say` | 暂时还处理不了 |
| 大模型 | `llm.py: MODEL` | `deepseek-v4-flash` |
| 大模型 key | 环境变量 `DEEPSEEK_API_KEY` 或 `.deepseek_key` 文件 | 已配置 |
| TTS 音色 | `tts.py: DEFAULT_VOICE` | `zh-CN-XiaoxiaoNeural` |

---

## 相关路径

```
/home/kf/dev/voice_operate/          本项目
├── audio/                           录音存档 + TTS 缓存（audio/tts_cache/）
├── models/                          Vosk 模型
├── notes/                           本目录（文档）
└── ...

/home/kf/LX/pai0/                     机械臂工作区
├── run_inference_b601_make_coffee_ACT_50k.sh    咖啡推理脚本
├── 50kact_b601/pretrained_model/                ACT 权重
├── lerobot/src/lerobot/                         lerobot 源码
└── logs/                                       推理日志
```
