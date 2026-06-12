import os
import json
import math
import requests
import yfinance as yf
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")
MARKET_MODE = os.environ.get("MARKET_MODE", "full")

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

TW_LOOKBACK_DAYS = 90
US_LOOKBACK_PERIOD = "1y"

TW_STOCKS = {
    "0050": "元大台灣50",
    "00988A": "主動統一台股增長",
    "6005": "群益證",
}

US_TICKERS = [
    "SPY",
    "QQQ",
    "^IXIC",
    "SOXX",
    "SMH",
    "^VIX",
    "NVDA",
    "TSM",
    "AVGO",
    "MSFT",
    "GOOGL",
    "META",
    "AMZN",
]


def now_tw():
    return datetime.now(TZ)


def safe_float(x):
    try:
        if x is None:
            return None
        if isinstance(x, float) and math.isnan(x):
            return None
        return float(x)
    except Exception:
        return None


def pct_change(new, old):
    if new is None or old in (None, 0):
        return None
    return (new / old - 1) * 100


def latest_row(rows, date_key="date"):
    if not rows:
        return None
    return sorted(rows, key=lambda x: x.get(date_key, ""))[-1]


def freshness_label(date_str):
    if not date_str:
        return "missing"

    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        age = (now_tw().date() - d).days
    except Exception:
        return "unknown"

    if age <= 1:
        return "fresh"
    if age <= 3:
        return "weekend_or_holiday_possible"
    return "stale_over_one_trading_day"


def finmind_fetch(dataset, stock_id=None, start_date=None, end_date=None):
    if not FINMIND_TOKEN:
        raise RuntimeError("Missing FINMIND_TOKEN")

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


def calc_tw_technical(price_rows):
    if not price_rows:
        return {}

    rows = sorted(price_rows, key=lambda x: x.get("date", ""))

    closes = [safe_float(r.get("close")) for r in rows]
    closes = [x for x in closes if x is not None]

    latest_close = closes[-1] if closes else None
    close_5 = closes[-6] if len(closes) >= 6 else None
    close_20 = closes[-21] if len(closes) >= 21 else None

    recent_60 = closes[-60:] if len(closes) >= 1 else []
    high_60 = max(recent_60) if recent_60 else None
    low_60 = min(recent_60) if recent_60 else None

    return {
        "pct_change_5d": pct_change(latest_close, close_5),
        "pct_change_20d": pct_change(latest_close, close_20),
        "high_60d": high_60,
        "low_60d": low_60,
        "pct_from_60d_high": pct_change(latest_close, high_60),
        "pct_from_60d_low": pct_change(latest_close, low_60),
    }


def fetch_tw_data():
    today = now_tw().date()
    start = today - timedelta(days=TW_LOOKBACK_DAYS)

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
        today_balance = market_margin.get("TodayBalance", 0) or 0
        yesterday_balance = market_margin.get("YesBalance", 0) or 0
        market_margin_delta = today_balance - yesterday_balance

    stocks = {}

    for stock_id, name in TW_STOCKS.items():
        price_rows = finmind_fetch(
            "TaiwanStockPrice",
            stock_id,
            start_date,
            end_date,
        )

        price = latest_row(price_rows)

        inst_date, inst = get_inst_summary(
            finmind_fetch(
                "TaiwanStockInstitutionalInvestorsBuySell",
                stock_id,
                start_date,
                end_date,
            )
        )

        margin = latest_row(
            finmind_fetch(
                "TaiwanStockMarginPurchaseShortSale",
                stock_id,
                start_date,
                end_date,
            )
        )

        technical = calc_tw_technical(price_rows)

        stocks[stock_id] = {
            "name": name,
            "price_date": price.get("date") if price else None,
            "open": safe_float(price.get("open")) if price else None,
            "max": safe_float(price.get("max")) if price else None,
            "min": safe_float(price.get("min")) if price else None,
            "close": safe_float(price.get("close")) if price else None,
            "spread": safe_float(price.get("spread")) if price else None,
            "trading_volume": price.get("Trading_Volume") if price else None,

            "institutional_date": inst_date,
            "foreign_investor_buy_sell": inst.get("Foreign_Investor"),
            "investment_trust_buy_sell": inst.get("Investment_Trust"),
            "dealer_self_buy_sell": inst.get("Dealer_self"),
            "dealer_hedging_buy_sell": inst.get("Dealer_Hedging"),

            "margin_date": margin.get("date") if margin else None,
            "margin_today_balance": margin.get("MarginPurchaseTodayBalance") if margin else None,
            "margin_delta_estimated": margin_change(margin),

            "technical": technical,
            "freshness": {
                "price": freshness_label(price.get("date") if price else None),
                "institutional": freshness_label(inst_date),
                "margin": freshness_label(margin.get("date") if margin else None),
            },
        }

    return {
        "source": "FinMind",
        "mode": "tw_intraday_or_latest_available",
        "lookback_start": start_date,
        "lookback_end": end_date,
        "market_margin": {
            "date": market_margin.get("date") if market_margin else None,
            "today_balance": market_margin.get("TodayBalance") if market_margin else None,
            "yesterday_balance": market_margin.get("YesBalance") if market_margin else None,
            "delta": market_margin_delta,
            "freshness": freshness_label(market_margin.get("date") if market_margin else None),
        },
        "stocks": stocks,
    }


def calc_us_technical(hist):
    if hist is None or hist.empty:
        return {}

    closes = [safe_float(x) for x in hist["Close"].tolist()]
    closes = [x for x in closes if x is not None]

    latest_close = closes[-1] if closes else None
    close_5 = closes[-6] if len(closes) >= 6 else None
    close_20 = closes[-21] if len(closes) >= 21 else None

    high_52w = safe_float(hist["High"].max())
    low_52w = safe_float(hist["Low"].min())

    return {
        "pct_change_5d": pct_change(latest_close, close_5),
        "pct_change_20d": pct_change(latest_close, close_20),
        "high_52w": high_52w,
        "low_52w": low_52w,
        "pct_from_52w_high": pct_change(latest_close, high_52w),
        "pct_from_52w_low": pct_change(latest_close, low_52w),
    }


def fetch_us_data():
    result = {}

    for ticker in US_TICKERS:
        try:
            obj = yf.Ticker(ticker)

            # 1 年日線：用於 52 週高低點與中期漲跌幅
            hist_daily = obj.history(period=US_LOOKBACK_PERIOD, auto_adjust=False)

            if hist_daily is None or hist_daily.empty:
                result[ticker] = {
                    "error": "no_daily_data",
                    "freshness": "missing",
                }
                continue

            last_daily = hist_daily.iloc[-1]
            prev_daily = hist_daily.iloc[-2] if len(hist_daily) >= 2 else None

            close = safe_float(last_daily.get("Close"))
            open_price = safe_float(last_daily.get("Open"))
            high = safe_float(last_daily.get("High"))
            low = safe_float(last_daily.get("Low"))
            volume = safe_float(last_daily.get("Volume"))
            prev_close = safe_float(prev_daily.get("Close")) if prev_daily is not None else None
            date = hist_daily.index[-1].strftime("%Y-%m-%d")

            # 盤中資料：若 yfinance 有回傳，額外記錄 regularMarketPrice
            info_price = None
            try:
                fast_info = obj.fast_info
                info_price = safe_float(getattr(fast_info, "last_price", None))
            except Exception:
                info_price = None

            technical = calc_us_technical(hist_daily)

            result[ticker] = {
                "date": date,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "previous_close": prev_close,
                "pct_change": pct_change(close, prev_close),
                "volume": volume,
                "intraday_or_latest_price": info_price,
                "intraday_vs_previous_close_pct": pct_change(info_price, prev_close),
                "technical": technical,
                "freshness": freshness_label(date),
            }

        except Exception as e:
            result[ticker] = {
                "error": str(e),
                "freshness": "missing",
            }

    return {
        "source": "Yahoo Finance via yfinance",
        "mode": "us_intraday_or_latest_available",
        "tickers": result,
    }


def build_market_data():
    generated_at = now_tw().strftime("%Y-%m-%d %H:%M:%S %Z")

    data = {
        "schema_version": "market-data-v2",
        "generated_at": generated_at,
        "generated_timezone": "Asia/Taipei",
        "market_mode": MARKET_MODE,
        "privacy_note": (
            "This file contains market data only. "
            "No personal holdings, cash, target allocation, or buy/sell recommendations are included."
        ),
    }

    # 簡化：每次都抓台股與美股，方便 ChatGPT 一次讀完整市場狀態。
    # 排程時間只代表主要用途：11:00 偏台股盤中，00:00 偏美股盤中。
    data["taiwan"] = fetch_tw_data()
    data["us"] = fetch_us_data()

    return data


def write_json(data, path="market_data.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_public_summary(data, path="market_data.md"):
    lines = []

    lines.append("# Public Market Data")
    lines.append("")
    lines.append(f"- Generated at: {data.get('generated_at')}")
    lines.append(f"- Market mode: {data.get('market_mode')}")
    lines.append("- Privacy: market data only; no personal holdings or recommendations.")
    lines.append("")

    lines.append("## Taiwan")
    lines.append("")

    tw = data["taiwan"]

    lines.append("### Market margin")
    mm = tw["market_margin"]
    lines.append(f"- Date: {mm.get('date')}")
    lines.append(f"- Today balance: {mm.get('today_balance')}")
    lines.append(f"- Yesterday balance: {mm.get('yesterday_balance')}")
    lines.append(f"- Delta: {mm.get('delta')}")
    lines.append(f"- Freshness: {mm.get('freshness')}")
    lines.append("")

    for stock_id, row in tw["stocks"].items():
        lines.append(f"### {stock_id} {row.get('name')}")
        lines.append(f"- Price date: {row.get('price_date')}")
        lines.append(f"- Open: {row.get('open')}")
        lines.append(f"- High: {row.get('max')}")
        lines.append(f"- Low: {row.get('min')}")
        lines.append(f"- Close: {row.get('close')}")
        lines.append(f"- Spread: {row.get('spread')}")
        lines.append(f"- Trading volume: {row.get('trading_volume')}")
        lines.append(f"- Foreign investor buy/sell: {row.get('foreign_investor_buy_sell')}")
        lines.append(f"- Investment trust buy/sell: {row.get('investment_trust_buy_sell')}")
        lines.append(f"- Margin delta estimated: {row.get('margin_delta_estimated')}")
        lines.append(f"- Technical: {row.get('technical')}")
        lines.append(f"- Freshness: {row.get('freshness')}")
        lines.append("")

    lines.append("## US")
    lines.append("")

    for ticker, row in data["us"]["tickers"].items():
        lines.append(f"### {ticker}")
        lines.append(f"- Date: {row.get('date')}")
        lines.append(f"- Open: {row.get('open')}")
        lines.append(f"- High: {row.get('high')}")
        lines.append(f"- Low: {row.get('low')}")
        lines.append(f"- Close: {row.get('close')}")
        lines.append(f"- Previous close: {row.get('previous_close')}")
        lines.append(f"- Pct change: {row.get('pct_change')}")
        lines.append(f"- Intraday/latest price: {row.get('intraday_or_latest_price')}")
        lines.append(f"- Intraday vs previous close pct: {row.get('intraday_vs_previous_close_pct')}")
        lines.append(f"- Technical: {row.get('technical')}")
        lines.append(f"- Freshness: {row.get('freshness')}")
        if row.get("error"):
            lines.append(f"- Error: {row.get('error')}")
        lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    data = build_market_data()
    write_json(data, "market_data.json")
    write_public_summary(data, "market_data.md")

    print("Generated market_data.json")
    print("Generated market_data.md")
    print(f"Market mode: {MARKET_MODE}")
    print(json.dumps(data, ensure_ascii=False, indent=2)[:3000])


if __name__ == "__main__":
    main()
