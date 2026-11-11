# diskmap

扫描目录树，输出一份可折叠的 HTML 报告。只读，不删除、不修改任何文件。

## 用法

| 场景 | 操作 |
|---|---|
| 双击扫 C 盘 | 双击 `run.bat`，提示里直接回车 |
| 双击扫别的盘 | 双击 `run.bat`，粘贴 `D:\` 回车 |
| 命令行指定 | `run.bat D:\` |
| 只扫某个目录 | `python diskmap.py D:\Projects` |
| 快速模式（跳过 inode，快 3 倍） | `python diskmap.py D:\ --fast` |
| 和上次对比 | `python diskmap.py D:\ --baseline reports\D-20261111-1405\tree.json` |
| 调整 Top 榜条数 | `python diskmap.py D:\ --top 50` |

不带参数直接跑 `python diskmap.py` 就扫当前目录。

## 报告在哪

`reports\<根名>-<年月日-时分秒>\`，每次一个目录，不覆盖。

| 文件 | 内容 |
|---|---|
| `index.html` | 主报告，双击打开，暗色主题，可折叠 |
| `summary.txt` | 纯文本汇总：耗时、规模、Top 榜、类别、基线对比 |
| `tree.csv` | 全量明细，Excel 打开（UTF-8 BOM，不乱码） |
| `tree.json` | 基线文件，下次用 `--baseline` 比它 |
| `denied.txt` | 没权限读到的目录清单 |

## 指标说明

- **物理占用**：按文件索引去重后的实际占用，等于资源管理器看到的数字。
- **逻辑占用**：不去重。两者差额就是硬链接重复量（WinSxS 里最多，通常几 GB）。
- **跳过链接**：junction / 符号链接，只记录不进入，避免绕回父目录。
- **无权限**：需要管理员权限才能读的目录，`denied.txt` 里有完整清单。

## 命令行参数

```
root             扫描根目录，默认当前目录
--out DIR        报告输出目录，默认 <脚本目录>/reports
--fast           不取 inode，不跳过硬链接去重（快约 3 倍）
--depth N        CSV 里保留的最大层级，默认全部
--baseline PATH  上一次的 tree.json，输出增减对比
--top N          Top 榜条数，默认 20
```

## 分类规则

`rules.json`，按"正斜杠规范化后的路径"做正则匹配，子目录默认继承父目录的类别：

```json
{"name": "pkg-cache", "color": "#e0a458", "pattern": "(?i)(/\\.nuget$|/\\.m2$|/\\.gradle$)"}
```

内置类别：`updater-cache` / `pkg-cache` / `temp` / `browser` / `system` / `data`（兜底）。
自定义类别会用它自己的 `color` 生成徽章。
