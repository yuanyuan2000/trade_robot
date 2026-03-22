import mplfinance as mpf

def plot_kline(df, title="Chart"):
    df["MA5"] = df["Close"].rolling(5).mean()
    df["MA20"] = df["Close"].rolling(20).mean()

    mpf.plot(
        df,
        type="candle",
        mav=(5, 20),
        volume=True,
        title=title,
        style="yahoo"
    )