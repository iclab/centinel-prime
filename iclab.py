#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import sys
import os
from utilities.experiment_mapping import experiment_mapping
from utilities.vpn_location_mapping import vpn_locations
from utilities.incus_helper import check_incus_installed

def setup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='ICLab')
    subparsers = parser.add_subparsers(dest='command', required=True)

    # List command
    list_parser = subparsers.add_parser('list', help='List available experiments or VPNs')
    list_parser.add_argument('item', choices=['experiments', 'vpn'], 
                            help='Item to list (experiments or vpn)')

    # Run command
    run_parser = subparsers.add_parser('run', help='Run an experiment')
    run_parser.add_argument('experiment', choices=list(experiment_mapping.keys()),
                           help='Name of the experiment to run')
    # Add experiment-specific arguments
    run_parser.add_argument('args', nargs='*', 
                           help='Additional arguments for the experiment')

    return parser

def main() -> None:
    parser = setup_parser()
    args = parser.parse_args()

    match args.command:
        case "list":
            match args.item:
                case "experiments":
                    print("Available experiments:")
                    for experiment in experiment_mapping.keys():
                        print(experiment)
                case "vpn":
                    print("Available VPNs:")
                    for vpn in vpn_locations.keys():
                        print(vpn)
        case "run":
            # Check if Incus is installed
            if not check_incus_installed():
                print("Error: Incus is not installed or not in the system PATH.")
                print("Please install Incus and make sure it's accessible from the command line.")
                sys.exit(1)
            
            # Load env file and then run exp 
            with open(".env", "r") as f:
                env_vars = f.readlines()
                for line in env_vars:
                    key, value = line.split("=")
                    os.environ[key] = value.strip()
            
            # Pass additional arguments to the experiment function
            experiment_mapping[args.experiment](*args.args)

if __name__ == "__main__":
    main()
