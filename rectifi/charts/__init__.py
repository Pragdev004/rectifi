"""Chart harness: the model writes plotting code, a fixed harness checks and runs it.

    validate.py  AST allow-list (pure, stdlib only)
    sandbox.py   runs checked code in a limited subprocess (pure, stdlib only); _child.py is that subprocess
    checks.py    looks at the finished figure; every rupee figure in its text must trace to Rectifi's records
    theme.py     Passbook colours for the PNGs
    graph.py     the LangGraph loop: write, validate, run, check, repair (the only module here that loads langchain)

This file imports nothing, so loading `rectifi.charts.validate` never loads a model library.
"""
