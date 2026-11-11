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
import sys
import time

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
MiB = 1024 * 1024
SYSTEM_FILE_NAMES = {"pagefile.sys", "hiberfil.sys", "swapfile.sys", "dumpstack.log.tmp"}
TOP_N = 20
TOP_FILES = 30
HTML_MIN_BYTES = 10 * MiB
HTML_MIN_RATIO = 0.01

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


def esc(s):
    return _html.escape(s, quote=False)


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
    __slots__ = ("name", "depth", "size", "files", "mtime", "kids", "cat")

    def __init__(self, name, depth):
        self.name = name
        self.depth = depth
        self.size = 0
        self.files = 0
        self.mtime = 0.0
        self.kids = None
        self.cat = "data"


class Scanner(object):
    def __init__(self, rules, fast=False, skip_prefix=None):
        self.rules = rules
        self.fast = fast
        self.skip_prefix = skip_prefix
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
        if self.skip_prefix and norm(path).lower().startswith(self.skip_prefix):
            self.skipped_reports += 1
            return Node(os.path.basename(path) or path, depth)

        self.dir_count += 1
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
                    child = self.walk(entry.path, depth + 1)
                    node.size += child.size
                    node.files += child.files
                    if child.mtime > node.mtime:
                        node.mtime = child.mtime
                    if node.kids is None:
                        node.kids = []
                    node.kids.append(child)
                else:
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


# ---------- 输出：CSV / JSON / summary ----------

def write_csv(node, root_path, fp, max_depth):
    w = csv.writer(fp)
    w.writerow(["path", "depth", "size_bytes", "size", "files", "last_write", "category"])
    for path, cur in iter_nodes(node, root_path):
        if cur.depth <= max_depth:
            w.writerow([path, cur.depth, cur.size, human(cur.size), cur.files,
                        stamp(cur.mtime), cur.cat])


def write_json(node, root_path, fp):
    nodes = {}
    for path, cur in iter_nodes(node, root_path):
        nodes[path] = [cur.size, cur.files, cur.cat]
    json.dump({"root": root_path, "nodes": nodes}, fp, ensure_ascii=False)


def write_summary(node, root_path, path_out, scanner, timings, baseline_path=None):
    lines = []
    add = lines.append
    add("扫描根    %s" % root_path)
    add("开始      %s" % timings["start"])
    add("结束      %s" % timings["end"])
    add("耗时      %.1f s（枚举 %.1f / 写出 %.1f）"
        % (timings["total"], scanner.scan_seconds, timings["write"]))
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
    add("物理占用  %s" % human(node.size))
    add("逻辑占用  %s       %s" % (human(scanner.logical),
                                   "未去重，含硬链接重复" if scanner.fast else "硬链接已去重"))
    add("")

    add("按目录 Top %d" % TOP_N)
    for k in sorted(node.kids or [], key=lambda n: n.size, reverse=True)[:TOP_N]:
        pct = (k.size * 100.0 / node.size) if node.size else 0.0
        add("  %10s %6.1f%%   %s" % (human(k.size), pct, os.path.join(root_path, k.name)))
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
.barcell{width:34%;padding-right:16px}
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


def tag_html(name, colors):
    if name == "data":
        return ""
    if name in TAG_CLASSES:
        return '<span class="tag %s">%s</span>' % (name, esc(name))
    color = colors.get(name, "#888888")
    return ('<span class="tag" style="background:%s;border-color:%s;color:%s">%s</span>'
            % (hex_rgba(color, 0.14), hex_rgba(color, 0.28), color, esc(name)))


def bar_cell(size, peak):
    ratio = (size * 100.0 / peak) if peak else 0.0
    return '<td class="barcell"><div class="bar"><i style="width:%.1f%%"></i></div></td>' % ratio


def render_tree(node, path, colors, out, open_levels=1):
    kids = node.kids or []
    if not kids:
        return
    kids = sorted(kids, key=lambda n: n.size, reverse=True)
    limit = max(node.size * HTML_MIN_RATIO, HTML_MIN_BYTES)
    shown = [k for k in kids if k.size >= limit] or kids[:5]
    shown_ids = set(id(k) for k in shown)
    for kid in shown:
        pct = (kid.size * 100.0 / node.size) if node.size else 0.0
        badge = tag_html(kid.cat, colors) if kid.cat != node.cat else ""
        tail = ('<span class="sz">%s</span><span class="pc">%.1f%%</span>%s'
                % (human(kid.size), pct, badge))
        if kid.kids:
            open_attr = " open" if kid.depth < open_levels else ""
            out.append('<details%s><summary><span class="name">%s</span>%s</summary>'
                       % (open_attr, esc(kid.name), tail))
            render_tree(kid, os.path.join(path, kid.name), colors, out, open_levels)
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
    w("</div></header>")

    top_dirs = sorted(node.kids or [], key=lambda n: n.size, reverse=True)[:TOP_N]
    w("<h2>按目录 Top %d</h2><table class='top'>" % TOP_N)
    peak = top_dirs[0].size if top_dirs else 0
    for k in top_dirs:
        pct = (k.size * 100.0 / node.size) if node.size else 0.0
        w("<tr><td class='size'>%s</td><td class='pct'>%.1f%%</td>%s"
          "<td class='pathcell'>%s</td></tr>"
          % (human(k.size), pct, bar_cell(k.size, peak),
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

    w("<h2>目录树</h2>")
    w("<p class='note'>每层只铺开 ≥ 父目录 1%% 或 ≥ 10 MB 的子项，其余折成一行；点击三角展开。</p>")
    w("<div class='tree'><details open><summary><span class='name'>%s</span>"
      "<span class='sz'>%s</span></summary>"
      % (esc(os.path.basename(root_path) or root_path), human(node.size)))
    render_tree(node, root_path, colors, out)
    w("</details></div>")

    w("<footer>diskmap · 只读扫描 · 数据为本次枚举结果</footer>")
    w("</div></body></html>")

    with io.open(path_out, "w", encoding="utf-8") as f:
        f.write("\n".join(out))


# ---------- 主流程 ----------

def main(argv=None):
    global TOP_N
    parser = argparse.ArgumentParser(
        description="扫描目录树，输出可折叠的 HTML 报告（只读，不删除任何文件）")
    parser.add_argument("root", nargs="?", default=".", help="扫描根目录，默认当前目录")
    parser.add_argument("--out", default=None, help="报告输出目录，默认 <脚本目录>/reports")
    parser.add_argument("--fast", action="store_true", help="不取 inode，跳过硬链接去重")
    parser.add_argument("--depth", type=int, default=10 ** 6, help="CSV 里保留的最大层级")
    parser.add_argument("--baseline", default=None, help="上一次的 tree.json，用于对比")
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

    started = time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.time()
    scanner = Scanner(rules, fast=args.fast, skip_prefix=norm(base).lower())
    node = scanner.scan(root)
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
        write_json(node, root, f)
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
                                           " (no dedupe)" if scanner.fast else " (physical)"))
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
