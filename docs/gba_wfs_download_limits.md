# GBA WFS 建筑下载限制记录

> 一句话总结：GBA WFS 只能按"小格网、慢节奏"抓取——必须携带 viewerSession 令牌（每会话 200 次请求、900 秒过期），单次 bbox 必须小于 0.1°，全局限速约 60 次/分钟（超出返回 429），且禁用分页/排序/字段裁剪参数，只允许 GET 与 GeoJSON。

- 服务地址：`https://tubvsig-so2sat-vm1.srv.mwn.de/geoserver/ows`
- 图层：`global3D:lod1_global`
- 验证日期：2026-09-16（实际请求逐项复核）

## 一、技术限制清单

| # | 限制项 | 规则 | 违反时的返回 |
|---|---|---|---|
| 1 | 会话令牌 | 必须先 `GET /viewer-session` 获取 `viewerSession`，所有 GetFeature 请求都要带该参数 | 403 `MISSING_VIEWER_SESSION` |
| 2 | 会话配额 | 每个令牌有效期 900 秒、最多 200 次 WFS 请求，超限需换新令牌 | 令牌失效/拒绝 |
| 3 | 请求头 | 必须带浏览器风格请求头（至少 `Referer`、`Origin`、`Accept`） | nginx 403 纯 HTML（WAF 拦截） |
| 4 | bbox 上限 | 单次请求 bbox 的经/纬跨度必须 **小于 0.1°**（约 11km）；0.099° 可用、0.1° 拒绝；按边长卡，长条 bbox 也不行 | 400 `BBOX_TOO_LARGE` |
| 5 | 限速 | 全局限速约 **60 次/分钟**（约 1 次/秒），超出即被限流 | 429 `Too Many Requests` |
| 6 | 禁用分页参数 | `count`、`startIndex`、`sortBy` 全部禁用 | 403 `PARAMETER_NOT_ALLOWED` |
| 7 | 禁用取数辅助参数 | `resultType=hits`、`maxFeatures`、`propertyName` 全部禁用（不能只取计数、不能裁字段） | 403 `PARAMETER_NOT_ALLOWED` |
| 8 | 输出格式 | 仅允许 `outputFormat=application/json`（GeoJSON） | 403 `OUTPUT_FORMAT_NOT_ALLOWED` |
| 9 | 请求方法 | 仅支持 GET，POST GetFeature 不可用 | 502 Bad Gateway |
| 10 | 可用图层 | 工作区仅 `global3D:lod1_global`（另有 `global3D:lod1_oceania`） | 图层不存在错误 |

## 二、许可限制（非技术）

- 数据集：**CC BY-NC 4.0**（禁止商用，需署名）
- 建筑轮廓部分（OSM / Microsoft）：**ODbL**（署名 + 相同方式共享）
- 接口定位为网页交互浏览，不建议高频、大规模并发抓取

## 三、对下载方案的影响

1. 无法分页，也无法一次拉取大范围 → 必须把目标区域拆成 ≤0.09° 的小请求逐块抓取
2. 每个 0.5° 标准格网需拆成 **64 个子请求**，任务耗时主要取决于源站响应时间（密集区单请求 10~50 秒）
3. 源站限速 ~60 次/分钟是吞吐天花板 → 并发超过 2 个已无收益
4. 空区域（如沙漠）单请求约 1~3 秒，密集城市（如阿尔及尔）单请求 10~50 秒

## 四、本项目的适配措施

| 措施 | 说明 |
|---|---|
| 单请求跨度上限 0.09° | `gba_wfs_client.MAX_REQUEST_SPAN = 0.09`，超限自动四分裂 |
| viewerSession 自动管理 | 每 ≤180 次请求或 900 秒前主动换新令牌；多线程各自独立令牌 |
| 全局节流 | 所有线程共享节奏器，默认 1.05 秒/请求（≈57 次/分钟）；可用环境变量 `GBA_WFS_MIN_INTERVAL` 调整 |
| 429 专用处理 | 按 `Retry-After` 或递增退避等待后重试同一请求，**绝不切分格网**（避免请求数翻倍） |
| 并发配置 | 推荐 `--tile-workers 2`（实测约 54 次/分钟，约单线程 2.2 倍）；3 以上无提升 |
| 断点续跑 | 分块落盘复用，失败重试只补缺失格网 |

## 五、限制来源与变更记录

- 早期接口支持 `count/startIndex/sortBy/propertyName` 分页批量抓取，后改为 viewer 会话 + 限流模式
- 2026-09-16 复核：0.1° 从"可用"收紧为"必须小于 0.1°"；限速 429 明确出现
- 如后续接口再变更，需重新复核本文件并同步 `scripts/gba_wfs_client.py` 中的参数
