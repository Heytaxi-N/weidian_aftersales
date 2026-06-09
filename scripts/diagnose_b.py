#!/usr/bin/env python
"""按 refund_id 诊断为什么这单没被 B 场景推送过。

用法：
    python scripts/diagnose_b.py <refund_id>

逐项打印：
  1. order_snapshots 是否抓到
  2. 状态是否为「待商家收货」
  3. 是否有退货单号
  4. logistics_cache 里的签收情况（已签收 N 天）
  5. pushed_records 里 B 场景历史
  6. 今天的 B 配额使用情况 + 此单在「今日候选」里按 deadline 排序的位置
  7. A/A2 推送历史（信息项）

依赖：所有信息从本地 SQLite 取，不发起任何网络请求。
若 logistics_cache 没有该 tracking_no 的记录，会提示「未命中物流缓存」。
"""
from __future__ import annotations

import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

# 让脚本无需通过 `python -m` 也能 import src.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db
from src.config import TIMEZONE
from src.rules.engine import (
    SIGNED_THRESHOLD_DAYS,
    STATUS_PENDING_MERCHANT_RECEIVE,
)
from src.runner import B_DAILY_QUOTA, _b_quota_used_today

OK = "✅"
NO = "❌"
INFO = "ℹ️ "


def _fmt_dt(s: str | None) -> str:
    if not s:
        return "—"
    try:
        return datetime.fromisoformat(s).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s


def diagnose(refund_id: str) -> str:
    """跑完所有检查，打印每行结果，返回最终结论字符串。"""
    reasons: list[str] = []

    with db.get_conn() as conn:
        # 1. snapshot
        snap = conn.execute(
            "SELECT * FROM order_snapshots WHERE refund_id = ? "
            "ORDER BY snapshot_at DESC LIMIT 1",
            (refund_id,),
        ).fetchone()
        if not snap:
            print(f"{NO} 1. 未在 order_snapshots 中找到此 refund_id")
            print(f"     可能此单不在抓取范围内（非待商家状态），或从未跑过抓取")
            return "该单从未被系统抓取过 — 无法判定是否应推 B"

        snap = dict(snap)
        global_latest = conn.execute(
            "SELECT MAX(snapshot_at) AS mx FROM order_snapshots"
        ).fetchone()["mx"]
        is_latest = snap["snapshot_at"] == global_latest
        stale_mark = "" if is_latest else \
            f"（⚠ 不在最新抓取里 — 全局最新={_fmt_dt(global_latest)}，"\
            f"此单可能已被店主处理或状态已变）"
        print(f"{OK} 1. 找到 snapshot（snapshot_at={_fmt_dt(snap['snapshot_at'])}）{stale_mark}")
        print(f"     order_id={snap['order_id']} item={snap.get('item_title') or '—'}")
        if not is_latest:
            reasons.append("已不在最新一次抓取里（疑似已处理）")

        # 2. 状态
        status = snap.get("status")
        if status == STATUS_PENDING_MERCHANT_RECEIVE:
            print(f"{OK} 2. 状态 = 「{status}」")
        else:
            print(f"{NO} 2. 状态 = 「{status}」，不是「{STATUS_PENDING_MERCHANT_RECEIVE}」")
            reasons.append(f"状态不是「{STATUS_PENDING_MERCHANT_RECEIVE}」")

        # 3. 退货单号
        tn = snap.get("return_tracking_no")
        if tn:
            print(f"{OK} 3. 退货单号 = {tn}")
        else:
            print(f"{NO} 3. 无退货单号")
            reasons.append("无退货单号")

        # 4. 物流签收（查 logistics_cache，不发请求）
        now = datetime.now(TIMEZONE)
        signed_at: datetime | None = None
        if tn:
            log_row = conn.execute(
                "SELECT * FROM logistics_cache WHERE tracking_no = ?", (tn,)
            ).fetchone()
            if not log_row:
                print(f"{NO} 4. logistics_cache 未命中 {tn}（本系统从未查过该单号物流）")
                reasons.append("物流未查询过")
            else:
                log_row = dict(log_row)
                sa = log_row.get("signed_at")
                carrier = log_row.get("carrier") or "—"
                last_q = _fmt_dt(log_row.get("last_query_at"))
                if not sa:
                    print(f"{NO} 4. 物流未签收（承运商={carrier}，最后查询={last_q}）")
                    reasons.append("物流未签收")
                else:
                    signed_at = datetime.fromisoformat(sa)
                    days = (now - signed_at).total_seconds() / 86400.0
                    if (now - signed_at) >= timedelta(days=SIGNED_THRESHOLD_DAYS):
                        print(f"{OK} 4. 已签收 {days:.1f} 天 "
                              f"≥ {SIGNED_THRESHOLD_DAYS} 天（承运商={carrier}，"
                              f"签收={_fmt_dt(sa)}）")
                    else:
                        print(f"{NO} 4. 已签收 {days:.1f} 天 "
                              f"< {SIGNED_THRESHOLD_DAYS} 天，未达推送阈值"
                              f"（签收={_fmt_dt(sa)}）")
                        reasons.append(
                            f"签收未满 {SIGNED_THRESHOLD_DAYS} 天（{days:.1f} 天）"
                        )
        else:
            print(f"{INFO} 4. 跳过物流查询（无退货单号）")

        # 5. B 历史推送（仅信息项 —— B 已取消引擎层去重，可重复推）
        b_rows = conn.execute(
            "SELECT pushed_at FROM pushed_records "
            "WHERE refund_id = ? AND scenario = 'B' ORDER BY pushed_at",
            (refund_id,),
        ).fetchall()
        if b_rows:
            times = ", ".join(_fmt_dt(r["pushed_at"]) for r in b_rows)
            print(f"{INFO} 5. B 历史推送 {len(b_rows)} 次：{times}")
        else:
            print(f"{INFO} 5. 此 refund_id 从未被 B 推过")

        # 6. 今日 B 配额 + 排序位置
        used = _b_quota_used_today(now)
        quota_left = max(0, B_DAILY_QUOTA - used)
        if quota_left == 0:
            print(f"{NO} 6. 今日 B 配额已耗尽（{used}/{B_DAILY_QUOTA}）— "
                  f"今天不会再推任何 B，等明天 00:00 配额重置")
            reasons.append(f"今日 B 配额已耗尽（{used}/{B_DAILY_QUOTA}）")
        else:
            print(f"{INFO} 6. 今日 B 配额：已用 {used}/{B_DAILY_QUOTA}，剩余 {quota_left}")

        # 估算此单在今日 B 候选里的 deadline 排名
        # （所有最新 snapshot 中：待商家收货 + 有 tracking + 在 logistics_cache 已签收 ≥2 天 + 未推过 B）
        today_start = datetime.combine(now.date(), dtime(0, 0), tzinfo=TIMEZONE)
        # B 候选池：限定到「全局最新一次抓取」里出现的 refund，
        # 避免把已被店主处理、早就不在 fetch 结果里的 stale refund 统计进来。
        # 条件：状态=待商家收货 + 有 tracking + 物流缓存里签收 ≥ 2 天
        latest_row = conn.execute(
            "SELECT MAX(snapshot_at) AS mx FROM order_snapshots"
        ).fetchone()
        latest_snapshot_at = latest_row["mx"]
        candidates = conn.execute("""
            SELECT s.refund_id, s.deadline_at
            FROM order_snapshots s
            JOIN logistics_cache l ON l.tracking_no = s.return_tracking_no
            WHERE s.snapshot_at = ?
              AND s.status = ?
              AND s.return_tracking_no IS NOT NULL
              AND l.signed_at IS NOT NULL
              AND l.signed_at <= ?
        """, (
            latest_snapshot_at,
            STATUS_PENDING_MERCHANT_RECEIVE,
            (now - timedelta(days=SIGNED_THRESHOLD_DAYS)).isoformat(),
        )).fetchall()

        def _key(row):
            d = row["deadline_at"]
            if not d:
                return (1, datetime.max.replace(tzinfo=TIMEZONE))
            return (0, datetime.fromisoformat(d))

        candidates_sorted = sorted(candidates, key=_key)
        rank = next((i for i, r in enumerate(candidates_sorted)
                     if r["refund_id"] == refund_id), None)
        total = len(candidates_sorted)
        if rank is None:
            print(f"{INFO}    此单未出现在今日 B 候选池中（条件不满足，见上文）")
        else:
            print(f"{INFO}    此单在今日 B 候选池中按 deadline 排序：第 {rank + 1}/{total}")
            # 只有配额还有剩时才比较 rank vs quota_left
            if quota_left > 0 and rank + 1 > quota_left:
                print(f"{NO}    rank {rank + 1} > 今日剩余配额 {quota_left}，"
                      f"本轮会被裁掉")
                reasons.append(
                    f"deadline 排名 {rank + 1} 超过剩余配额 {quota_left}"
                )

        # 7. A/A2 推送历史（信息项）
        a_rows = conn.execute(
            "SELECT scenario, pushed_at FROM pushed_records "
            "WHERE refund_id = ? AND scenario IN ('A', 'A2') "
            "ORDER BY pushed_at",
            (refund_id,),
        ).fetchall()
        if a_rows:
            print(f"{INFO} 7. A/A2 推送历史：")
            for r in a_rows:
                print(f"     - {r['scenario']} @ {_fmt_dt(r['pushed_at'])}")
        else:
            print(f"{INFO} 7. A/A2 从未推过")

    # 结论
    if not reasons:
        print()
        print(f"{OK} 结论：所有 B 条件均满足，此单应被 B 推送。")
        print("     如果实际未推，请检查最近一次跑的 logs/run-*.log，"
              "确认 _push_b 是否被调用、是否抛异常。")
        return "应推未推 — 查看运行日志"
    print()
    print(f"{NO} 结论：未推 B 的原因 — {'; '.join(reasons)}")
    return "; ".join(reasons)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    refund_id = argv[1].strip()
    try:
        diagnose(refund_id)
        return 0
    except Exception as e:
        print(f"{NO} 诊断脚本异常：{e!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
