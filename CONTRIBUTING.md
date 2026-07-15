# Contributing to rl-gym

Thanks for your interest in contributing!

## License
rl-gym is licensed under **AGPL-3.0** (see [LICENSE](LICENSE)). Commercial licenses are
available for use cases the AGPL doesn't fit — see the **License** section in the
[README](README.md).

## Contributor License Agreement (required)
Because future versions of rl-gym may be offered under different license terms, **every
contribution requires agreement to our [Contributor License Agreement](CLA.md).** It's short:
you keep ownership of your work, and you grant the maintainer the right to license your
contribution — including relicensing future versions.

**How to agree:** by opening a pull request, you confirm that you have read and agree to the
[CLA](CLA.md). (An automated CLA check may also ask you to sign once on your first PR.)

If you can't agree to the CLA, please open an issue instead of a pull request — we're happy
to discuss the idea and implement it independently.

## Making a change
- For anything non-trivial, **open an issue first** to discuss the approach.
- Keep pull requests focused and small; match the style of the surrounding code.
- Run the offline smoke test before submitting:
  ```bash
  python tests/iac_smoke_test.py
  ```
- Note that the scanner rules, reward, and gates are the core contract — changes there should
  come with tests in `tests/`.
