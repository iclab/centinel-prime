# ICLab Experiment Documentation

This document provides comprehensive documentation for using the ICLab experiment infrastructure with Incus virtualization.

## Table of Contents
1. [Prerequisites](#prerequisites)
2. [System Setup](#system-setup)
3. [Environment Configuration](#environment-configuration)
4. [Running Experiments](#running-experiments)
5. [Creating New Experiments](#creating-new-experiments)
6. [Incus Helper Functions](#incus-helper-functions)
7. [Troubleshooting](#troubleshooting)

## Prerequisites

Before running experiments, ensure you have:
- Incus installed and configured on your system
- Python 3.x installed
- Distrobuilder installed (for custom image creation)
- Required Python packages installed
- Proper network access for VPN connections
- The VPN ovpn files placed in the correct format in the `vpns` folder, and the entry
    made into the `vpn_location_mapping.py` file.

## System Setup

1. **Install Dependencies**:
   ```bash
   # Install Incus
   # Follow Incus installation instructions for your OS

   # Install Python dependencies
   pip install -r requirements.txt
   ```

2. **Build the ICLab Image**:
   ```bash
   cd infra/incus
   sudo ./build-image.sh
   ```

## Environment Configuration

1. Create a `.env` file in the root directory with the following variables:
   ```
   CLOUDFLARE_TOKEN=your_cloudflare_token
   MAXMIND_USER_ID=your_maxmind_user_id
   MAXMIND_LICENSE_KEY=your_maxmind_license_key
   ```

2. The environment variables will be automatically loaded when running experiments.

## Running Experiments

### Basic Usage

1. **List Available Experiments**:
   ```bash
   ./iclab.py list experiments
   ```

2. **List Available VPNs**:
   ```bash
   ./iclab.py list vpn
   ```

3. **Run an Experiment**:
   ```bash
   ./iclab.py run experiment_name [args]
   ```

### Example: Running a Baseline Experiment
```bash
./iclab.py run baseline
```

## Creating New Experiments

To create a new experiment, you'll need to:

1. Create a new experiment function in your code
2. Register it in the `experiment_mapping` dictionary
3. Use the `incus_helper` functions to manage VM instances

### Example Experiment Structure:
```python
def my_experiment(url_file_name: str, is_vpn: bool, vpn_provider: str = None, location: str = None, config: str = None):
    instance_name = f"iclab-exp-{is_vpn}-{vpn_provider}-{location}"
    
    try:
        # Start an instance
        incus_helper.start_instance(instance_name)
        
        # Run your experiment commands
        incus_helper.execute_command("your_command", instance_name)
        
        # Collect results
        incus_helper.pull_file(instance_name, "/path/to/results", "local_results")
        
        # Cleanup
        incus_helper.stop_and_remove_instance(instance_name)
        
    except Exception as e:
        print(f"Error in experiment: {str(e)}")
        incus_helper.stop_and_remove_instance(instance_name)
        raise
```

## Incus Helper Functions

The `incus_helper` module provides several key functions for managing VM instances. Here's a detailed guide on each function and their important considerations:

### Instance Management

```python
def start_instance(instance_name: str, image_name: str = "iclab-alpine", optional_args: dict = None)
```
Starts a new Incus VM instance.

**Parameters:**
- `instance_name`: Name for the new instance
- `image_name`: Base image to use (defaults to "iclab-alpine")
- `optional_args`: Dictionary of additional Incus configuration options

**Gotchas:**
- The function waits for the network adapter to be ready before returning
- There's a 5-second sleep after network detection to ensure VM agent is fully running
- If an instance with the same name exists, the function will return without creating a new one
- The function assumes the image exists - will fail if image is not present

```python
def stop_and_remove_instance(instance_name: str)
```
Stops and removes an Incus instance.

**Gotchas:**
- Function attempts both operations even if one fails
- Does not verify if instance exists before attempting to stop
- Errors during stop/delete are logged but don't raise exceptions
- No timeout mechanism for hanging instances

```python
def check_for_instance(instance_name: str) -> bool
```
Checks if an instance exists.

**Gotchas:**
- Only checks instance existence, not its state (running/stopped)
- Uses subprocess, so may fail if Incus daemon is not responding

### Command Execution

```python
def execute_command(command: str, instance_name: str, env: dict = {}, login_override: bool = False)
```
Executes a command in an instance and waits for completion.

**Parameters:**
- `command`: Command to execute
- `instance_name`: Target instance name
- `env`: Additional environment variables
- `login_override`: Use `-l` instead of `--login` for shell invocation

**Gotchas:**
- Always runs commands through a shell with login
- Automatically includes SSLKEYLOGFILE environment variable
- Loads additional environment variables from .env file
- For Ubuntu instances, set `login_override=True` due to dash shell limitations

```python
def execute_command_in_background(command: str, instance_name: str, env: dict = {})
```
Executes a command in background mode.

**Gotchas:**
- Returns a Popen object - you must manage the process lifecycle
- No built-in timeout mechanism
- Environment variables don't work the same as in `execute_command`
- Useful for long-running commands like tcpdump, but requires manual cleanup

### File Operations

```python
def pull_file(instance_name: str, file_path: str, local_path: str)
```
Copies a file from an instance to the local system.

**Gotchas:**
- No automatic directory creation - ensure local directory exists
- Fails silently if source file doesn't exist
- No progress indication for large files
- Error messages are printed but not raised as exceptions

### System Checks

```python
def check_incus_installed() -> bool
```
Verifies if Incus is installed and accessible.

**Gotchas:**
- Only checks command existence, not daemon status
- Doesn't verify if user has proper permissions
- No version compatibility check

### Best Practices

1. **Error Handling**
   ```python
   try:
       incus_helper.start_instance(instance_name)
       # Run your commands
   except Exception as e:
       # Always cleanup in exception handler
       incus_helper.stop_and_remove_instance(instance_name)
       raise
   ```

2. **Background Command Management**
   ```python
   # Start background process
   process = incus_helper.execute_command_in_background("tcpdump -w /tmp/capture.pcap", instance_name)
   try:
       # Do other work
       # ...
   finally:
       # Cleanup background process
       incus_helper.execute_command("killall tcpdump", instance_name)
   ```

3. **Environment Variables**
   ```python
   # Proper way to set environment variables
   env_vars = {
       "CUSTOM_VAR": "value",
       "ANOTHER_VAR": "value2"
   }
   incus_helper.execute_command("your_command", instance_name, env=env_vars)
   ```

### Common Patterns

1. **Waiting for VPN Connection**
<!-- TODO -->

2. **Safe File Transfer**
<!-- TODO -->

## Troubleshooting

### Common Issues

1. **VPN Connection Failures**
   - Check VPN credentials in the configuration
   - Verify network connectivity
   - Check VPN server status

2. **Instance Creation Failures**
   - Verify Incus is running
   - Check system resources
   - Ensure image exists

3. **File Transfer Issues**
   - Verify file paths
   - Check permissions
   - Ensure sufficient disk space

### Debug Mode

Enable debug mode by setting the `DEBUG` environment variable:
```bash
export DEBUG=true
```

This will provide more detailed output during experiment execution.
