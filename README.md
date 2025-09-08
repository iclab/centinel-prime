# ICLab Containerization Effort

This repository contains the virtualization infrastructure for the ICLab Project. 
The goal is to provide isolated virtual machine environments for running network 
measurement experiments through VPN connections.

## How to run: 

`iclab.py run baseline`

## Requirements

- incus installed and configured to work on the machine 
- distrobuilder to make the custom image
- tested on cpython-3.13.1+freethreaded-linux-x86_64-gnu, installable via uv 
