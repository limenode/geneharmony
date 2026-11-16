# Contributing to geneharmony

Thanks for your interest in contributing! Bug reports, feature requests, and pull requests are all welcome.

## Reporting issues

Please open an issue on [GitHub](https://github.com/limenode/geneharmony/issues). For bugs, include the geneharmony version, your Python version, and a minimal example that reproduces the problem.

## Development setup

We use [pixi](https://pixi.sh) to manage the development environment. Fork the repository, then:

```bash
git clone https://github.com/<your-username>/geneharmony.git
cd geneharmony
pixi install
```

This installs geneharmony in editable mode along with its dependencies. Run code inside the environment with:

```bash
pixi run python <script>
```

## Code style

Code is linted with [ruff](https://docs.astral.sh/ruff/). Please run it before opening a pull request:

```bash
ruff check src
```

Use type hints for new code, and keep comments for logic that isn't obvious from the code itself.

## Pull requests

1. Create a branch from `main`.
2. Make your changes, keeping each pull request focused on one change.
3. Open a pull request against `main` with a short description of what changed and why.
