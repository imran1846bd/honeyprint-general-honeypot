"""Read HoneyPrint YAML configuration without executing application code.

Requires PyYAML. This module handles file and YAML errors only. Schema,
platform capabilities and deployment compatibility belong to the validator.
Custom adapter identifiers are preserved for later registry resolution.
"""

from pathlib import Path
from typing import Any, Union

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode


class ConfigLoadError(ValueError):
    """A configuration file could not be read or parsed unambiguously."""


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Use safe YAML constructors and reject duplicate mapping keys."""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict:
        if not isinstance(node, MappingNode):
            raise ConstructorError(
                None, None, "expected a YAML mapping", node.start_mark
            )
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in result
            except TypeError as exc:
                raise ConstructorError(
                    "while reading a mapping",
                    node.start_mark,
                    "mapping keys must be hashable",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise ConstructorError(
                    "while reading a mapping",
                    node.start_mark,
                    "duplicate mapping key",
                    key_node.start_mark,
                )
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def load_config(path: Union[str, Path]) -> dict[str, Any]:
    """Load one UTF-8 YAML document whose root is a string-keyed mapping.

    Raises ConfigLoadError for unreadable files, invalid encoding, empty input,
    unsafe YAML tags, duplicate keys, multiple documents or an invalid root.
    YAML merge overrides are rejected when they produce duplicate keys.
    No adapter is imported or launched, and no configuration defaults are added.
    Error messages identify locations without echoing configuration values.
    """
    config_path = Path(path)
    try:
        content = config_path.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise ConfigLoadError(
            f"Configuration must be UTF-8: {config_path}"
        ) from exc
    except OSError as exc:
        raise ConfigLoadError(
            f"Cannot read configuration: {config_path} ({exc.strerror})"
        ) from exc

    try:
        config = yaml.load(content, Loader=_UniqueKeySafeLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = (
            f" at line {mark.line + 1}, column {mark.column + 1}"
            if mark is not None else ""
        )
        raise ConfigLoadError(
            f"Invalid or ambiguous YAML in {config_path}{location}"
        ) from exc

    if config is None:
        raise ConfigLoadError(f"Configuration is empty: {config_path}")
    if not isinstance(config, dict):
        raise ConfigLoadError(
            f"Configuration root must be a mapping: {config_path}"
        )
    if any(not isinstance(key, str) for key in config):
        raise ConfigLoadError(
            f"Configuration root keys must be strings: {config_path}"
        )
    return config
