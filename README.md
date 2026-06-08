# 微店售后提醒系统

一人经营的微店店主用的售后自动化助手：每天定时抓取微店退款单，按紧迫度和供货商分组推送到企业微信，避免漏处理而被自动退款。

## 解决什么问题

微店退款流程对商家有多个超时点，任何超时都会**自动同意退款**：

- 退货退款：商家 4 天处理 → 同意后买家 7 天寄货 → 寄出后商家 7 天确认收货
- 仅退款：商家 4 天处理

活跃退款单量级 100~200 笔，单靠人工巡检容易漏。本系统：

1. **临期提醒**：剩余时间 ≤48h 的退款单每天 9:00/21:00 自动推送
2. **已签收预处理**：买家寄回的包裹签收 ≥2 天还没确认收货，按合作供货商分组推送，方便整组转发上游
3. **整体概览**：每轮跑完先发"4-tab 待办计数"，店主一眼看全局
4. **去重 + 配额**：紧迫的每轮都推，已签收每天 10 条上限，避免轰炸

## 架构

```
[launchd 09:00 / 21:00]
        ↓
微店爬取（refundSearchList + getRefundDetailForPC + getOrderListForPC）
        ↓
SQLite (order_snapshots / pushed_records / logistics_cache / run_log)
        ↓
物流查询（vexpress/kuaidi.getExpressStepInfo，覆盖全部承运商）
        ↓
规则引擎 (A 临期 / A2 紧急 / B 已签收) — A/A2 不去重，B 配额 10/日
        ↓
推送层：概览 → A/A2 → B 按供货商分组 → （09:00 加早报）
        ↓
企业微信群机器人 (3.2s 节流，规避 20/min 限频)
        ↓
本地 FastAPI 面板（按需启）
```

## 推送规则

每轮 runner 跑完发送顺序：

| # | 内容 | 说明 |
|---|---|---|
| 1 | **📊 售后概览** | 4 个 tab 计数 + 待商家两类的临期数；0 隐藏 |
| 2 | **A/A2 临期提醒** | 每笔退款一条 markdown，每轮都推（不去重），直到处理完 |
| 3 | **B 已签收推送** | 按合作供货商分组：组头 1 条 + 组内多笔合并 1 条 + 每笔卡片图 |
| 4 | **☀️ 早报**（仅 09:00） | 昨日推送 / 已处理 / 仍未处理 / 今日新增 |

### A / A2 触发

| 档位 | 条件 | 行为 |
|---|---|---|
| **A** 临期 | 剩余 ≤48h 且 >24h | 每轮都推 |
| **A2** 紧急 | 剩余 ≤24h | 每轮都推 |

适用所有 `待商家处理` / `待商家收货` 状态的退款单。

### B 触发与排序

- 状态 = `待商家收货` 且 `物流签收 ≥ 2 天`
- 按 deadline 升序取前 N 笔（N = 10 - 今日已推 B）
- 按合作供货商分组（无供货商归"无供货商"组）
- 组顺序按"组内最紧迫笔"的 deadline 排
- 单笔走 `pushed_records` 去重，终身只推一次

### 消息格式示例

**概览：**
```
📊 售后概览
> 待商家处理：15 (临期 4)
> 待商家收货：96 (临期 7)
> 待买家处理：21
> 客服介入：1
```

**A2 紧急：**
```
🚨 紧急
> 退款单：`14411xxxxxxxxxxxx`
> 订单：`84822xxxxxxxxxxx`
> 买家：张某 / 138****xxxx
> 申请类型：退款退货
> 当前节点：待商家收货
> 剩余时间：18.0 小时
```

**B 已签收（按供货商）：**
```
合作供货商：供应商A（3 笔）
─────────────
张某，退，YT0000000000001
李某，退，JDX000000000001
王某，退，SF0000000000001
[卡片图 × 3]
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
│   ├── client.py            # 退款列表 + 详情抓取
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
│   └── engine.py            # A/A2/B 判定 + 合并 + B 去重
├── notify/
│   ├── wecom.py             # 企业微信 webhook + 3.2s 节流
│   ├── templates.py         # 概览 / A/A2 / B 多套模板
│   └── render.py            # Playwright 加载 about:blank 渲染签收卡片
├── dashboard/
│   └── app.py               # FastAPI 本地面板
├── report/
│   └── daily.py             # 早报
├── db.py                    # SQLite + 迁移
├── config.py                # 环境变量
└── runner.py                # 主入口：抓 → 判定 → 推送

scripts/
├── login.sh                 # 首次/失效后重登
├── run.sh                   # launchd 调用入口
├── dashboard.sh             # 启动本地面板
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
# 填 WECOM_WEBHOOK_URL（企业微信群机器人）/ WEIDIAN_USERNAME / WEIDIAN_PASSWORD

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
- 工作目录：`/Users/nick/Downloads/售后管理/`（**改路径时 plist 里的两处绝对路径也要改**）

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
   - 9:00 起床看企业微信，按概览 + 紧急 + 已签收的顺序处理
   - 21:00 睡前再看一轮（不发早报，仅推送当时紧迫和签收的）
   - 紧迫的退款单（A/A2）每轮都会重推，处理掉后下一轮自动消失

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
2. **抓包逆向 + httpx 直调**，不靠 Playwright 渲染：列表 / 详情 / 物流 / 供货商 4 个接口都用真实 XHR 形态直接 POST/GET。
3. **DANGER ZONE 隔离**：微店退款详情页有【同意退款】【拒绝退款】危险按钮，物流模块**严禁** Playwright 打开微店域名页面，全靠 cookies + httpx；卡片渲染用 about:blank。
4. **A/A2 不去重，B 去重**：紧迫单子需要持续在眼前；已签收信息相对静态发一次够。
5. **B 每日 10 条上限**：避免一次推 30+ 条导致店主无法处理完。
6. **3.2s 节流**：企业微信群机器人限频 20/分钟。

## 已知能力 vs 范围之外

✅ **能做**：
- 抓所有 4 个 tab 退款的总数（概览）
- 抓 `待商家处理` / `待商家收货` 详细数据（覆盖 100% 承运商物流）
- 按合作供货商分组推送
- 配额、去重、紧迫度排序

❌ **不做**：
- 不主动操作微店（不点确认收货、不同意退款）
- 不监控买家寄货那 7 天
- 不做多店铺、多账号
- 不部署到云、不做远程访问
- 不做 OCR / AI 自动判断

## 测试

```bash
.venv/bin/python -m pytest tests/ -v
```

当前 42 个单测覆盖：
- 规则引擎判定 + 合并 + 去重边界
- 物流签收解析（含京东"妥投"特殊用语）
- B 配额算法 + 紧迫度排序
- 微店列表 / 详情解析（用真实 dump 作 fixture）
- 卡片渲染输出 JPEG

## 提交历史

```
d1381b3 A/A2 临期紧急去掉 dedup：每轮都推直到处理完
d23f870 修复京东物流签收漏识别：scanType='取件运单妥投' 不含"签收"二字
ed88532 B 推送格式微调：组头独立 + 组内所有笔合并一条多行 markdown
43d43db B 推送拆分：供货商组头单独一条，每笔单独一条 markdown + 图
891a2f4 B 推送按合作供货商分组：店主一次只看一家供应商的所有待办
fa4b463 推送结构改造：概览 + A/A2 去链接 + B 每日 10 条紧凑格式
a24485d 物流改用微店内部 trace 接口，覆盖率 20% → 100%
6de9696 Initial commit: 微店售后提醒+预处理+管理系统
```

每次大改动都有独立提交 + 单测同步，可按需 revert。
