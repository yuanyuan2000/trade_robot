from config import SYMBOLS
from data_fetcher import update_local_data, get_current_price
from plotter import plot_kline

def main():
    for name, symbol in SYMBOLS.items():
        print(f"\n=== {name} ===")

        # 当前价格
        price = get_current_price(symbol)
        print(f"当前价格: {price}")

        # 更新数据
        df = update_local_data(name, symbol)

        # 画图
        plot_kline(df.tail(30), title=f"{name} - Last 30 Days")

if __name__ == "__main__":
    main()