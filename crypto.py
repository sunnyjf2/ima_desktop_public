# -*- coding: utf-8 -*-
"""敏感凭据的本地加密存储。

Windows 下使用 DPAPI（CryptProtectData / CryptUnprotectData），
凭据随当前用户账户加密，不随文件泄露；非 Windows 平台退化为 base64（仅混淆）。
"""
import ctypes
import base64
import os

_DPAPI_OK = False
try:
    _crypt32 = ctypes.windll.crypt32  # type: ignore
    _kernel32 = ctypes.windll.kernel32  # type: ignore

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32),
                    ("pbData", ctypes.POINTER(ctypes.c_byte))]

    def _blob(buf):
        cb = ctypes.create_string_buffer(buf, len(buf))
        return _DATA_BLOB(len(buf), ctypes.cast(cb, ctypes.POINTER(ctypes.c_byte)))

    def _call(fn, data: bytes) -> bytes:
        inp = _blob(data)
        out = _DATA_BLOB()
        ok = fn(0, ctypes.byref(inp), None, None, None, 0, ctypes.byref(out))
        if not ok:
            raise ctypes.WinError()
        res = ctypes.string_at(out.pbData, out.cbData)
        _kernel32.LocalFree(out.pbData)
        return res

    def protect(data: bytes) -> bytes:
        return _call(_crypt32.CryptProtectData, data)

    def unprotect(data: bytes) -> bytes:
        return _call(_crypt32.CryptUnprotectData, data)

    _DPAPI_OK = True
except Exception:
    _DPAPI_OK = False


def encrypt(plain: str) -> str:
    raw = plain.encode("utf-8")
    if _DPAPI_OK:
        try:
            return "dpapi:" + base64.b64encode(protect(raw)).decode("ascii")
        except Exception:
            pass
    return "b64:" + base64.b64encode(raw).decode("ascii")


def decrypt(token: str) -> str:
    if not token:
        return ""
    if token.startswith("dpapi:"):
        if _DPAPI_OK:
            try:
                raw = base64.b64decode(token[len("dpapi:"):])
                return unprotect(raw).decode("utf-8")
            except Exception:
                pass
        # dpapi 不可用或解密失败，回退尝试 b64（容错）
        try:
            return base64.b64decode(token[len("dpapi:"):]).decode("utf-8")
        except Exception:
            return ""
    if token.startswith("b64:"):
        return base64.b64decode(token[len("b64:"):]).decode("utf-8")
    # 旧明文兼容
    return token


def is_secure() -> bool:
    return _DPAPI_OK


if __name__ == "__main__":
    t = encrypt("hello 世界")
    print("secure:", is_secure(), "roundtrip:", decrypt(t))
