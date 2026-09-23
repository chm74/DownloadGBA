# GBA WFS 建筑 SHP 导出脚本说明

## 项目用途

这个项目用于从 `GlobalBuildingAtlas LoD1 WFS` 服务导出带高度字段的建筑数据，输出为 GIS 可直接使用的 `SHP` 或 `GPKG` 文件。

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
  只读状态看板：浏览器查看当前任务进度、队列与最近完成。
- `scripts/export_world_building_tasks.py`
  从 PostgreSQL 的 `world_building` 表导出按洲、国家、城市分组后的下载任务清单。

## 脚本关系

`run_gba_province_batch.py` 是批处理入口，内部会调用 `download_gba_lod1_wfs.py` 执行单个省份的实际下载任务。

`download_gba_lod1_wfs_adaptive.py` 是 `download_gba_lod1_wfs.py` 的增强版副本，保留原始入口方式，同时增加超大格网的自适应切分能力。

推荐理解顺序：

1. 先看 `download_gba_lod1_wfs.py`，理解单次抓取逻辑。
2. 再看 `download_gba_lod1_wfs_adaptive.py`，理解超大格网切分逻辑。
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

连接参数的默认值为 `building`、`postgres`、`127.0.0.1`、`5432`，可分别通过 `--dbname`、`--user`、`--host`、`--port` 覆盖；也可用 `--output` 指定清单位置。脚本依赖 PostgreSQL 自带的 `psql` 客户端，不需要额外安装 Python 数据库驱动。

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

- 调用 WFS 分页下载建筑要素
- 自动把字段标准化为统一输出列
- 按边界自动生成格网并分块抓取
- 支持复用已存在分块结果，便于断点续跑
- 支持把多个分块合并为最终成果文件

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
  单次 WFS 分页大小，默认 `5000`。
- `--max-pages`
  烟雾测试时限制分页数量。
- `--tile-format`
  分块输出格式，可选 `gpkg` 或 `shp`，默认 `gpkg`。
- `--merge-output`
  可选，指定最终合并输出文件。

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

这个脚本在 `scripts/download_gba_lod1_wfs.py` 的基础上新增了超大格网自适应处理能力，适合下面两类场景：

- 单个格网内建筑数量过大，分页请求会非常多
- 某个格网请求连续失败，难以稳定拉完

它保留了和原脚本一致的三种入口：

- `--bbox`
- `--place`
- `--boundary`

其中 `--place` 和 `--boundary` 模式会启用自适应逻辑。

#### 自适应规则

- 如果某个格网探测到的 `numberMatched` 超过阈值，就自动四分裂成更小子格网
- 如果某个格网在探测阶段失败，能继续切分时会自动四分裂；已到最小格网时改为直接下载
- 如果某个格网在实际下载阶段请求重试耗尽后仍失败，也会自动四分裂成更小子格网
- 如果格网已经小到最小阈值，则不再继续切分

#### 新增参数

- `--split-threshold`
  单格建筑数量超过该值时自动切分，默认 `20000`。
- `--failure-split-retries`
  单格请求连续失败多少次后改为切分，默认 `5`。
- `--min-grid-size`
  子格网允许切分到的最小尺寸，默认 `0.05`。
- `--probe-page-size`
  探测 `numberMatched` 时使用的请求页大小，默认 `1`。

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
- 覆盖表优先，其次本地行政边界（Natural Earth）匹配，再回退在线地名解析与缓存
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
  只打印计划，不下载、不改状态。
- `--sync-only` / `--summary` / `--running`
  只同步清单入库 / 只打印状态统计 / 打印当前正在执行的任务与分块进度。
- `--export-shp-only`
  为已完成的下载任务补导出交付用 SHP。
- `--move-up` / `--move-down` / `--move-top`
  调整待执行队列顺序（task_id 或子串，逗号分隔；置顶跨页生效，多个置顶按输入顺序排列），执行后退出，不下载。
- `--tile-workers`
  单任务内并发抓取的格网数，默认 1；受源站限流（约 60 请求/分钟）约束，推荐 2，调到 3 以上不会更快。
  客户端内置全局节流（默认 1.05 秒/请求，可用 `GBA_WFS_MIN_INTERVAL` 调整），遇到 429 自动等待重试。
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

完整目录规范、SQLite 结构与状态机说明见 `docs/world_building_tasks_pipeline.md`。

### 5. `scripts/status_server.py`

#### 用途

网页看板：浏览器查看当前下载任务、分块进度、队列分页（全部待执行任务，支持翻页、每页条数、按城市中英文搜索、手动排序）、最近完成情况；失败任务表提供「操作」列，可把失败任务「加入队列」（队尾）或「置顶」重新排入待执行队列（下次启动批处理生效）；并提供「批处理控制」，可修改 `tile-workers` 并发参数一键重启批处理、或一键停止当前下载任务（已完成分块复用，断点继续）。「数据处理」页签展示 SHP 处理队列（切分 → QGIS 校验 → 清洗 → 3857 重投影）的阶段、分片进度与日志尾。

#### 启动与访问

```powershell
python -u scripts/status_server.py --port 8765
```

- 本机访问 `http://127.0.0.1:8765/`，局域网访问 `http://<本机IP>:8765/`
- 后台常驻：`Start-Process python -ArgumentList "-u","scripts/status_server.py","--port","8765" -WorkingDirectory "E:\LoD1" -WindowStyle Hidden`
- 局域网首次访问需放行防火墙：`New-NetFirewallRule -DisplayName "GBA Status" -Direction Inbound -LocalPort 8765 -Protocol TCP -Action Allow`
- 局域网防误操作（可选）：加 `--action-token <令牌>`，重启操作需在页面输入该令牌
- 「最近完成」表格含「原始数据」列：显示历史已下载数据的路径（用 `scripts/sync_existing_data.py` 扫描同步，或任务下载完成后自动写入）

完整说明见 `docs/status_page.md`。

### 6. `scripts/run_shp_process_tasks.py`

#### 用途

建筑 SHP 处理队列：从「下载已完成」的数据中发现交付 SHP，串行执行切分 → QGIS 有效性校验 → 清洗 → EPSG:3857 重投影（调用 `Tools/Oneshp_pipline_qgis`），任务状态保存到独立库 `data/shp_process_state.db`。

#### 主要能力

- `--sync` 入队：下载状态库中 `existing_data_dir`/已完成任务 + 扫描 `data` 下 `*_buildings_height_gba*.shp` + `--register` 显式登记；只有 gpkg 的记为 `BLOCKED_NO_SHP`
- 命名规则：处理阶段以 `<process_name_prefix>` 开头命名（prefix 取下载清单 CSV 第 5 列，只读；缺失时从输入文件名推导）。
  输入副本 `<prefix>.shp` 或 `<prefix>_partNN.shp`，输出目录 `<prefix>_pipeline`，最终 3857 分片 `<prefix>_xxx_xxx_3857.shp`
- `--run` 执行：资源护栏（可用内存/磁盘不足时不启动）、隔离输入目录、调用处理工具、每 5 秒写阶段与分片进度、产物校验（CRS=3857 + 分片要素数）、写 `_PROCESS_DONE.json`、回写下载库 `processed_3857_dir`
- 手动排序：待处理队列支持 `queue_order` 顺序，可用 `--move-up / --move-down / --move-top` 或 `--order` 调整；看板「数据处理」页签的「排序」列提供 ↑/↓/⤒ 按钮，改动在下次 `--run` 生效
- 页面控制：看板「数据处理」页签支持「开始执行 / 停止处理」；开始可填数量（0=全部）与是否等待资源，停止会把 RUNNING 任务重置为 PENDING；执行器日志 `data/process_run.log`
- 队列备注：待处理队列每行可编辑备注（≤200 字符，失焦/回车自动保存，存 `process_tasks.note`），重跑与 sync 不会清空
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

完整说明见 `docs/status_page.md` 的「数据处理页签」一节。

## 推荐使用方式

1. 先用 `download_gba_lod1_wfs.py --bbox` 做小范围验证。
2. 常规区域下载优先使用 `download_gba_lod1_wfs.py`。
3. 如果遇到超大格网或连续失败，切换到 `download_gba_lod1_wfs_adaptive.py`。
4. 最后再使用 `run_gba_province_batch.py` 执行多省批处理。

## 相关文档

- `docs/gba_building_shp_height_process.md`
- `docs/gba_building_shp_height_usage.md`
- `docs/province_gba_batch_solution.md`
- `docs/world_building_tasks_pipeline.md`
- `docs/status_page.md`
- `docs/gba_wfs_download_limits.md`
