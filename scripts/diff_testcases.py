#!/usr/bin/env python3
"""
AI 测试用例质量评估脚本
对比 AI 生成的 HTML 测试用例表格与人工修改后的版本，
计算人工修改率并生成可视化 Diff 报告。

Usage:
    python diff_testcases.py <AI生成.html> <人工修改.html> [-o 输出报告.html]
"""

import argparse
import difflib
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

        parsed.append(
            {
                "module": cur_mod,
                "name": cells[1]["full_text"].strip(),
                "key": (cur_mod, cells[1]["full_text"].strip()),
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


# 模糊匹配阈值：名称+描述+预期 加权相似度 >= 该值才视为同一行
FUZZY_MATCH_THRESHOLD = 0.5


def _row_similarity(a_row, b_row):
    """计算两行的内容相似度，加权综合 用例名/描述/预期 三列。

    模块列不参与匹配（人工常给模块改名/加前缀），其它三列内容才是行的真正"身份"。
    """
    def ratio(a, b):
        if not a and not b:
            return 1.0
        if not a or not b:
            return 0.0
        return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()

    name = ratio(a_row["cells"][1]["full_text"], b_row["cells"][1]["full_text"])
    desc = ratio(a_row["cells"][2]["full_text"], b_row["cells"][2]["full_text"])
    expt = ratio(a_row["cells"][3]["full_text"], b_row["cells"][3]["full_text"])
    return name * 0.3 + desc * 0.4 + expt * 0.3


def _match_rows(ai_rows, hu_rows):
    """两阶段匹配：先 (模块, 用例名) 精确匹配，剩余用内容相似度贪心匹配。

    返回 pairs: list of (ai_idx | None, hu_idx | None)，按 AI 行顺序，
    末尾追加未匹配上的人工新增行。
    """
    n_ai, n_hu = len(ai_rows), len(hu_rows)
    ai_to_hu = [None] * n_ai
    hu_used = [False] * n_hu

    # Pass 1: 精确 key 匹配
    hu_by_key = {}
    for j, r in enumerate(hu_rows):
        hu_by_key.setdefault(r["key"], []).append(j)
    for i, ai in enumerate(ai_rows):
        bucket = hu_by_key.get(ai["key"], [])
        while bucket:
            j = bucket.pop(0)
            if not hu_used[j]:
                ai_to_hu[i] = j
                hu_used[j] = True
                break

    # Pass 2: 剩余行模糊匹配 — 计算所有候选对的相似度，按分数降序贪心
    unmatched_ai = [i for i in range(n_ai) if ai_to_hu[i] is None]
    unmatched_hu = [j for j in range(n_hu) if not hu_used[j]]

    candidates = []
    for i in unmatched_ai:
        for j in unmatched_hu:
            s = _row_similarity(ai_rows[i], hu_rows[j])
            if s >= FUZZY_MATCH_THRESHOLD:
                candidates.append((s, i, j))
    candidates.sort(reverse=True)

    used_ai = set()
    used_hu = set()
    for s, i, j in candidates:
        if i in used_ai or j in used_hu:
            continue
        ai_to_hu[i] = j
        used_ai.add(i)
        used_hu.add(j)
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


def _render_diff(ai_text, hu_text, cmap=None, cpos=0):
    """行级 diff。cmap 非空时为 equal 块保留原始颜色。"""
    if ai_text == hu_text:
        if cmap:
            return _colorize(ai_text, cmap, cpos)
        return f'<span class="red-text">{_h(ai_text)}</span>' if ai_text else ""

    ai_lines = ai_text.split("\n")
    hu_lines = hu_text.split("\n")

    line_starts = [0]
    for line in ai_lines[:-1]:
        line_starts.append(line_starts[-1] + len(line) + 1)

    sm = difflib.SequenceMatcher(None, ai_lines, hu_lines, autojunk=False)
    parts = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        pos = cpos + (line_starts[i1] if i1 < len(line_starts) else 0)
        ai_chunk = chr(10).join(ai_lines[i1:i2])
        hu_chunk = chr(10).join(hu_lines[j1:j2])
        if op == "equal":
            if cmap:
                parts.append(_colorize(ai_chunk, cmap, pos))
            else:
                parts.append(f'<span class="red-text">{_h(ai_chunk)}</span>')
        elif op == "delete":
            parts.append(f'<span class="deleted">{_h(ai_chunk)}</span>')
        elif op == "insert":
            parts.append(f'<span class="added">【{_h(hu_chunk)}】</span>')
        elif op == "replace":
            parts.append(_render_line_replace(ai_lines[i1:i2], hu_lines[j1:j2], cmap, pos))
    return "".join(parts)


def _render_line_replace(old_lines, new_lines, cmap=None, cpos=0):
    """对替换块逐行配对做字符级 diff。"""
    parts = []
    pos = cpos
    for i in range(max(len(old_lines), len(new_lines))):
        if i > 0:
            parts.append("<br>")
        ol = old_lines[i] if i < len(old_lines) else None
        nl = new_lines[i] if i < len(new_lines) else None
        if ol is None:
            parts.append(f'<span class="added">【{_h(nl)}】</span>')
        elif nl is None:
            parts.append(f'<span class="deleted">{_h(ol)}</span>')
        else:
            parts.append(_char_diff(ol, nl, cmap, pos))
        if ol is not None:
            pos += len(ol) + 1
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


def _char_diff(old, new, cmap=None, cpos=0):
    """单行字符级 diff。共同前缀 >= 3 时即使整体相似度低也做细粒度 diff。"""
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    prefix = 0
    for a, b in zip(old, new):
        if a == b:
            prefix += 1
        else:
            break
    if sm.ratio() < 0.3 and prefix < 3:
        return (
            f'<span class="deleted">{_h(old)}</span>'
            f'<span class="modified">【{_h(new)}】</span>'
        )
    ops = _clean_opcodes(sm.get_opcodes())
    parts = []
    for op, i1, i2, j1, j2 in ops:
        if op == "equal":
            chunk = old[i1:i2]
            if cmap:
                parts.append(_colorize(chunk, cmap, cpos + i1))
            else:
                parts.append(f'<span class="red-text">{_h(chunk)}</span>')
        elif op == "delete":
            parts.append(f'<span class="deleted">{_h(old[i1:i2])}</span>')
        elif op == "insert":
            parts.append(f'<span class="added">【{_h(new[j1:j2])}】</span>')
        elif op == "replace":
            parts.append(f'<span class="deleted">{_h(old[i1:i2])}</span>')
            parts.append(f'<span class="modified">【{_h(new[j1:j2])}】</span>')
    return "".join(parts)


def _render_cell(ai_cell, hu_cell, status):
    """渲染单个单元格的 HTML。"""
    if status == "deleted":
        return f'<span class="deleted">{_h(ai_cell["full_text"])}</span>'
    if status == "added":
        return f'<span class="added">【{_h(hu_cell["full_text"])}】</span>'

    # matched — 全文 diff，保留红/黑颜色
    ai_full = ai_cell["full_text"]
    hu_full = hu_cell["full_text"] if hu_cell else ""
    cmap = _build_color_map(ai_cell["segments"])

    if ai_full == hu_full:
        return _colorize(ai_full, cmap, 0)

    return _render_diff(ai_full, hu_full, cmap, 0)


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
            if d or a:
                n_modified += 1
                results.append((ai, hm, "modified"))
            else:
                n_matched += 1
                results.append((ai, hm, "matched"))
        elif ai_idx is not None:
            ai = ai_rows[ai_idx]
            total_del += sum(len(c["red_text"]) for c in ai["cells"])
            n_deleted += 1
            results.append((ai, None, "deleted"))
        else:
            hm = hu_rows[hu_idx]
            total_add += sum(len(c["red_text"]) for c in hm["cells"])
            n_added += 1
            results.append((None, hm, "added"))

    rate = (total_del + total_add) / ai_red_total * 100 if ai_red_total else 0

    Path(output).write_text(
        _build_html(results, ai_red_total, total_del, total_add, rate), encoding="utf-8"
    )

    print(f"\n{'='*45}")
    print(f"  AI 测试用例质量评估报告")
    print(f"{'='*45}")
    print(f"  AI 生成红色文本:  {ai_red_total} 字符")
    print(f"  人工删除:         {total_del} 字符")
    print(f"  人工新增:         {total_add} 字符")
    print(f"  人工修改率:       {rate:.1f}%")
    print(f"  ─" * 22)
    print(f"  行匹配: 完全一致 {n_matched} / 修改 {n_modified} / 删除 {n_deleted} / 新增 {n_added}")
    if rate > 90:
        print(f"  ⚠ 修改率偏高，可能存在未匹配上的同义行；请人工核对。")
    print(f"{'='*45}")
    print(f"  报告已生成: {output}")


def _build_html(results, ai_red_total, total_del, total_add, rate):
    hdrs = ["模块", "用例名称", "描述", "预期", "备注"]
    hdr_html = "".join(f'<th class="hdr">{h}</th>' for h in hdrs)

    rows = []
    for ai, hm, st in results:
        if st == "deleted":
            cls = ' class="row-del"'
            cs = "".join(
                f"<td>{_render_cell(ai['cells'][i], None, 'deleted')}</td>"
                for i in range(5)
            )
        elif st == "added":
            cls = ' class="row-add"'
            cs = "".join(
                f"<td>{_render_cell(None, hm['cells'][i], 'added')}</td>"
                for i in range(5)
            )
        else:
            cls = ""
            cs = "".join(
                f"<td>{_render_cell(ai['cells'][i], hm['cells'][i], 'matched')}</td>"
                for i in range(5)
            )
        rows.append(f"<tr{cls}>{cs}</tr>")

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
.added{{color:#0d7a0d;font-weight:bold;background:#f0fff0;
        padding:1px 2px;border-radius:2px}}
.modified{{color:#1155cc;font-weight:bold;background:#f0f4ff;
           padding:1px 2px;border-radius:2px}}
.row-del{{background:#fafafa}}
.row-del td{{color:#999}}
.row-add{{background:#f0fff0}}
</style>
</head>
<body>
<h1>AI 测试用例质量评估报告</h1>

<div class="cards">
  <div class="card rate">
    <div class="lbl">人工修改率</div>
    <div class="val">{rate:.1f}%</div>
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
  人工修改率 = ( 人工删除 + 人工新增 ) / AI 生成红色文本
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
