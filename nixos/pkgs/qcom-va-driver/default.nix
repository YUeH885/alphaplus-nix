{
  lib,
  stdenv,
  fetchFromGitHub,
  meson,
  ninja,
  pkg-config,
  libva,
  libdrm,
  libgbm,
  libglvnd,
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

  strictDeps = true;

  nativeBuildInputs = [
    meson
    ninja
    pkg-config
  ];

  buildInputs = [
    libva
    libdrm
    libgbm
    libglvnd
  ];

  mesonFlags = [
    "-Dcdsp_vpp=disabled"
    "-Dgpu_copy=enabled"
    "-Dvulkan_vpp=disabled"
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
