"""北京时间 18:30–24:00 调度 df-rank-monitor。

关键规则：不要重叠触发。GitHub Pages 部署经常在上一轮还没完成时被新一轮 push 打断，
表现为 "Deployment failed, try again later."。因此 tick 只在队列空闲时 dispatch；
如果最近一次 Pages 已失败，优先补发 retry。
"""
import datetime as dt
import json
import os
import subprocess
import sys
import time

REPO = "tnk-bot/df-rank-monitor"
TOKEN_ENV = "DF_RANK_GITHUB_TOKEN"
WINDOW_START_H = 18
WINDOW_START_M = 30
WINDOW_END_H = 24
POLL_SECONDS = 30


def bj_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8)))


def in_window(now_bj: dt.datetime) -> bool:
    minutes = now_bj.hour * 60 + now_bj.minute
    return (WINDOW_START_H * 60 + WINDOW_START_M) <= minutes < (WINDOW_END_H * 60)


def next_tick(now: dt.datetime) -> dt.datetime:
    base = now.replace(second=0, microsecond=0)
    add_min = (5 - now.minute % 5) % 5
    if add_min == 0:
        return base
    return base + dt.timedelta(minutes=add_min)


def next_window_open(now_bj: dt.datetime) -> dt.datetime:
    start_min = WINDOW_START_H * 60 + WINDOW_START_M
    cur_min = now_bj.hour * 60 + now_bj.minute
    if cur_min >= start_min:
        return (now_bj + dt.timedelta(days=1)).replace(hour=WINDOW_START_H, minute=WINDOW_START_M, second=0, microsecond=0)
    return now_bj.replace(hour=WINDOW_START_H, minute=WINDOW_START_M, second=0, microsecond=0)


def auth_header() -> str | None:
    token = os.environ.get(TOKEN_ENV)
    if not token:
        print("missing token env", file=sys.stderr, flush=True)
        return None
    return "Bearer " + token


def github_api(path: str):
    hdr = auth_header()
    if not hdr:
        return None
    cp = subprocess.run(
        [
            "curl", "-sS",
            "-H", "Accept: application/vnd.github+json",
            "-H", "Authorization: " + hdr,
            "-H", "X-GitHub-Api-Version: 2022-11-28",
            "https://api.github.com/" + path.lstrip("/"),
        ],
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        print(f"github api failed: {cp.returncode}", flush=True)
        return None
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError:
        print("github api returned non-json", flush=True)
        return None


def recent_runs():
    data = github_api(f"repos/{REPO}/actions/runs?per_page=12")
    if not data:
        return []
    return data.get("workflow_runs", [])


def busy_reason(runs) -> str | None:
    # 这两个 workflow 是关键路径。只要还在 queued/in_progress，就不要 dispatch 新的一轮。
    names = {"Update leaderboard report", "pages build and deployment", "Push on main"}
    for run in runs:
        if run.get("name") in names and run.get("status") in {"queued", "in_progress"}:
            return f"busy: {run.get('name')} {run.get('status')} {run.get('id')}"
    return None


def latest_update(runs):
    for run in runs:
        if run.get("name") == "Update leaderboard report":
            return run
    return None


def latest_pages(runs):
    for run in runs:
        if run.get("name") == "pages build and deployment":
            return run
    return None


def trigger(source: str) -> bool:
    hdr = auth_header()
    if not hdr:
        return False
    body = json.dumps({"event_type": "tick", "client_payload": {"source": source}})
    cp = subprocess.run(
        [
            "curl", "-sS", "-o", "/tmp/_tick_resp.json", "-w", "%{http_code}",
            "-X", "POST",
            "-H", "Accept: application/vnd.github+json",
            "-H", "Authorization: " + hdr,
            "-H", "X-GitHub-Api-Version: 2022-11-28",
            "-H", "Content-Type: application/json",
            "-d", body,
            f"https://api.github.com/repos/{REPO}/dispatches",
        ],
        capture_output=True,
        text=True,
    )
    code = cp.stdout.strip()
    print(f"[{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}] dispatch source={source} http={code}", flush=True)
    return code == "204"


def should_dispatch_at_tick(now_bj: dt.datetime) -> bool:
    # 在 5 分钟整点所在的前 90 秒内允许触发一次；避免 sleep wake 几百毫秒错过 18:30。
    return now_bj.minute % 5 == 0 and now_bj.second < 90


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "now":
        trigger("manual-now")
        return

    print("tick daemon started (non-overlap mode)", flush=True)
    last_dispatch_minute: str | None = None
    while True:
        now = bj_now()
        if not in_window(now):
            target = next_window_open(now)
            wait = max(1, min(300, int((target - now).total_seconds())))
            print(f"outside window; next window opens BJ {target.strftime('%Y-%m-%d %H:%M')} (sleep {wait}s)", flush=True)
            time.sleep(wait)
            continue

        runs = recent_runs()
        reason = busy_reason(runs)
        if reason:
            print(reason, flush=True)
            time.sleep(POLL_SECONDS)
            continue

        update = latest_update(runs)
        if update and update.get("status") == "completed" and update.get("conclusion") == "failure":
            minute_key = "update-failure-retry-" + now.strftime("%Y%m%d%H%M")
            if last_dispatch_minute != minute_key:
                if trigger("update-failure-retry"):
                    last_dispatch_minute = minute_key
            time.sleep(POLL_SECONDS)
            continue

        pages = latest_pages(runs)
        if pages and pages.get("status") == "completed" and pages.get("conclusion") == "failure":
            # Pages 刚失败，立即补发；但同一分钟只补发一次，避免失败风暴。
            minute_key = "retry-" + now.strftime("%Y%m%d%H%M")
            if last_dispatch_minute != minute_key:
                if trigger("pages-failure-retry"):
                    last_dispatch_minute = minute_key
            time.sleep(POLL_SECONDS)
            continue

        if should_dispatch_at_tick(now):
            minute_key = now.strftime("%Y%m%d%H%M")
            if last_dispatch_minute != minute_key:
                if trigger("scheduled"):
                    last_dispatch_minute = minute_key
            time.sleep(POLL_SECONDS)
            continue

        nxt = next_tick(now)
        wait = max(1, min(POLL_SECONDS, int((nxt - now).total_seconds())))
        time.sleep(wait)


if __name__ == "__main__":
    main()
