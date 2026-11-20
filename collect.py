#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC 五层决策看板 · 云端采集脚本（由 GitHub Actions 定时运行）

使用方法:
  双击本文件 (Windows/Mac 需已安装 Python 3.8+), 或在终端运行:  python BTC看板_一键启动.py
  首次运行会自动安装依赖 (requests / pandas / lxml), 然后自动打开浏览器。
  退出: 在运行窗口按 Ctrl+C 。

数据: 全部来自免费公开源 (Farside ETF流向 / FRED宏观 / DefiLlama稳定币 /
      Coinbase溢价 / Binance合约与现货 / 恐惧贪婪 / mempool算力), 无需 API Key, 无需手填。
      数据快照自动保存在本文件同目录的 snapshot.json 中。

仅供研究与数据观察, 不构成投资建议。加密资产波动极大, 杠杆可能造成超出本金的损失。
"""
import io
import os
import json
import subprocess
import sys
import threading
import time
import webbrowser
import datetime as dt
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path


def _ensure_deps():
    missing = []
    for m in ("requests", "pandas", "lxml"):
        try:
            __import__(m)
        except ImportError:
            missing.append(m)
    if missing:
        print("首次运行: 正在安装依赖", missing, "……")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *missing])


_ensure_deps()

import pandas as pd  # noqa: E402
import requests  # noqa: E402

ROOT = Path(__file__).resolve().parent
SNAP = ROOT / "snapshot.json"
REFRESH_S = 900          # 数据后台刷新间隔（秒）
PORT = 8000
UA = {"User-Agent": "Mozilla/5.0 (btc-dashboard/4.0)"}
LOCK = threading.Lock()



def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def get(url, params=None, text=False):
    r = requests.get(url, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    return r.text if text else r.json()


def day_of_ms(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def num(x):
    """解析 Farside 单元格: '(12.3)' 为负数, '-' 或空为缺失。"""
    s = str(x).strip().replace(",", "")
    if s in ("", "-", "nan", "None"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


# ---------------- 免费数据源 ----------------

def fetch_etf():
    html = get("https://farside.co.uk/btc/", text=True)
    tables = pd.read_html(io.StringIO(html))
    df = max(tables, key=len)
    df.columns = [str(c[-1]) if isinstance(c, tuple) else str(c) for c in df.columns]
    dates = pd.to_datetime(df.iloc[:, 0].astype(str), format="%d %b %Y", errors="coerce")
    total_cols = [c for c in df.columns if c.strip().lower() == "total"]
    if not total_cols:
        raise ValueError("Farside 表格中未找到 Total 列 (页面结构可能已变)")
    vals = df[total_cols[-1]].map(num)
    out = {}
    for d, v in zip(dates, vals):
        if pd.notna(d) and v is not None:
            out[d.strftime("%Y-%m-%d")] = v
    if not out:
        raise ValueError("未解析到任何日期行")
    return out


def fetch_stable():
    rows = get("https://stablecoins.llama.fi/stablecoincharts/all")
    return {day_of_ms(int(r["date"]) * 1000): float(r["totalCirculatingUSD"]["peggedUSD"])
            for r in rows}


def fred(series):
    txt = get("https://fred.stlouisfed.org/graph/fredgraph.csv", {"id": series}, text=True)
    d = pd.read_csv(io.StringIO(txt), na_values=["."])
    d.columns = ["date", "v"]
    d = d.dropna()
    return {r.date: float(r.v) for r in d.itertuples()}


def fetch_macro():
    y10 = fred("DGS10")
    walcl = fred("WALCL")      # 百万美元 (周频)
    tga = fred("WTREGEN")      # 百万美元 (周频)
    rrp = fred("RRPONTSYD")    # 十亿美元 (日频)
    liq = {}                   # 净流动性, 十亿美元
    for d in walcl:
        if d in tga and d in rrp:
            liq[d] = walcl[d] / 1000 - tga[d] / 1000 - rrp[d]
    ds = sorted(liq)
    if len(ds) < 6:
        raise ValueError("净流动性数据不足")
    last_d = ds[-1]
    target = (dt.date.fromisoformat(last_d) - dt.timedelta(days=28)).isoformat()
    prev_d = [d for d in ds if d <= target][-1]
    chg = liq[last_d] / liq[prev_d] - 1
    ys = sorted(y10)
    return {
        "net_liq_bn": round(liq[last_d], 1),
        "net_liq_date": last_d,
        "net_liq_chg_4w": round(chg, 4),
        "liq_state": 1 if chg > 0.01 else -1 if chg < -0.01 else 0,  # 启发式
        "dgs10": y10[ys[-1]],
        "dgs10_date": ys[-1],
    }


def fetch_premium():
    cb = float(get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")["price"])
    bn = float(get("https://api.binance.com/api/v3/ticker/price", {"symbol": "BTCUSDT"})["price"])
    return {"coinbase": cb, "binance_usdt": bn, "premium": round(cb / bn - 1, 5)}


def fetch_derivs():
    base = "https://fapi.binance.com/futures/data/"
    p = {"symbol": "BTCUSDT", "period": "1d", "limit": 500}

    def series(ep, field):
        return {day_of_ms(r["timestamp"]): float(r[field]) for r in get(base + ep, p)}

    out = {
        "global_ls_account": series("globalLongShortAccountRatio", "longShortRatio"),
        "top_ls_position": series("topLongShortPositionRatio", "longShortRatio"),
        "taker_buy_sell": series("takerlongshortRatio", "buySellRatio"),
        "oi_usd": series("openInterestHist", "sumOpenInterestValue"),
    }
    fund = {}
    for r in get("https://fapi.binance.com/fapi/v1/fundingRate",
                 {"symbol": "BTCUSDT", "limit": 1000}):
        fund.setdefault(day_of_ms(r["fundingTime"]), []).append(float(r["fundingRate"]))
    out["funding_daily_avg"] = {k: sum(v) / len(v) for k, v in fund.items()}
    return out


def fetch_trend():
    rows = get("https://api.binance.com/api/v3/klines",
               {"symbol": "BTCUSDT", "interval": "1d", "limit": 500})
    closes = pd.Series([float(r[4]) for r in rows[:-1]])  # 去掉未收盘的当日K线
    last = closes.iloc[-1]
    s200 = closes.rolling(200).mean().iloc[-1]
    s50 = closes.rolling(50).mean().iloc[-1]
    return {
        "close": round(float(last), 2),
        "sma50": round(float(s50), 2),
        "sma200": round(float(s200), 2),
        "above200": bool(last > s200),
        "golden": bool(s50 > s200),
        "dist200": round(float(last / s200 - 1), 4),
    }


# CFTC 官方免费接口 (publicreporting.cftc.gov, 无需密钥)
# 数据集: Traders in Financial Futures (金融期货), 芝商所比特币期货
CFTC_TFF = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"


def _pick(row, *names):
    """CFTC 字段名在不同数据集里有 _all 后缀差异, 依次尝试。"""
    for n in names:
        if n in row and row[n] not in (None, ""):
            return float(row[n])
    raise KeyError("CFTC 字段缺失: " + "/".join(names))


def fetch_cot():
    rows = get(CFTC_TFF, {
        "$where": "market_and_exchange_names like '%BITCOIN%' AND market_and_exchange_names like '%CHICAGO%'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": 60,
    })
    if not rows:
        raise ValueError("CFTC 未返回比特币期货数据 (数据集ID或字段可能已变)")
    rows = list(reversed(rows[:12]))  # 时间正序, 最近12周
    lf, days = [], []
    for r in rows:
        lf.append(int(_pick(r, "lev_money_positions_long", "lev_money_positions_long_all")
                      - _pick(r, "lev_money_positions_short", "lev_money_positions_short_all")))
        days.append(r["report_date_as_yyyy_mm_dd"][:10])
    last = rows[-1]
    am = int(_pick(last, "asset_mgr_positions_long", "asset_mgr_positions_long_all")
             - _pick(last, "asset_mgr_positions_short", "asset_mgr_positions_short_all"))
    return {"asof": days[-1], "days": days, "lf": lf, "am": am,
            "market": last.get("market_and_exchange_names", "")}


# Kote Charts 链上数据 (由自有比特币全节点计算, 免费; 数据接口需免费 API Key, 指标目录无需密钥)
# 署名要求: 非商业使用需注明 "Kote Charts"; 商业使用需其书面许可。
KOTE_CATALOG = "https://kotecharts.com/api/v1/metrics"
KOTE_CHART = "https://kotecharts.com/api/v1/public/charts/{}"


KOTE_CHARTS = "https://kotecharts.com/api/v1/public/charts/{}"
# 指标 ID 均取自 Kote 官方文档 (kotecharts.com/zh/docs#charts), 精确匹配, 不做模糊猜测
KOTE_IDS = {
    "netflow": "exchange-netflow",          # 交易所净流量 (BTC/日)
    "lth_profit": "supply-in-profit",       # 盈利中的供应量
    "mvrv_z": "mvrv-z-score",               # MVRV Z 分数
    "realized_price": "realized-price",     # 已实现价格
    "lth_sth_sopr": "sopr-ratio",           # LTH / STH SOPR 比率
    "nupl": "nupl",                         # NUPL
    "ssr": "ssr",                           # 稳定币供应比率
    "exchange_reserve": "exchange-reserve", # 交易所 BTC 储备
    "whale_acc": "whale-accumulation-score",# 巨鲸积累与派发得分
    "etf_flows": "etf-flows",               # 现货 ETF 资金流 (与 Farside 交叉验证)
}


def _to_day(t):
    if isinstance(t, str):
        return t[:10]
    t = float(t)
    if t > 1e11:  # 毫秒
        t /= 1000
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d")


def _series_points(payload):
    """解析 Kote 时间序列: 每点为 {"t": 日期, 数值字段...}, 返回 {日期: 数值}。"""
    d = payload.get("data", payload) if isinstance(payload, dict) else {}
    items = None
    if isinstance(d, dict):
        for k in ("series", "points", "items"):
            if isinstance(d.get(k), list):
                items = d[k]
                break
        if items is None:
            for v in d.values():
                if isinstance(v, list) and v and isinstance(v[0], dict) and "t" in v[0]:
                    items = v
                    break
    out = {}
    for p in items or []:
        if not (isinstance(p, dict) and "t" in p):
            continue
        nums = [k for k, v in p.items() if k != "t" and isinstance(v, (int, float))]
        if not nums:
            continue
        key = next((k for k in ("value", "v", "y") if k in nums), nums[0])
        out[_to_day(p["t"])] = float(p[key])
    return out


def fetch_onchain():
    key = os.environ.get("KOTE_API_KEY", "").strip()
    if not key:
        raise ValueError("未设置 KOTE_API_KEY（需在 GitHub 仓库 Secrets 中添加免费 Key）")
    hdr = {**UA, "X-API-Key": key}
    start = (dt.date.today() - dt.timedelta(days=365 * 3)).isoformat()

    def chart(cid):
        r = requests.get(KOTE_CHARTS.format(cid), params={"from": start, "granularity": "day"},
                         headers=hdr, timeout=30)
        r.raise_for_status()
        return _series_points(r.json())

    def safe_chart(cid):
        try:
            return chart(cid), None
        except Exception as e:
            return {}, str(e)[:80]

    netflow, _ = safe_chart(KOTE_IDS["netflow"])
    lth, err = safe_chart(KOTE_IDS["lth_profit"])
    if not netflow and not lth:
        raise ValueError("Kote 接口未返回数据（请检查 KOTE_API_KEY 与网络）")
    extra = {}
    for name, cid in KOTE_IDS.items():
        if name in ("netflow", "lth_profit"):
            continue
        pts, _ = safe_chart(cid)
        if pts:
            day = max(pts)
            extra[name] = {"id": cid, "value": round(pts[day], 4), "asof": day}
    return {"netflow": netflow, "lth": lth, "extra": extra,
            "attribution": "数据来源: Kote Charts (kotecharts.com)"}


# ---------- 新增观察点 (全部免费公开源) ----------
DERIBIT = "https://www.deribit.com/api/v2/public/"   # 比特币期权交易所, 公开接口无需密钥
EXTRA_FRED = {"dxy": "DTWEXBGS",      # 广义美元指数 (日频)
              "nasdaq": "NASDAQCOM",  # 纳斯达克综合指数 (日频)
              "real10y": "DFII10",    # 10年期实际利率 TIPS (日频)
              "hy_oas": "BAMLH0A0HYM2"}  # 高收益债信用利差 (日频)


def fetch_deribit():
    book = get(DERIBIT + "get_book_summary_by_currency",
               {"currency": "BTC", "kind": "option"})["result"]
    call_oi = sum(float(x.get("open_interest") or 0) for x in book if x["instrument_name"].endswith("-C"))
    put_oi = sum(float(x.get("open_interest") or 0) for x in book if x["instrument_name"].endswith("-P"))
    if call_oi <= 0:
        raise ValueError("Deribit 期权持仓为0 (接口返回异常)")
    end = int(time.time() * 1000)
    vol = get(DERIBIT + "get_volatility_index_data",
              {"currency": "BTC", "resolution": "3600",
               "start_timestamp": end - 7 * 24 * 3600 * 1000, "end_timestamp": end})["result"]["data"]
    last, first = vol[-1], vol[0]
    return {"put_call_oi": round(put_oi / call_oi, 4),
            "btc_options_oi": round(call_oi + put_oi, 1),
            "dvol": round(float(last[4]), 2),
            "dvol_chg_7d": round(float(last[4]) / float(first[4]) - 1, 4),
            "asof": day_of_ms(last[0])}


def fetch_extra():
    res = {}
    for key, sid in EXTRA_FRED.items():
        try:
            d = fred(sid)
            ds = sorted(d)
            if not ds:
                raise ValueError("无数据")
            res[key] = {"value": round(d[ds[-1]], 4), "asof": ds[-1]}
        except Exception as e:
            res[key] = {"value": None, "asof": None, "error": str(e)[:80]}
    return res


def build_view(out):
    """把所有数据源整理成统一观察表 (类别/指标/最新值/日期/状态), 页面直接渲染。"""
    src = out.get("sources", {})
    rows = []
    upd = (out.get("updated") or "")[:10] or None

    def last(d):
        if not isinstance(d, dict) or not d:
            return None, None
        k = max(d)
        return d[k], k

    def add(cat, name, val, asof, key):
        s = src.get(key, {})
        note, status = "", "ok"
        if val is None:
            status = "miss"
            note = (s.get("error") or "无数据")[:40]
        else:
            try:
                if asof and (dt.date.today() - dt.date.fromisoformat(str(asof)[:10])).days > 10:
                    status = "stale"
            except ValueError:
                pass
            if s and not s.get("ok", True):
                note = "沿用上次值"
        rows.append({"cat": cat, "name": name,
                     "value": (round(val, 6) if abs(val) < 0.01 else round(val, 4)) if isinstance(val, float) else val,
                     "asof": str(asof)[:10] if asof else None,
                     "status": status, "note": note})

    v, d = last(out.get("etf")); add("资金", "美国BTC现货ETF日净流入(美元m)", v, d, "etf")
    v, d = last(out.get("stable")); add("资金", "稳定币总市值(美元)", v, d, "stable")
    m = out.get("macro") or {}
    add("宏观", "10Y美债收益率(%)", m.get("dgs10"), m.get("dgs10_date"), "macro")
    add("宏观", "美联储净流动性(十亿美元)", m.get("net_liq_bn"), m.get("net_liq_date"), "macro")
    ex = out.get("extra") or {}
    for key, name, cat in [("dxy", "美元指数(广义)", "跨资产"), ("nasdaq", "纳斯达克综合指数", "跨资产"),
                           ("real10y", "10Y实际利率(TIPS,%)", "宏观"), ("hy_oas", "高收益债信用利差(%)", "宏观")]:
        e = ex.get(key) or {}
        add(cat, name, e.get("value"), e.get("asof"), "extra")
    p = out.get("premium") or {}
    add("现货", "Coinbase溢价(美国需求代理)", p.get("premium"), upd, "premium")
    dv = out.get("derivs") or {}
    v, d = last(dv.get("taker_buy_sell")); add("合约", "主动买卖比(Binance)", v, d, "derivs")
    v, d = last(dv.get("global_ls_account")); add("合约", "全球多空账户比", v, d, "derivs")
    v, d = last(dv.get("top_ls_position")); add("合约", "大户多空持仓比", v, d, "derivs")
    v, d = last(dv.get("funding_daily_avg")); add("合约", "资金费率(日均)", v, d, "derivs")
    dr = out.get("deribit") or {}
    add("期权", "Deribit BTC 看跌/看涨持仓比", dr.get("put_call_oi"), dr.get("asof"), "deribit")
    add("期权", "BTC 隐含波动率 DVOL", dr.get("dvol"), dr.get("asof"), "deribit")
    t = out.get("trend") or {}
    add("趋势", "BTC现价偏离200日均线", t.get("dist200"), upd, "trend")
    k = out.get("onchain") or {}
    v, d = last(k.get("netflow")); add("链上", "交易所净流入(BTC/日)", v, d, "onchain")
    v, d = last(k.get("lth")); add("链上", "盈利中的供应量(LTH口径,Kote)", v, d, "onchain")
    kx = k.get("extra") or {}
    for key, name, cat in [("mvrv_z", "MVRV Z分数", "估值周期"), ("realized_price", "已实现价格(美元)", "估值周期"),
                           ("nupl", "NUPL", "估值周期"), ("lth_sth_sopr", "LTH/STH SOPR比率", "估值周期"),
                           ("ssr", "稳定币供应比率(SSR)", "资金"), ("exchange_reserve", "交易所BTC储备", "链上"),
                           ("whale_acc", "巨鲸积累/派发得分", "链上"), ("etf_flows", "ETF资金流(Kote,交叉验证)", "资金")]:
        e = kx.get(key) or {}
        add(cat, name, e.get("value"), e.get("asof"), "onchain")
    cot = out.get("cot") or {}
    add("持仓", "CFTC杠杆资金净持仓(BTC期货)", (cot.get("lf") or [None])[-1], cot.get("asof"), "cot")
    return rows


FETCHERS = {
    "etf": fetch_etf,
    "stable": fetch_stable,
    "macro": fetch_macro,
    "premium": fetch_premium,
    "derivs": fetch_derivs,
    "trend": fetch_trend,
    "cot": fetch_cot,
    "onchain": fetch_onchain,
    "deribit": fetch_deribit,
    "extra": fetch_extra,
}


# ---------------- 快照与后台刷新 ----------------

def read_snap():
    try:
        return json.loads(SNAP.read_text(encoding="utf-8"))
    except Exception:
        return {}


def collect():
    prev = read_snap()
    out = {"updated": now_iso(), "sources": {},
           "notes": {"exchange_netflow": "无可靠免费数据源, 已停用手填, 待接入后自动更新"}}
    for k, fn in FETCHERS.items():
        try:
            out[k] = fn()
            out["sources"][k] = {"ok": True, "at": now_iso()}
        except Exception as e:
            old = prev.get(k)
            out[k] = old if old else None
            out["sources"][k] = {"ok": False, "at": now_iso(), "error": str(e)[:160],
                                 "fallback": "上次成功值" if old else "无可用数据"}
    try:
        out["view"] = build_view(out)
    except Exception as e:
        out["view"] = []
        print("view error:", e)
    with LOCK:
        SNAP.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def background():
    while True:
        try:
            collect()
            print(time.strftime("%H:%M:%S"), "数据已更新")
        except Exception as e:
            print("数据更新失败:", e)
        time.sleep(REFRESH_S)


if __name__ == "__main__":
    out = collect()
    print("updated", out["updated"], {k: v["ok"] for k, v in out["sources"].items()})
