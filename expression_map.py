# -*- coding: utf-8 -*-
'''
表情映射共享模块
=================
统一加载 expressions_custom.json (由 live2d_preview.py 生成),
向 live2d_test.py / chat_gui.py 提供查询与应用能力。

映射结构 (expressions_custom.json):
{
  "model": "MAOMAO/MAOMAO.model3.json",
  "mappings": [
    {
      "name": "love",            # 唯一名 (程序内部用)
      "alias": "爱心",            # 中文别名
      "hotkey": "1",             # 快捷键 (单个字符: 1~9 / a~z / f1~f12)
      "triggers": ["爱你", "喜欢"], # 聊天触发词
      "params": {"Param11": 1}    # 参数组合 {参数ID: 值}
    }
  ]
}

主要 API:
    reload()              重新从文件加载
    all_mappings()        返回全部映射
    get_by_name(name)     按名查询
    get_by_hotkey(key)    按快捷键查询
    match_trigger(text)   按触发词匹配, 返回第一个命中的映射
    params_of(mapping)    取参数字典
'''
import os
import json
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAP_FILE = os.path.join(BASE_DIR, 'expressions_custom.json')

_lock = threading.RLock()
_mappings = []
_loaded = False


def _normalize_hotkey(k):
    if k is None:
        return ''
    return str(k).strip().lower()


def reload():
    global _mappings, _loaded
    data = {}
    try:
        if os.path.exists(MAP_FILE):
            with open(MAP_FILE, encoding='utf-8') as f:
                data = json.load(f)
    except Exception as e:
        print('[expr_map] 加载失败:', e)
        data = {}
    mappings = data.get('mappings', []) or []
    clean = []
    for m in mappings:
        if not isinstance(m, dict):
            continue
        clean.append({
            'name': str(m.get('name') or ''),
            'alias': str(m.get('alias') or ''),
            'hotkey': _normalize_hotkey(m.get('hotkey')),
            'triggers': [str(t).strip() for t in (m.get('triggers') or [])
                         if str(t).strip()],
            'params': {str(k): float(v)
                       for k, v in (m.get('params') or {}).items()},
        })
    with _lock:
        _mappings = clean
        _loaded = True
    return clean


def _ensure():
    if not _loaded:
        reload()


def all_mappings():
    _ensure()
    with _lock:
        return list(_mappings)


def get_by_name(name):
    if not name:
        return None
    _ensure()
    name = str(name).strip().lower()
    with _lock:
        for m in _mappings:
            if m['name'].lower() == name:
                return m
    return None


def get_by_hotkey(key):
    key = _normalize_hotkey(key)
    if not key:
        return None
    _ensure()
    with _lock:
        for m in _mappings:
            if m['hotkey'] == key:
                return m
    return None


def match_trigger(text):
    if not text:
        return None
    _ensure()
    with _lock:
        cands = []
        for m in _mappings:
            for t in m['triggers']:
                if t and t in text:
                    cands.append((len(t), m))
        if not cands:
            return None
        cands.sort(key=lambda x: -x[0])
        return cands[0][1]


def params_of(mapping):
    if not mapping:
        return {}
    return dict(mapping.get('params') or {})


def pretty(mapping):
    if not mapping:
        return ''
    return '%s(%s) [键:%s] 触发:%s' % (
        mapping.get('alias') or mapping.get('name'),
        mapping.get('name'),
        mapping.get('hotkey') or '-',
        '/'.join(mapping.get('triggers') or []) or '-')


# 默认映射(当 expressions_custom.json 不存在时, 用内置 8 表情兜底)
DEFAULT_MAPPINGS = [
    {"name": "love", "alias": "爱心", "hotkey": "1",
     "triggers": ["爱你", "喜欢", "么么"], "params": {"Param11": 1}},
    {"name": "sweat", "alias": "流汗", "hotkey": "2",
     "triggers": ["流汗", "热"], "params": {"Param13": 1}},
    {"name": "angry", "alias": "生气气", "hotkey": "3",
     "triggers": ["生气", "哼", "讨厌"], "params": {"Param10": 1}},
    {"name": "dizzy", "alias": "蚊香", "hotkey": "4",
     "triggers": ["晕", "不懂"], "params": {"Param12": 1}},
    {"name": "ear_fold", "alias": "折耳", "hotkey": "5",
     "triggers": ["折耳"], "params": {"Param75": -1}},
    {"name": "star", "alias": "星星", "hotkey": "6",
     "triggers": ["星星", "厉害"], "params": {"Param9": 1}},
    {"name": "tongue", "alias": "舌头", "hotkey": "7",
     "triggers": ["舌头", "略略略"], "params": {"Paramshetou": 1}},
    {"name": "blush", "alias": "脸红", "hotkey": "8",
     "triggers": ["脸红", "害羞"], "params": {"Paramtouface1": 1}},
]


def ensure_default_file():
    """若不存在 expressions_custom.json, 用默认映射生成一份 (不覆盖已有)"""
    if os.path.exists(MAP_FILE):
        return False
    try:
        import json as _j
        with open(MAP_FILE, 'w', encoding='utf-8') as f:
            _j.dump({'model': '', 'mappings': DEFAULT_MAPPINGS},
                    f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print('[expr_map] 生成默认映射失败:', e)
        return False