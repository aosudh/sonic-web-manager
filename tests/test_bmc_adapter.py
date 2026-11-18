"""BMC page integration: read-only, honest data ages, no hardware writes."""
from datetime import datetime, timezone
import json
import tempfile
import unittest
from unittest.mock import patch

from server.sonic import SonicAdapter, AdapterError, _revision


class BmcAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.adapter = SonicAdapter(self.temp.name, demo=True)
        self.addCleanup(self.adapter.close)

    def test_demo_page_is_read_only_and_preserves_switch_configuration(self):
        before = _revision(self.adapter._config())
        with patch('subprocess.run', side_effect=AssertionError('No host access in DEMO')):
            page = self.adapter.page('status.bmc')
        self.assertEqual(page['bmc']['mode'], 'demo')
        self.assertIn('DEMO', page['description'])
        self.assertFalse(page['capability']['write'])
        self.assertEqual(page['actions'], [])
        self.assertFalse(page['bmc']['console']['available'])
        self.assertTrue(page['bmc']['sensors']['rows'])
        self.assertEqual(_revision(self.adapter._config()), before)
        json.dumps(page, allow_nan=False)

    def test_no_bmc_shell_or_reset_action_is_registered(self):
        for action in ('bmc.reset', 'bmc.shell', 'bmc.console.send', 'bmc.network.set'):
            with self.subTest(action=action), self.assertRaises(AdapterError):
                self.adapter.plan(action, {'command': 'reboot'})

    def test_cached_sensor_age_and_missing_status_are_not_reported_healthy(self):
        self.adapter.demo = False
        now = datetime.now(timezone.utc).strftime('%Y%m%d %H:%M:%S')
        sample = {
            'FAN_INFO': {'fresh': {'timestamp': now, 'presence': 'True', 'status': 'True', 'speed': '39'},
                         'stale': {'timestamp': '20000101 00:00:00', 'presence': 'True', 'status': 'True', 'speed': '39'}},
            'TEMPERATURE_INFO': {'unknown': {'timestamp': now, 'temperature': '31'}},
            'PSU_INFO': {'absent': {'timestamp': now, 'presence': 'False', 'status': 'False'}}}
        with patch.object(self.adapter, '_optional_table', side_effect=lambda db, table: (sample[table], None)):
            rows = {row['name']: row for row in self.adapter._bmc_sensors()['rows']}
        self.assertEqual(rows['fresh']['status'], '正常')
        self.assertEqual(rows['fresh']['unit'], '%')
        self.assertTrue(rows['fresh']['timestamp'].endswith('+00:00'))
        self.assertEqual(rows['fresh']['raw_timestamp'], now)
        self.assertEqual(rows['stale']['status'], '数据过期或时间未知')
        self.assertEqual(rows['unknown']['status'], '未知')
        self.assertEqual(rows['absent']['status'], '未在位')


if __name__ == '__main__':
    unittest.main()
