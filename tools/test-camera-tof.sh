#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

output=${1:-/tmp/tof.raw}
media=${MEDIA_DEVICE:-/dev/media0}
sensor=$(media-ctl -d "$media" -p | sed -n 's/^- entity [0-9]*: \(irs2381c [^ ]*\) (.*/\1/p')
video=$(media-ctl -d "$media" -e msm_vfe3_video0)

media-ctl -d "$media" -l '"msm_csiphy3":1 -> "msm_csid3":0 [1]'
media-ctl -d "$media" -l '"msm_csid3":1 -> "msm_vfe3_rdi0":0 [1]'
for pad in "\"$sensor\":0" '"msm_csiphy3":0' '"msm_csiphy3":1' \
           '"msm_csid3":0' '"msm_csid3":1' '"msm_vfe3_rdi0":0'; do
    media-ctl -d "$media" -V "$pad [fmt:SBGGR12_1X12/224x1903 field:none]"
done
v4l2-ctl -d "$video" --set-fmt-video=width=224,height=1903,pixelformat=pBCC

log=$(mktemp)
trap 'rm -f -- "$log"' EXIT
timeout -s INT -k 3 15 v4l2-ctl -d "$video" --stream-mmap --stream-poll \
    --stream-count=1 --stream-to="$output" --verbose 2>&1 | tee "$log"

# v4l2-ctl can return zero after STREAMON fails or no buffer completes.
awk '
    /cap dqbuf:/ {
        frames++
        for (i = 1; i <= NF; i++)
            if ($i == "bytesused:") size = $(i + 1)
    }
    END {
        if (frames != 1 || size != 639408) {
            printf "FAIL: frames=%d bytesused=%d\n", frames, size > "/dev/stderr"
            exit 1
        }
    }
' "$log"
size=$(stat -c %s -- "$output")
if [[ $size != 639408 ]]; then
    echo "FAIL: output has $size bytes, expected 639408" >&2
    exit 1
fi
echo "PASS: one frame, 639408 bytes ($output)"
