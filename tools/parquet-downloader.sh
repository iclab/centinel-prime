#!/usr/bin/env bash
set -euo pipefail

# parquet-downloader.sh
# - Sets up a network namespace with veth + NAT (optionally via VPN tun0)
# - Captures traffic with tcpdump inside the namespace while curling a URL
# - Kills tcpdump, runs a Python script (default: ./test.py)
# - Cleans up created resources and deletes all netns per request

usage() {
  cat << 'EOF'
Usage: tools/parquet-downloader.sh --url URL [--vpn true|false] [--debug[=true|false]]

Options:
  -u, --url URL              Required. URL to fetch via curl (inside the netns)
  -v, --vpn true|false       Optional. If true, route via tun0; default: false
  -d, --debug[=true|false]   Optional. If true, drop into bash inside the netns and skip curl/python; default: false

Notes:
  - Requires root (for ip, iptables, netns, tcpdump)
  - Detects default egress interface unless --vpn true is set (then uses tun0)
  - Uses a random 192.168.X.0/24 subnet per run to avoid collisions across netns
  - All names and file paths are randomly generated per run
  - curl downloads are discarded to /dev/null
  - Deletes all netns at the end
EOF
}

err() { echo "[error] $*" >&2; }
log() { echo "[info] $*"; }


USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"

# Write data to parquet file
PY_SCRIPT="/experiments/parquet-writer/write_pcap.py"

require_root() {
  if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    err "This script must be run as root"; exit 1
  fi
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    err "Required command not found: $1"; exit 1
  fi
}

truncate_ifname() {
  local name="$1"
  if [ ${#name} -gt 15 ]; then
    echo "${name:0:15}"
  else
    echo "$name"
  fi
}

parse_args() {
  URL=""
  VPN=false
  DEBUG=false

  while [ $# -gt 0 ]; do
    case "$1" in
      -u|--url)
        URL="${2:-}"; shift 2;;
      -v|--vpn)
        # Accept forms: --vpn, --vpn true, --vpn false
        if [ $# -ge 2 ] && [[ ! "${2:-}" =~ ^- ]]; then
          val="${2}"
          shift 2
        else
          val="true"
          shift 1
        fi
        val_lc=$(echo "$val" | tr '[:upper:]' '[:lower:]')
        case "$val_lc" in
          true|1|yes) VPN=true ;;
          false|0|no) VPN=false ;;
          *) err "Invalid value for --vpn: $val (use true|false)"; exit 1 ;;
        esac
        ;;
      -d|--debug)
        # Accept forms: --debug, --debug true, --debug false
        if [ $# -ge 2 ] && [[ ! "${2:-}" =~ ^- ]]; then
          dval="${2}"
          shift 2
        else
          dval="true"
          shift 1
        fi
        dval_lc=$(echo "$dval" | tr '[:upper:]' '[:lower:]')
        case "$dval_lc" in
          true|1|yes) DEBUG=true ;;
          false|0|no) DEBUG=false ;;
          *) err "Invalid value for --debug: $dval (use true|false)"; exit 1 ;;
        esac
        ;;
      -h|--help)
        usage; exit 0;;
      *)
        err "Unknown argument: $1"; usage; exit 1;;
    esac
  done

  if [ -z "$URL" ]; then
    err "--url is required"; usage; exit 1
  fi
}

detect_default_if() {
  ip route show default 0.0.0.0/0 2>/dev/null | awk '/default/ {print $5; exit}'
}

ensure_ip_forward() {
  # enable IPv4 forwarding only if not already enabled
  local cur
  if [ -r /proc/sys/net/ipv4/ip_forward ]; then
    cur=$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || echo 0)
  else
    cur=$(sysctl -n net.ipv4.ip_forward 2>/dev/null || echo 0)
  fi
  if [ "$cur" != "1" ]; then
    log "Enabling IPv4 forwarding"
    sysctl -w net.ipv4.ip_forward=1 >/dev/null
  else
    log "IPv4 forwarding already enabled; skipping"
  fi
}

iptables_rule_exists() {
  local table="$1"; shift
  if [ -n "$table" ]; then
    iptables -t "$table" -C "$@" >/dev/null 2>&1
  else
    iptables -C "$@" >/dev/null 2>&1
  fi
}

iptables_ensure_rule() {
  local table="$1"; shift
  if iptables_rule_exists "$table" "$@"; then
    return 0
  fi
  if [ -n "$table" ]; then
    iptables -t "$table" -A "$@"
  else
    iptables -A "$@"
  fi
}

iptables_delete_rule_if_present() {
  local table="$1"; shift
  if iptables_rule_exists "$table" "$@"; then
    if [ -n "$table" ]; then
      iptables -t "$table" -D "$@" || true
    else
      iptables -D "$@" || true
    fi
  fi
}

cleanup_created() {
  set +e
  log "Cleaning up created resources"
  # remove iptables rules we added
  log "adding iptables rules"
  log $(date)
  iptables_delete_rule_if_present "" FORWARD -i "$OUT_IF" -o "$HOST_VETH" -j ACCEPT
  iptables_delete_rule_if_present "" FORWARD -o "$OUT_IF" -i "$HOST_VETH" -j ACCEPT
  iptables_delete_rule_if_present nat POSTROUTING -s "$SUBNET_CIDR" -o "$OUT_IF" -j MASQUERADE
  log "removing iptables rules"
  log $(date)
  # delete host veth (removes the pair)
  ip link del "$HOST_VETH" >/dev/null 2>&1

  # delete namespace if still present (but don't error if already deleted)
  if ip netns list | awk '{print $1}' | grep -qx "$NETNS" 2>/dev/null; then
    ip netns delete "$NETNS" >/dev/null 2>&1 || true
  fi

  set -e
}

delete_all_netns() {
  log "Deleting all network namespaces (requested)"
  local ns
  while read -r ns; do
    [ -z "$ns" ] && continue
    log "Deleting netns: $ns"
    ip netns delete "$ns" || true
  done < <(ip netns list | awk '{print $1}')
}

main() {
  require_root
  need_cmd ip; need_cmd iptables; need_cmd tcpdump; need_cmd curl; need_cmd awk; need_cmd sysctl; need_cmd python3
  parse_args "$@"

  # random identifiers for this run
  TS=$(date +%Y%m%d_%H%M%S)
  NETNS="netns_${TS}_$RANDOM"

  # derive names and addressing
  # generate distinct veth names within the 15-char kernel limit
  BASE_SUFFIX="${NETNS: -11}"
  HOST_VETH="v-${BASE_SUFFIX}-h"  # 2 + 11 + 2 = 15
  NS_VETH="v-${BASE_SUFFIX}-n"    # 2 + 11 + 2 = 15

  # Use thread ID for deterministic subnet allocation to avoid collisions
  # Get thread ID from process ID and use modulo to ensure unique subnets
  THREAD_ID=${BASHPID:-$$}
  THIRD_OCTET=$(( 10 + (THREAD_ID % 200) ))
  GW_IP="192.168.${THIRD_OCTET}.1"
  NS_IP="192.168.${THIRD_OCTET}.2"
  HOST_CIDR="${GW_IP}/24"
  NS_CIDR="${NS_IP}/24"
  SUBNET_CIDR="192.168.${THIRD_OCTET}.0/24"

  # default outputs (randomized)
  PCAP_FILE="/tmp/${NETNS}_${TS}.pcap"

  # choose egress interface
  if $VPN; then
    OUT_IF="tun0"
  else
    OUT_IF=$(detect_default_if)
    if [ -z "$OUT_IF" ]; then
      OUT_IF="eth0" # fallback
    fi
  fi

  if ! ip link show "$OUT_IF" >/dev/null 2>&1; then
    err "Egress interface not found: $OUT_IF"; exit 1
  fi

  ensure_ip_forward

  # do not clobber existing netns
  if ip netns list | awk '{print $1}' | grep -qx "$NETNS"; then
    err "Network namespace already exists: $NETNS"; exit 1
  fi

  trap cleanup_created EXIT

  log "Creating netns $NETNS with veth $HOST_VETH <-> $NS_VETH on $SUBNET_CIDR (egress: $OUT_IF)"

  ip netns add "$NETNS"
  ip link add "$HOST_VETH" type veth peer name "$NS_VETH"
  ip link set "$NS_VETH" netns "$NETNS"

  ip addr add "$HOST_CIDR" dev "$HOST_VETH"
  ip link set dev "$HOST_VETH" up

  ip -n "$NETNS" addr add "$NS_CIDR" dev "$NS_VETH"
  ip -n "$NETNS" link set lo up
  ip -n "$NETNS" link set dev "$NS_VETH" up

  # NAT and forward rules specific to this veth/subnet
  iptables_ensure_rule "" FORWARD -i "$OUT_IF" -o "$HOST_VETH" -j ACCEPT
  iptables_ensure_rule "" FORWARD -o "$OUT_IF" -i "$HOST_VETH" -j ACCEPT
  iptables_ensure_rule nat POSTROUTING -s "$SUBNET_CIDR" -o "$OUT_IF" -j MASQUERADE

  # default route inside netns
  ip -n "$NETNS" route add default via "$GW_IP"

  if $DEBUG; then
    log "Debug mode enabled. Dropping into bash inside $NETNS. Cleanup will occur on exit."
    ip netns exec "$NETNS" bash -l
    delete_all_netns
    log "Done (debug)."
    exit 0
  fi

  log "Starting tcpdump inside $NETNS -> $PCAP_FILE"
  ip netns exec "$NETNS" tcpdump -i any -w "$PCAP_FILE" -U >/tmp/"${NETNS}"_tcpdump.log 2>&1 &
  TCPDUMP_PID=$!
  sleep 1

  log "Curling URL inside $NETNS -> /dev/null"
  set +e
  # Generate a random filename for SSLKEYLOG
  SSLKEYLOG_FILE="/tmp/sslkeylog_${RANDOM}_$$.log"


  # Test DNS resolution for the target URL
  URL_HOST=$(echo "$URL" | sed -E 's|^https?://([^/]+).*|\1|')
  log "Testing DNS resolution for target host: $URL_HOST"
  ip netns exec "$NETNS" nslookup "$URL_HOST" >/dev/null 2>&1
  TARGET_DNS_STATUS=$?
  if [ $TARGET_DNS_STATUS -ne 0 ]; then
    log "Warning: DNS resolution for target host failed (nslookup $URL_HOST returned $TARGET_DNS_STATUS)"
  else
    log "DNS resolution for target host passed"
  fi

  # Try HTTP/2 first, fallback to HTTP/1.1 if not supported
  log "Attempting curl with HTTP/2..."
  ip netns exec "$NETNS" env SSLKEYLOGFILE="$SSLKEYLOG_FILE" curl \
    --http2 \
    --max-time 30 \
    --connect-timeout 20 \
    -A "$USER_AGENT" \
    "$URL" \
    -o /dev/null \
    -k \
    --ciphers "DEFAULT:!DH" \
    -v 2>/tmp/"${NETNS}"_curl_debug.log

  CURL_STATUS=$?
  log "CURL_STATUS: $CURL_STATUS"
  if [ $CURL_STATUS -eq 16 ] || [ $CURL_STATUS -eq 92 ]; then
    # 16 = CURLE_HTTP2 (unsupported protocol)
    # 92 = CURLE_HTTP2_PROTOCOL_ERROR (protocol error)
    log "HTTP/2 failed, trying HTTP/1.1..."
    ip netns exec "$NETNS" env SSLKEYLOGFILE="$SSLKEYLOG_FILE" curl \
      --http1.1 \
      --max-time 30 \
      --connect-timeout 20 \
      -A "$USER_AGENT" \
      "$URL" \
      -o /dev/null \
      -k \
      --ciphers "DEFAULT:!DH" \
      -v 2>/tmp/"${NETNS}"_curl_debug.log
    CURL_STATUS=$?
  fi

  if [ $CURL_STATUS -ne 0 ]; then
    err "curl failed with status $CURL_STATUS"
    log "curl debug output:"
    cat /tmp/"${NETNS}"_curl_debug.log >&2
    
    # Additional debugging for exit code 255
    if [ $CURL_STATUS -eq 255 ]; then
      log "Exit code 255 detected - checking if curl process was killed"
      log "Checking network namespace status:"
      ip netns list | grep "$NETNS" >&2 || log "Namespace not found in list" >&2
      log "Checking if tcpdump is still running:"
      ps aux | grep tcpdump | grep "$NETNS" >&2 || log "tcpdump not found in process list" >&2
    fi
  fi

  log "Stopping tcpdump (pid $TCPDUMP_PID)"
  kill "$TCPDUMP_PID" >/dev/null 2>&1 || true
  wait "$TCPDUMP_PID" 2>/dev/null || true

  log "Running python script: $PY_SCRIPT"
  if [ -f "$PY_SCRIPT" ]; then
    CURL_STATUS=$CURL_STATUS PCAP_FILE=$PCAP_FILE URL=$URL SSLKEYLOGFILE=$SSLKEYLOG_FILE python3 "$PY_SCRIPT"
  else
    err "Python script not found: $PY_SCRIPT (skipping)"
  fi

  log "Done. pcap: $PCAP_FILE"
}

main "$@"


