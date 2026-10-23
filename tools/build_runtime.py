"""Setuptools publication hook; installed launchers never execute build config."""
from pathlib import Path

from setuptools.build_meta import (
    build_editable as build_editable,
)
from setuptools.build_meta import (
    build_sdist as build_sdist,
)
from setuptools.build_meta import (
    build_wheel as build_wheel,
)
from setuptools.build_meta import (
    get_requires_for_build_editable as get_requires_for_build_editable,
)
from setuptools.build_meta import (
    get_requires_for_build_sdist as get_requires_for_build_sdist,
)
from setuptools.build_meta import (
    get_requires_for_build_wheel as get_requires_for_build_wheel,
)
from setuptools.build_meta import (
    prepare_metadata_for_build_editable as prepare_metadata_for_build_editable,
)
from setuptools.build_meta import (
    prepare_metadata_for_build_wheel as prepare_metadata_for_build_wheel,
)
from setuptools.command.build_py import build_py

from codey.launcher.runtime import package_files
from tools.publish_runtime import ROOT, publish_runtime


class BuildRuntime(build_py):
    def run(self) -> None:
        super().run()
        if self.editable_mode:
            return
        target = Path(self.build_lib).resolve()
        if target == ROOT:
            raise ValueError("Wheel output must be separate from its source")
        # Reused build directories must not retain obsolete hashed UI assets.
        wanted = set(package_files(ROOT))
        for name in package_files(target):
            if name not in wanted:
                (target / name).unlink()
        publish_runtime(target, ROOT / "pyproject.toml", version=self.distribution.get_version())
