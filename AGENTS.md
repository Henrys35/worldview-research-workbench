# worldview-scout（Claude 与 Codex 共读）

**独立产线**：按市场、时间、播放门槛找竞品账号 → 抓公开证据 → 归纳内容世界观。

接到"找竞品世界观"类任务，先读 [SKILL.md](SKILL.md)——完整的五步工作流和红线都在那里。
动手前先读 [knowledge/KNOWLEDGE.md](knowledge/KNOWLEDGE.md)，已经踩过的坑不要再踩。

## 定位

这条产线独立运行，自己演进，自己积累教训。代码起源于 视频生产工具
的 `tvp research`（取证、报告、飞书写入三块），但**分出来之后两边各自独立**，
不跟母本同步，也不受视频生产线的规则约束。

产出物是**调研结论**（世界观、内容建议、覆盖与缺口），不生成视频、不发布、不碰投放。
调研结论要拿去拍片，交给视频产线，那是下一条线的事。

## 硬规矩

- **调研产出一律写仓库外**：`~/Data/automation-media/worldview-scout/<轮次>`。
  crawl 在同一目录锁定清单并续跑；discover 会覆盖候选与草稿，search 会复用关键词缓存。新轮次用新目录。
- **凭证永不入库**。飞书读 `~/.config/worldview-scout/credentials.json` 或环境变量；
  viral 读 `VIRAL_API_KEY` 或 `~/.config/worldview-scout/viral-api.json`；
  TikTok 登录态在 `~/.config/worldview-scout/research/`，权限 600。
- **不自动调付费模型**。世界观归纳由 agent 读证据后自己写，这是产出的核心，不能交给模板。
- **不做验证码绕过**，不读取日常浏览器的 Cookie 数据库，不用付费爬虫。
  浏览器路线只在用户为本工具授权登录的专用 profile/CDP 端口检查 TikTok cookies 来判断登录。
  当前实现会复用指定端口的现有 CDP 服务，使用前必须确认它属于此工具，不能接管日常浏览器。
- viral 的实时搜索**消耗 credits**，`discover` 跑之前会打印调用次数。大批量前先报给用户。
- 飞书只**新建**独立多维表，不接收生产表 ID，不改生产表结构或已发布记录。

## 主路只有一条

`scout discover` → 筛 → `scout crawl` → 写 analysis.json → `scout report` / `feishu`。
零配置，一个 viral API key 就够，不需要浏览器、不需要任何登录。**默认永远走这条。**

`scout search` 是进阶：走浏览器、免 credits、单次产出更大，但要装 `.[browser]`
并登录一次使用者自己的 TikTok。**只在使用者明确说「不想烧 credits」或「量不够」时才提。**
两条路产出格式一致，下游完全通用。

**浏览器由 `scout.browser` 用系统 Chrome 正常启动，工具只通过调试端口接管。**
不用 playwright 自己 launch：那样起来的实例带自动化标记，TikTok 会甩滑块验证码。
平台可能对自动启动的浏览器要求验证；任何验证码均交给人处理，不保证当前可达性。

新开的标签页会被 Chrome 节流到一行都不渲染，抓取前必须 `bring_to_front()`。

**验证码一律由人处理**：停下来如实报出被拦的关键词，让使用者在窗口里自己拖滑块。
不做任何反检测绕过——原始实现用的隐身浏览器会话没有接回来，这是有意的。

## 结构

```
scout/
  browser.py  probe.py      专用浏览器的启动、接管与可达性探测
  viral.py    discover.py   按条件发现候选账号（viral 路径）
  search.py                 按条件发现候选账号（浏览器路径），与 discover 同格式
  tiktok_public.py crawl.py 公开页取证、分析校验、报告
  feishu.py   feishu_tables.py  新建研究表并回读校验
knowledge/    实测教训，带日期和证据
specs/        调研清单（不放产出）
```

改行为前先跑 `./.venv/bin/python -m pytest tests/ -q`，以实际测试结果为准。
