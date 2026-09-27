"""Chat provider package.

The provider package contains both small data modules and heavyweight web
drivers. Import from the defining leaf module directly
(e.g. ``codey.providers.base``, ``codey.providers.catalog``,
``codey.providers.registry``, ``codey.providers.web_provider``);
this package keeps no convenience re-export layer so importing a leaf
never pulls in every browser driver. Submodule imports such as
``from codey.providers import controls`` remain supported.
"""
