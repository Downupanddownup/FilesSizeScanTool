#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""diskmap - 一次性扫出整个目录树的体积，输出可折叠的 HTML 报告。

用法:
    python diskmap.py                     # 扫当前目录
    python diskmap.py C:\\                 # 扫整盘
    python diskmap.py D:\\ --fast
    python diskmap.py D:\\ --baseline reports\\D-20261111-1405\\tree.json

只读，不删除、不修改任何文件。
"""

import argparse
import csv
import heapq
import html as _html
import io
import json
import os
import re
import shutil
import sys
import time

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
MiB = 1024 * 1024
SYSTEM_FILE_NAMES = {"pagefile.sys", "hiberfil.sys", "swapfile.sys", "dumpstack.log.tmp"}
TOP_N = 20
TOP_FILES = 30
HTML_MIN_BYTES = 10 * MiB
HTML_MIN_RATIO = 0.01
ROOT_ROWS = 200                # 根层最多铺开的行数，超过才折叠

# 分类规则跑在"正斜杠规范化"后的路径上，省掉反斜杠转义
DEFAULT_RULES = [
    {"name": "updater-cache", "color": "#e06c75", "pattern": r"(^|/)[^/]*updater$"},
    {"name": "pkg-cache",     "color": "#e0a458",
     "pattern": r"(?i)(/\.nuget$|/\.m2$|/\.gradle$|npm-cache$|/pnpm$|pnpm-cache$|node-gyp$|/pip$|/Dependencies$)"},
    {"name": "temp",          "color": "#a78bfa",
     "pattern": r"(?i)(/Temp$|/Logs?$|/Cache$|Code Cache$|CrashDumps$)"},
    {"name": "browser",       "color": "#c084fc",
     "pattern": r"(?i)(User Data/[^/]+$|User Data/Default$)"},
    {"name": "system",        "color": "#2dd4bf",
     "pattern": r"(?i)(/Windows$|WinSxS|pagefile\.sys|hiberfil\.sys|swapfile\.sys|DriverStore$|/Recovery$)"},
]
TAG_CLASSES = {"updater-cache", "pkg-cache", "temp", "browser", "system"}


# ---------- 小工具 ----------

def human(n):
    """<1KB 用 B，<1MB 用 KB，<1GB 用 MB，其余 GB 两位小数。"""
    if n < 1024:
        return "%d B" % n
    kb = n / 1024.0
    if kb < 1024:
        return "%.1f KB" % kb
    mb = kb / 1024.0
    if mb < 1024:
        return "%.1f MB" % mb
    return "%.2f GB" % (mb / 1024.0)


def fmt_int(n):
    return "{:,}".format(n)


def norm(path):
    return path.replace("\\", "/")


def stamp(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "-"


def mmss(seconds):
    """秒数换成 mm:ss（超过一小时再加小时）。"""
    s = int(seconds)
    if s >= 3600:
        return "%d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)
    return "%d:%02d" % (s // 60, s % 60)


def shorten(text, limit):
    """从左边截断，保留末尾——路径的前缀没有信息量。"""
    if limit <= 1 or len(text) <= limit:
        return text
    return "…" + text[-(limit - 1):]


def _enable_vt():
    """开启 Windows 控制台的 VT 转义支持，失败返回 False（走降级路径）。"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-12)          # STD_ERROR_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & 0x0004:                       # 已启用
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


class Progress(object):
    """两行进度：第一行心跳，第二行当前位置。写 stderr，不碰 stdout。"""

    def __init__(self, root, enabled):
        self.root = root
        self.enabled = bool(enabled)
        self.vt = False
        self.last = 0.0
        self.started = time.time()
        self.printed = False
        self.width = 100
        if self.enabled:
            try:
                self.width = max(60, shutil.get_terminal_size((100, 24)).columns - 2)
            except Exception:
                pass
            self.vt = _enable_vt()
            if not self.vt:
                sys.stderr.write("（当前终端不支持原地刷新，进度改为逐行追加）\n")

    def update(self, ops, size, path):
        if not self.enabled:
            return
        now = time.time()
        if now - self.last < 1.0:
            return
        self.last = now
        line1 = "任务 %s  ·  已扫 %s 条目 · %s · %s" % (
            self.root, fmt_int(ops), human(size), mmss(now - self.started))
        line2 = "正在扫描 " + shorten(path, self.width - 12)
        line1 = line1[:self.width].ljust(self.width)
        line2 = line2[:self.width].ljust(self.width)
        out = sys.stderr
        if self.vt:
            if self.printed:
                out.write("\x1b[2A")               # 回到上两行
            out.write("\x1b[2K" + line1 + "\n")
            out.write("\x1b[2K" + line2 + "\n")
            self.printed = True
        else:
            out.write(line1.rstrip() + "\n")
        out.flush()

    def finish(self):
        if not self.enabled:
            return
        sys.stderr.write("\n")
        sys.stderr.flush()


def esc(s):
    return _html.escape(s, quote=False)


def esc_attr(s):
    return _html.escape(s, quote=True)


def hex_rgba(value, alpha):
    """#rrggbb -> rgba(...)，给自定义分类的徽章上色用。"""
    try:
        h = value.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return "rgba(%d,%d,%d,%.2f)" % (r, g, b, alpha)
    except Exception:
        return "rgba(136,136,136,%.2f)" % alpha


def load_rules(script_dir):
    path = os.path.join(script_dir, "rules.json")
    rules = DEFAULT_RULES
    if os.path.isfile(path):
        try:
            with io.open(path, "r", encoding="utf-8-sig") as f:
                rules = json.load(f)
        except Exception as exc:
            sys.stderr.write("rules.json 读取失败，改用内置规则: %s\n" % exc)
    return [(r["name"], r.get("color", "#888888"), re.compile(r["pattern"])) for r in rules]


# ---------- 扫描 ----------

class Node(object):
    __slots__ = ("name", "depth", "size", "files", "mtime", "kids", "cat", "elapsed")

    def __init__(self, name, depth):
        self.name = name
        self.depth = depth
        self.size = 0
        self.files = 0
        self.mtime = 0.0
        self.kids = None
        self.cat = "data"
        self.elapsed = 0.0


class Scanner(object):
    def __init__(self, rules, fast=False, skip_prefixes=None, progress=None):
        self.rules = rules
        self.fast = fast
        self.skip_prefixes = [p for p in (skip_prefixes or []) if p]
        self.progress = progress
        self.ops = 0
        self.running = 0
        self.seen = set()
        self.dir_count = 0
        self.file_count = 0
        self.file_entries = 0
        self.logical = 0
        self.links = 0
        self.skipped_reports = 0
        self.system_files = 0
        self.denied = []
        self.top_files = []
        self.scan_seconds = 0.0

    def walk(self, path, depth):
        if self.skip_prefixes:
            low = norm(path).lower()
            if any(low.startswith(p) for p in self.skip_prefixes):
                self.skipped_reports += 1
                return Node(os.path.basename(path) or path, depth)

        self.dir_count += 1
        self.ops += 1
        if self.progress is not None:
            self.progress.update(self.ops, self.running, path)
        node = Node(os.path.basename(path) or path, depth)
        try:
            it = os.scandir(path)
        except OSError as exc:
            self.denied.append((path, type(exc).__name__))
            return node

        with it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    self.denied.append((entry.path, "stat-failed"))
                    continue
                if getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
                    self.links += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    self.ops += 1
                    child_start = time.time()
                    child = self.walk(entry.path, depth + 1)
                    child.elapsed = time.time() - child_start
                    node.size += child.size
                    node.files += child.files
                    if child.mtime > node.mtime:
                        node.mtime = child.mtime
                    if node.kids is None:
                        node.kids = []
                    node.kids.append(child)
                else:
                    self.ops += 1
                    self.running += st.st_size
                    self.logical += st.st_size
                    self.file_entries += 1
                    if depth == 0 and entry.name.lower() in SYSTEM_FILE_NAMES:
                        self.system_files += st.st_size
                    if not self.fast:
                        ino = getattr(st, "st_ino", 0)
                        if not ino:
                            # DirEntry 的目录项缓存里没有文件索引，必须真开一次句柄
                            try:
                                ino = os.stat(entry.path, follow_symlinks=False).st_ino
                            except OSError:
                                ino = 0
                        if ino:
                            if ino in self.seen:
                                continue
                            self.seen.add(ino)
                    node.size += st.st_size
                    node.files += 1
                    if st.st_mtime > node.mtime:
                        node.mtime = st.st_mtime
                    self.keep_top_file(entry.path, st.st_size)
        return node

    def keep_top_file(self, path, size):
        if len(self.top_files) < TOP_FILES:
            heapq.heappush(self.top_files, (size, path))
        elif size > self.top_files[0][0]:
            heapq.heapreplace(self.top_files, (size, path))

    def scan(self, root):
        started = time.time()
        node = self.walk(root, 0)
        self.scan_seconds = time.time() - started
        self.file_count = node.files
        return node

    def categorize(self, root_node, root_path):
        """自上而下贴标签，子目录默认继承父目录的类别。"""
        stack = [(root_path, root_node, "data")]
        while stack:
            path, node, inherited = stack.pop()
            cat = inherited
            if node.depth > 0:
                key = norm(path)
                for name, _color, rx in self.rules:
                    if rx.search(key):
                        cat = name
                        break
            node.cat = cat
            if node.kids:
                for kid in node.kids:
                    stack.append((os.path.join(path, kid.name), kid, cat))


def category_totals(root_node, system_files=0):
    """按类别汇总：每个节点只贡献"自己独占"的那部分，避免父子重复计数。"""
    buckets = {}

    def account(n):
        own = n.size
        for k in (n.kids or []):
            own -= k.size
            account(k)
        buckets[n.cat] = buckets.get(n.cat, 0) + own

    account(root_node)
    if system_files:               # 根目录直属的页面文件等，从 data 里挪出来
        buckets[root_node.cat] = buckets.get(root_node.cat, 0) - system_files
        buckets["system"] = buckets.get("system", 0) + system_files
    return buckets


def iter_nodes(node, path):
    stack = [(path, node)]
    while stack:
        p, cur = stack.pop()
        yield p, cur
        if cur.kids:
            for kid in cur.kids:
                stack.append((os.path.join(p, kid.name), kid))


# 可重建产物：名字命中的目录整块计入，不再往下走，避免父子重复计数
REBUILD_DIRS = {
    "node_modules": "node_modules",
    ".git": ".git 仓库",
    "__pycache__": "__pycache__",
    "build": "构建产物 build/dist/target/obj",
    "dist": "构建产物 build/dist/target/obj",
    "target": "构建产物 build/dist/target/obj",
    "obj": "构建产物 build/dist/target/obj",
    ".gradle": "包管理器缓存 .gradle/.m2/.nuget/.pnpm-store",
    ".m2": "包管理器缓存 .gradle/.m2/.nuget/.pnpm-store",
    ".nuget": "包管理器缓存 .gradle/.m2/.nuget/.pnpm-store",
    ".pnpm-store": "包管理器缓存 .gradle/.m2/.nuget/.pnpm-store",
    "pnpm-store": "包管理器缓存 .gradle/.m2/.nuget/.pnpm-store",
    ".cargo": "包管理器缓存 .gradle/.m2/.nuget/.pnpm-store",
    ".venv": "Python 虚拟环境 .venv/venv/env",
    "venv": "Python 虚拟环境 .venv/venv/env",
    "env": "Python 虚拟环境 .venv/venv/env",
}


def rebuildable_totals(root_node):
    """统计那些"删了能重建"的目录。命中即整块计入，不再深入。"""
    buckets = {}
    stack = list(root_node.kids or [])
    while stack:
        node = stack.pop()
        key = REBUILD_DIRS.get(node.name.lower())
        if key:
            buckets[key] = buckets.get(key, 0) + node.size
            continue
        if node.kids:
            stack.extend(node.kids)
    return buckets


# ---------- 输出：CSV / JSON / summary ----------

def write_csv(node, root_path, fp, max_depth):
    w = csv.writer(fp)
    w.writerow(["path", "depth", "size_bytes", "size", "files", "last_write",
                "category", "elapsed_s"])
    for path, cur in iter_nodes(node, root_path):
        if cur.depth <= max_depth:
            w.writerow([path, cur.depth, cur.size, human(cur.size), cur.files,
                        stamp(cur.mtime), cur.cat,
                        "%.1f" % cur.elapsed if cur.depth == 1 else ""])


def write_json(node, root_path, fp, mode="fast"):
    nodes = {}
    for path, cur in iter_nodes(node, root_path):
        nodes[path] = [cur.size, cur.files, cur.cat]
    json.dump({"root": root_path, "mode": mode, "nodes": nodes}, fp, ensure_ascii=False)


def write_summary(node, root_path, path_out, scanner, timings, baseline_path=None):
    lines = []
    add = lines.append
    add("扫描根    %s" % root_path)
    add("开始      %s" % timings["start"])
    add("结束      %s" % timings["end"])
    add("耗时      %.1f s（枚举 %.1f / 写出 %.1f）"
        % (timings["total"], scanner.scan_seconds, timings["write"]))
    rate = (scanner.dir_count + scanner.file_entries) / scanner.scan_seconds \
        if scanner.scan_seconds > 0 else 0
    add("条目/秒   %s        冷启动一般 2,500–4,000，热缓存更高" % fmt_int(int(rate)))
    add("")
    add("目录数    %s" % fmt_int(scanner.dir_count))
    if scanner.fast:
        add("文件数    %s" % fmt_int(scanner.file_count))
    else:
        add("文件数    %s        去重后；去重前有 %s 条目录项"
            % (fmt_int(scanner.file_count), fmt_int(scanner.file_entries)))
    add("跳过链接  %s        junction / 符号链接，未进入" % fmt_int(scanner.links))
    add("无权限    %s        详见 denied.txt" % fmt_int(len(scanner.denied)))
    if scanner.skipped_reports:
        add("排除报告  %s 个目录  报告目录自身不计入" % fmt_int(scanner.skipped_reports))
    add("")
    if scanner.fast:
        add("总占用    %s" % human(node.size))
        add("          未去重，含硬链接重复（通常偏高 1–7%；要精确请加 --exact）")
    else:
        add("物理占用  %s" % human(node.size))
        add("逻辑占用  %s       硬链接已去重" % human(scanner.logical))
    add("")

    add("按目录 Top %d" % TOP_N)
    for k in sorted(node.kids or [], key=lambda n: n.size, reverse=True)[:TOP_N]:
        pct = (k.size * 100.0 / node.size) if node.size else 0.0
        add("  %10s %6.1f%%  %9s 文件   %s"
            % (human(k.size), pct, fmt_int(k.files), os.path.join(root_path, k.name)))
    add("")

    add("按文件 Top %d" % TOP_FILES)
    for size, p in sorted(scanner.top_files, reverse=True)[:TOP_FILES]:
        add("  %10s          %s" % (human(size), p))
    add("")

    add("按类别")
    buckets = category_totals(node, scanner.system_files)
    for name, size in sorted(buckets.items(), key=lambda kv: -kv[1]):
        pct = (size * 100.0 / node.size) if node.size else 0.0
        add("  %10s %6.1f%%  %s" % (human(size), pct, name))
    add("")
    rebuild = rebuildable_totals(node)
    if rebuild:
        add("可重建产物（删了要重装/重建，不是垃圾）")
        rsum = 0
        for name, size in sorted(rebuild.items(), key=lambda kv: -kv[1]):
            pct = (size * 100.0 / node.size) if node.size else 0.0
            add("  %10s %6.1f%%  %s" % (human(size), pct, name))
            rsum += size
        pct = (rsum * 100.0 / node.size) if node.size else 0.0
        add("  " + "-" * 46)
        add("  %10s %6.1f%%  合计" % (human(rsum), pct))
        add("")

    tops = [k for k in (node.kids or []) if k.depth == 1]
    if tops:
        add("顶层目录耗时 Top 10")
        for k in sorted(tops, key=lambda n: -n.elapsed)[:10]:
            add("  %8s   %10s 文件  %10s   %s"
                % (mmss(k.elapsed), fmt_int(k.files), human(k.size),
                   os.path.join(root_path, k.name)))
        add("")

    add("注：denied 目录（%d 个）与跳过的链接未计入上述合计。" % len(scanner.denied))

    if baseline_path:
        add("")
        add("与基线对比（%s）" % os.path.basename(baseline_path))
        try:
            with io.open(baseline_path, "r", encoding="utf-8-sig") as f:
                old = json.load(f)["nodes"]
            now = {}
            for path, cur in iter_nodes(node, root_path):
                now[path] = cur.size
            deltas = []
            for path, size in now.items():
                if path == root_path:
                    continue
                prev = old.get(path, [0])[0]
                if abs(size - prev) >= MiB:
                    deltas.append((size - prev, size, path))
            deltas.sort(reverse=True)
            if not deltas:
                add("  无变化（超过 1 MB 的增减才算）")
            for d, size, path in deltas[:TOP_N]:
                add("  %+12s  现 %10s   %s" % (human(d), human(size), path))
            gone = [p for p in old if p not in now]
            if gone:
                add("  已消失 %d 项，合计 %s"
                    % (len(gone), human(sum(old[p][0] for p in gone))))
        except Exception as exc:
            add("  基线读取失败：%s" % exc)

    with io.open(path_out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------- 输出：index.html ----------

CSS = """
:root{
  --bg:#0e1116;--panel:#151a21;--line:#2a3340;
  --text:#dde6f0;--muted:#a5b2c1;--dim:#8794a4;
  --accent:#5b9bf8;--green:#4cc38a;--amber:#e0a458;--red:#e06c75;
  --purple:#a78bfa;--teal:#2dd4bf;--violet:#c084fc;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
 font:14.5px/1.7 "Segoe UI Variable","Segoe UI",system-ui,"Microsoft YaHei",sans-serif;
 -webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:28px 32px 80px}
header{border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:26px}
h1{margin:0;font-size:22px;font-weight:600;letter-spacing:.2px;color:#e8eef7}
h1 .path{color:var(--accent);font-family:Consolas,monospace}
.sub{margin-top:6px;color:var(--muted);font-size:12.5px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.chip{background:var(--panel);border:1px solid var(--line);border-radius:7px;
 padding:6px 12px;font-size:12.5px;color:var(--muted)}
.chip b{color:#fff;font-weight:600;font-family:Consolas,monospace;
 font-feature-settings:"tnum";margin-left:4px}
h2{font-size:13px;font-weight:600;letter-spacing:.6px;text-transform:uppercase;
 color:#95a3b3;margin:34px 0 10px}
table.top{width:100%;border-collapse:collapse;font-size:13.5px}
table.top td{padding:6px 10px 6px 0;border-bottom:1px solid rgba(45,55,68,.7);vertical-align:middle}
table.top tr:last-child td{border-bottom:none}
.size{font-family:Consolas,monospace;font-feature-settings:"tnum";color:#9ecbff;
 white-space:nowrap;width:96px}
.pct{color:var(--dim);font-family:Consolas,monospace;width:56px;text-align:right}
.cnt{color:var(--muted);font-family:Consolas,monospace;font-feature-settings:"tnum";
 white-space:nowrap;width:110px;text-align:right;padding-right:14px}
.barcell{width:30%;padding-right:16px}
.bar{height:5px;border-radius:3px;background:rgba(91,155,248,.18);position:relative;overflow:hidden}
.bar i{position:absolute;left:0;top:0;bottom:0;border-radius:3px;
 background:linear-gradient(90deg,#4a86e8,#6fb0ff)}
.pathcell{font-family:Consolas,monospace;color:#bcc8d6;font-size:13px;
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:0}
.tree{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 18px}
.tree>details{margin-left:0;border-left:none;padding-left:0}
details{margin-left:14px;border-left:1px solid rgba(45,55,68,.85);padding-left:12px}
summary{cursor:pointer;padding:3px 6px;list-style:none;white-space:nowrap;border-radius:5px}
summary::-webkit-details-marker{display:none}
summary:hover{background:rgba(91,155,248,.07)}
summary::before{content:"\\25B8";color:var(--dim);display:inline-block;width:15px;
 transition:transform .12s ease;transform-origin:40% 55%}
details[open]>summary::before{transform:rotate(90deg);color:var(--accent)}
.name{color:#e6edf7}
details[open]>summary>.name{color:#fff;font-weight:500}
.sz{font-family:Consolas,monospace;font-feature-settings:"tnum";color:#9ecbff;margin-left:9px}
.pc{color:#8b98a8;font-size:12.5px;margin-left:7px;font-family:Consolas,monospace}
.leaf{margin-left:14px;border-left:1px solid rgba(45,55,68,.85);
 padding:3px 6px 3px 21px;white-space:nowrap;color:#d5dee8}
.leaf:hover{background:rgba(91,155,248,.07);border-radius:5px}
.more{margin:3px 0 3px 27px;color:#8b98a8;font-size:13px;font-style:italic}
.copy{display:none;background:none;border:1px solid transparent;color:var(--dim);
 cursor:pointer;font:inherit;font-size:13px;line-height:1;padding:1px 5px;
 border-radius:4px;margin-left:8px;vertical-align:1px}
body.js .copy{display:inline-block}
summary:hover .copy,.leaf:hover .copy,.copy:focus{color:var(--accent);
 border-color:var(--line);outline:none}
.copy.ok{color:var(--green);border-color:rgba(76,195,138,.45)}
.tag{font-size:10.5px;letter-spacing:.3px;border-radius:4px;padding:1px 6px;margin-left:8px;
 background:rgba(224,108,117,.14);color:var(--red);border:1px solid rgba(224,108,117,.25)}
.tag.pkg-cache{background:rgba(224,164,88,.13);color:var(--amber);border-color:rgba(224,164,88,.25)}
.tag.temp{background:rgba(167,139,250,.13);color:var(--purple);border-color:rgba(167,139,250,.25)}
.tag.browser{background:rgba(192,132,252,.13);color:var(--violet);border-color:rgba(192,132,252,.25)}
.tag.system{background:rgba(45,212,191,.12);color:var(--teal);border-color:rgba(45,212,191,.22)}
.note{color:var(--dim);font-size:12.5px;margin:0 0 12px}
.cats{display:flex;gap:10px;flex-wrap:wrap}
.cat{background:var(--panel);border:1px solid var(--line);border-radius:8px;
 padding:8px 14px;font-size:12.5px;color:var(--muted);min-width:150px}
.cat b{display:block;color:#f2f7fd;font-family:Consolas,monospace;font-size:14px;
 font-feature-settings:"tnum";margin-bottom:2px}
footer{margin-top:40px;color:var(--dim);font-size:12px;
 border-top:1px solid var(--line);padding-top:14px}
"""

# 仅用于"复制完整路径"。没有它，报告照常可读，只是没有复制按钮。
JS = """
<script>
document.body.classList.add('js');
(function () {
  var btns = document.querySelectorAll('button.copy');
  for (var i = 0; i < btns.length; i++) {
    (function (b) {
      b.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();          /* 别让点击冒泡到 summary 触发折叠 */
        var p = b.getAttribute('data-p');
        var ok = function () {
          b.classList.add('ok');
          b.textContent = '✓';
          setTimeout(function () {
            b.classList.remove('ok');
            b.textContent = '⧉';
          }, 1500);
        };
        var fallback = function () {
          var t = document.createElement('textarea');
          t.value = p;
          t.style.position = 'fixed';
          t.style.left = '-9999px';
          document.body.appendChild(t);
          t.select();
          try { document.execCommand('copy'); ok(); } catch (err) {}
          document.body.removeChild(t);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(p).then(ok, fallback);
        } else {
          fallback();
        }
      });
    })(btns[i]);
  }
})();
</script>
"""


def tag_html(name, colors):
    if name == "data":
        return ""
    if name in TAG_CLASSES:
        return '<span class="tag %s">%s</span>' % (name, esc(name))
    color = colors.get(name, "#888888")
    return ('<span class="tag" style="background:%s;border-color:%s;color:%s">%s</span>'
            % (hex_rgba(color, 0.14), hex_rgba(color, 0.28), color, esc(name)))


def copy_btn(path):
    """行尾的复制按钮——默认不显示，JS 跑起来才出现。"""
    return ('<button class="copy" data-p="%s" title="复制完整路径" '
            'aria-label="复制完整路径">&#10697;</button>' % esc_attr(path))


def bar_cell(size, peak):
    ratio = (size * 100.0 / peak) if peak else 0.0
    return '<td class="barcell"><div class="bar"><i style="width:%.1f%%"></i></div></td>' % ratio


def render_tree(node, path, colors, out, open_levels=1):
    kids = node.kids or []
    if not kids:
        return
    kids = sorted(kids, key=lambda n: n.size, reverse=True)
    if node.depth == 0:
        shown = kids[:ROOT_ROWS]          # 根层不设阈值，全部铺开（上限 200 行）
    else:
        limit = max(node.size * HTML_MIN_RATIO, HTML_MIN_BYTES)
        shown = [k for k in kids if k.size >= limit] or kids[:5]
    shown_ids = set(id(k) for k in shown)
    for kid in shown:
        pct = (kid.size * 100.0 / node.size) if node.size else 0.0
        child_path = os.path.join(path, kid.name)
        badge = tag_html(kid.cat, colors) if kid.cat != node.cat else ""
        tail = ('<span class="sz">%s</span><span class="pc">%.1f%%</span>%s%s'
                % (human(kid.size), pct, badge, copy_btn(child_path)))
        if kid.kids:
            open_attr = " open" if kid.depth < open_levels else ""
            out.append('<details%s><summary><span class="name">%s</span>%s</summary>'
                       % (open_attr, esc(kid.name), tail))
            render_tree(kid, child_path, colors, out, open_levels)
            out.append("</details>")
        else:
            out.append('<div class="leaf"><span class="name">%s</span>%s</div>'
                       % (esc(kid.name), tail))
    hidden = [k for k in kids if id(k) not in shown_ids]
    if hidden:
        out.append('<div class="more">另有 %d 项 · 合计 %s</div>'
                   % (len(hidden), human(sum(k.size for k in hidden))))


def write_html(node, root_path, path_out, scanner, timings, colors):
    out = []
    w = out.append
    w("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>")
    w("<meta name='color-scheme' content='dark'>")
    w("<title>diskmap · %s</title><style>%s</style></head><body><div class='wrap'>"
      % (esc(root_path), CSS))
    w("<header><h1>磁盘占用 · <span class='path'>%s</span></h1>" % esc(root_path))
    w("<div class='sub'>%s · 枚举耗时 %.1f s · 总计 %.1f s</div>"
      % (timings["start"], scanner.scan_seconds, timings["total"]))
    w("<div class='chips'>")
    w("<span class='chip'>总计<b>%s</b></span>" % human(node.size))
    if not scanner.fast:
        w("<span class='chip'>逻辑<b>%s</b></span>" % human(scanner.logical))
    w("<span class='chip'>目录<b>%s</b></span>" % fmt_int(scanner.dir_count))
    w("<span class='chip'>文件<b>%s</b></span>" % fmt_int(scanner.file_count))
    w("<span class='chip'>跳过链接<b>%s</b></span>" % fmt_int(scanner.links))
    w("<span class='chip'>无权限<b>%s</b></span>" % fmt_int(len(scanner.denied)))
    rate = (scanner.dir_count + scanner.file_entries) / scanner.scan_seconds \
        if scanner.scan_seconds > 0 else 0
    w("<span class='chip'>条目/秒<b>%s</b></span>" % fmt_int(int(rate)))
    w("</div></header>")

    top_dirs = sorted(node.kids or [], key=lambda n: n.size, reverse=True)[:TOP_N]
    w("<h2>按目录 Top %d</h2><table class='top'>" % TOP_N)
    peak = top_dirs[0].size if top_dirs else 0
    for k in top_dirs:
        pct = (k.size * 100.0 / node.size) if node.size else 0.0
        w("<tr><td class='size'>%s</td><td class='pct'>%.1f%%</td>"
          "<td class='cnt'>%s 文件</td>%s<td class='pathcell'>%s</td></tr>"
          % (human(k.size), pct, fmt_int(k.files), bar_cell(k.size, peak),
             esc(os.path.join(root_path, k.name))))
    w("</table>")

    tops = sorted([k for k in (node.kids or []) if k.depth == 1],
                  key=lambda n: -n.elapsed)[:10]
    if tops:
        w("<h2>顶层目录耗时 Top %d</h2><table class='top'>" % len(tops))
        peak_t = tops[0].elapsed or 1.0
        for k in tops:
            share = (k.elapsed * 100.0 / scanner.scan_seconds) \
                if scanner.scan_seconds > 0 else 0.0
            w("<tr><td class='size'>%s</td><td class='pct'>%.1f%%</td>%s"
              "<td class='pathcell'>%s</td></tr>"
              % (mmss(k.elapsed), share, bar_cell(k.elapsed, peak_t),
                 esc(os.path.join(root_path, k.name))))
        w("</table>")

    w("<h2>按文件 Top %d</h2><table class='top'>" % TOP_FILES)
    files = sorted(scanner.top_files, reverse=True)[:TOP_FILES]
    peak = files[0][0] if files else 0
    for size, p in files:
        w("<tr><td class='size'>%s</td><td class='pct'></td>%s<td class='pathcell'>%s</td></tr>"
          % (human(size), bar_cell(size, peak), esc(p)))
    w("</table>")

    buckets = category_totals(node, scanner.system_files)
    w("<h2>按类别</h2><div class='cats'>")
    for name, size in sorted(buckets.items(), key=lambda kv: -kv[1]):
        pct = (size * 100.0 / node.size) if node.size else 0.0
        w("<div class='cat'><b>%s</b>%s · %.1f%%</div>" % (human(size), esc(name), pct))
    w("</div>")

    rebuild = rebuildable_totals(node)
    if rebuild:
        w("<h2>可重建产物</h2>")
        w("<p class='note'>删了要重装 / 重建，不属于垃圾。</p>")
        w("<div class='cats'>")
        rsum = 0
        for name, size in sorted(rebuild.items(), key=lambda kv: -kv[1]):
            pct = (size * 100.0 / node.size) if node.size else 0.0
            w("<div class='cat'><b>%s</b>%s · %.1f%%</div>"
              % (human(size), esc(name), pct))
            rsum += size
        pct = (rsum * 100.0 / node.size) if node.size else 0.0
        w("<div class='cat' style='border-color:#3a4553'><b>%s</b>合计 · %.1f%%</div>"
          % (human(rsum), pct))
        w("</div>")

    w("<h2>目录树</h2>")
    w("<p class='note'>每层只铺开 ≥ 父目录 1%% 或 ≥ 10 MB 的子项，其余折成一行；点击三角展开。</p>")
    w("<p class='note'>点每行右侧的 &#10697; 复制完整路径，Win+R 粘贴回车即可在资源管理器打开。</p>")
    w("<div class='tree'><details open><summary><span class='name'>%s</span>"
      "<span class='sz'>%s</span></summary>"
      % (esc(os.path.basename(root_path) or root_path), human(node.size)))
    render_tree(node, root_path, colors, out)
    w("</details></div>")

    w("<footer>diskmap · 只读扫描 · 数据为本次枚举结果</footer>")
    w(JS)
    w("</div></body></html>")

    with io.open(path_out, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


# ---------- 主流程 ----------

def signed(n):
    return ("+" if n > 0 else "-") + human(abs(n))


def load_tree(path):
    with io.open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def compare_main(path_a, path_b, out_path):
    """直接比较两份已有的 tree.json，不重新扫描。"""
    try:
        a, b = load_tree(path_a), load_tree(path_b)
    except Exception as exc:
        sys.stderr.write("读取失败：%s\n" % exc)
        return 2

    na, nb = a.get("nodes", {}), b.get("nodes", {})
    root = b.get("root") or a.get("root") or ""
    ma, mb = a.get("mode"), b.get("mode")

    lines = []
    add = lines.append
    add("A  %s   [%s]" % (path_a, ma or "旧版报告，未记录模式"))
    add("B  %s   [%s]" % (path_b, mb or "旧版报告，未记录模式"))
    if ma and mb and ma != mb:
        add("")
        add("⚠ 两份报告的口径不同（%s vs %s）：一边做了硬链接去重、一边没有。" % (ma, mb))
        add("  差异主要来自这里，不代表磁盘真实增减。要比真实变化，请用同模式再扫一次。")
    add("")

    ta = na.get(root, [0, 0])[0]
    tb = nb.get(root, [0, 0])[0]
    fa = na.get(root, [0, 0])[1]
    fb = nb.get(root, [0, 0])[1]
    add("总量  %s -> %s   %s (%+.2f%%)"
        % (human(ta), human(tb), signed(tb - ta), (tb - ta) * 100.0 / ta if ta else 0))
    add("文件  %s -> %s   (%+d)" % (fmt_int(fa), fmt_int(fb), fb - fa))
    add("")

    deltas = []
    for p in set(na) & set(nb):
        if p == root:
            continue
        d = nb[p][0] - na[p][0]
        if abs(d) >= MiB:
            deltas.append((d, nb[p][0], p))
    deltas.sort(reverse=True)

    add("增长 Top %d（≥1 MB）" % TOP_N)
    if deltas:
        for d, s, p in deltas[:TOP_N]:
            if d <= 0:
                break
            add("  %12s  现 %10s   %s" % (signed(d), human(s), p))
    else:
        add("  无（没有超过 1 MB 的增长）")
    add("")

    add("减少 Top %d（≥1 MB）" % TOP_N)
    shrinks = [x for x in deltas if x[0] < 0]
    if shrinks:
        for d, s, p in sorted(shrinks)[:TOP_N]:
            add("  %12s  现 %10s   %s" % (signed(d), human(s), p))
    else:
        add("  无（没有超过 1 MB 的减少）")

    gone = [p for p in na if p not in nb]
    if gone:
        add("")
        add("已消失 %d 项，合计 %s" % (len(gone), human(sum(na[p][0] for p in gone))))

    text = "\n".join(lines) + "\n"
    with io.open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    sys.stdout.write(text)
    sys.stdout.write("对照结果已写入 %s\n" % out_path)
    return 0


def main(argv=None):
    global TOP_N
    parser = argparse.ArgumentParser(
        description="扫描目录树，输出可折叠的 HTML 报告（只读，不删除任何文件）")
    parser.add_argument("root", nargs="?", default=".", help="扫描根目录，默认当前目录")
    parser.add_argument("--out", default=None, help="报告输出目录，默认 <脚本目录>/reports")
    parser.add_argument("--exact", action="store_true",
                        help="精确模式：逐文件取 inode 做硬链接去重（慢约 5 倍）")
    parser.add_argument("--depth", type=int, default=10 ** 6, help="CSV 里保留的最大层级")
    parser.add_argument("--baseline", default=None, help="上一次的 tree.json，用于对比")
    parser.add_argument("--compare", nargs=2, metavar=("A_JSON", "B_JSON"),
                        help="直接比较两份已有的 tree.json，不重新扫描")
    parser.add_argument("--top", type=int, default=TOP_N, help="Top 榜条数")
    args = parser.parse_args(argv)
    TOP_N = args.top

    root = (args.root or "").strip()
    repaired = False
    if len(root) >= 2 and root[0] == root[-1] and root[0] in "\"'":
        root = root[1:-1]
    elif root and root[-1] in "\"'":
        # cmd 会把 "C:\" 里的 \ 当成转义，传过来变成 C:"——这里补回来
        root = root[:-1]
        repaired = True
    if re.fullmatch(r"[A-Za-z]:", root):
        # 在 Windows 里 "C:" 是「当前目录」，不是盘根
        root += "\\"
        repaired = True
    if repaired:
        sys.stderr.write("注意：命令行传过来的路径被截断了，已按 %s 处理\n" % root)
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        sys.stderr.write("不是目录：%s\n" % root)
        return 2

    script_dir = os.path.dirname(os.path.abspath(__file__))
    rules = load_rules(script_dir)
    colors = {r[0]: r[1] for r in rules}
    base = os.path.abspath(args.out or os.path.join(script_dir, "reports"))

    if args.compare:
        os.makedirs(base, exist_ok=True)
        out = os.path.join(base, "compare-%s.txt" % time.strftime("%Y%m%d-%H%M%S"))
        return compare_main(args.compare[0], args.compare[1], out)

    started = time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.time()
    want_progress = sys.stderr.isatty() or os.environ.get("DISKMAP_FORCE_PROGRESS") == "1"
    progress = Progress(root, enabled=want_progress)
    scanner = Scanner(rules, fast=not args.exact,
                      skip_prefixes=[norm(os.path.join(script_dir, "reports")).lower(),
                                     norm(base).lower()],
                      progress=progress)
    node = scanner.scan(root)
    progress.finish()
    scanner.categorize(node, root)

    name = root.rstrip("\\/") or "root"
    label = os.path.basename(name.replace(":", "")) or "root"
    report_dir = os.path.join(base, "%s-%s" % (label, time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(report_dir, exist_ok=True)

    t_write = time.time()
    timings = {"start": started, "total": time.time() - t0, "write": 0.0, "end": ""}
    with io.open(os.path.join(report_dir, "tree.csv"), "w",
                 encoding="utf-8-sig", newline="") as f:
        write_csv(node, root, f, args.depth)
    with io.open(os.path.join(report_dir, "tree.json"), "w", encoding="utf-8") as f:
        write_json(node, root, f, "fast" if scanner.fast else "exact")
    write_html(node, root, os.path.join(report_dir, "index.html"), scanner, timings, colors)
    timings["end"] = time.strftime("%Y-%m-%d %H:%M:%S")
    timings["total"] = time.time() - t0
    timings["write"] = time.time() - t_write
    write_summary(node, root, os.path.join(report_dir, "summary.txt"),
                  scanner, timings, args.baseline)
    with io.open(os.path.join(report_dir, "denied.txt"), "w", encoding="utf-8") as f:
        for path, why in scanner.denied:
            f.write("%s\t%s\n" % (why, path))

    sys.stdout.write("scan done\n")
    sys.stdout.write("  root    %s\n" % root)
    sys.stdout.write("  total   %s%s\n" % (human(node.size),
                                           " (exact)" if not scanner.fast else ""))
    sys.stdout.write("  dirs    %s\n" % fmt_int(scanner.dir_count))
    sys.stdout.write("  files   %s\n" % fmt_int(scanner.file_count))
    sys.stdout.write("  time    %.1f s\n" % timings["total"])
    sys.stdout.write("  report  %s\n" % report_dir)
    return 0


if __name__ == "__main__":
    sys.setrecursionlimit(10000)
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    sys.exit(main())
