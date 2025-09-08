import pyarrow as pa
import pyarrow.parquet as pq
import json

metadata = json.load(open("/metadata.json"))

schema = pa.schema([
    pa.field("curl_status", pa.string(), nullable=False),
    pa.field("url", pa.string(), nullable=False),
    pa.field("pcap_data", pa.binary(), nullable=False),
    pa.field("sslkeylog_data", pa.binary(), nullable=False),
])

empty_table = pa.Table.from_arrays(
    [pa.array([], type=field.type) for field in schema],
    schema=schema,
)

pq.write_table(empty_table, "/output.parquet")