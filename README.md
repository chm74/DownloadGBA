# GBA WFS 建筑 SHP 导出脚本说明

## 项目用途

这个项目用于从 `GlobalBuildingAtlas LoD1 WFS` 服务导出带高度字段的建筑数据，输出为 GIS 可直接使用的 `SHP` 或 `GPKG` 文件。

## 获取代码

- 仓库地址：`https://github.com/chm74/DownloadGBA.git`
- 首次克隆：`git clone https://github.com/chm74/DownloadGBA.git`
- 更新：`git -c http.proxy=http://127.0.0.1:7890 pull --ff-only origin main`（无代理时删掉 `-c http.proxy=...`）

当前项目保留三类下载脚本，并新增一套任务清单驱动的批量下载链路：

- `scripts/download_gba_lod1_wfs.py`
  单次导出脚本，适合按范围、按地名、按本地边界抓取建筑数据。
- `scripts/download_gba_lod1_wfs_adaptive.py`
  自适应导出脚本，适合遇到超大格网、单格建筑量过高或远端请求不稳定时使用。
- `scripts/run_gba_province_batch.py`
  省级批处理脚本，适合批量运行多个省份任务，并统一记录日志与汇总结果。
- `scripts/run_world_building_tasks.py`
  按任务清单批量下载，任务状态保存到 SQLite，支持断点续跑、失败重试与完成标记。
- `scripts/world_tasks_lib.py`
  任务清单链路的支撑库，被 `run_world_building_tasks.py` 调用。
- `scripts/sync_existing_data.py`
  扫描已下载的省/市数据目录，把路径同步到任务状态库（看板「最近完成」表格显示）。
- `scripts/status_server.py` + `scripts/status_page.html`
  状态与操作看板：浏览器查看进度、调整队列、控制批处理，并操作数据处理与库表更新（详见下文第 5 节与 `docs/GBA建筑下载操作手册.md`）。
- `scripts/export_world_building_tasks.py`
  从 PostgreSQL 的 `world_building` 表导出按洲、国家、城市分组后的下载任务清单。

## 脚本关系

`run_gba_province_batch.py` 是批处理入口，内部会调用 `download_gba_lod1_wfs.py` 执行单个省份的实际下载任务。

`download_gba_lod1_wfs_adaptive.py` 与 `download_gba_lod1_wfs.py` 共用同一抓取内核（超大范围/截断/失败时的四分裂切分已内置在 `scripts/gba_wfs_client.py`），该脚本仅保留自适应入口与兼容参数，便于沿用旧命令。

推荐理解顺序：

1. 先看 `download_gba_lod1_wfs.py`，理解单次抓取逻辑。
2. 再看 `scripts/gba_wfs_client.py`，理解请求节流与自适应切分（`download_gba_lod1_wfs_adaptive.py` 与其共用该内核）。
3. 最后看 `run_gba_province_batch.py`，理解批量调度、日志和汇总方式。

## 数据源

- WFS 服务：`https://tubvsig-so2sat-vm1.srv.mwn.de/geoserver/ows`
- 图层名：`global3D:lod1_global`
- 数据源标识：`GlobalBuildingAtlas LoD1 WFS`

## 脚本清单

### 0. `scripts/export_world_building_tasks.py`

#### 用途

从 PostgreSQL 表 `world_building` 读取 `continent`、`country`、`city` 和 `shp_name`，去除空值并按前三个字段分组，生成后续自动下载使用的五列 CSV 清单。第四列 `download_dir` 按项目内的 `data\\洲\\国家\\城市` 规则生成；第五列 `process_name_prefix` 取 `shp_name` 在第一个下划线前的部分。每个唯一任务组合只保留一行，并按洲、国家、城市排序。

默认输出：

- `data/world_building_download_tasks.csv`

输出示例：

```csv
continent,country,city,download_dir,process_name_prefix
Africa,Algeria,Algeria,data\Africa\Algeria\Algeria,Algeria
Africa,Angola,Angola,data\Africa\Angola\Angola,Angola
Africa,Benin,Benin,data\Africa\Benin\Benin,Benin
```

建议通过环境变量传递数据库密码：

```powershell
$env:PGPASSWORD = "数据库密码"
python scripts/export_world_building_tasks.py
Remove-Item Env:PGPASSWORD
```

连接参数的默认值为 `building`、`postgres`、`172.16.1.145`、`5432`，可分别通过 `--dbname`、`--user`、`--host`、`--port` 覆盖；也可用 `--output` 指定清单位置。脚本依赖 PostgreSQL 自带的 `psql` 客户端，不需要额外安装 Python 数据库驱动。

### 1. `scripts/download_gba_lod1_wfs.py`

#### 用途

直接从官方 `GBA WFS` 服务抓取建筑面和高度字段，支持三种入口模式：

- `--bbox`
  直接按经纬度范围抓取，适合局部区域和烟雾测试。
- `--place`
  按地名解析边界后分格网抓取，适合整市、整区。
- `--boundary`
  按本地边界文件分格网抓取，适合已有 `SHP/GPKG` 边界的场景。

#### 主要能力

- 调用 WFS 按范围抓取建筑要素；返回被截断或 `BBOX_TOO_LARGE` 时自动四分裂更小范围（分页参数为兼容保留，切分实现在 `scripts/gba_wfs_client.py`）
- 自动把字段标准化为统一输出列
- 按边界自动生成格网并分块抓取
- 支持复用已存在分块结果，便于断点续跑
- 支持把多个分块合并为最终成果文件
- `--place` / `--boundary` 的最终合并成果默认按建筑内部代表点过滤到行政边界；原始分块仍保留完整格网

#### 主要输入

- 经纬度范围 `--bbox`
- 地名 `--place`
- 本地边界文件 `--boundary`

#### 主要输出

- `place_boundary.gpkg`
- `gba_wfs_grid.gpkg`
- `GBA_*.gpkg` 或 `GBA_*.shp`
- `*_buildings_height_gba.shp`
- `*_buildings_height_gba.gpkg`

#### 默认输出字段

- `GBA_ID`
- `Height`
- `H_VAR`
- `SOURCE`
- `REGION`
- `TILE_ID`
- `geometry`

#### 常用参数

- `--output`
  `--bbox` 模式下的输出文件。
- `--output-dir`
  `--place` / `--boundary` 模式下的输出目录。
- `--grid-size`
  分格网大小，默认 `0.2`。
- `--page-size`
  兼容参数（默认 `5000`）；当前抓取按范围整段返回，不使用分页。
- `--max-pages`
  烟雾测试时限制分页数量。
- `--tile-format`
  分块输出格式，可选 `gpkg` 或 `shp`，默认 `gpkg`。
- `--merge-output`
  可选，指定最终合并输出文件。
- `--boundary-filter`
  最终合并成果的行政边界过滤方式，可选 `representative-point`（默认）或 `none`。代表点模式保留整栋建筑，不切割建筑面。
- `--boundary-buffer-meters`
  过滤前对行政边界向外缓冲的米数，默认 `0`；边界精度不足时可按需设置小幅缓冲。

#### 示例

按局部范围导出：

```bash
python scripts/download_gba_lod1_wfs.py ^
  --bbox 116.2,39.8,116.4,40.0 ^
  --output data/gba_wfs/beijing_bbox_height.shp ^
  --page-size 5000
```

按地名导出：

```bash
python scripts/download_gba_lod1_wfs.py ^
  --place 北京市 ^
  --output-dir data/beijing_gba_wfs ^
  --grid-size 0.02 ^
  --page-size 5000 ^
  --tile-format gpkg ^
  --merge-output data/beijing_gba_wfs/beijing_buildings_height_gba.shp
```

按本地边界导出：

```bash
python scripts/download_gba_lod1_wfs.py ^
  --boundary data/beijing_admin/beijing_adm1.shp ^
  --output-dir data/beijing_gba_wfs ^
  --grid-size 0.02 ^
  --page-size 5000 ^
  --tile-format gpkg ^
  --merge-output data/beijing_gba_wfs/beijing_buildings_height_gba.shp
```

### 2. `scripts/download_gba_lod1_wfs_adaptive.py`

#### 用途

这个脚本与 `scripts/download_gba_lod1_wfs.py` 共用同一抓取内核（自适应切分已内置在 `scripts/gba_wfs_client.py`），适合下面两类场景：

- 单个请求范围过大：客户端在请求前按跨度上限（`0.09°`）自动预切分
- 请求被截断、返回 `BBOX_TOO_LARGE` 或持续失败：自动四分裂为更小范围再试（最小 `0.003°`）

它保留了和原脚本一致的三种入口：

- `--bbox`
- `--place`
- `--boundary`

三种入口共用同一抓取内核，切分行为一致。

#### 自适应规则（由 WFS 客户端实现）

- 请求前预切分：单次请求跨度超过 `0.09°` 时先四分裂
- 截断检测：返回要素数少于 `numberMatched`，或返回 `BBOX_TOO_LARGE` 时，自动四分裂重试
- 失败分裂：请求持续失败时自动四分裂；到达最小跨度 `0.003°` 后不再切分
- 例外：源站 429 限流只等待重试，绝不切分

#### 兼容参数

> 说明：实际切分由客户端决定（跨度上限 `0.09°`、最小 `0.003°`）；以下参数为兼容保留、当前不生效：

- `--split-threshold`（默认 `20000`）
- `--failure-split-retries`（默认 `5`）
- `--min-grid-size`（默认 `0.05`）
- `--probe-page-size`（默认 `1`）

#### 适合场景

- 全国级或省级大范围下载
- 城市密集区与稀疏区差异很大
- 原始固定格网方式容易卡在单个超大格网

#### 示例

按国家边界自适应下载：

```bash
python scripts/download_gba_lod1_wfs_adaptive.py ^
  --place Mongolia ^
  --output-dir data/mongolia_gba_wfs_adaptive ^
  --grid-size 0.5 ^
  --page-size 5000 ^
  --tile-format gpkg ^
  --split-threshold 20000 ^
  --failure-split-retries 5 ^
  --min-grid-size 0.05 ^
  --probe-page-size 1 ^
  --merge-output data/mongolia_gba_wfs_adaptive/mongolia_buildings_height_gba.shp
```

按本地边界自适应下载：

```bash
python scripts/download_gba_lod1_wfs_adaptive.py ^
  --boundary data/beijing_admin/beijing_adm1.shp ^
  --output-dir data/beijing_gba_wfs_adaptive ^
  --grid-size 0.2 ^
  --page-size 5000 ^
  --tile-format gpkg ^
  --split-threshold 10000 ^
  --failure-split-retries 5 ^
  --min-grid-size 0.02 ^
  --probe-page-size 1 ^
  --merge-output data/beijing_gba_wfs_adaptive/beijing_buildings_height_gba.shp
```

### 3. `scripts/run_gba_province_batch.py`

#### 用途

这个脚本用于批量执行省级建筑导出任务。它不会直接下载建筑数据，而是按省份逐个组装命令，再调用 `download_gba_lod1_wfs.py` 执行。

适合场景：

- 一次跑多个省份
- 优先使用本地省界文件
- 需要保留每个省份的运行日志
- 需要生成批处理汇总表

#### 主要能力

- 内置省级目标清单和省份别名解析
- 支持 `--provinces`、`--province-list`、`--all-mainland` 三种批量入口
- 优先从 `data/province_admin` 查找本地边界文件
- 找不到本地边界时可回退到 `--place`
- 为每个省份生成独立输出目录、运行日志和错误日志
- 生成 `batch_summary.csv` 汇总执行状态
- 支持 `--dry-run` 预览命令

#### 主要输入

- 省份列表或全部省份
- 本地边界根目录 `--boundary-root`
- 批量输出根目录 `--output-root`
- 单省任务通用参数，如 `--grid-size`、`--page-size`

#### 主要输出

每个省份通常会输出到：

- `data/province_batches/<slug>_gba_wfs/`

目录内常见内容：

- 最终成果文件 `*_buildings_height_gba.shp` 或 `gpkg`
- `run.log`
- `run.err.log`

批量汇总文件默认输出为：

- `data/province_batches/batch_summary.csv`

#### 常用参数

- `--provinces`
  直接传省份 slug 或别名。
- `--province-list`
  传入 JSON/Python 风格的省份列表字符串。
- `--all-mainland`
  执行全部内置省级目标。
- `--exclude`
  排除指定省份。
- `--boundary-root`
  本地省界目录，默认 `data/province_admin`。
- `--require-boundary`
  找不到本地边界时直接失败，不回退到 `--place`。
- `--output-root`
  批处理输出根目录，默认 `data/province_batches`。
- `--grid-size`
  省级任务默认 `0.5`。
- `--tile-format`
  分块格式，默认 `gpkg`。
- `--merge-format`
  最终合并格式，可选 `shp` 或 `gpkg`，默认 `shp`。
- `--boundary-filter` / `--boundary-buffer-meters`
  省级最终成果默认按建筑内部代表点过滤到省界；可关闭过滤或设置小幅边界缓冲，原始格网不受影响。
- `--skip-existing-merge`
  如果最终成果已存在则跳过该省份。
- `--summary-file`
  自定义汇总 CSV 路径。
- `--dry-run`
  只打印计划执行内容，不实际运行。

#### 示例

运行指定省份：

```bash
python scripts/run_gba_province_batch.py ^
  --provinces guangdong zhejiang ^
  --boundary-root data/province_admin ^
  --output-root data/province_batches ^
  --grid-size 0.5 ^
  --page-size 5000 ^
  --tile-format gpkg ^
  --merge-format shp
```

按省份列表字符串执行：

```bash
python scripts/run_gba_province_batch.py ^
  --province-list "['海南省','上海市','黑龙江省']" ^
  --skip-existing-merge
```

只预览即将执行的命令：

```bash
python scripts/run_gba_province_batch.py ^
  --all-mainland ^
  --dry-run
```

### 4. `scripts/run_world_building_tasks.py`

#### 用途

按 `data/world_building_download_tasks.csv` 任务清单批量下载建筑数据，任务状态保存到 SQLite，适合长时间、可中断、可续跑的全球批量生产。

#### 主要能力

- 自动识别清单编码（UTF-8 / GBK），校验字段与路径
- 边界解析：覆盖表 `skip` 首先判定；其余情况优先复用任务目录缓存 `place_boundary.gpkg`，无缓存时依次为覆盖表（`bbox` / `boundary` / `query`）→ 本地行政边界（Natural Earth）→ 在线地名解析。已缓存任务新增/修改覆盖后需加 `--refresh-boundary` 才重新解析
- 逐任务调用下载脚本（viewerSession 自动刷新、0.09° 请求自动切分），产物、日志、完成标记写入标准下载目录
- 任务完成自动导出交付用 SHP，超大任务按要素数自动分卷（`--shp-max-features`）
- SQLite 状态库支撑断点续跑、只跑失败项、强制重跑，`--export-shp-only` 可为历史成果补导出 SHP
- 待执行队列支持手动排序（上移 / 下移 / 置顶），页面与 CLI 均可操作，改动在下次启动批处理时生效
- 任务完成写 `_TASK_DONE.json`，任务目录可独立交付与校验

#### 常用参数

- `--tasks`
  清单路径，默认 `data/world_building_download_tasks.csv`。
- `--state-db`
  SQLite 状态库，默认 `data/world_building_download_tasks_state.db`。
- `--overrides`
  覆盖表路径，默认 `data/world_place_overrides.csv`。
- `--only` / `--continent` / `--country`
  按任务或区域过滤，逗号分隔。
- `--retry-failed` / `--status` / `--force`
  失败重试、指定状态、强制重跑。
- `--dry-run`
  只打印计划，不下载；注意仍会先把清单同步进状态库（可能新增/更新/重置任务）。
- `--sync-only` / `--summary` / `--running`
  只同步清单入库 / 只打印状态统计 / 打印当前正在执行的任务与分块进度。
- `--export-shp-only`
  为已完成的下载任务补导出交付用 SHP。
- `--move-up` / `--move-down` / `--move-top`
  调整待执行队列顺序（task_id 或子串，逗号分隔；置顶跨页生效，多个置顶按输入顺序排列），执行后退出，不下载。
- `--tile-workers`
  单任务内并发抓取的格网数，默认 1；受源站限流（约 60 请求/分钟）约束，推荐 2，调到 3 以上不会更快。
  客户端内置全局节流（默认 1.05 秒/请求，可用 `GBA_WFS_MIN_INTERVAL` 调整），遇到 429 自动等待重试。
- `--boundary-filter` / `--boundary-buffer-meters`
  最终成果默认按建筑内部代表点归属行政边界，消除省份/城市成果的阶梯状格网外轮廓；完整格网仍保存在 `tiles_national_v1`，可用 `--boundary-filter none` 保留旧的全格网合并语义。
- `--task-attempts` / `--sleep-seconds` / `--stop-on-error`
  单任务尝试次数、任务间隔、失败即停。

#### 示例

```bash
python scripts/run_world_building_tasks.py --sync-only
python scripts/run_world_building_tasks.py --dry-run --max-tasks 5
python scripts/run_world_building_tasks.py --continent Africa --sleep-seconds 2
python scripts/run_world_building_tasks.py --retry-failed
python scripts/run_world_building_tasks.py --move-top "Botswana"
```

完整目录规范、SQLite 结构与状态机见 `scripts/world_tasks_lib.py` 与相关单测（`tests/test_world_tasks_lib.py`、`tests/test_run_world_building_tasks.py`）。

### 5. `scripts/status_server.py`

#### 用途

网页看板：浏览器查看下载进度、队列（全部待执行任务，支持翻页、每页条数、按城市中英文搜索、手动排序与置顶）、最近完成与失败任务；失败任务可「重新入队」（置顶队首，不删除已下载数据）；队列任务可「重新下载」（删除该任务整个下载目录并重置到队首，同时把下游处理任务置为「等待重新下载」、删除其「库表更新」记录）。「批处理控制」支持重启/停止批处理；「CSV清单管理」页可检查 / 追加任务清单（追加后将 CSV 同步到状态库；有下载进程运行时同步会被拒，需先停止）。「数据处理」页签支持「开始执行 / 停止处理 / 同步队列」，并展示处理阶段、分片进度、分卷「自动合并」与日志尾。「库表更新」页签可将处理成果「拷贝到共享目录」「更新到库」（成功后自动登记为已更新；页面顶部按大洲汇总实际任务总数、建筑数据已下载/未下载数（未下载仅统计待下载任务）与处理完成率（点击大洲卡片可查看该洲建筑数据下载概览（未下载 / 已下载页签）），两栏均按大洲分组，「未更新到库」组内按处理完成时间倒序展示）。另有「日志」页签集中查看各阶段日志、「配置」页签保存数据库/共享目录/QGIS/处理默认数量并「检测环境」。

#### 启动与访问

```powershell
python -u scripts/status_server.py --port 8765
```

- 本机访问 `http://127.0.0.1:8765/`，局域网访问 `http://<本机IP>:8765/`
- 仓库根目录默认取**启动看板时的当前工作目录**；`启动.bat` 会先切换到自身所在目录。需要显式指定时用 `--repo-root <目录>`
- 后台常驻：`Start-Process python -ArgumentList "-u","scripts\status_server.py","--port","8765" -WorkingDirectory "E:\LoD1" -WindowStyle Hidden`
- 局域网首次访问需放行防火墙：`New-NetFirewallRule -DisplayName "GBA Status" -Direction Inbound -LocalPort 8765 -Protocol TCP -Action Allow`
- 局域网防误操作（可选）：加 `--action-token <令牌>`，重启操作需在页面输入该令牌
- 「最近完成」表格含「下载目录」列：显示该任务的下载数据目录；点该行「重新下载」会**永久删除该目录**并把任务重置为待执行队首（同时重置下游数据处理任务、删除其「库表更新」记录），请谨慎操作

完整说明见 `docs/GBA建筑下载操作手册.md`。

### 6. `scripts/run_shp_process_tasks.py`

#### 用途

建筑 SHP 处理队列：从「下载已完成」的数据中发现交付 SHP，串行执行切分 → QGIS 有效性校验 → 清洗 → EPSG:3857 重投影（调用 `Tools/Oneshp_pipline_qgis`），任务状态保存到独立库 `data/shp_process_state.db`。

#### 主要能力

- `--sync` 入队：下载状态库中 `existing_data_dir`/已完成任务 + 扫描 `data` 下 `*_buildings_height_gba*.shp` + `--register` 显式登记；只有 gpkg 的记为 `BLOCKED_NO_SHP`
- 命名规则：处理阶段以 `<process_name_prefix>` 开头命名（prefix 取下载清单 CSV 第 5 列，只读；缺失时从输入文件名推导）。
  输入副本 `<prefix>.shp` 或 `<prefix>_partNN.shp`，输出目录 `<prefix>_pipeline`，最终 3857 分片 `<prefix>_xxx_xxx_3857.shp`
- `--run` 执行：资源护栏（可用内存/磁盘不足时不启动）、隔离输入目录、调用处理工具、每 5 秒写阶段与分片进度、产物校验（CRS=3857 + 分片要素数）、写 `_PROCESS_DONE.json`、回写下载库 `processed_3857_dir`
- 手动排序：待处理队列支持 `queue_order` 顺序，可用 `--move-up / --move-down / --move-top` 或 `--order` 调整；看板「数据处理」页签的「排序」列提供 ↑/↓/⤒ 按钮，改动在下次 `--run` 生效
- 页面控制：看板「数据处理」页签支持「开始执行 / 停止处理」；处理数量取「配置」页「处理默认数量」（0=全部），可选是否等待资源，开始前会拦截上次残留的处理进程；停止会先确认处理进程完全退出（提示「未完全停止」时任务保持 RUNNING，可稍后重试停止），再把 RUNNING 任务重置为 PENDING；执行器日志 `data/logs/process/run.log`
- 队列备注：任务备注（≤200 字符，存 `process_tasks.note`），重跑与 sync 不会清空（接口 `/api/process/note`；看板处理页暂未提供编辑入口）
- 断点续跑：中断的 RUNNING 自动重置为 PENDING；`--retry-failed` 重试失败项；`--force` 强制重跑
- `--summary / --running`：CLI 查看队列统计与当前处理任务（与看板「数据处理」页签同源）

#### 常用参数

- `--process-db`
  处理状态库，默认 `data/shp_process_state.db`。
- `--download-db`
  下载状态库，默认 `data/world_building_download_tasks_state.db`。
- `--tasks`
  下载任务清单 CSV（`process_name_prefix` 权威来源，只读），默认 `data/world_building_download_tasks.csv`。
- `--output-root`
  输入/输出根目录，默认 `Tools/Oneshp_pipline_qgis/out_data`。
- `--only` / `--limit` / `--retry-failed` / `--force` / `--dry-run`
  过滤、限流、重试、强制与预览。
- `--min-free-ram-gb` / `--min-free-disk-gb` / `--wait-resources`
  资源护栏阈值（默认 3GB / 50GB）与等待模式。
- `--reset-task`
  把指定 `dataset_key` 重置为 PENDING。
- `--move-up` / `--move-down` / `--move-top`
  把指定任务（`dataset_key` 或前缀子串，逗号分隔）在待处理队列中上移/下移/置顶。
- `--order`
  按给定顺序重排待处理队列（未列出的保持原相对顺序），逗号分隔。

#### 示例

```powershell
python scripts/run_shp_process_tasks.py --sync
python scripts/run_shp_process_tasks.py --summary
python scripts/run_shp_process_tasks.py --dry-run --only "广西"
python scripts/run_shp_process_tasks.py --run --only "广西"
python scripts/run_shp_process_tasks.py --running
python scripts/run_shp_process_tasks.py --order "上海,云南,宁夏"
python scripts/run_shp_process_tasks.py --move-top "宁夏"
```

#### 注意事项

- 处理只针对下载已完成的数据，不修改原始下载目录；输入会复制到 `out_data/<数据集>_pipeline_input`，产物在 `out_data/<数据集>_pipeline`。
- 高度字段名需为 `HEIGHT` 或 `Height`，否则清洗阶段的零高剔除不生效（可用 `Tools/Oneshp_pipline_qgis/rename_height.py` 先改名）。
- 单个大文件处理以小时计，建议后台运行并用看板「数据处理」页签观察进度。

完整说明见 `docs/GBA建筑下载操作手册.md` 第 7 节「数据处理」。

## 推荐使用方式

1. 先用 `download_gba_lod1_wfs.py --bbox` 做小范围验证。
2. 常规区域下载优先使用 `download_gba_lod1_wfs.py`。
3. 如果遇到超大格网或连续失败，切换到 `download_gba_lod1_wfs_adaptive.py`。
4. 最后再使用 `run_gba_province_batch.py` 执行多省批处理。

## 相关文档

- `docs/GBA建筑下载操作手册.md`（项目简介 / 整个链路步骤 / 前置准备 / 启动 / 下载 / 修改优先级 / 数据处理 / 库表更新 / 日志 / 常见问题）

## 部署环境清单

### 需安装的软件

| 软件 | 用途 | 是否必需 |
|---|---|---|
| Python 3.12 | 运行所有脚本 | 必需 |
| uv | 建虚拟环境 / 装依赖 | 必需（否则用各工具 `setup.ps1`） |
| Git | 拉取代码 / 状态同步 | 必需 |
| QGIS 3.34.8 LTR | 数据处理（`qgis_process`） | 仅处理时需要 |
| PostgreSQL + psql | 重导任务清单 / SHP 入库 | 仅这两项需要 |

> QGIS 安装目录可在看板「配置」页的「QGIS 安装目录」中配置（默认 `D:\QGIS`，启动处理时注入环境变量 `QGIS_DIR`）；如果 `qgis_process-qgis.bat` 不在 `<QGIS>\bin` 下，可在同页「qgis_process 批处理文件」直接填完整路径（注入 `QGIS_PROCESS_BAT`）。未配置时回退到 `Tools/Oneshp_pipline_qgis/src/shp_pipeline_qgis/paths.py` 中的默认值。

### Python 环境依赖

```powershell
# 主下载链（装到系统 Python）
python -m pip install -r requirements-main.txt
# 处理工具
uv sync --project Tools\Oneshp_pipline_qgis
# SHP 入库工具
uv sync --project Tools\Twopyshp2pgsql
# 一键安装（等价于以上 + 提示后续配置）
powershell -ExecutionPolicy Bypass -File setup.ps1
```

可选：`pip install pytest`（跑测试）。

> `.venv` 不可跨机复制，克隆/拷贝到新机器后按上面重建即可（不纳入版本控制）。

### 需修改 / 配置

| 配置 | 位置 | 说明 |
|---|---|---|
| git 代理 | `拉取更新.txt` | 改成新机器代理（如 `http.proxy=http://127.0.0.1:7890`），无代理则删 |
| 数据库 + 共享目录 | `data/config.json`（随仓库自带；可直接编辑或用看板「配置」页保存）；直跑工具时用 `Tools/Twopyshp2pgsql/.env` | `PG_DBNAME/PG_USER/PG_PASSWORD/PG_HOST/PG_PORT`、`DATA_ROOT` |
| 导出清单口令 | `data/config.json` 优先；可选环境变量 `PGPASSWORD` | `export_world_building_tasks.py` 默认读 `data/config.json` |
| 看板端口 | 防火墙入站 `8765` | 局域网访问需放行 |
| （可选）限速 | 环境变量 `GBA_WFS_MIN_INTERVAL` | 默认 1.05 秒/请求 |

### 无需安装（随仓库自带）

- `data/boundaries/*.gpkg`（本地边界）
- `data/*.db`（状态库）

- 最小（仅下载）：Python 3.12 + 主下载链依赖 + 能访问 WFS。
- 完整：再加 uv、QGIS 3.34.8、PostgreSQL(psql)，并配置 `.env` 与代理。
