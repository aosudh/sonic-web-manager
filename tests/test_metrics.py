"""Host sampling and PMON normalization; no switch or hardware access."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from server.metrics import MetricsSampler, cpu_counters, cpu_percent, read_proc, sample_timestamp, temperature_row
from server.sonic import SonicAdapter


class MetricsTests(unittest.TestCase):
    def test_cpu_excludes_guest_double_count_and_treats_iowait_as_idle(self):
        old = cpu_counters('cpu  100 10 20 200 30 2 3 5 100 10\ncpu0 1 1 1 1')
        new = cpu_counters('cpu  120 10 30 220 40 2 3 5 120 10')
        self.assertEqual(cpu_percent(old, new), 50.0)
        self.assertEqual(len(old), 8)
        self.assertIsNone(cpu_percent(new, old))
        self.assertIsNone(cpu_percent(old, old))
        with self.assertRaises(ValueError):
            cpu_counters('cpu 1 2 -1 3')

    def test_proc_failure_isolated_and_invalid_memory_never_fabricated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'meminfo').write_text('MemTotal: 100 kB\nMemAvailable: 75 kB\n')
            (root/'uptime').write_text('23.9 11.0')
            data = read_proc(root)
            self.assertIsNone(data['cpu'])
            self.assertEqual(data['memory_percent'], 25.0)
            self.assertEqual(data['uptime'], 23)
            self.assertEqual(data['errors'], ['cpu'])
            (root/'meminfo').write_text('MemTotal: 0 kB\nMemAvailable: 0 kB\n')
            (root/'uptime').write_text('nan 1')
            data = read_proc(root)
            self.assertIsNone(data['memory_percent'])
            self.assertIsNone(data['uptime'])

    def test_initial_reset_missing_sample_recovery_and_staleness(self):
        now = [10.0]
        counters = [(100,0,0,100,0,0,0,0), (110,0,0,110,0,0,0,0),
                    (1,0,0,1,0,0,0,0), (11,0,0,11,0,0,0,0), None,
                    (20,0,0,20,0,0,0,0), (30,0,0,30,0,0,0,0)]
        sampler = MetricsSampler(reader=lambda: {'cpu':counters.pop(0),'memory_percent':23,'uptime':12},
                                 monotonic=lambda:now[0])
        self.addCleanup(sampler.close)
        for state in ('warming_up','ok','unknown','ok','unknown','warming_up','ok'):
            sampler.sample_once()
            value = sampler.snapshot()
            self.assertEqual(value['cpu_state'], state)
            self.assertEqual(value['cpu_percent'], 50.0 if state=='ok' else None)
            self.assertEqual(value['memory_percent'], 23)
            now[0] += 2
        now[0] += 20
        stale = sampler.snapshot()
        self.assertEqual(stale['cpu_state'], 'stale')
        self.assertIsNone(stale['cpu_percent'])
        self.assertIsNone(stale['memory_percent'])
        sampler.close()
        self.assertEqual(sampler.snapshot()['cpu_state'], 'stopped')

    def test_sampling_runs_without_requests_and_closes_once(self):
        calls = []
        sampled = threading.Event()
        def reader():
            calls.append(len(calls)+1)
            if len(calls)>=4:
                sampled.set()
            return {'cpu':(calls[-1]*10,0,0,calls[-1]*10,0,0,0,0), 'memory_percent':10,'uptime':calls[-1]}
        sampler = MetricsSampler(interval=.02, initial_interval=.01, reader=reader)
        self.addCleanup(sampler.close)
        sampler.start()
        first_thread = sampler._thread
        sampler.start()
        self.assertIs(sampler._thread, first_thread)
        self.assertTrue(sampled.wait(1))  # No browser snapshots trigger these samples.
        first = sampler.snapshot()
        self.assertEqual(first['cpu_state'], 'ok')
        self.assertEqual(first['cpu_percent'], 50.0)
        first['cpu_percent'] = -1
        self.assertEqual(sampler.snapshot()['cpu_percent'], 50.0)
        sampler.close()
        self.assertFalse(first_thread.is_alive())
        count = len(calls)
        sampler.start()
        time.sleep(.03)
        self.assertEqual(len(calls), count)

    def test_reader_exception_is_explicit_unknown(self):
        def broken():
            raise OSError('test')
        sampler = MetricsSampler(reader=broken)
        self.addCleanup(sampler.close)
        sampler.sample_once()
        value = sampler.snapshot()
        self.assertEqual(value['cpu_state'], 'unknown')
        self.assertIsNone(value['cpu_percent'])
        self.assertIsNotNone(value['memory_warning'])


class TemperatureTests(unittest.TestCase):
    now = datetime(2026,11,18,15,0,tzinfo=timezone.utc)

    def row(self, **values):
        return temperature_row('ASIC External Front', {'temperature':'35.5','warning_status':'False',
            'timestamp':self.now.strftime('%Y%m%d %H:%M:%S'), 'high_threshold':'77',
            'critical_high_threshold':'80', **values}, now=self.now)

    def test_float_zero_unknown_and_nonfinite_are_distinct(self):
        row = self.row()
        self.assertEqual(row['temperature'],35.5)
        self.assertEqual(row['high_threshold'],77.0)
        self.assertEqual(row['status'],'正常')
        self.assertEqual(row['unit'],'°C')
        self.assertIn('非芯片内部结温', row['sensor_note'])
        self.assertEqual(self.row(temperature='0')['temperature'],0.0)
        for value in ('N/A','nan','inf',None,True):
            with self.subTest(value=value):
                row = self.row(temperature=value)
                self.assertIsNone(row['temperature'])
                self.assertEqual(row['status'],'无有效读数')
                json.dumps(row,allow_nan=False)

    def test_thresholds_missing_warning_and_freshness(self):
        self.assertEqual(self.row(temperature='77')['status'],'告警')
        self.assertEqual(self.row(temperature='80')['status'],'严重告警')
        self.assertEqual(self.row(warning_status=None)['status'],'未知')
        for stamp in (None, 'invalid', '20261118 14:56:59', '20261118 15:00:11'):
            row = self.row(timestamp=stamp)
            self.assertFalse(row['fresh'])
            self.assertEqual(row['status'],'数据过期或时间未知')
        row = self.row(timestamp=(self.now-timedelta(seconds=90)).isoformat())
        self.assertEqual(row['age_seconds'],90.0)
        self.assertTrue(row['fresh'])
        self.assertTrue(row['timestamp'].endswith('+00:00'))

    def test_unix_seconds_float_string_and_integer_are_normalized(self):
        stamp = datetime.fromtimestamp(1795016571.468196, timezone.utc)
        now = stamp + timedelta(seconds=12)
        for raw in (1795016571.468196, '1795016571.468196'):
            with self.subTest(raw=raw):
                actual, age, fresh = sample_timestamp(raw, now)
                self.assertEqual(actual, stamp.isoformat())
                self.assertEqual(age, 12.0)
                self.assertTrue(fresh)
        for raw in (1795016571, '1795016571'):
            actual, age, fresh = sample_timestamp(raw, now)
            self.assertEqual(actual, datetime.fromtimestamp(1795016571,timezone.utc).isoformat())
            self.assertEqual(age, 12.5)
            self.assertTrue(fresh)

    def test_unix_seconds_future_stale_and_invalid_values_are_not_fresh(self):
        for delta in (11, -181):
            raw = (self.now + timedelta(seconds=delta)).timestamp()
            actual, age, fresh = sample_timestamp(str(raw),self.now)
            self.assertIsNotNone(actual)
            self.assertEqual(age, -delta)
            self.assertFalse(fresh)
        for raw in (None, True, False, '', 'nan', float('nan'), float('inf'),
                    '-inf', -1, 0, 946684799, 4102444800, 1795016571468.196,
                    '1795016571468.196', '1795016571 seconds', 10**400):
            with self.subTest(raw=raw):
                self.assertEqual(sample_timestamp(raw,self.now),(None,None,False))


class MetricsAdapterTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.adapter = SonicAdapter(folder.name,demo=True)
        self.addCleanup(self.adapter.close)

    def test_cached_overview_gets_current_metrics_even_when_base_is_cached(self):
        with patch.object(self.adapter,'_system_metrics',side_effect=[
                {'cpu_percent':None,'metrics_sampled_at':'first','cpu_state':'warming_up'},
                {'cpu_percent':42.5,'metrics_sampled_at':'second','cpu_state':'ok'}]):
            first, second = self.adapter.overview(), self.adapter.overview()
        self.assertEqual(first['sampled_at'],second['sampled_at'])
        self.assertIsNone(first['device']['cpu_percent'])
        self.assertEqual(second['device']['cpu_percent'],42.5)
        self.assertEqual(second['device']['metrics_sampled_at'],'second')

    def test_demo_metrics_never_start_host_sampler(self):
        with patch.object(self.adapter._metrics_sampler,'start',side_effect=AssertionError('host sampler in demo')):
            self.assertEqual(self.adapter.overview()['device']['cpu_state'],'ok')
            page = self.adapter.page('status.cpu')
        self.assertEqual(page['metrics']['cpu_percent'],21.0)
        self.assertTrue(page['metrics']['simulated'])

    def test_temperature_page_and_bmc_share_numeric_units_and_health(self):
        now = datetime.now(timezone.utc).strftime('%Y%m%d %H:%M:%S')
        values = {'normal':{'temperature':'31.5','timestamp':now,'warning_status':'False','high_threshold':'77'},
                  'missing':{'temperature':'N/A','timestamp':now,'warning_status':'False'}}
        with patch.object(self.adapter,'_optional_table',side_effect=lambda db,table:(values if table=='TEMPERATURE_INFO' else {},None)):
            page = self.adapter.page('status.temperature')
            bmc = self.adapter._bmc_sensors()
        rows = {r['name']:r for r in page['rows']}
        sensors = {r['name']:r for r in bmc['rows']}
        self.assertEqual(rows['normal']['temperature'],31.5)
        for name in values:
            self.assertEqual(rows[name]['temperature'],sensors[name]['value'])
            self.assertEqual(rows[name]['status'],sensors[name]['status'])
        columns = {c['key']:c for c in page['columns']}
        self.assertEqual(columns['temperature']['type'],'temperature')
        self.assertEqual(columns['high_threshold']['unit'],'°C')
        self.assertEqual(rows['missing']['status'],'无有效读数')
        json.dumps(page,allow_nan=False)

    def test_bmc_psu_unix_timestamp_is_not_unknown(self):
        now = datetime.now(timezone.utc)
        raw = str(now.timestamp())
        values = {'PSU 1':{'timestamp':raw,'power':'71.5','presence':'True','status':'True'}}
        with patch.object(self.adapter,'_optional_table',side_effect=lambda db,table:(values if table=='PSU_INFO' else {},None)):
            row = self.adapter._bmc_sensors()['rows'][0]
        self.assertEqual(row['status'],'正常')
        self.assertTrue(row['fresh'])
        self.assertEqual(row['raw_timestamp'],raw)
        self.assertEqual(row['timestamp'],now.isoformat())


if __name__ == '__main__':
    unittest.main()
