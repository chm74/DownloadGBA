# 任务清单驱动的建筑批量下载系统

## 1. 现状能力

- 数据源：`GlobalBuildingAtlas LoD1 WFS`，服务地址 `https://tubvsig-so2sat-vm1.srv.mwn.de/geoserver/ows`，图层 `global3D:lod1_global`，输出统一为 `EPSG:4326`。
- 下载脚本：
  - `scripts/download_gba_lod1_wfs.py`：按 `--bbox` / `--place` / `--boundary` 抓取，分格网、分页、分块复用、合并去重。
  - `scripts/download_gba_lod1_wfs_adaptive.py`：在标准脚本上增加超大格网四分裂、探测失败切分、下载失败切分能力。
- 任务来源：`scripts/export_world_building_tasks.py` 读取 PostgreSQL `world_building`，按 `continent,country,city` 分组去重，导出 `data/world_building_download_tasks.csv`（5 列：continent、country、city、download_dir、process_name_prefix）。第五列取 `shp_name` 在第一个下划线前的部分。
- 既有批处理：`scripts/run_gba_province_batch.py` 面向内置省份清单，与本任务清单互不影响。
- 本次补齐的能力：清单消费、SQLite 状态持久化、失败重试与断点续跑、范围解析与覆盖、下载完成标记。

## 2. 推荐架构

```
data/world_building_download_tasks.csv     （任务清单，唯一来源）
        │
        ▼
scripts/run_world_building_tasks.py        （执行器：选择/调度/重试/日志）
        │   ├── scripts/world_tasks_lib.py  （解析、归一化、范围解析、状态库、标记、命令拼装）
        │   ├── data/world_building_download_tasks_state.db （SQLite 任务状态）
        │   └── data/world_place_overrides.csv （特殊范围覆盖表）
        ▼
scripts/download_gba_lod1_wfs_adaptive.py  （下载引擎，复用标准格网下载实现）
        │   ├── scripts/gba_grid.py         （全国统一标准格网）
        │   └── scripts/gba_contract.py     （格网与数据语义版本）
        ▼
data/<continent>/<country>/<city>/         （标准下载目录 + _TASK_DONE.json）
```

组件职责：

| 组件 | 职责 |
|---|---|
| 任务清单 CSV | 定义要下载的分组任务与目录，人工可编辑，是唯一任务来源 |
| run_world_building_tasks.py | 读取清单、过滤任务、解析范围、调用下载脚本、写状态与标记 |
| world_tasks_lib.py | 纯函数与存储层：清单解析、地名归一化、覆盖表、SQLite、标记校验、命令拼装 |
| SQLite 状态库 | 任务级状态机与事件审计，支撑断点续跑、只跑失败项、统计 |
| 覆盖表 CSV | 对无法自动解析或需要固定范围的任务给出 bbox / 本地边界 / 查询 / 跳过 |
| 下载引擎 | 行政边界只选择全国标准格网；抓取完整格网、写完成标记并合并去重 |

## 3. 具体整改内容（已实施）

| 文件 | 状态 | 说明 |
|---|---|---|
| `scripts/world_tasks_lib.py` | 新增 | 清单解析（UTF-8/GBK 自动识别）、别名归一化、查询链、覆盖表、SQLite 状态库、完成标记、下载命令拼装 |
| `scripts/run_world_building_tasks.py` | 新增 | 清单执行器 CLI，含过滤、重试、校验、日志、退出码 |
| `data/world_place_overrides.csv` | 新增 | 港澳台与印尼三段的近似范围覆盖，可随时替换为本地边界 |
| `data/world_building_download_tasks_state.db` | 运行生成 | SQLite 状态库 |
| `tests/test_world_tasks_lib.py` | 新增 | 清单/别名/覆盖/标记/状态库单测 |
| `tests/test_run_world_building_tasks.py` | 新增 | 执行器成功、失败、dry-run 单测 |
| `docs/world_building_tasks_pipeline.md` | 新增 | 本文档 |
| `scripts/gba_grid.py` | 新增 | 以 `(-180,-90)` 为固定原点生成全国统一标准格网与稳定编号 |
| `scripts/gba_contract.py` | 新增 | 统一维护 `national-grid-v1`、`coverage-v2` 和目录命名 |
| `scripts/download_gba_lod1_wfs.py` | 修改 | 完整格网下载、取消建筑行政边界过滤、瓦片校验与安全续传 |
| 下载任务和状态页面 | 修改 | 按 `grid_manifest.json` 与 `done.json` 统计进度并阻止缺格网任务成功 |

## 4. 下载目录规范

每个任务一个目录，路径以清单第 4 列 `download_dir` 为准：

```
data/<continent>/<country>/<city>/
    place_boundary.gpkg                 # 范围边界缓存（本地行政边界或覆盖表范围）
    gba_wfs_grid.gpkg                   # 全国统一标准格网清单（空间文件）
    grid_manifest.json                  # 格网版本、边界摘要和预期格网 ID
    tiles_national_v1/                  # coverage-v2 原始格网瓦片
        GBA_050_I0598_J0232.gpkg        # 完整 0.5° 标准格网
        GBA_050_I0598_J0232.done.json   # 校验完成后原子写入的完成标记
        .partial/                       # 下载中间文件，停止后不会被复用
    <city>_buildings_height_gba.gpkg    # 合并成果（用于校验与续跑）
    <city>_buildings_height_gba.shp     # 交付用 SHP（超出分卷阈值时自动拆分）
    <city>_buildings_height_gba_partN.shp  # 超大任务的 SHP 分卷
    run.log / run.err.log               # 下载日志
    _TASK_DONE.json                     # 完成标记（成功后才写，含 shp_outputs 清单）
```

规则：

- `download_dir` 必须是仓库根内的相对路径；`\` 自动转换为 `/`；禁止 `..`。
- 标准格网固定以 `(-180,-90)` 为原点；行政边界只决定选择哪些格网，不缩小请求范围、不删除边界外建筑。
- 瓦片只有在数据文件和 `done.json` 的数量、CRS、大小、SHA256 全部一致时才可复用；空格网也写 `EMPTY` 标记。
- 旧的顶层 `GBA_0001.gpkg` 不会混入 `national-grid-v1` 合并；迁移按省重置任务，不会自动重跑全国。
- 若清单中 `download_dir` 为空，执行器按 `data/<continent>/<country>/<city>` 生成。
- 合并文件名由 city 生成 slug（保留中文与括号，替换空格与非法字符）。
- 每个任务默认输出交付用 SHP（`--shp-export`，默认开启）；单任务要素超过 `--shp-max-features`（默认 1000000）时自动拆分为 `_part1.shp`、`_part2.shp` 等分卷，规避 SHP/DBF 单文件 2GB 上限。
- 合并与导出阶段有进度日志：合并每读取 25 个分块输出一次 `[MERGE] ...`，SHP 导出每写完一卷输出一次 `[SHP] ...`（由执行器追加到任务 `run.log`），避免长时间无日志被误判为"卡住"。
- 合并 GPKG 始终保留，作为校验与断点续跑依据；`_TASK_DONE.json` 中的 `shp_outputs` 记录 SHP 分卷清单。
- 需要为已下载完成任务补导出 SHP 时执行：`python scripts/run_world_building_tasks.py --export-shp-only`。

## 5. SQLite 状态库

默认路径：`data/world_building_download_tasks_state.db`（WAL 模式，单实例写入）。

`tasks` 表字段：

| 字段 | 说明 |
|---|---|
| task_id | `continent\|country\|city` 主键 |
| continent / country / city / download_dir | 清单快照 |
| manifest_order | 清单中的顺序，保证执行顺序稳定 |
| status | 状态机当前值 |
| attempts | 尝试次数 |
| exit_code | 下载脚本退出码 |
| feature_count | 合并产物要素数（由 pyogrio 快速计数） |
| merge_output | 合并产物仓库相对路径 |
| existing_data_dir | 原始数据目录相对路径：任务下载成功后由执行器写入；历史数据由 `sync_existing_data.py` 扫描写入 |
| boundary_source | 范围来源 JSON（geocode / override_bbox / cache / ...） |
| last_error | 最近一次失败原因 |
| started_at / finished_at / duration_seconds | 时间信息 |
| updated_at | 状态更新时间 |

`task_events` 表记录 `IMPORT / START / DONE / FAIL / SKIP / RESET / VERIFY_FAIL` 事件，用于审计。

状态机：

```
PENDING ──► RUNNING ──► OK / OK_EMPTY
                │
                └─────► FAILED ──(重试或 --retry-failed)──► PENDING/RUNNING
SKIPPED 由覆盖表 skip 规则产生
```

- 进程中断遗留的 `RUNNING`，下次运行自动重跑。
- `OK` 任务默认跳过；执行器会先校验 `_TASK_DONE.json` 与产物一致性，不一致时自动置回 `PENDING`。
- 清单中 `download_dir` 变更时，已完成状态自动重置为 `PENDING`。

## 6. 范围解析与覆盖表

解析顺序：

1. 覆盖表命中：
   - `bbox`：直接生成矩形边界（港澳台、印尼三段当前用此方式）。
   - `boundary`：直接使用本地 shp/gpkg（有正式边界时推荐）。
   - `query`：使用指定地名或结构化查询（value 支持 JSON）。
   - `skip`：任务标记为 SKIPPED。
2. 缓存：`<download_dir>/place_boundary.gpkg` 已存在且未加 `--refresh-boundary` 时直接复用。
3. 自动解析链（Nominatim 结构化查询优先）：
   - `city == country`（如 Algeria/Algeria）→ `{"country": ...}`
   - 否则 `{"state": city, "country": ...}` → `{"city": city, "country": ...}` → `"city, country"` → `"city"`
4. 全部失败 → 任务 FAILED，原因写入 `last_error`，不阻塞其他任务。

国家名归一化：下划线转空格并内置别名表，例如 `United_States_of_America → United States`、`Congo(DRC) → Democratic Republic of the Congo`、`Turkmenistann → Turkmenistan`、`中国 → China`。

## 7. 完成标记

任务成功且校验通过后，在任务目录写入 `_TASK_DONE.json`（临时文件 + 重命名原子写入）：

```json
{
  "task_id": "Africa|Algeria|Algeria",
  "status": "OK",
  "feature_count": 123456,
  "download_dir": "data/Africa/Algeria/Algeria",
  "merge_output": "data/Africa/Algeria/Algeria/Algeria_buildings_height_gba.gpkg",
  "merge_size_bytes": 123456789,
  "data_semantics_version": "coverage-v2",
  "grid_algorithm": "national-grid-v1",
  "expected_tile_count": 65,
  "complete_tile_count": 62,
  "empty_tile_count": 3,
  "missing_tile_count": 0,
  "boundary_source": {"type": "geocode", "query": {"country": "Algeria"}},
  "params": {"engine": "adaptive", "grid_size": 0.5},
  "started_at": "...",
  "finished_at": "..."
}
```

- 无建筑的任务写 `OK_EMPTY`、`feature_count=0`，避免反复重跑。
- `--hash` 时额外记录 `merge_sha256`（大文件耗时，默认不开启）。
- 标记与状态库互为兜底：状态库用于调度，标记随目录移动，用于交付自证。

## 8. 常用命令

导出/刷新清单（已有脚本）：

```powershell
python scripts/export_world_building_tasks.py --password <口令> --output data/world_building_download_tasks.csv
```

并发提速（tile 级并发）：

```powershell
python -u scripts/run_world_building_tasks.py --tile-workers 2 --sleep-seconds 2
```

已有历史数据同步（可选）：

```powershell
# 扫描已下载数据目录，把路径写入任务的 existing_data_dir（看板"最近完成"表格显示，处理同步也会读取）
python scripts/sync_existing_data.py --root data/亚洲/中国 --dry-run
python scripts/sync_existing_data.py --root data/亚洲/中国

# 如需让批处理跳过这些已有任务，可加 --mark-status skipped
python scripts/sync_existing_data.py --root data/亚洲/中国 --mark-status skipped
```

- `--tile-workers` 表示单任务内并发抓取的 0.5° 格网数，默认 1
- 每个并发线程使用独立的 viewerSession 令牌与请求会话，互不干扰
- 源站对请求频率有限制（超过约 60 次/分钟会返回 429），客户端已内置**全局节流**：默认每次请求间隔 1.05 秒（可用环境变量 `GBA_WFS_MIN_INTERVAL` 调整），并对 429 自动等待重试（绝不切分格网）
- 受源站限流约束，推荐 `--tile-workers 2`；调到 3 以上不会更快
- 并发失败时，带有效完成标记的格网继续保留；半成品留在 `.partial`，重试只补缺失、损坏或未完成格网

下载完成后自动登记处理队列（默认开启）：

- 任务成功（状态 `OK`）且导出 SHP 后，执行器会自动把该任务同步进处理队列（`data/shp_process_state.db`），看板「数据处理」页签随即出现为待处理，无需手动 `--sync`
- 关闭自动登记：`--no-process-sync`；自定义处理库路径：`--process-db <path>`
- `--export-shp-only` 补导出 SHP 后也会对补出的任务做同样的登记
- 登记失败只打印 `[WARN]`（不影响下载）；历史完成数据可用页面「同步队列」按钮或 `python scripts/run_shp_process_tasks.py --sync` 补登记

同步清单到状态库并查看统计：

```powershell
python scripts/run_world_building_tasks.py --sync-only
python scripts/run_world_building_tasks.py --summary
python scripts/run_world_building_tasks.py --running
```

`--running` 会打印当前正在执行的任务、尝试次数、开始时间、任务目录与分块进度（已完成分块 / 总格网）。实时日志可用 `Get-Content data\world_tasks_run.log -Tail 20 -Wait` 查看。

预览执行计划（不解析范围、不下载、不改状态）：

```powershell
python scripts/run_world_building_tasks.py --dry-run --max-tasks 5
```

正式分批执行（按大洲、任务间隔 2 秒）：

```powershell
python scripts/run_world_building_tasks.py --continent Africa --sleep-seconds 2
```

断点续跑与失败重试：

```powershell
python scripts/run_world_building_tasks.py --retry-failed
python scripts/run_world_building_tasks.py --status failed --task-attempts 2
```

指定任务与强制重跑：

```powershell
python scripts/run_world_building_tasks.py --only "Africa|Algeria|Algeria"
python scripts/run_world_building_tasks.py --force --only "America|United_States_of_America|Texas"
python scripts/run_world_building_tasks.py --reset-task "Asia|China|广东省"
```

重新解析范围：

```powershell
python scripts/run_world_building_tasks.py --only "Africa|Algeria|Algeria" --refresh-boundary
```

## 9. 实施顺序

1. 支撑库 `world_tasks_lib.py`：清单解析、别名、覆盖表、SQLite、标记、命令拼装。
2. 执行器 `run_world_building_tasks.py`：选择/过滤、执行循环、日志、退出码。
3. 覆盖表 `world_place_overrides.csv`：港澳台与印尼三段近似范围。
4. 单测：解析、查询链、状态机、标记、执行成功/失败、dry-run（全部不触网）。
5. 文档与 README。
6. 冒烟：`--sync-only`、`--summary`、`--dry-run`，确认 258 个任务入库与计划正确。
7. 正式执行建议：先小国试点 → 按大洲分批 → 用 `--retry-failed` 收尾。

## 10. 风险与注意

- Nominatim 有访问频率限制，正式跑建议 `--sleep-seconds 2` 以上，且边界缓存可显著减少请求。
- 整国大任务（Russia、Brazil、Canada 等）单任务耗时很长，建议按大洲分批，并预留 TB 级磁盘。
- 港澳台、印尼三段当前为近似 bbox，若后续拿到正式边界，把覆盖表对应行改为 `kind=boundary, value=<路径>` 即可。
- standard 与 adaptive 引擎共享 `national-grid-v1` 格网和完成标记；旧版顶层顺序编号瓦片仅作为历史数据保留，不参与新版合并。
- 状态库与清单解耦：清单只读，状态只写库与标记，人工编辑清单不会破坏历史状态。
