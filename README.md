# Folotoy Display data feed

## 使用方式

这是 FoloToy AI Passport 的独立内容仓库。固件读取：
**<https://blkot.github.io/folotoy-display/device.json>**。

1. 打开 [content.yaml 在线编辑器](https://github.com/blkot/folotoy-display/edit/main/content.yaml)。
2. 修改 `username`（展示账号）、`display_name`（名字）、`headline`（首页短句）。
3. 点击 **Commit changes** 提交到 `main`，Actions 会自动生成并发布。
4. 设备每 5 分钟读取一次；GitHub 数据每小时的第 17 分钟自动同步，也可在 Actions 页面手动运行。

`null` 表示自动获取；列表也可改为手动内容。自动同步不会覆盖 `content.yaml`。
当前固件使用 ASCII 点阵字库，手动文字请使用英文、数字、标点；`headline` 最多 36 个字符。
配置及生成的数据均公开，不要填写密码或 Token。设备沿用已保存的 Wi-Fi，无需重新填写 GitHub Token。
发布与定时任务可能有延迟；GitHub 可能暂停长期无仓库活动的定时任务，届时可在 Actions 页面重新启用。

---

This repository builds the public data feed used by the Folotoy ESP32 display. The firmware reads `device.json` and the small raw image files from GitHub Pages. The published site is intended to be:

<https://blkot.github.io/folotoy-display/>

The JSON endpoint is <https://blkot.github.io/folotoy-display/device.json>.

The Pages feed is public. Profile data and any manual entries in `content.yaml` will be visible to anyone who opens the feed.

## Edit the content

Edit [`content.yaml`](content.yaml) on GitHub or in a local checkout. [Open the GitHub editor for `content.yaml`](https://github.com/blkot/folotoy-display/edit/main/content.yaml). The username selects the public GitHub profile. `display_name` and `headline` can override its public profile text. Leave either as `null` to use GitHub's value.

The `recent_repositories` and `activity_items` fields accept `null` for automatic public GitHub data, or an explicit list for manually selected items. An empty list publishes that section with no items. Entries are sorted by their dates, newest first. Manual dates use `YYYY-MM-DD`. For example:

```yaml
recent_repositories:
  - full_name: blkot/FoloToy
    pushed_date: '2026-10-01'

activity_items:
  - type: push
    repository: blkot/FoloToy
    date: '2026-10-01'
```

Manual entries are not overwritten by the build. The workflow also reads the public contribution calendar, public profile, public events, public repositories, and achievement badges. It runs on pushes to `main`, when started manually, and hourly at minute 17. Changes to `content.yaml` are published on the next push; automatic GitHub changes are picked up by the hourly run.

## GitHub setup

Enable **Settings → Pages → Build and deployment → GitHub Actions**. The workflow needs only the repository's built-in `GITHUB_TOKEN` with read access for public profile data, plus the Pages and OIDC permissions used by GitHub's Pages deployment actions. It tries `PROFILE_TOKEN` first when that repository secret is present and falls back to the built-in token if the optional secret is rejected or rate-limited. The build does not require a personal token.

The workflow uses GitHub's supported configure, upload, and deploy Pages actions pinned to verified full commit SHAs.

## Local build

Requires Python 3.12 or newer:

```powershell
python -m pip install -r requirements.txt
python build.py
```

Set `GITHUB_TOKEN` or `PROFILE_TOKEN` in the environment to enable the GraphQL contribution calendar locally. Without a token, the build can still use public REST endpoints; an unavailable optional section is marked unavailable or retained as stale data from the currently published Pages feed. The token is sent only to `api.github.com`. Avatar, achievement image, profile HTML, and Pages requests are unauthenticated.

The build writes the Pages artifact to `dist/`. `device.json` is limited to 16,384 bytes. Image assets are content-hashed, raw RGB565 little-endian files. `index.html` is a small status page linking to the device feed and source repository.

## Feed contract

See [PROTOCOL.md](PROTOCOL.md) for the exact JSON fields, date and calendar ordering, image layout, refresh behavior, and device-side caching notes. The generated `generated_at` timestamp is UTC. The device may use `refresh_seconds` as a refresh hint; scheduled GitHub Actions updates occur hourly.
