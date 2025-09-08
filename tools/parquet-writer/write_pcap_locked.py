import pyarrow as pa
import pyarrow.parquet as pq
import os
import fcntl
import time

curl_status = os.environ.get('CURL_STATUS')
pcap_file = os.environ.get('PCAP_FILE')
url = os.environ.get('URL')
sslkeylogfile = os.environ.get('SSLKEYLOGFILE')

with open(pcap_file, 'rb') as f:
    pcap_data = f.read()

with open(sslkeylogfile, 'rb') as f:
    sslkeylog_data = f.read()

# Use file locking to prevent concurrent writes
lock_file = "/output.parquet.lock"
output_file = "/output.parquet"

# Acquire lock
with open(lock_file, 'w') as lock_f:
    fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
    
    try:
        # Try to read existing table
        try:
            parquet_table = pq.read_table(output_file)
            existing_batches = parquet_table.to_batches()
        except (FileNotFoundError, OSError):
            # File doesn't exist or is corrupted, start fresh
            existing_batches = []
        
        # Create new batch
        new_batch = pa.RecordBatch.from_pydict({
            'curl_status': [curl_status],
            'url': [url],
            'pcap_data': [pcap_data],
            'sslkeylog_data': [sslkeylog_data],
        })
        
        # Combine with existing table
        if existing_batches:
            new_table = pa.Table.from_batches([existing_batches[0], new_batch])
        else:
            new_table = pa.Table.from_batches([new_batch])
        
        # Write to parquet
        pq.write_table(new_table, output_file)
        
    finally:
        # Release lock
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
