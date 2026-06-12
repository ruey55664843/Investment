import os
import math
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import yfinance as yf


# =========================
# User settings
# =========================

TZ = ZoneInfo("Asia/Taipei")
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

TWD_CASH = int(os.environ.get("TWD_CASH", "1000000"))
USD_CASH = float(os.environ.get("USD_CASH", "42245"))

# 美股目前部位：用「市值或成本」皆可，但建議之後定期改成最新市值
US_CURRENT_VALUE = {
    "NVDA": 34828,
    "TSM": 23026,
    "AVGO": 20986,
    "MSFT": 12801,
    "GOOGL": 12875,
    "META": 2249,
}

US_TARGETS = {
    "NVDA": 0.25,
    "TSM": 0.20,
    "AVGO": 0.20,
    "MSFT": 0.175,
    "GOOGL": 0.175,
}

US_WATCHLIST = ["META", "AMZN"]

TW_STOCKS = {
    "0050": "元大台灣50",
    "6005": "群益證",
}

LOOKBACK_DAYS = 14
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"


# =========================
# Utility
# =========================

def now_tw():
    return datetime.now(TZ)


def fmt_money(value, currency=""):
    if value is None:
        return "N/A"
    if currency:
        return f"{value:,.0f} {currency}"
    return f"{value:,.0f}"


def safe_float(x):
    try:
        if x is None:
            return None
        if isinstance(x, float) and math.isnan(x):
            return None
        return float(x)
    except Exception:
        return None


def latest_row(rows, date_key="date"):
    if not rows:
        return None
    return sorted(rows, key=lambda x: x.get(date_key, ""))[-1]


def data_age_days(date_str):
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        return (now_tw().date() - d).days
    except Exception:
        return None


def freshness_label(date_str):
    age = data_age_days(date_str)
    if age is None:
        return "資料缺漏，訊號降級"
    if age <= 1:
        return "資料新鮮"
    if age <= 3:
        return "可能遇到週末/假日，訊號小幅降級"
    return "資料超過一個交易日，訊號降級"


# =========================
# FinMind / Taiwan
# =========================

def finmind_fetch(dataset, stock_id=None, start_date=None, end_date=None):
    if not FINMIND_TOKEN:
        raise RuntimeError("Missing FINMIND_TOKEN. Please set GitHub Secret: FINMIND_TOKEN")

    params = {
        "dataset": dataset,
        "start_date": start_date,
        "end_date": end_date,
        "token": FINMIND_TOKEN,
    }
    if stock_id:
        params["data_id"] = stock_id

    r = requests.get(FINMIND_URL, params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"{dataset} HTTP {r.status_code}: {r.text[:500]}")

    payload = r.json()
    if payload.get("status") != 200:
        raise RuntimeError(f"{dataset} API error: {payload}")

    return payload.get("data", [])


def get_inst_summary(rows):
    if not rows:
        return None, {}

    latest_date = sorted(set(x.get("date") for x in rows if x.get("date")))[-1]
    latest = [x for x in rows if x.get("date") == latest_date]

    result = {}
    for x in latest:
        name = x.get("name")
        buy = x.get("buy", 0) or 0
        sell = x.get("sell", 0) or 0
        result[name] = buy - sell

    return latest_date, result


def margin_change(row):
    if not row:
        return None
    buy = row.get("MarginPurchaseBuy", 0) or 0
    sell = row.get("MarginPurchaseSell", 0) or 0
    repay = row.get("MarginPurchaseCashRepayment", 0) or 0
    return buy - sell - repay


def fetch_tw_data():
    today = now_tw().date()
    start = today - timedelta(days=LOOKBACK_DAYS)
    start_date = start.strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    market_margin = latest_row(
        finmind_fetch(
            "TaiwanStockTotalMarginPurchaseShortSale",
            start_date=start_date,
            end_date=end_date,
        )
    )
    market_margin_delta = None
    if market_margin:
        market_margin_delta = (market_margin.get("TodayBalance", 0) or 0) - (market_margin.get("YesBalance", 0) or 0)

    results = {}
    for sid, name in TW_STOCKS.items():
        price = latest_row(finmind_fetch("TaiwanStockPrice", sid, start_date, end_date))
        inst_date, inst = get_inst_summary(
            finmind_fetch("TaiwanStockInstitutionalInvestorsBuySell", sid, start_date, end_date)
        )
        margin = latest_row(finmind_fetch("TaiwanStockMarginPurchaseShortSale", sid, start_date, end_date))
        results[sid] = {
            "name": name,
            "price": price,
            "inst_date": inst_date,
            "inst": inst,
            "margin": margin,
            "margin_delta": margin_change(margin),
        }

    return {
        "start_date": start_date,
        "end_date": end_date,
        "market_margin": market_margin,
        "market_margin_delta": market_margin_delta,
        "stocks": results,
    }


def tw_signal_0050(row, market_margin_delta):
    price = row.get("price") or {}
    inst = row.get("inst") or {}
    margin_delta = row.get("margin_delta")

    close = safe_float(price.get("close"))
    spread = safe_float(price.get("spread"))
    foreign = inst.get("Foreign_Investor", 0) or 0

    score = 0
    notes = []

    if close is not None and close <= 97:
        score += 2
        notes.append("價格進入預設加碼區")
    if spread is not None and spread <= -2:
        score += 1
        notes.append("單日跌幅達低接觀察區")
    if foreign > 0:
        score += 1
        notes.append("外資轉買")
    else:
        notes.append("外資未轉買")
    if margin_delta is not None and margin_delta < 0:
        score += 1
        notes.append("融資下降")
    elif margin_delta is not None:
        notes.append("融資增加，降低信號")
    if market_margin_delta is not None and market_margin_delta < 0:
        score += 1
        notes.append("大盤融資下降")
    elif market_margin_delta is not None:
        notes.append("大盤融資增加，降低信號")

    if score >= 4:
        return "🟢 加碼", 100000, notes
    if score >= 2:
        return "🟡 小額加碼", 50000, notes
    return "⚪ 觀察", 0, notes


def tw_signal_6005(row, tw_individual_cash_cap=30000):
    inst = row.get("inst") or {}
    margin_delta = row.get("margin_delta")

    foreign = inst.get("Foreign_Investor", 0) or 0
    trust = inst.get("Investment_Trust", 0) or 0

    score = 0
    notes = []

    if foreign > 0:
        score += 1
        notes.append("外資買超")
    else:
        notes.append("外資未買超")
    if margin_delta is not None and margin_delta < 0:
        score += 1
        notes.append("融資下降")
    elif margin_delta is not None:
        notes.append("融資增加")
    if trust < 0:
        notes.append("投信賣超，需保守")

    if score >= 2:
        return "🟡 小額加碼", min(tw_individual_cash_cap, 20000), notes
    return "⚪ 觀察", 0, notes


# =========================
# US market
# =========================

def yf_history(ticker, period="7d"):
    obj = yf.Ticker(ticker)
    hist = obj.history(period=period, auto_adjust=False)
    if hist is None or hist.empty:
        return None
    return hist


def fetch_us_data():
    tickers = list(dict.fromkeys(list(US_TARGETS.keys()) + US_WATCHLIST + ["QQQ", "^IXIC"]))
    results = {}

    for t in tickers:
        try:
            hist = yf_history(t, "10d")
            if hist is None:
                results[t] = {"error": "no data"}
                continue

            last = hist.iloc[-1]
            prev = hist.iloc[-2] if len(hist) >= 2 else None

            close = safe_float(last.get("Close"))
            prev_close = safe_float(prev.get("Close")) if prev is not None else None
            pct = None
            if close is not None and prev_close not in (None, 0):
                pct = (close / prev_close - 1) * 100

            last_date = hist.index[-1].strftime("%Y-%m-%d")
            results[t] = {
                "date": last_date,
                "close": close,
                "prev_close": prev_close,
                "pct_change": pct,
                "freshness": freshness_label(last_date),
            }
        except Exception as e:
            results[t] = {"error": str(e)}

    return results


def us_allocation():
    invested = sum(US_CURRENT_VALUE.values())
    total = invested + USD_CASH

    rows = {}
    for t, current in US_CURRENT_VALUE.items():
        target = US_TARGETS.get(t)
        weight = current / total if total else 0
        target_value = target * total if target is not None else None
        gap = target_value - current if target_value is not None else None
        rows[t] = {
            "current": current,
            "weight": weight,
            "target": target,
            "target_value": target_value,
            "gap": gap,
        }
    return total, rows


def us_buy_plan(us_prices):
    total, alloc = us_allocation()
    qqq = us_prices.get("QQQ", {})
    qqq_pct = qqq.get("pct_change")
    qqq_fresh = qqq.get("freshness", "資料缺漏")

    remaining = USD_CASH
    suggestions = []

    candidates = []
    for t in US_TARGETS:
        row = alloc[t]
        price_info = us_prices.get(t, {})
        pct = price_info.get("pct_change")
        gap = row["gap"] or 0

        score = 0
        notes = []

        if gap > 0:
            score += min(3, gap / 3000)
            notes.append("低於目標配置")
        else:
            notes.append("已高於或接近目標配置")

        if pct is not None and pct <= -2:
            score += 1
            notes.append("單日回檔")
        elif pct is not None and pct >= 2:
            score -= 1
            notes.append("單日大漲，不追價")

        if qqq_pct is not None and qqq_pct <= -1.5:
            score += 0.5
            notes.append("Nasdaq/QQQ 回檔提高低接分數")
        elif qqq_pct is not None and qqq_pct >= 1.5:
            score -= 0.5
            notes.append("Nasdaq/QQQ 強彈，降低追價")

        if "超過" in qqq_fresh or "缺漏" in qqq_fresh:
            score -= 1
            notes.append("美股資料新鮮度不足，降低信號")

        candidates.append((score, t, gap, notes))

    candidates.sort(reverse=True, key=lambda x: x[0])

    for score, t, gap, notes in candidates:
        if remaining <= 0:
            buy = 0
        elif score >= 2 and gap > 0:
            buy = min(5000, max(1000, round(gap / 2 / 500) * 500), remaining)
        elif score >= 1 and gap > 0:
            buy = min(2000, max(0, round(gap / 3 / 500) * 500), remaining)
        else:
            buy = 0

        remaining -= buy
        suggestions.append({
            "ticker": t,
            "score": score,
            "gap": gap,
            "buy": buy,
            "notes": notes,
        })

    return suggestions, remaining, alloc


# =========================
# Report
# =========================

def build_report(tw, us_prices):
    generated = now_tw().strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = []

    lines.append("# Ray Daily Investment Report")
    lines.append("")
    lines.append(f"- 產生時間: {generated}")
    lines.append(f"- 台股資料區間: {tw['start_date']} ~ {tw['end_date']}")
    lines.append(f"- 台幣現金: {fmt_money(TWD_CASH, 'TWD')}")
    lines.append(f"- 美元現金: {fmt_money(USD_CASH, 'USD')}")
    lines.append("")

    # TW operations
    stocks = tw["stocks"]
    s0050, buy0050, n0050 = tw_signal_0050(stocks["0050"], tw["market_margin_delta"])
    s6005, buy6005, n6005 = tw_signal_6005(stocks["6005"])

    total_tw_buy = min(TWD_CASH, buy0050 + buy6005)
    tw_remaining = TWD_CASH - total_tw_buy

    lines.append("## 今日操作與風險監控")
    lines.append("")
    lines.append("### 台股")
    lines.append(f"- 0050: {s0050}，建議買入 {fmt_money(buy0050, 'TWD')}")
    lines.append(f"  - 原因: {'、'.join(n0050)}")
    lines.append(f"- 6005 群益證: {s6005}，建議買入 {fmt_money(buy6005, 'TWD')}")
    lines.append(f"  - 原因: {'、'.join(n6005)}")
    lines.append(f"- 台股投入後剩餘現金: {fmt_money(tw_remaining, 'TWD')}")
    lines.append("")

    mm_delta = tw.get("market_margin_delta")
    if mm_delta is not None:
        lines.append(f"- 大盤融資變化: {fmt_money(mm_delta, 'TWD')}")
        lines.append("- 風險判斷: " + ("融資增加，降低加碼強度" if mm_delta > 0 else "融資下降，籌碼壓力改善"))
    else:
        lines.append("- 大盤融資資料缺漏，台股信號降級")
    lines.append("")

    # US operations
    us_suggestions, us_remaining, us_alloc = us_buy_plan(us_prices)
    lines.append("### 美股")
    for item in us_suggestions:
        t = item["ticker"]
        price = us_prices.get(t, {})
        price_txt = "N/A" if price.get("close") is None else f"{price.get('close'):.2f}"
        pct_txt = "N/A" if price.get("pct_change") is None else f"{price.get('pct_change'):.2f}%"
        lines.append(
            f"- {t}: 建議買入 {fmt_money(item['buy'], 'USD')}；"
            f"收盤 {price_txt}，日漲跌 {pct_txt}；配置缺口 {fmt_money(item['gap'], 'USD')}"
        )
        lines.append(f"  - 原因: {'、'.join(item['notes'])}")
    lines.append(f"- 美股投入後剩餘現金: {fmt_money(us_remaining, 'USD')}")
    lines.append("")

    qqq = us_prices.get("QQQ", {})
    ixic = us_prices.get("^IXIC", {})
    lines.append("## 市場資料新鮮度")
    lines.append("")
    lines.append(f"- QQQ: {qqq.get('date', 'N/A')}，{qqq.get('freshness', '資料缺漏')}")
    lines.append(f"- Nasdaq(^IXIC): {ixic.get('date', 'N/A')}，{ixic.get('freshness', '資料缺漏')}")
    for sid, r in stocks.items():
        p = r.get("price") or {}
        lines.append(f"- {sid}: {p.get('date', 'N/A')}，{freshness_label(p.get('date'))}")
    lines.append("")

    lines.append("## 加碼排名")
    lines.append("")
    tw_rank = sorted(
        [
            ("0050", buy0050, s0050),
            ("6005 群益證", buy6005, s6005),
        ],
        key=lambda x: x[1],
        reverse=True,
    )
    lines.append("### 台股")
    for name, buy, signal in tw_rank:
        lines.append(f"- {name}: {signal}，{fmt_money(buy, 'TWD')}")
    lines.append("")

    lines.append("### 美股")
    for item in sorted(us_suggestions, key=lambda x: x["buy"], reverse=True):
        lines.append(f"- {item['ticker']}: {fmt_money(item['buy'], 'USD')}，score={item['score']:.2f}")
    lines.append("")

    lines.append("## 下一個加碼條件")
    lines.append("")
    lines.append("- 0050: 跌破 97 元，或單日跌幅 >= 2% 且外資轉買、融資下降。")
    lines.append("- 群益證: 外資連買且融資下降才小額加碼，單次不超過 20,000~30,000 TWD。")
    lines.append("- MSFT / GOOGL: 若仍低於目標配置且單日回檔 >= 2%，優先加碼。")
    lines.append("- NVDA / AVGO / TSM: 若已高於目標配置，不追價；等明顯回檔再補。")
    lines.append("")

    lines.append("## 原始資料")
    lines.append("")
    lines.append("### 台股")
    for sid, r in stocks.items():
        p = r.get("price") or {}
        m = r.get("margin") or {}
        lines.append(f"#### {sid} {r['name']}")
        lines.append(f"- 日期: {p.get('date')}")
        lines.append(f"- 收盤價: {p.get('close')}")
        lines.append(f"- 漲跌: {p.get('spread')}")
        lines.append(f"- 成交量: {p.get('Trading_Volume')}")
        lines.append(f"- 法人日期: {r.get('inst_date')}")
        for k, v in (r.get("inst") or {}).items():
            lines.append(f"  - {k}: {v:,}")
        lines.append(f"- 融資日期: {m.get('date')}")
        lines.append(f"- 融資餘額: {m.get('MarginPurchaseTodayBalance')}")
        lines.append(f"- 融資增減估算: {r.get('margin_delta')}")
        lines.append("")

    lines.append("### 美股配置")
    _, alloc = us_allocation()
    for t, row in alloc.items():
        target = row["target"]
        target_txt = "觀察名單" if target is None else f"{target*100:.1f}%"
        lines.append(
            f"- {t}: current={fmt_money(row['current'], 'USD')}, "
            f"weight={row['weight']*100:.1f}%, target={target_txt}, "
            f"gap={fmt_money(row['gap'], 'USD') if row['gap'] is not None else 'N/A'}"
        )

    return "\n".join(lines)


def main():
    tw = fetch_tw_data()
    us_prices = fetch_us_data()
    report = build_report(tw, us_prices)

    with open("report.md", "w", encoding="utf-8") as f:
        f.write(report)

    print(report)
    print("\nreport.md generated successfully.")


if __name__ == "__main__":
    main()
