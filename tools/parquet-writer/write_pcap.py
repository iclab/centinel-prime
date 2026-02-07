import pyarrow as pa
import pyarrow.parquet as pq
import os
import threading
import base64

curl_status = os.environ.get('CURL_STATUS')
pcap_file = os.environ.get('PCAP_FILE')
url = os.environ.get('URL')
sslkeylogfile = os.environ.get('SSLKEYLOGFILE')

# Use PID + thread ID to create unique filename across processes
thread_id = threading.get_ident()
output_file = f"/output_{os.getpid()}_{thread_id}.parquet"

# check if file exists
if not os.path.exists(pcap_file):
    print(f"PCAP file {pcap_file} does not exist")
    pcap_data = b''
else: 
    with open(pcap_file, 'rb') as f:
        pcap_data = f.read()

# convert the pcap data to base64
pcap_data = base64.b64encode(pcap_data).decode('utf-8')
    
if not os.path.exists(sslkeylogfile):
    print(f"SSL keylog file {sslkeylogfile} does not exist")
    sslkeylog_data = b''
else:
    with open(sslkeylogfile, 'rb') as f:
        sslkeylog_data = f.read()

# convert the sslkeylog data to base64
sslkeylog_data = base64.b64encode(sslkeylog_data).decode('utf-8')

# Create new table for this thread's data
new_batch = pa.RecordBatch.from_pydict({
    'curl_status': [curl_status],
    'url': [url],
    'pcap_data': [pcap_data],
    'sslkeylog_data': [sslkeylog_data],
})

# Write to thread-specific file
new_table = pa.Table.from_batches([new_batch])
pq.write_table(new_table, output_file)


