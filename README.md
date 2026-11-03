# pdf-chapter-localizer

把 PDF 学术/教材章节转成 Obsidian 中文笔记组的**确定性脚手架**。

它只做可以 100% 交给机器的部分，不接管译述。

## 分工边界

| 层 | 内容 | 谁来做 |
|---|---|---|
| 纯确定性 | 页码偏移探测、分页文本导出、双轨取图、噪声过滤、MD5 去重、图注抽取、命名校验、交付验证、临时目录清理 | 本仓库脚本 |
| 强语义 | 文本失真还原（`!` → π 或负号、上标压平、`×` 丢失）、图号串号纠正、译者注、忠实译述 | AI 或人工 |

判断依据：`pdftotext` 会把 π 和负号都吐成 `!`，把 `2⁸` 压成 `28`。没有任何确定性规则能还原，只能读上下文判断。

## 依赖

- Python 3.10+（仅标准库）
- `pdftotext` / `pdfimages` / `pdftoppm`（poppler-utils 或 MiKTeX）

工具路径优先取命令行参数，其次 `PATH`，最后探测常见安装目录。找不到时会给出明确提示，不会静默失败。

## 子命令

```bash
# 1. 探测页码偏移（PDF 页 = 原书页 + offset），用众数投票
python tools/build_chapter.py probe --pdf <pdf> --scan 1-40

# 2. 导出指定页范围，产出带 === PAGE N === 标记的 pages.txt
python tools/build_chapter.py extract --pdf <pdf> --pages 167-268 --out <workdir>

# 3. 抽图注，产出 captions.csv（英文图注 + 待填的中文短名）
python tools/build_chapter.py captions --pages-txt <workdir>/pages.txt --pages 203-268

# 4. 双轨取图，产出 manifest.csv（已去噪、已算 MD5、标出重号）
python tools/build_chapter.py images --pdf <pdf> --pages 203-268 --out <workdir>

# 5. 填好 manifest 的 final_name 后归档到 attachments/
python tools/build_chapter.py apply-names --manifest <workdir>/manifest.csv --attachments chp5_集合/attachments

# 6. 跑交付验证清单（断链 / 歧义 / BOM / frontmatter / 图片引用 / MOC 结构 / 题号）
python tools/build_chapter.py validate --vault . --dir chp5_集合 --moc chp5_集合.md --chapter 5 --no-answer --work <workdir>

# 7. 清理临时目录
python tools/build_chapter.py clean --work <workdir>
```

## 典型施工顺序

```
probe → extract → captions + images → （AI 译述各节）→ apply-names → validate → clean
```

`captions` 与 `images` 相互独立，可以并行。

## 关键约定

- **页码换算**每本书都要重新 `probe`，不可沿用上一本。
- **图注取自 PDF 文本层**，不依赖看图。命名只需翻译图注，不需要打开图片。
- **`fig-x` / `code-x` 类无编号图**没有图注，靠相邻段落上下文命名。
- **文本层无图注内容**的行会标 `needs_image=yes`，这类才需要定点看图。
- 输出统一 UTF-8 无 BOM。
- `validate` 退出码 0 表示全部通过。

## 已验证范围

两章共 102 页（36 + 66）、17 份笔记、103 张图、19 项检查全通过：断链 0、歧义 0、悬空图片引用 0、未引用附件 0、BOM 无、代码围栏成对。

## 许可

仅供个人学习与研究使用。处理受版权保护的出版物时，衍生译文的复制与分发需自行评估授权范围。
