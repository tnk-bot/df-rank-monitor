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


def fetch_rankings(max_pages: int = 0, platform: str = "", competition_id: int = 0) -> tuple[list[dict[str, Any]], str, int]:
    """抓取排行榜。max_pages <= 0 表示不分页上限，跟随接口 totalPage 抓全量。"""
    rows: list[dict[str, Any]] = []
    source_time = ""
    total_pages = 1
    page = 0
    while True:
        page += 1
        if max_pages > 0 and page > max_pages:
            break
        jdata = request_rank_page(page, platform=platform, competition_id=competition_id, rank_type=1)
        if not source_time:
            source_time = str(jdata.get("curDateTime") or "")
        if page == 1:
            total_pages = max(1, int_or_zero(jdata.get("totalPage")) or 1)
        page_rows = jdata.get("sqlData") or []
        if not isinstance(page_rows, list):
            page_rows = []
        rows.extend(page_rows)
        if not page_rows or page >= total_pages:
            break
        time.sleep(0.35)
    return rows, source_time, total_pages


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


def load_recent_window(conn: sqlite3.Connection, since_iso: str) -> dict[str, list[tuple[str, float, int]]]:
    """返回 {name: [(fetched_at, warehouse_m, rank) ...]}，按 fetch 时间升序，仅含 fetched_at>=since 的快照。"""
    out: dict[str, list[tuple[str, float, int]]] = {}
    cur = conn.execute(
        """
        SELECT r.name, s.fetched_at, r.warehouse_m, r.rank
        FROM rank_rows r
        JOIN snapshots s ON s.id = r.snapshot_id
        WHERE s.fetched_at >= ?
        ORDER BY s.fetched_at ASC, r.rank ASC
        """,
        (since_iso,),
    )
    for name, fetched_at, value, rank in cur.fetchall():
        out.setdefault(name, []).append((str(fetched_at), float(value), int(rank)))
    return out


def _two_series_paths(
    points: list[tuple[str, float, int]],
    width: int,
    height: int,
    pad: int = 4,
) -> tuple[str, str, str, bool]:
    """返回 (value_poly, rank_poly, axis_state, has_data)。"""
    if len(points) < 2:
        return "", "", "", False
    values = [p[1] for p in points]
    ranks = [p[2] for p in points]
    v_lo, v_hi = min(values), max(values)
    v_span = v_hi - v_lo or 1.0
    r_lo, r_hi = min(ranks), max(ranks)
    r_span = r_hi - r_lo or 1
    inner_w = max(1, width - pad * 2)
    inner_h = max(1, height - pad * 2)
    value_coords, rank_coords = [], []
    for i, value in enumerate(values):
        x = pad + (i / (len(points) - 1)) * inner_w
        y_v = pad + (1 - (value - v_lo) / v_span) * inner_h
        value_coords.append(f"{x:.1f},{y_v:.1f}")
    for i, rank in enumerate(ranks):
        x = pad + (i / (len(points) - 1)) * inner_w
        y_r = pad + (rank - r_lo) / r_span * inner_h
        rank_coords.append(f"{x:.1f},{y_r:.1f}")
    return (
        " ".join(value_coords),
        " ".join(rank_coords),
        f"{v_lo:.2f}-{v_hi:.2f}M / #{int(r_lo)}-{int(r_hi)}",
        True,
    )


def sparkline(points: list[tuple[str, float, int]], width: int = 140, height: int = 30) -> str:
    value_poly, rank_poly, axis_state, has_data = _two_series_paths(points, width, height)
    if not has_data:
        return "<span class='muted'>暂无趋势</span>"
    delta_v = points[-1][1] - points[0][1]
    klass = "up" if delta_v >= 0 else "down"
    return (
        f"<svg class='spark {klass}' width='{width}' height='{height}' viewBox='0 0 {width} {height}' "
        f"aria-label='趋势 {html.escape(axis_state)}'>"
        f"<polyline class='spark-rank' points='{rank_poly}' fill='none' stroke='currentColor' "
        f"stroke-width='1' stroke-dasharray='2,2' stroke-linecap='round' stroke-linejoin='round' opacity='.55'/>"
        f"<polyline class='spark-value' points='{value_poly}' fill='none' stroke='currentColor' "
        f"stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/>"
        f"</svg>"
    )


def bar_chart(items: list[RankItem], top_n: int = 15) -> str:
    """保留为工具函数；当前报告不再使用。"""
    top = items[:top_n]
    if not top:
        return ""
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
    """保留为工具函数；当前报告不再使用。"""
    counts: dict[str, int] = {}
    value_sum: dict[str, float] = {}
    for item in items:
        counts[item.platform] = counts.get(item.platform, 0) + 1
        value_sum[item.platform] = value_sum.get(item.platform, 0.0) + item.warehouse_m
    rows = []
    for platform, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        color = PLATFORM_COLORS.get(platform, "#64748B")
        pct = count / max(1, len(items)) * 100
        rows.append(
            f"<div class='plat-row'><span class='dot' style='background:{color}'></span>"
            f"<span>{html.escape(platform)}</span><strong>{count} 人</strong>"
            f"<em>{pct:.0f}% / 均值 {value_sum[platform] / count:.2f}M</em></div>"
        )
    return "\n".join(rows)


def search_keyword(item: RankItem) -> str:
    """对前端搜索可见的字段拼成的文本。前端脚本会用 row.textContent 做匹配，这里仅作占位说明。"""
    return f"{item.name} {item.platform} #{item.rank}"


def render_report(conn: sqlite3.Connection, output: Path) -> None:
    items = load_items(conn)
    if not items:
        raise RuntimeError("没有可展示的数据，请先执行 once 抓取。")
    all_names = [item.name for item in items]
    history = load_history(conn, all_names)
    latest = items[0]
    total_value = sum(item.warehouse_m for item in items)
    avg_value = total_value / len(items)
    max_kills = max((item.defeated_agents for item in items), default=0)
    generated_at = now_local_iso()

    # 解析 latest.fetched_at -> 1 小时前，作为 SQLite 比较参数
    try:
        window_start_dt = dt.datetime.fromisoformat(latest.fetched_at) - dt.timedelta(hours=1)
    except Exception:
        window_start_dt = (dt.datetime.now().astimezone() - dt.timedelta(hours=1))
    window_start_iso = window_start_dt.isoformat(timespec="seconds")
    recent = load_recent_window(conn, window_start_iso)
    snapshot_count_in_window = len({
        ts for points in recent.values() for ts, _v, _r in points
    })
    # 序列化为紧凑 JSON，前端按需渲染大图
    trend_payload = {
        "window_start": window_start_iso,
        "window_end": latest.fetched_at,
        "snapshot_count_in_window": snapshot_count_in_window,
        "players": {
            name: [
                [ts, round(value, 4), rank]
                for ts, value, rank in points
            ]
            for name, points in recent.items()
        },
    }
    trend_json = json.dumps(trend_payload, ensure_ascii=False, separators=(",", ":"))

    table_rows: list[str] = []
    for idx, item in enumerate(items):
        trend = sparkline(history.get(item.name, []))
        safe_url = html.escape(item.live_url, quote=True)
        safe_name = html.escape(item.name)
        safe_name_attr = html.escape(item.name, quote=True)
        link = f"<a href='{safe_url}' target='_blank' rel='noreferrer'>{safe_name}</a>" if item.live_url else safe_name
        keyword_text = f"{item.name} {item.platform} #{item.rank}".lower()
        table_rows.append(
            "<tr class='data-row' "
            f"data-player-idx='{idx}' data-player-name='{safe_name_attr}' data-keywords='{html.escape(keyword_text, quote=True)}'>"
            f"<td class='num expand-cell'>"
            f"<button type='button' class='expand-btn' aria-label='展开趋势' aria-expanded='false' "
            f"data-player-name='{safe_name_attr}'>▸</button>"
            f"</td>"
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
.hero {{ display:flex; justify-content:space-between; gap:20px; align-items:flex-end; margin-bottom:22px; flex-wrap:wrap; }}
h1 {{ margin:0 0 8px; font-size:30px; letter-spacing:-.02em; }}
.sub {{ color:var(--muted); }}
.cards {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin:20px 0; }}
.card {{ background:rgba(255,255,255,.86); border:1px solid var(--line); border-radius:18px; padding:16px; box-shadow:0 10px 30px rgba(15,23,42,.06); backdrop-filter:blur(8px); }}
.card .k {{ color:var(--muted); font-size:13px; }} .card .v {{ margin-top:6px; font-size:26px; font-weight:800; }}
.section-title {{ display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:12px; flex-wrap:wrap; }}
.section-title h2 {{ margin:0; font-size:18px; }}
.search-box {{ display:flex; align-items:center; gap:8px; }}
.search-box input {{ padding:7px 11px; border:1px solid var(--line); border-radius:10px; min-width:200px; font-size:13px; background:#fff; }}
.search-box input:focus {{ outline:2px solid var(--accent); outline-offset:-1px; border-color:var(--accent); }}
.search-box button {{ padding:7px 12px; border:0; border-radius:10px; background:#f1f5f9; color:#334155; font-size:13px; cursor:pointer; }}
.search-box button:hover {{ background:#e2e8f0; }}
.search-box .meta {{ color:var(--muted); font-size:12px; }}
.table-wrap {{ background:var(--card); border:1px solid var(--line); border-radius:16px; overflow:auto; max-height:78vh; }}
table {{ width:100%; border-collapse:separate; border-spacing:0; }}
thead th {{ position:sticky; top:0; z-index:1; }}
th,td {{ padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:middle; }} th {{ text-align:left; color:var(--muted); font-size:12px; background:#f8fafc; }}
tr:last-child td {{ border-bottom:0; }}
.num {{ text-align:right; font-variant-numeric:tabular-nums; }} .strong {{ font-weight:800; }} .name {{ min-width:190px; }}
.pill {{ display:inline-flex; padding:3px 8px; border-radius:999px; background:#f1f5f9; color:#334155; font-size:12px; }}
.spark {{ display:block; }} .spark.up {{ color:var(--good); }} .spark.down {{ color:var(--bad); }} .muted {{ color:var(--muted); }}
.expand-btn {{ width:24px; height:24px; border:1px solid var(--line); border-radius:8px; background:#fff; color:#334155; cursor:pointer; line-height:1; font-size:13px; padding:0; }}
.expand-btn:hover {{ background:#f1f5f9; }}
.expand-btn[aria-expanded="true"] {{ background:#eef2ff; color:var(--accent); transform:rotate(90deg); }}
.expand-cell {{ width:38px; }}
tr.highlight {{ background:rgba(79,70,229,.10); }}
tr.data-row.hidden {{ display:none; }}
tr.expand-row.hidden {{ display:none; }}
.expand-panel {{ padding:16px 18px 18px; background:#fbfcfe; }}
.expand-panel-head {{ display:flex; justify-content:space-between; align-items:flex-end; gap:12px; flex-wrap:wrap; margin-bottom:10px; }}
.expand-panel-head strong {{ font-size:15px; }}
.expand-panel-head .muted {{ font-size:12px; }}
.expand-panel svg {{ width:100%; height:240px; display:block; }}
.expand-panel table {{ margin-top:14px; background:#fff; }}
.legend {{ display:flex; gap:14px; align-items:center; flex-wrap:wrap; }}
.legend .item {{ display:flex; align-items:center; gap:6px; font-size:12px; color:var(--muted); }}
.legend .swatch {{ width:18px; height:3px; border-radius:2px; }}
.legend .swatch.dash {{ background:repeating-linear-gradient(90deg,var(--accent2) 0 6px,#fff 6px 9px); height:2px; border-radius:0; }}
.kpi-row {{ display:flex; gap:14px; flex-wrap:wrap; }}
.kpi {{ background:#fff; border:1px solid var(--line); border-radius:10px; padding:8px 12px; font-size:13px; }}
.kpi b {{ color:var(--accent); }}
.kpi .down b {{ color:var(--bad); }}
.kpi .up b {{ color:var(--good); }}
.footer {{ margin-top:18px; color:var(--muted); font-size:12px; }}
@media (max-width:900px) {{ .cards {{ grid-template-columns:1fr; }} .hero {{ display:block; }} .search-box {{ width:100%; }} .search-box input {{ flex:1; min-width:0; }} table {{ font-size:12px; }} th,td {{ padding:8px; }} .expand-panel svg {{ height:200px; }} }}
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
    <div class="card"><div class="k">已抓取人数</div><div class="v">{len(items)}</div><div class="sub">本次快照样本</div></div>
    <div class="card"><div class="k">平均仓库价值</div><div class="v">{avg_value:.2f}M</div><div class="sub">按本次抓取样本计算</div></div>
    <div class="card"><div class="k">1 小时快照</div><div class="v">{snapshot_count_in_window}</div><div class="sub">用于展开大图</div></div>
  </div>

  <section class="card" style="margin-top:8px;">
    <div class="section-title">
      <h2>排行榜明细与趋势</h2>
      <div class="search-box">
        <span class="meta" id="count_meta">{len(items)} 人</span>
        <input type="search" id="search" placeholder="搜索选手名 / 平台 / 排名，例如：腰子、虎牙、#1" autocomplete="off" spellcheck="false">
        <button type="button" id="clear-btn">清空</button>
      </div>
    </div>
    <div class="sub" style="margin:-4px 0 10px;">每个行点击 ▸ 按钮展开近 1 小时内该选手「仓库价值（实线，左轴）」与「排名（虚线，右轴，越低越好）」的曲线变化。</div>
    <div class="table-wrap">
      <table>
        <thead><tr><th></th><th class="num">排名</th><th>平台</th><th>选手</th><th class="num">仓库价值</th><th class="num">击败</th><th class="num">破译砖</th><th class="num">局数</th><th>趋势</th></tr></thead>
        <tbody id="rank-tbody">{''.join(table_rows)}</tbody>
      </table>
    </div>
  </section>

  <div class="footer">
    定时运行示例：<code>python3 {html.escape(str(Path(__file__).resolve()))} watch --interval 300</code>。每次抓取会覆盖最新快照并重写本 HTML 报告。
  </div>
</div>
<script type="application/json" id="trend-data">{trend_json}</script>
<script>
(function() {{
  const data = JSON.parse(document.getElementById('trend-data').textContent || '{{}}');
  const players = data.players || {{}};
  const windowStart = data.window_start;
  const windowEnd = data.window_end;

  const tbody = document.getElementById('rank-tbody');
  const input = document.getElementById('search');
  const clearBtn = document.getElementById('clear-btn');
  const meta = document.getElementById('count_meta');
  if (!tbody || !input) return;
  const dataRows = Array.from(tbody.querySelectorAll('tr.data-row'));
  const total = dataRows.length;
  const PANEL_CLASS = 'expand-row';

  // —— 搜索过滤 ——
  const normalise = (s) => (s || '').toString().trim().toLowerCase();
  function hidePanel(panel) {{ panel && panel.classList.add('hidden'); }}
  function showPanel(panel) {{ panel && panel.classList.remove('hidden'); }}

  function applyFilter() {{
    const q = normalise(input.value);
    let visible = 0;
    dataRows.forEach((row) => {{
      const kw = row.dataset.keywords || '';
      const match = !q || kw.indexOf(q) !== -1;
      row.classList.toggle('hidden', !match);
      row.classList.remove('highlight');
      const panel = row.nextElementSibling && row.nextElementSibling.classList.contains(PANEL_CLASS)
        ? row.nextElementSibling : null;
      if (match) {{
        visible += 1;
        // 同步面板可见性（在筛选态里，搜索命中的选手若面板被打开，仍可见）
        if (panel && panel.dataset.expanded === '1') showPanel(panel);
      }} else {{
        hidePanel(panel);
      }}
    }});
    meta.textContent = q ? (visible + ' / ' + total + ' 人') : (total + ' 人');
  }}

  function highlightFirst() {{
    const visibleRows = dataRows.filter((r) => !r.classList.contains('hidden'));
    visibleRows.forEach((r) => r.classList.remove('highlight'));
    if (visibleRows.length === 1) {{
      visibleRows[0].classList.add('highlight');
      visibleRows[0].scrollIntoView({{block: 'center'}});
    }}
  }}

  let timer = null;
  input.addEventListener('input', () => {{
    clearTimeout(timer);
    timer = setTimeout(() => {{ applyFilter(); highlightFirst(); }}, 80);
  }});
  input.addEventListener('keydown', (e) => {{
    if (e.key === 'Escape') {{ input.value = ''; applyFilter(); }}
  }});
  clearBtn.addEventListener('click', () => {{
    input.value = ''; applyFilter(); input.focus();
  }});

  // —— 大图渲染 ——
  function pad2(n) {{ return n < 10 ? '0' + n : '' + n; }}
  function formatHHMM(iso) {{
    try {{
      const d = new Date(iso);
      return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
    }} catch (_) {{ return iso; }}
  }}
  function nice(v, step) {{
    if (!isFinite(v)) return '0';
    const fmt = (Math.abs(v) >= 100) ? v.toFixed(0) : (Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2));
    return fmt.replace(/\.0+$/, '');
  }}

  function buildPanel(playerName) {{
    const points = players[playerName] || [];
    const wrap = document.createElement('tr');
    wrap.className = PANEL_CLASS + ' hidden';
    wrap.dataset.expanded = '1';
    wrap.innerHTML = '<td colspan="9" class="expand-panel-cell"><div class="expand-panel">' +
      '<div class="expand-panel-head">' +
      '<strong>' + escapeHtml(playerName) + ' · 近 1 小时</strong>' +
      '<span class="muted">窗口：' + formatHHMM(windowStart) + ' → ' + formatHHMM(windowEnd) + '，共 ' + points.length + ' 次快照</span>' +
      '</div>' +
      '<div class="legend">' +
      '<span class="item"><span class="swatch" style="background:var(--good)"></span>仓库价值（左轴，M）</span>' +
      '<span class="item"><span class="swatch dash"></span>排名（右轴，越低越好）</span>' +
      '</div>' +
      '<div class="kpi-row" data-kpi></div>' +
      '<div data-chart></div>' +
      '<div data-table></div>' +
      '</div></td>';
    renderPanelContent(wrap, points);
    return wrap;
  }}

  function escapeHtml(s) {{
    return (s || '').toString().replace(/[&<>"']/g, (c) => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
  }}

  function renderPanelContent(panel, points) {{
    const kpiBox = panel.querySelector('[data-kpi]');
    const chartBox = panel.querySelector('[data-chart]');
    const tableBox = panel.querySelector('[data-table]');

    if (!points.length) {{
      chartBox.innerHTML = '<div class="muted" style="padding:30px;text-align:center;">近 1 小时尚未抓到该选手快照，请稍候再来。</div>';
      tableBox.innerHTML = '';
      kpiBox.innerHTML = '';
      return;
    }}
    const values = points.map((p) => p[1]);
    const ranks = points.map((p) => p[2]);
    const vDelta = values[values.length - 1] - values[0];
    const rDelta = ranks[0] - ranks[ranks.length - 1]; // 正值：上升（排名数变小=上升）
    const vDeltaLabel = (vDelta >= 0 ? '+' : '') + vDelta.toFixed(2) + 'M';
    const rDeltaLabel = (rDelta > 0 ? '+' : (rDelta < 0 ? '' : '±')) + rDelta + ' 名';

    kpiBox.innerHTML =
      '<div class="kpi">起始：<b>' + values[0].toFixed(2) + 'M / #' + ranks[0] + '</b></div>' +
      '<div class="kpi">最新：<b>' + values[values.length - 1].toFixed(2) + 'M / #' + ranks[ranks.length - 1] + '</b></div>' +
      '<div class="kpi ' + (vDelta >= 0 ? 'up' : 'down') + '">仓库价值 Δ：<b>' + vDeltaLabel + '</b></div>' +
      '<div class="kpi ' + (rDelta > 0 ? 'up' : (rDelta < 0 ? 'down' : '')) + '">排名 Δ：<b>' + rDeltaLabel + '</b></div>';

    // 绘制 SVG 双轴图
    const W = Math.max(360, (chartBox.clientWidth || chartBox.parentNode.clientWidth || 720));
    const H = 240;
    const padL = 46, padR = 46, padT = 14, padB = 30;
    const innerW = W - padL - padR;
    const innerH = H - padT - padB;
    const vMin = Math.min.apply(null, values);
    const vMax = Math.max.apply(null, values);
    const vSpan = (vMax - vMin) || 1;
    const rMinRaw = Math.min.apply(null, ranks);
    const rMaxRaw = Math.max.apply(null, ranks);
    const rSpan = (rMaxRaw - rMinRaw) || 1;
    // 右轴刻度取整：使每个 tick 都是整数 step 的整数倍
    const rAxis = buildIntegerAxis(rMinRaw, rMaxRaw, 4);
    const rMin = rAxis.min;
    const rMax = rAxis.max;
    const n = points.length;
    const xOf = (i) => padL + (n === 1 ? innerW / 2 : (i / (n - 1)) * innerW);
    const yV = (v) => padT + (1 - (v - vMin) / vSpan) * innerH;
    const yR = (r) => padT + ((r - rMin) / (rMax - rMin || 1)) * innerH;

    const vPath = points.map((p, i) => (i ? 'L' : 'M') + xOf(i).toFixed(1) + ',' + yV(p[1]).toFixed(1)).join(' ');
    const rPath = points.map((p, i) => (i ? 'L' : 'M') + xOf(i).toFixed(1) + ',' + yR(p[2]).toFixed(1)).join(' ');

    // 轴 ticks
    const ticks = 4;
    let yGrid = '';
    let leftAxis = '';
    let rightAxis = '';
    for (let t = 0; t <= ticks; t++) {{
      // y 坐标按"等间距"切分（保持视觉均分），但右轴刻度值按整数 step 排
      const ratio = t / ticks;
      const y = padT + ratio * innerH;
      yGrid += '<line x1="' + padL + '" x2="' + (W - padR) + '" y1="' + y + '" y2="' + y + '" stroke="#e5e7eb" stroke-dasharray="2,2"/>';
      const vVal = vMax - ratio * vSpan;
      leftAxis += '<text x="' + (padL - 6) + '" y="' + (y + 4) + '" font-size="11" fill="#16a34a" text-anchor="end">' + nice(vVal) + 'M</text>';
      const rVal = rAxis.min + t * rAxis.step;
      rightAxis += '<text x="' + (W - padR + 6) + '" y="' + (y + 4) + '" font-size="11" fill="#0891b2" text-anchor="start">#' + Math.round(rVal) + '</text>';
    }}
    // x 轴时间标签（首 / 中 / 末）
    const labelIdxs = n <= 3 ? Array.from({{length: n}}, (_, i) => i) : [0, Math.floor((n - 1) / 2), n - 1];
    let xAxis = '';
    labelIdxs.forEach((i) => {{
      xAxis += '<text x="' + xOf(i) + '" y="' + (H - padB + 14) + '" font-size="11" fill="#64748b" text-anchor="middle">' + formatHHMM(points[i][0]) + '</text>';
    }});

    // 节点（默认 + 一个会被 hover 复用的 marker 组）
    let vDots = '';
    let rDots = '';
    points.forEach((p, i) => {{
      const t = p[0];
      vDots += '<g data-i="' + i + '" class="dot-v"><circle cx="' + xOf(i) + '" cy="' + yV(p[1]) + '" r="3" fill="#16a34a"/>' +
        '<title>' + formatHHMM(t) + ' · ' + p[1].toFixed(2) + 'M · #' + p[2] + '</title></g>';
      rDots += '<g data-i="' + i + '" class="dot-r"><circle cx="' + xOf(i) + '" cy="' + yR(p[2]) + '" r="2.5" fill="#0891b2"/>' +
        '<title>' + formatHHMM(t) + ' · ' + p[1].toFixed(2) + 'M · #' + p[2] + '</title></g>';
    }});

    chartBox.innerHTML =
      '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" class="trend-svg">' +
      yGrid + leftAxis + rightAxis +
      '<line x1="' + padL + '" x2="' + padL + '" y1="' + padT + '" y2="' + (H - padB) + '" stroke="#cbd5e1"/>' +
      '<line x1="' + (W - padR) + '" x2="' + (W - padR) + '" y1="' + padT + '" y2="' + (H - padB) + '" stroke="#cbd5e1"/>' +
      xAxis +
      '<g class="hover-layer" pointer-events="none">' +
        '<line class="hover-line" x1="0" x2="0" y1="' + padT + '" y2="' + (H - padB) + '" stroke="#1f2937" stroke-width="1" stroke-dasharray="3,3" opacity="0"/>' +
        '<g class="hover-markers" opacity="0">' +
          '<circle class="hover-v" r="4.5" fill="#16a34a" stroke="#fff" stroke-width="2"/>' +
          '<circle class="hover-r" r="4" fill="#0891b2" stroke="#fff" stroke-width="2"/>' +
        '</g>' +
        '<g class="hover-tip" transform="translate(0,0)" opacity="0">' +
          '<rect x="-58" y="-44" width="116" height="42" rx="8" fill="rgba(15,23,42,.92)"/>' +
          '<text class="ht-time"  x="0" y="-28" font-size="11" fill="#cbd5e1" text-anchor="middle"></text>' +
          '<text class="ht-value" x="0" y="-15" font-size="12" fill="#86efac" text-anchor="middle" font-weight="700"></text>' +
          '<text class="ht-rank"  x="0" y="-2"  font-size="12" fill="#7dd3fc" text-anchor="middle" font-weight="700"></text>' +
        '</g>' +
      '</g>' +
      '<path d="' + rPath + '" fill="none" stroke="#0891b2" stroke-width="1.5" stroke-dasharray="6,4" opacity=".85"/>' +
      '<path d="' + vPath + '" fill="none" stroke="#16a34a" stroke-width="2.2" stroke-linejoin="round"/>' +
      rDots + vDots +
      '</svg>';

    // —— 鼠标悬浮：竖线 + 圆点 + 坐标 tooltip ——
    const svg = chartBox.querySelector('svg');
    const hoverLayer = svg.querySelector('.hover-layer');
    const hoverLine = svg.querySelector('.hover-line');
    const hoverMarkers = svg.querySelector('.hover-markers');
    const hoverV = svg.querySelector('.hover-v');
    const hoverR = svg.querySelector('.hover-r');
    const hoverTip = svg.querySelector('.hover-tip');
    const htTime = svg.querySelector('.ht-time');
    const htValue = svg.querySelector('.ht-value');
    const htRank = svg.querySelector('.ht-rank');
    svg.style.cursor = 'crosshair';

    function nearestIndex(mxClient) {{
      const rect = svg.getBoundingClientRect();
      // SVG viewBox 是 0..W，CSS 实际像素可能被缩放
      const vbToPx = rect.width / W;
      const mx = (mxClient - rect.left) / vbToPx;
      if (n === 1) return 0;
      // 每个点的 xOf 折算到 vb 坐标
      const step = innerW / (n - 1);
      let idx = Math.round((mx - padL) / step);
      if (idx < 0) idx = 0;
      if (idx > n - 1) idx = n - 1;
      return idx;
    }}

    function moveHandler(e) {{
      const idx = nearestIndex(e.clientX);
      const x = xOf(idx);
      const yv = yV(points[idx][1]);
      const yr = yR(points[idx][2]);
      hoverLine.setAttribute('x1', x.toFixed(1));
      hoverLine.setAttribute('x2', x.toFixed(1));
      hoverLine.setAttribute('opacity', '0.85');
      hoverV.setAttribute('cx', x.toFixed(1));
      hoverV.setAttribute('cy', yv.toFixed(1));
      hoverR.setAttribute('cx', x.toFixed(1));
      hoverR.setAttribute('cy', yr.toFixed(1));
      hoverMarkers.setAttribute('opacity', '1');
      // tooltip 内容
      htTime.textContent  = formatHHMM(points[idx][0]);
      htValue.textContent = '仓库 ' + points[idx][1].toFixed(2) + 'M';
      htRank.textContent  = '排名 #' + points[idx][2];
      // 让 tooltip 一直在画布内
      let tx = x;
      const tipHalf = 58;
      if (tx - tipHalf < padL) tx = padL + tipHalf;
      if (tx + tipHalf > W - padR) tx = W - padR - tipHalf;
      hoverTip.setAttribute('transform', 'translate(' + tx.toFixed(1) + ',' + (padT + 18) + ')');
      hoverTip.setAttribute('opacity', '1');
    }}

    function leaveHandler() {{
      hoverLine.setAttribute('opacity', '0');
      hoverMarkers.setAttribute('opacity', '0');
      hoverTip.setAttribute('opacity', '0');
    }}

    svg.addEventListener('mousemove', moveHandler);
    svg.addEventListener('mouseleave', leaveHandler);
    // 触摸事件（移动设备）
    svg.addEventListener('touchstart', (e) => {{ if (e.touches[0]) moveHandler(e.touches[0]); }}, {{ passive: true }});
    svg.addEventListener('touchmove', (e) => {{ if (e.touches[0]) moveHandler(e.touches[0]); }}, {{ passive: true }});
    svg.addEventListener('touchend', leaveHandler);

    // 数据表（时间正序）
    const rows = points.slice().reverse().map((p) =>
      '<tr><td class="num">' + formatHHMM(p[0]) + '</td>' +
      '<td class="num">' + p[1].toFixed(2) + 'M</td>' +
      '<td class="num">#' + p[2] + '</td></tr>'
    ).join('');
    tableBox.innerHTML =
      '<table><thead><tr><th>时间</th><th>仓库价值</th><th>排名</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }}

  // 把区间 [lo, hi] 向上/下取整到 ticks+1 个整数倍刻度，保证所有刻度为整数
  function buildIntegerAxis(lo, hi, ticks) {{
    const span = Math.max(1, hi - lo);
    const rawStep = span / ticks;
    // 取 step 为整数（不小 1），保证 (ticks+1)*step >= span
    let step = Math.max(1, Math.round(rawStep));
    if (step * ticks < span) step += 1; // 防止太密；保证 step * ticks >= span
    const niceLo = Math.floor(lo / step) * step;
    let niceHi = Math.ceil(hi / step) * step;
    if (niceHi - niceLo < step * ticks) niceHi = niceLo + step * ticks; // 容差
    return {{ min: niceLo, max: niceHi, step: step }};
  }}

  // —— 展开/收起切换 ——
  tbody.addEventListener('click', (e) => {{
    const btn = e.target.closest('button.expand-btn');
    if (!btn) return;
    const row = btn.closest('tr.data-row');
    if (!row) return;
    const expanded = btn.getAttribute('aria-expanded') === 'true';
    let panel = row.nextElementSibling;
    if (!panel || !panel.classList.contains(PANEL_CLASS)) {{
      panel = buildPanel(btn.dataset.playerName);
      row.parentNode.insertBefore(panel, row.nextSibling);
    }}
    if (expanded) {{
      btn.setAttribute('aria-expanded', 'false');
      panel.classList.add('hidden');
      panel.dataset.expanded = '0';
    }} else {{
      // 重渲染（保险）
      renderPanelContent(panel, players[btn.dataset.playerName] || []);
      btn.setAttribute('aria-expanded', 'true');
      panel.dataset.expanded = '1';
      panel.classList.remove('hidden');
    }}
  }});

  applyFilter();
}})();
</script>
</body>
</html>"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(doc, encoding="utf-8")


def run_once(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    report = Path(args.output)
    conn = connect_db(data_dir)
    rows, source_time, total_pages = fetch_rankings(
        max_pages=args.pages, platform=args.platform, competition_id=args.competition_id
    )
    snapshot_id = store_snapshot(conn, rows, source_time, args.platform, args.competition_id)
    render_report(conn, report)
    print(
        f"OK snapshot_id={snapshot_id} rows={len(rows)} total_pages={total_pages} "
        f"source_time={source_time} report={report}"
    )
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
    parser.add_argument("--pages", type=int, default=0, help="每次最多抓取页数，每页 10 条；0 或负数表示不分页上限，跟随接口抓全量")
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
