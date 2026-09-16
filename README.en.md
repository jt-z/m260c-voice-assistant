# m260c-voice-assistant

**Voice control for your desktop and a robot arm** — built on a WHEELTEC M260C smart speaker
(iFlytek XFM-DP 6-mic circular array + M2-series noise-reduction board), it closes the full loop on Linux:
**wake word → recording → offline ASR → command execution / LLM Q&A → spoken reply**.

Say *"你好宽宽, 打开图片"* and the latest image opens; say *"给我倒杯咖啡"* and the robot-arm inference
script is launched; anything else is answered by DeepSeek and read out loud.

中文文档: [README.md](README.md)

![Overall preview](ui/Screenshot%20from%202026-09-16%2016-22-15.png)

*Overall preview of the voice assistant in action*

---

## Features

| Capability | Description |
|---|---|
| **On-board wake word + DOA** | The wake word runs in the speaker firmware (currently「你好宽宽」). Wake events and the 0–360° sound-source angle are reported over serial — zero wake latency on the PC side |
| **Always-on capture + ring pre-roll** | Recording starts at process launch and a 1.2 s ring buffer is kept, so the audio right before the wake event is prepended — **this fixes the classic "first syllable of the command is cut off" problem** |
| **Offline ASR** | SenseVoice-Small (FunASR + ModelScope, CPU) by default; fall back to lightweight Vosk with `--asr vosk`. Rolling re-decoding produces near-real-time subtitles |
| **Command execution** | 「打开图片」(open image) / 「关闭图片」(close image) / 「给我倒杯咖啡」(make coffee → launches robot-arm inference script; say「停止」to abort) |
| **LLM fallback** | Anything that doesn't match a command goes to DeepSeek; the reply is spoken via TTS. Without an API key it runs in stub mode so the whole pipeline still works |
| **Speech synthesis** | edge-tts → ffmpeg → paplay, cached by hash of *text + voice* (works offline afterwards), with timeout and retries |
| **Real-time HUD** | Sci-fi style PySide6/Qt6 UI: sound-source radar, spectrum waterfall, level bar, status and diagnostics panels. Display-only — the program keeps running if you close it |
| **Zero-dependency protocol** | `mic_serial.py` implements the 0xA5 frame protocol with stdlib `termios` only (handshake / version / manual wakeup / change wake word) — **no iFlytek account required** |

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│ M260C smart speaker (on-board wake engine, firmware)      │
└───────────────────────────┬──────────────────────────────┘
                            │ USB serial /dev/ttyACM2 (CH9102, 115200)
                            │ 0xA5 frames: handshake 0x01 / device event 0x04
                            ▼
              ┌─────────────────────────────┐
              │ serial_reader thread          │ ACKs handshake, parses events
              │  - throttled 0xFF ack (1/s)   │ auto-reconnect on unplug
              │  - wake + angle/beam/score    │ 30s diagnostic summary
              └──────────────┬──────────────┘
                             │ _wake_q.put((angle, beam, score))
                             ▼
┌──────────────────────────────────────────────────────────┐
│ run_live main loop (worker thread)                        │
│  _wake_q.get() → _busy.set()                              │
│      ↓                                                    │
│  stream_recognize()                                       │
│      ├─ AudioCapture.grab_stream(3s)  1.2s pre-roll + live │
│      ├─ save audio/cmd_YYYYmmdd_HHMMSS.wav                │
│      ├─ SenseVoice rolling re-decode (every 0.5s → text)   │
│      └─ final decode on the complete audio                 │
│      ↓                                                    │
│  dispatch(text)                                           │
│      ├─ command hit → run action (image / coffee task)     │
│      └─ miss → DeepSeek Q&A + TTS, else fallback phrase     │
└──────────────────────────────────────────────────────────┘
                    ┌────────────────────────────────────┐
                    │ AudioCapture thread (always on)     │
                    │  arecord → 4000B chunks → ring buf  │
                    │  on_chunk callback → HUD waterfall  │
                    └────────────────────────────────────┘
```

> Key design point: the **control channel (serial) and the audio channel (USB sound card) are independent**.
> Wake detection and DOA happen inside the noise-reduction board; the PC only records, recognizes, decides and speaks.
> Serial I/O lives in its own thread so the handshake never goes blind while recording.

---

## Hardware

- **WHEELTEC M260C smart speaker**: iFlytek XFM-DP 6-mic circular array, M2-series noise-reduction board, on-board USB sound card
- Serial chip CH9102 (`1a86:55d4`) → `/dev/ttyACM*` (the code auto-detects it by USB ID, never hard-codes the port)
- Capture: XFM-DP (USB Audio In, 16000 Hz mono); Playback: C-Media USB sound card (48000 Hz stereo)
- Robot-arm task (optional): a lerobot + ACT model workspace, see "Coffee task" below

```bash
$ ls -l /dev/ttyACM*
crw-rw---- 1 root dialout 166, 2 /dev/ttyACM2     # noise-reduction board (used here)
$ pactl list short sources | grep iflytek
… alsa_input.usb-iflytek_XFM-DP-V0.0.18_..._mono-fallback  s16le 1ch 16000Hz
```

## Requirements

**System**: Python 3.12, `ffmpeg`, `paplay` (PulseAudio), `arecord`/`aplay` (ALSA), a Linux desktop session (HUD needs X11/Wayland)

**Python packages**

```bash
pip install funasr modelscope edge-tts PySide6 requests
```

| Package | Purpose |
|---|---|
| `funasr` + `modelscope` | SenseVoice-Small offline ASR (~936 MB auto-downloaded to `~/.cache/modelscope` on first run) |
| `edge-tts` | Online Chinese TTS (results are cached, so playback works offline later) |
| `PySide6` | HUD interface |
| `requests` | DeepSeek HTTP API |
| `vosk` (optional) | Lightweight ASR fallback; bring your own `models/vosk-model-small-cn-0.22` |

---

## Quick start

```bash
git clone https://github.com/jt-z/m260c-voice-assistant.git
cd m260c-voice-assistant

# LLM fallback (optional; without a key it runs in stub mode)
export DEEPSEEK_API_KEY=sk-xxxxxxxx      # or put it in a .deepseek_key file

# Run with the HUD
python voice_command_demo.py --ui --log-file run.log

# Run headless (no UI)
python voice_command_demo.py
```

**How to use: say the wake word「你好宽宽」first, then your command.**

| You say | Result |
|---|---|
| 你好宽宽 → 打开图片 | Opens the newest image on the Desktop (falls back to Pictures) |
| 你好宽宽 → 关闭图片 | Closes the image opened before |
| 你好宽宽 → 给我倒杯咖啡 | Opens a terminal window running the robot-arm inference script; say「停止」to abort |
| 你好宽宽 → anything else | Answered by DeepSeek and spoken via TTS |
| (no match / no API key) | Speaks the fallback phrase「暂时还处理不了」 |

### Common commands

```bash
# —— main program ——
python voice_command_demo.py --ui                 # with HUD
python voice_command_demo.py --asr vosk           # use Vosk instead
python voice_command_demo.py --no-llm             # disable the LLM fallback
python voice_command_demo.py --duration 5         # record 5s after wake (default 3)
python voice_command_demo.py --wav a.wav          # recognize an existing WAV (no hardware)
python voice_command_demo.py --coffee-script /path/x.sh   # use another coffee-task script

# —— hardware / protocol (no iFlytek account needed) ——
python voice_interact_test.py                     # wake → record → playback loop
python voice_interact_test.py --version           # query board firmware version
python voice_interact_test.py --manual-wake       # trigger a wake manually
python voice_interact_test.py --set-wakeword "ni2 hao3 kuan1 kuan1"   # change wake word (replug speaker)
python voice_interact_test.py --threshold 900     # wake threshold, higher = harder

# —— benchmarks / UI preview / module self-test ——
python bench_asr.py                               # Vosk vs SenseVoice comparison
python sound_radar_hud.py 30                      # HUD preview only (simulated data)
python tts.py "hello"
python llm.py "hello"
```

---

## Modules

| File | Responsibility | Dependencies |
|---|---|---|
| `voice_command_demo.py` | **Main program**: wires everything together | all |
| `mic_serial.py` | iFlytek XFM-DP serial protocol (0xA5 frames / handshake / events / commands) | stdlib `termios` only |
| `audio_capture.py` | Always-on capture + 1.2 s ring pre-roll buffer | stdlib + `arecord` |
| `asr_sensevoice.py` | SenseVoice-Small offline ASR wrapper (PCM or WAV input) | `funasr` + ModelScope |
| `tts.py` | edge-tts → ffmpeg → paplay with hash cache, timeout, retries | `edge-tts`, `ffmpeg`, `paplay` |
| `llm.py` | DeepSeek Q&A (OpenAI-compatible HTTP API), stub mode without key | `requests` |
| `sound_radar_hud.py` | PySide6 HUD: radar + waterfall + status/log/stats | `PySide6` |
| `voice_interact_test.py` | Hardware test tool (firmware version, wake word, audio routing, record/playback) | stdlib |
| `bench_asr.py` | Vosk vs SenseVoice benchmark | both |

---

## Documentation

Detailed technical docs live in `notes/` — **currently written in Chinese**. Entry point: [notes/README.md](notes/README.md)

| Doc | Content |
|---|---|
| [01-架构与线程模型.md](notes/01-架构与线程模型.md) | Pipeline, module roles, threads/queues, shared-state synchronization |
| [02-硬件与串口协议.md](notes/02-硬件与串口协议.md) | M260C/XFM-DP hardware, 0xA5 frame format, message types, commands |
| [03-音频采集与TTS.md](notes/03-音频采集与TTS.md) | Always-on capture + pre-roll, playback routing, TTS chain and cache |
| [04-语音识别.md](notes/04-语音识别.md) | Vosk vs SenseVoice, rolling re-decoding, first-syllable loss |
| [05-命令与咖啡任务.md](notes/05-命令与咖啡任务.md) | Command grammar, image feature, coffee task and script keys |
| [06-大模型问答.md](notes/06-大模型问答.md) | DeepSeek integration, reasoning-model token pitfalls, config |
| [07-HUD界面.md](notes/07-HUD界面.md) | PySide6 real-time UI: radar + spectrum waterfall |
| [08-踩坑与排错.md](notes/08-踩坑与排错.md) | **Every pitfall we hit and its fix, plus a symptom-based troubleshooting guide** |

---

## What is *not* in this repository

Only **source code and docs** are versioned. The following are excluded (see [.gitignore](.gitignore)) and must be provided locally:

| Excluded | Why / how to get it |
|---|---|
| `语音模块客户资料V6.0_20260617/` | Vendor (WHEELTEC / iFlytek) customer materials and SDKs — **copyrighted by the vendor, not redistributable**; the protocol is reimplemented in `mic_serial.py` and documented in `notes/` |
| `models/` | Vosk models are large; download `vosk-model-small-cn-0.22` into this directory yourself |
| `audio/` | Real recordings (contain human speech) and TTS cache — local runtime artifacts |
| `.deepseek_key` | API key, **never commit it**; prefer the `DEEPSEEK_API_KEY` environment variable |

---

## Known limitations

- The **wake word lives in the speaker firmware**. Change it over the serial command
  (`voice_interact_test.py --set-wakeword "..."`) — **the speaker must be replugged** for it to take effect.
- The factory wake word is「小微小微」; this repo is currently configured for「你好宽宽」. Make sure it matches your
  device (see `voice_command_demo.py: WAKE_WORD_TEXT`).
- The **coffee task depends on an external workspace**: the default script path
  `COFFEE_SCRIPT = /home/kf/LX/pai0/run_inference_b601_make_coffee_ACT_50k.sh` plus the lerobot/ACT checkpoint must be
  prepared separately; use `--coffee-script` to point at your own script.
- **No iFlytek offline command-word / online AIUI recognition** is included (that needs an iFlytek open-platform APPID
  and resource files). Local ASR replaces it, so no iFlytek account is required.
- The HUD needs a graphical session (`DISPLAY`/Wayland); drop `--ui` on headless machines. If Qt6 complains about a
  missing `libxcb-cursor.so.0`, install the corresponding system package.
- The first run downloads the SenseVoice weights (~936 MB) and takes ~5–6 s to load; each recognition afterwards takes
  0.1–0.25 s.

---

## License

Released under the [MIT License](LICENSE).

Third-party components (FunASR / ModelScope / SenseVoice models, edge-tts, PySide6, Vosk and its models) remain under
their own licenses. The vendor materials in `语音模块客户资料V6.0_20260617/` are not part of this repository and are
not covered by this project's MIT license.
