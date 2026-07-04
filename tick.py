"""每整 5 分钟 (BJ 18:30 - 24:00) 触发 GitHub workflow 的本地调度器。

GitHub Pages 的部署阶段偶发返回 "Deployment failed, try again later."；
因此每次触发后等待一段时间检查最近的 Pages 部署，失败则补发一次 dispatch。
"""
import os
import sys
import json
import datetime as dt
import subprocess
import time

REPO = "tnk-bot/df-rank-monitor"
TOKEN_ENV = "DF_RANK_GITHUB_TOKEN"

# 抓取窗口：每天 BJ 18:30 - 24:00 (含)
WINDOW_START_H = 18
WINDOW_START_M = 30
WINDOW_END_H = 24  # 即次日 00:00 前
PAGES_RETRY_DELAY_SECONDS = 180


def in_window(now_bj: dt.datetime) -> bool:
    minutes = now_bj.hour * 60 + now_bj.minute
    start = WINDOW_START_H * 60 + WINDOW_START_M  # 1110
    end = WINDOW_END_H * 60  # 1440
    return start <= minutes < end


def next_tick(now: dt.datetime) -> dt.datetime:
    base = now.replace(second=0, microsecond=0)
    add_min = (5 - now.minute % 5) % 5
    if add_min == 0 and now.second == 0 and now.microsecond == 0:
        return base
    if add_min == 0:
        add_min = 5
    return base + dt.timedelta(minutes=add_min)


def auth_header() -> str | None:
    token = os.environ.get(TOKEN_ENV)
    if not token:
        print("missing token env", file=sys.stderr, flush=True)
        return None
    return "Bearer " + token


def trigger() -> bool:
    hdr_authorization = auth_header()
    if not hdr_authorization:
        return False
    body = json.dumps({"event_type": "tick", "client_payload": {"source": "sandbox-tick"}})
    url = f"https://api.github.com/repos/{REPO}/dispatches"
    cp = subprocess.run(
        [
            "curl", "-sS", "-o", "/tmp/_tick_resp.json", "-w", "%{http_code}",
            "-X", "POST",
            "-H", "Accept: application/vnd.github+json",
            "-H", "Authorization: " + hdr_authorization,
            "-H", "X-GitHub-Api-Version: 2022-11-28",
            "-H", "Content-Type: application/json",
            "-d", body, url,
        ],
        capture_output=True, text=True,
    )
    code = cp.stdout.strip()
    print(f"[{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}] tick http={code}", flush=True)
    return code == "204"


def github_api(path: str):
    hdr_authorization = auth_header()
    if not hdr_authorization:
        return None
    url = "https://api.github.com/" + path.lstrip("/")
    cp = subprocess.run(
        [
            "curl", "-sS",
            "-H", "Accept: application/vnd.github+json",
            "-H", "Authorization: " + hdr_authorization,
            "-H", "X-GitHub-Api-Version: 2022-11-28",
            url,
        ],
        capture_output=True, text=True,
    )
    if cp.returncode != 0:
        print(f"github api failed: {cp.returncode}", flush=True)
        return None
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError:
        return None


def latest_pages_deploy_conclusion():
    data = github_api(f"repos/{REPO}/actions/runs?per_page=10")
    if not data:
        return None
    for run in data.get("workflow_runs", []):
        if run.get("name") == "pages build and deployment":
            return run.get("status"), run.get("conclusion"), run.get("updated_at")
    return None


def trigger_with_pages_retry() -> bool:
    ok = trigger()
    if not ok:
        return False
    time.sleep(PAGES_RETRY_DELAY_SECONDS)
    status = latest_pages_deploy_conclusion()
    print(f"pages status after tick: {status}", flush=True)
    if status and status[0] == "completed" and status[1] == "failure":
        print("pages deploy failed; retry dispatch once", flush=True)
        trigger()
    return True


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "now":
        trigger_with_pages_retry()
        return
    print("tick daemon started", flush=True)
    while True:
        now_utc = dt.datetime.now(dt.timezone.utc)
        now_bj = now_utc.astimezone(dt.timezone(dt.timedelta(hours=8)))
        if in_window(now_bj):
            nxt = next_tick(now_bj)
            wait = (nxt - now_bj).total_seconds()
            if wait > 0:
                print(f"next tick at BJ {nxt.strftime('%H:%M')} (sleep {wait:.0f}s)", flush=True)
                time.sleep(wait)
            trigger_with_pages_retry()
        else:
            # 不在窗口时段，等到下一个 18:30 BJ
            now_bj = dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8)))
            target_min = WINDOW_START_H * 60 + WINDOW_START_M
            cur_min = now_bj.hour * 60 + now_bj.minute
            if cur_min >= target_min:
                target = (now_bj + dt.timedelta(days=1)).replace(hour=WINDOW_START_H, minute=WINDOW_START_M, second=0, microsecond=0)
            else:
                target = now_bj.replace(hour=WINDOW_START_H, minute=WINDOW_START_M, second=0, microsecond=0)
            wait = (target - now_bj).total_seconds()
            print(f"outside window; next window opens BJ {target.strftime('%Y-%m-%d %H:%M')} (sleep {wait/60:.1f}m)", flush=True)
            time.sleep(wait)


if __name__ == "__main__":
    main()
