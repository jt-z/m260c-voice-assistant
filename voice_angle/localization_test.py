#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
localization_test.py —— M260C 环形六麦声源定位测试

协议依据（厂商客户资料 V6.0）:
  - 新版M2系列/产品文档/串口通信协议文档.pdf
  - 新版M2系列/例程/Linux/serial_reader.py   (0xA5 帧 / 唤醒事件 JSON)
  - M2系列/文档/3.协议手册-R818降噪板.pdf     (angle / beam 定义)
唤醒事件中的 angle(0~359°) 与 beam(0~5) 由降噪板固件完成声源定位后上报,
本脚本只负责采集与统计; 串口层复用本仓库 mic_serial.py(与厂商协议一致, 纯标准库)。

测试流程:
  按提示站到指定方位 -> 连续说唤醒词 -> 采集 N 个有效样本 -> 自动换下一方位。
  结束后输出每个方位的圆周均值 / 圆周标准差 / 与期望偏差 / ±45° 命中率,
  并把原始样本写入 CSV。

用法:
  python3 voice_angle/localization_test.py
  python3 voice_angle/localization_test.py --positions 0,90,180,270 --per-position 10
  python3 voice_angle/localization_test.py --port /dev/ttyACM0 --csv voice_angle/result.csv
不给 --csv 时, CSV 默认写入本脚本所在目录(voice_angle/), 便于随仓库一起跟踪。
"""
import argparse
import csv
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)          # 串口层复用仓库根目录的 mic_serial

from mic_serial import (MicSerial, MSG_SHAKE, MSG_AIUI, SerialTimeoutError)

WAKE_WORD_TEXT = "你好宽宽"
DEFAULT_POSITIONS = "0,45,90,135,180,225,270,315"
HIT_TOLERANCE = 45.0          # 判定"方位命中"的角度容差
DEDUP_WINDOW = 30.0           # 固件会重发同一唤醒事件, 按 start_ms 去重(秒)
ACK_INTERVAL = 1.0            # 握手确认节流(秒)


# ---------------------------------------------------------------- 载荷解析
def find_nums(obj, key, out=None):
    """递归提取 JSON 中所有指定键的数值, 兼容 content.info 里再套 JSON 字符串"""
    if out is None:
        out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, (int, float)):
                out.append(float(v))
            elif k == "info" and isinstance(v, str):
                try:
                    find_nums(json.loads(v), key, out)
                except Exception:
                    pass
            else:
                find_nums(v, key, out)
    elif isinstance(obj, list):
        for v in obj:
            find_nums(v, key, out)
    return out


def has_key(obj, key):
    """递归判断 JSON 中是否存在指定键(如 ivw)"""
    if isinstance(obj, dict):
        if key in obj:
            return True
        return any(has_key(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(has_key(v, key) for v in obj)
    return False


def open_mic(port):
    """打开降噪板串口: 指定端口, 或按 USB ID 1a86:55d4 自动识别"""
    if port:
        return MicSerial(port).open()
    detected, mic = MicSerial.autodetect(timeout=1.5)
    if not detected:
        raise SerialTimeoutError("未找到降噪板串口, 请检查 USB 连接 (lsusb | grep 1a86)")
    return mic if mic else MicSerial(detected).open()


# ---------------------------------------------------------------- 唤醒事件
def wait_wake(mic, state):
    """阻塞等待一条新的唤醒事件, 返回 (angles, beam, score, start_ms, raw_obj)"""
    while True:
        for typ, sid, payload in mic.read_frames(timeout=0.5):
            if typ == MSG_SHAKE:
                now = time.time()
                if now - state["last_ack"] >= ACK_INTERVAL:
                    mic.ack_handshake(typ, sid, payload)
                    state["last_ack"] = now
                continue
            if typ != MSG_AIUI:
                continue

            text = payload.decode("utf-8", "replace")
            try:
                obj = json.loads(text)
            except Exception:
                continue

            angles = find_nums(obj, "angle")
            is_wake = has_key(obj, "ivw") or bool(angles) \
                or "awake" in text.lower() or "wakeup" in text.lower()
            if not is_wake:
                continue

            # 同一唤醒事件固件会重发多次, 用 start_ms 去重
            start_ms = int(find_nums(obj, "start_ms")[0]) if find_nums(obj, "start_ms") else None
            now = time.time()
            state["seen"] = {k: t for k, t in state["seen"].items()
                             if now - t < 2 * DEDUP_WINDOW}
            if start_ms is not None:
                if now - state["seen"].get(start_ms, 0.0) < DEDUP_WINDOW:
                    continue
                state["seen"][start_ms] = now

            beam_vals = find_nums(obj, "beam")
            score_vals = find_nums(obj, "score")
            return ([int(round(a)) % 360 for a in angles],
                    int(beam_vals[0]) if beam_vals else None,
                    score_vals[0] if score_vals else None,
                    start_ms,
                    obj)

        time.sleep(0.01)


# ---------------------------------------------------------------- 圆周统计
def circular_stats(angles):
    """返回 (圆周均值, 圆周标准差, 合成向量长度 R), 单位: 度"""
    rad = [math.radians(a) for a in angles]
    sum_sin = sum(math.sin(r) for r in rad)
    sum_cos = sum(math.cos(r) for r in rad)
    mean = math.degrees(math.atan2(sum_sin, sum_cos)) % 360.0
    r_bar = min(1.0, math.hypot(sum_sin, sum_cos) / len(rad))
    std = math.degrees(math.sqrt(-2.0 * math.log(r_bar))) if r_bar > 1e-9 else None
    return mean, std, r_bar


def angular_diff(a, b):
    """两个角度之间的最小圆周距离(0~180°)"""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


# ---------------------------------------------------------------- 采集
def parse_positions(text):
    """解析 --positions, 如 '0,90,180,270'"""
    positions = []
    for part in text.split(","):
        part = part.strip()
        if part:
            positions.append(float(part) % 360.0)
    if not positions:
        raise ValueError("--positions 不能为空")
    return positions


def run(args):
    positions = parse_positions(args.positions)
    if args.per_position < 1:
        raise SystemExit("--per-position 必须 >= 1")

    csv_path = args.csv or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        time.strftime("localization_%Y%m%d_%H%M%S.csv"))
    csv_file = open(csv_path, "w", newline="", encoding="utf-8")
    writer = csv.writer(csv_file)
    writer.writerow(["time", "expected_angle", "measured_angle",
                     "beam", "score", "start_ms"])
    csv_file.flush()

    try:
        mic = open_mic(args.port)
    except (SerialTimeoutError, OSError) as e:
        csv_file.close()
        raise SystemExit("[错误] %s" % e)

    print("=" * 78)
    print("M260C 环形六麦声源定位测试")
    print("串口: %s @115200    唤醒词: %s" % (mic.port, WAKE_WORD_TEXT))
    print("方位: %s" % ", ".join("%g°" % p for p in positions))
    print("每方位样本: %d 次" % args.per_position)
    print("说明: angle 以厂商协议手册定义的阵列 0° 为基准, 首次测试请先确认朝向")
    print("=" * 78)

    state = {"seen": {}, "last_ack": 0.0}
    groups = defaultdict(list)
    order = []

    try:
        for pos in positions:
            if pos not in order:
                order.append(pos)
            print("\n>>> 请站到 %g° 方向, 连续说唤醒词「%s」%d 次"
                  % (pos, WAKE_WORD_TEXT, args.per_position))
            got = 0
            while got < args.per_position:
                angles, beam, score, start_ms, obj = wait_wake(mic, state)
                if not angles:
                    print("    [跳过] 该唤醒事件没有 angle 字段: %s" % obj)
                    continue
                got += 1
                rec = {"angle": angles[0], "beam": beam, "score": score,
                       "start_ms": start_ms}
                groups[pos].append(rec)
                writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), "%g" % pos,
                                 angles[0],
                                 "" if beam is None else beam,
                                 "" if score is None else "%.4f" % score,
                                 "" if start_ms is None else start_ms])
                csv_file.flush()
                print("    [%d/%d] angle=%d°  beam=%s  score=%s"
                      % (got, args.per_position, angles[0],
                         "-" if beam is None else beam,
                         "-" if score is None else "%.3f" % score))
    except KeyboardInterrupt:
        print("\n[提示] 采集被中断, 将输出已采集样本的统计")
    except (OSError, SerialTimeoutError) as e:
        print("\n[错误] 串口异常: %s" % e)
    finally:
        mic.close()
        csv_file.close()

    print_stats(groups, order)
    print("\nCSV 已保存: %s" % csv_path)
    return 0


# ---------------------------------------------------------------- 报告
def print_stats(groups, order):
    if not any(groups.values()):
        print("\n没有采集到有效样本。")
        return

    print("\n" + "=" * 108)
    print("声源定位统计 (角度按圆周统计, 0°/360° 连续)")
    print("=" * 108)
    print("%6s %4s %7s %7s %7s %13s %9s %12s %9s"
          % ("期望", "样本", "均值", "圆周σ", "偏差", "范围", "±45°命中",
             "beam分布", "均值score"))
    print("-" * 108)

    total_n = total_hit = 0
    total_err = 0.0
    for pos in order:
        recs = groups.get(pos, [])
        if not recs:
            continue
        angles = [r["angle"] for r in recs]
        mean, std, _ = circular_stats(angles)
        err = angular_diff(mean, pos)
        hits = sum(1 for a in angles if angular_diff(a, pos) <= HIT_TOLERANCE)
        beams = Counter("?" if r["beam"] is None else str(r["beam"]) for r in recs)
        beam_txt = "/".join("%s:%d" % (b, c) for b, c in beams.most_common())
        scores = [r["score"] for r in recs if r["score"] is not None]
        score_txt = "%.2f" % (sum(scores) / len(scores)) if scores else "-"
        std_txt = "%.1f" % std if std is not None else "-"
        range_txt = "%d~%d" % (min(angles), max(angles))
        print("%6g %4d %7.1f %7s %7.1f %13s %6d/%-3d %12s %9s"
              % (pos, len(angles), mean, std_txt, err, range_txt,
                 hits, len(angles), beam_txt, score_txt))
        total_n += len(angles)
        total_hit += hits
        total_err += err * len(angles)

    print("-" * 108)
    if total_n:
        print("合计: %d 个样本, 平均绝对偏差 %.1f°, ±%g° 命中率 %.1f%%"
              % (total_n, total_err / total_n, HIT_TOLERANCE,
                 total_hit * 100.0 / total_n))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="M260C 环形六麦声源定位测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  %(prog)s\n"
               "  %(prog)s --positions 0,90,180,270 --per-position 10\n"
               "  %(prog)s --port /dev/ttyACM0 --csv result.csv")
    ap.add_argument("--port", help="降噪板串口(默认按 USB ID 1a86:55d4 自动识别)")
    ap.add_argument("--positions", default=DEFAULT_POSITIONS,
                    help="期望方位列表, 逗号分隔(默认 %s)" % DEFAULT_POSITIONS)
    ap.add_argument("--per-position", type=int, default=5,
                    help="每个方位采集的有效样本数(默认 5)")
    ap.add_argument("--csv", help="CSV 输出路径(默认写入本目录 localization_时间戳.csv)")
    raise SystemExit(run(ap.parse_args()))
