# -*- coding: utf-8 -*-
"""Static checks on the OPNsense plugin sources."""
import os
import re
import shlex
import xml.etree.ElementTree as ET

import pytest

PLUGIN = os.path.join(os.path.dirname(__file__), '..', 'plugin')
SRC = os.path.join(PLUGIN, 'src')
TEMPLATES = os.path.join(SRC, 'opnsense', 'service', 'templates', 'OPNsense', 'UnifiGateway')
MODEL = os.path.join(SRC, 'opnsense', 'mvc', 'app', 'models', 'OPNsense', 'UnifiGateway')


def _files(suffix):
    for root, _, files in os.walk(SRC):
        for name in files:
            if name.endswith(suffix):
                yield os.path.join(root, name)


@pytest.mark.parametrize('path', list(_files('.xml')))
def test_xml_is_well_formed(path):
    ET.parse(path)


def test_form_fields_exist_in_model():
    model = ET.parse(os.path.join(MODEL, 'UnifiGateway.xml')).getroot()
    fields = {f.tag for f in model.find('items/general')}
    form = ET.parse(os.path.join(SRC, 'opnsense', 'mvc', 'app', 'controllers', 'OPNsense',
                                 'UnifiGateway', 'forms', 'general.xml')).getroot()
    for field in form.iter('id'):
        assert field.text.startswith('unifigateway.general.')
        assert field.text.split('.')[-1] in fields


def test_targets_reference_existing_templates():
    with open(os.path.join(TEMPLATES, '+TARGETS')) as f:
        for line in f:
            if line.strip():
                assert os.path.exists(os.path.join(TEMPLATES, line.split(':')[0]))


class Helpers:

    def __init__(self, data):
        self.data = data

    def exists(self, path):
        node = self.data
        for part in path.split('.'):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True


def _render(general):
    jinja2 = pytest.importorskip('jinja2')
    with open(os.path.join(TEMPLATES, 'unifi_gateway')) as f:
        template = jinja2.Template(f.read())
    data = {'OPNsense': {'unifigateway': {'general': general}}} if general is not None else {}
    return template.render(helpers=Helpers(data), **data)


def _rc_vars(text):
    return dict(shlex.split(line)[0].split('=', 1)
                for line in text.splitlines() if line.strip())


def test_rc_conf_enabled():
    rendered = _rc_vars(_render({
        'enabled': '1', 'inform_url': 'http://unifi.lan:8080/inform',
        'wan_interface': 'wan', 'lan_interface': 'opt2',
        'firmware': None, 'hostname': 'edge',
    }))
    assert rendered['unifi_gateway_enable'] == 'YES'
    env = dict(item.split('=', 1) for item in rendered['unifi_gateway_env'].split())
    assert env == {
        'UNIFI_GW_SOURCE': 'local', 'OPNSENSE_WAN_INTERFACE': 'wan',
        'OPNSENSE_LAN_INTERFACE': 'opt2',
        'UNIFI_ADOPT_URL': 'http://unifi.lan:8080/inform', 'UNIFI_GW_HOSTNAME': 'edge',
    }


def test_rc_conf_disabled_or_missing():
    assert _rc_vars(_render({'enabled': '0'})) == {'unifi_gateway_enable': 'NO'}
    assert _rc_vars(_render(None)) == {'unifi_gateway_enable': 'NO'}


def test_env_names_are_understood_by_daemon():
    with open(os.path.join(TEMPLATES, 'unifi_gateway')) as f:
        names = set(re.findall(r'\b([A-Z_]{6,})=', f.read()))
    import importlib
    import sys
    sys.modules.pop('unifi_gateway', None)
    gateway = importlib.import_module('unifi_gateway')
    known = set(gateway.ENV_OVERRIDES) | {'UNIFI_ADOPT_URL'}
    sys.modules.pop('unifi_gateway', None)
    assert names <= known, names - known


def test_package_bundles_every_module():
    """build-pkg.sh lists the daemon's modules by name; a missing one only
    shows up as an ImportError on the router."""
    root = os.path.join(PLUGIN, '..')
    with open(os.path.join(PLUGIN, 'build-pkg.sh')) as f:
        bundled = set(re.search(r'for f in ([^;]+); do', f.read()).group(1).split())
    modules = {name for name in os.listdir(root) if name.endswith('.py')}
    assert modules <= bundled


def _package_script(name):
    with open(os.path.join(PLUGIN, 'build-pkg.sh')) as f:
        text = f.read()
    return re.search(r'"\$META/\+%s" <<\'EOF\'\n(.*?)\nEOF\n' % name, text, re.S).group(1)


def test_upgrade_restarts_enabled_service():
    """pkg upgrade runs the old package's PRE_DEINSTALL, which stops the
    service, and then the new one's POST_INSTALL, which must start it again;
    only once the template reload has written rc.conf.d, and only if enabled."""
    assert 'rc.d/unifi_gateway stop' in _package_script('PRE_DEINSTALL')
    lines = _package_script('POST_INSTALL').splitlines()
    reload = next(i for i, line in enumerate(lines) if 'template reload OPNsense/UnifiGateway' in line)
    start = next(i for i, line in enumerate(lines) if 'rc.d/unifi_gateway restart' in line)
    assert reload < start
    assert 'rc.d/unifi_gateway enabled' in lines[start]
