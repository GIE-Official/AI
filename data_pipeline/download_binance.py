import os
import requests
import zipfile
from datetime import datetime, timedelta

def download_batch_data(symbols, start_date, end_date):
    """
    Automated downloader for Binance aggTrades across multiple assets and dates.
    Aligns with the Ma-Chao Equation theoretical framework.
    """
    base_url = "https://data.binance.vision/data/spot/daily/aggTrades"
    
    if not os.path.exists("data_pipeline"):
        os.makedirs("data_pipeline")

    current_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    while current_dt <= end_dt:
        date_str = current_dt.strftime("%Y-%m-%d")
        
        for symbol in symbols:
            file_name = f"{symbol}-aggTrades-{date_str}.zip"
            url = f"{base_url}/{symbol}/{file_name}"
            save_path = os.path.join("data_pipeline", f"raw_{file_name}")

            print(f"Targeting: {symbol} | Date: {date_str}")
            
            try:
                response = requests.get(url, stream=True, timeout=10)
                if response.status_code == 200:
                    with open(save_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=1024):
                            f.write(chunk)
                    
                    # Unzip and clean up
                    with zipfile.ZipFile(save_path, 'r') as zip_ref:
                        zip_ref.extractall("data_pipeline/")
                    os.remove(save_path)
                    print(f"Status: Success -> {symbol} {date_str}")
                else:
                    print(f"Status: Skipped (HTTP {response.status_code})")
            except Exception as e:
                print(f"Status: Error -> {str(e)}")
        
        current_dt += timedelta(days=1)

if __name__ == "__main__":
    # ==========================================================================
    # Configuration: Multi-Asset Scalability
    # ==========================================================================
    assets = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    begin = "2024-08-01"
    finish = "2024-08-07"
    
    download_batch_data(assets, begin, finish)
