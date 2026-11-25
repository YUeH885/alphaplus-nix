{ lib
, stdenv
, fetchFromGitHub
, meson
, ninja
, pkg-config
}:

stdenv.mkDerivation {
  pname = "hexagonrpc";
  version = "0.4.0";

  src = fetchFromGitHub {
    owner = "linux-msm";
    repo = "hexagonrpc";
    rev = "v0.4.0";
    hash = "sha256-OC6wXBCIW4XznWG0zzxRK3BzWMVK2Jq/gTL36sJV1PE=";
  };

  patches = [
    ./patches/0001-fix-file-descriptor-lifetime.patch
    ./patches/0002-support-writable-sensor-registry.patch
    ./patches/0003-receive-large-input-buffers.patch
    ./patches/0004-support-atomic-registry-updates.patch
    ./patches/0005-support-unsigned-shell-domains.patch
    ./patches/0006-supervise-client-sessions.patch
    ./patches/0007-preserve-64-bit-heap-addresses.patch
  ];

  nativeBuildInputs = [
    meson
    ninja
    pkg-config
  ];

  meta = with lib; {
    description = "FastRPC userspace daemon and client libraries for Qualcomm Hexagon DSP";
    homepage = "https://github.com/linux-msm/hexagonrpc";
    license = licenses.bsd3;
    platforms = platforms.linux;
  };
}
