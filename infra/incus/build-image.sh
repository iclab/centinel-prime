#!/bin/bash

# check for sudo permissions
if [ "$EUID" -ne 0 ]
  then echo "Please run as root"
  exit
fi

# Source environment variables
if [ -f ../../.env ]; then
  export $(cat ../../.env | xargs)
else
  echo ".env file not found"
  echo "Please create a .env file using the .env.sample file as a reference"
  exit 1
fi

# check if distrobuilder is installed
if ! command -v distrobuilder &> /dev/null
then
  echo "distrobuilder could not be found, please install and try again"
  exit
fi

auto_confirm=false
while [[ $# -gt 0 ]]; do
  case $1 in
    -y|--yes)
      auto_confirm=true
      echo "Auto-confirm set, proceeding with build - image will be overwritten"
      shift
      ;;
    *)
      shift
      ;;
  esac
done

# check if any vm is using the image
if incus list | grep -q 'iclab-alpine'; then
  echo "Warning: There are still VMs using the existing iclab-alpine image"
  echo "Existing VMs will not be affected, but new VMs will use the new image"
  read -p "Press enter to continue"
fi


# Show warning and ask for confirmation unless auto-confirm is set
if [ "$auto_confirm" = false ]; then
  echo "Warning: This will overwrite the existing iclab-alpine image"
  read -p "Press enter to continue"
fi

# remove iclab-alpine if it exists
if incus image list | grep -q 'iclab-alpine'; then
  # check if image exists in incus
  if incus image list | grep -q 'iclab-alpine'; then
    echo "Removing existing iclab-alpine image"
    incus image delete iclab-alpine
  fi
fi

envsubst '${MAXMIND_USER_ID},${MAXMIND_LICENSE_KEY},${CLOUDFLARE_TOKEN}' < alpine.yaml > alpine.yaml.output
# build the image
sudo distrobuilder build-incus ./alpine.yaml.output --import-into-incus='iclab-alpine' --type unified --vm

# remove build artifacts
sudo rm *.tar.xz alpine.yaml.output

echo "Image built successfully"
