{ libcamera, libssc, onnxruntime }:

libcamera.overrideAttrs (oldAttrs: {
  pname = "libcamera-alphaplus";

  buildInputs = (oldAttrs.buildInputs or [ ]) ++ [
    libssc
    onnxruntime
  ];

  mesonFlags = (oldAttrs.mesonFlags or [ ]) ++ [ "-Dpipelines=qcom,uvcvideo" "-Dipas=[]" "-Dandroid=disabled" ];

  postInstall = (oldAttrs.postInstall or "") + ''
    # wrapQtAppsHook wraps executable files found in libexec. Preserve the preload library.
    if [ -f "$out/libexec/libcamera/v4l2-compat.so" ]; then
      chmod a-x "$out/libexec/libcamera/v4l2-compat.so"
    fi
  '';

  patches = (oldAttrs.patches or [ ]) ++ [
    ./patches/0001-libcamera-v4l2-expose-device-ioctl-sequence-offset-a.patch
    ./patches/0002-libcamera-sensor-add-LG-G8-sensor-helpers-and-ancill.patch
    ./patches/0003-libcamera-add-draft-flash-controls.patch
    ./patches/0004-libcamera-qcom-add-factory-AE-AF-controllers-and-SSC.patch
    ./patches/0005-libcamera-qcom-add-QTI-front-AWB-engine-and-IMX520-c.patch
    ./patches/0006-libcamera-qcom-add-native-LG-rear-AWB-engine-and-EEP.patch
    ./patches/0007-libcamera-qcom-add-native-CAMSS-IFE-pipeline-handler.patch
    ./patches/0008-libcamera-qcom-add-asynchronous-FC4-AWB-inference-an.patch
    ./patches/0009-qcom-fix-capture-lifecycle-and-flash-controls.patch
    ./patches/0010-qcom-add-independent-exposure-and-gain-modes.patch
    ./patches/0011-qcom-report-awb-convergence.patch
    ./patches/0012-qcom-apply-scaler-crop-per-request.patch
    ./patches/0013-qcam-add-zoom-and-tap-focus.patch
    ./patches/0014-qcom-clear-sensor-test-pattern-on-stop.patch
    ./patches/0015-qcom-add-awb-mode-lux-auto-flicker-and-bf-roi.patch
    ./patches/0016-qcom-support-raw-and-dual-stream.patch
    ./patches/0017-qcom-support-dual-camera-vfe-concurrency.patch
    ./patches/0018-qcom-add-hardware-LSC-BPC-BCC-Demux-Gr-Gb-and-ColorS.patch
    ./patches/0019-qcom-enable-IMX351-16MP-Dual-IFE-full-resolution-ISP.patch
    ./patches/0020-qcom-add-hybrid-PDAF-CDAF-autofocus-for-IMX363-and-S.patch
    ./patches/0021-qcom-add-IRS2381C-ToF-depth-and-raw-camera-support.patch
    ./patches/0022-qcom-drive-dual-channel-flash-and-Still-Movie-Pan-Ti.patch
    ./patches/0023-qcom-add-hardware-FD501-face-detection-support.patch
    ./patches/0024-qcom-add-front-ToF-assisted-autofocus-for-IMX520.patch
    ./patches/0025-qcom-synchronize-main-flash-strobe-to-VFE-SOF.patch
    ./patches/0026-qcom-add-extended-4K60-and-120fps-sensor-modes.patch
    ./patches/0027-qcom-calibrate-exposure-start-timestamps-and-fc4-stability.patch
  ];
})
