#!/usr/bin/env python3
"""
AI 测试用例质量评估脚本
对比 AI 生成的 HTML 测试用例表格与人工修改后的版本，
计算 AI 用例准确率并生成可视化 Diff 报告。

准确率 = (AI生成 - 人工删除) / (AI生成 + 人工新增)
       = AI 被保留的字数 / (AI 总投入 + 人工补充)
同时惩罚 AI 冗余（删除）和遗漏（新增）。

Usage:
    python diff_testcases.py <AI生成.html> <人工修改.html> [-o 输出报告.html]
"""

import argparse
import difflib
import json
import re
import sys
from html import escape as html_escape
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

# ── 颜色处理 ──────────────────────────────────────────────


def _parse_color(c):
    """解析颜色字符串为 (r, g, b)，失败返回 None。支持 #rgb / #rrggbb / rgb(...)。"""
    if not c:
        return None
    c = c.strip().lower()
    m = re.match(r"#([0-9a-f]{6})$", c)
    if m:
        x = int(m.group(1), 16)
        return ((x >> 16) & 0xff, (x >> 8) & 0xff, x & 0xff)
    m = re.match(r"#([0-9a-f]{3})$", c)
    if m:
        h = m.group(1)
        return (int(h[0] * 2, 16), int(h[1] * 2, 16), int(h[2] * 2, 16))
    m = re.match(r"rgba?\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)", c)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _is_default_color(rgb):
    """判断颜色是否为"默认文本色"（黑/深灰/白/中性灰），即非高亮。"""
    r, g, b = rgb
    if max(r, g, b) < 80:          # 接近黑
        return True
    if min(r, g, b) > 230:         # 接近白
        return True
    if max(r, g, b) - min(r, g, b) < 25:  # 灰色（RGB 三通道接近）
        return True
    return False


def _dominant_channel(rgb):
    """返回主导 RGB 通道：'R' / 'G' / 'B'。"""
    r, g, b = rgb
    if r >= g and r >= b:
        return "R"
    if g >= r and g >= b:
        return "G"
    return "B"


class ColorMatcher:
    """判定一个颜色字符串是否属于"同色系"（主导通道相同）。"""

    def __init__(self, target_hex):
        self.target = (target_hex or "").strip().lower()
        rgb = _parse_color(self.target)
        self.target_rgb = rgb
        self.dominant = _dominant_channel(rgb) if rgb else None

    def __call__(self, color):
        if not color:
            return False
        c = color.strip().lower()
        if self.target and c == self.target:
            return True
        rgb = _parse_color(c)
        if rgb is None or _is_default_color(rgb):
            return False
        return self.dominant is not None and _dominant_channel(rgb) == self.dominant


# ── HTML 解析 ──────────────────────────────────────────────


def parse_css_color_map(soup):
    """从 <style> 块提取 CSS class → 文字颜色 的映射。"""
    tag = soup.find("style")
    if not tag or not tag.string:
        return {}
    cmap = {}
    for m in re.finditer(r"\.(\w+)\s*\{([^}]+)\}", tag.string):
        cls, props = m.group(1), m.group(2)
        cm = re.search(r"(?<![-\w])color\s*:\s*([^;}\s]+)", props)
        if cm:
            cmap[cls] = cm.group(1).strip().lower()
    return cmap


def _element_color(el, cmap):
    """取元素的有效文字颜色：inline style 优先于 class。"""
    if not isinstance(el, Tag):
        return None
    style = el.get("style", "")
    if style:
        m = re.search(r'(?<![-\w])color\s*:\s*([^;"\s]+)', style)
        if m:
            return m.group(1).strip().lower()
    for c in el.get("class", []):
        if c in cmap:
            return cmap[c]
    return None


def detect_highlight_color(soup, cmap):
    """探测表格主体内最常见的"非默认"文本色，按字符数加权。无则返回 None。"""
    table = soup.find("table", class_="waffle")
    if not table:
        return None
    tbody = table.find("tbody") or table
    rows = tbody.find_all("tr")[1:]  # 跳过表头

    counts = {}
    for row in rows:
        for td in row.find_all("td"):
            # 取 td 自身的颜色作为代表（嵌套 span 通常不覆盖）
            color = _element_color(td, cmap)
            if not color:
                continue
            rgb = _parse_color(color)
            if rgb is None or _is_default_color(rgb):
                continue
            counts[color] = counts.get(color, 0) + len(td.get_text(strip=True))

    if not counts:
        return None
    return max(counts.items(), key=lambda x: x[1])[0]


def _is_highlight(element, cmap, matcher):
    """判断元素的文字颜色是否被 matcher 视为高亮色。"""
    color = _element_color(element, cmap)
    return matcher(color) if color else False


def _extract_segments(el, cmap, matcher, parent_red=False):
    """递归提取元素内的 (text, is_red) 片段列表。is_red 实际含义为"高亮色"。"""
    segs = []
    red = parent_red or _is_highlight(el, cmap, matcher)
    for ch in el.children:
        if isinstance(ch, NavigableString):
            t = str(ch)
            if t:
                segs.append((t, red))
        elif isinstance(ch, Tag):
            if ch.name == "br":
                segs.append(("\n", red))
            else:
                segs.extend(_extract_segments(ch, cmap, matcher, red))
    return segs


def _make_cell(segments):
    return {
        "segments": segments,
        "full_text": "".join(t for t, _ in segments).strip(),
        "red_text": "".join(t for t, r in segments if r).strip(),
        "black_text": "".join(t for t, r in segments if not r).strip(),
    }


def parse_html_table(filepath, match_target=None, role="文件"):
    """解析 HTML 文件，返回 (rows, detected_color, match_target_used)。

    match_target:
      - None → 用本文件自动探测出的高亮色作为匹配目标（AI 文件场景）。
      - 指定值 → 用 ColorMatcher 做"同色系"匹配（人工文件场景，传入 AI 探测出的色）。
    detected_color: 本文件中实际探测到的高亮色（用于日志展示，与 match_target 可能不同）。
    role: 出错信息中标识文件角色（"AI" / "人工" 等）。
    """
    soup = BeautifulSoup(Path(filepath).read_text(encoding="utf-8"), "html.parser")
    cmap = parse_css_color_map(soup)

    table = soup.find("table", class_="waffle")
    if not table:
        sys.exit(f"Error: 在 {filepath} 中未找到 <table class='waffle'>")

    detected = detect_highlight_color(soup, cmap)

    if match_target is None:
        if detected is None:
            sys.exit(
                f"Error: 无法从{role}文件 {filepath} 自动探测高亮色。\n"
                f"请确认表格内容已用非黑色文本标记，或通过 CLI 参数手动指定颜色。"
            )
        match_target = detected

    matcher = ColorMatcher(match_target)

    tbody = table.find("tbody") or table
    rows = tbody.find_all("tr")[1:]  # 跳过列标题行

    NUM_COLS = 5
    spans = {}  # col -> (remaining, cell_data)
    parsed = []
    cur_mod = ""

    for row in rows:
        tds = row.find_all("td")
        cells = []
        ti = 0

        for col in range(NUM_COLS):
            if col in spans and spans[col][0] > 0:
                cells.append(spans[col][1])
                spans[col] = (spans[col][0] - 1, spans[col][1])
            elif ti < len(tds):
                td = tds[ti]
                ti += 1
                cell = _make_cell(_extract_segments(td, cmap, matcher))
                rs = int(td.get("rowspan", 1))
                if rs > 1:
                    spans[col] = (rs - 1, cell)
                cells.append(cell)
            else:
                cells.append(_make_cell([]))

        mod = cells[0]["full_text"].strip()
        if mod:
            cur_mod = mod
        elif cur_mod:
            cells[0] = _make_cell([(cur_mod, False)])

        # Google Sheets HTML exports include spacer/freezebar rows, and some sheets
        # also contain section-only rows. They should not participate in testcase
        # matching or scoring because they create fake additions/deletions.
        if not any(cells[i]["full_text"].strip() for i in range(1, NUM_COLS)):
            continue

        name = cells[1]["full_text"].strip()
        parsed.append(
            {
                "module": cur_mod,
                "name": name,
                "norm_name": _normalize_name(name),
                "name_variants": _name_variants(name),
                "body_text": "\n".join(c["full_text"].strip() for c in cells[2:5]),
                "key": (_normalize_name(cur_mod), _normalize_name(name)),
                "cells": cells,
            }
        )

    return parsed, detected, match_target


# ── Diff 计算 ──────────────────────────────────────────────


def _diff(a, b):
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    ops = sm.get_opcodes()
    d = sum(i2 - i1 for op, i1, i2, _, _ in ops if op in ("delete", "replace"))
    a_ = sum(j2 - j1 for op, _, _, j1, j2 in ops if op in ("insert", "replace"))
    return d, a_, ops


# ── 行匹配 ────────────────────────────────────────────────


# 模糊匹配阈值：身份分数（用例名 + 整行正文加权）超过该值才视为同一条用例。
# 略高于 0.5，避免只因通用词（视频/开关/效果）重复就错配。
FUZZY_MATCH_THRESHOLD = 0.62


def _normalize_for_match(text):
    """规范化文本用于匹配：去空白、编号、常见标点差异。"""
    if not text:
        return ""
    text = str(text).lower()
    text = text.replace("\u3000", " ")
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"^\s*\d+(?:\.\d+)*\s*", "", text)
    text = re.sub(r"[\s\r\n\t]+", "", text)
    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("【", "").replace("】", "")
    text = text.replace("：", ":").replace("；", ";").replace("，", ",")
    text = text.replace("。", ".").replace("、", ",")
    text = text.replace("—", "-").replace("–", "-").replace("－", "-")
    text = re.sub(r"[\"'`·]", "", text)
    return text


def _normalize_name(name):
    """规范化用例名/模块名。"""
    s = _normalize_for_match(name)
    # 去掉章节号残留，如 2.1/2.2。normalize 后小数点可能仍保留。
    s = re.sub(r"^\d+(?:\.\d+)*", "", s)
    return s


def _name_variants(name):
    """生成用例名的匹配变体，处理“分类-用例名”“模块——用例名”等人工重组。"""
    raw = _normalize_name(name)
    variants = {raw}
    if not raw:
        return [""]

    parts = [p for p in re.split(r"[-:|/]+", raw) if p]
    if len(parts) > 1:
        variants.add(parts[-1])
        variants.add("".join(parts[1:]))

    return sorted(v for v in variants if v)


def _similarity(a, b):
    """文本相似度，兼顾整体相似和“短文本被长文本扩写包含”的情况。"""
    a = _normalize_for_match(a)
    b = _normalize_for_match(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 4 and shorter in longer:
        return 0.98

    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    ratio = sm.ratio()
    common = sum(block.size for block in sm.get_matching_blocks())
    coverage = common / max(1, len(shorter))
    # coverage 对扩写/压缩很关键，但容易被少量通用词抬高，所以轻微打折。
    return max(ratio, coverage * 0.88)


def _best_similarity(a_values, b_values):
    return max(_similarity(a, b) for a in a_values for b in b_values)


def _row_similarity(a_row, b_row):
    """计算两行是否为同一条用例的身份分数。

    只看两个信号：用例名变体相似度 和 整行正文相似度。
    标题强一致时，body 仅作辅助加成 —— 描述被扩写也仍判为同一条。
    """
    name = _best_similarity(a_row["name_variants"], b_row["name_variants"])
    body = _similarity(a_row["body_text"], b_row["body_text"])

    if name >= 0.9:
        return 0.7 + 0.3 * body
    return 0.5 * name + 0.5 * body


def _match_rows(ai_rows, hu_rows):
    """匹配 AI 行与人工行。

    所有过阈值的候选对按分数降序贪心吃掉；冲突极少，足够好。
    返回 pairs: list of (ai_idx | None, hu_idx | None)，按 AI 行顺序，
    末尾追加未匹配上的人工新增行。
    """
    n_ai, n_hu = len(ai_rows), len(hu_rows)
    ai_to_hu = [None] * n_ai
    hu_used = [False] * n_hu

    candidates = []
    for i, ai in enumerate(ai_rows):
        for j, hu in enumerate(hu_rows):
            s = _row_similarity(ai, hu)
            if s >= FUZZY_MATCH_THRESHOLD:
                candidates.append((s, i, j))
    candidates.sort(reverse=True)

    for _s, i, j in candidates:
        if ai_to_hu[i] is None and not hu_used[j]:
            ai_to_hu[i] = j
            hu_used[j] = True

    pairs = [(i, ai_to_hu[i]) for i in range(n_ai)]
    for j in range(n_hu):
        if not hu_used[j]:
            pairs.append((None, j))
    return pairs


# ── HTML 渲染 ──────────────────────────────────────────────


def _h(text):
    """文本转 HTML（转义 + 换行转 <br>）。"""
    return html_escape(text).replace("\n", "<br>")


def _build_color_map(segments):
    """从 segments 构建 position → is_red 映射，与 stripped full_text 对齐。"""
    raw_flags = []
    for text, is_red in segments:
        raw_flags.extend([is_red] * len(text))
    raw_text = "".join(t for t, _ in segments)
    left = len(raw_text) - len(raw_text.lstrip())
    length = len(raw_text.strip())
    return raw_flags[left : left + length]


def _is_red_range(cmap, start, end):
    """判断 cmap[start:end] 区间是否包含任意红色（用于决定差异是否计分）。"""
    if not cmap:
        return False
    end = min(end, len(cmap))
    start = max(0, start)
    for i in range(start, end):
        if cmap[i]:
            return True
    return False


def _colorize(text, cmap, pos):
    """渲染文本，根据 color map 保留原始红/黑颜色。"""
    parts = []
    i = 0
    while i < len(text):
        ci = pos + i
        is_red = cmap[ci] if ci < len(cmap) else False
        j = i + 1
        while j < len(text):
            cj = pos + j
            if (cmap[cj] if cj < len(cmap) else False) != is_red:
                break
            j += 1
        chunk = text[i:j]
        if is_red:
            parts.append(f'<span class="red-text">{_h(chunk)}</span>')
        else:
            parts.append(_h(chunk))
        i = j
    return "".join(parts)


def _render_diff(ai_text, hu_text, ai_cmap=None, hu_cmap=None, ai_pos=0, hu_pos=0):
    """行级 diff。ai_cmap/hu_cmap 非空时为 equal 块保留原始颜色；
    若某段差异在 AI 与人工双方都不涉及红色文本（即不计分），
    则只渲染人工最终文本为普通字符，不加任何 diff 样式。"""
    if ai_text == hu_text:
        if ai_cmap:
            return _colorize(ai_text, ai_cmap, ai_pos)
        return f'<span class="red-text">{_h(ai_text)}</span>' if ai_text else ""

    ai_lines = ai_text.split("\n")
    hu_lines = hu_text.split("\n")

    ai_starts = [0]
    for line in ai_lines[:-1]:
        ai_starts.append(ai_starts[-1] + len(line) + 1)
    hu_starts = [0]
    for line in hu_lines[:-1]:
        hu_starts.append(hu_starts[-1] + len(line) + 1)

    sm = difflib.SequenceMatcher(None, ai_lines, hu_lines, autojunk=False)
    parts = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        a_off = ai_starts[i1] if i1 < len(ai_starts) else 0
        h_off = hu_starts[j1] if j1 < len(hu_starts) else 0
        ai_chunk = chr(10).join(ai_lines[i1:i2])
        hu_chunk = chr(10).join(hu_lines[j1:j2])
        a_start, a_end = ai_pos + a_off, ai_pos + a_off + len(ai_chunk)
        h_start, h_end = hu_pos + h_off, hu_pos + h_off + len(hu_chunk)

        if op == "equal":
            if ai_cmap:
                parts.append(_colorize(ai_chunk, ai_cmap, a_start))
            else:
                parts.append(f'<span class="red-text">{_h(ai_chunk)}</span>')
            continue

        scoring = (
            _is_red_range(ai_cmap, a_start, a_end)
            or _is_red_range(hu_cmap, h_start, h_end)
        )
        if not scoring:
            # 双方都是黑字 → 不计分，直接渲染人工最终文本（删除则丢弃）
            if hu_chunk:
                parts.append(_h(hu_chunk))
            continue

        if op == "delete":
            parts.append(f'<span class="deleted">{_h(ai_chunk)}</span>')
        elif op == "insert":
            parts.append(f'<span class="added">【{_h(hu_chunk)}】</span>')
        elif op == "replace":
            parts.append(_render_line_replace(
                ai_lines[i1:i2], hu_lines[j1:j2], ai_cmap, hu_cmap, a_start, h_start
            ))
    return "".join(parts)


def _render_line_replace(old_lines, new_lines, ai_cmap=None, hu_cmap=None, ai_pos=0, hu_pos=0):
    """对替换块逐行配对做字符级 diff。非计分黑字差异不加样式；
    黑字行被人工删除时直接丢弃，不在报告中残留。"""
    parts = []
    a_pos = ai_pos
    h_pos = hu_pos
    for i in range(max(len(old_lines), len(new_lines))):
        ol = old_lines[i] if i < len(old_lines) else None
        nl = new_lines[i] if i < len(new_lines) else None

        rendered = None
        if ol is None:
            scoring = _is_red_range(hu_cmap, h_pos, h_pos + len(nl))
            rendered = (
                f'<span class="added">【{_h(nl)}】</span>' if scoring else _h(nl)
            )
        elif nl is None:
            scoring = _is_red_range(ai_cmap, a_pos, a_pos + len(ol))
            if scoring:
                rendered = f'<span class="deleted">{_h(ol)}</span>'
            # 否则黑字被删 → 不渲染
        else:
            rendered = _char_diff(ol, nl, ai_cmap, hu_cmap, a_pos, h_pos)

        if rendered:
            if parts:
                parts.append("<br>")
            parts.append(rendered)

        if ol is not None:
            a_pos += len(ol) + 1
        if nl is not None:
            h_pos += len(nl) + 1
    return "".join(parts)


def _clean_opcodes(ops, min_equal=3):
    """合并过短的 equal 块（< min_equal 字符）与相邻非 equal 块，避免碎片化。"""
    if not ops:
        return ops
    cleaned = []
    for op, i1, i2, j1, j2 in ops:
        if op == "equal" and (i2 - i1) < min_equal:
            op = "replace"
        if cleaned and cleaned[-1][0] != "equal" and op != "equal":
            p = cleaned[-1]
            cleaned[-1] = ("replace", p[1], i2, p[3], j2)
        else:
            cleaned.append((op, i1, i2, j1, j2))
    return cleaned


def _char_diff(old, new, ai_cmap=None, hu_cmap=None, ai_pos=0, hu_pos=0):
    """单行字符级 diff。共同前缀 >= 3 时即使整体相似度低也做细粒度 diff。
    非计分（双方都不涉及红色）的字符差异不加样式，仅渲染人工最终文本。"""
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    prefix = 0
    for a, b in zip(old, new):
        if a == b:
            prefix += 1
        else:
            break
    if sm.ratio() < 0.3 and prefix < 3:
        scoring = (
            _is_red_range(ai_cmap, ai_pos, ai_pos + len(old))
            or _is_red_range(hu_cmap, hu_pos, hu_pos + len(new))
        )
        if not scoring:
            return _h(new)
        return (
            f'<span class="deleted">{_h(old)}</span>'
            f'<span class="modified">【{_h(new)}】</span>'
        )
    ops = _clean_opcodes(sm.get_opcodes())
    parts = []
    for op, i1, i2, j1, j2 in ops:
        a_start, a_end = ai_pos + i1, ai_pos + i2
        h_start, h_end = hu_pos + j1, hu_pos + j2

        if op == "equal":
            chunk = old[i1:i2]
            if ai_cmap:
                parts.append(_colorize(chunk, ai_cmap, a_start))
            else:
                parts.append(f'<span class="red-text">{_h(chunk)}</span>')
            continue

        scoring = (
            _is_red_range(ai_cmap, a_start, a_end)
            or _is_red_range(hu_cmap, h_start, h_end)
        )
        if not scoring:
            if op != "delete":
                parts.append(_h(new[j1:j2]))
            continue

        if op == "delete":
            parts.append(f'<span class="deleted">{_h(old[i1:i2])}</span>')
        elif op == "insert":
            parts.append(f'<span class="added">【{_h(new[j1:j2])}】</span>')
        elif op == "replace":
            parts.append(f'<span class="deleted">{_h(old[i1:i2])}</span>')
            parts.append(f'<span class="modified">【{_h(new[j1:j2])}】</span>')
    return "".join(parts)


def _render_cell(ai_cell, hu_cell, status, row_scoring=True):
    """渲染单个单元格的 HTML。

    row_scoring=False 时（整行删除/新增但全是黑字，不计分），使用更轻的样式：
    删除仅保留删除线，新增不加 【】 和绿底。
    """
    if status == "deleted":
        cls = "deleted" if row_scoring else "deleted-plain"
        return f'<span class="{cls}">{_h(ai_cell["full_text"])}</span>'
    if status == "added":
        if row_scoring:
            return f'<span class="added">【{_h(hu_cell["full_text"])}】</span>'
        return _h(hu_cell["full_text"])

    # matched — 全文 diff，保留红/黑颜色
    ai_full = ai_cell["full_text"]
    hu_full = hu_cell["full_text"] if hu_cell else ""
    ai_cmap = _build_color_map(ai_cell["segments"])
    hu_cmap = _build_color_map(hu_cell["segments"]) if hu_cell else []

    if ai_full == hu_full:
        return _colorize(ai_full, ai_cmap, 0)

    return _render_diff(ai_full, hu_full, ai_cmap, hu_cmap, 0, 0)


def _render_cell_plain(cell):
    """渲染单元格的"原始"形态：仅按 segments 的红/黑色保留，不做 diff。"""
    if cell is None:
        return ''
    if not cell["full_text"]:
        return ''
    cmap = _build_color_map(cell["segments"])
    return _colorize(cell["full_text"], cmap, 0)


# ── 主流程 ─────────────────────────────────────────────────


def generate_report(ai_rows, hu_rows, output):
    pairs = _match_rows(ai_rows, hu_rows)

    ai_red_total = sum(
        len(c["red_text"]) for r in ai_rows for c in r["cells"]
    )
    total_del = 0
    total_add = 0
    results = []
    n_matched = n_modified = n_deleted = n_added = 0
    # 按行保留率分桶（仅统计 AI 含红字的行；人工新增红字行单独一桶）
    buckets = {"usable": 0, "minor": 0, "severe": 0, "deleted": 0, "added": 0}

    for ai_idx, hu_idx in pairs:
        if ai_idx is not None and hu_idx is not None:
            ai = ai_rows[ai_idx]
            hm = hu_rows[hu_idx]
            d = a = 0
            for i in range(5):
                dd, aa, _ = _diff(
                    ai["cells"][i]["red_text"], hm["cells"][i]["red_text"]
                )
                d += dd
                a += aa
            total_del += d
            total_add += a
            ai_red = sum(len(c["red_text"]) for c in ai["cells"])
            bucket_key = None
            if ai_red > 0:
                # 对称公式：与报告顶部总准确率一致，同时惩罚 AI 冗余(删除)和人工补充(新增)
                retention = (ai_red - d) / (ai_red + a) * 100
                if retention >= 90:
                    bucket_key = "usable"
                elif retention >= 70:
                    bucket_key = "minor"
                else:
                    bucket_key = "severe"
                buckets[bucket_key] += 1
            status = "modified" if (d or a) else "matched"
            if status == "modified":
                n_modified += 1
            else:
                n_matched += 1
            results.append((ai, hm, status, bucket_key))
        elif ai_idx is not None:
            ai = ai_rows[ai_idx]
            ai_red = sum(len(c["red_text"]) for c in ai["cells"])
            total_del += ai_red
            bucket_key = "deleted" if ai_red > 0 else None
            if bucket_key:
                buckets[bucket_key] += 1
            n_deleted += 1
            results.append((ai, None, "deleted", bucket_key))
        else:
            hm = hu_rows[hu_idx]
            hm_red = sum(len(c["red_text"]) for c in hm["cells"])
            total_add += hm_red
            bucket_key = "added" if hm_red > 0 else None
            if bucket_key:
                buckets[bucket_key] += 1
            n_added += 1
            results.append((None, hm, "added", bucket_key))

    denom = ai_red_total + total_add
    accuracy = (ai_red_total - total_del) / denom * 100 if denom else 0

    Path(output).write_text(
        _build_html(results, ai_red_total, total_del, total_add, accuracy, buckets),
        encoding="utf-8",
    )

    print(f"\n{'='*45}")
    print(f"  AI 测试用例质量评估报告")
    print(f"{'='*45}")
    print(f"  AI 生成红色文本:  {ai_red_total} 字符")
    print(f"  人工删除:         {total_del} 字符")
    print(f"  人工新增:         {total_add} 字符")
    print(f"  AI 用例准确率:    {accuracy:.1f}%")
    print(f"  ─" * 22)
    print(f"  行匹配: 完全一致 {n_matched} / 修改 {n_modified} / 删除 {n_deleted} / 新增 {n_added}")
    if accuracy < 30:
        print(f"  [WARN] 准确率偏低，可能存在未匹配上的同义行；请人工核对。")
    print(f"{'='*45}")
    print(f"  报告已生成: {output}")


def _build_html(results, ai_red_total, total_del, total_add, accuracy, buckets):
    hdrs = ["模块", "用例名称", "描述", "预期", "备注"]
    hdr_html = "".join(f'<th class="hdr">{h}</th>' for h in hdrs)
    hdr_html += '<th class="hdr detail-col">操作</th>'

    bucket_total = sum(buckets.values())
    bucket_defs = [
        ("直接可用",   "≥ 90%",        "usable",  "#0d7a0d"),
        ("轻微修改",   "70%-90%",      "minor",   "#1a73e8"),
        ("严重修改",   "0%-70%",       "severe",  "#f29900"),
        ("人工删除",   "整行删除",       "deleted", "#ea4335"),
        ("人工新增",   "绿色新行",       "added",   "#5f6368"),
    ]
    bucket_rows = []
    for label, rng, key, color in bucket_defs:
        n = buckets[key]
        pct = (n / bucket_total * 100) if bucket_total else 0
        clickable = ' class="bucket-row"' if n > 0 else ' class="bucket-row disabled"'
        bucket_rows.append(
            f'<tr{clickable} data-bucket="{key}">'
            f'<td><span class="dot" style="background:{color}"></span>{label}</td>'
            f'<td style="color:#888">{rng}</td>'
            f'<td style="text-align:right;font-weight:bold">{n}</td>'
            f'<td style="text-align:right">{pct:.1f}%</td>'
            f'<td class="bucket-counter">{"" if n == 0 else "&nbsp;"}</td></tr>'
        )
    bucket_rows.append(
        f'<tr class="total-row"><td colspan="2">合计</td>'
        f'<td style="text-align:right;font-weight:bold">{bucket_total}</td>'
        f'<td style="text-align:right">100%</td><td></td></tr>'
    )
    bucket_html = "".join(bucket_rows)

    rows = []
    details = []  # 每行原始内容（AI / 人工）的浮窗数据
    for idx, (ai, hm, st, bucket_key) in enumerate(results):
        attrs = []
        classes = []
        if st == "deleted":
            row_scoring = any(c["red_text"] for c in ai["cells"])
            if row_scoring:
                classes.append("row-del")
            cs = "".join(
                f"<td>{_render_cell(ai['cells'][i], None, 'deleted', row_scoring)}</td>"
                for i in range(5)
            )
        elif st == "added":
            row_scoring = any(c["red_text"] for c in hm["cells"])
            if row_scoring:
                classes.append("row-add")
            cs = "".join(
                f"<td>{_render_cell(None, hm['cells'][i], 'added', row_scoring)}</td>"
                for i in range(5)
            )
        else:
            cs = "".join(
                f"<td>{_render_cell(ai['cells'][i], hm['cells'][i], 'matched')}</td>"
                for i in range(5)
            )
        cs += (
            f'<td class="detail-col">'
            f'<button class="detail-btn" data-idx="{idx}" type="button">详情</button>'
            f'</td>'
        )
        if classes:
            attrs.append(f'class="{" ".join(classes)}"')
        if bucket_key:
            attrs.append(f'data-bucket="{bucket_key}"')
        attr_str = (" " + " ".join(attrs)) if attrs else ""
        rows.append(f"<tr{attr_str}>{cs}</tr>")

        # 收集弹窗数据
        ai_html = [_render_cell_plain(c) for c in ai["cells"]] if ai else None
        hu_html = [_render_cell_plain(c) for c in hm["cells"]] if hm else None
        title = (ai or hm)["name"] if (ai or hm) else ""
        module = (ai or hm)["module"] if (ai or hm) else ""
        details.append({
            "title": title,
            "module": module,
            "status": st,
            "ai": ai_html,
            "hu": hu_html,
        })

    # </script> 在 JSON 里不可能出现，但保险起见做个替换防注入
    details_json = json.dumps(details, ensure_ascii=False).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>AI 测试用例质量评估报告</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:Arial,"Microsoft YaHei",sans-serif;margin:0;padding:24px;background:#f5f7fa}}
h1{{font-size:22px;color:#333;margin:0 0 20px}}
.cards{{display:flex;gap:14px;margin-bottom:20px;flex-wrap:wrap}}
.card{{background:#fff;border-radius:10px;padding:18px 24px;
       box-shadow:0 2px 8px rgba(0,0,0,.07);flex:1;min-width:180px}}
.card .lbl{{color:#888;font-size:12px;margin-bottom:4px}}
.card .val{{font-size:26px;font-weight:bold}}
.card.rate .val{{color:#1a73e8}}
.card.del .val{{color:#ea4335}}
.card.add .val{{color:#0d7a0d}}
.card.tot .val{{color:#333}}
small{{font-size:13px;color:#aaa;font-weight:normal}}
.formula{{background:#fff;border-radius:8px;padding:10px 18px;margin-bottom:16px;
          box-shadow:0 2px 8px rgba(0,0,0,.07);font-size:12px;color:#666;
          line-height:1.6}}
.legend{{background:#fff;border-radius:8px;padding:10px 18px;margin-bottom:16px;
         box-shadow:0 2px 8px rgba(0,0,0,.07);font-size:12px;color:#666;
         display:flex;gap:20px;flex-wrap:wrap}}
.legend span{{display:inline-flex;align-items:center;gap:5px}}
table{{border-collapse:collapse;width:100%;background:#fff;
       border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.07)}}
th.hdr{{background:#2f4f4f;color:#fff;font-weight:bold;text-align:center;
        padding:10px 8px;font-size:10pt}}
td{{border:1px solid #e0e0e0;padding:8px 10px;vertical-align:top;
    font-size:10pt;line-height:1.7}}
.red-text{{color:#ea4335}}
.deleted{{text-decoration:line-through;color:#999;background:#fff0f0;
          padding:1px 2px;border-radius:2px}}
.deleted-plain{{text-decoration:line-through}}
.added{{color:#0d7a0d;font-weight:bold;background:#f0fff0;
        padding:1px 2px;border-radius:2px}}
.modified{{color:#1155cc;font-weight:bold;background:#f0f4ff;
           padding:1px 2px;border-radius:2px}}
.row-del{{background:#fafafa}}
.row-del td{{color:#999}}
.row-add{{background:#f0fff0}}
.bucket-wrap{{background:#fff;border-radius:8px;padding:14px 18px;margin-bottom:16px;
              box-shadow:0 2px 8px rgba(0,0,0,.07)}}
.bucket-wrap h2{{margin:0 0 10px;font-size:14px;color:#333}}
.bucket-table{{width:auto;border-radius:0;box-shadow:none;min-width:380px}}
.bucket-table td{{padding:6px 14px;font-size:12px;border:none;
                  border-bottom:1px solid #f0f0f0}}
.bucket-table tr:last-child td{{border-bottom:none}}
.bucket-table .total-row td{{font-weight:bold;color:#333;border-top:1px solid #ddd;
                             padding-top:8px}}
.bucket-table tr.bucket-row{{cursor:pointer;transition:background .15s}}
.bucket-table tr.bucket-row:hover{{background:#f0f4fa}}
.bucket-table tr.bucket-row.disabled{{cursor:default;opacity:.5}}
.bucket-table tr.bucket-row.disabled:hover{{background:transparent}}
.bucket-table tr.bucket-active{{background:#eef3fc}}
.bucket-table .bucket-counter{{font-size:11px;color:#999;text-align:right;
                               white-space:nowrap;padding-left:8px}}
.dot{{display:inline-block;width:8px;height:8px;border-radius:50%;
      margin-right:8px;vertical-align:middle}}
tr.row-highlight{{box-shadow:inset 4px 0 0 #1a73e8;background:#fff8e1 !important}}
tr.row-highlight td{{color:inherit !important}}
tr.row-current{{outline:2px solid #ff6d00;outline-offset:-2px;background:#fff3e0 !important}}
#nav-pill{{position:fixed;right:24px;bottom:32px;background:#fff;
           border:1px solid #e0e0e0;border-radius:999px;
           padding:6px 6px 6px 16px;display:flex;align-items:center;gap:10px;
           box-shadow:0 6px 20px rgba(0,0,0,.18);z-index:1000;font-size:13px}}
#nav-pill[hidden]{{display:none}}
#nav-pill .pill-label{{font-weight:bold;color:#333}}
#nav-pill .pill-counter{{color:#666;min-width:54px;text-align:center}}
#nav-pill button{{border:none;background:#1a73e8;color:#fff;
                  padding:7px 14px;border-radius:999px;cursor:pointer;
                  font-size:12px;font-weight:500}}
#nav-pill button:hover{{background:#1557b0}}
#nav-pill .pill-prev{{background:#eef3fc;color:#1a73e8}}
#nav-pill .pill-prev:hover{{background:#dde7f7}}
.detail-col{{width:64px;text-align:center;white-space:nowrap}}
.detail-btn{{border:1px solid #1a73e8;background:#fff;color:#1a73e8;
             padding:3px 10px;border-radius:4px;cursor:pointer;font-size:12px}}
.detail-btn:hover{{background:#1a73e8;color:#fff}}
.row-del .detail-btn{{border-color:#bbb;color:#999}}
.row-del .detail-btn:hover{{background:#999;color:#fff;border-color:#999}}
#detail-modal{{position:fixed;inset:0;z-index:2000;display:flex;
               align-items:center;justify-content:center}}
#detail-modal[hidden]{{display:none}}
#detail-modal .modal-backdrop{{position:absolute;inset:0;
                               background:rgba(0,0,0,.45)}}
#detail-modal .modal-box{{position:relative;background:#fff;border-radius:10px;
                          width:min(1280px, 96vw);max-height:88vh;display:flex;
                          flex-direction:column;box-shadow:0 12px 36px rgba(0,0,0,.25)}}
#detail-modal .modal-header{{display:flex;align-items:center;
                             justify-content:space-between;padding:14px 20px;
                             border-bottom:1px solid #eee}}
#detail-modal .modal-header h3{{margin:0;font-size:15px;color:#333}}
#detail-modal .modal-header .modal-meta{{font-size:12px;color:#888;margin-top:2px}}
#detail-modal .modal-close{{border:none;background:transparent;font-size:22px;
                            color:#999;cursor:pointer;line-height:1;padding:4px 10px;
                            border-radius:6px}}
#detail-modal .modal-close:hover{{background:#f5f5f5;color:#333}}
#detail-modal .modal-actions{{display:flex;align-items:center;gap:12px}}
.mode-toggle{{display:inline-flex;background:#f1f3f5;border-radius:8px;
              padding:3px;font-size:12px;gap:2px}}
.mode-toggle button{{border:none;background:transparent;color:#5f6368;
                     padding:5px 14px;cursor:pointer;font-size:12px;
                     border-radius:6px;font-weight:500;transition:background .15s}}
.mode-toggle button:not(.active):hover{{background:#e6e8eb}}
.mode-toggle button.active{{background:#fff;color:#1a73e8;
                            box-shadow:0 1px 2px rgba(0,0,0,.08)}}
#detail-modal .modal-body{{padding:16px 20px;overflow:auto;flex:1;background:#fafbfc}}
.compare-table{{width:100%;border-collapse:collapse;background:#fff;
                table-layout:fixed;border:1px solid #e6e8eb;
                border-radius:8px;overflow:hidden}}
.compare-table th,.compare-table td{{border:1px solid #eef0f3;padding:10px 14px;
                                     font-size:12px;line-height:1.7;
                                     vertical-align:top;word-break:break-word}}
.compare-table th{{font-weight:600}}
.compare-table th.field-label{{background:#fafbfc;color:#5f6368;width:88px;
                               text-align:left;font-weight:500;
                               white-space:nowrap}}
.compare-table th.col-head{{background:#fafbfc;color:#5f6368;text-align:center;
                            font-weight:500;white-space:nowrap}}
.compare-table th.corner{{background:#fafbfc;border-right-color:#e6e8eb;
                          border-bottom-color:#e6e8eb}}
.compare-table th.ai-head{{background:#e8f0fe;color:#1557b0;white-space:nowrap}}
.compare-table th.hu-head{{background:#e6f4ea;color:#0d652d;white-space:nowrap}}
/* 列模式：AI/人工 在顶部 → 居中 + 底部强调线 */
.compare-table.col-mode th.ai-head{{text-align:center;
                                     box-shadow:inset 0 -2px 0 #1a73e8}}
.compare-table.col-mode th.hu-head{{text-align:center;
                                     box-shadow:inset 0 -2px 0 #0d7a0d}}
/* 行模式：AI/人工 在左侧 → 左对齐 + 左侧强调线，垂直居中避免大色块 */
.compare-table.row-mode th.ai-head{{text-align:left;vertical-align:middle;
                                     width:108px;box-shadow:inset 4px 0 0 #1a73e8}}
.compare-table.row-mode th.hu-head{{text-align:left;vertical-align:middle;
                                     width:108px;box-shadow:inset 4px 0 0 #0d7a0d}}
.compare-table td.empty-cell{{color:#bbb;font-style:italic;text-align:center}}
.detail-empty{{color:#bbb;font-style:italic;font-size:12px;padding:8px 12px}}
#nav-pill .pill-close{{background:transparent;color:#999;padding:4px 9px;
                       font-size:18px;line-height:1;font-weight:bold}}
#nav-pill .pill-close:hover{{background:#f5f5f5;color:#333}}
</style>
</head>
<body>
<h1>AI 测试用例质量评估报告</h1>

<div class="cards">
  <div class="card rate">
    <div class="lbl">AI 用例准确率</div>
    <div class="val">{accuracy:.1f}%</div>
  </div>
  <div class="card tot">
    <div class="lbl">AI 生成红色文本</div>
    <div class="val">{ai_red_total} <small>字符</small></div>
  </div>
  <div class="card del">
    <div class="lbl">人工删除</div>
    <div class="val">{total_del} <small>字符</small></div>
  </div>
  <div class="card add">
    <div class="lbl">人工新增</div>
    <div class="val">{total_add} <small>字符</small></div>
  </div>
</div>

<div class="formula">
  AI 用例准确率 = ( AI 生成红色文本 − 人工删除 ) / ( AI 生成红色文本 + 人工新增 )
  <br>
  <span style="color:#999">同时惩罚 AI 冗余（删除）和遗漏（新增）；当 AI 全被采纳且无新增时为 100%。</span>
</div>

<div class="bucket-wrap">
  <h2>用例修改率分布（按行）</h2>
  <table class="bucket-table">
    <tbody>{bucket_html}</tbody>
  </table>
  <div style="font-size:11px;color:#999;margin-top:8px">
    准确率 = ( AI 红字 − 该行被删红字 ) ÷ ( AI 红字 + 该行人工新增红字 )；与顶部总准确率公式一致，同时惩罚删除和新增。仅统计 AI 含红字的行与人工新增的红字行。
    <br>点击任一行可高亮下方对应用例，并显示右下角悬浮按钮，点「下一条」依次循环该桶内的用例。
  </div>
</div>

<div class="legend">
  <span><span class="deleted">删除示例</span> 被人工删除</span>
  <span><span class="added">【新增示例】</span> 被人工新增</span>
  <span><span class="modified">【修改示例】</span> 被人工修改（替换）</span>
  <span><span class="red-text">红色文本</span> AI 生成未修改</span>
</div>

<table>
<thead><tr>{hdr_html}</tr></thead>
<tbody>
{"".join(rows)}
</tbody>
</table>
<div id="nav-pill" hidden>
  <span class="pill-label"></span>
  <span class="pill-counter"></span>
  <button class="pill-prev" type="button">↑ 上一条</button>
  <button class="pill-next" type="button">下一条 ↓</button>
  <button class="pill-close" type="button" aria-label="关闭">×</button>
</div>
<div id="detail-modal" hidden>
  <div class="modal-backdrop"></div>
  <div class="modal-box">
    <div class="modal-header">
      <div>
        <h3 class="modal-title">用例对比</h3>
        <div class="modal-meta"></div>
      </div>
      <div class="modal-actions">
        <div class="mode-toggle" role="group" aria-label="布局模式">
          <button class="active" data-mode="col" type="button">左右对比</button>
          <button data-mode="row" type="button">行模式</button>
        </div>
        <button class="modal-close" type="button" aria-label="关闭">×</button>
      </div>
    </div>
    <div class="modal-body"></div>
  </div>
</div>
<script id="row-details" type="application/json">{details_json}</script>
<script>
(function(){{
  let activeBucket = null;
  let activeMatches = [];
  let idx = -1;

  const pill = document.getElementById('nav-pill');
  const labelEl = pill.querySelector('.pill-label');
  const counterEl = pill.querySelector('.pill-counter');
  const prevBtn = pill.querySelector('.pill-prev');
  const nextBtn = pill.querySelector('.pill-next');
  const closeBtn = pill.querySelector('.pill-close');

  function clearAll(){{
    document.querySelectorAll('tr.row-highlight, tr.row-current')
      .forEach(function(el){{ el.classList.remove('row-highlight', 'row-current'); }});
    document.querySelectorAll('tr.bucket-active')
      .forEach(function(el){{ el.classList.remove('bucket-active'); }});
    document.querySelectorAll('.bucket-counter')
      .forEach(function(el){{ el.innerHTML = '&nbsp;'; }});
  }}

  function jump(bucket, mode){{
    // mode: 'reset' (回到第 1 条) | 'next' (循环 +1) | 'prev' (循环 -1)
    const trigger = document.querySelector(
      'tr.bucket-row[data-bucket="' + bucket + '"]'
    );
    const matches = Array.from(document.querySelectorAll(
      'tbody tr[data-bucket="' + bucket + '"]'
    )).filter(function(r){{ return !r.classList.contains('bucket-row'); }});
    if (matches.length === 0) return;

    clearAll();
    if (trigger) trigger.classList.add('bucket-active');
    matches.forEach(function(m){{ m.classList.add('row-highlight'); }});

    const n = matches.length;
    if (mode === 'reset' || activeBucket !== bucket) {{
      idx = 0;
    }} else if (mode === 'prev') {{
      idx = (idx - 1 + n) % n;
    }} else {{
      idx = (idx + 1) % n;
    }}
    activeBucket = bucket;
    activeMatches = matches;

    const target = matches[idx];
    target.classList.add('row-current');
    target.scrollIntoView({{behavior: 'smooth', block: 'center'}});

    const text = (idx + 1) + ' / ' + matches.length;
    const tblCounter = trigger && trigger.querySelector('.bucket-counter');
    if (tblCounter) tblCounter.textContent = text;

    const labelTd = trigger && trigger.querySelector('td:first-child');
    labelEl.textContent = labelTd ? labelTd.textContent.trim() : bucket;
    counterEl.textContent = text;
    pill.hidden = false;
  }}

  document.querySelectorAll('.bucket-row').forEach(function(tr){{
    if (tr.classList.contains('disabled')) return;
    tr.addEventListener('click', function(){{ jump(tr.dataset.bucket, 'reset'); }});
  }});

  prevBtn.addEventListener('click', function(){{
    if (activeBucket) jump(activeBucket, 'prev');
  }});

  nextBtn.addEventListener('click', function(){{
    if (activeBucket) jump(activeBucket, 'next');
  }});

  closeBtn.addEventListener('click', function(){{
    clearAll();
    pill.hidden = true;
    activeBucket = null;
    idx = -1;
  }});

  // ── 详情浮窗 ────────────────────────────────────────────
  const details = JSON.parse(document.getElementById('row-details').textContent);
  const modal = document.getElementById('detail-modal');
  const modalTitle = modal.querySelector('.modal-title');
  const modalMeta = modal.querySelector('.modal-meta');
  const modalBody = modal.querySelector('.modal-body');
  const colNames = ['模块', '用例名称', '描述', '预期', '备注'];

  let currentMode = 'col';
  let currentDetail = null;
  const EMPTY = '<span style="color:#bbb">—</span>';

  function cellHtml(cells, i){{
    if (!cells) return '<td class="empty-cell">（无）</td>';
    return '<td>' + (cells[i] || EMPTY) + '</td>';
  }}

  function renderCol(d){{
    // 列模式：字段在左侧标签列（窄），AI / 人工 各占剩余宽度的一半
    const rows = colNames.map(function(name, i){{
      return '<tr><th class="field-label">' + name + '</th>'
           + cellHtml(d.ai, i)
           + cellHtml(d.hu, i) + '</tr>';
    }}).join('');
    return '<table class="compare-table col-mode">'
         + '<colgroup>'
         + '<col style="width:96px">'
         + '<col><col>'
         + '</colgroup>'
         + '<thead><tr>'
         + '<th class="corner"></th>'
         + '<th class="ai-head">AI 原始</th>'
         + '<th class="hu-head">人工最终</th>'
         + '</tr></thead>'
         + '<tbody>' + rows + '</tbody></table>';
  }}

  function renderRow(d){{
    // 行模式：左侧 AI/人工 标签列 + 5 字段列
    // 模块/用例名称/备注 给固定窄宽，描述/预期 自动撑开
    const colWidths = [null, '96px', '128px', null, null, '96px']; // 0: 标签列
    const cols = '<colgroup>'
               + '<col style="width:108px">'
               + colWidths.slice(1).map(function(w){{
                   return w ? '<col style="width:' + w + '">' : '<col>';
                 }}).join('')
               + '</colgroup>';
    const heads = colNames.map(function(n){{
      return '<th class="col-head">' + n + '</th>';
    }}).join('');
    function buildRow(label, cls, cells){{
      const tds = colNames.map(function(_, i){{ return cellHtml(cells, i); }}).join('');
      return '<tr><th class="' + cls + '">' + label + '</th>' + tds + '</tr>';
    }}
    return '<table class="compare-table row-mode">'
         + cols
         + '<thead><tr>'
         + '<th class="corner"></th>' + heads
         + '</tr></thead>'
         + '<tbody>'
         + buildRow('AI 原始', 'ai-head', d.ai)
         + buildRow('人工最终', 'hu-head', d.hu)
         + '</tbody></table>';
  }}

  function renderBody(d){{
    modalBody.innerHTML = currentMode === 'row' ? renderRow(d) : renderCol(d);
  }}

  function openModal(idx){{
    const d = details[idx];
    if (!d) return;
    currentDetail = d;
    modalTitle.textContent = d.title || '用例对比';
    const statusLabel = {{
      matched: '完全一致', modified: '修改', deleted: '人工删除', added: '人工新增'
    }}[d.status] || d.status;
    modalMeta.textContent = (d.module ? d.module + ' · ' : '') + statusLabel;
    renderBody(d);
    modal.hidden = false;
    document.body.style.overflow = 'hidden';
  }}

  modal.querySelectorAll('.mode-toggle button').forEach(function(btn){{
    btn.addEventListener('click', function(){{
      const mode = btn.dataset.mode;
      if (mode === currentMode) return;
      currentMode = mode;
      modal.querySelectorAll('.mode-toggle button').forEach(function(b){{
        b.classList.toggle('active', b.dataset.mode === mode);
      }});
      if (currentDetail) renderBody(currentDetail);
    }});
  }});

  function closeModal(){{
    modal.hidden = true;
    document.body.style.overflow = '';
  }}

  document.querySelectorAll('.detail-btn').forEach(function(btn){{
    btn.addEventListener('click', function(e){{
      e.stopPropagation();
      openModal(parseInt(btn.dataset.idx, 10));
    }});
  }});
  modal.querySelector('.modal-close').addEventListener('click', closeModal);
  modal.querySelector('.modal-backdrop').addEventListener('click', closeModal);
  document.addEventListener('keydown', function(e){{
    if (e.key === 'Escape' && !modal.hidden) closeModal();
  }});
}})();
</script>
</body>
</html>"""


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="AI 测试用例质量评估")
    p.add_argument("ai_file", help="AI 生成的 HTML 文件")
    p.add_argument("human_file", help="人工修改后的 HTML 文件")
    p.add_argument(
        "-o", "--output", default="diff_report.html", help="输出报告 (默认: diff_report.html)"
    )
    p.add_argument(
        "--ai-color", default=None,
        help="手动指定 AI 文件的高亮色（如 #ea4335）。不指定时自动探测。"
    )
    p.add_argument(
        "--human-color", default=None,
        help="手动指定人工文件用于匹配的目标色。不指定时沿用 AI 探测出的色。"
    )
    args = p.parse_args()

    ai_rows, ai_detected, ai_target = parse_html_table(
        args.ai_file, args.ai_color, role="AI"
    )
    human_target = args.human_color or ai_target
    hu_rows, hu_detected, hu_target = parse_html_table(
        args.human_file, human_target, role="人工"
    )

    print(f"\n[颜色探测] AI 文件:   探测={ai_detected}  匹配={ai_target}")
    if hu_detected and ai_target:
        same_family = ColorMatcher(ai_target)(hu_detected)
        note = "与 AI 同色系" if same_family else "⚠ 与 AI 不同色系，可能漏匹配"
    else:
        note = "⚠ 未探测到高亮色"
    print(f"[颜色探测] 人工文件: 探测={hu_detected}  匹配={hu_target}  ({note})")

    generate_report(ai_rows, hu_rows, args.output)
