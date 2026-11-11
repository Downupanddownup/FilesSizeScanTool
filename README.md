# diskmap

扫描目录树，输出一份可折叠的 HTML 报告。只读，不删除、不修改任何文件。

## 用法

| 场景 | 操作 |
|---|---|
| 双击扫 C 盘 | 双击 `run.bat`，提示里直接回车 |
| 双击扫别的盘 | 双击 `run.bat`，粘贴 `D:\` 回车 |
| 命令行指定 | `run.bat D:\` |
| 只扫某个目录 | `python diskmap.py D:\Projects` |
| 要精确的硬链接去重（慢约 5 倍） | `python diskmap.py D:\ --exact` |
| 和上次对比 | `python diskmap.py D:\ --baseline reports\D-20261111-1405\tree.json` |
| 比两份已有报告（不重扫） | `python diskmap.py --compare reports\A\tree.json reports\B\tree.json` |
| 调整 Top 榜条数 | `python diskmap.py D:\ --top 50` |

不带参数直接跑 `python diskmap.py` 就扫当前目录。

**默认是快模式**：不逐文件取 inode，总量会比实际偏高（C 盘那种有 WinSxS 的系统盘约 7%，数据盘约 1%）。
需要精确数字时加 `--exact`，代价是慢约 5 倍——实测每个文件多花约 45 µs。

## 报告在哪

`reports\<根名>-<年月日-时分秒>\`，每次一个目录，不覆盖。

| 文件 | 内容 |
|---|---|
| `index.html` | 主报告，双击打开，暗色主题，可折叠 |
| `summary.txt` | 纯文本汇总：耗时、规模、Top 榜、类别、可重建产物、基线对比 |
| `tree.csv` | 全量明细，Excel 打开（UTF-8 BOM，不乱码） |
| `tree.json` | 基线文件，下次用 `--baseline` 或 `--compare` 比它 |
| `denied.txt` | 没权限读到的目录清单 |

## 三处口径

- **目录树**：根目录那一层**全部铺开**（最多 200 行），更深的层才按"≥ 父目录 1% 或 ≥ 10 MB"裁剪，其余折成一行。
- **可重建产物**：`node_modules` / `.git` / `__pycache__` / `build`·`dist`·`target`·`obj` / 包管理器缓存 / Python 虚拟环境，命中即整块计入。它们**不是垃圾**，删了要重装或重建。
- **比较**：`--baseline` 会重扫；`--compare` 直接读两份 `tree.json`，不重扫。两份报告模式不同（精确 / 快）时，`--compare` 会警告口径差异。

## 指标说明

- **总占用**：默认口径，不去重。含硬链接重复，通常偏高 1–7%。
- **物理 / 逻辑占用**：只有 `--exact` 模式才分别给出。物理是按文件索引去重后的实际占用，等于资源管理器看到的数字；两者差额就是硬链接重复量（WinSxS 里最多）。
- **条目/秒**：`(目录数 + 文件条目) ÷ 枚举耗时`。冷启动一般 2,500–4,000，热缓存会更高。
- **跳过链接**：junction / 符号链接，只记录不进入，避免绕回父目录。
- **无权限**：需要管理员权限才能读的目录，`denied.txt` 里有完整清单。

## 命令行参数

```
root             扫描根目录，默认当前目录
--out DIR        报告输出目录，默认 <脚本目录>/reports
--exact          精确模式：逐文件取 inode 做硬链接去重（慢约 5 倍）
--depth N        CSV 里保留的最大层级，默认全部
--baseline PATH  上一次的 tree.json，输出增减对比（会重扫）
--compare A B    比较两份已有的 tree.json，不重扫
--top N          Top 榜条数，默认 20
```

## 分类规则

`rules.json`，按"正斜杠规范化后的路径"做正则匹配，子目录默认继承父目录的类别：

```json
{"name": "pkg-cache", "color": "#e0a458", "pattern": "(?i)(/\\.nuget$|/\\.m2$|/\\.gradle$)"}
```

内置类别：`updater-cache` / `pkg-cache` / `temp` / `browser` / `system` / `data`（兜底）。
自定义类别会用它自己的 `color` 生成徽章。
