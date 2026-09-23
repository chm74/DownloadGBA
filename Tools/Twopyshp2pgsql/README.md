# twopyshp2pgsql

按区域把 shp 文件的包围盒写入 PostgreSQL 表 `world_building`。

目录结构约定：`root/洲/国家/区域/*.shp`

## 为什么不能直接拷贝 .venv

本目录的 `.venv` 是用 uv 创建的，`.venv/pyvenv.cfg` 里写死了创建时使用的 Python 路径，例如：

```
home = C:\Users\Administrator\AppData\Roaming\uv\python\cpython-3.12.12-windows-x86_64-none
```

`.venv\Scripts\python.exe` 只是个重定向器，启动时按这个路径去找真正的 Python。把整个目录拷到别的机器后，对方机器没有这个目录，就会报：

```
No Python at 'C:\Users\Administrator\AppData\Roaming\uv\python\cpython-3.12.12-windows-x86_64-none\python.exe'
```

**结论：`.venv` 不可移植，分发时不要打包它，在目标机器上重建即可（见下文）。**

截图中的报错就是这个原因，并不代表脚本文件损坏。当前旧包
`Twopyshp2pgsql.zip` 含有原机器的 `.venv`，不要再用于分发。

## 目标机部署（三步）

1. 安装 Python 3.12 或 uv（二选一，联网）：

   ```
   winget install Python.Python.3.12
   winget install astral-sh.uv
   ```

2. 解压后在本目录执行环境安装脚本：

   ```
   powershell -ExecutionPolicy Bypass -File .\setup.ps1
   ```

   脚本会自动判断：装了 uv 就执行 `uv sync`；只有 Python 就执行
   `python -m venv .venv` + `pip install -r requirements.txt`。如果目录中误带了
   其他机器的失效 `.venv`，脚本会自动清理后重建。

3. 运行：

   ```
   .\run.ps1 -Region 广西壮族自治区 -Root D:\YZ\1.0.116\BuildingData\AutoGenerate
   ```

   多个区域：`.\run.ps1 -Region 北京市 上海市 -Root D:\YZ\1.0.116\BuildingData\AutoGenerate`

如果提示禁止运行脚本，一律用 `powershell -ExecutionPolicy Bypass -File .\脚本名.ps1` 方式执行。

## 配置

数据库连接和数据根目录支持通过 `.env` 配置（模板见 `.env.example`）：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| PG_DBNAME | 数据库名 | building |
| PG_USER | 用户名 | postgres |
| PG_PASSWORD | 密码 | frontfree |
| PG_HOST | 主机 | 127.0.0.1 |
| PG_PORT | 端口 | 5432 |
| DATA_ROOT | shp 数据根目录 | `\\192.168.2.121\BuildingData\AutoGenerate` |

首次部署：

```
copy .env.example .env
```

然后按目标机器实际情况修改 `.env`。命令行参数 `--root` 优先级高于 `DATA_ROOT`。

## 打包分发

```
.\pack.ps1
```

产物为 `dist\twopyshp2pgsql-portable.zip`，已自动排除 `.venv`、`__pycache__`、`logs`、
`.waylog`、`dist`、旧压缩包和 `.env`，只带源码、`pyproject.toml`、`uv.lock`、
`requirements.txt` 和各脚本，通常只有几 MB。目标机器解压后按“目标机部署”三步执行。

这里的 `portable` 表示程序目录可以复制，不表示完全不需要运行环境。若目标机器明确禁止
安装 Python/uv，则应另做“独立可执行版”（例如 PyInstaller/Nuitka），并在相同 Windows
架构上完整验证 Fiona、GDAL、PROJ 等地理库；这种产物更大，升级和排错成本也更高，不建议
作为日常内部分发的首选。

## 手动运行（等价命令）

不想用脚本时，在本目录执行：

```
.\.venv\Scripts\python.exe main3_region.py --continent 亚洲 --country 中国 --region 广西壮族自治区 --root D:\YZ\1.0.116\BuildingData\AutoGenerate --log-dir .\logs
```

## 常见问题

- 报 `No Python at ...`：说明拷了别的机器的 `.venv`，直接运行 `setup.ps1`，脚本会清理并重建。
- 没网：在有网机器上执行 `uv sync` 或 `pip install -r requirements.txt` 后，把 pip 缓存
  （`%LOCALAPPDATA%\pip\Cache`）或 uv 缓存（`%LOCALAPPDATA%\uv\cache`）一并拷到目标机器，
  再执行 `setup.ps1`。
- Python 版本不符：`pyproject.toml` 要求 3.12 及以上，推荐 3.12。
