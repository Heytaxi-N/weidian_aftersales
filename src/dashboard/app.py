"""本地按需启的售后管理面板。

启动：./scripts/dashboard.sh  → http://localhost:8765
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from jinja2 import DictLoader, Environment, select_autoescape

from src.config import DASHBOARD_PORT, ROOT, TIMEZONE
from src.db import get_conn
from src.rules.engine import PENDING_STATUSES, TIER_URGENT_HOURS, TIER_WARN_HOURS

app = FastAPI(title="微店售后管理")

BASE_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif;
       max-width: 1100px; margin: 24px auto; padding: 0 16px; color: #222; }
h1 { border-bottom: 2px solid #eee; padding-bottom: 8px; }
nav a { margin-right: 16px; color: #2563eb; text-decoration: none; }
nav a.active { font-weight: 600; color: #111; }
table { border-collapse: collapse; width: 100%; font-size: 14px; margin-top: 16px; }
th, td { border-bottom: 1px solid #eee; padding: 8px 6px; text-align: left; vertical-align: top; }
th { background: #f9fafb; font-weight: 600; }
tr:hover { background: #fafafa; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; }
.tag-warn { background: #fef3c7; color: #92400e; }
.tag-urgent { background: #fee2e2; color: #991b1b; }
.tag-b { background: #dbeafe; color: #1e40af; }
.tag-done { background: #d1fae5; color: #065f46; }
.tag-pending { background: #f3f4f6; color: #374151; }
.btn { background: #2563eb; color: white; border: none; padding: 8px 16px;
       border-radius: 6px; cursor: pointer; font-size: 14px; }
.btn:hover { background: #1d4ed8; }
.btn-secondary { background: #6b7280; }
small.muted { color: #6b7280; font-size: 12px; }
img.shot { max-width: 360px; border: 1px solid #ddd; border-radius: 4px; }
form.filters { margin: 16px 0; padding: 12px; background: #f9fafb; border-radius: 6px; }
form.filters select, form.filters input { margin-right: 8px; padding: 4px 8px; }
"""

LAYOUT = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>{{ title }} - 售后管理</title>
<style>{{ css }}</style></head><body>
<h1>售后管理</h1>
<nav>
  <a href="/" class="{{ 'active' if active=='index' else '' }}">当前待处理</a>
  <a href="/pushed" class="{{ 'active' if active=='pushed' else '' }}">推送历史</a>
  <form action="/refresh" method="post" style="display:inline">
    <button class="btn" type="submit">🔄 立即抓取</button>
  </form>
  <small class="muted">最后抓取：{{ last_run or '从未' }}</small>
</nav>
{{ body|safe }}
</body></html>"""

INDEX_BODY = """
<p>当前快照（{{ snapshot_at or '无' }}）中所有待商家状态的退款单：<b>{{ rows|length }}</b> 条</p>
<table>
<tr><th>退款单号</th><th>订单</th><th>类型</th><th>状态</th><th>截止</th><th>剩余</th><th>买家</th><th>运单号</th></tr>
{% for r in rows %}
<tr>
  <td><a href="/refund/{{ r.refund_id }}"><code>{{ r.refund_id }}</code></a></td>
  <td>{{ r.order_id }}</td>
  <td>{{ r.refund_type }}</td>
  <td>{{ r.status }}</td>
  <td><small>{{ r.deadline_at or '—' }}</small></td>
  <td>
    {% if r.hours_left is not none %}
      {% if r.hours_left <= 24 %}<span class="tag tag-urgent">{{ '%.1f'|format(r.hours_left) }}h</span>
      {% elif r.hours_left <= 48 %}<span class="tag tag-warn">{{ '%.1f'|format(r.hours_left) }}h</span>
      {% else %}{{ '%.1f'|format(r.hours_left) }}h{% endif %}
    {% else %}—{% endif %}
  </td>
  <td>{{ r.buyer_name or '—' }} {{ r.buyer_phone or '' }}</td>
  <td>{{ r.return_tracking_no or '—' }}</td>
</tr>
{% endfor %}
</table>
"""

PUSHED_BODY = """
<form class="filters" method="get">
  场景：
  <select name="scenario">
    <option value="">全部</option>
    <option value="A" {% if filter_scn=='A' %}selected{% endif %}>A 临期</option>
    <option value="A2" {% if filter_scn=='A2' %}selected{% endif %}>A2 紧急</option>
    <option value="B" {% if filter_scn=='B' %}selected{% endif %}>B 已签收</option>
  </select>
  状态：
  <select name="state">
    <option value="">全部</option>
    <option value="pending" {% if filter_state=='pending' %}selected{% endif %}>仍未处理</option>
    <option value="done" {% if filter_state=='done' %}selected{% endif %}>已处理</option>
  </select>
  <button class="btn btn-secondary" type="submit">筛选</button>
</form>

<p>共 <b>{{ rows|length }}</b> 条推送记录</p>
<table>
<tr><th>时间</th><th>退款单</th><th>场景</th><th>处理状态</th></tr>
{% for r in rows %}
<tr>
  <td><small>{{ r.pushed_at }}</small></td>
  <td><a href="/refund/{{ r.refund_id }}"><code>{{ r.refund_id }}</code></a></td>
  <td>
    {% if r.scenario=='A2' %}<span class="tag tag-urgent">A2 紧急</span>
    {% elif r.scenario=='A' %}<span class="tag tag-warn">A 临期</span>
    {% else %}<span class="tag tag-b">B 已签收</span>{% endif %}
  </td>
  <td>
    {% if r.is_handled %}<span class="tag tag-done">已处理</span>
    {% else %}<span class="tag tag-pending">未处理</span>{% endif %}
  </td>
</tr>
{% endfor %}
</table>
"""

REFUND_BODY = """
<p><a href="/">← 返回</a></p>
<h2>退款单 <code>{{ refund_id }}</code></h2>

<h3>推送记录</h3>
{% if pushes %}
<table>
<tr><th>时间</th><th>场景</th><th>消息</th><th>截图</th></tr>
{% for p in pushes %}
<tr>
  <td><small>{{ p.pushed_at }}</small></td>
  <td>
    {% if p.scenario=='A2' %}<span class="tag tag-urgent">A2</span>
    {% elif p.scenario=='A' %}<span class="tag tag-warn">A</span>
    {% else %}<span class="tag tag-b">B</span>{% endif %}
  </td>
  <td><pre style="white-space:pre-wrap;font-size:12px;max-width:500px">{{ p.message_text }}</pre></td>
  <td>{% if p.screenshot_path %}<a href="/screenshot/{{ p.refund_id }}/{{ p.scenario }}">看图</a>{% endif %}</td>
</tr>
{% endfor %}
</table>
{% else %}<p>无推送记录</p>{% endif %}

<h3>快照时间线（共 {{ snapshots|length }} 条）</h3>
<table>
<tr><th>快照时间</th><th>状态</th><th>截止</th><th>运单</th></tr>
{% for s in snapshots %}
<tr>
  <td><small>{{ s.snapshot_at }}</small></td>
  <td>{{ s.status }}</td>
  <td><small>{{ s.deadline_at or '—' }}</small></td>
  <td>{{ s.return_tracking_no or '—' }}</td>
</tr>
{% endfor %}
</table>
"""

j2 = Environment(loader=DictLoader({
    "layout": LAYOUT,
    "index": INDEX_BODY,
    "pushed": PUSHED_BODY,
    "refund": REFUND_BODY,
}), autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)


def _layout(title: str, body_template: str, active: str, **ctx) -> str:
    body = j2.get_template(body_template).render(**ctx)
    with get_conn() as conn:
        row = conn.execute("SELECT started_at FROM run_log ORDER BY started_at DESC LIMIT 1").fetchone()
    last_run = row["started_at"] if row else None
    return j2.get_template("layout").render(
        title=title, body=body, active=active, css=BASE_CSS, last_run=last_run,
    )


@app.get("/", response_class=HTMLResponse)
def index():
    now = datetime.now(TIMEZONE)
    with get_conn() as conn:
        latest = conn.execute("SELECT MAX(snapshot_at) AS s FROM order_snapshots").fetchone()
        snapshot_at = latest["s"] if latest else None
        rows = []
        if snapshot_at:
            for r in conn.execute(
                "SELECT * FROM order_snapshots WHERE snapshot_at = ? ORDER BY deadline_at",
                (snapshot_at,),
            ):
                if r["status"] not in PENDING_STATUSES:
                    continue
                hours_left = None
                if r["deadline_at"]:
                    try:
                        dt = datetime.fromisoformat(r["deadline_at"])
                        hours_left = (dt - now).total_seconds() / 3600
                    except ValueError:
                        pass
                rows.append({**dict(r), "hours_left": hours_left})
    return _layout("当前待处理", "index", "index", snapshot_at=snapshot_at, rows=rows)


@app.get("/pushed", response_class=HTMLResponse)
def pushed(scenario: str = "", state: str = ""):
    sql = "SELECT * FROM pushed_records WHERE 1=1"
    params: list = []
    if scenario:
        sql += " AND scenario = ?"
        params.append(scenario)
    sql += " ORDER BY pushed_at DESC"
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params)]
        latest = conn.execute("SELECT MAX(snapshot_at) AS s FROM order_snapshots").fetchone()
        snapshot_at = latest["s"] if latest else None
        pending_now: set[str] = set()
        if snapshot_at:
            for r in conn.execute(
                "SELECT refund_id FROM order_snapshots WHERE snapshot_at = ? AND status IN (?,?)",
                (snapshot_at, *PENDING_STATUSES),
            ):
                pending_now.add(r["refund_id"])
    for r in rows:
        r["is_handled"] = r["refund_id"] not in pending_now
    if state == "pending":
        rows = [r for r in rows if not r["is_handled"]]
    elif state == "done":
        rows = [r for r in rows if r["is_handled"]]
    return _layout("推送历史", "pushed", "pushed", rows=rows,
                   filter_scn=scenario, filter_state=state)


@app.get("/refund/{refund_id}", response_class=HTMLResponse)
def refund_detail(refund_id: str):
    with get_conn() as conn:
        pushes = [dict(r) for r in conn.execute(
            "SELECT * FROM pushed_records WHERE refund_id = ? ORDER BY pushed_at",
            (refund_id,),
        )]
        snapshots = [dict(r) for r in conn.execute(
            "SELECT * FROM order_snapshots WHERE refund_id = ? ORDER BY snapshot_at",
            (refund_id,),
        )]
    return _layout(f"退款 {refund_id}", "refund", "",
                   refund_id=refund_id, pushes=pushes, snapshots=snapshots)


@app.get("/screenshot/{refund_id}/{scenario}")
def screenshot(refund_id: str, scenario: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT screenshot_path FROM pushed_records WHERE refund_id = ? AND scenario = ?",
            (refund_id, scenario),
        ).fetchone()
    if not row or not row["screenshot_path"]:
        return HTMLResponse("无截图", status_code=404)
    p = Path(row["screenshot_path"])
    if not p.exists():
        return HTMLResponse("截图文件已删除", status_code=404)
    return FileResponse(p)


@app.post("/refresh")
def refresh():
    subprocess.Popen(
        [sys.executable, "-m", "src.runner"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return RedirectResponse("/", status_code=303)


def serve():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=DASHBOARD_PORT)


if __name__ == "__main__":
    serve()
