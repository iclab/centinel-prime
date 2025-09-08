import json
import pyarrow as pa
import pyarrow.parquet as pq
import glob
import os

def merge_parquet_files():
    """Merge all thread-specific parquet files into a single output file."""
    
    # Find all thread-specific parquet files
    pattern = "/output_*.parquet"
    parquet_files = glob.glob(pattern)
    
    if not parquet_files:
        print("No thread-specific parquet files found")
        return
    
    print(f"Found {len(parquet_files)} thread-specific parquet files")
    
    # Read and combine all tables
    tables = []
    for file_path in parquet_files:
        try:
            table = pq.read_table(file_path)
            tables.append(table)
            print(f"Successfully read {file_path} with {len(table)} rows")
        except Exception as e:
            print(f"Error reading {file_path}: {e}")
            continue
    
    if not tables:
        print("No valid tables to merge")
        return
    
    # Concatenate all tables
    merged_table = pa.concat_tables(tables)
    print(f"Merged table has {len(merged_table)} total rows")

    # add custom metadata
    data = json.load(open("/metadata.json"))
    existing_metadata = merged_table.schema.metadata
    merged_metadata = {**existing_metadata, **{"custom_metadata": json.dumps(data.encode("utf-8"))}}
    merged_table = merged_table.replace_schema_metadata(merged_metadata)

    # Write merged table to final output
    pq.write_table(merged_table, "/output.parquet")
    print("Successfully wrote merged table to /output.parquet")
    
    # Clean up thread-specific files
    for file_path in parquet_files:
        try:
            os.remove(file_path)
            print(f"Removed {file_path}")
        except Exception as e:
            print(f"Error removing {file_path}: {e}")

if __name__ == "__main__":
    merge_parquet_files()
