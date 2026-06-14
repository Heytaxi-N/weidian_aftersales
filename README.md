# 微店售后提醒系统

一人经营的微店店主用的售后自动化助手：每天定时抓取微店退款单，按紧迫度和供货商分组推送到企业微信，避免漏处理而被自动退款。**同时覆盖卖家版（自己的店 vs 客户）和买家版（自己 vs 代发供货商）两侧售后。**

## 解决什么问题

店主既是**卖家**（自己开店卖货），也是**买家**（向上游供货商下单代发）。一笔退货会在两侧各产生一个售后单，任一侧超时都会**自动同意/关闭退款**，造成损失。

**卖家版**超时点（客户 ↔ 店主）：
- 退货退款：商家 4 天处理 → 同意后买家 7 天寄货 → 寄出后商家 7 天确认收货
- 仅退款：商家 4 天处理

**买家版**超时点（店主 ↔ 供货商）：
- 供货商同意退货后，店主有约 7 天把客户填的退货单号转填进买家版，否则超时关闭、拿不回供货商货款

活跃退款单量级 100~200 笔，单靠人工巡检容易漏。本系统每天 9:00/21:00 自动跑，按场景推送：

1. **A/A2 临期提醒**（卖家版）：剩余时间 ≤48h / ≤24h 的退款单每轮都推
2. **B 已签收预处理**（卖家版）：买家寄回包裹签收 ≥2 天还没确认收货，按合作供货商分组推送，方便整组转发上游
3. **C 买家版临期**（买家版）：买家版「待买家处理退货」剩余 ≤25h，提醒店主尽快处理
4. **D 待填退货单号**（买家版）：把卖家版客户填的退货单号，按客户手机号关联到对应买家版售后，提醒店主复制单号去买家版填写
5. **整体概览**：每轮跑完先发"4-tab 待办计数"，店主一眼看全局

卖家版（A/A2/B/概览/告警）和买家版（C/D）走**两个独立的企业微信群机器人**，互不干扰。

## 架构

```
[launchd 09:00 / 21:00]
        ↓
卖家版爬取（refundSearchList + getRefundDetailForPC + getOrderListForPC）
        ↓
SQLite (order_snapshots / pushed_records / logistics_cache / run_log)
        ↓
物流查询（vexpress/kuaidi.getExpressStepInfo，覆盖全部承运商）
        ↓
卖家版规则引擎 (A 临期 / A2 紧急 / B 已签收) — A/A2 不去重，B 配额 20/日
        ↓
推送：概览 → A/A2 → B 按供货商分组 ──────────→ 卖家版 webhook
        ↓
买家版爬取（buyer.frontRefundList 全店铺 + getRefundDetail + getOrderDetailForApp）
        ↓
买家版规则 (C 临期 ≤25h / D 按客户手机号关联卖家版退货单号)
        ↓
推送：C 临期 → D（唯一→自动填单号 / 多笔→转人工）→ 巡检心跳 ──→ 买家版 webhook
        ↓（09:00 加早报）
企业微信群机器人 (按 webhook 分桶 3.2s 节流，规避 20/min 限频)
        ↓
本地 FastAPI 面板（按需启）
```

## 推送规则

每轮 runner 跑完发送顺序（卖家版 webhook → 买家版 webhook）：

| # | 内容 | webhook | 说明 |
|---|---|---|---|
| 1 | **📊 售后概览** | 卖家版 | 4 个 tab 计数 + 待商家两类的临期数；0 隐藏 |
| 2 | **A/A2 临期提醒** | 卖家版 | 每笔退款一条 markdown，每轮都推（不去重）|
| 3 | **B 已签收推送** | 卖家版 | 按合作供货商分组：组头 1 条 + 组内多笔合并 1 条 text + 每笔卡片图 |
| 4 | **C 买家版临期** | 买家版 | 「待买家处理退货」剩余 ≤25h，每笔一条，每轮都推 |
| 5 | **D 退货单号** | 买家版 | 唯一匹配 → **自动填进买家版** + 审计消息；多笔 → 转人工提醒 |
| 6 | **📋 买家版巡检心跳** | 买家版 | 每轮固定一条，汇总待办 + C/D 数（0 绿 / >0 橙红），让店主有感知 |
| 7 | **☀️ 早报**（仅 09:00） | 卖家版 | 昨日推送 / 已处理 / 仍未处理 / 今日新增 |

### A / A2 触发（卖家版）

| 档位 | 条件 | 行为 |
|---|---|---|
| **A** 临期 | 剩余 ≤48h 且 >24h | 每轮都推 |
| **A2** 紧急 | 剩余 ≤24h | 每轮都推 |

适用所有 `待商家处理` / `待商家收货` 状态的退款单。

### B 触发与排序（卖家版）

- 状态 = `待商家收货` 且 `物流签收 ≥ 2 天`
- **不去重**：签收 ≥2 天的单子每轮都进 B 候选（早期版本"终身只推一次"已移除）
- 按 deadline 升序取前 N 笔（N = **20** - 今日已推 B 条数，跨天重置）
- 按合作供货商分组（无供货商归"无供货商"组），组顺序按"组内最紧迫笔"的 deadline 排
- 组内合并消息用 `text` 而非 `markdown`，便于店主长按转发到微信给供货商

### C 触发（买家版）

- 买家版状态 = `待买家处理退货`
- 剩余倒计时 ≤ 25h（直接取详情接口的 `refundCard.autoCountdownInSecond`，服务端权威值，不本地推算）
- 每轮都推，不去重不限额

### D 触发与关联（买家版，可自动填单号）

- 买家版状态 = `待买家处理退货`（店主仍需操作的）
- 按**客户手机号**关联：买家版订单收件人手机（`getOrderDetailForApp.buyerInfo.telephone`）== 卖家版 `待商家收货` 退款的客户手机（`buyer_phone`/`receiver_phone`），归一化后比较（去空格/横线/前导 86）
- **唯一匹配**（同手机号在卖家版只有 1 个退货单号）→ **自动填进买家版**（`buyer.submitExpressInfo`，承运商由卖家 `return_express_type` 在 `getSuggestExpressList` 枚举里反查），填后发 `✅ 已自动填入` 审计消息
- **多笔候选**（同手机号多个不同单号，无法安全消歧）→ 不自动，发 `⚠️ 请手动填` 列出候选
- 承运商解析不出 / 提交失败 → 企微告警，降级人工
- 幂等：已填的记 `pushed_records(scenario=D_FILLED)`，不重填
- 开关：`D_AUTOFILL_ENABLED`（默认关，已灰度通过并放开）；`D_AUTOFILL_LIMIT` 灰度上限。关闭时退回纯提醒（每轮推单号让店主手动填）

### 消息格式示例

**概览（卖家版）：**
```
📊 售后概览
> 待商家处理：15 (临期 4)
> 待商家收货：96 (临期 7)
> 待买家处理：21
> 客服介入：1
```

**A2 紧急（卖家版）：**
```
🚨 紧急
> 退款单：`14411xxxxxxxxxxxx`
> 订单：`84822xxxxxxxxxxx`
> 买家：张某 / 138****xxxx
> 申请类型：退款退货
> 当前节点：待商家收货
> 剩余时间：18.0 小时
```

**B 已签收（卖家版，按供货商）：**
```
合作供货商：供应商A（3 笔）
─────────────
张某，退，YT0000000000001
李某，退，JDX000000000001
王某，退，SF0000000000001
[卡片图 × 3]
```

**C 买家版临期：**
```
⚠️ 买家版退款临期提醒（剩余 22.5h）
> 店铺：供应商A
> 商品：北极狐 26春夏男
> 规格：藏青色;2XL
> 收件人：张某 / 138****xxxx
> 退款编号：`14411xxxxxxxxxxxx`
> 当前操作：商家同意退货，请退回商品
```

**D 自动填入（买家版，唯一匹配，审计消息）：**
```
✅ 已自动填入退货单号
> 供货商：供应商A
> 商品：On跑男款两色微　规格：黑色;M
> 收件人：王某 / 198****xxxx
> 物流：京东快递　单号：`JDX0000000000000`
> 退款编号：`14411xxxxxxxxxxxx`
```

**D 转人工（买家版，多笔候选无法自动消歧）：**
```
⚠️ 买家版待填单号（多个候选，请手动填）
> 供货商：供应商A
> 商品：lulu同源梭织　规格：藏青色;L
> 收件人：王某 / 198****xxxx
> 退款编号：`14411xxxxxxxxxxxx`
> 候选退货单号（同一客户多笔，需你判断）：
>   · `21878...001`（运动裤）
>   · `21878...002`（短袖）
```
（关闭自动填时，D 退回纯提醒：列单号让店主手动填。）

**买家版巡检心跳（每轮固定一条）：**
```
📋 买家版巡检 06-13 21:00
> 待买家处理退货：3 笔
> C 临期提醒(≤25h)：0(绿)　D 待填单号：2(橙红)
```

## 技术栈

- Python 3.12
- Playwright（登录持久化 + 物流卡片渲染）
- httpx（直调微店内部接口）
- FastAPI（本地面板）
- SQLite（本机存储，无远端）
- launchd（macOS 定时）

## 文件布局

```
src/
├── weidian/
│   ├── client.py            # 卖家版退款列表 + 详情抓取
│   ├── buyer_client.py      # 买家版退款列表(frontRefundList) + 退款/订单详情
│   ├── login.py             # 账号密码登录 + 有头浏览器过滑块
│   ├── overview.py          # 4 tab 计数 (refundSearchCount)
│   ├── order_supplier.py    # 按 orderId 查合作供货商
│   ├── trace_dump.py        # 一次性抓 trace XHR（用户手动点）
│   └── supplier_dump.py     # 一次性抓供货商相关 XHR
├── logistics/
│   ├── weidian_trace.py     # 微店内部物流接口（覆盖所有承运商）
│   ├── sogou.py             # legacy（Sogou 搜索方案，已不用）
│   └── kuaidi100.py         # legacy
├── rules/
│   └── engine.py            # A/A2/B 判定 + C(evaluate_buyer) + D(match_d)
├── notify/
│   ├── wecom.py             # 企业微信 webhook（双 webhook + 按桶 3.2s 节流）
│   ├── templates.py         # 概览 / A/A2 / B / C / D 多套模板
│   └── render.py            # Playwright 加载 about:blank 渲染签收卡片
├── dashboard/
│   └── app.py               # FastAPI 本地面板
├── report/
│   └── daily.py             # 早报
├── db.py                    # SQLite + 迁移
├── config.py                # 环境变量（含两个 webhook）
└── runner.py                # 主入口：卖家版抓→判定→推 A/A2/B；买家版抓→推 C/D

scripts/
├── login.sh                 # 首次/失效后重登
├── run.sh                   # launchd 调用入口
├── dashboard.sh             # 启动本地面板
├── diagnose_b.py            # 按 refund_id 诊断某笔为什么没被 B 推
├── trace_dump.sh            # 抓物流接口（用过一次后弃用）
├── supplier_dump.sh         # 抓供货商接口（用过一次后弃用）
├── smoke_test.sh            # DB / 推送 / 物流烟测
└── com.shop.aftersale.plist # launchd 定时配置

data/
├── shop.db                  # SQLite
├── storage_state.json       # Playwright 登录态
├── screenshots/             # 物流签收卡片图
└── logs/                    # 运行日志
```

## 数据模型

```sql
order_snapshots (snapshot_at, refund_id 主键)
  -- 每轮抓取写一份快照，含 status / deadline_at / buyer / receiver /
  -- item_title / item_id / return_tracking_no / supplier_name / raw_json

pushed_records (refund_id, scenario 主键)
  -- 推送历史，按 (refund_id, scenario) 去重；B 限额按今天 00:00 起 count

logistics_cache (tracking_no 主键)
  -- 物流查询缓存，6h TTL；"未知"态 30 分钟 TTL

run_log (started_at, finished_at, ok, note)
  -- 每轮 runner 执行记录
```

## 部署 / 日常使用

### 一次性设置

```bash
# 1. 安装依赖
uv venv --python 3.12 .venv
uv pip install -e ".[dev]"
.venv/bin/python -m playwright install chromium

# 2. 配置 .env
cp .env.example .env
# 填 WECOM_WEBHOOK_URL（卖家版群机器人）/ WECOM_WEBHOOK_URL_BUYER（买家版群机器人）
#   / WEIDIAN_USERNAME / WEIDIAN_PASSWORD

# 3. 首次登录微店（弹有头浏览器，自动填表，手动过滑块）
./scripts/login.sh
# 完成后 data/storage_state.json 会自动保存

# 4. 烟测一下（验证 DB / 企业微信 / 物流模块能跑通）
./scripts/smoke_test.sh
```

### 启用定时推送（每天 9:00 / 21:00 自动跑）

```bash
# 1. 拷贝 plist 到 macOS LaunchAgents 目录
cp scripts/com.shop.aftersale.plist ~/Library/LaunchAgents/

# 2. 加载（注册到 launchd）
launchctl load ~/Library/LaunchAgents/com.shop.aftersale.plist

# 3. 立即触发一次验证（不等到下一个 9:00/21:00）
launchctl start com.shop.aftersale
# 几分钟内企业微信会收到完整推送，且 data/logs/launchd.out.log 会写日志
```

定时配置在 `scripts/com.shop.aftersale.plist`：

- **09:00** 跑一轮 + 发早报
- **21:00** 跑一轮（不发早报）
- 日志：`data/logs/launchd.out.log` / `launchd.err.log`
- 工作目录：`/Users/nick/projects/weidian_aftersales/`（**改路径时 plist 里的绝对路径也要改**）

**修改定时时间**：编辑 plist 里的 `StartCalendarInterval` 段，改完重新加载：
```bash
launchctl unload ~/Library/LaunchAgents/com.shop.aftersale.plist
cp scripts/com.shop.aftersale.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.shop.aftersale.plist
```

**查看定时是否生效**：
```bash
launchctl list | grep aftersale
# 输出三列：PID(运行中才有) / 上次退出码 / Label
# 如果第二列是 0，说明上次跑成功了
```

**临时关闭定时**：
```bash
launchctl unload ~/Library/LaunchAgents/com.shop.aftersale.plist
```

### 看板（按需启）

看板**不常驻**，需要时手动开：

```bash
./scripts/dashboard.sh
# 输出: Dashboard 启动后请打开 http://localhost:8765
```

浏览器打开 **http://localhost:8765** 即可。三个页面：

| URL | 内容 |
|---|---|
| `/` | 当前所有待处理退款（最新快照）+ 「🔄 立即抓取」按钮 |
| `/pushed` | 推送历史（按时间倒序，可按场景 A/A2/B 和「已处理/未处理」筛选）|
| `/refund/<refund_id>` | 单笔退款的完整时间线（每轮快照变化 + 推送记录 + 截图链接）|

每页顶部都有「🔄 立即抓取」按钮，点击后后台异步跑一轮（约 5~15 分钟）。

用完按 Ctrl+C 关掉终端即可。

### 日常使用流程

**最常见的两种用法**：

1. **被动接收推送（推荐）**
   - 9:00 起床看企业微信：卖家版群按概览 + 紧急 + 已签收处理；买家版群按临期(C) + 待填单号(D)处理
   - 21:00 睡前再看一轮（不发早报）
   - 紧迫/待办单子（A/A2/C/D）每轮都会重推，处理掉后下一轮自动消失

2. **主动看面板**
   - 跑 `./scripts/dashboard.sh` 开面板
   - 想看「我现在到底有多少待办」→ `/`
   - 想看「这个 refund 之前推过吗」→ 点 `/pushed` 搜
   - 想立即抓最新数据 → 点页面顶部「立即抓取」

**手动立即跑一次**（不等定时）：
```bash
./scripts/run.sh                       # 普通跑
./scripts/run.sh --with-daily-report   # 含早报（同 09:00 行为）
./scripts/run.sh --dry-run             # 抓但不推送、不写库（调试用）
./scripts/run.sh --skip-logistics      # 跳过物流查询，5 秒跑完（应急）
```

**异常处理**：

| 现象 | 原因 | 解决 |
|---|---|---|
| 收到「微店登录失效」企业微信告警 | storage_state.json 过期 | 重跑 `./scripts/login.sh` |
| 收到「微店抓取失败」告警 | 微店接口变动 / 网络故障 | 看 `data/logs/run-YYYYMMDD.log` |
| 定时没触发 | Mac 在 9:00/21:00 处于休眠 | launchd 唤醒后会补跑；或改时间到你在线时段 |
| 某笔已签收但没推 | 物流接口未识别签收关键字（如京东"妥投"） | 已修；如再遇到把那笔 refund_id 给我 |
| 一次推送太多 | 紧迫单子积压 | 临时跑 `./scripts/run.sh --skip-logistics` 跳过 B；或调 plan 里的 quota |

## 关键技术决策

1. **核心实体是 `refund_id`** —— 一个订单可有多个退款单，独立监控。
2. **抓包逆向 + httpx 直调**，不靠 Playwright 渲染：卖家版列表/详情/物流/供货商、买家版列表/退款详情/订单详情都用真实 XHR 形态直接 POST/GET。
3. **DANGER ZONE 隔离 + 唯一开放的写动作**：微店退款详情页有【同意退款】【拒绝退款】危险按钮，物流模块**严禁** Playwright 打开微店域名页面，全靠 cookies + httpx；卡片渲染用 about:blank。**唯一开放的写操作是"上传退货物流单号"**（买家版 D 自动填，`buyer.submitExpressInfo`），且仅在客户手机号**唯一匹配**时自动执行、默认关闭（`D_AUTOFILL_ENABLED`）、提交后发企微审计消息；多笔候选一律转人工。同意/拒绝退款、确认收货等仍严禁。
4. **A/A2/C/D 不去重，每轮都推**：紧迫/待办单子需要持续在眼前直到处理；B 也已移除去重，只受每日配额限制。
5. **B 每日 20 条上限**：避免一次推太多导致店主无法处理完（早期是 10，后调到 20）。
6. **买家版用 `buyer.frontRefundList` 而非 SSR 页**：SSR 页 `weidian.com/user/order/refundList` 受 cookie `sid` 绑定只返回单店铺，会漏其他供货商的售后；frontRefundList API 返回全部店铺。
7. **D 关联用客户手机号**：代发场景下，买家版订单收件人 = 卖家版退款客户，是唯一可靠的跨侧关联键（买家版退款详情的 buyerName/buyerPhone 是店主自己，不能用）。
8. **双 webhook 隔离**：卖家版(A/A2/B/概览/告警)和买家版(C/D/心跳)走两个群机器人，节流按 webhook URL 分桶，互不阻塞。
9. **3.2s 节流**：企业微信群机器人限频 20/分钟。
10. **登录失效大小写不敏感识别**：微店会返回大写 `LOGIN ERROR`，判断登录失效统一走 `_is_login_error()`（`msg.lower()` 匹配），失效即抛 `WeidianNotLoggedIn` → 发企微告警，避免被当普通错误静默吞掉（2026-06-13 踩过：失效跑空却记 ok=1 无告警）。
11. **买家版巡检心跳**：C/D 只在命中时推，易让店主分不清"没待办"还是"跑挂了"。故每轮固定发一条心跳（含 C/D 计数，0 绿 >0 橙红）；抓取失败则发告警 —— 要么心跳要么告警，不会两头无声。

## 已知能力 vs 范围之外

✅ **能做**：
- 抓所有 4 个 tab 退款的总数（概览）
- 抓卖家版 `待商家处理` / `待商家收货` 详细数据（覆盖 100% 承运商物流）
- 抓买家版**全部供货商**的售后（frontRefundList，含倒计时、客户信息）
- 按合作供货商分组推送（卖家版 B）
- 按客户手机号关联两侧售后，提醒转填退货单号（D）
- **自动填退货单号（D）**：手机号唯一匹配时自动提交到买家版（默认关，需 `D_AUTOFILL_ENABLED`）；多笔转人工
- 配额、紧迫度排序、双 webhook 分流

❌ **不做**：
- 不点确认收货、不同意/拒绝退款（仅"上传退货物流单号"这一个写动作开放，见决策 3）
- 不监控买家寄货那 7 天
- 不做多账号（多店铺已通过买家版全店铺覆盖）
- 不部署到云、不做远程访问
- 不做 OCR / AI 自动判断

## 测试

```bash
.venv/bin/python -m pytest tests/ -v
```

当前 84 个单测（+2 skipped）覆盖：
- 卖家版规则引擎判定 + 合并边界（A/A2/B）
- 买家版 C 触发（倒计时边界）+ D 匹配/分类（手机号归一化 / 状态门控 / 唯一 vs 多笔）
- 买家版 SSR/API 解析 + 详情字段映射 + 自动填 `submit_return_express`（mock）
- C/D 推送模板渲染
- 登录失效识别（大小写不敏感，锁住 `LOGIN ERROR` 回归）
- 物流签收解析（含京东"妥投"特殊用语）
- B 配额算法 + 紧迫度排序
- 卖家版列表 / 详情解析（用真实 dump 作 fixture）
- 卡片渲染输出 JPEG

## 提交历史

```
5066213 修复登录失效被静默吞掉：大小写不敏感识别 LOGIN ERROR
5ec9589 巡检心跳 C/D 数字按值上色：0 绿、>0 橙红
68a1706 买家版巡检心跳：C/D 无命中也每轮发一条
92d7510 D 自动填单号（二）：提交能力 + runner 接线 + 审计
b14a080 D 自动填单号（一）：唯一/多笔分类逻辑 + 配置开关
687d1fc 新增场景 C：买家版退款临期推送（独立 webhook）
1e1d3b4 B 取消引擎层去重：签收 ≥2 天的单子每轮都进 B 候选
3c775dc B 配额扩到 20 + 修复 B 消息转发到微信 + 新增漏推诊断脚本
a24485d 物流改用微店内部 trace 接口，覆盖率 20% → 100%
6de9696 Initial commit: 微店售后提醒+预处理+管理系统
```

每次大改动都有独立提交 + 单测同步，可按需 revert。
