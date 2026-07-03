# 三角洲行动主播巅峰赛排行榜监控

这个仓库用于定时抓取腾讯活动页公开排行榜数据，并生成适合 GitHub Pages 发布的静态 HTML 图表报告。

## 本地运行

```bash
python3 df_rank_monitor.py once --pages 5 --data-dir data --output docs/index.html
python3 df_rank_monitor.py serve --host 127.0.0.1 --port 8765 --output docs/index.html
```

## GitHub Pages 部署方式

1. 推送本仓库到 GitHub。
2. 在仓库 Settings -> Pages 中选择：
   - Source: Deploy from a branch
   - Branch: main
   - Folder: /docs
3. Actions 会每 5 分钟运行一次 `.github/workflows/update-pages.yml`，更新：
   - `docs/index.html`
   - `data/rank_history.sqlite3`

## 注意

GitHub Actions 的定时任务不是精确定时，通常会有数分钟延迟。这个方案适合公网静态展示，不适合秒级实时监控。
