{
  description = "Local LLM Benchmarks - Harbor benchmarking script";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/e9a7635a57597d9754eccebdfc7045e6c8600e6b";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
    in
    {
      packages.${system} = {
        default = pkgs.symlinkJoin {
          name = "llm-benchmarks-tools";
          paths = with pkgs; [
            uv
            python3
            docker
          ];
        };
      };

      devShells.${system} = {
        default = pkgs.mkShell {
          packages = with pkgs; [
            uv
            python3
            docker
          ];
        };
      };
    };
}
