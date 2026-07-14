#!/bin/bash
# Miracast render-pipeline bisection for the "not-negotiated" failure.
#
# Run AS ROOT while a Windows source is CONNECTED and streaming:
#   sudo bash tools/diag_pipeline.sh
#
# Why this works: castd's own streaming pipeline has already died by the
# time you run this (that's the bug being diagnosed), so UDP port 1028 is
# free, the RTP stream is still flowing (the control-channel keep-alive
# holds the session open), and the DRM display is free because the idle
# screen only restarts on disconnect. Three pipeline stages run for 8
# seconds each under `timeout`; exit code 124 means the stage survived
# the full 8 s without an error -- that stage is GOOD.
set -u

CAPS='application/x-rtp,media=video,encoding-name=MP2T,payload=33,clock-rate=90000'
LOGDIR=/tmp/castd-diag
mkdir -p "$LOGDIR"

if pgrep -f idle_screen.png >/dev/null; then
    echo "WARNING: the idle-screen pipeline is holding the display, which"
    echo "means no source is currently streaming. Connect from Windows"
    echo "(Win+K) first, wait for it to show connected, then re-run."
    echo
fi

echo "=== RTP packets arriving on 1028 (3s sample) ==="
timeout 3 tcpdump -i p2p-wlan1-0 -n -c 3 udp port 1028 2>/dev/null \
    || echo "(none seen -- is the source connected and streaming?)"
echo

run_stage() {
    local name="$1"; shift
    echo "=== stage: $name ==="
    timeout 8 gst-launch-1.0 -v "$@" >"$LOGDIR/$name.log" 2>&1
    local rc=$?
    if [ "$rc" -eq 124 ]; then
        echo "exit=124  -> survived 8s, stage GOOD"
    else
        echo "exit=$rc  -> stage DIED"
    fi
    echo "--- errors/warnings ---"
    grep -E "ERROR|Internal data|not-negotiated|could not link|no element|WARN" "$LOGDIR/$name.log" | head -8
    echo "--- last negotiated caps ---"
    grep -E "caps = " "$LOGDIR/$name.log" | tail -12
    echo
}

# Stage 1: RTP front end only. If this dies, the problem is in the
# udpsrc caps / jitterbuffer / depayloader (e.g. wrong payload type).
run_stage front \
    udpsrc port=1028 ! "$CAPS" ! rtpjitterbuffer latency=200 ! rtpmp2tdepay ! fakesink

# Stage 2: video branch only, no audio. If stage 1 was GOOD and this
# dies, the -v caps above show exactly which link failed. If the TV
# shows the Windows desktop for 8 seconds, video works and the audio
# branch is the killer.
run_stage video \
    udpsrc port=1028 ! "$CAPS" ! rtpjitterbuffer latency=200 ! rtpmp2tdepay \
    ! tsdemux ! queue ! h264parse ! v4l2h264dec ! v4l2convert \
    ! kmssink driver-name=vc4 sync=false

# Stage 2b: the hardware decoder ALONE, output discarded. First bisection
# run showed h264parse negotiated fine (1024x768@60 high 4.2) and then
# nothing -- this stage separates "the decoder itself fails" from "the
# decoder-to-display link fails".
run_stage decode \
    udpsrc port=1028 ! "$CAPS" ! rtpjitterbuffer latency=200 ! rtpmp2tdepay \
    ! tsdemux ! queue ! h264parse ! v4l2h264dec ! fakesink

# Stage 2c: SOFTWARE decode all the way to the display. If the TV shows
# the Windows desktop here, the kmssink display path is fine and the
# hardware decoder (or its output negotiation) is the sole problem.
run_stage swvideo \
    udpsrc port=1028 ! "$CAPS" ! rtpjitterbuffer latency=200 ! rtpmp2tdepay \
    ! tsdemux ! queue ! h264parse ! avdec_h264 ! videoconvert \
    ! kmssink driver-name=vc4 sync=false

# Stage 3: the exact pipeline castd runs, audio branch included.
run_stage full \
    udpsrc port=1028 ! "$CAPS" ! rtpjitterbuffer latency=200 ! rtpmp2tdepay \
    ! tsdemux name=demux \
    demux. ! queue ! h264parse ! v4l2h264dec ! v4l2convert \
    ! kmssink driver-name=vc4 sync=false \
    demux. ! queue ! aacparse ! avdec_aac ! audioconvert ! audioresample ! alsasink

echo "full logs kept in $LOGDIR/*.log"
