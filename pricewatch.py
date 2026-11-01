#!/usr/bin/env python3
"""
小雲 10P+ 比價網站產生器

讀取 sources.json 裡的商店頁面,抓價格、換算成新台幣,
把結果寫成 docs/index.html(最低價黃色標示)、docs/prices.json,
並把每次結果累積到 docs/history.csv。

由 GitHub Actions 每小時執行一次,docs/ 資料夾以 GitHub Pages 發布。

本機測試:
    pip install -r requirements.txt
    python pricewatch.py
"""

import csv
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
CONFIG_PATH = BASE / "sources.json"
OUT_DIR = BASE / "docs"
HTML_PATH = OUT_DIR / "index.html"
JSON_PATH = OUT_DIR / "prices.json"
CSV_PATH = OUT_DIR / "history.csv"

TZ = ZoneInfo("Asia/Taipei")
REQUEST_TIMEOUT = 20
HISTORY_POINTS = 72  # 圖表最多顯示最近幾次
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}


# ----------------------------------------------------------------------
# 價格解析
# ----------------------------------------------------------------------
def to_number(text):
    """'HK$ 1,580' -> 1580.0;無法解析則回傳 None"""
    if text is None:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(text))
    if not m:
        return None
    try:
        value = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def price_from_json_ld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                offers = node.get("offers")
                if offers:
                    stack.append(offers)
                for key in ("price", "lowPrice"):
                    if key in node and to_number(node[key]):
                        return to_number(node[key])
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return None


def price_from_meta(soup):
    for attrs in (
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
        {"itemprop": "price"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and to_number(tag.get("content")):
            return to_number(tag["content"])
    return None


PRICE_IN_TEXT = re.compile(r"(?:NT\$|NTD|\$|＄)\s*([\d,]{3,}(?:\.\d+)?)")


def extract_price(page_html, source):
    """
    依序收集候選價格:CSS selector -> regex -> JSON-LD -> meta -> (scan_text)網頁文字中的 $ 金額。
    若設定了 min_price / max_price,只接受落在範圍內的第一個價格,
    這樣不會把運費門檻、配件價格誤當成商品價格。
    """
    soup = BeautifulSoup(page_html, "html.parser")
    lo = source.get("min_price")
    hi = source.get("max_price")

    def ok(v):
        return v is not None and (lo is None or v >= lo) and (hi is None or v <= hi)

    text = soup.get_text(" ", strip=True)
    candidates = []

    if source.get("selector"):
        for el in soup.select(source["selector"]):
            candidates.append(to_number(el.get("content") or el.get_text(" ", strip=True)))

    if source.get("regex"):
        for m in re.finditer(source["regex"], text):
            candidates.append(to_number(m.group(1)))

    candidates.append(price_from_json_ld(soup))
    candidates.append(price_from_meta(soup))

    if source.get("scan_text"):
        for m in PRICE_IN_TEXT.finditer(text):
            candidates.append(to_number(m.group(1)))

    for v in candidates:
        if ok(v):
            return v
    return None


def fetch_one(source, rates):
    result = {
        "name": source["name"],
        "url": source["url"],
        "currency": source["currency"],
        "price": None,
        "price_twd": None,
        "error": None,
    }
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or resp.encoding
        price = extract_price(resp.text, source)
        if price is None:
            result["error"] = "找不到合理範圍內的價格(頁面可能需要 JavaScript 或擋掉機器人)"
        else:
            rate = rates.get(source["currency"])
            if rate is None:
                result["error"] = f"缺少 {source['currency']} 匯率"
            else:
                result["price"] = price
                result["price_twd"] = round(price * rate)
    except requests.RequestException as exc:
        result["error"] = f"連線失敗:{exc.__class__.__name__}"
    return result


def mark_lowest(results):
    valid = [r["price_twd"] for r in results if r["price_twd"] is not None]
    lowest = min(valid, default=None)
    for r in results:
        r["is_lowest"] = lowest is not None and r["price_twd"] == lowest
    return lowest


# ----------------------------------------------------------------------
# 歷史紀錄
# ----------------------------------------------------------------------
CSV_HEADER = ["時間", "商店", "幣別", "原價", "約合TWD", "是否最低", "錯誤"]


def append_history(results, stamp):
    OUT_DIR.mkdir(exist_ok=True)
    new_file = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(CSV_HEADER)
        for r in results:
            w.writerow(
                [
                    stamp,
                    r["name"],
                    r["currency"],
                    r["price"] if r["price"] is not None else "",
                    r["price_twd"] if r["price_twd"] is not None else "",
                    "Y" if r["is_lowest"] else "",
                    r["error"] or "",
                ]
            )


def load_lowest_history():
    """回傳 [(時間, 當次最低TWD), ...],給趨勢圖用"""
    if not CSV_PATH.exists():
        return []
    per_run = {}
    with CSV_PATH.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            twd = to_number(row.get("約合TWD"))
            if twd is None:
                continue
            t = row["時間"]
            per_run[t] = min(per_run.get(t, twd), twd)
    return sorted(per_run.items())[-HISTORY_POINTS:]


def trend_svg(points):
    if len(points) < 2:
        return "<p class='note'>累積兩次以上的紀錄後,這裡會出現最低價趨勢圖。</p>"
    w, h, pad = 640, 160, 28
    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    coords = []
    for i, (_, v) in enumerate(points):
        x = pad + i * (w - 2 * pad) / (len(points) - 1)
        y = h - pad - (v - lo) * (h - 2 * pad) / span
        coords.append((x, y))
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    last_x, last_y = coords[-1]
    return (
        f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='最低價趨勢圖' "
        f"style='width:100%;max-width:{w}px'>"
        f"<polyline points='{poly}' fill='none' stroke='#555' stroke-width='2'/>"
        f"<circle cx='{last_x:.1f}' cy='{last_y:.1f}' r='5' fill='#ffeb3b' stroke='#555' stroke-width='2'/>"
        f"<text x='{pad}' y='14' font-size='12' fill='#666'>最高 NT$ {hi:,.0f}</text>"
        f"<text x='{pad}' y='{h - 8}' font-size='12' fill='#666'>最低 NT$ {lo:,.0f}</text>"
        f"</svg>"
    )


# ----------------------------------------------------------------------
# 輸出網頁
# ----------------------------------------------------------------------
def write_site(results, stamp, config, lowest):
    OUT_DIR.mkdir(exist_ok=True)
    title = config.get("title", "小雲 10P+ 比價")

    ordered = sorted(results, key=lambda x: (x["price_twd"] is None, x["price_twd"] or 0))
    rows = []
    for r in ordered:
        cls = " class='lowest'" if r["is_lowest"] else ""
        name_link = (
            f"<a href='{html.escape(r['url'])}' target='_blank' rel='noopener'>"
            f"{html.escape(r['name'])}</a>"
        )
        if r["price_twd"] is None:
            body = f"<td colspan='3' class='err'>取價失敗:{html.escape(r['error'] or '')}</td>"
        else:
            body = (
                f"<td>{html.escape(r['currency'])} {r['price']:,.0f}</td>"
                f"<td>NT$ {r['price_twd']:,}</td>"
                f"<td>{'最低價' if r['is_lowest'] else ''}</td>"
            )
        rows.append(f"<tr{cls}><td>{name_link}</td>{body}</tr>")

    headline = (
        f"目前最低價:<strong>NT$ {lowest:,}</strong>" if lowest is not None else "目前沒有取得任何價格"
    )

    page = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="1800">
<title>{html.escape(title)}</title>
<style>
 body{{font-family:system-ui,"Noto Sans TC","PingFang TC",sans-serif;margin:0;color:#222;background:#fafafa}}
 main{{max-width:760px;margin:0 auto;padding:24px 16px}}
 h1{{font-size:22px;margin:0 0 4px}}
 .sub{{color:#666;font-size:14px;margin:0 0 20px}}
 table{{border-collapse:collapse;width:100%;background:#fff}}
 th,td{{border:1px solid #ddd;padding:10px 12px;text-align:left;font-size:15px}}
 th{{background:#f1f1f1}}
 tr.lowest{{background:#ffeb3b;font-weight:600}}
 .err{{color:#b00020}}
 .note{{color:#666;font-size:13px;margin-top:14px}}
 h2{{font-size:16px;margin:28px 0 8px}}
 a{{color:#0b57d0}}
 @media(max-width:520px){{th,td{{padding:8px;font-size:14px}}}}
</style></head><body><main>
<h1>{html.escape(title)}</h1>
<p class="sub">{headline} · 最後更新 {stamp}(台灣時間)</p>
<table>
<tr><th>商店</th><th>原價</th><th>約合新台幣</th><th></th></tr>
{''.join(rows)}
</table>
<h2>最低價趨勢</h2>
{trend_svg(load_lowest_history())}
<p class="note">黃色列為目前最低價。非新台幣價格依固定匯率換算,僅供參考,實際以商店結帳頁為準。
資料每小時自動更新一次,<a href="history.csv">下載歷史紀錄</a>。</p>
</main></body></html>"""
    HTML_PATH.write_text(page, encoding="utf-8")

    JSON_PATH.write_text(
        json.dumps(
            {"updated": stamp, "lowest_twd": lowest, "results": results},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ----------------------------------------------------------------------
def main():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    sources = config.get("sources", [])
    rates = config.get("rates_to_twd", {"TWD": 1.0})
    if not sources:
        sys.exit("sources.json 的 sources 是空的,請先加入商店。")

    stamp = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    results = [fetch_one(s, rates) for s in sources]
    lowest = mark_lowest(results)

    append_history(results, stamp)
    write_site(results, stamp, config, lowest)

    for r in results:
        if r["price_twd"] is None:
            print(f"[失敗] {r['name']}: {r['error']}")
        else:
            flag = "  <-- 最低價" if r["is_lowest"] else ""
            print(f"[成功] {r['name']}: {r['currency']} {r['price']:,.0f} ≈ NT$ {r['price_twd']:,}{flag}")


if __name__ == "__main__":
    main()
