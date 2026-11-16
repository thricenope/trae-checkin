#!/usr/bin/env python3
"""Trae CN 每日签到 —— 多账号、零第三方依赖。

账号来源（二选一）：
  1. 环境变量 TRAE_ACCOUNTS（GitHub Actions 用 Secret 注入）
  2. 同目录 accounts.json（本地调试用）

每个账号形如：
  {"name": "主账号", "token": "eyJ...", "device_id": "118...", "user_id": "153..."}

运行后会写 docs/status.json（不含任何 token），供网页看板读取。
"""

import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://api.trae.cn"
STATUS_URL = f"{BASE}/trae/api/v2/ug/checkin_credits/status"
CLAIM_URL = f"{BASE}/trae/api/v2/ug/checkin_credits/claim"
USAGE_URL = f"{BASE}/trae/api/v2/pay/ide_user_ent_usage"

# 非浏览器 UA：带浏览器 UA 会被前置 WAF 直接 403
UA = "Trae-Checkin/1.0"
TIMEOUT = 20
MAX_RETRIES = 3
# 服务端限流是 HTTP 200 + body 里的业务码，不是 429
RATE_LIMIT_CODES = {9074, 429}

ROOT = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.path.join(ROOT, "docs", "status.json")
TZ = timezone(timedelta(hours=8))


def log(msg):
    print(msg, flush=True)


_ctx = None


def ssl_context():
    """优先用系统 CA：本机开代理时 Python 自带证书链常校验失败，系统库里有代理根证书。"""
    global _ctx
    if _ctx is None:
        cafile = os.environ.get("SSL_CERT_FILE")
        for p in ("/etc/ssl/cert.pem", "/opt/homebrew/etc/ca-certificates/cert.pem"):
            if not cafile and os.path.exists(p):
                cafile = p
                break
        _ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
    return _ctx


def post(url, headers, body, op, max_retries=MAX_RETRIES):
    """带限流重试的 POST；401/403 直接失败，重试没有意义。"""
    data = json.dumps(body).encode("utf-8")
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl_context()) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
            if parsed.get("code") in RATE_LIMIT_CODES:
                last_err = f"限流 code={parsed.get('code')}"
                log(f"  [{op}] {last_err} ({attempt}/{max_retries})")
            else:
                return parsed
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {"code": e.code, "message": f"HTTP {e.code}，token 失效或已过期"}
            try:
                parsed = json.loads(e.read().decode("utf-8"))
                if parsed.get("code") not in RATE_LIMIT_CODES:
                    return parsed
                last_err = f"HTTP {e.code} code={parsed.get('code')}"
            except (ValueError, OSError):
                last_err = f"HTTP {e.code}"
            log(f"  [{op}] {last_err} ({attempt}/{max_retries})")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = str(e)
            log(f"  [{op}] 网络异常：{last_err} ({attempt}/{max_retries})")

        if attempt < max_retries:
            time.sleep(2 + random.uniform(0, 2))
    return {"code": -1, "message": f"重试 {max_retries} 次仍失败：{last_err}"}


def headers_for(acc):
    h = {
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Authorization": f"Cloud-IDE-JWT {acc['token']}",
    }
    if acc.get("device_id"):
        h["x-device-id"] = str(acc["device_id"])
    if acc.get("user_id"):
        h["x-user-id"] = str(acc["user_id"])
    if acc.get("region"):
        h["X-User-Region"] = str(acc["region"])
    return h


def get_balance(acc):
    """可用积分 = total_amount - consumed_amount；拿不到就返回 None，不编造。"""
    parsed = post(USAGE_URL, headers_for(acc), {"require_usage": True, "req_source": 1},
                  "balance", max_retries=2)
    summary = (parsed.get("usage_summary") or {})
    if not summary:
        return None
    try:
        return int(summary.get("total_amount", 0)) - int(summary.get("consumed_amount", 0))
    except (TypeError, ValueError):
        return None


def run_account(acc):
    name = acc.get("name") or acc.get("user_id") or "未命名"
    log(f"== {name} ==")
    result = {"name": name, "user_id": acc.get("user_id"), "ok": False,
              "checked_in": False, "credited": None, "balance": None, "message": ""}

    if not acc.get("token"):
        result["message"] = "缺少 token"
        return result

    status = post(STATUS_URL, headers_for(acc), {"req_source": 1}, "status")
    code = status.get("code")
    if code != 0:
        result["message"] = status.get("message") or f"查询失败 code={code}"
        log(f"  状态查询失败：{result['message']}")
        return result

    credits = status.get("credits")
    if status.get("checked_in"):
        result.update(ok=True, checked_in=True, credited=credits, message="今日已签到")
        log("  今日已签到，跳过")
    elif not status.get("enable", True):
        result["message"] = "当前不可签到（活动未开放或账号不满足条件）"
        log(f"  {result['message']}")
    else:
        claim = post(CLAIM_URL, headers_for(acc), {"req_source": 1}, "claim")
        if claim.get("code") == 0:
            result.update(ok=True, checked_in=True, credited=credits, message="签到成功")
            log("  签到成功")
        elif claim.get("code") in RATE_LIMIT_CODES:
            result["message"] = "风控/设备今日名额已用（code 9074）"
            log(f"  {result['message']}")
        else:
            result["message"] = claim.get("message") or f"签到失败 code={claim.get('code')}"
            log(f"  {result['message']}")

    result["balance"] = get_balance(acc)
    return result


def load_accounts():
    raw = os.environ.get("TRAE_ACCOUNTS", "").strip()
    if raw:
        data = json.loads(raw)
    else:
        path = os.path.join(ROOT, "accounts.json")
        if not os.path.exists(path):
            sys.exit("未找到账号：请设置 TRAE_ACCOUNTS 或创建 accounts.json（可运行 extract_creds.py 生成）")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    if isinstance(data, dict):
        data = data.get("accounts", [])
    return [a for a in data if isinstance(a, dict)]


def notify(results):
    """可选通知：飞书群机器人。失败不影响签到结果。"""
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        return
    failed = [r for r in results if not r["ok"]]
    title = "Trae 签到完成" if not failed else f"Trae 签到异常（{len(failed)} 项失败）"
    lines = [f"**{title}**"]
    for r in results:
        mark = "✅" if r["ok"] else "❌"
        line = f"{mark} {r['name']}：{r['message']}"
        if r.get("credited"):
            line += f"  +{r['credited']}"
        if r.get("balance") is not None:
            line += f"（余额 {r['balance']}）"
        lines.append(line)
    payload = {"msg_type": "text", "content": {"text": "\n".join(lines)}}
    try:
        req = urllib.request.Request(webhook, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # 通知失败不改写签到结果
        log(f"通知发送失败：{e}")


def main():
    accounts = load_accounts()
    log(f"共 {len(accounts)} 个账号，开始签到")
    results = [run_account(a) for a in accounts]
    notify(results)

    now = datetime.now(TZ)
    out = {
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "run_url": os.environ.get("RUN_URL", ""),
        "accounts": results,
    }
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    ok = sum(1 for r in results if r["ok"])
    log(f"完成：{ok}/{len(results)} 成功，结果已写入 docs/status.json")
    # 有失败时返回非 0，让 Actions 标红提醒
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
