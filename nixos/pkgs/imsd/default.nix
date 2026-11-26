{ lib
, stdenv
, fetchgit
, pkg-config
, makeWrapper
, glib
, opencore-amr
, vo-amrwbenc
}:

stdenv.mkDerivation {
  pname = "imsd";
  version = "1.2.4";

  src = fetchgit {
    url = "https://forgejo.catcrafts.net/Catcrafts/imsd.git";
    rev = "d48c1017dad95f8623a92c0dfcc5968f956c52f6";
    hash = "sha256-GWHXLzlhvNEnGoTNXw4KT6KvMaCZDqFqrfqjsxWBedU=";
  };

  patches = [
    ./patches/ims-profile-requests-pcscf.patch
    ./patches/aka-resynchronize-with-auts.patch
    ./patches/ims-pani-uses-serving-duplex.patch
    ./patches/aka-honors-challenge-qop.patch
    ./patches/reinvite-handles-sdp-negotiation.patch
  ];

  nativeBuildInputs = [ pkg-config makeWrapper ];
  buildInputs = [ glib ];
  strictDeps = true;

  buildPhase = ''
    runHook preBuild
    cxxFlags="-std=c++23 -fmodules -O2"
    gioFlags="$("$PKG_CONFIG" --cflags gio-2.0)"
    gioLibs="$("$PKG_CONFIG" --libs gio-2.0)"
    "$CXX" $cxxFlags -x c++ -c ${stdenv.cc.cc}/include/c++/${stdenv.cc.cc.version}/bits/std.cc -o std.o
    for module in Imsd-Util Imsd-Aka Imsd-Ipsec Imsd-Qmi Imsd-Sip Imsd-Sdp Imsd-Sms Imsd-Messages Imsd-Engine Imsd ImsdBus; do
      "$CXX" $cxxFlags $gioFlags -x c++ -c "interfaces/$module.cppm" -o "$module.o"
    done
    for program in main media dialerd pcscf; do
      "$CXX" $cxxFlags $gioFlags -c "implementations/$program.cpp" -o "$program.o"
    done
    "$CXX" -o imsd main.o Imsd*.o std.o $gioLibs
    "$CXX" -o imsd-pcscf pcscf.o Imsd-*.o Imsd.o std.o
    "$CXX" -o imsd-media media.o std.o
    "$CXX" -o imsd-dialerd dialerd.o std.o $gioLibs
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    install -D -m755 -t "$out/bin" imsd imsd-dialerd imsd-media imsd-pcscf
    install -Dm755 packaging/ims-pdn-up.sh "$out/libexec/ims-pdn-up"
    install -Dm644 packaging/net.catcrafts.IMS1.conf "$out/share/dbus-1/system.d/net.catcrafts.IMS1.conf"
    install -Dm644 packaging/net.catcrafts.IMS1.policy "$out/share/polkit-1/actions/net.catcrafts.IMS1.policy"
    wrapProgram "$out/bin/imsd-media" \
      --prefix LD_LIBRARY_PATH : "${lib.makeLibraryPath [ opencore-amr vo-amrwbenc ]}"
    runHook postInstall
  '';

  meta = {
    description = "Userspace IMS and VoLTE for Qualcomm modems";
    homepage = "https://forgejo.catcrafts.net/Catcrafts/imsd";
    license = lib.licenses.gpl3Only;
    mainProgram = "imsd";
    platforms = lib.platforms.linux;
  };
}
