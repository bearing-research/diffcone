#!/bin/sh
# one-env.sh NAME: fixenv.sh NAME with its output in $E/logs/env-NAME.log.
: "${E:?set E to the evaluation work directory}"
mkdir -p "$E/logs"
"$(cd "$(dirname "$0")" && pwd)/fixenv.sh" "$1" > "$E/logs/env-$1.log" 2>&1
echo "$1 exit $?"
