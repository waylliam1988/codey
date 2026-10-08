"""Explicit model consent for scripted fixtures, independent of discovery."""
from codey.providers.model_preferences import ModelPreferences


def enable_models(state_home, **choices):
    store = ModelPreferences(state_home)
    view = store.snapshot()
    for source, models in choices.items():
        view["sources"][source] = {"enabled": True, "models": models}
    store.save(view["sources"], base_revision=view["revision"])
