"""北京时间 18:30–24:00 每 150 秒调度 df-rank-monitor。

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
INTERVAL_SECONDS = 150
POLL_SECONDS = 30
STUCK_SECONDS = 480  # 关键 workflow 卡 queued/in_progress 超过 8 分钟自动取消并补发


def bj_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=8)))


def in_window(now_bj: dt.datetime) -> bool:
    minutes = now_bj.hour * 60 + now_bj.minute
    return (WINDOW_START_H * 60 + WINDOW_START_M) <= minutes < (WINDOW_END_H * 60)


def window_start(now_bj: dt.datetime) -> dt.datetime:
    return now_bj.replace(
        hour=WINDOW_START_H,
        minute=WINDOW_START_M,
        second=0,
        microsecond=0,
    )


def schedule_slot(now_bj: dt.datetime) -> tuple[str, dt.datetime]:
    """返回当前 150 秒时隙的唯一键和下一时隙起点。"""
    start = window_start(now_bj)
    elapsed = max(0, (now_bj - start).total_seconds())
    slot = int(elapsed // INTERVAL_SECONDS)
    next_start = start + dt.timedelta(seconds=(slot + 1) * INTERVAL_SECONDS)
    return f"{now_bj:%Y%m%d}-{slot}", next_start


def dispatch_wait_seconds(runs, last_dispatch_at: float | None) -> float:
    """两次实际 dispatch 之间至少间隔 150 秒，包括 daemon 重启后的第一轮。"""
    if last_dispatch_at is not None:
        elapsed = time.monotonic() - last_dispatch_at
        return max(0.0, INTERVAL_SECONDS - elapsed)
    update = latest_update(runs)
    created_at = update.get("created_at") if update else None
    if not created_at:
        return 0.0
    try:
        created = dt.datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    age = (dt.datetime.now(dt.timezone.utc) - created).total_seconds()
    return max(0.0, INTERVAL_SECONDS - age)


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
    # 这三个 workflow 是关键路径。只要还在 queued/in_progress，就不要 dispatch 新的一轮。
    names = {"Update leaderboard report", "pages build and deployment", "Push on main"}
    for run in runs:
        if run.get("name") in names and run.get("status") in {"queued", "in_progress"}:
            return f"busy: {run.get('name')} {run.get('status')} {run.get('id')}"
    return None


def stuck_run(runs) -> dict | None:
    """任一关键 workflow 卡 queued/in_progress 超 STUCK_SECONDS，返回该 run。"""
    names = {"Update leaderboard report", "pages build and deployment", "Push on main"}
    now = dt.datetime.now(dt.timezone.utc)
    for run in runs:
        if run.get("name") not in names:
            continue
        if run.get("status") not in {"queued", "in_progress"}:
            continue
        updated = run.get("updated_at")
        if not updated:
            continue
        try:
            t = dt.datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            continue
        elapsed = (now - t).total_seconds()
        if elapsed >= STUCK_SECONDS:
            return run
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


def cancel_run(run_id: int) -> bool:
    hdr = auth_header()
    if not hdr:
        return False
    cp = subprocess.run(
        [
            "curl", "-sS", "-o", "/tmp/_tick_cancel.json", "-w", "%{http_code}",
            "-X", "POST",
            "-H", "Accept: application/vnd.github+json",
            "-H", "Authorization: " + hdr,
            "-H", "X-GitHub-Api-Version: 2022-11-28",
            f"https://api.github.com/repos/{REPO}/actions/runs/{int(run_id)}/cancel",
        ],
        capture_output=True,
        text=True,
    )
    code = cp.stdout.strip()
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    print("[" + ts + "] cancel run=" + str(run_id) + " http=" + code, flush=True)
    return code in {"202", "204"}


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


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "now":
        trigger("manual-now")
        return

    print("tick daemon started (non-overlap mode)", flush=True)
    last_scheduled_slot: str | None = None
    last_retry_key: str | None = None
    last_dispatch_at: float | None = None
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
            stuck = stuck_run(runs)
            if stuck:
                rid = stuck.get("id")
                if cancel_run(rid):
                    print(f"stuck: canceled {stuck.get('name')} {rid}", flush=True)
                    if runs is None: runs = []
                    # 等几秒再补发，让 cancel 先落地
                    time.sleep(5)
                    wait_for_interval = dispatch_wait_seconds(runs, last_dispatch_at)
                    if wait_for_interval > 0:
                        time.sleep(wait_for_interval)
                    if trigger("stuck-cancel-retry"):
                        last_dispatch_at = time.monotonic()
                        last_scheduled_slot = schedule_slot(now)[0]
                    time.sleep(POLL_SECONDS)
                    continue
            print(reason, flush=True)
            time.sleep(POLL_SECONDS)
            continue

        update = latest_update(runs)
        if update and update.get("status") == "completed" and update.get("conclusion") == "failure":
            minute_key = "update-failure-retry-" + now.strftime("%Y%m%d%H%M")
            if last_retry_key != minute_key:
                wait_for_interval = dispatch_wait_seconds(runs, last_dispatch_at)
                if wait_for_interval > 0:
                    time.sleep(wait_for_interval)
                if trigger("update-failure-retry"):
                    last_dispatch_at = time.monotonic()
                    last_retry_key = minute_key
                    last_scheduled_slot = schedule_slot(now)[0]
            time.sleep(POLL_SECONDS)
            continue

        pages = latest_pages(runs)
        if pages and pages.get("status") == "completed" and pages.get("conclusion") == "failure":
            # Pages 刚失败，立即补发；但同一分钟只补发一次，避免失败风暴。
            minute_key = "retry-" + now.strftime("%Y%m%d%H%M")
            if last_retry_key != minute_key:
                wait_for_interval = dispatch_wait_seconds(runs, last_dispatch_at)
                if wait_for_interval > 0:
                    time.sleep(wait_for_interval)
                if trigger("pages-failure-retry"):
                    last_dispatch_at = time.monotonic()
                    last_retry_key = minute_key
                    last_scheduled_slot = schedule_slot(now)[0]
            time.sleep(POLL_SECONDS)
            continue

        slot_key, nxt = schedule_slot(now)
        if last_scheduled_slot != slot_key:
            wait_for_interval = dispatch_wait_seconds(runs, last_dispatch_at)
            if wait_for_interval > 0:
                time.sleep(wait_for_interval)
            if trigger("scheduled"):
                last_dispatch_at = time.monotonic()
                last_scheduled_slot = slot_key
            time.sleep(POLL_SECONDS)
            continue

        wait = max(1, min(POLL_SECONDS, int((nxt - now).total_seconds())))
        time.sleep(wait)


if __name__ == "__main__":
    main()
