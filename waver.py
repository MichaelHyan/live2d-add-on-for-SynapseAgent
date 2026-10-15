# -*- coding: utf-8 -*-
'''
音频包络生成 (供 Live2D 口型驱动)
==================================
用 librosa 读取合成语音 (output.mp3), 生成每 10ms 的音量包络,
写入 live2d_test.wav 后由渲染线程驱动 ParamMouthOpenY。

ffmpeg 说明:
    librosa 解码 mp3 可能依赖 ffmpeg 后端。
    本模块启动时从 config.json 读取 ffmpeg_path, 把其所在目录注入 PATH,
    解决 "缺少 ffmpeg" 的问题。
'''
import os
import json

# ---------------------------------------------------------------------------
# 注入 ffmpeg 到 PATH (供 librosa / audioread 使用)
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_FFMPEG_PATH = None
_AUDIO_FILE = 'output.mp3'

try:
    with open(os.path.join(BASE_DIR, 'config.json'), encoding='utf-8') as _f:
        _cfg = json.load(_f)
    _FFMPEG_PATH = _cfg.get('ffmpeg_path')
    _AUDIO_FILE = _cfg.get('tts', {}).get('output_path', 'output.mp3')
except Exception as _e:
    print('[waver] config.json 读取失败:', _e)


def _inject_ffmpeg():
    """把 ffmpeg 所在目录加入 PATH"""
    if not _FFMPEG_PATH:
        return
    exe = _FFMPEG_PATH
    if not os.path.isabs(exe):
        exe = os.path.normpath(os.path.join(BASE_DIR, exe))
    ff_dir = os.path.dirname(exe)
    if os.path.isdir(ff_dir):
        os.environ['PATH'] = ff_dir + os.pathsep + os.environ.get('PATH', '')
        print('[waver] 已注入 ffmpeg 目录:', ff_dir)
    else:
        print('[waver] ffmpeg 目录不存在:', ff_dir)


_inject_ffmpeg()

import numpy as np
import librosa


def _resolve(audio_file):
    if not audio_file:
        return os.path.join(BASE_DIR, _AUDIO_FILE)
    if os.path.isabs(audio_file):
        return audio_file
    return os.path.normpath(os.path.join(BASE_DIR, audio_file))


def get_wave(audio_file=None):
    try:
        audio_data, sample_rate = librosa.load(_resolve(audio_file), sr=None)
    except Exception as e:
        print('Error loading audio file: %s' % e)
        return []
    samples_per_10ms = int(sample_rate * 0.01)
    out = []
    for i in range(1, len(audio_data), samples_per_10ms):
        loudness = audio_data[i]
        out.append(loudness * 10)
    return out


def get_wave_max(audio_file=None):
    try:
        audio_data, sample_rate = librosa.load(_resolve(audio_file), sr=None)
    except Exception as e:
        print('Error loading audio file: %s' % e)
        return []
    samples_per_10ms = int(sample_rate * 0.01)
    out = []
    for i in range(0, len(audio_data) - samples_per_10ms + 1, samples_per_10ms):
        segment = audio_data[i:i + samples_per_10ms]
        max_loudness = np.max(np.abs(segment))
        out.append(max_loudness * 10)
    return out


if __name__ == '__main__':
    a = get_wave_max()
    print(a)
    print(len(a))