# -*- coding: utf-8 -*-
"""OPNsense data collection.

Usage:
    from collectors import create_collector
    dc = create_collector(config, device)
"""
from .opnsense import OPNsenseCollector
from .source import SourceError

__all__ = ['OPNsenseCollector', 'SourceError', 'create_collector', 'create_source']


def create_source(config):
    """[opnsense] source = api (default, works off-box) or local (on the
    router itself, as the OPNsense plugin runs)."""
    kind = config.get('opnsense', 'source', fallback='api').strip().lower()
    if kind == 'local':
        from .local_source import LocalSource
        return LocalSource()
    if kind != 'api':
        raise ValueError('Unknown [opnsense] source %r: use api or local' % kind)
    from .api_source import create_api_source
    return create_api_source(config)


def create_collector(config, device):
    return OPNsenseCollector(
        config, create_source(config),
        wan_port=device.wan_port, lan_port=device.lan_port,
    )
