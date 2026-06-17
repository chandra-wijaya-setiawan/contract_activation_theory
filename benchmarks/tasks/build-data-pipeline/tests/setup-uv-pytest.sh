#!/bin/bash

apt-get update
apt-get install -y curl

curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env

if [ "$PWD" = "/" ]; then
    echo "Error: No working directory set."
    exit 1
fi

uv init
uv add pytest pandas
