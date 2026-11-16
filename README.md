# Trae CN 每日签到（多账号）

每天北京时间 08:10 由 GitHub Actions 自动执行 Trae CN 签到，结果写进 `docs/status.json`，
用 GitHub Pages 出一个手机可看的看板。不需要开电脑，不需要服务器。

```
extract_creds.py   从本机 Trae 客户端读登录态 → accounts.json（只在本机跑）
checkin.py         多账号签到 → docs/status.json（Actions 里跑）
docs/index.html    看板页面（GitHub Pages）
```

## 一、拿到账号凭据（在本机跑）

```bash
python3 extract_creds.py
```

脚本只读本机 `~/Library/Application Support/Trae CN/` 下的登录态文件，解密后写入
`accounts.json`，终端里只显示打码信息，不会打印 token。

多账号：把每个账号的 token 和设备号整理成数组即可（同一个客户端里切换账号登录，
再跑一次脚本，把新账号的对象追加进去）：

```json
[
  {"name": "主账号", "token": "eyJ...", "device_id": "1180223269519258", "user_id": "153..."},
  {"name": "小号",   "token": "eyJ...", "device_id": "1180223269519259", "user_id": "162..."}
]
```

> `device_id` 必须是该账号客户端里的真实设备号，随便编一个会返回 `9074`。
> 同一个设备号下的多个账号，可能只有第一个能领到当日积分。

## 二、推到 GitHub

```bash
git init && git add . && git commit -m "init"
gh repo create trae-checkin --public --source=. --push
```

没有 `gh` 就在网页上新建仓库再 `git remote add origin ... && git push -u origin main`。

> 如果 `git push` 报 `refusing to allow a Personal Access Token to create or update workflow`，
> 说明令牌缺 `workflow` 权限，执行一次 `gh auth refresh -s workflow` 再推。

## 三、填账号 Secret

网页：仓库 `Settings → Secrets and variables → Actions → New repository secret`
- `TRAE_ACCOUNTS`：`accounts.json` 的全部内容（整个 JSON 数组）
- `FEISHU_WEBHOOK`（可选）：飞书群机器人地址，成功和失败都会推送

命令行：

```bash
gh secret set TRAE_ACCOUNTS < accounts.json
```

## 四、开看板

`Settings → Pages` → Source 选 `Deploy from a branch` → 分支 `main`、目录 `/docs` → Save。

一分钟后访问 `https://<你的用户名>.github.io/trae-checkin/`。
第一次没有数据时，去 `Actions → Trae 每日签到 → Run workflow` 手动触发一次。

## 常见情况

| 现象 | 含义 |
| --- | --- |
| `code 1001` / `HTTP 401` | token 过期或失效，重新跑 `extract_creds.py` 更新 Secret |
| `code 9074` | 设备号无效，或该设备今日名额已被别的账号用掉 |
| `今日已签到` | 正常，重复运行不会重复领 |
| Actions 延迟 | 免费版定时任务可能推迟几十分钟，属正常 |

token 有效期约 14 天，`refreshToken` 约 180 天。过期后看板会显示失败，重新提取一次即可。

## 注意

- token 等同账号密码，只放仓库 Secret，不要写进代码或提交 `accounts.json`（已在 `.gitignore`）。
- 签到额度按账号/设备计算，工具只做「代替你点按钮」，不绕过任何风控；同一设备多账号是否都能领到，取决于服务端策略。
- 自动化操作可能违反平台服务条款，风险自负。
