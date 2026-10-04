"""Windows DPAPI 加解密（ctypes，无第三方依赖）。

API Key 只以 CryptProtectData(CurrentUser) 的密文形式落库；
明文只在内存中短暂存在，不写日志、不写设置表。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

CRYPTPROTECT_UI_FORBIDDEN = 0x01

_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(DATA_BLOB),
    wt.LPCWSTR,
    ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wt.DWORD,
    ctypes.POINTER(DATA_BLOB),
]
_crypt32.CryptProtectData.restype = wt.BOOL
_crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(DATA_BLOB),
    ctypes.POINTER(wt.LPWSTR),
    ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wt.DWORD,
    ctypes.POINTER(DATA_BLOB),
]
_crypt32.CryptUnprotectData.restype = wt.BOOL
_kernel32.LocalFree.argtypes = [wt.HLOCAL]
_kernel32.LocalFree.restype = wt.HLOCAL


class DpapiError(RuntimeError):
    pass


def _blob_from_bytes(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def protect(plaintext: str, entropy: bytes = b"explorer-dict-v1") -> bytes:
    """加密为当前用户可解的密文。"""
    if plaintext is None:
        raise DpapiError("明文为 None")
    data = plaintext.encode("utf-8")
    in_blob, _keep = _blob_from_bytes(data)
    ent_blob, _keep2 = _blob_from_bytes(entropy) if entropy else (DATA_BLOB(), None)
    out_blob = DATA_BLOB()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "explorer-dict",
        ctypes.byref(ent_blob) if entropy else None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise DpapiError(f"CryptProtectData 失败: {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _kernel32.LocalFree(out_blob.pbData)


def unprotect(ciphertext: bytes, entropy: bytes = b"explorer-dict-v1") -> str:
    """解密。密文损坏或跨用户时抛 DpapiError。"""
    if not ciphertext:
        raise DpapiError("密文为空")
    in_blob, _keep = _blob_from_bytes(bytes(ciphertext))
    ent_blob, _keep2 = _blob_from_bytes(entropy) if entropy else (DATA_BLOB(), None)
    out_blob = DATA_BLOB()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(ent_blob) if entropy else None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise DpapiError(f"CryptUnprotectData 失败: {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
    finally:
        _kernel32.LocalFree(out_blob.pbData)
