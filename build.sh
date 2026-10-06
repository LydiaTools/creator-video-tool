#!/bin/zsh
set -euo pipefail
project_dir="${0:A:h}"
app_dir="$project_dir/dist/出海视频.app"
mkdir -p "$project_dir/dist"
staging_dir="$(mktemp -d "$project_dir/dist/.build-XXXXXX")"
staged_app="$staging_dir/出海视频.app"
mkdir -p "$staged_app/Contents/MacOS" "$staged_app/Contents/Resources"
swiftc -O -parse-as-library "$project_dir"/Sources/*.swift -o "$staged_app/Contents/MacOS/CreatorVideo"
for module in video_tool muse_batch volume_identity production stable3_timing; do
  cp "$project_dir/Sources/$module.py" "$staged_app/Contents/Resources/$module.py"
done
cp "$project_dir/Info.plist" "$staged_app/Contents/Info.plist"
codesign --force --deep --sign - "$staged_app"
codesign --verify --deep --strict "$staged_app"
# Activate only a fully built and signed bundle; retain the prior app for recovery.
if [[ -d "$app_dir" ]]; then
  mv "$app_dir" "$project_dir/dist/previous-${staging_dir:t}.app"
fi
mv "$staged_app" "$app_dir"
rmdir "$staging_dir"
print "Built: $app_dir"
