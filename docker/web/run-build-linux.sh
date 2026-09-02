#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: run-build-linux <build_linux.sh> [arguments...]" >&2
    exit 2
fi

script_path=$(readlink -f "$1")
shift

if [[ ! -f "$script_path" ]]; then
    echo "build script not found: $script_path" >&2
    exit 2
fi

# Windows checkouts commonly expose CRLF files through Docker Desktop bind
# mounts. Execute normalized content while preserving $0 so build_linux.sh
# still resolves the repository root from its original location. Its sourced
# distribution helper is redirected to a normalized temporary copy as well.
normalized_linux_dir=$(mktemp -d)
trap 'rm -rf "$normalized_linux_dir"' EXIT

linux_dir=$(dirname "$script_path")/scripts/linux.d
while IFS= read -r -d '' helper_path; do
    sed 's/\r$//' "$helper_path" > "$normalized_linux_dir/$(basename "$helper_path")"
done < <(find "$linux_dir" -maxdepth 1 -type f -print0)

source_line='source "./scripts/linux.d/${DISTRIBUTION}"'
replacement_line='source "${ORCA_NORMALIZED_LINUX_D}/${DISTRIBUTION}"'
normalized_script=$(sed 's/\r$//' "$script_path")

if [[ "$normalized_script" != *"$source_line"* ]]; then
    echo "expected distribution source line was not found in $script_path" >&2
    exit 2
fi

normalized_script=${normalized_script/"$source_line"/"$replacement_line"}
export ORCA_NORMALIZED_LINUX_D="$normalized_linux_dir"

bash -c "$normalized_script" "$script_path" "$@"
