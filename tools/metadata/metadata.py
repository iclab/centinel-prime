import json
from datetime import datetime
import argparse
import geoip2.database
import cymru
from cloudflare import Cloudflare
import os
import hashlib




# get args
parser = argparse.ArgumentParser()
parser.add_argument("--is_vpn", action="store_true", help="Whether this run uses a VPN")
parser.add_argument("--vpn_provider", type=str)
parser.add_argument("--vpn_config", type=str)
parser.add_argument("--vpn_ip", type=str)
parser.add_argument("--ip", type=str, required=True)
parser.add_argument("--url_list", type=str, required=True)
parser.add_argument("--cloudflare_token", type=str, help="Cloudflare API token (optional)")



def validate_vpn_args(args):
    if args.is_vpn:
        if args.vpn_provider is None:
            parser.error("--vpn_provider is required when --is_vpn is True")
        if args.vpn_config is None:
            parser.error("--vpn_config is required when --is_vpn is True")
        if args.vpn_ip is None:
            parser.error("--vpn_ip is required when --is_vpn is True")

args = parser.parse_args()
validate_vpn_args(args)

DISABLE_CLOUDFLARE = None

CLOUDFLARE_TOKEN = args.cloudflare_token or os.environ.get("CLOUDFLARE_TOKEN")

if CLOUDFLARE_TOKEN is None:
    print("WARNING: No Cloudflare token provided - skipping Cloudflare lookups")
    DISABLE_CLOUDFLARE = True

if not os.path.isfile(f"{args.url_list}"):
    parser.error(f"ERROR: URL list file {args.url_list} does not exist")
    exit(1)


with open(f"{args.url_list}", "rb") as f:
    file_hash = hashlib.file_digest(f, "sha256").hexdigest()

metadata = {
    "is_vpn": args.is_vpn,
    "ip": args.ip,
    "timestamp": datetime.now().isoformat(),
    "url_list": args.url_list,
    "url_list_hash": file_hash,
}

hash_string = str(metadata["is_vpn"])+metadata["timestamp"]

IP_TO_LOOKUP = args.ip

if args.is_vpn:
    hash_string += args.vpn_provider+args.vpn_config

    import dns.resolver

    metadata["vpn_provider"] = args.vpn_provider
    metadata["vpn_config"] = args.vpn_config
    metadata["vpn_ip"] = args.vpn_ip
    IP_TO_LOOKUP = args.vpn_ip

    vpn_config_path = f"/vpns/{args.vpn_provider}/{args.vpn_config}"

    if not os.path.exists(vpn_config_path):
        print(f"ERROR: VPN config file {vpn_config_path} does not exist")
        exit(1)

    with open(vpn_config_path, "r") as f:
        vpn_config = f.readlines()

    for line in vpn_config:
        if line.startswith("remote"):
            metadata["vpn_connection_hostname"] = line.split(" ")[1]

    resolver = dns.resolver.Resolver()
    answers = resolver.resolve(metadata["vpn_connection_hostname"], 'A')
    metadata["vpn_entry_ip"] = answers[0].to_text()


metadata["session_id"] = hashlib.sha256(hash_string.encode()).hexdigest()


# add geoip from maxmind
with geoip2.database.Reader('/data/maxmind/GeoLite2-Country.mmdb') as reader:
    response = reader.country(IP_TO_LOOKUP)
    metadata["maxmind_country_code"] = response.country.iso_code

    if args.is_vpn:
       response = reader.country(metadata["vpn_entry_ip"])
       metadata["maxmind_vpn_entry_country_code"] = response.country.iso_code

with geoip2.database.Reader('/data/maxmind/GeoLite2-ASN.mmdb') as reader:
    response = reader.asn(IP_TO_LOOKUP)
    metadata["maxmind_asn"] = response.autonomous_system_number
    metadata["maxmind_asn_organization"] = response.autonomous_system_organization

    if args.is_vpn:
        response = reader.asn(metadata["vpn_entry_ip"])
        metadata["maxmind_vpn_entry_asn"] = int(response.autonomous_system_number)
        metadata["maxmind_vpn_entry_asn_organization"] = response.autonomous_system_organization

# add ASN from team cymru
try:
    cymru_asn, cymru_country_code, cymru_organization = cymru.get_cymru_info(IP_TO_LOOKUP)
    metadata["cymru_asn"] = int(cymru_asn)
    metadata["cymru_asn_country_code"] = cymru_country_code
    metadata["cymru_asn_organization"] = cymru_organization
except Exception as e:
    metadata["cymru_asn"] = None
    metadata["cymru_asn_country_code"] = None
    metadata["cymru_asn_organization"] = None

if args.is_vpn:
    try:
        cymru_asn, cymru_country_code, cymru_organization = cymru.get_cymru_info(metadata["vpn_entry_ip"])
        metadata["cymru_vpn_entry_asn"] = int(cymru_asn)
        metadata["cymru_vpn_entry_asn_country_code"] = cymru_country_code
        metadata["cymru_vpn_entry_asn_organization"] = cymru_organization
    except Exception as e:
        metadata["cymru_vpn_entry_asn"] = None
        metadata["cymru_vpn_entry_asn_country_code"] = None
        metadata["cymru_vpn_entry_asn_organization"] = None

# add data from cloudflare radar
if not DISABLE_CLOUDFLARE:
    cf = Cloudflare(
        api_token=CLOUDFLARE_TOKEN,
    )
    response = cf.radar.entities.get(
        ip=IP_TO_LOOKUP,
    )
    metadata["cloudflare_asn"] = int(response.ip.asn)
    metadata["cloudflare_asn_organization"] = response.ip.asnOrgName
    metadata["cloudflare_asn_country_code"] = response.ip.asnLocation
    metadata["cloudflare_country_code"] = response.ip.location

if args.is_vpn:
    response = cf.radar.entities.get(
        ip=metadata["vpn_entry_ip"],
    )
    metadata["cloudflare_vpn_entry_asn"] = int(response.ip.asn)
    metadata["cloudflare_vpn_entry_asn_organization"] = response.ip.asnOrgName
    metadata["cloudflare_vpn_entry_asn_country_code"] = response.ip.asnLocation
    metadata["cloudflare_vpn_entry_country_code"] = response.ip.location

print(metadata['session_id'])

with open("/metadata.json", "w") as f:
    json.dump(metadata, f, indent=4)

