import importlib.util
from pathlib import Path
import tempfile
import unittest

try:
    import yaml
except ImportError:
    yaml = None


@unittest.skipIf(yaml is None, 'DSH setup YAML dependency unavailable')
class DesktopConfigTests(unittest.TestCase):
    def test_update_preserves_other_plugins_and_existing_scope_and_backups_original(self):
        spec = importlib.util.spec_from_file_location('configure_desktop', Path(__file__).resolve().parents[2] / 'scripts/configure-desktop.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = '- id: other\n  config:\n    value: keep\n- id: bili-agent\n  name: dsh-bili-agent\n  config:\n    provider: keep-provider\n    dmAllowedPeers: "22"\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cordis.patch.yml'
            preview = Path(directory) / 'prepared.yml'
            path.write_text(original, encoding='utf-8')
            module.configure(path, '1092209523', preview)
            self.assertEqual(path.read_text(encoding='utf-8'), original)
            settings = module.configure(path, '1092209523', apply=True)
            self.assertTrue(settings['enablePanel'])
            self.assertTrue(settings['dmSendEnabled'])
            self.assertEqual(settings['dmAllowedPeers'], '22,1092209523')
            data = yaml.safe_load(path.read_text(encoding='utf-8'))
            self.assertEqual(data[0], yaml.safe_load(original)[0])
            self.assertEqual(data[1]['config']['provider'], 'keep-provider')
            self.assertEqual(next(Path(directory).glob('*.bak')).read_text(encoding='utf-8'), original)
            with self.assertRaisesRegex(ValueError, 'INVALID_RECIPIENT_UID'):
                module.configure(path, 'nickname', apply=True)
