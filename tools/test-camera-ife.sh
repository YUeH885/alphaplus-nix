#!/usr/bin/env bash
set -euo pipefail
export LC_ALL=C

mode=${1:-pix}
width=${2:-2016}
height=${3:-1136}
count=${4:-120}
output=${5:-/dev/null}
config_test=${IFE_CONFIG_TEST:-}
config_profile=${IFE_CONFIG_PROFILE:-factory}

if [[ $# -gt 5 || ! $width =~ ^[0-9]+$ || ! $height =~ ^[0-9]+$ ||
      ! $count =~ ^[0-9]+$ ]] || (( count < 2 )); then
    echo "Usage: $0 [pix|raw|dual|dual-raw] [WIDTH] [HEIGHT] [FRAMES>=2] [OUTPUT]" >&2
    exit 1
fi

case $mode in
    pix|dual)
        primary=pix
        secondary=raw
        ;;
    raw|dual-raw)
        primary=raw
        secondary=pix
        ;;
    *)
        echo "Unknown path: $mode" >&2
        exit 1
        ;;
esac

if [[ $mode == dual* ]] && (( count < 8 )); then
    echo "Dual capture requires at least 8 frames" >&2
    exit 1
fi

media-ctl -r
sensor_entity=$(media-ctl -p | sed -n 's/^- entity [0-9]*: \(s5k3m5 [0-9]*-[0-9a-f]*\) (.*/\1/p')
media-ctl -l "\"$sensor_entity\":0 -> \"video-mux\":0 [1]"
media-ctl -l '"msm_csiphy2":1 -> "msm_csid0":0 [1]'
for pad in "\"$sensor_entity\":0" '"video-mux":0' '"video-mux":2' '"msm_csiphy2":0' \
           '"msm_csid0":0'; do
    media-ctl -V "$pad [fmt:SGBRG10_1X10/${width}x${height} field:none]"
done
sensor=$(media-ctl -e "$sensor_entity")
v4l2-ctl -d "$sensor" --set-ctrl=exposure=1000,analogue_gain=256,test_pattern=0

setup_output()
{
    local path=$1 line port index format source node
    case $path in
        pix)
            line=msm_vfe0_pix
            port=4
            index=3
            format=NV12
            source=YUYV8_1_5X8
            ;;
        raw)
            line=msm_vfe0_rdi0
            port=1
            index=0
            format=pGAA
            source=SGBRG10_1X10
            ;;
    esac
    node=$(media-ctl -e "msm_vfe0_video$index")
    media-ctl -l "\"msm_csid0\":$port -> \"$line\":0 [1]"
    for pad in "\"msm_csid0\":$port" "\"$line\":0"; do
        media-ctl -V "$pad [fmt:SGBRG10_1X10/${width}x${height} field:none]"
    done
    media-ctl -V "\"$line\":1 [fmt:$source/${width}x${height} field:none]"
    v4l2-ctl -d "$node" --set-fmt-video=width="$width",height="$height",pixelformat="$format"
    v4l2-ctl -d "$node" --get-fmt-video
    if [[ $path == "$primary" ]]; then
        video=$node
        config_line=$line
    else
        second_video=$node
    fi
}

setup_output "$primary"
if [[ $mode == dual* ]]; then
    setup_output "$secondary"
fi

work=$(mktemp -d)
log=$work/primary
config_pid=
capture_pid=
cleanup()
{
    if [[ -n $capture_pid ]]; then
        kill "$capture_pid" 2>/dev/null || true
        wait "$capture_pid" 2>/dev/null || true
    fi
    if [[ -n $config_pid ]]; then
        kill "$config_pid" 2>/dev/null || true
        wait "$config_pid" 2>/dev/null || true
    fi
    rm -rf -- "$work"
}
trap cleanup EXIT
if [[ -n $config_test ]]; then
    "$config_test" "$(media-ctl -e "$config_line")" "$count" \
        "$config_profile" &
    config_pid=$!
fi
capture()
(
    exec stdbuf -oL v4l2-ctl -d "$1" --stream-mmap=6 --stream-poll \
        --stream-count="$2" --stream-to="$3" --verbose
)

if [[ $mode == dual* ]]; then
    second_count=$(( count / 4 ))
    capture "$video" "$count" "$output" > "$log" 2>&1 &
    capture_pid=$!
    for _ in {1..100}; do
        if grep -q 'cap dqbuf:' "$log"; then break; fi
        if ! kill -0 "$capture_pid" 2>/dev/null; then break; fi
        sleep 0.02
    done
    if ! grep -q 'cap dqbuf:' "$log"; then
        cat "$log" >&2
        echo "$primary capture did not start" >&2
        exit 1
    fi
    for round in 1 2; do
        capture "$second_video" "$second_count" /dev/null 2>&1 | tee "$work/second-$round"
    done
    primary_rc=0
    wait "$capture_pid" || primary_rc=$?
    capture_pid=
    cat "$log"
    if (( primary_rc != 0 )); then
        exit "$primary_rc"
    fi
else
    capture "$video" "$count" "$output" 2>&1 | tee "$log"
fi
if [[ -n $config_pid ]]; then
    wait "$config_pid"
    config_pid=
fi

validate_capture()
{
    local log=$1 expected=$2
    # v4l2-ctl can return success after a select timeout without a completed frame.
    awk -v expected="$expected" -v timing="$log.timing" '
    /cap dqbuf:/ {
        for (i = 1; i <= NF; i++) {
            if ($i == "seq:") seq = $(i + 1)
            if ($i == "bytesused:") size = $(i + 1)
            if ($i == "ts:") stamp = $(i + 1)
        }
        if (frames && seq != previous + 1) gaps++
        if (!frames) {
            first = stamp
            frame_size = size
        }
        if (size != frame_size || !size) bad_size++
        previous = seq
        last = stamp
        frames++
    }
    END {
        if (frames != expected || gaps || bad_size || last <= first) {
            printf "FAIL: frames=%d expected=%d sequence_gaps=%d bad_sizes=%d\n", \
                frames, expected, gaps, bad_size > "/dev/stderr"
            exit 1
        }
        printf "PASS: %d frames, %d bytes/frame, %.3f fps\n", \
            frames, frame_size, (frames - 1) / (last - first)
        printf "%.6f %.6f\n", first, last > timing
    }
' "$log"
}

validate_capture "$log" "$count"
if [[ $mode == dual* ]]; then
    for round in 1 2; do
        validate_capture "$work/second-$round" "$second_count"
    done
    awk -v first="$primary" -v second="$secondary" '
        NR == 1 { start = $1; end = $2; stopped = start; next }
        {
            if ($1 <= stopped || $2 >= end) failed = 1
            stopped = $2
        }
        END {
            if (failed || NR != 3 || end - stopped < (end - start) / 4) {
                printf "FAIL: %s did not run before and after both %s captures\n", first, second > "/dev/stderr"
                exit 1
            }
            printf "PASS: %s ran %.3f s after the last %s frame\n", first, end - stopped, second
        }
    ' "$log.timing" "$work/second-1.timing" "$work/second-2.timing"
fi
