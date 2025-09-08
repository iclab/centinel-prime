#!/bin/sh
# take filename as argument
filename=$1
max_parallel=100  # adjust this number based on your needs

# Check if GNU Parallel is installed using which
which parallel >/dev/null 2>&1
if [ $? -ne 0 ]; then
    echo "GNU Parallel is not installed. Please install it first."
    exit 1
fi

# Count total number of URLs
total_urls=$(wc -l < "$filename")
echo "Processing $total_urls URLs..."

# Process URLs in parallel and store response codes with progress bar
cat "$filename" | parallel --progress --bar -j "$max_parallel" \
    'eval "curl -s -o /dev/null {} -w \"{}\t%{http_code}\t$?\" 2>/dev/null"' >> response_codes.txt

echo "Done! Results saved in response_codes.txt"

