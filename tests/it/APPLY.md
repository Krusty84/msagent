# Apply growth-visualization bundle

This zip is the **picture layer**: HTML/JS renderer, CLI `viz` hook, the
8th intensive test, and the testing procedures doc. 

## Run

```
python -m pytest tests/it/exgraph/test_evolver_value.py -q --noconftest
# expected: 8 passed

EXGRAPH_GROWTH_HTML=$PWD/artifacts/exgraph_growth_demo.html \
  python -m pytest tests/it/exgraph/test_evolver_value.py::test_growth_html_highlights_new_over_trajectories \
  -q --noconftest

python -m msagent.exgraph.visualize \
  --fixtures tests/fixtures/trajectories \
  -o artifacts/exgraph_growth_demo.html
```

Open `exgraph_growth_demo.html` in a browser (no server, no CDN). Press Play.

Gold ring + `NEW ·` = appeared on that trajectory.
Step 2 = Recipe. Step 3 = Accuracy control (recipes must not leak).

Full procedure: `exgraph_intensive_testing_procedures.md` §6–§7.

Nothing in this zip is pushed to GitHub from the authoring side.
