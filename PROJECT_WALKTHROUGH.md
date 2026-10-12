# Project walkthrough: design to current fix

This document explains how the portfolio dashboard is intended to work, how
the design maps to the implementation, and the bug investigation completed
most recently. It is written as a learning guide for a junior developer.

For the full product requirements, assumptions, and roadmap, see
[DESIGN.md](DESIGN.md). For setup and transaction-file instructions, see
[README.md](README.md).

## 1. Start with the product goal

The app is a local dashboard for an individual investor. It reads stock
transactions and helps answer questions such as:

- What stocks do I currently hold, and what are they worth?
- How has the portfolio changed over time?
- What are the cost basis and unrealized gains or losses?
- Which realized gains are short-term or long-term, and what might estimated
  federal tax look like?

It is a monitoring and planning tool, not a brokerage system or tax-preparation
product. Those boundaries matter: the app states its assumptions and
limitations rather than implying its estimates are guaranteed or filing-ready.

## 2. Turn the goal into an application design

The design separates the work into a data path and user-facing views:

1. Read a local CSV or a user-uploaded CSV/Excel file. The repository does not
   include personal transaction data, so a fresh clone requires an upload.
2. Normalize and validate the transaction fields before doing calculations.
3. Download prices, split events, and dividends for portfolio tickers and a
   benchmark.
4. Calculate split-adjusted FIFO tax lots and realized sales.
5. Calculate holdings and historical portfolio values.
6. Render charts, summary metrics, tax estimates, and downloadable CSV reports.
7. Optionally save display preferences locally.

The input checks are intentionally early in this sequence. Invalid rows should
be explained to the user before the app makes a market-data request or builds
portfolio results from bad data.

## 3. Technologies and their roles

| Technology | Role in this project |
|---|---|
| Python | Application language and calculation logic. |
| Streamlit | Web-style dashboard, inputs, metrics, charts, and downloads. |
| pandas | CSV/Excel tables, validation, time series, grouping, and calculations. |
| yfinance | Retrieves daily market prices and corporate actions from Yahoo Finance. |
| Plotly | Renders the current-allocation donut chart. |
| OpenAI Python SDK | Optional portfolio Q&A through the Responses API. |
| openpyxl | Enables reading Excel `.xlsx` transaction files through pandas. |
| JSON and local files | Store dashboard preferences and the bundled transaction sample. |
| PowerShell | Used for local command-line reproduction and validation in this work. |

The project does not use a database. The dashboard and calculations run in a
single Python app process; the transaction data is read from a file or upload.
When chat is enabled, the app sends a compact calculated portfolio snapshot and
recent conversation turns to the configured OpenAI API account.

## 4. How the design appears in the code

The main application is [app.py](app.py). Its major stages include:

- **Input:** `load_transactions` reads an uploaded CSV/Excel file or a local
  `transactions.csv`, normalizes column names, validates values, and sorts
  transactions by date. If neither input is available, it asks the user to
  upload a file.
- **Market data:** `get_market_data` downloads and caches closing prices,
  splits, and dividends.
- **Tax lots:** `build_tax_lots` processes split events and applies FIFO
  matching to sells.
- **Holdings:** `summarize_holdings` aggregates open lots into positions.
- **History:** `build_history` values portfolio positions over time and tracks
  dividends received.
- **Presentation:** the remainder of the Streamlit script builds the controls,
  charts, tax tables, and CSV downloads.
- **Chat:** an optional Streamlit chat panel supplies current holdings, gain
  summaries, tax assumptions, and trailing-year ticker price ranges to the
  OpenAI Responses API. It does not send the original transaction file.

The transaction schema and how to run the project are in [README.md](README.md).
The product flow, calculation assumptions, error behavior, limitations, and
future ideas are in [DESIGN.md](DESIGN.md).

## 5. The error we investigated

The reported error was `ZeroDivisionError: division by zero`. We searched the
app for division operations and inspected the relevant calculations. The
failure was in the individual-stock chart, which adjusts transaction prices
for stock splits.

Yahoo Finance split data uses `0` on days when no split occurred. The previous
chart calculation selected every later daily split value and multiplied them
together. If all those values were zero, their product was zero, and the app
then divided the transaction price by zero.

This was a data-semantics issue: zero meant "no split event," not "a split
factor that should be included in the adjustment."

## 6. The fix and why it works

The stock-history calculation now filters for split values that are both:

- later than the transaction date; and
- greater than zero.

It multiplies only those actual split factors. pandas returns `1` for the
product of an empty selection, which is the correct neutral adjustment when no
future split exists: dividing by one leaves the transaction price unchanged.
When a real future split exists, its factor is still used to adjust the price.

The change is localized to the transaction-price adjustment in
[app.py](app.py).

## 7. Verification performed

The fix was checked with a small pandas-based reproduction:

- Before the fix, a series of no-split values produced product `0.0`, and
  dividing by it raised `ZeroDivisionError`.
- After filtering, no split events produced product `1.0`.
- A future split factor of `2.0` produced an adjusted price equal to half the
  original price.
- `python -m py_compile app.py` completed successfully.

These checks validate the denominator behavior and Python syntax. They are
focused checks, not a full end-to-end test of live Yahoo Finance requests or
the rendered Streamlit dashboard.

## 8. Local run and next steps

Follow the setup instructions in [README.md](README.md). In brief, create and
activate a virtual environment, install the dependencies from
[requirements.txt](requirements.txt), then start Streamlit:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Useful follow-up engineering work would be to add automated tests for split
adjustments (no future splits, one split, and multiple splits), then expand
coverage to transaction validation, FIFO matching, and historical valuation.
The broader product roadmap is recorded in [DESIGN.md](DESIGN.md).

To enable portfolio chat, configure `OPENAI_API_KEY` as described in
[README.md](README.md). The chat model can be changed with `OPENAI_MODEL`.

## 9. Lessons to carry forward

- Trace a runtime error to the exact operation and its input values before
  changing code.
- Understand what special values mean in external data; a numeric zero may be
  a sentinel for "no event."
- Use the mathematical identity for an empty product (`1`) when combining
  multiplicative adjustment factors.
- Keep a bug fix close to the calculation that produced the invalid value.
- Verify both sides of the behavior: the edge case with no events and the
  normal case with a real event.
