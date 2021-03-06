{
  pkgsPath ? <nixpkgs>,
  pkgs ? import pkgsPath {},
  pythonPackages ? pkgs.python3Packages,
}:

let
  inherit (builtins) replaceStrings readFile;
  version = replaceStrings ["\n"] [""] (readFile ./version.txt);
  rtyaml = pythonPackages.buildPythonPackage {
    name = "rtyaml-0.0.3";
    src = pkgs.fetchurl {
      url = "https://pypi.python.org/packages/ba/35/d17851c3a79b52379739b71182da24ac29a4cb3f3c2d02ee975c9625db4b/rtyaml-0.0.3.tar.gz";
      sha256 = "0f7d5n3hs0by9rjl9pzkigdr21ml3q8kpd45c302cjm2i9xy2i45";
    };
    propagatedBuildInputs = [pythonPackages.pyyaml];
  };
  isPy3 = pythonPackages.isPy3k or false;
in

pythonPackages.buildPythonPackage rec {
  name = "pynix-${version}";
  buildInputs = with pythonPackages; [ipython nose mock];
  propagatedBuildInputs = with pythonPackages; [
    pkgs.coreutils
    pkgs.gzip
    pkgs.nix.out
    pkgs.pv
    pkgs.which
    flask
    requests
    ipdb
    six
    datadiff
    rtyaml
    python_magic
  ] ++ (if isPy3 then [] else [
    pythonPackages.futures
    pythonPackages.backports_lzma
    pythonPackages.repoze_lru
  ]);
  checkPhase = ''
    # HACK: try to detect this failure case at runtime
    if ! nix-store -q --hash ${pkgs.nix} >/dev/null 2>&1; then
      export NIX_REMOTE=daemon
    fi
    nosetests tests
  '';
  src = ./.;
  makeWrapperArgs = [
    "--set NIX_BIN_PATH ${pkgs.lib.makeBinPath [pkgs.nix.out]}"
  ];
}
