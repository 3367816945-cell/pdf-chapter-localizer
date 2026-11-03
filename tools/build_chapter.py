#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 章节中文化 —— 确定性脚手架

只做可以 100% 交给机器的部分：
    probe       探测页码偏移（PDF 页 = 原书页 + offset）
    extract     导出指定页范围的文本（带 === PAGE N === 标记）
    images      双轨取图 + 去噪 + 生成 manifest.csv
    apply-names 按 manifest 的 final_name 归档到 attachments/
    validate    跑交付验证清单（断链 / BOM / frontmatter / 图片引用 / MOC 结构 / 题号）
    clean       删除临时目录

不做的部分（必须 AI 或人工）：失真还原、图号纠正、忠实译述、译者注、图片语义命名。

用法示例：
    python build_chapter.py probe    --pdf F:/CS106BX-Reader.pdf --scan 1-40
    python build_chapter.py extract  --pdf F:/CS106BX-Reader.pdf --pages 167-268 --out %TEMP%/cs106b_chp4
    python build_chapter.py images   --pdf F:/CS106BX-Reader.pdf --pages 167-202 --out %TEMP%/cs106b_chp4
    python build_chapter.py apply-names --manifest %TEMP%/cs106b_chp4/manifest.csv --attachments chp4_流/attachments
    python build_chapter.py validate --vault . --dir chp4_流 --moc chp4_流.md --chapter 4
    python build_chapter.py clean    --work %TEMP%/cs106b_chp4
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

# Windows 控制台默认 GBK，统一改成 UTF-8 并容错，避免中文报错中断
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ---------------------------------------------------------------- 通用工具

EXE_SUFFIX = ".exe" if os.name == "nt" else ""

# 工具候选目录（按优先级回溯，实际路径由 --xxx 参数覆盖）
TOOL_HINTS = {
    "pdftotext": [
        r"F:\tex\miktex\bin\x64",
        r"E:\tex\miktex\bin\x64",
        r"D:\tex\miktex\bin\x64",
        r"C:\Program Files\MiKTeX\miktex\bin\x64",
        r"C:\Program Files (x86)\MiKTeX\miktex\bin\x64",
        r"C:\Program Files\poppler\Library\bin",
        r"C:\Program Files\poppler\bin",
    ],
    "pdfimages": [],
    "pdftoppm": [],
}
TOOL_HINTS["pdfimages"] = TOOL_HINTS["pdftotext"]
TOOL_HINTS["pdftoppm"] = TOOL_HINTS["pdftotext"]


class ToolMissing(RuntimeError):
    pass


def resolve_tool(name: str, explicit: str | None) -> str:
    """按 显式参数 → PATH → 候选目录 的顺序定位可执行文件。"""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
        raise ToolMissing(f"--{name} 指定的路径不存在：{explicit}")

    found = shutil.which(name)
    if found:
        return found

    for d in TOOL_HINTS.get(name, []):
        cand = Path(d) / (name + EXE_SUFFIX)
        if cand.is_file():
            return str(cand)

    raise ToolMissing(
        f"找不到 {name}。请用 --{name} <完整路径> 显式指定；"
        f"已尝试 PATH 与候选目录：{', '.join(TOOL_HINTS.get(name, [])) or '无'}"
    )


def parse_range(text: str) -> tuple[int, int]:
    """解析 '167-268' / '167..268' / '167' 为 (起, 止)。"""
    m = re.fullmatch(r"\s*(\d+)\s*(?:[-–.]{1,2}\s*(\d+))?\s*", text)
    if not m:
        raise ValueError(f"无法解析页范围：{text}（示例：167-268）")
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    if b < a:
        raise ValueError(f"页范围起止颠倒：{text}")
    return a, b


def run_tool(cmd: list[str]) -> str:
    """执行外部工具，返回 stdout 文本（UTF-8 容错）。"""
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"命令失败（退出码 {proc.returncode}）：{' '.join(cmd)}\n{proc.stderr.strip()[:800]}"
        )
    return proc.stdout


def write_utf8(path: Path, text: str) -> None:
    """UTF-8 无 BOM 写出（等价于 .NET UTF8Encoding($false)）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


# ---------------------------------------------------------------- probe

# 页眉形态：'58   Functions and Libraries' / '2.1 The idea of a function   57'
RE_HEAD_FIRST = re.compile(r"^\s*(\d{1,4})\s+\S")
RE_HEAD_LAST = re.compile(r"^\s*\S.*?\b(\d{1,4})\s*$")


def cmd_probe(args: argparse.Namespace) -> int:
    tool = resolve_tool("pdftotext", args.pdftotext)
    pdf = Path(args.pdf)
    if not pdf.is_file():
        print(f"[FAIL] PDF 不存在：{pdf}", file=sys.stderr)
        return 2

    start, end = parse_range(args.scan)
    print(f"探测范围：PDF {start}-{end}   源：{pdf}")
    print(f"{'PDF页':>6}  {'候选原书页':>10}  {'偏移':>5}  首行")
    print("-" * 78)

    votes: Counter[int] = Counter()
    for page in range(start, end + 1):
        try:
            text = run_tool([tool, "-f", str(page), "-l", str(page), "-layout", str(pdf), "-"])
        except RuntimeError as exc:
            print(f"{page:>6}  {'--':>10}  {'--':>5}  [读取失败] {exc}")
            continue

        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            print(f"{page:>6}  {'--':>10}  {'--':>5}  [空页]")
            continue

        head = lines[0].strip()[:52]
        printed = None
        m = RE_HEAD_FIRST.match(head)
        if m:
            printed = int(m.group(1))
        else:
            m = RE_HEAD_LAST.match(head)
            if m:
                printed = int(m.group(1))

        if printed is None or not (0 < printed < 2000):
            print(f"{page:>6}  {'?':>10}  {'--':>5}  {head}")
            continue

        off = page - printed
        votes[off] += 1
        print(f"{page:>6}  {printed:>10}  {off:>5}  {head}")

    print("-" * 78)
    if not votes:
        print("[FAIL] 未能从任何页的页眉提取出原书页码，请人工核对首行样式。")
        return 1

    offset, hits = votes.most_common(1)[0]
    total = sum(votes.values())
    print(f"众数偏移 = PDF页 - 原书页 = {offset}（命中 {hits}/{total} 页）")
    print(f"换算规则：原书页 = PDF 页 - {offset}")
    if len(votes) > 1:
        others = ", ".join(f"{k}:{v}" for k, v in votes.most_common()[1:4])
        print(f"[注意] 存在其他偏移票数（{others}），可能是章首/章末无页眉页，跳过即可。")
    return 0


# ---------------------------------------------------------------- extract

def cmd_extract(args: argparse.Namespace) -> int:
    tool = resolve_tool("pdftotext", args.pdftotext)
    pdf = Path(args.pdf)
    if not pdf.is_file():
        print(f"[FAIL] PDF 不存在：{pdf}", file=sys.stderr)
        return 2

    start, end = parse_range(args.pages)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    target = out / "pages.txt"

    chunks: list[str] = []
    chars = 0
    empty: list[int] = []
    for page in range(start, end + 1):
        text = run_tool([tool, "-f", str(page), "-l", str(page), "-layout", str(pdf), "-"])
        body = text.rstrip("\n")
        if not body.strip():
            empty.append(page)
        chars += len(body)
        chunks.append(f"\n=== PAGE {page} ===\n{body}\n")

    write_utf8(target, "".join(chunks))

    marks = sum(1 for ln in target.read_text(encoding="utf-8").splitlines() if ln.startswith("=== PAGE "))
    expect = end - start + 1
    print(f"输出：{target}")
    print(f"页数：{expect}   标记数：{marks}   字符数：{chars}")
    if empty:
        print(f"[注意] 空页：{empty}")
    if marks != expect:
        print(f"[FAIL] 标记数与页数不一致（{marks} != {expect}）")
        return 1
    return 0


# ---------------------------------------------------------------- images

NAME_RE = re.compile(r"^(?:fig|table|ex|code|fig-x|code-x)-.+-p\d+\.png$")


def parse_size(text: str) -> int:
    """'41.5K' / '1.2M' / '832' → 字节数。"""
    m = re.fullmatch(r"\s*([\d.]+)\s*([KMG]?)\s*", text or "")
    if not m:
        return 0
    val = float(m.group(1))
    mul = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[m.group(2)]
    return int(val * mul)


def is_noise(width: int, height: int, size: int) -> str:
    """返回噪声类别，空串表示可用。"""
    if width <= 200 and height <= 100:
        return "header_decoration"
    if abs(width - 858) <= 12 and abs(height - 929) <= 12:
        return "generic_illustration"
    if size and size < 1500 and width * height < 40000:
        return "tiny"
    return ""


def cmd_images(args: argparse.Namespace) -> int:
    pdfimages = resolve_tool("pdfimages", args.pdfimages)
    pdftoppm = resolve_tool("pdftoppm", args.pdftoppm) if args.mode in ("auto", "render") else None
    pdf = Path(args.pdf)
    if not pdf.is_file():
        print(f"[FAIL] PDF 不存在：{pdf}", file=sys.stderr)
        return 2

    start, end = parse_range(args.pages)
    out = Path(args.out)
    work = out / "_raw"
    work.mkdir(parents=True, exist_ok=True)

    # 1) 位图清单
    listing = run_tool([pdfimages, "-list", "-f", str(start), "-l", str(end), str(pdf)])
    rows: dict[int, list[dict]] = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) < 14 or not parts[0].isdigit():
            continue
        try:
            rec = {
                "page": int(parts[0]),
                "type": parts[2],
                "width": int(parts[3]),
                "height": int(parts[4]),
                "size": parse_size(parts[13]),
            }
        except (ValueError, IndexError):
            continue
        rows.setdefault(rec["page"], []).append(rec)

    print(f"位图清单：{sum(len(v) for v in rows.values())} 个对象，覆盖 {len(rows)} 页")

    # 2) 逐页处理
    manifest: list[dict] = []
    seen_md5: dict[str, str] = {}
    pages = list(range(start, end + 1))

    for page in pages:
        objs = rows.get(page, [])
        usable = [o for o in objs if not is_noise(o["width"], o["height"], o["size"])]
        noise = [o for o in objs if is_noise(o["width"], o["height"], o["size"])]

        use_bitmap = args.mode == "bitmap" or (args.mode == "auto" and usable)

        if use_bitmap and objs:
            prefix = work / f"bit-p{page:03d}"
            before = set(work.glob(f"bit-p{page:03d}*"))
            run_tool([pdfimages, "-png", "-f", str(page), "-l", str(page), str(pdf), str(prefix)])
            produced = sorted(set(work.glob(f"bit-p{page:03d}*")) - before)

            # pdfimages 只输出它导出的对象，按体积/尺寸顺序与清单可用项对齐
            for idx, f in enumerate(produced):
                md5 = md5_of(f)
                dup = seen_md5.get(md5, "")
                if not dup:
                    seen_md5[md5] = f.name
                rec = {
                    "pdf_page": page,
                    "source_file": f.name,
                    "kind": "bitmap",
                    "width": "",
                    "height": "",
                    "bytes": f.stat().st_size,
                    "md5": md5,
                    "dup_of": dup,
                    "needs_crop": "no",
                    "final_name": "",
                    "note": "",
                }
                manifest.append(rec)

            for o in noise:
                manifest.append({
                    "pdf_page": page, "source_file": "", "kind": "noise",
                    "width": o["width"], "height": o["height"], "bytes": o["size"],
                    "md5": "", "dup_of": "", "needs_crop": "no", "final_name": "",
                    "note": is_noise(o["width"], o["height"], o["size"]),
                })
        elif pdftoppm:
            prefix = work / f"shot-p{page:03d}"
            before = set(work.glob(f"shot-p{page:03d}*"))
            run_tool([
                pdftoppm, "-png", "-r", str(args.dpi),
                "-f", str(page), "-l", str(page), str(pdf), str(prefix),
            ])
            produced = sorted(set(work.glob(f"shot-p{page:03d}*")) - before)
            for f in produced:
                manifest.append({
                    "pdf_page": page, "source_file": f.name, "kind": "render",
                    "width": "", "height": "", "bytes": f.stat().st_size,
                    "md5": md5_of(f), "dup_of": "", "needs_crop": "yes",
                    "final_name": "", "note": "整页渲染，需裁切出 Figure/Table 区域",
                })
        else:
            continue

    # 3) 写 manifest
    fields = ["pdf_page", "source_file", "kind", "width", "height", "bytes",
              "md5", "dup_of", "needs_crop", "final_name", "note"]
    target = out / "manifest.csv"
    with open(target, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(manifest)

    usable_n = sum(1 for r in manifest if r["kind"] != "noise")
    noise_n = sum(1 for r in manifest if r["kind"] == "noise")
    print(f"清单：{target}")
    print(f"可用 {usable_n} 个（bitmap {sum(1 for r in manifest if r['kind'] == 'bitmap')} / "
          f"render {sum(1 for r in manifest if r['kind'] == 'render')}），噪声 {noise_n} 个")
    print("[人工] 下一步：填写 manifest.csv 的 final_name（语义命名必须人来定），再跑 apply-names。")
    return 0


# ---------------------------------------------------------------- captions

# 图注首行：Figure 5-1. / Table 5-3: / Figure 4-8 – （兼容中英分隔符）
RE_CAPTION = re.compile(
    r"^\s*(Figure|Table)\s+(\d+)\s*[-–]\s*(\d+)\s*[.:：–—-]?\s*(.*)$",
    re.IGNORECASE,
)
RE_PAGE_MARK = re.compile(r"^===\s*PAGE\s+(\d+)\s*===$")
# 图注续行终止条件：空行、新图注、页码标记、或典型页眉
RE_CAPTION_STOP = re.compile(r"^\s*$|^\s*(Figure|Table)\s+\d+\s*[-–]\s*\d+|^===\s*PAGE\b")


def load_pages(path: Path) -> dict[int, list[str]]:
    """把 pages.txt 拆成 {PDF页: [行]}。"""
    pages: dict[int, list[str]] = {}
    current: int | None = None
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        m = RE_PAGE_MARK.match(line)
        if m:
            current = int(m.group(1))
            pages[current] = []
            continue
        if current is not None:
            pages[current].append(line.rstrip())
    return pages


def looks_like_header(line: str) -> bool:
    """页眉形如 '58   Functions and Libraries' 或 '5.1 Stacks   213'。"""
    s = line.strip()
    if not s:
        return True
    if re.match(r"^\d{1,4}\s+\S", s):
        return True
    if re.match(r"^\S.*?\b\d{1,4}$", s) and len(s) < 80:
        return True
    return False


def cmd_captions(args: argparse.Namespace) -> int:
    src = Path(args.pages_txt)
    if not src.is_file():
        print(f"[FAIL] pages.txt 不存在：{src}", file=sys.stderr)
        return 2

    pages = load_pages(src)
    if not pages:
        print(f"[FAIL] 未解析到任何 === PAGE N === 标记：{src}", file=sys.stderr)
        return 2

    start, end = (parse_range(args.pages) if args.pages
                  else (min(pages), max(pages)))

    rows: list[dict] = []
    for page in sorted(pages):
        if not (start <= page <= end):
            continue
        lines = pages[page]
        for idx, line in enumerate(lines):
            m = RE_CAPTION.match(line)
            if not m:
                continue
            kind_raw, chap, num, tail = m.groups()
            kind = "fig" if kind_raw.lower() == "figure" else "table"

            # 续接：图注可能折行，最多再收 2 行
            parts = [tail.strip()] if tail.strip() else []
            for nxt in lines[idx + 1: idx + 3]:
                if RE_CAPTION_STOP.match(nxt) or looks_like_header(nxt):
                    break
                parts.append(nxt.strip())

            caption = " ".join(p for p in parts if p)
            caption = re.sub(r"\s+", " ", caption).strip()
            # 去掉折行残留的连字符
            caption = re.sub(r"(\w)-\s+(\w)", r"\1\2", caption)

            label = f"{kind_raw.capitalize()} {chap}-{num}"
            rows.append({
                "pdf_page": page,
                "label": label,
                "kind": kind,
                "english_caption": caption,
                "suggested_name": "",
                "needs_image": "no" if caption else "yes",
                "note": "" if caption else "文本层无图注内容，需定点看图命名",
            })

    # 重复 label 检查（同一图跨页、或文本层串号）
    seen: dict[str, list[int]] = {}
    for r in rows:
        seen.setdefault(r["label"], []).append(r["pdf_page"])
    for r in rows:
        pages_of = seen[r["label"]]
        if len(pages_of) > 1:
            r["note"] = (r["note"] + "；" if r["note"] else "") + \
                        f"该编号出现在多页 {pages_of}，确认是否跨页图或串号"

    out = Path(args.out) if args.out else src.parent / "captions.csv"
    fields = ["pdf_page", "label", "kind", "english_caption",
              "suggested_name", "needs_image", "note"]
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    need = sum(1 for r in rows if r["needs_image"] == "yes")
    dup = len(seen) - len({k for k, v in seen.items() if len(v) == 1})
    print(f"输出：{out}")
    print(f"图注 {len(rows)} 条（fig {sum(1 for r in rows if r['kind'] == 'fig')} / "
          f"table {sum(1 for r in rows if r['kind'] == 'table')}），"
          f"文本层缺内容 {need} 条，重号 {dup} 组")
    if rows:
        print("-" * 78)
        for r in rows[:args.show]:
            cap = r["english_caption"][:56] or "[无]"
            print(f"p{r['pdf_page']:<4} {r['label']:<12} {cap}")
        if len(rows) > args.show:
            print(f"... 另有 {len(rows) - args.show} 条，见 CSV")
    print("[人工] 下一步：把 english_caption 译成中文短名填入 suggested_name"
          "（约 20 token/条，不需要看图）；needs_image=yes 的才需要定点看图。")
    return 0


# ---------------------------------------------------------------- apply-names

def cmd_apply_names(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest)
    if not manifest.is_file():
        print(f"[FAIL] manifest 不存在：{manifest}", file=sys.stderr)
        return 2

    src_dir = manifest.parent
    for sub in (src_dir / "_raw", src_dir):
        if sub.is_dir():
            src_dir = sub
            break

    dest = Path(args.attachments)
    dest.mkdir(parents=True, exist_ok=True)

    copied = skipped = failed = 0
    with open(manifest, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("final_name") or "").strip()
            if not name:
                continue
            source = (row.get("source_file") or "").strip()
            if row.get("kind") == "noise":
                print(f"[跳过] 噪声项不应归档：p{row.get('pdf_page')}")
                skipped += 1
                continue
            if not NAME_RE.match(name):
                print(f"[FAIL] 命名不合规（应形如 fig-4-1-短名-p172.png）：{name}")
                failed += 1
                continue
            s = Path(source)
            if not s.is_absolute():
                s = src_dir / source
            if not s.is_file():
                print(f"[FAIL] 源文件缺失：{s}")
                failed += 1
                continue
            d = dest / name
            if d.exists():
                print(f"[跳过] 目标已存在，不覆盖：{name}")
                skipped += 1
                continue
            shutil.copy2(s, d)
            copied += 1

    print(f"归档：{dest}")
    print(f"复制 {copied} / 跳过 {skipped} / 失败 {failed}")
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------- validate

EXCLUDE_DIRS = {".obsidian", ".trae", ".tools", ".git", ".trash"}

RE_WIKILINK = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
RE_MDLINK = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
RE_FENCE = re.compile(r"^\s*```")
RE_QNUM = re.compile(r"^###\s+(\d+)\s*[.、．]")

FRONTMATTER_FIELDS = ["title", "chapter", "section", "source", "source_pages", "tags"]
MOC_REQUIRED = [
    ("章首题词", r">\s*\[!quote\]\s*章首题词"),
    ("章首引言", r"^##\s+章首引言"),
    ("本章目录", r"^##\s+本章目录"),
    ("学习提示", r">\s*\[!tip\]\s*学习提示"),
    ("术语表", r"^##\s+术语表"),
    ("对照表", r"^##\s+对照表"),
    ("已知偏差", r"^##\s+已知偏差"),
    ("学习进度", r"^##\s+学习进度"),
]


def read_text(path: Path) -> str:
    # utf-8-sig 兼容 BOM，但 BOM 本身会单独检测
    return path.read_text(encoding="utf-8-sig", errors="replace")


def has_bom(path: Path) -> bool:
    with open(path, "rb") as fh:
        return fh.read(3) == b"\xef\xbb\xbf"


def parse_frontmatter(text: str) -> dict | None:
    if not text.startswith("---"):
        return None
    parts = text.split("\n---", 2)
    if len(parts) < 2:
        return None
    body = parts[0][3:]
    fields: dict[str, str] = {}
    key = None
    for line in body.splitlines():
        if not line.strip():
            continue
        if line.startswith(("  ", "\t")):
            # 列表型字段（如 tags）的值写在缩进项里，标记为已填
            if key and line.strip().startswith("-") and not fields.get(key):
                fields[key] = "<list>"
            continue
        m = re.match(r"^([A-Za-z_]+)\s*:\s*(.*)$", line)
        if m:
            key = m.group(1)
            fields[key] = m.group(2).strip()
    return fields


def all_vault_md(vault: Path) -> list[Path]:
    out: list[Path] = []
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            if f.endswith(".md"):
                out.append(Path(root) / f)
    return out


def build_link_index(vault: Path, files: list[Path]) -> dict[str, list[str]]:
    """stem -> [相对路径（无 .md 后缀）]"""
    index: dict[str, list[str]] = {}
    for f in files:
        r = rel(f, vault)
        stem = r[:-3] if r.endswith(".md") else r
        index.setdefault(Path(stem).name, []).append(stem)
    return index


def cmd_validate(args: argparse.Namespace) -> int:
    vault = Path(args.vault).resolve()
    section_dir = vault / args.dir
    moc = vault / args.moc

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    # 1) 目录与 md 清单
    md_files = sorted(section_dir.glob("*.md")) if section_dir.is_dir() else []
    check("节目录存在", section_dir.is_dir(), str(section_dir))
    check("md 数量", bool(md_files), f"{len(md_files)} 份：" + "、".join(p.name for p in md_files))
    if args.expect_md:
        check("md 数量符合预期", len(md_files) == args.expect_md,
              f"实际 {len(md_files)} / 期望 {args.expect_md}")

    # 2) frontmatter 六字段
    bad_fm: list[str] = []
    for f in md_files:
        fm = parse_frontmatter(read_text(f))
        if fm is None:
            bad_fm.append(f"{f.name}(无 frontmatter)")
            continue
        missing = [k for k in FRONTMATTER_FIELDS if not fm.get(k)]
        if missing:
            bad_fm.append(f"{f.name}(缺 {','.join(missing)})")
        if args.chapter and fm.get("chapter", "").strip().strip('"') != str(args.chapter):
            bad_fm.append(f"{f.name}(chapter={fm.get('chapter')})")
    check("frontmatter 六字段", not bad_fm, "；".join(bad_fm))

    # 3) 附件命名
    att = section_dir / "attachments"
    pngs = sorted(att.glob("*.png")) if att.is_dir() else []
    bad_names = [p.name for p in pngs if not NAME_RE.match(p.name)]
    check("附件命名规范", not bad_names, f"{len(pngs)} 张；不合规：{'、'.join(bad_names[:6])}")
    check("附件目录非空", bool(pngs), f"{len(pngs)} 张 PNG")

    # 4) 图片引用存在性
    referenced: set[str] = set()
    missing_refs: list[str] = []
    for f in md_files:
        base = f.parent
        for m in RE_MDLINK.finditer(read_text(f)):
            target = m.group(1).strip().strip('"').strip("'")
            if target.startswith(("http://", "https://", "data:")):
                continue
            target = target.split("#")[0]
            p = (base / target).resolve()
            referenced.add(str(p))
            if not p.exists():
                missing_refs.append(f"{f.name} -> {target}")
    check("图片引用无悬空", not missing_refs, "；".join(missing_refs[:8]))

    unused = [p.name for p in pngs if str(p.resolve()) not in referenced]
    check("无未引用附件", not unused, "；".join(unused[:8]))

    # 5) 答案文件缺席（可选约定）
    if args.no_answer:
        answers = [p.name for p in md_files if "答案" in p.name]
        check("无答案文件", not answers, "、".join(answers))

    # 6) BOM
    bom = [p.name for p in md_files if has_bom(p)]
    check("UTF-8 无 BOM", not bom, "、".join(bom[:8]))

    # 7) 代码围栏成对
    odd = []
    for f in md_files:
        n = sum(1 for ln in read_text(f).splitlines() if RE_FENCE.match(ln))
        if n % 2:
            odd.append(f"{f.name}({n})")
    check("代码围栏成对", not odd, "、".join(odd))

    # 8) 题号连续性
    qdetail = []
    for name in ("复习题.md", "编程习题.md"):
        f = section_dir / name
        if not f.is_file():
            check(f"{name} 存在", False, str(f))
            continue
        nums = [int(m.group(1)) for ln in read_text(f).splitlines() if (m := RE_QNUM.match(ln))]
        dups = sorted({n for n in nums if nums.count(n) > 1})
        gaps = []
        if nums:
            gaps = [n for n in range(1, max(nums) + 1) if n not in nums]
        qdetail.append(f"{name}: {len(nums)} 题" + (f"，重复 {dups}" if dups else "") +
                       (f"，缺号 {gaps}" if gaps else ""))
        check(f"{name} 题号", bool(nums), f"{len(nums)} 题" + (f"；重复 {dups}" if dups else "") +
              (f"；缺号 {gaps}" if gaps else ""))

    # 9) MOC 结构
    if moc.is_file():
        text = read_text(moc)
        check("MOC frontmatter", parse_frontmatter(text) is not None, str(moc))
        missing_secs = [label for label, pat in MOC_REQUIRED
                        if not re.search(pat, text, flags=re.MULTILINE)]
        check("MOC 九小节", not missing_secs, "缺：" + "、".join(missing_secs) if missing_secs else "齐全")
        progress = text.split("## 学习进度", 1)[1] if "## 学习进度" in text else ""
        boxes = len(re.findall(r"^\s*-\s*\[[ xX]\]", progress, flags=re.MULTILINE))
        check("MOC 学习进度复选框", boxes == len(md_files),
              f"{boxes} 项 / md {len(md_files)} 份")
        if args.expect_boxes:
            check("复选框数量符合预期", boxes == args.expect_boxes,
                  f"实际 {boxes} / 期望 {args.expect_boxes}")
        # callout 语法：] 后需有空格
        bad_callout = re.findall(r">\s*\[!\w+\][^\s\n]", text)
        check("callout 语法", not bad_callout, f"{len(bad_callout)} 处 ] 后缺空格")
    else:
        check("MOC 存在", False, str(moc))

    # 10) 全库 wikilink 消歧
    files = all_vault_md(vault)
    index = build_link_index(vault, files)
    dead: list[str] = []
    ambi: list[str] = []
    total_links = 0
    for f in files:
        r = rel(f, vault)
        for m in RE_WIKILINK.finditer(read_text(f)):
            target = m.group(1).strip()
            if target.startswith(("http://", "https://")):
                continue
            total_links += 1
            if target.endswith(".md"):
                target = target[:-3]
            if "/" in target or "\\" in target:
                norm = target.replace("\\", "/")
                stem = norm[:-3] if norm.endswith(".md") else norm
                hits = [p for p in files if rel(p, vault)[:-3] == stem]
                if not hits:
                    dead.append(f"{r} -> [[{target}]]")
            else:
                cands = index.get(target, [])
                if not cands:
                    dead.append(f"{r} -> [[{target}]]")
                elif len(cands) > 1:
                    ambi.append(f"{r} -> [[{target}]]（{len(cands)} 处同名）")
    check("全库 wikilink 无断链", not dead, f"扫描 {len(files)} 份 md / {total_links} 条链接；断链 "
          + str(len(dead)) + ("：" + "；".join(dead[:8]) if dead else ""))
    check("全库 wikilink 无歧义", not ambi, "；".join(ambi[:8]))

    # 11) 临时目录残留
    if args.work:
        w = Path(args.work)
        check("临时目录已清理", not w.exists(), str(w))

    # 输出
    width = max(len(c[0]) for c in checks) + 2
    print("=" * 78)
    print(f"验证对象：{section_dir}")
    print("=" * 78)
    failed = 0
    for name, ok, detail in checks:
        flag = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{flag}] {name.ljust(width)}{detail}")
    print("-" * 78)
    print(f"共 {len(checks)} 项，通过 {len(checks) - failed}，失败 {failed}")
    if qdetail:
        print("题号明细：" + "；".join(qdetail))
    return 0 if failed == 0 else 1


# ---------------------------------------------------------------- clean

def cmd_clean(args: argparse.Namespace) -> int:
    work = Path(args.work)
    if not work.exists():
        print(f"[OK] 不存在，无需清理：{work}")
        return 0
    if args.dry_run:
        print(f"[dry-run] 将删除：{work}")
        return 0
    shutil.rmtree(work)
    ok = not work.exists()
    print(f"[{'OK' if ok else 'FAIL'}] 删除 {work} → {'已清除' if ok else '仍存在'}")
    return 0 if ok else 1


# ---------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_chapter.py",
        description="PDF 章节中文化确定性脚手架（不含译述）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probe", help="探测页码偏移（众数投票）")
    p.add_argument("--pdf", required=True)
    p.add_argument("--scan", default="1-40", help="探测页范围，默认 1-40")
    p.add_argument("--pdftotext")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("extract", help="导出页范围文本")
    p.add_argument("--pdf", required=True)
    p.add_argument("--pages", required=True, help="如 167-268")
    p.add_argument("--out", required=True)
    p.add_argument("--pdftotext")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("images", help="双轨取图并生成 manifest.csv")
    p.add_argument("--pdf", required=True)
    p.add_argument("--pages", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--mode", choices=["auto", "bitmap", "render"], default="auto")
    p.add_argument("--dpi", type=int, default=150)
    p.add_argument("--pdfimages")
    p.add_argument("--pdftoppm")
    p.set_defaults(func=cmd_images)

    p = sub.add_parser("captions", help="从 pages.txt 抽图注，生成 captions.csv（免看图命名）")
    p.add_argument("--pages-txt", required=True)
    p.add_argument("--pages", help="限定页范围，如 203-268；省略则用全文")
    p.add_argument("--out")
    p.add_argument("--show", type=int, default=20, help="终端预览条数，默认 20")
    p.set_defaults(func=cmd_captions)

    p = sub.add_parser("apply-names", help="按 manifest 的 final_name 归档")
    p.add_argument("--manifest", required=True)
    p.add_argument("--attachments", required=True)
    p.set_defaults(func=cmd_apply_names)

    p = sub.add_parser("validate", help="跑交付验证清单")
    p.add_argument("--vault", default=".")
    p.add_argument("--dir", required=True, help="节目录名，如 chp5_集合")
    p.add_argument("--moc", required=True, help="MOC 文件名，如 chp5_集合.md")
    p.add_argument("--chapter", type=int, help="期望章节号")
    p.add_argument("--expect-md", type=int, help="期望 md 份数")
    p.add_argument("--expect-boxes", type=int, help="期望复选框项数")
    p.add_argument("--no-answer", action="store_true", help="断言无答案文件")
    p.add_argument("--work", help="额外检查该临时目录是否已清理")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("clean", help="删除临时目录")
    p.add_argument("--work", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_clean)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ToolMissing as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
