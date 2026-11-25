{
  buildPackages,
  expat,
  libdisplay-info,
  libdrm,
  libx11,
  libxcb,
  libxcb-keysyms,
  libxrandr,
  libxshmfence,
  mesa,
  pkg-config,
  stdenv,
  udev,
  wayland,
  wayland-protocols,
  zlib,
  zstd,
}:

stdenv.mkDerivation {
  pname = "turnip-alphaplus";
  inherit (mesa) version src;

  patches = [ ./a640-iova-alignment.patch ];
  strictDeps = true;

  depsBuildBuild = [ buildPackages.stdenv.cc buildPackages.pkg-config ];
  nativeBuildInputs = [ pkg-config ] ++ (with buildPackages; [
    bison
    flex
    glslang
    meson
    ninja
    wayland-scanner
    (python3.withPackages (p: [
      p.lxml
      p.mako
      p.packaging
      p.pyyaml
    ]))
  ]);
  buildInputs = [
    expat
    libdisplay-info
    libdrm
    libx11
    libxcb
    libxcb-keysyms
    libxrandr
    libxshmfence
    udev
    wayland
    wayland-protocols
    zlib
    zstd
  ];

  mesonFlags = [
    "-Dauto_features=disabled"
    "-Dplatforms=x11,wayland"
    "-Dgallium-drivers="
    "-Dopengl=false"
    "-Dvulkan-drivers=freedreno"
    "-Dfreedreno-kmds=msm"
    "-Ddisplay-info=enabled"
    "-Dexpat=enabled"
    "-Dshader-cache=enabled"
    "-Dxlib-lease=enabled"
    "-Dzlib=enabled"
    "-Dzstd=enabled"
    "-Db_ndebug=true"
  ];

  postPatch = ''
    patchShebangs --build src
  '';
}
