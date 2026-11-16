"""SQL identifiers are schema metadata, never value parameters."""
import re


def oracle_identifier(name: str) -> str:
    # Django's generated identifiers use this alphabet. Fail closed on unusual
    # quoted names instead of interpolating SQL punctuation from schema metadata.
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$#]*', name):
        raise ValueError('Unsupported Oracle schema identifier')
    return '"' + name + '"'
