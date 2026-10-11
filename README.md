# Portfolio overview

A Streamlit dashboard that reads stock transactions, downloads market prices and corporate actions with `yfinance`, and shows portfolio/stock history, current holdings, and estimated U.S. federal capital gains tax.

See [DESIGN.md](DESIGN.md) for the product design, data flow, calculation assumptions, limitations, and roadmap.

For a junior-developer walkthrough from the design through the recent bug fix, see [PROJECT_WALKTHROUGH.md](PROJECT_WALKTHROUGH.md).

## Run locally

```powershell
cd StockPortfolio
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

The repository does not include personal transaction data. Upload a CSV or Excel workbook using the sidebar to get started. If a local `transactions.csv` is present beside the app, it is used until you upload another file. Choose a benchmark and set assumed short-term and long-term federal rates in the sidebar. Valid uploads are previewed before price history loads, and invalid data reports the affected spreadsheet rows. The app includes portfolio and individual ticker charts, a benchmark comparison, allocation chart, open tax-lot view, and CSV downloads for holdings, transactions, and realized gains. Use **Save dashboard preferences** to store the benchmark, chart period, and tax rate assumptions locally on this computer.

## Transaction format

Required headers (case-insensitive): `ticker`, `date`, `transaction_type`, `quantity`, `price`. Use `BUY` or `SELL`; dates should be Excel-readable dates or ISO format such as `2025-01-31`. Sell quantities cannot exceed the shares held at that time. Only the first sheet of an uploaded Excel workbook is read.

Market data comes from Yahoo Finance through yfinance. Historical values use daily closing prices, adjust share counts for stock splits, and add recorded cash dividends as retained cash. Tax lots are matched FIFO and adjusted for splits because the input has no specific-lot identifiers; a holding is long-term only after more than one year. Estimated federal tax uses the editable rates and separately nets gains and losses by term and tax year; it does not model loss netting between terms, carryovers, state taxes, NIIT, fees, wash sales, or other tax rules. Unrealized tax is hypothetical if open shares were sold at the displayed price. Estimates are not tax advice. The benchmark comparison indexes portfolio value but does not adjust for external cash flows, so additional buys and sales may distort the comparison.
