#!/usr/bin/env bash
# Step 1 only: bash generate_wallets.sh 100
set -Eeuo pipefail
umask 077
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec bash "$SCRIPT_DIR/start.sh" generate "$@"
