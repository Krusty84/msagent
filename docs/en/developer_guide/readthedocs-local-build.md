# ReadTheDocs Local Build

Use this procedure to reproduce the repository's Sphinx build locally. It creates an HTML preview but does not publish documentation.

## Prepare the Environment

The root `.readthedocs.yaml` selects Python 3.11, `docs/conf.py`, and `docs/requirements.txt`. From the repository root, install the same documentation dependencies:

```bash
python -m pip install -r docs/requirements.txt
```

## Build HTML

```bash
python -m sphinx -E -b html docs docs/_build/html
```

The build succeeds when the command exits with code 0. Review warnings to ensure the current change did not introduce a new broken link, missing `toctree` target, or parser error.

## Preview the Result

Open `docs/_build/html/index.html` directly, or serve the generated directory:

```bash
cd docs/_build/html
python -m http.server 8000
```

Then open `http://localhost:8000/`.

## Clean Generated Files

Stop the preview server with `Ctrl+C`, then run the cleanup command from the repository root:

```bash
rm -rf docs/_build
```

Do not commit generated files. When the project has been imported into ReadTheDocs and its repository integration is active, pushes trigger remote builds using `.readthedocs.yaml`.

## References

- [ReadTheDocs configuration reference](https://docs.readthedocs.io/en/stable/config-file/v2.html)
- [Sphinx documentation](https://www.sphinx-doc.org/)
