#!/bin/sh
# The HyperlightPython driver's create_command: ClickHouse runs it with the function's signature as arguments
exec clickhouse-hyperlight create "$@"
