import pandas as pd
import os

def preprocess_csv(file_path):
    """
    Cleans raw Binance aggTrades CSV files for Ma-Chao Equation numerical integration.
    """
    column_names = [
        'agg_trade_id', 'price', 'quantity', 
        'first_id', 'last_id', 'timestamp', 'is_buyer_maker'
    ]
    
    print(f"Reading: {file_path}")
    df = pd.read_csv(file_path, names=column_names)
    
    # Transform to datetime and keep physical features
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df = df[['timestamp', 'price', 'quantity']]
    
    # Export to Parquet for high-speed I/O performance
    output_path = file_path.replace(".csv", "_cleaned.parquet")
    df.to_parquet(output_path, compression='snappy')
    print(f"Exported: {output_path}")

if __name__ == "__main__":
    target_dir = "data_pipeline"
    for file in os.listdir(target_dir):
        if file.endswith(".csv"):
            preprocess_csv(os.path.join(target_dir, file))
