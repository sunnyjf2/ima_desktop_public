# -*- coding: utf-8 -*-
"""全局配置：凭据、默认知识库、路径。敏感字段经 crypto 加密后落盘。"""
import os
import json

from crypto import encrypt, decrypt

import sys

if getattr(sys, "frozen", False):
    # 冻结态：数据写在 exe 同级（可写、可持久化，重打包不丢）
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DB_PATH = os.path.join(DATA_DIR, "memory.db")
SKILLS_DIR = os.path.join(DATA_DIR, "skills")
MEMORY_DIR = os.path.join(DATA_DIR, "memory")      # 文件化记忆目录（.md）
LOCAL_LIB_DIR = os.path.join(DATA_DIR, "local_libs")  # 本地知识库默认根
os.makedirs(SKILLS_DIR, exist_ok=True)
os.makedirs(MEMORY_DIR, exist_ok=True)
os.makedirs(LOCAL_LIB_DIR, exist_ok=True)

DEFAULTS = {
    "openapi_clientid": "",
    "openapi_apikey": "",
    "cookie_xima_cookie": "",
    "cookie_xima_bkn": "",
    "default_kb_id": "",
    "download_dir": os.path.join(DATA_DIR, "downloads"),
    "memory_file": "",            # 当前使用的记忆文件名
    "model_id": "official_3",     # 当前对话模型
    "model_type": 3,
    "model_label": "DeepSeek-V4-Flash / 快速",
    "local_libs": [],             # 本地知识库目录列表
}

_PLAIN_KEYS = ("default_kb_id", "download_dir", "memory_file",
               "model_id", "model_type", "model_label", "local_libs")


def _deep_decrypt(v, max_rounds=8):
    """循环解密，兼容历史上被重复加密的配置。

    修复背景：旧版 save() 会对内存中已经是密文的值再次 encrypt()，
    而 load() 未把密文还原，导致每保存一次配置（切模型/加本地库等）
    密文就多包一层，最终 get() 只解一层 → 拿到无效凭据。
    这里不断脱壳，直到不再是 b64:/dpapi: 包装。
    """
    n = 0
    while n < max_rounds and isinstance(v, str) and (
            v.startswith("b64:") or v.startswith("dpapi:")):
        nv = decrypt(v)
        if not nv or nv == v:
            break
        v = nv
        n += 1
    return v


class Config:
    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        """读盘：非明文键在内存中一律保存【明文】，避免 save() 重复加密。"""
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                for k, v in raw.items():
                    if k in _PLAIN_KEYS or not isinstance(v, str):
                        self.data[k] = v
                    else:
                        self.data[k] = _deep_decrypt(v)
            except Exception:
                pass

    def save(self):
        out = {}
        for k, v in self.data.items():
            if k in _PLAIN_KEYS:
                out[k] = v
            elif isinstance(v, str) and v:
                out[k] = encrypt(v)
            else:
                out[k] = ""
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)

    # --- 通用 getter/setter ---
    def get(self, key, default=""):
        v = self.data.get(key, default)
        if key in _PLAIN_KEYS:
            return v
        if not isinstance(v, str):
            return ""
        # load() 已还原为明文；此处兜底处理仍是密文的情况
        if v.startswith("b64:") or v.startswith("dpapi:"):
            return _deep_decrypt(v)
        return v

    def set(self, key, value):
        # 防御：绝不把密文写回内存，否则 save() 会再包一层
        if (key not in _PLAIN_KEYS and isinstance(value, str)
                and (value.startswith("b64:") or value.startswith("dpapi:"))):
            value = _deep_decrypt(value)
        self.data[key] = value if value is not None else ""
        self.save()

    # --- 便捷属性 ---
    def openapi(self):
        return self.get("openapi_clientid"), self.get("openapi_apikey")

    def has_openapi(self):
        a, b = self.openapi()
        return bool(a and b)

    def cookie(self):
        return self.get("cookie_xima_cookie"), self.get("cookie_xima_bkn")

    def has_cookie(self):
        a, b = self.cookie()
        return bool(a and b)

    def download_dir(self):
        d = self.get("download_dir") or DEFAULTS["download_dir"]
        os.makedirs(d, exist_ok=True)
        return d

    def set_download_dir(self, d):
        self.set("download_dir", d)

    # --- 记忆文件 ---
    def memory_file(self):
        return self.get("memory_file") or ""

    def set_memory_file(self, name):
        self.set("memory_file", name or "")

    # --- 对话模型 ---
    def model(self):
        return (self.get("model_id") or "official_3",
                self.get("model_type") if self.get("model_type") != "" else 3,
                self.get("model_label") or "")

    def set_model(self, model_id, model_type, label=""):
        self.data["model_id"] = model_id or ""
        self.data["model_type"] = model_type
        self.data["model_label"] = label or ""
        self.save()

    # --- 本地知识库目录 ---
    def local_libs(self):
        v = self.get("local_libs")
        return v if isinstance(v, list) else []

    def add_local_lib(self, path):
        libs = self.local_libs()
        if path and path not in libs:
            libs.append(path)
            self.data["local_libs"] = libs
            self.save()

    def remove_local_lib(self, path):
        libs = [p for p in self.local_libs() if p != path]
        self.data["local_libs"] = libs
        self.save()


_config = None


def get_config():
    global _config
    if _config is None:
        _config = Config()
    return _config


if __name__ == "__main__":
    c = get_config()
    c.set("cookie_xima_bkn", "516827693")
    print("bkn:", c.get("cookie_xima_bkn"))
    print("openapi:", c.openapi())
