# Bull Gems Python 链下客户端

Python 3.10+。默认维护 **100 个本地钱包**，邀请码默认 `0x75527f54`，对应[邀请链接](https://gem.bullshot.io/r/0x75527f54)。注册登录请求发送 `refCode: "0x75527f54"`。首次成功登录即按服务端规则注册，后续启动复用同一个钱包文件，不会每次重建地址。

## 运行（Linux 服务器）

将 `bull_gems.py`、`requirements.txt` 放进服务器上的同一目录，然后：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# 第一步：只生成和保存钱包，不发出任何 HTTP 请求
python bull_gems.py --wallets 100 --init-only

# 先自行保管 bull_gems_data/wallets.json，再启动注册、签到和挖矿
python bull_gems.py --wallets 100
```

后台运行：

```bash
nohup .venv/bin/python -u bull_gems.py --wallets 100 > bull_gems.log 2>&1 &
tail -f bull_gems.log
```

使用 `Ctrl+C` 或 `SIGTERM` 退出；再次运行相同命令即可续跑。默认数据目录跟随脚本位置，而不是当前工作目录。可用 `--data-dir /your/private/directory` 显式指定，后续始终使用同一目录。

Windows：用 `python -m venv .venv` 创建环境，通过 `.venv\Scripts\python.exe` 执行上述 Python 命令即可。

## 本地文件

```text
bull_gems_data/
  wallets.json          # address + private_key，包含全部钱包的明文私钥
  state/<address>.json  # bearer token、任务计时、最近能量、暂停/开工状态
  client.lock           # 阻止同一数据目录被两份进程同时使用
```

- 钱包在任何注册请求前原子落盘；损坏、地址不匹配或丢失钱包文件时停止，不静默覆盖。
- Linux 数据目录权限为 `0700`，新建私钥和状态文件为 `0600`。Windows 文件权限继承所在目录的 ACL。
- 私钥只用于本地 `personal_sign`，不上传，不输出到日志；发出的登录数据只有地址、签名、message 和邀请码。状态文件中的 token 同样需自行保管。
- 私钥使用 `Account.create()` 的随机源产生；保存后重启复用。随机新地址与既有地址碰撞的概率极低，但客户端无法独立证明某地址在服务端从未注册过。
- 增大 `--wallets` 只会追加钱包；不能用较小数字忽略已有钱包。不要删除钱包文件来“重试”。

## 执行逻辑

1. 取得 nonce，只签署以 `Sign in to Bull GEM on Base: ` 开头的登录消息。
2. 带 `refCode` 登录；读取 `/user/me` 并要求 `referredBy` 非空，空值则暂停该钱包，不做任务或挖矿。该检查确认存在上级关系，不单凭非空字段宣称已核对上级的具体身份。
3. 请求均携带 `Authorization: Bearer <token>` 和 `X-Wallet-Address`。401 重新登录后仅重试原请求一次；登录仍失败则退避，不死循环。
4. 用 `GET /quests` 查询服务端任务状态，只处理 `daily_miner_signin`、`weekly_miner_checkin`，跳过其他任务及 X/Twitter 平台任务。启动后至少等 10 秒，服务端要求更长则等更长。已领取任务每 5 分钟重新查询，是否重置以服务端为准，不猜时区。
5. 先领取所有 `ready` 矿位；也识别前端可领取的 `completed` 状态。之后刷新矿位、读取 `/mining/stats` 的实时能量；能量不少于 5 且未领取矿位少于 5 个时，每轮再开一个。
6. 只选择 API **实际返回**、符合等级/算力条件的 **5 能量、60 秒** 矿洞。没有匹配项时保留领取和签到，暂停新开工，**不自动改选更贵矿洞**。
7. 通常空闲等待 15 秒；任务倒计时可提前唤醒。默认并发 3 个钱包、全局请求间隔至少 0.25 秒。100 个钱包共享额度，单个钱包实际轮询间隔可能超过 15 秒；不保证矿位满 60 秒时立即领取。
8. 429 全局遵守 `Retry-After`，其他网络错误指数退避。403/重定向/关键字段变化会暂停该钱包，不轮换代理或尝试绕过访问限制。

该脚本没有 RPC、交易构造、链上签名、授权、充值或提现代码；只调用列出的 HTTPS 接口，不使用浏览器、钱包扩展或二维码。

## 故障恢复

日志只显示地址、动作和 HTTP 状态，不显示响应正文、token 或私钥。暂停原因写入对应 `state/<address>.json` 的 `paused_reason`。

修正邀请码、接口字段或访问问题后，可重试已暂停的钱包：

```bash
python bull_gems.py --wallets 100 --resume
```

`--resume` 不会自动清除 `pending_open`。开工 POST 如果超时或返回 5xx，服务端可能已接收，所以脚本先查询矿位，不直接重复开工；发现新矿位后自动解除。如果始终无法确认，只暂停该钱包的新开工，已有矿位仍可领取。人工核对服务端确定无需保留标记后，先停止运行进程，再执行：

```bash
python bull_gems.py --clear-pending 0xYour40HexCharacterWalletAddress
python bull_gems.py --wallets 100
```

其他选项：`--workers 3`、`--interval 15`、`--request-interval 0.25`、`--skip-quests`、`--location-id ID`、`--ref-code CODE`。显式指定矿洞 ID 仍要求它符合 5/60 条件。

## 核对与测试范围

2026-11-05 只读检查了 [Bull Gems 公开前端](https://gem.bullshot.io/) 及其 [JavaScript 构建产物](https://gem.bullshot.io/assets/index-DEur3qZy.js)，确认上述接口路径、登录消息、任务分组/状态、矿位字段和能量来源。公开前端的备用矿洞配置包含 `10` 能量 / `600` 秒的 Greenstone Quarry，**不是**你描述的 `5` / `60`，所以脚本不硬编码备用矿洞 ID。

直接匿名读取矿洞接口返回 403，未实际生成生产钱包、注册账号、绑定邀请或挖矿。实际 API 的返回和权限仍需在你的服务器运行时确认。测试使用公开的确定性测试私钥、模拟 HTTP，不访问真实注册服务：

```bash
python -m unittest -v test_bull_gems
```

本地测试依赖版本：`eth-account 0.13.7`、`requests 2.32.5`。
