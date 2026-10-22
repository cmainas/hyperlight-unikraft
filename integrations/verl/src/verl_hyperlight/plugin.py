"""Registers :func:`run_code` with verl for ``hyperlight://`` URLs, at verl's import.

verl imports this module through the ``verl.plugins`` entry points it loads at its own import (0.9.0 and
later). A verl with sandbox runners gets it registered for the ``hyperlight`` scheme; an earlier one only
knows Sandbox Fusion's HTTP service, and gets its ``call_sandbox_api`` wrapped so that a ``hyperlight://``
URL comes here and any other goes on as before.
"""

import functools
import logging

from .runner import is_hyperlight_url, run_code


logger = logging.getLogger(__name__)


def install() -> None:
    """Makes verl send ``hyperlight://`` URLs to :func:`run_code`."""
    try:
        from verl.utils.reward_score.sandbox_fusion import utils
    except ImportError as e:
        logger.debug("verl's sandbox_fusion reward is not importable, nothing to install: %s", e)
        return
    if hasattr(utils, "register_sandbox_runner"):
        utils.register_sandbox_runner("hyperlight", run_code)
        return
    if getattr(utils.call_sandbox_api, "_hyperlight", False):
        return
    original = utils.call_sandbox_api

    # The arguments pass through as they come, so a verl that changes call_sandbox_api's keeps its
    # other URLs working: only hyperlight:// ones need run_code's
    @functools.wraps(original)
    def call_sandbox_api(sandbox_fusion_url, *args, **kwargs):
        if is_hyperlight_url(sandbox_fusion_url):
            return run_code(sandbox_fusion_url, *args, **kwargs)
        return original(sandbox_fusion_url, *args, **kwargs)

    call_sandbox_api._hyperlight = True
    utils.call_sandbox_api = call_sandbox_api


try:
    install()
except Exception as e:  # verl's plugin loader would log this only at debug level
    logger.warning("verl-hyperlight could not route hyperlight:// URLs of this verl: %r", e)
