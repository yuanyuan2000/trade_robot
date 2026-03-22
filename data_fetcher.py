import yfinance as yf
import pandas as pd
import os
from config import SYMBOLS, DATA_PATH

def fetch_data(symbol, period="1mo"):
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval="1d")
    return df

def update_local_data(name, symbol):
    file_path = f"{DATA_PATH}{name}.csv"

    new_data = fetch_data(symbol)

    if os.path.exists(file_path):
        old_data = pd.read_csv(file_path, index_col=0, parse_dates=True)
        df = pd.concat([old_data, new_data]).drop_duplicates()
    else:
        df = new_data

    df.to_csv(file_path)
    return df

def get_current_price(symbol):
    ticker = yf.Ticker(symbol)
    return ticker.history(period="1d")["Close"].iloc[-1]