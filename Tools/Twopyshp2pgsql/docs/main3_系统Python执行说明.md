# main3.py 系统 Python 执行说明

本文档说明如何在不依赖虚拟环境的前提下，单独执行 `main3.py` 完成数据更新。

## 1. 适用前提

执行前需要满足以下条件：

- 已安装 Python 3.12 或更高版本
- 当前使用的系统 Python 已安装项目运行依赖
- 当前机器可以访问 PostgreSQL 数据库
- 当前机器可以访问 `DATA_ROOT` 指向的共享目录

## 2. 确认当前 Python

先确认命令行里的 `python` 指向哪个解释器：

```powershell
python -c "import sys; print(sys.executable)"
python --version
```

如果版本低于 3.12，不要继续执行，先切换到正确的 Python。

## 3. 安装依赖

进入项目目录：

```powershell
cd "g:\Vscode_2024\平台部\Product\图观场景自动生成\工程代码\Tools\pyshp2pgsql"
```

推荐直接按项目依赖安装：

```powershell
python -m pip install -U pip
python -m pip install -e .
```

如果你只想补齐运行 `main3.py` 所需依赖，也可以手动安装：

```powershell
python -m pip install psycopg2-binary geopandas fiona shapely pandas sqlalchemy tqdm pyproj python-dotenv rarfile
```

## 4. 验证依赖是否安装成功

```powershell
python -c "import psycopg2, geopandas; print('dependencies ok')"
```

如果这里报错，说明当前系统 Python 仍然缺依赖，不能直接执行 `main3.py`。

## 5. 检查 main3.py 配置

执行前确认 [main3.py](/g:/Vscode_2024/平台部/Product/图观场景自动生成/工程代码/Tools/pyshp2pgsql/main3.py) 中以下配置正确：

- `DB_CFG`
- `DATA_ROOT`
- `TARGET_CITY`
- `TARGET_CONTINENT`
- `TARGET_COUNTRY`

脚本会先删除目标城市旧数据，再重新导入新的 `shp` 数据，因此这些配置必须确认无误。

## 6. 执行脚本

```powershell
python main3.py
```

## 7. 执行结果判断

执行成功时，控制台会输出类似结果：

```text
更新完成: city=北京市, 删除=21, 新增=24, 跳过=0
```

同时日志会写入：

`log_main3_beijing.log`

## 8. 常见问题

### 8.1 缺少 psycopg2

说明当前执行的系统 Python 没装 `psycopg2-binary`：

```powershell
python -m pip install psycopg2-binary
```

### 8.2 缺少 geopandas / fiona / shapely

说明 GIS 相关依赖未装完整，重新执行：

```powershell
python -m pip install -e .
```

### 8.3 `python main3.py` 和 `python -m pip install` 不是同一个环境

用下面两条命令确认是否同一个解释器：

```powershell
python -c "import sys; print(sys.executable)"
python -m pip --version
```

如果两者对应的 Python 路径不一致，说明你装依赖的环境和运行脚本的环境不是同一个，需要先统一解释器。

## 9. 结论

`main3.py` 不强制依赖虚拟环境。

只要系统 Python 满足以下条件，就可以直接运行：

- Python 版本正确
- 依赖安装完整
- 数据库和共享目录可访问
- 脚本配置正确

如果这些条件做不到，再考虑使用虚拟环境。
