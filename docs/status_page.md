# 下载任务状态看板

## 用途

网页看板，通过浏览器查看全球建筑下载任务的实时状态：

- 当前正在执行的任务、尝试次数、开始时间、分块进度条、预计剩余时间、产物大小
- 当前任务卡片提供「停止当前任务并加入队尾」：先停止下载 runner 与 worker，确认进程退出后再把任务原子重置为 `PENDING` 并追加到全部待执行任务队尾；已完成分块保留。若进程未完全退出，任务继续保持 `RUNNING`。
- 队列分页列出**全部**待执行任务（默认按手动排序 `queue_order`，未排序时按清单顺序），支持每页 15/30/50/100/200 条与首页/上一页/下一页/末页切换；「排序」列提供 ↑ / ↓ / ⤒（置顶，跨页生效）按钮，改动在**下次启动批处理**时生效（可用「重启批处理」立即套用）；大洲/国家/城市显示为"英文(中文)"，中文名来自本地 Natural Earth 行政边界（`data/boundaries`）
- 队列支持**按城市名称搜索**（中英文均可，大小写不敏感，如 `Burundi` / `布隆迪`）；搜索后分页、排序与「共 N 条」统计均基于过滤结果，并显示未过滤总数
- 最近完成任务（要素数、耗时、完成时间、下载目录）与失败任务（错误原因）；**最近完成**每项提供「重新下载」：确认后删除该任务的整个下载目录（分片、完成标记、网格清单、合并/分卷产物），把任务重置为待执行并排到队首（累计「重下次数」+1），同时把下游「数据处理」队列中的同一数据集置为「等待重新下载」并删除其在「库表更新」页签的手工标记；服务端拒绝历史数据引用（`existing_data_dir` 与下载目录不一致）、共享目录、符号链接及仓库外的路径。失败任务表提供「操作」列：「重新入队」把 FAILED 任务重置为待执行并添加到队首（清空尝试次数与最后错误，**不删除已下载数据**）；改动均在**下次启动批处理**时生效
- 批处理整体状态（运行中 / 已停止 / 全部完成）与磁盘剩余空间
- 页面每 15 秒自动刷新，无外部 CDN 依赖
- **批处理控制**：查看当前 `tile-workers`（单任务内并发抓取格网数，看板固定为 2）与 runner PID，可一键重启批处理
- **页签导航**：顶部「建筑数据下载」为上述下载看板；「数据处理」为处理队列看板（切分 → QGIS 校验 → 清洗 → 3857 重投影），见下文
- **第三页签「库表更新」**：列出处理完成（`OK`）的任务，可手动勾选「已更新」并填写更新时间与备注；保存后写入 `data/db_update_status.json`（受 `--action-token` 保护）

下载进度展示只查询 SQLite 与任务目录；当前任务卡片的停止并重新入队、队列「排序」列与批处理控制区会实际写入状态库或停止/重启下载进程，已完成分块全部复用、任务从断点继续。处理页签当前为只读展示，启动/重试通过 CLI 完成。「库表更新」页签的手动标记保存到 JSON 文件，不影响下载与处理流程。

## 启动

```powershell
python -u scripts/status_server.py --port 8765
```

访问：

- 本机：`http://127.0.0.1:8765/`
- 局域网：`http://<本机IP>:8765/`（首次需放行防火墙）

后台常驻启动（推荐与批处理一起使用）：

```powershell
Start-Process python -ArgumentList "-u","scripts/status_server.py","--port","8765" -WorkingDirectory "E:\LoD1" -WindowStyle Hidden
```

局域网放行防火墙（管理员 PowerShell，只需执行一次）：

```powershell
New-NetFirewallRule -DisplayName "GBA Status" -Direction Inbound -LocalPort 8765 -Protocol TCP -Action Allow
```

## 数据来源

| 数据 | 来源 |
|---|---|
| 任务状态、队列、最近完成、失败原因 | `data/world_building_download_tasks_state.db`（SQLite 只读连接） |
| 当前任务分块进度 | 任务目录 `GBA_*.gpkg` 数量 ÷ `gba_wfs_grid.gpkg` 要素数 |
| 产物大小、SHP 分卷数 | 当前任务目录扫描 |
| 日志尾行、卡住判断 | 任务目录 `run.log`，超过 `--log-stale-minutes`（默认 10 分钟）未更新会标红 |
| 处理队列状态、阶段、分片进度、日志尾 | `data/shp_process_state.db`（`process_tasks` / `process_events`，由 `scripts/run_shp_process_tasks.py` 写入） |
| 处理产物大小 | 处理输出目录 `out_data/<数据集>_pipeline/final` 扫描 |
| 磁盘剩余 | 仓库所在磁盘 |

## 接口

- `GET /`：HTML 看板页面
- `GET /api/status`：JSON 快照（页面每 15 秒调用），支持 `queue_offset`、`queue_limit` 分页与 `queue_search`（按城市中英文模糊搜索，大小写不敏感）参数；返回 `queue_total`（过滤后条数）、`queue_total_all`（未过滤总数）；`processing` 字段为处理队列快照
- `GET /api/control`：批处理控制状态（运行中、runner PID、worker PID、当前 tile-workers）
- `POST /api/restart`：重启批处理（并发固定 `tile_workers=2`），无需请求体参数
- `POST /api/stop`：停止当前下载任务（不重启），返回停止的 PID 列表
- `POST /api/queue/reorder`：调整下载待执行队列顺序，请求体 `{"task_id": "...", "direction": "up|down|top"}`（兼容 `dataset_key` 字段名）；`top` 跨页生效；排序在下次启动批处理时生效；受 `--action-token` 保护
- `POST /api/queue/requeue`：把失败任务重新入队，请求体 `{"task_id": "...", "position": "tail|top"}`（缺省 `tail`；兼容 `dataset_key` 字段名）；任务不存在返回 404，非 FAILED 状态或非法 position 返回 400；成功后返回 `{"ok": true, "task_id": ..., "position": ..., "queue": [...], "pending_total": N}`；受 `--action-token` 保护
- `POST /api/queue/requeue-completed`：确认删除已完成任务的整个下载目录（分片、完成标记、网格清单、合并/分卷产物）后，把该任务重置为待执行并置顶到队首，同时重置下游「数据处理」队列中的同一数据集（已完成处理会删除其产物与输入副本），并**删除该数据集在「库表更新」页签的手工标记**（`db_update_status.json` 中移除该条目）。请求体包含 `task_id`、`confirm_delete: true` 和页面展示的 `expected_download_dir`。服务端重新校验状态（仅 OK/OK_EMPTY）、路径（须在仓库根内且非符号链接、不与其它任务共享）、完成标记及历史数据引用；失败返回 400/404，清理未完成时返回 200 且 `ok:false`、`error` 说明；受 `--action-token` 保护
- `POST /api/process/reorder`：调整处理队列顺序，请求体 `{"dataset_key": "...", "direction": "up|down|top"}`；受 `--action-token` 保护
- `POST /api/process/requeue`：把处理失败任务重新加入待处理队列，请求体 `{"dataset_key": "...", "position": "tail|top"}`（缺省 `tail`）；任务不存在返回 404，非 FAILED 状态或非法 position 返回 400；成功后返回 `{"ok": true, "dataset_key": ..., "position": ..., "queue": [...], "pending_total": N}`；受 `--action-token` 保护
- `POST /api/process/requeue-completed`：确认永久删除处理产物后，将已完成任务重置并置顶，并把该数据集在「库表更新」页签的手工标记**重置为“未更新”**（`updated=false`，清空更新时间与备注）；请求体包含 `dataset_key`、`confirm_delete: true` 和页面展示的 `expected_output_dir`、`expected_input_dir`。服务端重新校验状态、路径及共享引用；受 `--action-token` 保护
- `POST /api/process/note`：保存处理队列备注，请求体 `{"dataset_key": "...", "note": "..."}`（≤200 字符；key 不存在返回 404）；受 `--action-token` 保护
- `POST /api/process/sync`：扫描下载完成的数据并同步到处理队列（等价于 `run_shp_process_tasks.py --sync`），返回 `{"ok": true, "total": N, "added": N, "updated": N}`；受 `--action-token` 保护
- `GET /api/process/control`：处理队列控制状态（运行中、执行器 PID、当前任务、默认日志文件）
- `POST /api/process/start`：启动处理队列，请求体 `{"limit": 0, "wait_resources": true}`（0=全部待处理；已在运行返回 409）；受 `--action-token` 保护
- `POST /api/process/stop`：停止处理队列（连同 pipeline 子进程），并把 RUNNING 任务重置为 PENDING；受 `--action-token` 保护
- `GET /api/db_updated`：库表更新看板快照（已处理完成任务 + 手动标记）
- `POST /api/db_updated`：保存手动标记，请求体 `{"rows": [{"dataset_key": "...", "updated": true, "updated_at": "...", "note": "..."}]}`；受 `--action-token` 保护
- `GET /api/config`：读取看板配置（数据库连接 + 区域成果 SHP 共享目录 + QGIS 安装目录），附 `env` 环境存在性快照（含 QGIS）
- `POST /api/config`：保存配置，请求体 `{"database": {"host": "...", "port": 5432, "dbname": "...", "user": "...", "password": "..."}, "result_root": "...", "qgis_dir": "D:\\QGIS"}`；写入 `data/config.json`；校验端口 1~65535、主机/库名/用户/共享目录非空；受 `--action-token` 保护
- `GET /api/env`：环境检测——逐项检查入库工具解释器（存在 + 能否 `import geopandas, psycopg2, dotenv`）、入库脚本、入库日志目录、处理工具解释器（存在 + `import geopandas, shapely, pyproj`）、QGIS（`<qgis_dir>/bin/qgis_process-qgis.bat`）；返回 `{"ok": bool, "missing": [...], "items": [...]}`
- `POST /api/db_updated/import`：对某区域执行“更新到库”。请求体 `{"dataset_key": "洲|国家|区域"}`；先检查共享目录 `result_root/洲/国家/区域/*.shp` 与处理产物 `out_data/<前缀>_pipeline/final/*_3857.shp` 是否一致：
  - 不一致 → 返回 `{"ok": false, "consistent": false, "missing": [...], "extra": [...], "message": "内容不一致，请手动检查共享目录"}`，**不执行**；
  - 一致 → 后台运行 `Tools/twopyshp2pgsql/main3_region.py`（数据库连接与共享目录取自 `/api/config`，以环境变量注入），成功后自动把该区域登记为「已更新」；受 `--action-token` 保护
- `GET /api/db_updated/import-status`：查询入库任务状态（`running|done|error|blocked|idle`）
- 其他路径返回 404

## 数据处理页签

数据来源为独立处理库 `data/shp_process_state.db`，由 `scripts/run_shp_process_tasks.py` 维护：

- 处理控制：显示运行状态 / 执行器 PID / 当前任务；可填「数量」（0=全部待处理）并勾选「资源不足时等待」，点「开始执行」在后台启动处理队列；点「停止处理」会连同 pipeline 子进程一起停止，并把 RUNNING 任务重置为「待处理」（可再次开始续跑）；点「同步队列」手动扫描下载完成的数据并加入待处理队列（等价于 `--sync`）。执行器日志 `data/logs/process/run.log`
  - 注意：启动后为独立后台进程，重启看板不影响它；与下载解耦，处理前建议先在「建筑数据下载」页签停止下载
  - 下载完成的任务默认会自动登记进处理队列（由 `run_world_building_tasks.py` 在任务成功后写入，`--no-process-sync` 可关闭）；看板启动前的历史完成数据可点「同步队列」补登记
  - 对已处理完成（`OK`）的数据集，同步会比较下载完成时间及源 SHP 文件指纹；发现源数据晚于处理完成时间或文件指纹变化时，自动恢复为 `PENDING`。队列顺序和备注保留，旧尝试次数、阶段、进度、日志、错误和完成时间清空
- 左侧卡片：处理任务总数、运行中、待处理、已完成、失败/阻塞
- 当前处理任务：命名前缀、阶段（准备输入 / 切分 / QGIS 校验 / 清洗 / 重投影 / 产物校验）、分片进度条（n/N）、预计剩余、源要素数、产物大小、来源与输出目录、stdout 日志尾（超过 10 分钟无进度更新标记「可能卡住」）
- 待处理队列：排序（`#` 序号 + ↑/↓/⤒ 按钮）、数据集、命名前缀、来源目录、要素数、SHP 文件数、备注
- 队列备注：可直接编辑（上限 200 字符），**失焦或回车自动保存**、`Esc` 撤销；保存到处理库 `process_tasks.note`，重跑/sync 不会清空；与「库表更新」页签的备注（存 `db_update_status.json`）互相独立
- 队列排序：点「排序」列按钮即可手动调整待处理任务的执行顺序（即时写入处理库 `queue_order`），**在下次 `--run` 时生效**；也可用 CLI `--move-up / --move-down / --move-top / --order` 调整
- 最近完成：命名前缀、分片数、要素数、耗时、完成时间、输出目录
- 处理「最近完成」每项提供两个按钮：**「重做处理」**——确认后永久删除该任务的处理输出目录和处理输入副本，**保留原始下载数据**，将任务重置为待处理并排到队首；**「重新下载」**——删除原始下载目录 + 处理产物，退回下载队列（该数据集在数据处理中置为「等待重新下载」，下载完成后自动恢复为「待处理」）。两者都会把该数据集在「库表更新」页签的手工标记重置/删除。服务端拒绝共享目录或输出根目录以外的删除路径
- 等待重新下载：左侧卡片单独计数；该状态不出现在「待处理队列」，「重新下载」完成后自动恢复为待处理
- 失败/阻塞：失败原因与尝试次数；失败表提供「操作」列：**「重新入队」**——重置为待处理并添加到队首（清空阶段与错误，尝试次数保留），**在下次 `--run` 时生效**；阻塞表示只有合并 gpkg、缺少交付 SHP（先运行 `python scripts/run_world_building_tasks.py --export-shp-only`）
- 命名规则：处理阶段以 `<process_name_prefix>` 开头（prefix 取下载清单 CSV 第 5 列，只读；缺失时从输入文件名推导）：输入副本 `<prefix>.shp` / `<prefix>_partNN.shp`，目录 `<prefix>_pipeline(_input)`，最终分片 `<prefix>_xxx_xxx_3857.shp`

队列命令：

```powershell
python scripts/run_shp_process_tasks.py --sync      # 发现可处理数据集并入队
python scripts/run_shp_process_tasks.py --dry-run   # 预览执行计划
python scripts/run_shp_process_tasks.py --run       # 串行执行（含资源护栏）
python scripts/run_shp_process_tasks.py --summary   # 队列统计
python scripts/run_shp_process_tasks.py --running   # 当前处理任务
python scripts/run_shp_process_tasks.py --order "上海,云南,宁夏"   # 按指定顺序重排
python scripts/run_shp_process_tasks.py --move-top "宁夏"          # 置顶
```

## 库表更新页签

数据来源：处理库 `data/shp_process_state.db` 中状态为 `OK` 的任务 + 手动维护文件 `data/db_update_status.json`。

- 左侧卡片：已处理任务总数、已更新、未更新
- 子选项卡：「未更新到库」「更新到库」两栏（按钮上显示各自条数），按「已更新」状态筛选，默认显示「未更新到库」
- 表格列：数据集、命名前缀、处理完成时间、输出目录、备注（可编辑）；「更新到库」栏额外有「更新时间」（只读，由入库自动填入）与「更新详情」列，「未更新到库」栏额外有「操作」列
- 「备注」：两栏都可编辑，失焦/回车自动保存（仅内容变化时提交），结果用**浮层（仿 element-ui Message）提示**；失败回退到上次已保存值。编辑中暂停自动刷新以免覆盖。
- 「更新详情」：该区域最近一次入库的 `删除 X 条 / 新增 Y 个 SHP`（含跳过数），来自 `data/db_import_status.json` 的 `import_detail`；解析 `main3_region.py` 输出的 `__IMPORT_SUMMARY__` JSON 汇总行
- 「未更新到库」的行提供「操作 → 更新到库」按钮：点击后**先检查**共享目录 `result_root/洲/国家/区域/*.shp` 与处理产物 `out_data/<前缀>_pipeline/final/*_3857.shp` 是否一致（按文件名比对）；不一致则弹窗列出缺失/多余并浮层提示、不执行；一致则后台运行 `Tools/twopyshp2pgsql/main3_region.py`，成功后自动登记为「已更新」，结果用**浮层提示**；入库进行中按钮保持禁用。数据库连接与共享目录取自「配置」页签；入库结果写入 `data/db_import_status.json`，服务重启/刷新后仍可查询（`GET /api/db_updated/import-status`）
- 表格分页：默认每页 15 条，可切换 15/30/50/100/200，支持首页/上一页/下一页/末页
- 不再有「保存修改」按钮：更新时间由系统写入，备注自动保存
- 重做会同步标记：处理页「最近完成」的「重做处理」会把该数据集标记重置为「未更新」；下载页「最近完成」的「重新下载」会删除该数据集标记（任务转 PENDING 后该行也从本表消失）
- 需要重新扫描处理结果时先运行 `python scripts/run_shp_process_tasks.py --sync`（或点数据处理页签的「同步队列」）

## 批处理控制

页面「批处理控制」区提供：

- 当前状态：运行中 / 未运行、当前 tile-workers、runner PID
- 「重启批处理」按钮（会二次确认；`tile-workers` 固定为 2）
- 「停止下载任务」按钮（会二次确认；只停止进程，不做其他操作，已完成分块保留）

重启逻辑（服务端）：

1. 先停 runner 并等待其退出（避免 runner 因 worker 退出触发自动重试再起新 worker）
2. 再停所有 worker（会重新探测，覆盖刚被 runner 拉起的 worker）
3. 启动批处理（并发固定 `tile-workers=2`），日志覆盖写入 `data/logs/download/run.log`

停止逻辑（服务端）：与重启的停机步骤一致（先 runner 后 worker），不重新启动；中断的任务在下次启动批处理时自动从断点续跑（分块复用）。

安全选项：启动服务时加 `--action-token <令牌>` 后，重启请求必须携带该令牌（页面会提示输入并保存在浏览器本地），用于局域网防误操作。

## 常用参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `8765` | 监听端口 |
| `--state-db` | `data/world_building_download_tasks_state.db` | 状态库路径 |
| `--process-db` | `data/shp_process_state.db` | 处理队列状态库路径 |
| `--db-update-file` | `data/db_update_status.json` | 库表更新页签的手动维护数据 |
| `--page` | `scripts/status_page.html` | 页面模板路径 |
| `--queue-limit` | `15` | 队列显示条数 |
| `--recent-limit` | `10` | 最近完成/失败显示条数 |
| `--log-stale-minutes` | `10` | 日志多久未更新视为可能卡住 |
| `--refresh-seconds` | `15` | 页面自动刷新间隔 |
| `--python-exe` | 当前解释器 | 重启批处理时使用的 Python |
| `--action-token` | 空 | 可选操作令牌，设置后重启请求需匹配 |

## 说明

- 状态库使用 WAL 模式，只读查询与下载批处理的写入互不阻塞；处理库同为 WAL，页面只读。
- 预计剩余时间按"已用时间 ÷ 已完成分块 × 剩余分块"粗估，密集区域误差较大；处理页签同理（按已完成 3857 分片数粗估）。
- 若页面显示"批处理已停止（有待执行任务）"，重新执行下载命令即可续跑：
  `python -u scripts/run_world_building_tasks.py --sleep-seconds 2 --task-attempts 2`
- 处理页签若提示"处理状态库不存在"，先运行一次
  `python scripts/run_shp_process_tasks.py --sync`。
- 库表更新页签的手动标记保存在 `data/db_update_status.json`（可用 `--db-update-file` 改路径），删除该文件即可清空标记。

## 配置页签

- 配置数据库连接（主机/端口/库名/用户名/密码）、「区域成果 SHP 共享目录」与「QGIS 安装目录」，保存后写入 `data/config.json`（随 git 同步）。
- 进入页签时读取当前配置填充表单，密码以 `password` 输入框展示、可直接编辑。
- 「保存」调用 `POST /api/config`；未配置时读取回退到默认值（`main3_region.py` 默认 DB/共享目录、`D:\QGIS`）。
- 「环境 / 诊断」区显示各环境项存在状态，并可点「检测环境」调用 `GET /api/env` 做完整检测（含依赖导入）：结果用右侧浮层提示（正常/缺失），下方逐项列出明细。
