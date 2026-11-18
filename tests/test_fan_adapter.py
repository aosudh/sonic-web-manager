import tempfile
import unittest
from unittest.mock import patch
from server.sonic import SonicAdapter, AdapterError, _revision


class FanAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.adapter = SonicAdapter(self.temp.name, demo=True)

    def tearDown(self):
        self.adapter.close()
        self.temp.cleanup()

    def test_simulated_fan_setting_does_not_dirty_network_config(self):
        before = _revision(self.adapter._config())
        plan = self.adapter.plan('fan.control', {'mode': 'fixed', 'duty_percent': 40})
        self.assertEqual(self.adapter._fan_status()['mode'], 'auto')
        result = self.adapter.apply(plan['id'])
        self.assertTrue(result['success'])
        self.assertEqual(self.adapter._fan_status()['duty_percent'], 40)
        self.assertEqual(_revision(self.adapter._config()), before)
        self.assertFalse(result['pending_save'])

    def test_stale_fan_plan_is_rejected_even_if_network_revision_unchanged(self):
        first = self.adapter.plan('fan.control', {'mode': 'fixed', 'duty_percent': 40})
        second = self.adapter.plan('fan.control', {'mode': 'fixed', 'duty_percent': 50})
        self.adapter.apply(first['id'])
        with self.assertRaises(AdapterError):
            self.adapter.apply(second['id'])
        self.assertEqual(self.adapter._fan_status()['duty_percent'], 40)

    def test_live_controller_is_never_simulated(self):
        live = SonicAdapter(self.temp.name, demo=False)
        config = self.adapter._config()
        with patch.object(live, '_config', return_value=config), patch.object(live, '_multi_asic', return_value=False), patch.object(live, '_run') as run:
            self.assertFalse(live._action_available('fan.control'))
            with self.assertRaises(AdapterError):
                live.plan('fan.control', {'mode': 'fixed', 'duty_percent': 40})
            run.assert_not_called()
        self.assertIsNone(live._fan_control)

    def test_fan_speed_column_has_percentage_semantics(self):
        page = self.adapter.page('status.fans')
        column = next(c for c in page['columns'] if c['key'] == 'speed')
        self.assertEqual(column['type'], 'percent')
        self.assertTrue(page['control']['simulated'])


if __name__ == '__main__':
    unittest.main()
