# -*- coding: utf-8 -*-
"""
bench_asr.py —— Vosk(small) / SenseVoice / Fun-ASR-Nano 基准对比(同一批真实录音)

说明: audio/*.wav 是你实测时录下的真实音频, 但没有逐条人工标注,
      所以这里用"代理指标"做相对比较(不是绝对准确率):
        1) 命中命令词比例 : 文本里是否出现 打开/关闭/看/图片/照片
        2) 命令完整率     : 是否同时含 (打开|关闭|关掉|看) 且 (图片|照片)
        3) 无异常重复字   : 是否出现 3 个以上连续相同字符(如 关关关)
        4) 平均耗时       : 单条解码时间
注意: 录音里含唤醒词(预滚会带入), 所以"文本是否以命令字开头"没有意义, 故不采用.
用法: <lerobot-python> bench_asr.py [--limit N] [--engines vosk,sv,nano]
      nano = Fun-ASR-Nano-2512(LLM-ASR, ~800M), 首次自动从 ModelScope 下载, GPU 推理
"""
import argparse
import glob
import json
import os
import re
import sys
import time
import wave

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
WAVS_DIR = os.path.join(PROJECT_DIR, "audio")
VOSK_MODEL_DIR = os.path.join(PROJECT_DIR, "models", "vosk-model-small-cn-0.22")
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
    model = Model(VOSK_MODEL_DIR)

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


# ------------------------------------------------------- Fun-ASR-Nano
def nano_factory():
    from funasr import AutoModel
    model = AutoModel(model="FunAudioLLM/Fun-ASR-Nano-2512",
                      trust_remote_code=True, remote_code="./model.py",
                      device="cuda:0", hub="ms", disable_update=True,
                      log_level="ERROR")

    def run(path):
        res = model.generate(input=[path], cache={}, batch_size=1,
                             language="中文", itn=True)
        t = res[0]["text"] if res else ""
        return re.sub(r"[，。！？、,.!?\s]", "", t)   # LLM 直接出标点, 无需去标签
    return run


ENGINES = {"vosk": ("Vosk-small", vosk_factory),
           "sv": ("SenseVoice", sv_factory),
           "nano": ("FunASR-Nano", nano_factory)}


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
    ap.add_argument("--engines", default="vosk,sv",
                    help="逗号分隔: vosk,sv,nano(默认 vosk,sv; nano 首次下载约2GB, 走GPU)")
    a = ap.parse_args()

    files = wav_files(a.limit or None)
    print(f"样本: {len(files)} 条真实录音\n")

    selected = [e.strip() for e in a.engines.split(",") if e.strip()]
    for e in selected:
        if e not in ENGINES:
            raise SystemExit(f"未知引擎: {e} (可选: {','.join(ENGINES)})")

    texts, times = {}, {}
    for key in selected:
        name, factory = ENGINES[key]
        print(f"加载 {name} …")
        run = factory()
        r_texts, r_times = [], []
        for i, f in enumerate(files, 1):
            t0 = time.time(); txt = run(f); dt = time.time() - t0
            r_texts.append(txt); r_times.append(dt)
            if i % 10 == 0:
                print(f"  ...{name} 已处理 {i}/{len(files)}")
        texts[name], times[name] = r_texts, r_times

    print("\n=== 汇总(代理指标, 同一批样本对比) ===")
    for key in selected:
        name = ENGINES[key][0]
        summarize(name, texts[name], times[name])

    print(f"\n=== 并排样本(最近 {min(a.show, len(files))} 条) ===")
    names = [ENGINES[k][0] for k in selected]
    for idx in range(max(0, len(files) - a.show), len(files)):
        row = " | ".join(f"{n}: {texts[n][idx] or '(空)'}" for n in names)
        print(f"{os.path.basename(files[idx])[-15:]}  {row}")


if __name__ == "__main__":
    main()
