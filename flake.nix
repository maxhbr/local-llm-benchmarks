{
  description = "Local LLM Benchmarks - unified runners for aider, llama-benchy, terminal-bench and agent_bench";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/e9a7635a57597d9754eccebdfc7045e6c8600e6b";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      lib = pkgs.lib;

      pyEnv = pkgs.python3.withPackages (ps: with ps; [ openai pydantic ]);

      # Sources used by the bash drivers.  We assemble a tiny derivation
      # that contains the shell scripts plus lib/ and the python script,
      # so the wrappers below can reference stable paths in the store.
      src = pkgs.runCommand "local-llm-benchmarks-src" { } ''
        mkdir -p $out/lib
        cp ${./lib/common.sh}                    $out/lib/common.sh
        cp ${./aider-polyglot-benchmarks.sh}     $out/aider-polyglot-benchmarks.sh
        cp ${./llama-benchy-benchmarks.sh}       $out/llama-benchy-benchmarks.sh
        cp ${./terminal-bench-benchmarks.sh}     $out/terminal-bench-benchmarks.sh
        cp ${./agent_bench.py}                   $out/agent_bench.py
        cp ${./run_benchmarks.py}                $out/run_benchmarks.py
        chmod +x $out/*.sh $out/*.py
      '';

      commonRuntime = with pkgs; [ bash coreutils curl jq git ];

      # llama-benchy installs pre-built wheels (numpy, tokenizers, ...) that
      # need libstdc++ and libz from a stable LD path.  Export the same
      # path the devShell uses so the wrappers work outside `nix develop`.
      benchLdLibraryPath = lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib pkgs.zlib ];

      # Wrap a bash benchmark script with the right PATH.  Each script
      # uses its own $SCRIPT_DIR to locate lib/common.sh; we point it at
      # the shared src/ derivation above.
      mkBenchScript = { name, srcFile, runtimeInputs }:
        pkgs.writeShellApplication {
          inherit name;
          runtimeInputs = runtimeInputs ++ commonRuntime;
          text = ''
            export LD_LIBRARY_PATH=${benchLdLibraryPath}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
            exec ${pkgs.bash}/bin/bash ${src}/${srcFile} "$@"
          '';
        };

      aider-bench = mkBenchScript {
        name = "aider-bench";
        srcFile = "aider-polyglot-benchmarks.sh";
        runtimeInputs = with pkgs; [ podman docker ];
      };

      llama-benchy-bench = mkBenchScript {
        name = "llama-benchy-bench";
        srcFile = "llama-benchy-benchmarks.sh";
        # llama-benchy needs uv + python at runtime to build/run its venv.
        runtimeInputs = with pkgs; [ uv python3 ];
      };

      terminal-bench = mkBenchScript {
        name = "terminal-bench";
        srcFile = "terminal-bench-benchmarks.sh";
        runtimeInputs = with pkgs; [ uv python3 ];
      };

      agent-bench = pkgs.writeShellApplication {
        name = "agent-bench";
        runtimeInputs = commonRuntime;
        text = ''
          exec ${pyEnv}/bin/python ${src}/agent_bench.py "$@"
        '';
      };

      # Single entrypoint: reads a TOML matrix and dispatches to the four
      # per-benchmark wrappers.  Needs python3 >= 3.11 for stdlib `tomllib`.
      run-benchmarks = pkgs.writeShellApplication {
        name = "run-benchmarks";
        runtimeInputs = commonRuntime ++ [
          pkgs.python3
          aider-bench
          llama-benchy-bench
          terminal-bench
          agent-bench
        ];
        text = ''
          exec ${pkgs.python3}/bin/python3 ${src}/run_benchmarks.py "$@"
        '';
      };
    in
    {
      packages.${system} = {
        inherit aider-bench llama-benchy-bench terminal-bench agent-bench run-benchmarks;
        default = run-benchmarks;
      };

      apps.${system} = {
        aider-bench        = { type = "app"; program = "${aider-bench}/bin/aider-bench"; };
        llama-benchy-bench = { type = "app"; program = "${llama-benchy-bench}/bin/llama-benchy-bench"; };
        terminal-bench     = { type = "app"; program = "${terminal-bench}/bin/terminal-bench"; };
        agent-bench        = { type = "app"; program = "${agent-bench}/bin/agent-bench"; };
        run-benchmarks     = { type = "app"; program = "${run-benchmarks}/bin/run-benchmarks"; };
        default            = { type = "app"; program = "${run-benchmarks}/bin/run-benchmarks"; };
      };

      devShells.${system}.default = pkgs.mkShell {
        packages = commonRuntime ++ (with pkgs; [
          uv
          python3
          pyEnv
          podman
          docker
          entr
        ]);
        # llama-benchy installs pre-built wheels (numpy, tokenizers, ...) that
        # need libstdc++ and libz from a stable LD path.
        shellHook = ''
          export LD_LIBRARY_PATH=${benchLdLibraryPath}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
        '';
      };
    };
}
