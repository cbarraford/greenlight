# Contributing

Issues and pull requests are welcome. For anything large, open an issue first.

- Setup, layout and tests are in [README.md](README.md). Run `make setup-hooks` once per clone.
- The quality bar, the privacy rules for the diagnostic trail, and the checks to run before a PR are in [AGENTS.md](AGENTS.md). Coding agents read it too.
- `scripts/` is Python stdlib only. Keep functions small and add tests in `tests/test_<module>.py`.
- PRs from forks run CI on GitHub-hosted runners.

By contributing you agree your work is released under the [MIT License](LICENSE).
