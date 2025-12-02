{
  description = "SNCF Max - Automated TGV Max trip discovery and booking";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        
        python = pkgs.python312;
        
        pythonPackages = python.pkgs;
        
        # Python dependencies
        pythonDeps = with pythonPackages; [
          # Core
          requests
          
          # CLI
          click
          rich
          tabulate
          
          # Browser automation (for booking)
          playwright
          
          # Dev tools
          pytest
          pytest-asyncio
          black
          mypy
        ];
        
        # The sncf-max package
        sncf-max = pythonPackages.buildPythonPackage {
          pname = "sncf-max";
          version = "0.1.0";
          src = ./.;
          format = "pyproject";
          
          nativeBuildInputs = with pythonPackages; [
            setuptools
            wheel
          ];
          
          propagatedBuildInputs = pythonDeps;
          
          doCheck = false;
        };
        
      in {
        # Development shell
        devShells.default = pkgs.mkShell {
          buildInputs = [
            (python.withPackages (ps: pythonDeps))
            pkgs.playwright-driver.browsers
          ];
          
          shellHook = ''
            export PYTHONPATH="$PWD/src:$PYTHONPATH"
            export PLAYWRIGHT_BROWSERS_PATH="${pkgs.playwright-driver.browsers}"
            export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=true
            
            echo "🚄 SNCF Max Development Environment"
            echo ""
            echo "Commands:"
            echo "  python -m sncf_max.cli --help    Run CLI"
            echo "  python example.py                Run example"
            echo "  pytest                           Run tests"
            echo ""
          '';
        };
        
        # The package
        packages = {
          default = sncf-max;
          sncf-max = sncf-max;
        };
        
        # App entry point
        apps.default = {
          type = "app";
          program = "${sncf-max}/bin/sncf-max";
        };
      }
    );
}

