{ ffmpeg_7 }:

ffmpeg_7.overrideAttrs (oldAttrs: {
  pname = "ffmpeg-vpu";

  patches = (oldAttrs.patches or [ ]) ++ [
    ./patches/0001-v4l2-p010-drm-prime.patch
    ./patches/0002-v4l2-format-lifecycle-encoding.patch
  ];
})
