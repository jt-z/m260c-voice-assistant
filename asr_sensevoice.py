# -*- coding: utf-8 -*-
"""
asr_sensevoice.py —— SenseVoice-Small 离线识别封装(FunASR + ModelScope)

用途: 作为"最终解码"引擎 —— Vosk 负责界面实时逐字, 本引擎给出干净、完整的最终文本。
模型: iic/SenseVoiceSmall (首次自动从 ModelScope 下载, 约 936MB, 缓存在 ~/.cache/modelscope)
依赖: funasr(已在 lerobot 环境安装), CPU 推理; 3~4s 音频约 0.1~0.2s(RTF≈0.03)

用法:
    from asr_sensevoice import SenseVoice
    sv = SenseVoice()               # 首次加载约 5~6s(读 936MB 权重), 之后每次识别 0.1~0.25s
    text = sv.decode("a.wav")       # 去掉 <|zh|> 等标记与标点, 返回纯文本
"""
import logging
import re

_TAG = re.compile(r"<\|[^|]*\|>")          # <|zh|> <|NEUTRAL|> <|Speech|> 等标记
_PUNC = re.compile(r"[，。！？、,.!?\s]")


def _quiet_logs():
    """压掉 funasr/modelscope 的 INFO 噪音(它们导入时会重置日志级别, 故需导入后再压)"""
    logging.disable(logging.INFO)          # 屏蔽 INFO 及以下; 本模块输出用 print, 不受影响
    for name in ("funasr", "modelscope", "modelscope_hub"):
        logging.getLogger(name).setLevel(logging.ERROR)


class SenseVoice:
    def __init__(self, model="iic/SenseVoiceSmall", device="cpu", hub="ms"):
        from funasr import AutoModel
        _quiet_logs()
        self.model = AutoModel(model=model, device=device, disable_update=True,
                               disable_pbar=True, hub=hub, log_level="ERROR")

    def decode(self, wav_path: str) -> str:
        """识别 16k 单声道 WAV, 返回去掉标记/标点的纯中文文本"""
        res = self.model.generate(input=wav_path, cache={}, language="zh",
                                  use_itn=True, batch_size_s=60)
        text = res[0]["text"] if res else ""
        return _PUNC.sub("", _TAG.sub("", text))
