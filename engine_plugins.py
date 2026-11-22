"""Which plugin packages this build loads (see core/plugins.py).

Empty: the engine runs on its own. To add product-specific routes, voice
builders or seeders, put them in a package and list its name here.
"""
PLUGINS: tuple[str, ...] = ()
