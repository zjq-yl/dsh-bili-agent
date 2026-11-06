"""Prepare or apply the requested desktop plugin settings, preserving other plugins."""
import argparse
import copy
import datetime
import json
import os
from pathlib import Path
import re

import yaml


def update_document(text: str, peer_uid: str) -> tuple[str, dict]:
    """Enable the requested recipient and panel in the installed plugin entry."""
    if not re.fullmatch(r'[1-9][0-9]{0,29}', peer_uid):
        raise ValueError('INVALID_RECIPIENT_UID')
    document = yaml.safe_load(text)
    if not isinstance(document, list):
        raise ValueError('EXPECTED_DESKTOP_PATCH_LIST')
    updated = copy.deepcopy(document)
    entries = [entry for entry in updated if isinstance(entry, dict)
               and entry.get('id') == 'bili-agent' and entry.get('name') == 'dsh-bili-agent']
    if len(entries) != 1:
        raise ValueError('EXPECTED_ONE_INSTALLED_BILI_AGENT')
    config = entries[0].setdefault('config', {})
    if not isinstance(config, dict):
        raise ValueError('INVALID_PLUGIN_CONFIG')
    peers = str(config.get('dmAllowedPeers') or '')
    allowed = [value for value in re.split(r'[,，\s]+', peers) if value]
    if any(not re.fullmatch(r'[1-9][0-9]{0,29}', value) for value in allowed):
        raise ValueError('INVALID_EXISTING_RECIPIENT_SCOPE')
    config.update(dmSendEnabled=True, dmAllowedPeers=','.join(dict.fromkeys([*allowed, peer_uid])),
                  enablePanel=True, panelPort=0)
    rendered = yaml.safe_dump(updated, allow_unicode=True, sort_keys=False)
    assert yaml.safe_load(rendered) == updated
    return rendered, config


def configure(path: Path, peer_uid: str, output: Path | None = None, apply: bool = False) -> dict:
    """Prepare settings, optionally replacing the profile after an original-file backup."""
    original = path.read_text(encoding='utf-8-sig')
    rendered, config = update_document(original, peer_uid)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding='utf-8')
    if apply:
        if path.read_text(encoding='utf-8-sig') != original:
            raise ValueError('CONFIG_CHANGED_DURING_UPDATE')
        backup = path.with_name(path.name + '.' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
        backup.write_text(original, encoding='utf-8')
        temporary = path.with_name(path.name + '.bili-agent.tmp')
        try:
            temporary.write_text(rendered, encoding='utf-8')
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return {**{key: config[key] for key in ('enablePanel', 'panelPort', 'dmSendEnabled', 'dmAllowedPeers')},
            'data_dir': config.get('dataDir') or str(path.parents[2] / 'plugins-data/bili-agent')}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', type=Path, default=Path.home() / '.dsh/profiles/desktop/cordis.patch.yml')
    parser.add_argument('--peer-uid', required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    settings = configure(args.profile, args.peer_uid, args.output, args.apply)
    print(json.dumps({'applied': args.apply, 'settings': settings}, ensure_ascii=False))


if __name__ == '__main__':
    main()
