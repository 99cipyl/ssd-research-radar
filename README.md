# SSD Research Radar

这是一个 SSD / NAND / NVMe 资料聚合器。它解决四个不同的问题：

1. HTML 面板与历史 Feed 只公开滚动最近 5 年的资料；SQLite 中的更旧行只用于去重、版本核验和防止旧资料被误报为新增；
2. Codex 定时检查负责只在出现新资料或来源异常时通知。
3. 公网 RSS、分卷历史 Feed 和 OPML 供 NetNewsWire 等手机阅读器订阅。
4. 每个条目先进入站内中文研究简报，再由简报提供原文入口；实时 Feed 不直接把读者丢到原站。

不依赖第三方 Python 包，也不会抓取 Google Scholar 或 IEEE Xplore 页面。

## 已接入来源

- DBLP 的 USENIX FAST 近 5 年可检索论文；
- NVMW 官方 Program（按每个报告拆分，并保留官方 Abstract）；
- ETH SAFARI 网站近 5 年的 WordPress 文章与今后更新；
- NVM Express 网站近 5 年文章、技术资源，以及按具体规范拆分的当前版本变化；
- OCP Storage 公开 RSS；
- OpenAlex 中与 FTL、GC、wear leveling、NAND retention/disturb/read-retry/scrubbing、ZNS、FDP、KV SSD 和计算存储匹配的论文。

Google Scholar 和 IEEE Xplore 没有适合本机无凭据轮询的稳定公开 RSS/API，因此学术主题由 OpenAlex 拉取；Scholar/Xplore 邮件提醒仍可作为补漏层。

## 使用

首次同步会回溯滚动最近 5 年并建立基线，但不会把旧资料推送成“新增”：

```bash
cd /path/to/ssd-research-radar
python3 radar.py sync
```

以后再次运行只报告新增或发生实质变化的记录：

```bash
python3 radar.py sync --format json
```

直接双击 `site/index.html` 可以查看历史面板。也可以启动本地服务：

```bash
python3 radar.py serve --open
```

此时仅供本机预览的地址为：

- 历史面板：`http://127.0.0.1:8765/`
- 实时 RSS：`http://127.0.0.1:8765/live.xml`
- NetNewsWire 导入页：`http://127.0.0.1:8765/import.html`

`127.0.0.1` 只能在这台 Mac 上访问，不能作为手机订阅地址。手机订阅必须使用下面的公网发布方式。

## 手机 / NetNewsWire 订阅

生成公网 Feed 时设置站点根地址。地址必须是可由手机访问的 HTTPS 目录；脚本会自动补齐末尾 `/`：

```bash
export RADAR_PUBLIC_BASE_URL="https://YOUR_NAME.github.io/ssd-research-radar/"
python3 radar.py build
```

发布 `site/` 目录后，可以：

- 只订阅即时更新：`https://YOUR_NAME.github.io/ssd-research-radar/live.xml`；
- 打开 `https://YOUR_NAME.github.io/ssd-research-radar/import.html` 下载并导入 OPML，一次获得即时 Feed 和最近 5 年的历史分卷；
- `feed.xml` 是与 `live.xml` 字节完全相同的兼容别名，旧订阅无需迁移。

`live.xml` 最多保留最近 350 个“首次基线以后新增或实质更新”的事件。首次建库导入的旧资料不会进入 Live，也不会制造几千条未读通知。历史快照按首发日保留最近 5 年；一篇更旧的资料如果今天发生实质更新，仍会作为当天事件进入 Live。专业简报历史使用 32 个预创建的稳定哈希分片 `professional-archive-01.xml` 至 `professional-archive-32.xml`；分片只发布通过校验的专业简报，全部公网地址一次性写入 `netnewswire.opml`。

每个 RSS 条目的链接都指向 `item.html?id=<稳定ID>`。页面只加载该条资料的分片 JSON，而不是在手机上下载整个历史库，并固定展示：内容是什么、问题、核心思想、支持核心思想的原文短句、机制、证据/结果、SSD 全链路位置、工程价值、阅读建议、局限和证据等级。原始摘要默认折叠，原文是页面内的独立按钮；页面同时标注整理模型、生成时间以及“AI 自动整理、未经人工全文复核”。

### 专业简报与证据边界

- 新资料和实质更新会优先从官方页面补取证据：FAST 使用 USENIX 官方摘要，SAFARI/NVM Express 使用官方 WordPress 正文，NVMW 使用逐报告 Abstract，OCP 使用已保存的邮件正文；
- GitHub Actions 使用内置 `GITHUB_TOKEN` 和 `models: read` 调用 GitHub Models，生成经过字段校验的中文专业简报，不需要另存模型 API Key；
- 模型提示明确禁止使用标题猜测论文结论。无摘要/正文时，问题、核心思想、机制和结果会标记为“原页面未提供”；
- 只有状态为 `professional` 且全部字段、证据原句、SSD 层级与数字声明通过校验的资料，才会进入 Live、完整 Feed、历史分卷和 WebSub 推送；该门槛同时适用于新增、更新和历史基线。网络、配额或校验失败时只留在待整理队列，按退避窗口自动重试；
- 搜索面板、`archive.json`、详情分片与所有 RSS 统一只展示滚动最近 5 年内已经通过校验的专业简报；未完成或校验失败的条目完全留在后台重试，不公开占位内容。RSS 历史分卷预先固定并可为空；专业回填通过后自动进入原分卷，用户一次导入 OPML 即可持续收到。证据等级会区分官方网页正文、官方摘要、来源摘要/摘录和仅元数据。

OPML 中已经把订阅分成两个文件夹：

- “SSD 即时更新（开启通知）”：用于手机新内容提醒；
- “SSD 专业简报历史（自动回填，建议关闭通知）”：只出现完成专业整理的旧资料，建议关闭通知；后台回填会自动增加，无需重新导入。

RSS 的 `<channel><link>` 和 Atom `rel=self` 均使用 `RADAR_PUBLIC_BASE_URL`，所以部署到 GitHub Pages、Cloudflare Pages 或其他静态 HTTPS 托管均可。没有设置该变量时，`build` 会退回本机预览地址，不能用于手机。

### WebSub 加速

如果使用的同步服务支持 WebSub，可再设置 Hub：

```bash
export RADAR_PUBLIC_BASE_URL="https://YOUR_NAME.github.io/ssd-research-radar/"
export RADAR_WEBSUB_HUB="https://YOUR_WEBSUB_HUB/"
python3 radar.py sync
```

脚本会在 `live.xml` 中加入 `rel=hub`，并且只有出现新增/更新事件时才向 Hub 发布 ping。若 ping 失败：

- 来源同步本身仍然成功；
- Feed 已经包含新事件；
- `reports/latest.json` 和 Markdown 报告会出现非阻断 warning；
- 事件的 WebSub 待发布状态不会被确认，下一次同步会重试。

WebSub 可以缩短支持它的服务端阅读器的发现延迟；最终手机通知速度仍受所用 RSS 账户、NetNewsWire 后台刷新以及 iOS 调度影响。

其他命令：

```bash
python3 radar.py stats
python3 radar.py doctor
python3 radar.py backup
python3 radar.py build
```

## 历史范围与边界

公开历史是“滚动最近 5 年”，截止日每天前移，并在 `archive.json`、`status.json` 和同步报告中显式记录。规则是：

- 历史面板、历史 RSS 和专业回填按“最早已知首发日”判定；该日期只能在发现更早的可信记录时向前修正，无日期条目不进入历史快照；
- 今天新发现但首发日已早于截止日的资料不会伪装成“新增”；老资料今天发生实质内容更新则仍会进入当天事件流；
- SQLite 内部保留更旧的记录和版本证据，只用于去重和防止重复通知；它们不进入网页、RSS、OPML 历史或模型回填队列；
- OCP Groups.io 的公开 RSS 只提供最近 20 条；没有 API Key 时从现在起持续跟踪；
- 源站已经删除、从未公开或需要企业权限的资料无法凭 RSS 恢复；
- OpenAlex 无法按“索引更新时间”做可靠增量，因此首次基线以后每天重扫最近一年。云端不会再做 25 年关键词月度全扫，以免超出免费搜索预算后在末页限流并丢掉整批结果；月度 `--full` 只用于有界的 FAST TOC 回扫。发布日期早于滚动窗口、但一年后才被 OpenAlex 补录的论文仍可能漏掉，因此 Scholar/IEEE 邮件提醒保留为补漏层。

强烈建议申请并配置免费的 OpenAlex API Key，在运行环境中设置 `OPENALEX_API_KEY`；脚本会自动使用它，但不会把密钥写入数据库或报告。没有 Key 时其他来源仍会继续同步，但 OpenAlex 更容易受到匿名配额或限流影响。

如需一次性导入 OCP Storage 约 645 条公开旧邮件，可在 Groups.io 创建 API Key，并设置 `GROUPS_IO_API_KEY`；也可以把 Key 单独放在被忽略的 `data/groupsio_api_key` 文件中。没有 Key 时脚本自动退回官方 RSS，并从现在开始持续积累，不会影响其他来源。

## 数据与去重

- 数据库：`data/radar.sqlite3`
- 最新机器可读结果：`reports/latest.json`
- 每次运行报告：`reports/*.md`
- 面板：`site/index.html`
- 单条中文简报：`site/item.html` + `site/items/<分片>/<稳定ID>.json`
- 可导入的数据：`site/archive.json`
- 手机实时 RSS：`site/live.xml`
- 兼容 RSS：`site/feed.xml`
- 专业简报历史分片 RSS：`site/professional-archive-*.xml`（32 个稳定哈希分片，只发布已通过校验的条目）
- NetNewsWire 浏览器导入页：`site/import.html`
- NetNewsWire 导入清单：`site/netnewswire.opml`（UTF-8 BOM，兼容缺少 charset 的静态托管响应）

RSS 是首次基线后的“已完成专业整理的新增与实质更新事件流”；历史面板和 `archive.json` 也只保留滚动最近 5 年内通过专业校验的内容。资料后续发生变化时，旧快照保存在 SQLite 的 `item_versions` 表作为内部去重与核验证据，不会被新内容无痕覆盖；结构化简报保存在 `item_briefs` 表，原始来源内容与专业总结不会混为同一字段。

论文优先按 DOI 去重；没有 DOI 时使用规范化标题和年份。网站文章和邮件按来源的稳定 ID 保存。新增来源第一次导入时自动建立自己的基线，不会制造历史通知洪水。待通知事件使用持久化 outbox：若程序在生成报告前退出，下次同步会再次交付，宁可重复一次也不会永久漏报。

## 自动通知

公开仓库的 GitHub Actions 会约每 15 分钟检查 RSS、WordPress 和规范页；FAST 与 OpenAlex 每天检查一次，月初只对 FAST 做一次完整 TOC 回扫。OpenAlex 在首次基线以后采用最近一年的滚动重扫，不执行月度全历史回放。另有本机 Codex Heartbeat 作为故障提醒与备用检查。

自动检查读取 `reports/latest.json`：

- `new_count > 0`：发送标题、来源、日期、站内专业简报链接、核心思想和工程相关性；
- `failures` 非空：报告失败来源或专业简报重试，下一次会自动重试；专业简报证据校验失败只产生黄色警告并继续拦截该条，不会把已成功的同步与部署误标成红色；
- 没有新增也没有故障：保持静默。

云端检查不依赖 Mac 是否开机；本机 Heartbeat 在 Mac 关机、睡眠或 Codex Desktop 未运行时会暂停。GitHub 定时任务和 iOS 后台刷新均为尽力而为，不是严格实时 SLA；SQLite 历史通过独立状态分支持久保存。
