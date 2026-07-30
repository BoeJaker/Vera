"""vera.operator.docs — documentation generation built on the operator.

The documentation *mission* uses the operator to screenshot Vera's own UI, then
these helpers turn the captures + the live capability registry into GitHub-ready
markdown:

  • :mod:`domain_map`  — the 34 documentation domains ↔ panels ↔ capability groups
  • :mod:`doc_scaffold` — inject/update managed auto-blocks in each doc file
  • :mod:`gallery`     — build the top-level screenshot gallery index
"""
