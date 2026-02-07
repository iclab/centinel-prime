import pyarrow.parquet as pq
import pandas as pd
import json
import os
import sys
import base64
from pathlib import Path

def _read_custom_metadata(file_path):
    """Read custom metadata from parquet schema metadata."""
    try:
        metadata = pq.ParquetFile(file_path).schema_arrow.metadata or {}
        raw = metadata.get(b"custom_metadata")
        if not raw:
            return {}
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)
    except Exception as e:
        print(f"Error reading custom metadata: {e}")
        return {}

def print_data_structure(file_path):
    """Print the structure of the parquet file"""
    try:
        # Read metadata first
        parquet_file = pq.ParquetFile(file_path)
        print("=== PARQUET FILE STRUCTURE ===")
        print(f"Schema: {parquet_file.schema}")
        print(f"Number of rows: {parquet_file.metadata.num_rows}")
        print(f"Number of columns: {parquet_file.metadata.num_columns}")
        print(f"Number of row groups: {parquet_file.num_row_groups}")
        
        # Read a sample to see column names and types
        df = pd.read_parquet(file_path)
        print(f"\nColumns: {list(df.columns)}")
        print(f"Data types:\n{df.dtypes}")
        print(f"Shape: {df.shape}")
        
        return df
    except Exception as e:
        print(f"Error reading parquet file: {e}")
        return None

def create_row_folders(df, output_dir="output_rows", base_metadata=None):
    """Create folders for each row with separated data"""
    try:
        if base_metadata is None:
            base_metadata = {}
        # Create output directory
        Path(output_dir).mkdir(exist_ok=True)
        
        # Check if required columns exist
        has_pcap = 'pcap_data' in df.columns
        has_sslkeylogfile = 'sslkeylog_data' in df.columns
        
        print(f"\n=== CREATING ROW FOLDERS ===")
        print(f"Output directory: {output_dir}")
        print(f"Has pcap column: {has_pcap}")
        print(f"Has sslkeylogfile column: {has_sslkeylogfile}")
        
        for index, row in df.iterrows():
            # Create folder for this row
            row_folder = Path(output_dir) / f"row_{index}"
            row_folder.mkdir(exist_ok=True)
            
            print(f"Processing row {index} -> {row_folder}")
            
            # Write pcap file if column exists
            if has_pcap and pd.notna(row['pcap_data']):
                pcap_path = row_folder / "data.pcap"
                try:
                    # Handle both string and bytes data
                    pcap_raw_data = row['pcap_data']
                    
                    if isinstance(pcap_raw_data, bytes):
                        # Data is already bytes, decode as base64
                        pcap_b64_data = pcap_raw_data.decode('utf-8')
                    else:
                        # Data is string
                        pcap_b64_data = str(pcap_raw_data)
                    
                    # Fix base64 padding if needed
                    missing_padding = len(pcap_b64_data) % 4
                    if missing_padding:
                        pcap_b64_data += '=' * (4 - missing_padding)
                    
                    pcap_data = base64.b64decode(pcap_b64_data)
                    with open(pcap_path, 'wb') as f:
                        f.write(pcap_data)
                    print(f"  - Wrote pcap file: {pcap_path} ({len(pcap_data)} bytes)")
                except Exception as e:
                    print(f"  - Error decoding pcap data: {e}")
                    # Try to write raw data if base64 decoding fails
                    try:
                        if isinstance(pcap_raw_data, bytes):
                            with open(pcap_path, 'wb') as f:
                                f.write(pcap_raw_data)
                        else:
                            with open(pcap_path, 'wb') as f:
                                f.write(str(pcap_raw_data).encode('utf-8'))
                        print(f"  - Wrote pcap file as raw data: {pcap_path}")
                    except Exception as e2:
                        print(f"  - Failed to write pcap file: {e2}")
            
            # Write sslkeylogfile if column exists
            if has_sslkeylogfile and pd.notna(row['sslkeylog_data']):
                ssl_path = row_folder / "sslkeylogfile.txt"
                try:
                    # Handle both string and bytes data
                    ssl_raw_data = row['sslkeylog_data']
                    
                    if isinstance(ssl_raw_data, bytes):
                        # Data is already bytes, decode as base64
                        ssl_b64_data = ssl_raw_data.decode('utf-8')
                    else:
                        # Data is string
                        ssl_b64_data = str(ssl_raw_data)
                    
                    # Fix base64 padding if needed
                    missing_padding = len(ssl_b64_data) % 4
                    if missing_padding:
                        ssl_b64_data += '=' * (4 - missing_padding)
                    
                    ssl_data = base64.b64decode(ssl_b64_data).decode('utf-8')
                    with open(ssl_path, 'w') as f:
                        f.write(ssl_data)
                    print(f"  - Wrote sslkeylogfile: {ssl_path} ({len(ssl_data)} characters)")
                except Exception as e:
                    print(f"  - Error decoding sslkeylogfile data: {e}")
                    # Try to write the raw data as text if base64 decoding fails
                    try:
                        if isinstance(ssl_raw_data, bytes):
                            with open(ssl_path, 'w') as f:
                                f.write(ssl_raw_data.decode('utf-8', errors='replace'))
                        else:
                            with open(ssl_path, 'w') as f:
                                f.write(str(ssl_raw_data))
                        print(f"  - Wrote sslkeylogfile as raw text: {ssl_path}")
                    except Exception as e2:
                        print(f"  - Failed to write sslkeylogfile: {e2}")
            
            # Create JSON with remaining data
            row_data = row.to_dict()
            
            # Remove binary data from JSON (convert to placeholder)
            json_data = {}
            for key, value in row_data.items():
                if key in ['pcap_data', 'sslkeylog_data']:
                    # Skip binary columns, they're written as separate files
                    continue
                elif isinstance(value, bytes):
                    # Convert bytes to base64 or skip
                    json_data[key] = f"<binary_data_{len(value)}_bytes>"
                else:
                    json_data[key] = value
            
            # Merge base (file-level) metadata with row-level metadata
            merged_metadata = dict(base_metadata)
            merged_metadata.update(json_data)

            json_path = row_folder / "metadata.json"
            with open(json_path, 'w') as f:
                json.dump(merged_metadata, f, indent=2, default=str)
            print(f"  - Wrote metadata: {json_path}")
            
    except Exception as e:
        print(f"Error creating row folders: {e}")

if __name__ == "__main__":
    # Get file path from command line or use default
    if len(sys.argv) > 1:
        file_path = sys.argv[1]
    else:
        print("Usage: python main.py <path_to_parquet_file>")
        sys.exit(1)
        
    if not os.path.exists(file_path):
        print(f"File not found: {file_path}")
        print("Usage: python main.py <path_to_parquet_file>")
        sys.exit(1)
    
    print(f"Reading parquet file: {file_path}")
    
    # Print structure
    df = print_data_structure(file_path)
    base_metadata = _read_custom_metadata(file_path)
    
    if df is not None:
        # Create row folders
        create_row_folders(df, base_metadata=base_metadata)