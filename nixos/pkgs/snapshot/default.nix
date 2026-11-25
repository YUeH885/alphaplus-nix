{ snapshot }:

snapshot.overrideAttrs (old: {
  pname = "snapshot-alphaplus";
  patches = (old.patches or [ ]) ++ [ ./camera-orientation.patch ];
})
