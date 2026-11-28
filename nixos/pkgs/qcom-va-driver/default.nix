{
  lib,
  stdenv,
  buildPackages,
  fetchFromGitHub,
  meson,
  ninja,
  pkg-config,
  libva,
  libdrm,
  libgbm,
  libglvnd,
  vulkan-headers,
  vulkan-loader,
}:

stdenv.mkDerivation {
  pname = "qcom-va-driver";
  version = "1.1.1-unstable-2026-11-25";

  src = fetchFromGitHub {
    owner = "radxa-pkg";
    repo = "qcom-va-driver";
    rev = "ee8622c18e59288f0c042e492f3de7ab1f0a27ef";
    hash = "sha256-RFtxyomnichk1ifcbc3xd862gDE6YOTZLrSvpOaiQXQ=";
  };

  patches = [
    ./vulkan-vpp-32bit-ssbo.patch
    ./decoder-drc.patch
    ./encoder-frame-timestamps.patch
    ./hevc-main10-encode.patch
  ];

  strictDeps = true;

  nativeBuildInputs = [
    meson
    ninja
    pkg-config
    buildPackages.glslang
  ];

  buildInputs = [
    libva
    libdrm
    libgbm
    libglvnd
    vulkan-headers
    vulkan-loader
  ];

  mesonFlags = [
    "-Dcdsp_vpp=disabled"
    "-Dgpu_copy=enabled"
    "-Dvulkan_vpp=enabled"
    "-Dvpp_backend=auto"
    "-Dv4l2_symlink=true"
    "-Dtests=false"
  ];

  meta = {
    description = "VA-API backend for Qualcomm Iris via Linux V4L2 stateful M2M";
    homepage = "https://github.com/radxa-pkg/qcom-va-driver";
    license = lib.licenses.mit;
    platforms = [ "aarch64-linux" ];
  };
}
