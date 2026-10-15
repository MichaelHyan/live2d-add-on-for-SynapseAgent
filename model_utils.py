# -*- coding: utf-8 -*-
'''
模型信息解析工具
================
统一从 config.json / model3.json / live2d_window.json 解析:
  * 模型路径与模型名
  * 可用表情  (model3.json FileReferences.Expressions)
  * 可用动作  (model3.json FileReferences.Motions + live2d_window.json motion_file)
  * 是否已存在与当前模型匹配的表情预设 (expressions_custom.json)

供 live2d_preview.py / chat_gui.py / live2d_test.py 复用。

主要 API:
    get_model_path()    当前模型定义文件绝对路径
    get_model_name()    模型名 (取 model3.json 文件名, 如 MAOMAO)
    get_expressions()   可用表情名列表
    get_motions()       可用动作信息 dict
    get_model_info()    以上信息的汇总 dict
    has_preset()        是否存在匹配当前模型的预设, 返回 (bool, reason)
'''
import os
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAP_FILE = os.path.join(BASE_DIR, 'expressions_custom.json')

DEFAULT_MODEL = '.\\MAOMAO\\MAOMAO.model3.json'


def _load_json(path, default=None):
    try:
        with open(path, encoding='utf-8-sig') as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def _abspath(p):
    if not p:
        return p
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(BASE_DIR, p))


def get_model_path():
    """从 config.json 读取模型定义文件路径 (config 指定的模型)"""
    cfg = _load_json(os.path.join(BASE_DIR, 'config.json'), {})
    return _abspath(cfg.get('model_path', DEFAULT_MODEL))


def get_model_name():
    """模型名: 取模型文件名去掉扩展名 (如 MAOMAO.model3.json -> MAOMAO)"""
    mp = get_model_path()
    base = os.path.basename(mp or '')
    for suf in ('.model3.json', '.model.json', '.json'):
        if base.lower().endswith(suf):
            base = base[:-len(suf)]
            break
    return base or 'Live2D'


def get_expressions():
    """可用表情名列表 (model3.json 的 FileReferences.Expressions)"""
    mj = _load_json(get_model_path(), {})
    fr = mj.get('FileReferences', {}) or {}
    out = []
    for e in (fr.get('Expressions', []) or []):
        nm = str(e.get('Name') or '').strip()
        if nm:
            out.append(nm)
    return out


def get_motions():
    """
    可用动作信息:
        groups      : model3.json 注册的动作组名列表 (可能为空)
        motion_file : live2d_window.json 指定的动作文件 (自写播放器使用)
        model_dir   : 模型所在目录
    """
    mp = get_model_path()
    mj = _load_json(mp, {})
    fr = mj.get('FileReferences', {}) or {}
    motions = fr.get('Motions', {}) or {}
    groups = [str(k) for k in motions.keys()]
    wcfg = _load_json(os.path.join(BASE_DIR, 'live2d_window.json'), {})
    mfile = wcfg.get('motion_file')
    return {
        'groups': groups,
        'motion_file': mfile,
        'model_dir': os.path.dirname(mp),
    }


def get_model_info():
    """汇总当前模型信息"""
    return {
        'path': get_model_path(),
        'name': get_model_name(),
        'expressions': get_expressions(),
        'motions': get_motions(),
    }


def has_preset():
    """
    检查是否存在与当前模型匹配的表情预设。
    返回 (bool, reason)。
    """
    if not os.path.exists(MAP_FILE):
        return False, '未找到表情预设文件 expressions_custom.json'
    data = _load_json(MAP_FILE, {})
    mappings = data.get('mappings', []) or []
    if not mappings:
        return False, 'expressions_custom.json 中没有任何表情映射'
    model_field = str(data.get('model') or '').strip()
    cur = os.path.relpath(get_model_path(), BASE_DIR)
    if model_field:
        a = os.path.normpath(model_field.replace('/', os.sep)).lower()
        b = os.path.normpath(cur).lower()
        if a != b:
            return False, ('预设对应的模型 (%s) 与当前模型 (%s) 不一致'
                           % (model_field, cur))
    return True, '已检测到 %d 条表情映射' % len(mappings)


if __name__ == '__main__':
    info = get_model_info()
    print('model name :', info['name'])
    print('model path :', info['path'])
    print('expressions:', info['expressions'])
    print('motions    :', info['motions'])
    print('has_preset :', has_preset())