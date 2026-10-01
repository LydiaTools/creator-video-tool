#!/bin/zsh
set -euo pipefail
project_dir="${0:A:h}"
app_dir="$project_dir/dist/出海视频.app"
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources"
swiftc -O -parse-as-library "$project_dir"/Sources/*.swift -o "$app_dir/Contents/MacOS/CreatorVideo"
cp "$project_dir/Sources/video_tool.py" "$app_dir/Contents/Resources/video_tool.py"
cp "$project_dir/Sources/muse_batch.py" "$app_dir/Contents/Resources/muse_batch.py"
cp "$project_dir/Sources/volume_identity.py" "$app_dir/Contents/Resources/volume_identity.py"
cp "$project_dir/Info.plist" "$app_dir/Contents/Info.plist"
codesign --force --deep --sign - "$app_dir"
codesign --verify --deep --strict "$app_dir"
print "Built: $app_dir"
