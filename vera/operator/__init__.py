"""vera.operator — general observe→think→act web/computer operator.

The operator lets Vera drive any web page (or a VM/desktop served through a web
page) the way a person would: it *observes* (screenshot + accessibility/DOM
tree), *thinks* (a provider-pluggable LLM picks the next action), and *acts*
(click / type / scroll / navigate …). The primitives are a clean capability
toolkit; ``operator.run`` is a dedicated loop that drives them; and the existing
agent-loop engine can drive the same primitives via the ``operator`` loop
profile.

Documentation generation is the first *mission* built on top (see
``vera.operator.missions.documentation``): it navigates Vera's own UI, seeds
representative data, screenshots every panel, and regenerates the docs.
"""
