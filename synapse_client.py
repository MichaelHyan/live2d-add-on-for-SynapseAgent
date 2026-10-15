# -*- coding: utf-8 -*-
'''
SynapseAgent HTTP 客户端
========================
封装 http://127.0.0.1:56700/cnmd 的 ping / ask / answer 调用。

接口协议 (见 README_for_addon.md):
  ping   -> {"status": 0, "response": ""}
  ask    -> {"status": 1, "response": ""}
  answer -> {"status": 0/1, "response": "文本"}
     type=rp : 拉取一条回复 (会更新消息队列)
     type=rl : 拉取全部记录 (不改队列)

  status: 0 表示 Agent 不在回答循环中(闲置), 1 表示仍在作答/有未读信息

用法:
    import synapse_client
    c = synapse_client.SynapseClient()
    c.set_prompt('#bot prompt PROMPTOVERWRITE <提示词全文>')       # 覆写人设
    c.ask('你好')                          # 发送消息
    status, resp = c.answer_rp()           # 拉取回复
    text = synapse_client.strip_tool_call(resp)   # 去掉指令标签
'''
import json
import re
import threading

import requests

DEFAULT_URL = 'http://127.0.0.1:56700/cnmd'
DEFAULT_HEADERS = {'Content-Type': 'application/json; charset=utf-8'}
DEFAULT_TIMEOUT = 15

# 用拼接方式构造标签正则, 避免源码中出现字面量标签
_TAG_OPEN = '<' + 'tool_call>'
_TAG_CLOSE = '<' + '/tool_call>'
_TOOL_CALL_RE = re.compile(re.escape(_TAG_OPEN) + r'(.*?)' +
                           re.escape(_TAG_CLOSE), re.DOTALL)


def strip_tool_call(text):
    """去掉指令标签, 返回纯文本"""
    if not text:
        return ''
    return _TOOL_CALL_RE.sub('', text).strip()


def parse_tool_calls(text):
    """提取全部指令标签中的内容"""
    if not text:
        return []
    return [m.group(1).strip() for m in _TOOL_CALL_RE.finditer(text)]


class SynapseClient:
    def __init__(self, url=DEFAULT_URL, timeout=DEFAULT_TIMEOUT):
        self.url = url
        self.timeout = timeout
        self._lock = threading.Lock()

    def _post(self, body):
        with self._lock:
            resp = requests.post(
                self.url,
                data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                headers=DEFAULT_HEADERS,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            try:
                return resp.json()
            except ValueError:
                return resp.text

    @staticmethod
    def _parse(reply):
        """兼容 {status, response} 与 {ok, result:{response}} 两种返回结构"""
        status, response = 0, ''
        if isinstance(reply, dict):
            if 'response' in reply:
                status = reply.get('status', 0)
                response = reply.get('response', '') or ''
            elif isinstance(reply.get('result'), dict):
                status = 0 if reply.get('ok') else 1
                response = reply['result'].get('response', '') or ''
        elif isinstance(reply, str):
            response = reply
        return status, response

    def ping(self):
        """连通性测试"""
        return self._post({'method': 'ping'})

    def ask(self, content):
        """向 Agent 发送消息"""
        return self._post({'method': 'ask', 'params': {'content': content}})

    def answer_rp(self):
        """拉取一条回复并更新队列, 返回 (status, response)"""
        return self._parse(
            self._post({'method': 'answer', 'params': {'type': 'rp'}}))

    def answer_rl(self):
        """拉取全部记录(不改队列), 返回 (status, response)"""
        return self._parse(
            self._post({'method': 'answer', 'params': {'type': 'rl'}}))

    def set_prompt(self, prompt):
        """发送提示词覆写指令: '#bot prompt PROMPTOVERWRITE <提示词全文>'"""
        return self.ask(prompt)