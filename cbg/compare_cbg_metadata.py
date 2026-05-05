#!/usr/bin/env python3
"""
Compare CBG geolocation results with metadata from external sources.

Each data source (CBG, MaxMind, Cymru, Cloudflare) acts as a "voter" to build
confidence in the true VPN location. The script aggregates votes and identifies
consensus or disagreement patterns. 
"""

import os
import re
import csv
import ast
import math
import json
import glob
import argparse
from datetime import datetime
from collections import Counter

import reverse_geocoder as rg   # TODO: delete this module later

SIGMA = 500000 # for confidence score
WEIGHTS = {'cbg': 1.0,
           'cloudflare': 0.9,
           'maxmind': 0.8, 
           'cymru': 0.5}
 
def load_iso_mapping(iso_fpath: str) -> dict: #tuple[dict, dict]:
    """Load ISO 3166 country code mappings."""
    name_to_iso2 = {}
    # iso2_to_name = {}
    with open(iso_fpath, 'r', encoding='ISO-8859-1') as f:
        reader = csv.DictReader(f)
        for row in reader:
            iso2 = row['ISO_A2']
            name = row['NAME']
            name_to_iso2[name] = iso2
            # iso2_to_name[iso2] = name 
    return name_to_iso2 #, iso2_to_name 


def load_cbg_results(cbg_fpath: str) -> dict:
    """Load CBG results CSV into a dict keyed by vpn_ip."""
    results = {}
    with open(cbg_fpath, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            vpn_hostname = row['vpn_name'] 
            cnts = row.get('countries', '')
            if cnts.startswith('['): 
                try:
                    countries = ast.literal_eval(cnts)
                except (ValueError, SyntaxError):
                    countries = []
            else:
                countries = [cnts] if cnts and cnts != 'None' else []

            results[vpn_hostname] = {
                'countries': countries,
                'match': row.get('match', ''),
                'area_km2': float(row['area_km2']) if row.get('area_km2') and row['area_km2'] != 'None' else None,
                'center': row.get('center', '')
            }
    return results


def find_metadata_for_vpn(vpn_name: str, claimed_cc: str, pings_dir: str, date: datetime) -> dict:
    """Find metadata.json for a VPN by searching the pings directory."""
    # The metadata.json is in the same directory as the ping file
    pattern = f"{pings_dir}/*/pings_*_{claimed_cc}_{vpn_name}.ovpn.csv"
    matches = glob.glob(pattern) 

    if matches:
        # Get metadata from same directory as ping file
        ping_dir = os.path.dirname(matches[0])
        metadata_path = os.path.join(ping_dir, "metadata.json")
        if os.path.exists(metadata_path):
            with open(metadata_path, 'r') as f:
                return json.load(f)

    return None


def extract_claimed_country_from_name(vpn_name: str, ) -> str:
    """Extract claimed country code from VPN name.""" 
    # TODO: Handle different naming conventions per provider
    match = re.search(r'-([A-Z]{2})-', vpn_name)
    if match:
        return match.group(1)
    return ''


def get_cbg_vote(cbg_result: dict, claimed_cc: str, name_to_iso2: dict) -> tuple[dict, float]:
    """Get CBG's vote for the claimed country and confidence score for each country. """ 
        
    # TODO: Replace with center_country = cbg_result['center_country']
    lat, lon = ast.literal_eval(cbg_result['center'])
    result = rg.search((lat, lon))
    center_country = result[0]['cc']

    intersecting = set([name_to_iso2.get(ct, ct) for ct in cbg_result.get('countries', [])])
    
    n = len(intersecting)
    cs_cbg_per_country = {}

    if n > 0 and cbg_result.get("area_km2") is not None:
        for cc in intersecting:
            # ci = 1.0 if center is in this country, else 0.5
            # precision component (CBG geometry)
            ci = 1.0 if center_country == cc else 0.5
            cs_cbg = ci * (1.0/n) * math.exp(-cbg_result['area_km2'] / SIGMA)
            cs_cbg_per_country[cc] = cs_cbg 

    ci_cbg_for_claim = cs_cbg_per_country.get(claimed_cc, 0.0)
    
    return cs_cbg_per_country, ci_cbg_for_claim, center_country


def aggregate_votes(claimed_cc: str,
                    votes: dict, 
                    cs_cbg_per_country: dict,
                    ci_cbg_for_claim: float) -> dict:
    """Aggregate votes from all sources. 

    Returns:
        {   
            'votes_for_claim': count of how many sources agree with the claimed country,
            
            'conf_cbg': float (0-1),
            'conf_maxmind': float (0-1),
            'conf_cloudflare': float (0-1),
            'conf_cymru': float (0-1),
            'conf_total_claim': float (0-1)

            'conf_total_winner': normalized total float (0-1),
            'predicted_iso2': country code iso2 or None,
            'verdict': 'conflict', 'soft conflict' ,etc
        }
    """
    aggregation = { 'votes_for_claim': 0,
                  'cbg_votes': [],
                  'conf_cbg': 0.0,
                  'conf_maxmind': 0.0,
                  'conf_cloudflare': 0.0,
                  'conf_cymru': 0.0,
                  'conf_total_claim': 0.0,
                  'conf_total_winner': 0.0,
                  'predicted_iso2': None,
                  'verdict': None }
     
    valid_votes = {k: v for k, v in votes.items() if v} 

    all_candidates = set(list(cs_cbg_per_country.keys()))
    all_candidates.update([cc for cc in votes.values() if cc])
    all_candidates.add(claimed_cc)
 
    votes_for_claim = 0
    for cc in valid_votes.values():
        if cc == claimed_cc:
            votes_for_claim += 1
    # +1 for CBG if its highest confidence country is the claimed country
    if cs_cbg_per_country:
        max_conf = max(cs_cbg_per_country.values())
        top_countries = [cc for cc, conf in cs_cbg_per_country.items() if conf == max_conf]
        if claimed_cc in top_countries:
            votes_for_claim += 1
        aggregation['cbg_votes'] = top_countries    
    aggregation['votes_for_claim'] = votes_for_claim
   
    active_weights_sum = WEIGHTS['cbg'] + sum(WEIGHTS[s] for s in valid_votes.keys())

    conf_all = {} 

    for cc in all_candidates:
        s_cbg = cs_cbg_per_country.get(cc, 0.0)
        s_max = 1.0 if votes.get("maxmind") == cc else 0.0
        s_cf = 1.0 if votes.get("cloudflare") == cc else 0.0
        s_cym = 1.0 if votes.get("cymru") == cc else 0.0

        # weighted sum
        weighted_sum = (WEIGHTS['cbg'] * s_cbg) + \
                    (WEIGHTS['maxmind'] * s_max) + \
                    (WEIGHTS['cloudflare'] * s_cf) + \
                    (WEIGHTS['cymru'] * s_cym)
        
        # unified confidence score
        conf_all[cc] = weighted_sum / active_weights_sum
 
        if cc == claimed_cc:
            aggregation['conf_cbg'] = s_cbg
            aggregation['conf_maxmind'] = s_max
            aggregation['conf_cloudflare'] = s_cf
            aggregation['conf_cymru'] = s_cym

    # identify predicted winner, claimed score    
    predicted_iso2 = max(conf_all, key=conf_all.get)
    conf_total_winner = conf_all[predicted_iso2]
    conf_totla_claim = conf_all.get(claimed_cc, 0.0)

    # verdict logic
    if conf_total_winner < 0.4:
        verdict = "Uncertain"
    elif predicted_iso2 == claimed_cc:
        verdict = "Match" if conf_total_winner >= 0.8 else "Soft_Match"
    else:
        # disagreement
        if ci_cbg_for_claim == 0: verdict = "Conflict"
        else: verdict = "Soft Conflict"

    aggregation['predicted_iso2'] = predicted_iso2
    aggregation['conf_total'] = conf_total_winner
    aggregation['conf_claim'] = conf_totla_claim
    aggregation['verdict'] = verdict

    return aggregation


def process_vpn(vpn_name: str, 
                claimed_cc: str, 
                cbg_result: dict, 
                metadata: dict,
                name_to_iso2: dict) -> dict:
    """Process a single VPN and collect votes from all sources."""   
    votes = {}

    # CBG vote
    cs_cbg_per_country, ci_cbg_for_claim, center_country = get_cbg_vote(cbg_result, claimed_cc, name_to_iso2) 

    # Metadata votes  
    votes['maxmind'] = ''
    votes['cymru'] = ''
    votes['cloudflare'] = '' 

    if metadata:
        votes['maxmind'] = metadata.get('maxmind_country_code', '')
        votes['cymru'] = metadata.get('cymru_asn_country_code', '')
        votes['cloudflare'] = metadata.get('cloudflare_country_code', '')

    metadata_count = 1 + sum(1 for cc in [votes['maxmind'], votes['cymru'], votes['cloudflare']] if cc != '')
 
    aggregation = aggregate_votes(claimed_cc, votes, cs_cbg_per_country, ci_cbg_for_claim)
 
    result = {
        # raw data
        'vpn_name': vpn_name,
        'claimed_iso2': claimed_cc,
        'cbg_center_coords': cbg_result.get('center') if cbg_result else None,
        'cbg_center_country': center_country,     
        'cbg_area_km2': cbg_result.get('area_km2') if cbg_result else None,
        'cbg_overlap_count': len(cbg_result.get('countries', [])) if cbg_result else 0,     
        'cbg_match': cbg_result.get('match', '') if cbg_result else '',           
        # individual votes
        'cbg_top_iso2': aggregation['cbg_votes'],
        'maxmind_iso2': votes['maxmind'],
        'cymru_iso2': votes['cymru'],
        'cloudflare_iso2': votes['cloudflare'],
        # aggregated results
        'sources_retrieved': metadata_count,
        'votes_for_claim': aggregation['votes_for_claim'],
        'claim_support_ratio': aggregation['votes_for_claim'] / metadata_count,
        # weighted confidence score for the claimed country
        'conf_cbg': aggregation['conf_cbg'],        
        'conf_maxmind': aggregation['conf_maxmind'],    
        'conf_cloudflare': aggregation['conf_cloudflare'],  
        'conf_cymru': aggregation['conf_cymru'],       
        'conf_total_claim': aggregation['conf_claim'],
        # more   
        'conf_total_winner': aggregation['conf_total'],
        'predicted_iso2': aggregation['predicted_iso2'],     
        'verdict': aggregation['verdict']        
    }  
        
    return result
 

def run_comparison(cbg_results_fpath: str, 
                   pings_dir: str, 
                   date: datetime,
                   output_fpath: str, 
                   iso_file: str) -> list:
    """Run the comparison between CBG results and metadata sources."""
    print("Loading ISO country code mappings...")
    name_to_iso2 = load_iso_mapping(iso_file)
 
    print(f"Loading CBG results from {cbg_results_fpath}...")
    cbg_results = load_cbg_results(cbg_results_fpath)
    print(f"Found {len(cbg_results)} VPN results")

    print("Processing VPNs and collecting votes...")
    results = []
    for vpn_name, cbg_result in cbg_results.items():
        claimed_cc = extract_claimed_country_from_name(vpn_name)

        # Find corresponding metadata
        metadata = find_metadata_for_vpn(vpn_name, claimed_cc, pings_dir, date)

        # Process and collect votes
        result = process_vpn(vpn_name, claimed_cc, cbg_result, 
                             metadata, name_to_iso2)
        results.append(result) 
        
    print(f"\nWriting results to {output_fpath}...")
    output_columns = [
        'vpn_name',
        'claimed_iso2',
        'cbg_center_coords',
        'cbg_center_country',
        'cbg_area_km2',
        'cbg_overlap_count',    # The number of countries
        'cbg_match',            # "Yes", "Possibly", "No"
        'cbg_top_iso2',
        'maxmind_iso2',
        'cloudflare_iso2',
        'cymru_iso2',
        # aggregated results
        'sources_retrieved',
        'votes_for_claim',
        'claim_support_ratio',
        # weighted confidence score for the claimed country
        'conf_cbg',        # cf score for the caliemd country
        'conf_maxmind',   # 1 if match else 0 (* weight)
        'conf_cloudflare', # 1 if match else 0 (* weight)
        'conf_cymru',      # 1 if match else 0 (* weight)
        'conf_total_claim',  # normalize the sum of all scores for the claimed cc
        'conf_total_winner',
        'predicted_iso2',     # country that earns the highest weighted sum.
        'verdict',        # "Match": winner == claimed_cc AND total consensus is > 0.7
                          # "Conflict": winner != claimed_cc AND s_cbg > 0.4
                          # "Soft Conflict": winner != claimed_cc AND s_cbg is low
                          # "Uncertain": total_s < 0.4     all sources disagree.
        ] 
    
    with open(output_fpath, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=output_columns)
        writer.writeheader()
        for r in results: 
            row = dict(r)
            row['conf_total_claim'] = f"{r['conf_total_claim']:.2f}"
            row['conf_total_winner'] = f"{r['conf_total_winner']:.2f}"
            writer.writerow(row)

    print("Done!")
    return results

 
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--cbg-results', type=str, required=True,
                        help='Path to CBG results CSV file')
    ap.add_argument('--pings-dir', type=str, required=True,
                        help='Base directory containing ping files and metadata')
    ap.add_argument('--date', type=str, required=True,
                        help='Date in YYYY-MM-DD format')
    ap.add_argument('--iso-file', type=str,
                        default='cbg/data/raw/iso3166.csv',
                        help='Path to ISO 3166 country codes CSV')
    ap.add_argument('--output', type=str, required=True,
                        help='Output CSV file path')

    args = ap.parse_args()
 
    date = datetime.strptime(args.date, '%Y-%m-%d')
 
    run_comparison(
        cbg_results_fpath=args.cbg_results,
        pings_dir=args.pings_dir,
        date=date,
        output_fpath=args.output,
        iso_file=args.iso_file
    )


if __name__ == '__main__':
    main()
 