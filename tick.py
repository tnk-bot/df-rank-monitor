"""每整 5 分钟 (BJ 18:30 - 24:00) 触发 GitHub workflow 的本地调度器。"""
import os
import sys
import json
import datetime as dt
import subprocess
import time

REPO = "tnk-bot/df-rank-monitor"
TOKEN_ENV="DF_RANK_GITHUB_TOKEN"

# 抓取窗口：每天 BJ 18:30 - 24:00 (含)
WINDOW_START_H = 18
WINDOW_START_M = 30
WINDOW_END_H = 24  # 即次日 00:00 前


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


def trigger():
    token = os.environ.get(TOKEN_ENV)
    if not token:
        print("missing token env", file=sys.stderr)
        return False
    hdr_authorization = "Bearer " + token
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


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "now":
        trigger()
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
            # 触发完后顺便对上整分钟
            trigger()
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
