# viral 实时搜索的参数白名单与字段

**2026-09-22 实测**，服务端 400 报错直接给出了白名单：

| 参数 | 只接受 | 坑 |
|---|---|---|
| `sortBy` | `relevance` / `date` / `likes` | **没有 views**。按播放排序或筛选只能本地做 |
| `dateRange` | `yesterday` / `this-week` / `this-month` / `last-3-months` / `last-6-months` / `all-time` | 只有 6 档枚举，**给不了"近 15 天"这种精确窗口**。先用最接近的一档拉，再本地按 `published_at` 精确切 |
| `region` | 任意 | **不过滤结果**，见 region-follows-language |

认证头是 `x-api-key`，不是 `Authorization: Bearer`（用 Bearer 会 401）。

**分页**：每页固定 12 条 + `nextPageToken`，实测翻到 8 页仍有下页，没见到上限。
每页一次调用，**消耗 credits**。

**单条 30 个字段**，比公开页抓取拿到的还全：
view/like/comment/share/bookmark count、engagement_rate、published_at、region、
caption、hashtags、duration_seconds、music_*、**download_url（可直接取原片）**。

这些约束已固化在 `scout/viral.py`，传错值直接抛异常，不会静默返回错数据。
