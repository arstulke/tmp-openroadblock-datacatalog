{
  description = "Generates a static WZDx feed registry (USDOT registry layout) into dist/";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    # Builds the Python dependencies from pyproject.toml and uv.lock.
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
    }:
    let
      inherit (nixpkgs) lib;

      forAllSystems = lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

      # Prefer the prebuilt wheels pinned in uv.lock over building from sdist.
      overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};

          pythonSet =
            (pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python313; }).overrideScope
              (
                lib.composeManyExtensions [
                  pyproject-build-systems.overlays.default
                  overlay
                ]
              );

          # Virtualenv with every dependency from uv.lock.
          venv = pythonSet.mkVirtualEnv "openroadblock-datacatalog-env" workspace.deps.default;
        in
        {
          default = pkgs.stdenvNoCC.mkDerivation {
            pname = "openroadblock-datacatalog";
            version = "0.1.0";

            # The script finds its templates/ and static/ next to itself.
            src = ./scripts;

            nativeBuildInputs = [ pkgs.makeWrapper ];

            installPhase = ''
              runHook preInstall
              mkdir -p $out/share/openroadblock-datacatalog $out/bin
              cp -r . $out/share/openroadblock-datacatalog/
              # git is needed at runtime to read the first commit date of each feed.
              makeWrapper ${venv}/bin/python $out/bin/build-catalog \
                --add-flags $out/share/openroadblock-datacatalog/build_catalog.py \
                --prefix PATH : ${lib.makeBinPath [ pkgs.git ]}
              runHook postInstall
            '';

            meta.mainProgram = "build-catalog";
          };
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = lib.getExe self.packages.${system}.default;
        };
      });
    };
}
