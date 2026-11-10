# Bull Gems Python 链下客户端

Python 3.10+。**生成钱包与读取运行分成两步**，钱包数量由生成脚本控制，统一保存到脚本目录的 `wallet.json`。邀请码默认 `0x75527f54`，对应[邀请链接](https://gem.bullshot.io/r/0x75527f54)。首次成功登录即按服务端规则注册。

## Linux 两步运行

解压完整脚本包后，在该目录执行：

### 第一步：生成钱包

```bash
bash generate_wallets.sh 100
```

把 `100` 改成需要的数量（1～10000）。只生成地址和私钥并保存到 **`wallet.json`**，不调用注册、登录或挖矿接口；首次准备环境时可能需要从 pip 源安装依赖。完成后脚本退出，不会开始挖矿。

数量表示**目标总数**：已有 100 个时再次传入 100 不变，传入 150 只追加 50 个；不允许传入较小数量来删除已有私钥。调整数量前先在运行窗口按 `Ctrl+C`，等待退出。

旧版用户：如果 `wallet.json` 尚不存在，但 `bull_gems_data/wallets.json` 存在，生成步骤会先复用旧文件的全部地址和私钥，再按目标总数补齐；旧文件不会删除或修改。若只剩状态文件而私钥文件丢失，则拒绝重建。

### 第二步：读取钱包并在当前窗口循环运行

```bash
bash start.sh
```

只读取 **`wallet.json` 中实际存在的全部钱包**，然后在当前窗口持续注册、签到和挖矿。运行脚本绝不生成、补齐或覆盖钱包，也不会写回私钥文件；缺少文件时会提示先执行第一步。邀请码为 `0x75527f54`。同一数据目录不允许重复运行。

两个入口共享独立 `.venv` 环境；依赖未变化且环境可用时，不会重复联网安装。

**前台持续循环，日志只在当前窗口打印，不创建或追加任何日志文件。按 `Ctrl+C` 即可停止，无需额外命令。** 收到退出信号后开始收尾，等待正在进行的请求结束后退出；关闭终端或 SSH 会话也会请求停止。旧版本已经生成的日志文件不会被本版修改或删除。

需要 Linux 和 Python 3.10+；Ubuntu/Debian 以 root 运行时会自动补装缺少的 Python/venv。不会要求浏览器、钱包扩展或扫码。

普通网络错误、API 错误、单钱包异常会自动退避重试，不再因全部钱包失败而结束进程；客户端初始化和调度异常也会每 30 秒重试。启动器阶段的钱包文件缺失、依赖安装失败会报错退出。单钱包失败不影响其他钱包，旧版 `paused_reason` 会自动重新检查，无需 `--resume`。403/关键字段异常至少冷却 5 分钟后复查，仍保留签名、矿位上限等检查。**邀请关系未确认只提示，不阻塞签到和挖矿。**

启动提示不等于“全部账号已注册/正在挖矿”。实际结果看当前窗口日志；找不到 5/60 矿洞时仍按下文规则暂停新开工。

## 手动运行（可选）

Shell 入口会自动准备环境。需要手动调用 Python 时：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# 第一步：只生成和保存钱包
python bull_gems.py --init-only --wallets 100 --wallet-file wallet.json

# 第二步：只读取钱包并循环运行
python bull_gems.py --wallet-file wallet.json
```

`bash start.sh` 和直接调用 Python 都在当前窗口运行，不使用 `nohup`、`&`、`> 文件名`、`tee` 等后台或日志文件重定向。

使用 `Ctrl+C` 或 `SIGTERM` 退出；再次运行相同命令即可续跑。默认数据目录跟随脚本位置，而不是当前工作目录。可用 `--data-dir /your/private/directory` 显式指定，后续始终使用同一目录。

Windows：用 `python -m venv .venv` 创建环境，通过 `.venv\Scripts\python.exe` 执行上述 Python 命令即可。

## 本地文件

```text
wallet.json             # 全部地址 + private_key，仅生成步骤创建/追加
bull_gems_data/
  state/<address>.json  # bearer token、任务计时、最近能量、暂停/开工状态
  client.lock           # 阻止同一数据目录被两份进程同时使用
```

- 生成步骤把钱包原子落盘；运行步骤只读取并校验。损坏、地址不匹配或丢失钱包文件时，不发送注册请求、不静默覆盖或重建。客户端会等待后重读，启动器也会提前检查文件是否存在。
- Linux 数据目录权限为 `0700`，新建私钥和状态文件为 `0600`。Windows 文件权限继承所在目录的 ACL。
- 私钥只用于本地 `personal_sign`，不上传，不输出到日志；发出的登录数据只有地址、签名、message 和邀请码。状态文件中的 token 同样需自行保管。
- 私钥使用 `Account.create()` 的随机源产生；保存后重启复用。随机新地址与既有地址碰撞的概率极低，但客户端无法独立证明某地址在服务端从未注册过。
- 只有生成步骤可使用 `--wallets`；运行步骤按文件中的实际数量运行，不能用 `--wallets` 生成或补齐。不要删除钱包文件来“重试”。

### 私钥保存与后期导出

全部私钥集中保存在脚本目录的 **`wallet.json`**，生成及启动时会打印绝对路径。后期直接复制或下载这个 JSON 文件即可，字段格式如下（仅为占位示例）：

```json
{
  "version": 1,
  "wallets": [
    {"address": "0x...", "private_key": "0x...", "created_at": "..."}
  ]
}
```

该文件可被其他 Python 脚本直接读取，`wallets` 数组中每条记录就是一组地址和私钥。运行窗口不会打印实际私钥。

## 执行逻辑

1. 取得 nonce，只签署以 `Sign in to Bull GEM on Base: ` 开头的登录消息。
2. 带 `refCode` 登录；读取 `/user/me` 检查邀请信息。`referredBy` 非空时记为已确认，空值或接口未返回该字段时分别提示；**只要登录成功，都继续签到和挖矿，不为邀请关系等待、重新登录或触发 5 分钟退避**。同一邀请状态不会每轮重复提示，状态变化时才再次提示。非空字段也不等于已核对上级的具体身份。
3. 请求均携带 `Authorization: Bearer <token>` 和 `X-Wallet-Address`。401 重新登录后仅重试原请求一次；登录仍失败则退避，不死循环。
4. 用 `GET /quests` 查询服务端任务状态，只处理 `daily_miner_signin`、`weekly_miner_checkin`，跳过其他任务及 X/Twitter 平台任务。启动后至少等 10 秒，服务端要求更长则等更长。已领取任务每 5 分钟重新查询，是否重置以服务端为准，不猜时区。
5. 先领取所有 `ready` 矿位；也识别前端可领取的 `completed` 状态。之后刷新矿位、读取 `/mining/stats` 的实时能量；能量不少于 5 且未领取矿位少于 5 个时，每轮再开一个。
6. 只选择 API **实际返回**、符合等级/算力条件的 **5 能量、60 秒** 矿洞。没有匹配项时保留领取和签到，暂停新开工，**不自动改选更贵矿洞**。
7. 通常空闲等待 15 秒；任务倒计时可提前唤醒。默认并发 3 个钱包、全局请求间隔至少 0.25 秒。全部钱包共享额度，单个钱包实际轮询间隔可能超过 15 秒；不保证矿位满 60 秒时立即领取。
8. 429 全局遵守 `Retry-After`，其他网络错误指数退避。403/重定向/关键字段变化会等待至少 5 分钟后重查该钱包，不轮换代理或尝试绕过访问限制。未知异常和状态文件保存失败也不会使整个循环结束。

该脚本没有 RPC、交易构造、链上签名、授权、充值或提现代码；只调用列出的 HTTPS 接口，不使用浏览器、钱包扩展或二维码。

## 故障恢复

日志仅输出到启动窗口，显示地址、动作、HTTP 状态、网络失败的请求方法/接口路径和耗时，以及定期的能量/矿位快照，不显示响应正文、URL 查询参数、token 或私钥。可保存状态时，最近错误和重试时间记录在对应 `state/<address>.json` 的 `last_error`、`retry_at` 中；这是运行状态，不是日志文件。保存失败时保留内存状态并继续重试。读取失败的状态文件不会被空状态覆盖，修复后自动重新加载。

正常运行不需要手动恢复，`--resume` 仅作为旧命令的兼容选项保留。按 `Ctrl+C` 即可正常结束循环；SIGTERM、关闭终端产生的 SIGHUP 同样会请求停止。`--init-only`、`--clear-pending` 仍是执行一次即退出的维护命令。

旧版反复出现 `referredBy is empty; no quest or mining requests were allowed; retry in 300.0s` 是客户端把邀请关系当作前置条件，并非挖矿接口返回的错误。本版已改为仅提示 `referral unconfirmed; quests and mining continue`，区分字段未返回与明确空值；保留有效 token，不会为此重复登录。覆盖更新程序文件后重新 `bash start.sh` 即可，旧暂停/退避状态会自动解除，无需删除或重新生成 `wallet.json`，也不需要删除状态文件。

### ReadTimeout 与运行状态

`ReadTimeout` 表示等待响应数据时超过读取等待时间，不是 HTTP 错误码。当前连接/读取等待仍为 `10` / `30` 秒，没有因这次诊断修改等待时间或增加开矿 POST 的盲目重试。仅凭异常类型无法确认是某个服务端接口慢还是服务器出口网络问题；不会将其当作邀请失败处理。

2026-11-10 起网络错误日志包含具体接口和耗时。例如（仅示例，不是服务器实测）：

```text
WARNING 0x... ReadTimeout GET /mining/stats (elapsed=30.0s); retry in 15.8s
INFO 0x... mining check: energy=0; occupied=0/5; status=waiting_energy
```

成功读到用户资料、矿位和能量后，每个钱包首次及之后至少间隔 5 分钟打印一条状态快照，不额外发送请求。快照是本轮新开矿之前的服务端读数；若一直请求失败，则只能打印相应错误，不会编造能量或正常状态。

- `waiting_energy`：能量不足 5，继续等待。
- `positions_full`：未领取矿位已占满 5 个。
- `pending_confirmation`：前一次开矿结果不明，继续查状态，暂不重复开矿。
- `checking_location`：能量和矿位满足基础条件，接下来检查是否存在可用的 5/60 矿洞；不代表已经开矿，实际成功看 `miner deployed`。

旧版正常空闲检查不打印日志，因此画面上可能只见到零星超时。每次成功完成一轮后错误退避计数会归零；如果超时日志一直是 `retry in 15.xs` 且相隔十几分钟，这通常表示中间有成功轮次，而非连续失败。签到领取成功也只证明签到完成，不等于矿位已开工。

不会自动清除 `pending_open`。开工 POST 如果超时或返回 5xx，服务端可能已接收，所以脚本先查询矿位，不直接重复开工；发现新矿位后自动解除。如果始终无法确认，只暂停该钱包的新开工，主循环仍继续，已有矿位仍可领取。人工核对服务端确定无需保留标记后，先停止运行进程，再执行：

```bash
python bull_gems.py --clear-pending 0xYour40HexCharacterWalletAddress
bash start.sh
```

其他选项：`--workers 3`、`--interval 15`、`--request-interval 0.25`、`--skip-quests`、`--location-id ID`、`--ref-code CODE`。显式指定矿洞 ID 仍要求它符合 5/60 条件。

## 核对与测试范围

2026-11-05 只读检查了 [Bull Gems 公开前端](https://gem.bullshot.io/) 及其 [JavaScript 构建产物](https://gem.bullshot.io/assets/index-DEur3qZy.js)，确认上述接口路径、登录消息、任务分组/状态、矿位字段和能量来源。公开前端的备用矿洞配置包含 `10` 能量 / `600` 秒的 Greenstone Quarry，**不是**你描述的 `5` / `60`，所以脚本不硬编码备用矿洞 ID。

2026-11-09 再次只读检查[当前公开前端构建产物](https://gem.bullshot.io/assets/index-BhN3lGyH.js)：登录仍提交 `refCode`，该构建未引用 `referredBy` 字段。仅凭这个公开构建不能判断你的 `/user/me` 是缺少字段还是明确返回空值，也不能证明邀请是否实际绑定；本版不再把邀请确认当作挖矿的前置条件。

直接匿名读取矿洞接口返回 403，未实际生成生产钱包、注册账号、绑定邀请或挖矿。实际 API 的返回和权限仍需在你的服务器运行时确认。测试使用公开的确定性测试私钥、模拟 HTTP，不访问真实注册服务：

```bash
python -m unittest -v test_bull_gems test_start_sh
```

本地测试依赖版本：`eth-account 0.13.7`、`requests 2.32.5`。
