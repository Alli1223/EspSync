"""EspSync - low-latency screen-to-light sync for ESPHome lights."""

__version__ = "0.4.0"

try:  # Installer builds stamp their exact version (e.g. "0.2.0", or "0.1.0.57" from main).
    from espsync_build import VERSION as __version__  # type: ignore[import-not-found,no-redef]
except ImportError:
    pass
