"""Streamlit dashboard for tracking a transaction-based stock portfolio."""

from __future__ import annotations

import json
import os
from datetime import date
from io import BytesIO
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
import yfinance as yf


st.set_page_config(page_title="Portfolio overview", page_icon="📈", layout="wide")

REQUIRED_COLUMNS = ["ticker", "date", "transaction_type", "quantity", "price"]
DEFAULT_FILE = Path(__file__).with_name("transactions.csv")
PREFERENCES_FILE = Path(__file__).with_name("preferences.json")


def load_preferences() -> dict:
    """Load saved dashboard display preferences."""
    try:
        return json.loads(PREFERENCES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_preferences(preferences: dict) -> None:
    """Persist dashboard display preferences locally."""
    PREFERENCES_FILE.write_text(json.dumps(preferences, indent=2), encoding="utf-8")


def load_transactions(uploaded_file) -> pd.DataFrame:
    """Load transactions from an uploaded CSV/XLSX file or the bundled sample."""
    if uploaded_file is None:
        if not DEFAULT_FILE.is_file():
            raise FileNotFoundError(
                "No bundled transaction file is available. Upload a CSV or Excel transaction file to continue."
            )
        data = pd.read_csv(DEFAULT_FILE)
    elif uploaded_file.name.lower().endswith((".xlsx", ".xls")):
        data = pd.read_excel(BytesIO(uploaded_file.getvalue()))
    else:
        data = pd.read_csv(uploaded_file)

    data.columns = [str(column).strip().lower() for column in data.columns]
    missing = sorted(set(REQUIRED_COLUMNS) - set(data.columns))
    if missing:
        raise ValueError("Missing required columns: " + ", ".join(missing))

    data = data[REQUIRED_COLUMNS].copy()
    if data.empty:
        raise ValueError("The file has no transaction rows.")
    data["ticker"] = data["ticker"].astype(str).str.strip().str.upper()
    data["transaction_type"] = data["transaction_type"].astype(str).str.strip().str.upper()
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
    for column in ("quantity", "price"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    invalid = data.isna().any(axis=1) | data["ticker"].isin(["", "NAN", "NONE"])
    if invalid.any():
        rows = ", ".join(str(index + 2) for index in data.index[invalid][:10])
        raise ValueError(f"Blank or invalid values on spreadsheet row(s): {rows}. Check ticker, date, quantity, and price.")
    future_dates = data["date"] > pd.Timestamp.today().normalize()
    if future_dates.any():
        rows = ", ".join(str(index + 2) for index in data.index[future_dates][:10])
        raise ValueError(f"Future transaction date(s) on spreadsheet row(s): {rows}.")
    if not data["transaction_type"].isin(["BUY", "SELL"]).all():
        invalid = ~data["transaction_type"].isin(["BUY", "SELL"])
        rows = ", ".join(str(index + 2) for index in data.index[invalid][:10])
        raise ValueError(f"transaction_type must be BUY or SELL (spreadsheet row(s): {rows}).")
    if (data[["quantity", "price"]] <= 0).any().any():
        invalid = (data[["quantity", "price"]] <= 0).any(axis=1)
        rows = ", ".join(str(index + 2) for index in data.index[invalid][:10])
        raise ValueError(f"Quantity and price must be greater than zero (spreadsheet row(s): {rows}).")
    return data.sort_values("date", kind="stable").reset_index(drop=True)


@st.cache_data(ttl=900, show_spinner=False)
def get_market_data(tickers: tuple[str, ...], start_date: date) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Fetch daily closing prices, splits, and dividends from Yahoo Finance."""
    if not tickers:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    raw = yf.download(
        list(tickers), start=start_date, end=date.today() + pd.Timedelta(days=1),
        auto_adjust=False, actions=True, progress=False, group_by="column", threads=True,
    )
    if raw.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    def extract(field: str, default: float) -> pd.DataFrame:
        if isinstance(raw.columns, pd.MultiIndex):
            if field in raw.columns.get_level_values(0):
                result = raw[field]
            elif field in raw.columns.get_level_values(1):
                result = raw.xs(field, axis=1, level=1)
            else:
                result = pd.DataFrame(index=raw.index)
        elif field in raw.columns:
            result = raw[[field]].rename(columns={field: tickers[0]})
        else:
            result = pd.DataFrame(index=raw.index)
        if isinstance(result, pd.Series):
            result = result.to_frame(name=tickers[0])
        result.columns = [str(column).upper() for column in result.columns]
        return result.reindex(columns=[ticker.upper() for ticker in tickers], fill_value=default)

    close = extract("Close", float("nan")).sort_index().ffill()
    splits = extract("Stock Splits", 0.0).sort_index().fillna(0.0)
    dividends = extract("Dividends", 0.0).sort_index().fillna(0.0)
    return close, splits, dividends


def classify_holding_period(acquired: pd.Timestamp, disposed: pd.Timestamp) -> str:
    """Classify a lot as long-term only when held for more than one year."""
    return "Long-term" if disposed > acquired + pd.DateOffset(years=1) else "Short-term"


def build_tax_lots(
    transactions: pd.DataFrame, as_of: pd.Timestamp, splits: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply stock splits and FIFO sales; return realized gains and open lots."""
    realized = []
    remaining = []
    for ticker, trades in transactions.groupby("ticker", sort=False):
        lots = []
        split_events = splits[ticker][splits[ticker] > 0] if ticker in splits else pd.Series(dtype=float)
        split_cursor = 0

        def apply_splits(until: pd.Timestamp) -> None:
            nonlocal split_cursor
            while split_cursor < len(split_events) and split_events.index[split_cursor] <= until:
                factor = float(split_events.iloc[split_cursor])
                for open_lot in lots:
                    open_lot["Shares"] *= factor
                    open_lot["Unit basis"] /= factor
                split_cursor += 1

        for trade in trades.itertuples(index=False):
            apply_splits(trade.date)
            quantity, price = float(trade.quantity), float(trade.price)
            if trade.transaction_type == "BUY":
                lots.append({"Ticker": ticker, "Acquired": trade.date, "Shares": quantity, "Unit basis": price})
                continue
            available = sum(lot["Shares"] for lot in lots)
            if quantity > available + 1e-9:
                raise ValueError(f"{ticker}: sell of {quantity:g} shares exceeds holdings on {trade.date.date()}.")
            to_sell = quantity
            while to_sell > 1e-9:
                lot = lots[0]
                sold = min(to_sell, lot["Shares"])
                gain = sold * (price - lot["Unit basis"])
                realized.append({
                    "Ticker": ticker,
                    "Date sold": trade.date,
                    "Shares": sold,
                    "Proceeds": sold * price,
                    "Cost basis": sold * lot["Unit basis"],
                    "Gain / loss": gain,
                    "Term": classify_holding_period(lot["Acquired"], trade.date),
                    "Tax year": trade.date.year,
                })
                lot["Shares"] -= sold
                to_sell -= sold
                if lot["Shares"] <= 1e-9:
                    lots.pop(0)
        apply_splits(as_of)
        for lot in lots:
            lot["Term"] = classify_holding_period(lot["Acquired"], as_of)
            remaining.append(lot)
    return pd.DataFrame(realized), pd.DataFrame(remaining)


def summarize_holdings(open_lots: pd.DataFrame) -> pd.DataFrame:
    """Aggregate open FIFO lots by ticker."""
    if open_lots.empty:
        return pd.DataFrame(columns=["Ticker", "Shares", "Cost basis", "Average cost"])
    open_lots = open_lots.copy()
    open_lots["Cost basis"] = open_lots["Shares"] * open_lots["Unit basis"]
    holdings = open_lots.groupby("Ticker", as_index=False).agg(Shares=("Shares", "sum"), **{"Cost basis": ("Cost basis", "sum")})
    holdings["Average cost"] = holdings["Cost basis"] / holdings["Shares"]
    return holdings


def build_history(
    transactions: pd.DataFrame, prices: pd.DataFrame, splits: pd.DataFrame, dividends: pd.DataFrame
) -> pd.DataFrame:
    """Value daily holdings and accrued cash dividends, accounting for splits."""
    if prices.empty:
        return pd.DataFrame()
    history = prices.copy()
    for ticker in history.columns:
        trades = transactions[transactions["ticker"] == ticker]
        trade_rows = list(trades.itertuples(index=False))
        trade_cursor = 0
        split_events = splits[ticker][splits[ticker] > 0] if ticker in splits else pd.Series(dtype=float)
        split_cursor = 0
        dividend_events = dividends[ticker] if ticker in dividends else pd.Series(dtype=float)
        shares = 0.0
        cash_dividends = 0.0
        values, dividend_totals = [], []
        for day in history.index:
            if day in dividend_events.index and float(dividend_events.loc[day]) > 0:
                cash_dividends += shares * float(dividend_events.loc[day])
            while split_cursor < len(split_events) and split_events.index[split_cursor] <= day:
                shares *= float(split_events.iloc[split_cursor])
                split_cursor += 1
            while trade_cursor < len(trade_rows) and trade_rows[trade_cursor].date <= day:
                trade = trade_rows[trade_cursor]
                shares += float(trade.quantity) if trade.transaction_type == "BUY" else -float(trade.quantity)
                trade_cursor += 1
            # Yahoo's historical Close is split-adjusted, so express that day's
            # shares in the same latest-share units as its price series.
            future_factor = float(split_events.loc[split_events.index > day].prod()) if not split_events.empty else 1.0
            values.append(float(prices.loc[day, ticker]) * max(shares, 0) * future_factor)
            dividend_totals.append(cash_dividends)
        history[ticker] = values
        history[f"{ticker} dividends"] = dividend_totals
    asset_columns = list(prices.columns)
    dividend_columns = [f"{ticker} dividends" for ticker in asset_columns]
    history["Portfolio value"] = history[asset_columns].sum(axis=1)
    history["Dividends received"] = history[dividend_columns].sum(axis=1)
    history["Portfolio incl. dividends"] = history["Portfolio value"] + history["Dividends received"]
    return history


def get_openai_api_key() -> str | None:
    """Read an API key from Streamlit secrets or the process environment."""
    try:
        return st.secrets.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    except (FileNotFoundError, KeyError):
        return os.getenv("OPENAI_API_KEY")


def portfolio_chat_reply(question: str, context: dict, messages: list[dict]) -> str:
    """Ask the configured LLM to answer using the current portfolio snapshot."""
    from openai import OpenAI

    client = OpenAI(api_key=get_openai_api_key())
    conversation = [
        {"role": message["role"], "content": message["content"]}
        for message in messages[-10:]
    ]
    conversation.append({"role": "user", "content": question})
    response = client.responses.create(
        model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
        instructions=(
            "You are a portfolio dashboard assistant. Answer using the supplied current "
            "portfolio snapshot and recent conversation. For prices and 52-week ranges, "
            "use only the supplied market data and mention its as-of date. If data is "
            "missing, say so. For tax questions, explain the dashboard's assumptions and "
            "that estimates are not tax advice; do not claim to determine the user's tax "
            "liability or invent tax rules. Give concise, clear answers."
        ),
        input=[
            {"role": "developer", "content": "Current portfolio snapshot (JSON):\n" + json.dumps(context)},
            *conversation,
        ],
        max_output_tokens=700,
    )
    return response.output_text


preferences = load_preferences()

st.title("Portfolio overview")
st.caption("Holdings, current valuation, and historical portfolio value from your transactions.")

with st.sidebar:
    st.header("Portfolio data")
    uploaded = st.file_uploader("Upload transactions", type=["csv", "xlsx"])
    st.caption("Required columns: ticker, date, transaction_type, quantity, price")
    st.caption("Prices are fetched from Yahoo Finance. Data is cached for 15 minutes.")
    benchmarks = ["SPY", "QQQ", "DIA"]
    saved_benchmark = preferences.get("benchmark", "SPY")
    benchmark = st.selectbox("Benchmark", benchmarks, index=benchmarks.index(saved_benchmark) if saved_benchmark in benchmarks else 0)
    st.subheader("U.S. federal rate assumptions")
    short_rate = st.number_input("Short-term rate (%)", min_value=0.0, max_value=100.0, value=float(preferences.get("short_rate", 24.0)), step=0.5) / 100
    long_rate = st.number_input("Long-term rate (%)", min_value=0.0, max_value=100.0, value=float(preferences.get("long_rate", 15.0)), step=0.5) / 100

try:
    transactions = load_transactions(uploaded)
except Exception as error:
    st.error(str(error))
    st.stop()

st.success(f"Loaded {len(transactions)} validated transactions across {transactions['ticker'].nunique()} tickers.")
with st.expander("Review validated transactions"):
    st.dataframe(transactions, use_container_width=True, hide_index=True)

start_date = min(transactions["date"].min().date(), date.today() - pd.Timedelta(days=366))
asset_tickers = tuple(sorted(transactions["ticker"].unique()))
market_tickers = tuple(sorted(set(asset_tickers + (benchmark,))))
with st.spinner("Loading prices and corporate actions from Yahoo Finance..."):
    all_prices, all_splits, all_dividends = get_market_data(market_tickers, start_date)

missing_prices = [ticker for ticker in asset_tickers if ticker not in all_prices or all_prices[ticker].dropna().empty]
if all_prices.empty or missing_prices:
    if missing_prices:
        st.error("No Yahoo Finance price history found for: " + ", ".join(missing_prices))
        st.stop()
    st.error("Yahoo Finance returned no price data. Check your connection and ticker symbols, then refresh.")
    st.stop()

prices = all_prices.reindex(columns=asset_tickers).ffill()
splits = all_splits.reindex(columns=asset_tickers, fill_value=0.0)
dividends = all_dividends.reindex(columns=asset_tickers, fill_value=0.0)
try:
    realized_sales, open_lots = build_tax_lots(transactions, pd.Timestamp.today().normalize(), splits)
    holdings = summarize_holdings(open_lots)
except Exception as error:
    st.error(str(error))
    st.stop()
latest = prices.ffill().iloc[-1]
holdings["Price"] = holdings["Ticker"].map(latest)
holdings["Market value"] = holdings["Shares"] * holdings["Price"]
holdings["Unrealized P&L"] = holdings["Market value"] - holdings["Cost basis"]
holdings["Return"] = holdings["Unrealized P&L"] / holdings["Cost basis"] * 100
open_lots = open_lots.copy()
open_lots["Price"] = open_lots["Ticker"].map(latest)
open_lots["Unrealized gain / loss"] = open_lots["Shares"] * (open_lots["Price"] - open_lots["Unit basis"])
if not open_lots.empty:
    term_gain = open_lots.pivot_table(index="Ticker", columns="Term", values="Unrealized gain / loss", aggfunc="sum", fill_value=0)
    term_gain.columns = [f"{term} unrealized gain / loss" for term in term_gain.columns]
    holdings = holdings.merge(term_gain, on="Ticker", how="left")
for column in ("Short-term unrealized gain / loss", "Long-term unrealized gain / loss"):
    if column not in holdings:
        holdings[column] = 0.0
history = build_history(transactions, prices, splits, dividends)
portfolio_value = float(holdings["Market value"].sum())
cost_basis = float(holdings["Cost basis"].sum())
unrealized = portfolio_value - cost_basis
dividend_income = float(history["Dividends received"].iloc[-1])

left, middle, right, fourth = st.columns(4)
left.metric("Current market value", f"${portfolio_value:,.2f}")
middle.metric("Open cost basis", f"${cost_basis:,.2f}")
right.metric("Unrealized gain / loss", f"${unrealized:,.2f}", f"{unrealized / cost_basis:.2%}" if cost_basis else None)
fourth.metric("Dividends received", f"${dividend_income:,.2f}")

st.subheader("Historical portfolio value")
periods = ["1M", "3M", "6M", "YTD", "1Y", "All"]
saved_period = preferences.get("period", "All")
period = st.selectbox("View period", periods, index=periods.index(saved_period) if saved_period in periods else 5)
if st.sidebar.button("Save dashboard preferences"):
    save_preferences({"benchmark": benchmark, "short_rate": short_rate * 100, "long_rate": long_rate * 100, "period": period})
    st.sidebar.success("Preferences saved on this computer.")
last_date = history.index.max()
period_start = {
    "1M": last_date - pd.DateOffset(months=1),
    "3M": last_date - pd.DateOffset(months=3),
    "6M": last_date - pd.DateOffset(months=6),
    "YTD": pd.Timestamp(year=last_date.year, month=1, day=1),
    "1Y": last_date - pd.DateOffset(years=1),
    "All": history.index.min(),
}[period]
chart_history = history.loc[history.index >= period_start]
st.area_chart(chart_history[["Portfolio incl. dividends"]], color="#38bdf8")
benchmark_prices = all_prices[benchmark].reindex(chart_history.index).ffill()
base_value = chart_history.loc[chart_history["Portfolio incl. dividends"] > 0, "Portfolio incl. dividends"]
if not benchmark_prices.dropna().empty and not base_value.empty:
    comparison_start = base_value.index[0]
    benchmark_base = benchmark_prices.loc[benchmark_prices.index >= comparison_start].dropna()
    portfolio_index = chart_history.loc[comparison_start:, "Portfolio incl. dividends"] / float(base_value.iloc[0]) * 100
    benchmark_index = benchmark_prices.loc[comparison_start:] / float(benchmark_base.iloc[0]) * 100
    comparison = pd.DataFrame({"Portfolio (indexed)": portfolio_index, benchmark: benchmark_index}).dropna()
    st.markdown(f"**Performance comparison** · portfolio vs. {benchmark}, both indexed to 100")
    st.line_chart(comparison)
st.caption("Portfolio value includes cash dividends received and assumes they remain in cash. Buy and sell activity changes the value line; performance indexing is not cash-flow adjusted. Historical closes are adjusted for stock splits. Fees and taxes are excluded.")

overview_left, overview_right = st.columns(2)
with overview_left:
    st.markdown("**Current allocation**")
    allocation = px.pie(holdings, values="Market value", names="Ticker", hole=0.58)
    allocation.update_layout(margin={"t": 10, "b": 10, "l": 10, "r": 10}, legend_title_text="Holding")
    st.plotly_chart(allocation, use_container_width=True)
with overview_right:
    st.markdown("**Individual stock history**")
    selected_ticker = st.selectbox("Select stock", list(asset_tickers))
    st.line_chart(prices.loc[prices.index >= period_start, [selected_ticker]])
    stock_trades = transactions[transactions["ticker"] == selected_ticker][["date", "price", "transaction_type"]].rename(columns={"date": "Trade date", "price": "Transaction price", "transaction_type": "Side"})
    if not stock_trades.empty:
        ticker_splits = splits[selected_ticker]
        stock_trades["Transaction price"] = stock_trades.apply(
            lambda trade: trade["Transaction price"] / float(
                ticker_splits.loc[(ticker_splits.index > trade["Trade date"]) & (ticker_splits > 0)].prod()
            ),
            axis=1,
        )
        st.scatter_chart(stock_trades, x="Trade date", y="Transaction price", color="Side")

st.subheader("Capital gains and estimated federal tax")
annual = pd.DataFrame()
if realized_sales.empty:
    st.info("No sales are recorded, so there are no realized gains or losses to classify.")
else:
    annual = realized_sales.pivot_table(index=["Tax year", "Term"], values="Gain / loss", aggfunc="sum").reset_index()
    annual["Assumed rate"] = annual["Term"].map({"Short-term": short_rate, "Long-term": long_rate})
    annual["Estimated federal tax"] = annual["Gain / loss"].clip(lower=0) * annual["Assumed rate"]
    annual["Assumed rate"] *= 100
    st.markdown("**Realized sales by tax year**")
    st.dataframe(annual, use_container_width=True, hide_index=True, column_config={
        "Gain / loss": st.column_config.NumberColumn(format="$%.2f"),
        "Assumed rate": st.column_config.NumberColumn(format="%.1f%%"),
        "Estimated federal tax": st.column_config.NumberColumn(format="$%.2f"),
    })

open_term_gains = open_lots.groupby("Term")["Unrealized gain / loss"].sum() if not open_lots.empty else pd.Series(dtype=float)
open_tax = sum(
    max(float(gain), 0) * {"Short-term": short_rate, "Long-term": long_rate}[term]
    for term, gain in open_term_gains.items()
)
st.metric("Estimated tax on currently open gains if sold today", f"${open_tax:,.2f}")
st.caption("U.S. federal estimate only. FIFO is assumed because the file has no tax-lot identifiers. Rates are editable assumptions, not your determined tax brackets. Estimates apply each rate separately to positive net gains by term and year; they exclude loss netting between terms, carryovers, state taxes, the net investment income tax, and other tax rules. Unrealized tax is hypothetical if positions were sold at the displayed prices. Consult a tax professional for filing.")
with st.expander("Open tax lots"):
    if open_lots.empty:
        st.info("No open tax lots.")
    else:
        lot_view = open_lots[["Ticker", "Acquired", "Shares", "Unit basis", "Term", "Unrealized gain / loss"]].copy()
        st.dataframe(lot_view, use_container_width=True, hide_index=True)

st.subheader("Current holdings")
if holdings.empty:
    st.info("No open positions remain.")
display = holdings[["Ticker", "Shares", "Price", "Market value", "Average cost", "Cost basis", "Short-term unrealized gain / loss", "Long-term unrealized gain / loss", "Unrealized P&L", "Return"]].copy()
st.dataframe(
    display,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Shares": st.column_config.NumberColumn(format="%.4f"),
        "Price": st.column_config.NumberColumn(format="$%.2f"),
        "Market value": st.column_config.NumberColumn(format="$%.2f"),
        "Average cost": st.column_config.NumberColumn(format="$%.2f"),
        "Cost basis": st.column_config.NumberColumn(format="$%.2f"),
        "Short-term unrealized gain / loss": st.column_config.NumberColumn(format="$%.2f"),
        "Long-term unrealized gain / loss": st.column_config.NumberColumn(format="$%.2f"),
        "Unrealized P&L": st.column_config.NumberColumn(format="$%.2f"),
        "Return": st.column_config.NumberColumn(format="%.2f%%"),
    },
)

st.subheader("Ask about your portfolio")
st.caption(
    "Ask about holdings, gains, the dashboard's tax estimates, or a ticker's 52-week "
    "high/low. Your question and a summary of portfolio holdings and tax figures are "
    "sent to the configured OpenAI API. Market ranges use available Yahoo Finance daily closes."
)
api_key = get_openai_api_key()
if not api_key:
    st.info("To enable chat, set OPENAI_API_KEY in your environment or Streamlit secrets, then restart the app.")
else:
    chat_context = {
        "as_of": str(prices.index[-1].date()),
        "market_data_source": "Yahoo Finance daily Close, split-adjusted",
        "portfolio_market_value": round(portfolio_value, 2),
        "open_cost_basis": round(cost_basis, 2),
        "unrealized_gain_loss": round(unrealized, 2),
        "dividends_received": round(dividend_income, 2),
        "open_gain_tax_estimate_if_sold_today": round(float(open_tax), 2),
        "tax_assumptions": {
            "lot_method": "FIFO",
            "short_term_rate_percent": round(short_rate * 100, 2),
            "long_term_rate_percent": round(long_rate * 100, 2),
            "limitations": "Simplified federal estimate; excludes cross-term loss netting, carryovers, state tax, NIIT, fees, wash sales, and other tax rules.",
        },
        "holdings": holdings.round(2).to_dict(orient="records"),
        "realized_gains_by_year_and_term": (
            realized_sales.groupby(["Tax year", "Term"])["Gain / loss"].sum().round(2).reset_index().to_dict(orient="records")
            if not realized_sales.empty else []
        ),
        "ticker_52_week_ranges": {
            ticker: {
                "low": round(float(prices[ticker].loc[prices.index >= prices.index[-1] - pd.Timedelta(days=365)].min()), 2),
                "high": round(float(prices[ticker].loc[prices.index >= prices.index[-1] - pd.Timedelta(days=365)].max()), 2),
                "available_days": int(prices[ticker].loc[prices.index >= prices.index[-1] - pd.Timedelta(days=365)].count()),
            }
            for ticker in asset_tickers
        },
    }
    if "portfolio_chat" not in st.session_state:
        st.session_state.portfolio_chat = []
    for chat_message in st.session_state.portfolio_chat:
        with st.chat_message(chat_message["role"]):
            st.markdown(chat_message["content"])
    if question := st.chat_input("Ask about holdings, taxes, or 52-week ranges", max_chars=1000):
        st.session_state.portfolio_chat.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            with st.spinner("Reviewing your portfolio..."):
                try:
                    answer = portfolio_chat_reply(question, chat_context, st.session_state.portfolio_chat[:-1])
                    st.markdown(answer)
                    st.session_state.portfolio_chat.append({"role": "assistant", "content": answer})
                except Exception as error:
                    st.error(f"The portfolio assistant could not respond: {error}")

with st.expander("Transaction history"):
    st.dataframe(transactions, use_container_width=True, hide_index=True)

st.subheader("Download reports")
download_left, download_middle, download_right = st.columns(3)
download_left.download_button("Holdings CSV", holdings.to_csv(index=False), "portfolio_holdings.csv", "text/csv")
download_middle.download_button("Transactions CSV", transactions.to_csv(index=False), "portfolio_transactions.csv", "text/csv")
if annual.empty:
    download_right.download_button("Realized gains CSV", realized_sales.to_csv(index=False), "realized_gains.csv", "text/csv", disabled=True)
else:
    download_right.download_button("Realized gains CSV", annual.to_csv(index=False), "realized_gains_by_year.csv", "text/csv")
