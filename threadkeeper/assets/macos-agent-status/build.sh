#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

app_name="ThreadKeeperAgentStatus"
build_dir="$PWD/build"
app_dir="$build_dir/$app_name.app"
bin_dir="$app_dir/Contents/MacOS"

rm -rf "$app_dir"
mkdir -p "$bin_dir"
cp Info.plist "$app_dir/Contents/Info.plist"

arch="$(uname -m)"
target="${arch}-apple-macos13.0"

swiftc \
  -O \
  -parse-as-library \
  -target "$target" \
  -framework SwiftUI \
  -framework AppKit \
  ThreadKeeperAgentStatus.swift \
  -o "$bin_dir/$app_name"

echo "$app_dir"
