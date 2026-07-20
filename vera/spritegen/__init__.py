"""spritegen — structured character → sprite-sheet / companion-package pipeline.

This directory is a regular package so its stage modules can import one another
via ``from Vera.vera.spritegen import <module>``. Only ``spritegen_capabilities``
is registered in ``capability_orchestration._module_files`` (loaded by basename),
so the @capability decorators run exactly once; everything else is a plain helper
imported through this package.
"""
