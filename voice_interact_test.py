# -*- coding: utf-8 -*-
"""
voice_interact_test.py —— M260C 智能音箱语音交互测试(纯 Python)

硬件: M260C 智能音箱(环形六麦阵列 + M2 系列降噪板, USB 设备名 XFM-DP-V0.0.18)
参考文档: /home/kf/dev/voice_operate/语音模块客户资料V6.0_20260617
         - readme【使用前必看】.pdf
         - 2.离线.../语音模块使用指南v2.0_20260601.pdf   (串口协议/唤醒/录音说明)
         - 6.M260C智能音箱使用资料/M260C智能音箱使用说明_20260601.pdf
协议实现: mic_serial.py(0xA5 帧, 115200, 握手/版本/唤醒事件)

本脚本仅依赖 Python 标准库与系统自带 arecord/aplay, 无需讯飞账号即可测试:
  1) 串口链路: 握手帧收发自动确认; --version 可查询降噪板固件版本
  2) 唤醒检测: 对音箱说唤醒词(当前「小宽小宽」, 以板内设置为准),
               解析并打印唤醒事件与声源角度(环形 0~360°)
  3) 录音拾音: 唤醒后自动用 XFM-DP 麦克风录制 N 秒指令音频存为 WAV
  4) 扬声器播报: 录制完成后回放到 C-Media USB 扬声器(双扬声器)

注意: 离线命令词/在线 AI 识别需要讯飞开放平台 APPID 与资源文件(文档第 3.3 节),
      本脚本不含识别引擎, 只做硬件链路级语音交互测试。

用法示例:
  python3 voice_interact_test.py                  # 唤醒->录音->回放 循环测试
  python3 voice_interact_test.py --version        # 仅查询固件版本
  python3 voice_interact_test.py --duration 5     # 每次唤醒后录 5 秒
  python3 voice_interact_test.py --no-playback    # 唤醒后只录音不播放
  python3 voice_interact_test.py --port /dev/ttyACM1   # 手动指定串口
  python3 voice_interact_test.py --set-wakeword "ni2 hao3 xiao3 wei1"   # 改唤醒词(如「你好小微」, 改完需拔插)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mic_serial import (MicSerial, MSG_SHAKE, MSG_AIUI, MSG_CONTROL,
                        MSG_CONFIRM, SerialTimeoutError, TYPE_NAMES,
                        pretty_payload)

AUDIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio")
WAV_RATE = 16000
# 当前板内唤醒词(2026-09 通过 --set-wakeword 改为「小宽小宽」; 出厂默认为「小微小微」)
WAKE_WORD_TEXT = "小宽小宽"
WAKE_WORD_PINYIN = "xiao3 kuan1 xiao3 kuan1"

# ---------------------------------------------------------------- 音频设备解析
def ala_card_ids(kind: str):
    """kind: 'arecord'/'aplay', 返回 {card_id: [display names]}"""
    out = subprocess.run([kind, "-l"], capture_output=True, text=True).stdout
    cards = {}
    for line in out.splitlines():
        m = re.match(r"\s*card \d+: ([^\s]+) \[([^\]]*)\]", line)
        if m:
            cid, disp = m.group(1), m.group(2)
            cards.setdefault(cid, []).append(disp)
    return cards


def pulse_source_for_xfm():
    """通过 PulseAudio 找到 iflytek XFM 麦克风源名(PA 已占用独占, 需经其录音)"""
    try:
        out = subprocess.run(["pactl", "list", "sources", "short"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if "XFM-DP" in line:
            return line.split("\t")[1]
    return None


def pulse_sink_for_speaker():
    """通过 PulseAudio 找到 C-Media USB 扬声器 sink 名
    (拔插后 PA 常独占 ALSA 设备, 此时直连 plughw 会报 Device busy, 需经 PA 播放)"""
    try:
        out = subprocess.run(["pactl", "list", "sinks", "short"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and ("C-Media" in line or "USB_Audio_Device" in line) \
                and "XFM" not in line:
            return parts[1]
    return None


def build_arecord_cmd(seconds: int, wav: str):
    """构造录音命令: 优先 PulseAudio 的 XFM 源; 失败回退 ALSA hw 直连"""
    src = pulse_source_for_xfm()
    if src:
        env = dict(os.environ, PULSE_SOURCE=src)
        cmd = ["arecord", "-D", "default", "-f", "S16_LE",
               "-r", str(WAV_RATE), "-c", "1", "-d", str(seconds), wav]
        return cmd, env, "PulseAudio 源 %s" % src
    cap_ids = ala_card_ids("arecord")
    xfm = next((c for c, d in cap_ids.items() if any("XFM" in x for x in d)), None)
    if xfm:
        cmd = ["arecord", "-D", "plughw:CARD=%s,DEV=0" % xfm, "-f", "S16_LE",
               "-r", str(WAV_RATE), "-c", "1", "-d", str(seconds), wav]
        return cmd, None, "ALSA 卡 %s" % xfm
    return ["arecord", "-f", "S16_LE", "-r", str(WAV_RATE), "-c", "1",
            "-d", str(seconds), wav], None, "系统默认录音设备"


def playback_cmds(wav: str):
    """播放候选命令(按优先级): 先经 PulseAudio 播到 C-Media USB 扬声器
    (拔插后 PA 常独占 ALSA, 直连 plughw 会报 Device busy), 再回退 ALSA plughw"""
    cmds = []
    sink = pulse_sink_for_speaker()
    if sink and shutil.which("paplay"):
        cmds.append(["paplay", "--device=%s" % sink, wav])
    pb_ids = ala_card_ids("aplay")
    target = next((c for c, d in pb_ids.items()
                   if any("USB Audio" in x and "XFM" not in x for x in d)), None)
    if target and shutil.which("aplay"):
        cmds.append(["aplay", "-D", "plughw:CARD=%s,DEV=0" % target, wav])
    if not cmds:
        cmds.append(["aplay", wav])
    return cmds


def record_audio(seconds: int, wav: str):
    cmd, env, desc = build_arecord_cmd(seconds, wav)
    print(f"[录音] 设备: {desc}  时长 {seconds}s -> {wav}")
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(wav) or os.path.getsize(wav) < 1000:
        print("[录音] 失败:", r.stderr.strip() or r.stdout.strip())
        return False
    print(f"[录音] 完成 {os.path.getsize(wav)} 字节")
    return True


def play_audio(wav: str):
    """回放录音: 按候选通路依次尝试(PA -> ALSA plughw)"""
    for cmd in playback_cmds(wav):
        print("[播放] ", " ".join(cmd))
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            print("[播放] 完成")
            return True
        print("[播放] 失败: " + (r.stderr.strip() or r.stdout.strip()))
    return False


# ---------------------------------------------------------------- JSON 载荷解析
def find_angles(obj, out=None):
    """递归提取 JSON 里的声源角度: 兼容 ivw.angle / content.info 嵌套 / 顶层 angle"""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "angle" and isinstance(v, (int, float)):
                out.append(int(v))
            elif k == "info" and isinstance(v, str):
                try:
                    find_angles(json.loads(v), out)
                except Exception:
                    pass
            else:
                find_angles(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_angles(v, out)
    return out


def has_key(obj, key):
    """递归判断 JSON 中是否存在指定键(如 ivw)"""
    if isinstance(obj, dict):
        if key in obj:
            return True
        for v in obj.values():
            if has_key(v, key):
                return True
    elif isinstance(obj, list):
        for v in obj:
            if has_key(v, key):
                return True
    return False


def find_values(obj, key, out=None):
    """递归收集 JSON 中所有指定键的数值(用于按 ivw.start_ms 去重)"""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, (int, float)):
                out.append(int(v))
            elif k == "info" and isinstance(v, str):
                try:
                    find_values(json.loads(v), key, out)
                except Exception:
                    pass
            else:
                find_values(v, key, out)
    elif isinstance(obj, list):
        for v in obj:
            find_values(v, key, out)
    return out


def open_mic(args):
    """打开降噪板串口(自动识别或 --port 指定)"""
    if args.port:
        return MicSerial(args.port).open()
    port, m = MicSerial.autodetect(timeout=1.5)
    if not port:
        raise SerialTimeoutError("未找到降噪板串口, 请检查 USB 连接")
    return m if m else MicSerial(port).open()


# ---------------------------------------------------------------- 交互主循环
def run_interact(args):
    if not os.path.exists(AUDIO_DIR):
        os.makedirs(AUDIO_DIR)

    # ---- 打开串口 ----
    try:
        mic = open_mic(args)
    except SerialTimeoutError as e:
        print(f"[错误] {e}")
        sys.exit(1)
    print("=" * 60)
    print("M260C 智能音箱语音交互测试")
    print(f"[串口] {mic.port} @115200  打开成功")
    print(f"[提示] 对音箱说唤醒词(当前: {WAKE_WORD_TEXT} / {WAKE_WORD_PINYIN}) 触发交互")
    print("=" * 60)

    # ---- 修改唤醒词(资料: {"type":"wakeup_keywords", ...}, 改完需重新插拔设备) ----
    if args.set_wakeword:
        cmd = {"type": "wakeup_keywords",
               "content": {"keyword": args.set_wakeword, "threshold": str(args.threshold)}}
        print(f"[唤醒词] 下发: keyword='{args.set_wakeword}' threshold={args.threshold}")
        replies, ok = mic.cmd_json(cmd, sid=3, wait_reply=3)
        for typ, sid, payload in replies:
            print("[唤醒词] ", pretty_payload(typ, payload).replace("\n", " "))
        print("[唤醒词] " + ("设备已回应" if ok else "未收到回应(命令可能已生效)")
              + " —— 请拔插一次音箱(重新上电), 再用新唤醒词测试")
        print(f"[唤醒词] 提示: 当前下发值 '{args.set_wakeword}'; "
              f"恢复出厂默认可用 --set-wakeword \"xiao3 wei1 xiao3 wei1\"")
        mic.close()
        return

    # ---- 版本查询 ----
    if args.version:
        replies, ok = mic.cmd_json({"type": "version"}, sid=1, wait_reply=4)
        for typ, sid, payload in replies:
            if typ == MSG_AIUI:
                print("[版本] ", pretty_payload(typ, payload))
        if not ok:
            print("[版本] 未收到回应(超时)")
        mic.close()
        return

    # ---- 手动唤醒(可选, 仅作命令链路测试) ----
    if args.manual_wake:
        replies, ok = mic.cmd_json({"type": "manual_wakeup", "content": {"beam": 0}},
                                   sid=2, wait_reply=3)
        print("[手动唤醒] 回应:",
              "; ".join(pretty_payload(t, p) for t, _, p in replies) or "(无)")
        mic.close()
        return

    # ---- 主循环: 监听唤醒 -> 录音 -> 回放 ----
    print(f"[提示] 唤醒后自动录音 {args.duration}s 并回放, Ctrl+C 退出")
    print(f"[提示] 录音文件目录: {AUDIO_DIR}")
    recent_wakes = {}      # ivw.start_ms -> 上次触发时间, 用于去重
    last_ack_t = 0.0       # 握手确认节流: 最多每秒 1 次
    try:
        while True:  # 外层: 断线后自动重连
            try:
                for typ, sid, payload in mic.read_frames(timeout=0.5):
                    # 握手帧节流确认(避免写缓冲区积压导致 EAGAIN)
                    if typ == MSG_SHAKE:
                        now = time.time()
                        if now - last_ack_t >= 1.0:
                            mic.ack_handshake(typ, sid, payload)
                            last_ack_t = now
                        continue
                    if typ not in (MSG_AIUI, MSG_CONTROL, MSG_CONFIRM):
                        print(f"[串口] {TYPE_NAMES.get(typ, hex(typ))} 消息 sid={sid} "
                              f"载荷={payload.hex(' ')[:60]}")
                        continue

                    text = payload.decode("utf-8", "replace")
                    try:
                        obj = json.loads(text)
                    except Exception:
                        obj = None
                    angles = find_angles(obj) if obj else []
                    is_wake = has_key(obj, "ivw") or bool(angles) \
                        or ("awake" in text.lower() or "wakeup" in text.lower())
                    if not is_wake:
                        print(f"[事件] type={TYPE_NAMES.get(typ, hex(typ))} sid={sid} "
                              + pretty_payload(typ, payload).replace("\n", " "))
                        continue

                    # 同一唤醒事件可能被固件重发, 用 start_ms 去重(30s 窗口)
                    stamp = tuple(find_values(obj, "start_ms")) if obj else ()
                    now = time.time()
                    if stamp and stamp in recent_wakes \
                            and now - recent_wakes[stamp] < 30.0:
                        print(f"[唤醒] 忽略重复事件 start_ms={stamp[0]}")
                        continue
                    if stamp:
                        recent_wakes = {k: t for k, t in recent_wakes.items()
                                        if now - t < 60.0}
                        recent_wakes[stamp] = now

                    ts = time.strftime("%Y%m%d_%H%M%S")
                    print("\n" + "-" * 60)
                    print(f"[唤醒] {ts}  声源角度: "
                          + (" ".join(f"{a}°" for a in sorted(set(angles)))
                             if angles else "未知"))
                    print("[事件] ", pretty_payload(typ, payload))
                    # 唤醒后: 录指令音频并回放
                    wav = os.path.join(AUDIO_DIR, f"cmd_{ts}.wav")
                    if record_audio(args.duration, wav) and not args.no_playback:
                        play_audio(wav)
                    print("-" * 60 + "\n")
            except (OSError, SerialTimeoutError) as e:
                print(f"[警告] 串口异常({e}), 3 秒后重连...")
                try:
                    mic.close()
                except OSError:
                    pass
                while True:  # 持续尝试重连, Ctrl+C 可中断
                    time.sleep(3)
                    try:
                        mic = open_mic(args)
                        break
                    except SerialTimeoutError:
                        print("[警告] 重连失败, 继续重试...")
                last_ack_t = 0.0
    except KeyboardInterrupt:
        print("\n用户中断, 退出")
    finally:
        try:
            mic.close()
        except OSError:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="M260C 智能音箱语音交互测试")
    ap.add_argument("--port", help="降噪板串口(默认自动检测)")
    ap.add_argument("--version", action="store_true", help="仅查询降噪板固件版本")
    ap.add_argument("--manual-wake", action="store_true",
                    help="发送手动唤醒命令后退出(命令链路测试)")
    ap.add_argument("--duration", type=int, default=4, help="唤醒后录音秒数(默认4)")
    ap.add_argument("--no-playback", action="store_true", help="唤醒后不自动回放")
    ap.add_argument("--set-wakeword", metavar="PINYIN",
                    help="修改唤醒词, 拼音带声调. 当前「小宽小宽」= xiao3 kuan1 xiao3 kuan1; "
                         "出厂默认「小微小微」= xiao3 wei1 xiao3 wei1. 改完需重新插拔设备")
    ap.add_argument("--threshold", default="900", help="唤醒阈值(默认900, 越大越难唤醒)")
    run_interact(ap.parse_args())
