#!/usr/bin/env python3
"""
三角洲行动 2026 主播巅峰赛排行榜定时抓取与图表报告。

默认抓取腾讯活动页未登录排行榜接口：
https://df.qq.com/cp/a20260611dfs/index.html

用法：
  python3 df_rank_monitor.py once
  python3 df_rank_monitor.py watch --interval 300
  python3 df_rank_monitor.py serve --host 127.0.0.1 --port 8765
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ACT_ID = "17_GK5XIm"
FLOW_ID = "570030"
TOKEN = "H0A9F4"
API_URL = "https://dfm.ams.game.qq.com/ide/"
REFERER = "https://df.qq.com/cp/a20260611dfs/index.html"
DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_REPORT = Path(__file__).resolve().parent / "report.html"

PLATFORM_COLORS = {
    "B站": "#00A1D6",
    "斗鱼": "#FF7700",
    "抖音": "#111827",
    "快手": "#FF5000",
    "虎牙": "#F6A21A",
    "小红书": "#FF2442",
}


@dataclass(frozen=True)
class RankItem:
    snapshot_id: int
    fetched_at: str
    source_time: str
    rank: int
    platform: str
    name: str
    live_url: str
    warehouse_raw: str
    warehouse_m: float
    defeated_agents: int
    decrypted_bricks: int
    total_rounds: int


def now_local_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def parse_warehouse_m(value: str) -> float:
    text = (value or "").strip().upper().replace(",", "")
    if not text:
        return 0.0
    multiplier = 1.0
    if text.endswith("M"):
        text = text[:-1]
        multiplier = 1.0
    elif text.endswith("万"):
        text = text[:-1]
        multiplier = 0.01
    elif text.endswith("K"):
        text = text[:-1]
        multiplier = 0.001
    try:
        return float(text) * multiplier
    except ValueError:
        return 0.0


def int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def request_rank_page(page: int, platform: str = "", competition_id: int = 0, rank_type: int = 1, retries: int = 3) -> dict[str, Any]:
    # 参数来自页面 js/act.js 的 rankListAPI；sPlatId 必须是 0，platName 为空才表示所有平台。
    payload = {
        "iChartId": FLOW_ID,
        "iSubChartId": FLOW_ID,
        "sIdeToken": TOKEN,
        "sPlatId": "0",
        "sArea": "36",
        "sPartition": "36",
        "sRoleId": "",
        "type": str(rank_type),
        "page": str(page),
        "search": "",
        "platName": platform,
        "competitionId": str(competition_id),
        "pageNo": "10",
        "iUin": "8939059349175372121",
        "signToken": "",
        "isPreengage": "1",
        "needGopenid": "1",
    }
    body = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; df-rank-monitor/1.0)",
            "Referer": REFERER,
            "Origin": "https://df.qq.com",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", "replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"接口返回不是 JSON：{raw[:300]}") from exc
            if str(data.get("iRet")) != "0":
                raise RuntimeError(f"接口错误 iRet={data.get('iRet')} sMsg={data.get('sMsg')} raw={raw[:500]}")
            jdata = data.get("jData") or data.get("details", {}).get("jData") or {}
            if str(jdata.get("iRet", "0")) != "0":
                raise RuntimeError(f"排行榜错误 iRet={jdata.get('iRet')} sMsg={jdata.get('sMsg')}")
            return jdata
        except Exception as exc:  # noqa: BLE001 - 公开接口偶发 110001，需要退避重试
            last_error = exc
            if attempt < retries:
                time.sleep(1.2 * attempt)
    assert last_error is not None
    raise last_error


def fetch_rankings(max_pages: int = 5, platform: str = "", competition_id: int = 0) -> tuple[list[dict[str, Any]], str]:
    rows: list[dict[str, Any]] = []
    source_time = ""
    total_pages = 1
    for page in range(1, max_pages + 1):
        jdata = request_rank_page(page, platform=platform, competition_id=competition_id, rank_type=1)
        if not source_time:
            source_time = str(jdata.get("curDateTime") or "")
        if page == 1:
            total_pages = max(1, int_or_zero(jdata.get("totalPage")) or 1)
        page_rows = jdata.get("sqlData") or []
        if not isinstance(page_rows, list):
            page_rows = []
        rows.extend(page_rows)
        if page >= total_pages or not page_rows:
            break
        time.sleep(0.35)
    return rows, source_time


def connect_db(data_dir: Path) -> sqlite3.Connection:
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / "rank_history.sqlite3")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            source_time TEXT NOT NULL,
            platform_filter TEXT NOT NULL,
            competition_id INTEGER NOT NULL,
            row_count INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rank_rows (
            snapshot_id INTEGER NOT NULL,
            rank INTEGER NOT NULL,
            rankdid INTEGER,
            open_id TEXT,
            platform TEXT NOT NULL,
            name TEXT NOT NULL,
            live_url TEXT,
            warehouse_raw TEXT NOT NULL,
            warehouse_m REAL NOT NULL,
            defeated_agents INTEGER NOT NULL,
            decrypted_bricks INTEGER NOT NULL,
            total_rounds INTEGER NOT NULL,
            PRIMARY KEY (snapshot_id, rank),
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
        )
        """
    )
    return conn


def store_snapshot(conn: sqlite3.Connection, rows: list[dict[str, Any]], source_time: str, platform: str, competition_id: int) -> int:
    fetched_at = now_local_iso()
    with conn:
        cur = conn.execute(
            "INSERT INTO snapshots(fetched_at, source_time, platform_filter, competition_id, row_count) VALUES (?, ?, ?, ?, ?)",
            (fetched_at, source_time, platform, competition_id, len(rows)),
        )
        snapshot_id = int(cur.lastrowid)
        for row in rows:
            rank = int_or_zero(row.get("rankwid")) or int_or_zero(row.get("rank"))
            warehouse_raw = str(row.get("warehouseValue") or "")
            conn.execute(
                """
                INSERT INTO rank_rows(
                    snapshot_id, rank, rankdid, open_id, platform, name, live_url,
                    warehouse_raw, warehouse_m, defeated_agents, decrypted_bricks, total_rounds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    rank,
                    int_or_zero(row.get("rankdid")),
                    str(row.get("openId") or ""),
                    str(row.get("platName") or ""),
                    str(row.get("userName") or ""),
                    str(row.get("liveUrl") or ""),
                    warehouse_raw,
                    parse_warehouse_m(warehouse_raw),
                    int_or_zero(row.get("defeatedAgents")),
                    int_or_zero(row.get("decryptedBricks")),
                    int_or_zero(row.get("totalRounds")),
                ),
            )
    return snapshot_id


def latest_snapshot_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    return int(row[0]) if row else None


def load_items(conn: sqlite3.Connection, snapshot_id: int | None = None) -> list[RankItem]:
    if snapshot_id is None:
        snapshot_id = latest_snapshot_id(conn)
    if snapshot_id is None:
        return []
    rows = conn.execute(
        """
        SELECT r.snapshot_id, s.fetched_at, s.source_time, r.rank, r.platform, r.name, r.live_url,
               r.warehouse_raw, r.warehouse_m, r.defeated_agents, r.decrypted_bricks, r.total_rounds
        FROM rank_rows r
        JOIN snapshots s ON s.id = r.snapshot_id
        WHERE r.snapshot_id = ?
        ORDER BY r.rank ASC
        """,
        (snapshot_id,),
    ).fetchall()
    return [RankItem(*row) for row in rows]


def load_history(conn: sqlite3.Connection, names: Iterable[str], limit: int = 24) -> dict[str, list[tuple[str, float, int]]]:
    result: dict[str, list[tuple[str, float, int]]] = {}
    for name in names:
        rows = conn.execute(
            """
            SELECT s.source_time, r.warehouse_m, r.rank
            FROM rank_rows r
            JOIN snapshots s ON s.id = r.snapshot_id
            WHERE r.name = ?
            ORDER BY s.id DESC
            LIMIT ?
            """,
            (name, limit),
        ).fetchall()
        result[name] = list(reversed([(str(t), float(v), int(rank)) for t, v, rank in rows]))
    return result


def sparkline(points: list[tuple[str, float, int]], width: int = 120, height: int = 28) -> str:
    if len(points) < 2:
        return "<span class='muted'>暂无趋势</span>"
    values = [p[1] for p in points]
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    coords = []
    for i, value in enumerate(values):
        x = i * width / (len(values) - 1)
        y = height - ((value - lo) / span) * (height - 4) - 2
        coords.append(f"{x:.1f},{y:.1f}")
    delta = values[-1] - values[0]
    klass = "up" if delta >= 0 else "down"
    return (
        f"<svg class='spark {klass}' width='{width}' height='{height}' viewBox='0 0 {width} {height}' aria-label='趋势'>"
        f"<polyline points='{' '.join(coords)}' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/>"
        f"</svg>"
    )


def bar_chart(items: list[RankItem], top_n: int = 15) -> str:
    top = items[:top_n]
    max_value = max((item.warehouse_m for item in top), default=1.0)
    out = ["<div class='bar-chart'>"]
    for item in top:
        pct = max(2.0, item.warehouse_m / max_value * 100)
        out.append(
            "<div class='bar-row'>"
            f"<div class='bar-label'><span class='rank'>#{item.rank}</span>{html.escape(item.name)}<span class='plat'>{html.escape(item.platform)}</span></div>"
            f"<div class='bar-track'><div class='bar-fill' style='width:{pct:.2f}%;'></div></div>"
            f"<div class='bar-value'>{item.warehouse_m:.2f}M</div>"
            "</div>"
        )
    out.append("</div>")
    return "\n".join(out)


def platform_summary(items: list[RankItem]) -> str:
    counts: dict[str, int] = {}
    value_sum: dict[str, float] = {}
    for item in items:
        counts[item.platform] = counts.get(item.platform, 0) + 1
        value_sum[item.platform] = value_sum.get(item.platform, 0.0) + item.warehouse_m
    total = sum(counts.values()) or 1
    rows = []
    for platform, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        color = PLATFORM_COLORS.get(platform, "#64748B")
        pct = count / total * 100
        rows.append(
            f"<div class='plat-row'><span class='dot' style='background:{color}'></span>"
            f"<span>{html.escape(platform)}</span><strong>{count} 人</strong>"
            f"<em>{pct:.0f}% / 均值 {value_sum[platform] / count:.2f}M</em></div>"
        )
    return "\n".join(rows)


def render_report(conn: sqlite3.Connection, output: Path) -> None:
    items = load_items(conn)
    if not items:
        raise RuntimeError("没有可展示的数据，请先执行 once 抓取。")
    top_names = [item.name for item in items[:10]]
    history = load_history(conn, top_names)
    latest = items[0]
    total_value = sum(item.warehouse_m for item in items)
    avg_value = total_value / len(items)
    max_kills = max((item.defeated_agents for item in items), default=0)
    generated_at = now_local_iso()

    table_rows = []
    for item in items[:50]:
        trend = sparkline(history.get(item.name, []))
        safe_url = html.escape(item.live_url, quote=True)
        safe_name = html.escape(item.name)
        link = f"<a href='{safe_url}' target='_blank' rel='noreferrer'>{safe_name}</a>" if item.live_url else safe_name
        table_rows.append(
            "<tr>"
            f"<td class='num'>#{item.rank}</td>"
            f"<td><span class='pill'>{html.escape(item.platform)}</span></td>"
            f"<td class='name'>{link}</td>"
            f"<td class='num strong'>{item.warehouse_m:.2f}M</td>"
            f"<td class='num'>{item.defeated_agents}</td>"
            f"<td class='num'>{item.decrypted_bricks}</td>"
            f"<td class='num'>{item.total_rounds}</td>"
            f"<td>{trend}</td>"
            "</tr>"
        )

    doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>三角洲主播巅峰赛排行榜监控</title>
<style>
:root {{ color-scheme: light; --bg:#f6f7fb; --card:#ffffff; --text:#172033; --muted:#667085; --line:#e6e8ef; --accent:#4f46e5; --accent2:#06b6d4; --good:#16a34a; --bad:#dc2626; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:linear-gradient(180deg,#eef2ff 0,#f8fafc 280px); color:var(--text); font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
a {{ color:#2563eb; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
.wrap {{ max-width:1180px; margin:0 auto; padding:28px 18px 42px; }}
.hero {{ display:flex; justify-content:space-between; gap:20px; align-items:flex-end; margin-bottom:22px; }}
h1 {{ margin:0 0 8px; font-size:30px; letter-spacing:-.02em; }}
.sub {{ color:var(--muted); }}
.cards {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin:20px 0; }}
.card {{ background:rgba(255,255,255,.86); border:1px solid var(--line); border-radius:18px; padding:16px; box-shadow:0 10px 30px rgba(15,23,42,.06); backdrop-filter:blur(8px); }}
.card .k {{ color:var(--muted); font-size:13px; }} .card .v {{ margin-top:6px; font-size:26px; font-weight:800; }}
.grid {{ display:grid; grid-template-columns:1.7fr .9fr; gap:16px; align-items:start; }}
.section-title {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; }}
.section-title h2 {{ margin:0; font-size:18px; }}
.bar-chart {{ display:grid; gap:10px; }}
.bar-row {{ display:grid; grid-template-columns:220px 1fr 70px; gap:10px; align-items:center; }}
.bar-label {{ overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }} .rank {{ display:inline-block; width:34px; color:var(--muted); }} .plat {{ margin-left:8px; color:var(--muted); font-size:12px; }}
.bar-track {{ height:14px; background:#eef2ff; border-radius:999px; overflow:hidden; }} .bar-fill {{ height:100%; border-radius:999px; background:linear-gradient(90deg,var(--accent),var(--accent2)); }} .bar-value {{ text-align:right; font-variant-numeric:tabular-nums; font-weight:700; }}
.plat-row {{ display:grid; grid-template-columns:18px 1fr auto; gap:8px; align-items:center; padding:9px 0; border-bottom:1px dashed var(--line); }} .plat-row em {{ grid-column:2/4; color:var(--muted); font-style:normal; font-size:12px; }} .dot {{ width:10px; height:10px; border-radius:50%; }}
table {{ width:100%; border-collapse:separate; border-spacing:0; overflow:hidden; border:1px solid var(--line); border-radius:16px; background:var(--card); }}
th,td {{ padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:middle; }} th {{ text-align:left; color:var(--muted); font-size:12px; background:#f8fafc; }} tr:last-child td {{ border-bottom:0; }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; }} .strong {{ font-weight:800; }} .name {{ min-width:190px; }}
.pill {{ display:inline-flex; padding:3px 8px; border-radius:999px; background:#f1f5f9; color:#334155; font-size:12px; }}
.spark {{ display:block; }} .spark.up {{ color:var(--good); }} .spark.down {{ color:var(--bad); }} .muted {{ color:var(--muted); }}
.footer {{ margin-top:18px; color:var(--muted); font-size:12px; }}
@media (max-width:900px) {{ .cards,.grid {{ grid-template-columns:1fr; }} .hero {{ display:block; }} .bar-row {{ grid-template-columns:1fr; }} .bar-value {{ text-align:left; }} table {{ font-size:12px; }} th,td {{ padding:8px; }} }}
</style>
</head>
<body>
<div class="wrap">
  <div class="hero">
    <div>
      <h1>三角洲行动 2026 主播巅峰赛排行榜</h1>
      <div class="sub">数据源时间：{html.escape(latest.source_time)}；本地抓取：{html.escape(latest.fetched_at)}；报告生成：{html.escape(generated_at)}</div>
    </div>
    <div class="sub">数据来自腾讯活动页公开未登录排行榜接口</div>
  </div>

  <div class="cards">
    <div class="card"><div class="k">当前第一</div><div class="v">{html.escape(latest.name)}</div><div class="sub">{html.escape(latest.platform)} · {latest.warehouse_m:.2f}M</div></div>
    <div class="card"><div class="k">已抓取人数</div><div class="v">{len(items)}</div><div class="sub">默认最多前 50 名展示</div></div>
    <div class="card"><div class="k">平均仓库价值</div><div class="v">{avg_value:.2f}M</div><div class="sub">按本次抓取样本计算</div></div>
    <div class="card"><div class="k">最高击败数</div><div class="v">{max_kills}</div><div class="sub">击败干员数</div></div>
  </div>

  <div class="grid">
    <section class="card">
      <div class="section-title"><h2>Top 15 仓库价值横向对比</h2><span class="sub">越长代表仓库总价值越高</span></div>
      {bar_chart(items, 15)}
    </section>
    <section class="card">
      <div class="section-title"><h2>平台分布</h2><span class="sub">人数 / 均值</span></div>
      {platform_summary(items)}
    </section>
  </div>

  <section class="card" style="margin-top:16px; overflow:auto;">
    <div class="section-title"><h2>排行榜明细与趋势</h2><span class="sub">趋势线来自本地历史快照；刚开始抓取时会显示“暂无趋势”</span></div>
    <table>
      <thead><tr><th class="num">排名</th><th>平台</th><th>选手</th><th class="num">仓库价值</th><th class="num">击败</th><th class="num">破译砖</th><th class="num">局数</th><th>趋势</th></tr></thead>
      <tbody>{''.join(table_rows)}</tbody>
    </table>
  </section>

  <div class="footer">定时运行示例：<code>python3 {html.escape(str(Path(__file__).resolve()))} watch --interval 300</code>。每次抓取会追加到 SQLite，并重写本 HTML 报告。</div>
</div>
</body>
</html>"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(doc, encoding="utf-8")


def run_once(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    report = Path(args.output)
    conn = connect_db(data_dir)
    rows, source_time = fetch_rankings(max_pages=args.pages, platform=args.platform, competition_id=args.competition_id)
    snapshot_id = store_snapshot(conn, rows, source_time, args.platform, args.competition_id)
    render_report(conn, report)
    print(f"OK snapshot_id={snapshot_id} rows={len(rows)} source_time={source_time} report={report}")
    return 0


def run_watch(args: argparse.Namespace) -> int:
    print(f"开始定时抓取：interval={args.interval}s pages={args.pages} output={args.output}")
    while True:
        try:
            run_once(args)
        except Exception as exc:  # noqa: BLE001 - 定时进程需要不中断地记录错误
            print(f"ERROR {now_local_iso()} {exc}", file=sys.stderr)
        time.sleep(args.interval)


def run_serve(args: argparse.Namespace) -> int:
    import http.server
    import socketserver

    report_dir = Path(args.output).resolve().parent
    os.chdir(report_dir)
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer((args.host, args.port), handler) as httpd:
        print(f"服务已启动：http://{args.host}:{args.port}/{Path(args.output).name}")
        httpd.serve_forever()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="三角洲行动主播巅峰赛排行榜抓取与 HTML 图表报告")
    parser.add_argument("command", choices=["once", "watch", "serve"], help="once=抓一次；watch=循环定时抓；serve=启动本地静态网页服务")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="SQLite 历史数据目录")
    parser.add_argument("--output", default=str(DEFAULT_REPORT), help="HTML 报告输出路径")
    parser.add_argument("--pages", type=int, default=5, help="每次最多抓取页数，每页 10 条")
    parser.add_argument("--platform", default="", help="平台筛选，空字符串表示所有平台；可填 B站/斗鱼/抖音/快手/虎牙/小红书")
    parser.add_argument("--competition-id", type=int, default=0, help="轮次：0=页面默认最新；1/3/5=指定阶段；6=全周期，按官方接口限制可用")
    parser.add_argument("--interval", type=int, default=300, help="watch 模式抓取间隔秒数")
    parser.add_argument("--host", default="127.0.0.1", help="serve 监听地址")
    parser.add_argument("--port", type=int, default=8765, help="serve 监听端口")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "once":
        return run_once(args)
    if args.command == "watch":
        return run_watch(args)
    if args.command == "serve":
        return run_serve(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
