# Market Crawler — Live Feed integration

Add `import live_feed` beside the existing local imports in `app.py`.

Replace:
```python
tab1, tab2 = st.tabs(["📈 Market crawler", "📄 Report crawler"])
```

With:
```python
tab1, tab2, tab3 = st.tabs([
    "📈 Market crawler",
    "📄 Report crawler",
    "📰 Live news & calendar",
])
```

Keep the existing `with tab2: report_crawler.render()` block and the complete
existing `with tab1:` block unchanged. After the `with tab1:` block, add:

```python
with tab3:
    live_feed.render()
```

Upload `live_feed_revised.py` to the repository as `live_feed.py` beside
`app.py`, `report_crawler.py`, and `universal_market_analyzer.py`.

The revised module passed a Python syntax check. It also changes geopolitical
headline scoring so that words such as “raises” do not automatically make a war
headline bullish for every currency. Risk-off headlines use a cautious haven/risk-
sensitive currency heuristic. This is not a validated forecasting model or
financial advice.
