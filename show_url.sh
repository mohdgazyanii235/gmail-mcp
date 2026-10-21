#!/usr/bin/env bash
# Prints the connector address to paste into Claude. It is not a secret: signing in needs your password and authenticator code.
cd "$(dirname "$0")"
set -a; . ./.env; set +a
echo "https://$PUBLIC_HOSTNAME/mcp"
