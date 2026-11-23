#!/usr/bin/env bash
set -euo pipefail

mkdir -p /data/logs /data/db /tmp/hems_cache
exec python3 -m hems.main
