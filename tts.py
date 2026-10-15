# -*- coding: utf-8 -*-
'''
语音合成 (双后端自动回退)
==========================
后端 1: edge-tts 联网合成 (音色与旧项目一致, 优先)
    - 强制 IPv4 (规避 IPv6 连接失败 WinError 64)
    - 带重试 (默认 3 次)
后端 2: 本机离线语音 Microsoft SAPI (网络不可用时自动回退)
    - 通过 pywin32 调用, 音色选中文 (如 Microsoft Huihui Desktop)
    - 输出 WAV, 不依赖网络, 永不失败

音色预设 (voice_offset, 仅 edge-tts 生效):
    0 童声    zh-CN-YunxiaNeural
    1 男声低  zh-CN-YunyangNeural
    2 男声高  zh-CN-YunxiNeural
    3 女声低  zh-CN-XiaoxiaoNeural
    4 女声高  zh-CN-XiaoyiNeural     <- 旧项目默认

用法:
    import tts
    path = tts.bake('你好呀喵', path='output.mp3', offset=4)
    # 返回实际生成的文件路径 (可能是 .mp3 或回退后的 .wav)
'''
import os
import socket
import asyncio
import time

try:
    import edge_tts
except Exception as e:  # pragma: no cover
    edge_tts = None
    print('[tts] edge_tts 不可用:', e)

try:
    import win32com.client
except Exception:
    win32com = None

VOICE_OFFSETS = [
    'zh-CN-YunxiaNeural',
    'zh-CN-YunyangNeural',
    'zh-CN-YunxiNeural',
    'zh-CN-XiaoxiaoNeural',
    'zh-CN-XiaoyiNeural',
]

# 记录强制 IPv4 前的原始 getaddrinfo
_ORIG_GETADDRINFO = socket.getaddrinfo

EDGE_RETRIES = 3


def _ipv4_first(host, port, family=0, type=0, proto=0, flags=0):
    """强制解析为 IPv4, 规避 IPv6 连接失败"""
    return _ORIG_GETADDRINFO(host, port, socket.AF_INET, type, proto, flags)


# ---------------------------------------------------------------------------
# 后端 1: edge-tts
# ---------------------------------------------------------------------------
async def _edge_async(text, path, offset):
    voice = VOICE_OFFSETS[offset % len(VOICE_OFFSETS)]
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(path)


def _bake_edge(text, path, offset, retries=EDGE_RETRIES):
    """联网合成; 成功返回 True, 失败返回 False"""
    if edge_tts is None:
        return False
    socket.getaddrinfo = _ipv4_first
    try:
        for i in range(retries):
            try:
                asyncio.run(_edge_async(text, path, offset))
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    return True
            except Exception as e:
                print('[tts] edge-tts 第%d次失败: %s' % (i + 1, e))
                time.sleep(0.8)
        return False
    finally:
        socket.getaddrinfo = _ORIG_GETADDRINFO


# ---------------------------------------------------------------------------
# 后端 2: 本机离线 SAPI
# ---------------------------------------------------------------------------
def _pick_chinese_voice(sp):
    """挑选中文语音 (优先含 Chinese/Huihui)"""
    try:
        voices = sp.GetVoices()
        for i in range(voices.Count):
            desc = voices.Item(i).GetDescription()
            if 'Chinese' in desc or 'Huihui' in desc or '中文' in desc:
                return i
    except Exception:
        pass
    return 0


def _bake_sapi(text, path):
    """离线合成, 输出 WAV (把 .mp3 换成 .wav); 成功返回 wav 路径"""
    if win32com is None:
        raise RuntimeError('pywin32 未安装, 且 edge-tts 不可用, 无法合成语音')
    wav_path = os.path.splitext(path)[0] + '.wav'
    if os.path.exists(wav_path):
        try:
            os.remove(wav_path)
        except Exception:
            pass
    sp = win32com.client.Dispatch('SAPI.SpVoice')
    idx = _pick_chinese_voice(sp)
    try:
        sp.Voice = sp.GetVoices().Item(idx)
    except Exception:
        pass
    stream = win32com.client.Dispatch('SAPI.SpFileStream')
    # SSFMCreateForWrite = 3
    stream.Open(wav_path, 3, False)
    sp.AudioOutputStream = stream
    sp.Speak(text)
    stream.Close()
    print('[tts] 已使用离线语音合成:', os.path.basename(wav_path))
    return wav_path


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def bake(text, path='output.mp3', offset=4):
    """
    合成语音: 优先 edge-tts (联网), 失败自动回退本机离线语音。
    返回实际生成的文件路径 (.mp3 或 .wav)。
    """
    if not text:
        raise ValueError('文本为空')
    if _bake_edge(text, path, offset):
        return path
    print('[tts] 联网合成失败, 回退到本机离线语音')
    return _bake_sapi(text, path)