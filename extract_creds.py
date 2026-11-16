#!/usr/bin/env python3
"""从本机 Trae CN 客户端提取登录态，生成 accounts.json。

原理：客户端把登录态用 AES-128-CBC 加密后存在 storage.json，
密钥由 32 字节随机 key 经 SHA-512 派生（实现见客户端 byteCrypto.js）。
本脚本只读文件、不打印 token，解密结果写到本地 accounts.json。

用法：
  python3 extract_creds.py            # 生成 accounts.json，输出打码信息
  python3 extract_creds.py --json     # 直接打印完整 JSON（用于 gh secret set）
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys

# 客户端 byteCrypto.js 里的混淆常量（AES 变体）
JHE = bytes([82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
             124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
             84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78,
             8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37])
KHE = bytes([31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
             96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
             160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
             23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125])

HEADER_LEN, KEY_LEN, DIGEST_LEN, BLOCK = 6, 32, 64, 16
AES_HEADER = bytes([116, 99, 5, 16, 0, 0])  # "tc" + version

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(ROOT, "accounts.json")


def openssl():
    for p in (shutil.which("openssl"), "/usr/bin/openssl", "/opt/homebrew/bin/openssl"):
        if p and os.path.exists(p):
            return p
    sys.exit("未找到 openssl，无法解密。macOS/Linux 自带；Windows 请安装 Git for Windows 或改用 WSL。")


def derive_key(key: bytes):
    digest = hashlib.sha512(key).digest()
    mask = bytes(a ^ b for a, b in zip(JHE, KHE))
    d = hashlib.sha512(digest + mask).digest()
    return d[:16], d[16:32]


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    out = subprocess.run([openssl(), "enc", "-d", "-aes-128-cbc",
                          "-K", key.hex(), "-iv", iv.hex(), "-nopad"],
                         input=data, capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode(errors="replace").strip())
    return out.stdout


def strip_padding(buf: bytes) -> bytes:
    n = buf[-1] if buf else 0
    if 1 <= n <= BLOCK and buf.endswith(bytes([n]) * n):
        return buf[:-n]
    return buf


def decrypt_blob(value):
    raw = base64.b64decode(value)
    if raw[:HEADER_LEN] != AES_HEADER:
        raise ValueError("不是受支持的加密格式（客户端版本可能已变）")
    key = raw[HEADER_LEN:HEADER_LEN + KEY_LEN]
    aes_key, iv = derive_key(key)
    plain = strip_padding(aes_cbc_decrypt(aes_key, iv, raw[HEADER_LEN + KEY_LEN:]))
    if hashlib.sha512(plain[DIGEST_LEN:]).digest() != plain[:DIGEST_LEN]:
        raise ValueError("校验失败，解密内容不完整")
    return plain[DIGEST_LEN:]


def app_dirs():
    home = os.path.expanduser("~")
    cands = [
        os.path.join(home, "Library/Application Support/Trae CN"),      # macOS
        os.path.join(os.environ.get("APPDATA", ""), "Trae CN"),          # Windows
        os.path.join(home, ".config/Trae CN"),                           # Linux
    ]
    return [c for c in cands if c and os.path.isdir(c)]


def device_id_of(app_dir):
    path = os.path.join(app_dir, "aha", "TinyStorage")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f).get("tiny_storage_data", {})
    value = data.get("aha.device.device_id")
    if not value:
        return None
    info = json.loads(decrypt_blob(value))
    return info.get("device_id_str") or info.get("uuid")


def mask(s, keep=6):
    s = str(s or "")
    return s[:keep] + "..." + s[-4:] if len(s) > keep + 6 else s


def main():
    want_json = "--json" in sys.argv
    dirs = app_dirs()
    if not dirs:
        sys.exit("未找到 Trae CN 数据目录，请确认客户端已安装并登录过。")

    app_dir = dirs[0]
    storage = os.path.join(app_dir, "User/globalStorage/storage.json")
    if not os.path.exists(storage):
        sys.exit(f"未找到登录态文件：{storage}")

    with open(storage, encoding="utf-8") as f:
        store = json.load(f)

    device_id = device_id_of(app_dir)
    if not device_id:
        sys.exit("未找到设备号（aha/TinyStorage），签到必须带真实设备号。")

    accounts, seen = [], set()
    for key, value in store.items():
        if not key.startswith("iCubeAuthInfo://"):
            continue
        if key.endswith("/usertag"):
            continue
        try:
            info = json.loads(decrypt_blob(value))
        except Exception:
            continue
        user_id = str(info.get("userId") or "")
        token = info.get("token")
        if not token or user_id in seen:
            continue
        seen.add(user_id)
        account = info.get("account") or {}
        accounts.append({
            "name": account.get("username") or f"账号{user_id[-4:]}" if user_id else "账号",
            "token": token,
            "device_id": device_id,
            "user_id": user_id,
            "region": (info.get("userRegion") or {}).get("region", "CN"),
        })

    if not accounts:
        sys.exit("没有解出任何账号，请确认客户端已登录。")

    # 合并历史账号：客户端同一时间只存一个登录态，切号后重跑不会丢掉之前的号
    old = []
    if os.path.exists(OUT_FILE):
        try:
            with open(OUT_FILE, encoding="utf-8") as f:
                old = [a for a in json.load(f) if isinstance(a, dict)]
        except (ValueError, OSError):
            old = []
    merged = {(a.get("user_id") or a.get("name")): a for a in old}
    added = len([a for a in accounts if a["user_id"] not in merged])
    for a in accounts:
        merged[a["user_id"]] = a
    accounts = list(merged.values())

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)

    if want_json:
        print(json.dumps(accounts, ensure_ascii=False))
        return

    print(f"已写入 {OUT_FILE}（共 {len(accounts)} 个账号，本次新增 {added} 个）\n")
    for a in accounts:
        print(f"  名称：{a['name']}")
        print(f"  用户：{mask(a['user_id'])}")
        print(f"  设备：{a['device_id']}")
        print(f"  Token：{mask(a['token'], 8)}（{len(a['token'])} 字符）\n")
    print("下一步：把 accounts.json 的内容整体写入仓库 Secret，名称为 TRAE_ACCOUNTS")
    print("（README 里有 GitHub 网页端和 gh 命令行两种加法）")


if __name__ == "__main__":
    main()
