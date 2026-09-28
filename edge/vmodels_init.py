"""vmodels - the model-package helpers the builder ships beside model_builder.py.

Deployed as vmodels/__init__.py next to vmodels/nlp_inventory.py and
vmodels/model_package.py (copies of vera/models/*): the builder needs
package_nlp_directory() to write content-verified packages into the store's
manifest, without shipping vera/models/__init__.py and everything it imports.
"""
