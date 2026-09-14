# -*- coding: utf-8 -*-
"""
bench_asr.py —— Vosk(small) vs SenseVoice 基准对比(同一批真实录音)

说明: audio/*.wav 是你实测时录下的真实音频, 但没有逐条人工标注,
      所以这里用"代理指标"做相对比较(不是绝对准确率):
        1) 命中命令词比例 : 文本里是否出现 打开/关闭/看/图片/照片
        2) 命令完整率     : 是否同时含 (打开|关闭|关掉|看) 且 (图片|照片)
        3) 无异常重复字   : 是否出现 3 个以上连续相同字符(如 关关关)
        4) 平均耗时       : 单条解码时间
注意: 录音里含唤醒词(预滚会带入), 所以"文本是否以命令字开头"没有意义, 故不采用.
用法: <lerobot-python> bench_asr.py [--limit N]
"""
import argparse
import glob
import json
import os
import re
import sys
import time
import wave

WAVS_DIR = "/home/kf/dev/voice_operate/audio"
GRAMMAR = ('["打 开 图 片", "打 开 照 片", "看 一 下 图 片", '
           '"关 闭 图 片", "关 掉 图 片", "[unk]"]')
HIT_KEYS = ("打开", "关闭", "看图", "图片", "照片", "关掉")
ACTION = re.compile(r"打开|关闭|关掉|看")
OBJECT = re.compile(r"图片|照片|图像")
DUP = re.compile(r"(.)\1{2,}")


def wav_files(limit=None):
    files = sorted(glob.glob(os.path.join(WAVS_DIR, "*.wav")))
    return files[-limit:] if limit else files


# ---------------------------------------------------------------- Vosk
def vosk_factory():
    from vosk import Model, KaldiRecognizer, SetLogLevel
    SetLogLevel(-1)
    model = Model("/home/kf/dev/voice_operate/models/vosk-model-small-cn-0.22")

    def run(path):
        rec = KaldiRecognizer(model, 16000, GRAMMAR)
        segs = []
        with wave.open(path, "rb") as w:
            while True:
                data = w.readframes(4000)
                if not data:
                    break
                if rec.AcceptWaveform(data):
                    t = json.loads(rec.Result()).get("text", "")
                    t = t.replace(" ", "").replace("[unk]", "")
                    if t:
                        segs.append(t)
        tail = json.loads(rec.FinalResult()).get("text", "")
        tail = tail.replace(" ", "").replace("[unk]", "")
        return "".join(segs) + tail
    return run


# ---------------------------------------------------------------- SenseVoice
def sv_factory():
    from funasr import AutoModel
    model = AutoModel(model="iic/SenseVoiceSmall", device="cpu",
                      disable_update=True, hub="ms", log_level="ERROR")

    def run(path):
        res = model.generate(input=path, cache={}, language="zh",
                             use_itn=True, batch_size_s=60)
        t = res[0]["text"] if res else ""
        t = re.sub(r"<\|[^|]*\|>", "", t)          # 去掉 <|zh|> <|NEUTRAL|> 等标记
        return re.sub(r"[，。！？、,.!?\s]", "", t)
    return run


def summarize(name, results, times):
    n = len(results)
    hit = sum(1 for t in results if any(k in t for k in HIT_KEYS))
    complete = sum(1 for t in results if ACTION.search(t) and OBJECT.search(t))
    nodup = sum(1 for t in results if t and not DUP.search(t))
    empty = sum(1 for t in results if not t)
    avg = sum(times) / max(1, len(times))
    print(f"{name:12s} 命中 {hit:3d}/{n} | 命令完整 {complete:3d}/{n} | "
          f"无重复字 {nodup:3d}/{n} | 空结果 {empty:3d}/{n} | 平均 {avg*1000:6.0f} ms")
    return dict(hit=hit, complete=complete, nodup=nodup, empty=empty, avg=avg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只用最近 N 条(默认全部)")
    ap.add_argument("--show", type=int, default=12, help="并排展示前 N 条")
    a = ap.parse_args()

    files = wav_files(a.limit or None)
    print(f"样本: {len(files)} 条真实录音\n")

    print("加载 Vosk …")
    vrun = vosk_factory()
    print("加载 SenseVoice(首次会从 ModelScope 下载模型)…")
    srun = sv_factory()

    vtext, stext, vtime, stime = [], [], [], []
    for i, f in enumerate(files, 1):
        t0 = time.time(); vt = vrun(f); dt_v = time.time() - t0
        t0 = time.time(); st = srun(f); dt_s = time.time() - t0
        vtext.append(vt); stext.append(st); vtime.append(dt_v); stime.append(dt_s)
        if i % 10 == 0:
            print(f"  ...已处理 {i}/{len(files)}")

    print("\n=== 汇总(代理指标, 同一批样本对比) ===")
    summarize("Vosk-small", vtext, vtime)
    summarize("SenseVoice", stext, stime)

    print(f"\n=== 并排样本(最近 {min(a.show, len(files))} 条) ===")
    for f, vt, st in list(zip(files, vtext, stext))[-a.show:]:
        print(f"{os.path.basename(f)[-15:]}  Vosk: {vt or '(空)':<16} | SV: {st or '(空)'}")


if __name__ == "__main__":
    main()
